from __future__ import annotations

import tempfile
import unittest
from datetime import date

from caldav_client import CollectionObject, SyncResult
from models import Task, task_to_vtodo_ics
from pull import PullService
from state import SyncState


class FakeCalDav:
    def __init__(self, ics_text: str) -> None:
        self.ics_text = ics_text

    def sync_collection(self, collection: str, sync_token: str | None) -> SyncResult:
        if collection == "/diomgis/tasks/":
            return SyncResult([CollectionObject("/diomgis/tasks/task.ics", '"etag-1"')], "token-1")
        return SyncResult([], "token-2")

    def get_href(self, href: str) -> str:
        return self.ics_text


class FakeFns:
    vault = "Core"

    def __init__(self) -> None:
        self.patches: list[tuple[str, dict[str, object]]] = []

    def patch_frontmatter(self, path: str, updates: dict[str, object]) -> dict[str, object]:
        self.patches.append((path, updates))
        return {}


class PullServiceTests(unittest.TestCase):
    def test_pull_completed_vtodo_writes_frontmatter_via_fns(self) -> None:
        task = Task(path="Tasks/Done.md", title="Done", status="已完成", due_date=date(2026, 6, 10))
        ics_text = task_to_vtodo_ics(task, "Core")

        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            state.remember_uid(task.task_uid, task.path)
            fns = FakeFns()
            service = PullService(
                fns,  # type: ignore[arg-type]
                FakeCalDav(ics_text),  # type: ignore[arg-type]
                state,
                tasks_collection="/diomgis/tasks/",
                events_collection="/diomgis/core-vault/",
            )

            stats = service.run_once()

        self.assertEqual(stats.written, 1)
        self.assertEqual(fns.patches[0][0], "Tasks/Done.md")
        self.assertEqual(fns.patches[0][1]["task_status"], "已完成")


if __name__ == "__main__":
    unittest.main()
