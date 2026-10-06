import tempfile
import unittest
from dataclasses import replace
from datetime import date

from caldav_client import CollectionObject, RemoteObject
from models import Task, calendar_components
from pull import PullService
from state import SyncState
from tests.helpers import EVENTS, TASKS, MemoryDav, MemoryFns, note


class PullServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SyncState.load(self.tmp.name + '/state.json')
        self.dav = MemoryDav()
        self.fns = MemoryFns(note(due_date='2026-06-10'))
        self.service = PullService(self.fns, self.dav, self.state, tasks_collection=TASKS, events_collection=EVENTS)

    def test_remote_completion_writes_correct_date_and_schedules_event_retirement(self):
        self.dav.seed(Task('Tasks/A.md', 'A', status='已完成', done_date=date(2026, 6, 1)))
        stats = self.service.run_once()
        self.assertEqual(stats.written, 1)
        self.assertEqual(self.fns.patches[0][1]['task_status'], '已完成')
        self.assertEqual(self.fns.patches[0][1]['done_date'], '2026-06-01')
        self.assertEqual(self.state.note_record('Tasks/A.md')['terminal'], 'COMPLETED')
        self.assertEqual(self.state.retirement('Tasks/A.md')['kinds'], ['VEVENT'])

    def test_cancelled_reference_archive_completed_and_review_sources_cannot_be_restored(self):
        for source in [note(type='reference'), note(cancelled_date='2026-09-08'),
                       note('04 - Archives/Tasks/A.md'), note(status='已完成', done_date='2026-06-01'),
                       replace(note(), frontmatter={})]:
            with self.subTest(source=source.frontmatter):
                self.dav.objects.clear()
                self.fns.notes = {source.path: source}
                self.dav.seed(Task(source.path, 'A', due_date=date(2026, 6, 20)))
                self.service.run_once()
                self.assertEqual(self.fns.patches, [])

    def test_terminal_record_protects_active_looking_source_until_restore(self):
        self.state.note_record('Tasks/A.md')['terminal'] = 'COMPLETED'
        self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 7, 1)))
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])

    def test_completed_echo_does_not_overwrite_historical_done_date(self):
        self.fns.notes['Tasks/A.md'] = note(status='已完成', done_date='2026-06-01')
        self.dav.seed(Task('Tasks/A.md', 'A', status='已完成', done_date=date(2026, 7, 1)))
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])
        self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['done_date'], '2026-06-01')

    def test_blocked_survives_client_removal_of_custom_property(self):
        self.fns.notes['Tasks/A.md'] = note(status='阻塞')
        self.dav.seed(Task('Tasks/A.md', 'A', status='阻塞', due_date=date(2026, 7, 1)))
        self._edit('VTODO', remove=['X-OBSIDIAN-TASK-STATUS'])
        self.service.run_once()
        self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['task_status'], '阻塞')
        self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['due_date'], '2026-07-01')

    def test_legacy_cancelled_is_blocked_only_when_local_source_confirms_it(self):
        for local_status in ['阻塞', '待办']:
            with self.subTest(status=local_status):
                self.dav.objects.clear()
                self.fns.notes['Tasks/A.md'] = note(status=local_status)
                self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 7, 1)))
                self._edit('VTODO', status='CANCELLED', remove=['X-BRIDGE-POLICY-VERSION'])
                self.service.run_once()
                self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['task_status'], local_status)
                self.assertNotIn('cancelled_date', self.fns.notes['Tasks/A.md'].frontmatter)

    def test_new_cancelled_task_is_not_completion(self):
        self.dav.seed(Task('Tasks/A.md', 'A', status='已取消'))
        self.service.run_once()
        fm = self.fns.notes['Tasks/A.md'].frontmatter
        self.assertEqual(fm['task_status'], '已取消')
        self.assertIn('cancelled_date', fm)
        self.assertNotIn('done_date', fm)

    def test_cancelled_event_does_not_cancel_or_reschedule_task(self):
        self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 7, 1)), todo=False)
        self._edit('VEVENT', status='CANCELLED')
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])
        self.assertEqual(self.state.retirement('Tasks/A.md')['kinds'], ['VEVENT'])

    def test_active_event_move_changes_only_due_date(self):
        self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 7, 1)), todo=False)
        self.service.run_once()
        self.assertEqual(self.fns.patches, [('Tasks/A.md', {'due_date': '2026-07-01'})])

    def test_source_read_failure_is_durable_even_after_server_cursor_advances(self):
        self.dav.seed(Task('Tasks/A.md', 'A', status='已完成'))
        self.fns.fail_paths.add('Tasks/A.md')
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])
        loaded = SyncState.load(str(self.state.path))
        self.assertEqual(loaded.get_sync_token(TASKS), 'token-new')
        self.assertEqual(len(loaded.pending_pull()), 1)
        self.fns.fail_paths.clear()
        self.dav.report_objects = []
        service = PullService(self.fns, self.dav, loaded, tasks_collection=TASKS, events_collection=EVENTS)
        service.run_once()
        self.assertEqual(len(self.fns.patches), 1)
        self.assertEqual(loaded.pending_pull(), {})

    def test_remote_deletion_does_not_cancel_source(self):
        self.dav.report_objects = [CollectionObject(TASKS + 'removed.ics', deleted=True)]
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])
        self.assertIsNone(self.state.retirement('Tasks/A.md'))

    def test_source_changed_between_reads_is_retried_without_patch(self):
        self.dav.seed(Task('Tasks/A.md', 'A', status='已完成'))
        original = self.fns.get_note
        count = 0
        def changing_read(path):
            nonlocal count
            count += 1
            if count == 2:
                self.fns.notes[path] = note(type='reference')
            return original(path)
        self.fns.get_note = changing_read
        self.service.run_once()
        self.assertEqual(self.fns.patches, [])
        self.assertEqual(len(self.state.pending_pull()), 1)

    def test_unchanged_remote_fields_do_not_overwrite_new_local_status(self):
        from push import PushService
        from tests.helpers import MemoryWs
        push = PushService(self.fns, self.dav, self.state, fns_ws=MemoryWs(), tasks_collection=TASKS, events_collection=EVENTS)
        push.run_once()
        self.fns.notes['Tasks/A.md'] = note(status='进行中', due_date='2026-06-10')
        for href, old in list(self.dav.objects.items()):
            if href.startswith(TASKS):
                from icalendar import Calendar
                cal = Calendar.from_ical(old.ics_text)
                next(c for c in cal.walk() if c.name == 'VTODO')['PRIORITY'] = 1
                self.dav.objects[href] = replace(old, etag='new-priority', ics_text=cal.to_ical().decode())
        self.service.run_once()
        self.assertEqual(self.fns.notes['Tasks/A.md'].frontmatter['task_status'], '进行中')
        self.assertEqual(self.fns.patches, [('Tasks/A.md', {'priority': 1})])

    def _edit(self, kind, *, status=None, remove=()):
        for href, obj in list(self.dav.objects.items()):
            from icalendar import Calendar
            cal = Calendar.from_ical(obj.ics_text)
            component = next((c for c in cal.walk() if c.name == kind), None)
            if component is None:
                continue
            if status: component['STATUS'] = status
            for key in remove: component.pop(key, None)
            self.dav.objects[href] = RemoteObject(href, obj.etag + '-edit', cal.to_ical().decode())
