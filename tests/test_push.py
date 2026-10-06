import tempfile
import unittest
from dataclasses import replace
from datetime import date

from fns_ws import NoteSyncMessage
from models import Task, calendar_components, task_uid
from push import CALDAV_MAPPING_VERSION, PushService
from state import SyncState
from tests.helpers import EVENTS, TASKS, MemoryDav, MemoryFns, MemoryWs, note


class PushServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SyncState.load(self.tmp.name + '/state.json')
        self.dav = MemoryDav()
        self.fns = MemoryFns(note())
        self.ws = MemoryWs()
        self.service = PushService(self.fns, self.dav, self.state, fns_ws=self.ws, tasks_collection=TASKS, events_collection=EVENTS)

    def test_initial_scan_caches_cursor_and_versions_without_backfilling_completed(self):
        self.fns.notes['Tasks/Done.md'] = note('Tasks/Done.md', '已完成')
        self.service.run_once()
        self.assertEqual(len(self.dav.puts), 1)
        self.assertTrue(self.state.initial_task_scan_completed())
        self.assertEqual(self.state.get_caldav_mapping_version(), CALDAV_MAPPING_VERSION)
        self.assertEqual(self.state.get_fns_note_sync_last_time(), 1000)

    def test_reference_and_cancellation_retire_actual_href_and_never_rebuild(self):
        for fm in [{'type': 'reference'}, {'tags': ['type/reference'], 'cancelled_date': '2026-09-08'}]:
            with self.subTest(fm=fm):
                self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 6, 10)), href=TASKS + 'client-chosen.ics')
                self.fns.notes['Tasks/A.md'] = replace(note(), frontmatter=fm)
                self.ws.messages = [NoteSyncMessage('NoteSyncModify', 'Tasks/A.md')]
                self.service.run_once()
                self.assertEqual(self.dav.objects, {})
                self.assertEqual(self.fns.patches, [])
                self.service.run_once(paths=['Tasks/A.md'])
                self.assertEqual(self.dav.objects, {})
                self.assertEqual(self.state.retirement('Tasks/A.md')['kinds'], ['VEVENT', 'VTODO'])

    def test_completed_task_keeps_completion_and_retires_legacy_event(self):
        self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 6, 10)))
        self.fns.notes['Tasks/A.md'] = note(status='已完成', due_date='2026-06-10', done_date='2026-06-01')
        self.service.run_once()
        self.assertEqual(len(self.dav.objects), 1)
        component = calendar_components(next(iter(self.dav.objects.values())).ics_text)[0]
        self.assertEqual(str(component['STATUS']), 'COMPLETED')
        self.assertEqual(component['COMPLETED'].dt.date(), date(2026, 6, 1))

    def test_partial_retirement_survives_restart_and_412(self):
        task = Task('Tasks/A.md', 'A', due_date=date(2026, 6, 10))
        self.dav.seed(task)
        self.fns.notes['Tasks/A.md'] = note(type='reference')
        event_href = self.dav.object_href(EVENTS, task.event_uid)
        self.dav.fail_delete.add(event_href)
        stats = self.service.run_once()
        self.assertEqual(stats.deleted_tasks, 1)
        self.assertEqual(stats.conflicts, 1)
        self.assertFalse(self.state.retirement(task.path)['settled'])
        self.dav.fail_delete.clear()
        loaded = SyncState.load(str(self.state.path))
        service = PushService(self.fns, self.dav, loaded, fns_ws=self.ws, tasks_collection=TASKS, events_collection=EVENTS)
        service.run_once()
        self.assertEqual(self.dav.objects, {})
        self.assertTrue(loaded.retirement(task.path)['settled'])

    def test_bad_metadata_and_unreadable_initial_note_stay_pending_after_version_advances(self):
        self.fns.notes['Tasks/Bad.md'] = note('Tasks/Bad.md', due_date='{{date}}')
        self.fns.notes['Tasks/Unavailable.md'] = note('Tasks/Unavailable.md')
        self.fns.fail_paths.add('Tasks/Unavailable.md')
        self.service.run_once()
        pending = self.state.get_fns_pending_note_changes()
        self.assertEqual({x['path'] for x in pending}, {'Tasks/Bad.md', 'Tasks/Unavailable.md'})
        self.assertEqual(len(self.dav.puts), 1)
        self.fns.notes['Tasks/Bad.md'] = note('Tasks/Bad.md')
        self.fns.fail_paths.clear()
        self.service.run_once()
        self.assertEqual(self.state.get_fns_pending_note_changes(), [])

    def test_interrupted_initial_scan_does_not_mark_migration_complete(self):
        self.fns.scan_fail = True
        with self.assertRaises(Exception):
            self.service.run_once()
        self.assertIsNone(self.state.get_caldav_mapping_version())
        self.assertFalse(self.state.initial_task_scan_completed())
        self.assertEqual(self.state.get_fns_pending_note_changes()[0]['path'], 'Tasks/A.md')

    def test_version_scan_preserves_existing_cursor_and_tracked_non_tasks(self):
        self.state.set_fns_note_sync_last_time(100)
        self.state.set_caldav_mapping_version(CALDAV_MAPPING_VERSION - 1)
        self.state.remember_uid(task_uid('Resources/Ref.md'), 'Resources/Ref.md')
        self.fns.notes['Resources/Ref.md'] = note('Resources/Ref.md', type='reference')
        self.dav.seed(Task('Resources/Ref.md', 'Ref'))
        self.service.run_once()
        self.assertEqual(self.state.get_fns_note_sync_last_time(), 100)
        self.assertFalse(any('Resources/Ref.md' in x.ics_text for x in self.dav.objects.values()))

    def test_terminal_task_needs_explicit_restore(self):
        self.fns.notes['Tasks/A.md'] = note(type='reference')
        self.service.run_once()
        self.fns.notes['Tasks/A.md'] = note()
        self.service.run_once(paths=['Tasks/A.md'])
        self.assertEqual(self.dav.puts, [])
        self.state.restore('Tasks/A.md')
        self.service.run_once()
        self.assertEqual(len(self.dav.puts), 1)

    def test_plain_remote_delete_is_not_local_cancellation(self):
        self.service.run_once()
        self.dav.objects.clear()
        self.service.run_once()
        self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['task_status'], '待办')
        self.assertEqual(self.dav.objects, {})
        self.service.run_once(paths=['Tasks/A.md'])
        self.assertEqual(len(self.dav.objects), 1)

    def test_confirmed_fns_deletion_requires_missing_source_and_ownership(self):
        self.service.run_once()
        self.fns.notes.clear()
        self.ws.messages = [NoteSyncMessage('NoteSyncDelete', 'Tasks/A.md')]
        self.service.run_once()
        self.assertEqual(self.dav.objects, {})
        self.assertEqual(self.state.note_record('Tasks/A.md')['terminal'], 'DELETED')

    def test_verified_rename_reuses_uids_and_survives_next_push(self):
        self.service.run_once()
        old_uid = task_uid('Tasks/A.md')
        old = self.fns.notes.pop('Tasks/A.md')
        self.fns.notes['Tasks/B.md'] = replace(old, path='Tasks/B.md')
        self.ws.messages = [NoteSyncMessage('NoteSyncDelete', 'Tasks/A.md'), NoteSyncMessage('NoteSyncRename', 'Tasks/B.md')]
        self.service.run_once()
        self.assertEqual(len(self.dav.objects), 1)
        component = calendar_components(next(iter(self.dav.objects.values())).ics_text)[0]
        self.assertEqual(str(component['UID']), old_uid)
        self.assertEqual(str(component['X-OBSIDIAN-PATH']), 'Tasks/B.md')
        self.ws.messages = []
        self.service.run_once(paths=['Tasks/B.md'])
        self.assertEqual(len(self.dav.objects), 1)

    def test_ambiguous_rename_does_not_create_a_duplicate(self):
        self.service.run_once()
        self.fns.notes.pop('Tasks/A.md')
        self.fns.notes['Tasks/B.md'] = note('Tasks/B.md', '进行中')
        self.ws.messages = [NoteSyncMessage('NoteSyncRename', 'Tasks/B.md')]
        self.service.run_once()
        self.assertEqual(len(self.dav.objects), 1)
        self.assertIn('Tasks/B.md', self.state.data['lifecycle']['reviews'])
        self.state.set_caldav_mapping_version(0)
        self.service.run_once()
        self.assertEqual(len(self.dav.objects), 1)

    def test_due_date_can_be_added_after_missing_event_was_retired(self):
        self.service.run_once()
        self.fns.notes['Tasks/A.md'] = note(due_date='2026-06-10')
        self.service.run_once(paths=['Tasks/A.md'])
        self.assertEqual(len(self.dav.objects), 2)

    def test_unacknowledged_phone_edit_is_not_overwritten_when_pull_has_not_run(self):
        self.service.run_once()
        href, old = next(iter(self.dav.objects.items()))
        from icalendar import Calendar
        cal = Calendar.from_ical(old.ics_text)
        next(c for c in cal.walk() if c.name == 'VTODO')['STATUS'] = 'COMPLETED'
        self.dav.objects[href] = replace(old, etag='phone-change', ics_text=cal.to_ical().decode())
        writes = len(self.dav.puts)
        self.service.run_once(paths=['Tasks/A.md'])
        self.assertEqual(len(self.dav.puts), writes)
        self.assertEqual(str(calendar_components(self.dav.objects[href].ics_text)[0]['STATUS']), 'COMPLETED')
        self.assertEqual(len(self.state.pending_pull()), 1)

    def test_fns_empty_singleton_due_date_does_not_leave_failed_retirement(self):
        self.fns.notes['Tasks/A.md'] = note(due_date=[None])
        self.service.run_once()
        self.assertEqual(self.state.get_fns_pending_note_changes(), [])
