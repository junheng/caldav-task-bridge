from __future__ import annotations

import unittest
from types import SimpleNamespace

from main import run_once


class FakePush:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> None:
        self.calls += 1


class FakePull:
    def __init__(self, stats: object) -> None:
        self.calls = 0
        self.stats = stats

    def run_once(self) -> object:
        self.calls += 1
        return self.stats


class MainSyncOrderTests(unittest.TestCase):
    def test_both_runs_pull_before_push_when_no_remote_changes(self) -> None:
        push = FakePush()
        pull = FakePull(SimpleNamespace(changed=0, written=0, deleted=0, skipped_unmatched=0))

        run_once("both", push, pull)  # type: ignore[arg-type]

        self.assertEqual(pull.calls, 1)
        self.assertEqual(push.calls, 1)

    def test_both_skips_push_when_pull_saw_remote_changes(self) -> None:
        push = FakePush()
        pull = FakePull(SimpleNamespace(changed=1, written=1, deleted=0, skipped_unmatched=0))

        run_once("both", push, pull)  # type: ignore[arg-type]

        self.assertEqual(pull.calls, 1)
        self.assertEqual(push.calls, 0)


if __name__ == "__main__":
    unittest.main()
