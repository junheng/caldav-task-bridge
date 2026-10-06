import unittest
from lifecycle import Lifecycle as L, classify_note


class LifecycleTests(unittest.TestCase):
    def test_cancelled_reference_does_not_become_active_from_path_or_checklist(self):
        d = classify_note('Tasks/Cancelled.md', {'tags': ['type/reference'], 'cancelled_date': '2026-09-08'})
        self.assertEqual(d.kind, L.CANCELLED)

    def test_reference_and_verified_archive_override_legacy_status(self):
        for path, fm in [('Tasks/Ref.md', {'type': 'reference', 'task_status': '待办'}),
                         ('04 - Archives/Tasks/Old.md', {'task_status': '待办'})]:
            self.assertEqual(classify_note(path, fm).kind, L.OUT_OF_SCOPE)
        self.assertEqual(classify_note('Tasks/Archives notes.md', {'task_status': '待办'}).kind, L.ACTIVE)

    def test_ambiguous_or_invalid_evidence_requires_review(self):
        for fm in [{}, {'type': 'task'}, {'task_status': ''}, {'task_status': 'unknown'},
                   {'task_status': ['待办', '已完成']}, {'tags': {'type/task': True}},
                   {'type': 'task', 'tags': ['type/reference'], 'task_status': '待办'},
                   {'cancelled_date': '2026-09-08bad'}, {'cancelled_date': 'bad'},
                   {'task_status': '已完成', 'cancelled_date': '2026-09-08'},
                   {'task_status': '待办', 'done_date': '2026-09-08'},
                   {'task_status': '待办', 'due_date': '{{date}}'}]:
            with self.subTest(fm=fm):
                self.assertEqual(classify_note('Tasks/Review.md', fm).kind, L.REVIEW)

    def test_legacy_valid_status_and_fns_singleton_values_remain_supported(self):
        for status in ['待办', '进行中', '阻塞']:
            self.assertEqual(classify_note('Tasks/Old.md', {'task_status': [status], 'due_date': ['2020-01-01']}).kind, L.ACTIVE)
        self.assertEqual(classify_note('Tasks/Done.md', {'task_status': '已完成'}).kind, L.COMPLETED)
        self.assertEqual(classify_note('Tasks/Cancelled.md', {'task_status': '已取消'}).kind, L.CANCELLED)
