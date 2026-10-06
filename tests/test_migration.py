import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date

from models import Task, calendar_components
from migration import apply_preview, preview
from push import PushService
from state import SyncState
from tests.helpers import EVENTS, TASKS, MemoryDav, MemoryFns, MemoryWs, note


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SyncState.load(self.tmp.name + '/state.json')
        self.fns = MemoryFns(note(type='reference', cancelled_date='2026-09-08'))
        self.dav = MemoryDav()
        self.dav.seed(Task('Tasks/A.md', 'A', due_date=date(2026, 6, 10)))
        self.service = PushService(self.fns, self.dav, self.state, fns_ws=MemoryWs(), tasks_collection=TASKS, events_collection=EVENTS)
        self.state.set_sync_token(TASKS, 'original-token')
        self.state.set_fns_note_sync_last_time(100)
        self.state.save()

    def test_preview_has_zero_writes_and_preserves_state_and_cursors(self):
        before = self.state.path.read_bytes()
        data = copy.deepcopy(self.state.data)
        plan = preview(self.service)
        self.assertEqual(self.dav.puts, [])
        self.assertEqual(self.dav.deletes, [])
        self.assertEqual(self.fns.patches, [])
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertEqual(self.state.data, data)
        self.assertEqual(plan['summary']['delete_objects_by_kind'], {'VTODO': 1, 'VEVENT': 1})

    def test_apply_checks_source_and_remote_etag_and_backs_up_before_delete(self):
        plan = preview(self.service)
        result = apply_preview(plan, self.service, self.tmp.name + '/backup')
        self.assertEqual(result['summary'], {'applied': 1})
        self.assertEqual(self.dav.objects, {})
        self.assertEqual(self.fns.patches, [])
        from pathlib import Path
        self.assertEqual(len(list(Path(self.tmp.name + '/backup').glob('*.ics'))), 2)
        self.assertEqual(Path(self.tmp.name + '/backup/state.json').read_bytes().decode().count('original-token'), 1)
        self.assertTrue(result['receipts'][0]['source_unchanged'])

    def test_changed_source_and_remote_are_not_applied(self):
        for change in ('source', 'remote'):
            with self.subTest(change=change):
                plan = preview(self.service)
                if change == 'source':
                    self.fns.notes['Tasks/A.md'] = note(type='reference', cancelled_date='2026-09-09')
                else:
                    href, old = next(iter(self.dav.objects.items()))
                    self.dav.objects[href] = replace(old, etag='new-etag')
                result = apply_preview(plan, self.service, self.tmp.name + '/backup-' + change)
                self.assertEqual(result['summary'], {'pending': 1})
                self.assertEqual(self.dav.deletes, [])

    def test_reviewed_instruction_note_requires_explicit_per_path_approval(self):
        self.fns = MemoryFns(replace(note('AGENTS.md'), frontmatter={}))
        self.service.fns = self.fns
        self.dav.objects.clear()
        self.dav.seed(Task('AGENTS.md', 'AGENTS'))
        plan = preview(self.service)
        self.assertEqual(plan['notes'][0]['classification'], 'REVIEW')
        skipped = apply_preview(plan, self.service, self.tmp.name + '/skip')
        self.assertEqual(skipped['summary'], {'skipped': 1})
        applied = apply_preview(plan, self.service, self.tmp.name + '/approved', approve_reviews=('AGENTS.md',))
        self.assertEqual(applied['summary'], {'applied': 1})
        self.assertEqual(self.dav.objects, {})
        self.service.run_once(paths=['AGENTS.md'])
        self.assertEqual(self.dav.objects, {})

    def test_two_writers_cannot_own_same_state(self):
        with self.state.exclusive():
            with self.assertRaisesRegex(RuntimeError, 'Another bridge process'):
                with self.state.exclusive():
                    pass

    def test_remote_change_between_preflight_and_delete_is_not_overwritten(self):
        plan = preview(self.service)
        original = self.dav.get_object
        calls = {}
        def changing_read(href):
            calls[href] = calls.get(href, 0) + 1
            if calls[href] == 2 and href in self.dav.objects:
                self.dav.objects[href] = replace(self.dav.objects[href], etag='changed-after-preflight')
            return original(href)
        self.dav.get_object = changing_read
        result = apply_preview(plan, self.service, self.tmp.name + '/concurrent')
        self.assertEqual(result['summary'], {'pending': 1})
        self.assertEqual(self.dav.deletes, [])

    def test_missing_active_objects_are_explicit_create_actions(self):
        self.fns.notes['Tasks/A.md'] = note(due_date='2026-06-10')
        self.dav.objects.clear()
        plan = preview(self.service)
        self.assertEqual(plan['summary']['objects_by_action'], {'create': 2})
        result = apply_preview(plan, self.service, self.tmp.name + '/new')
        self.assertEqual(result['summary'], {'applied': 1})
        self.assertEqual(len(self.dav.objects), 2)

    def test_completed_history_without_remote_todo_is_not_backfilled(self):
        self.fns.notes['Tasks/A.md'] = note(status='已完成', done_date='2026-06-01')
        self.dav.objects.clear()
        plan = preview(self.service)
        result = apply_preview(plan, self.service, self.tmp.name + '/history')
        self.assertEqual(result['summary'], {'applied': 1})
        self.assertEqual(self.dav.puts, [])
