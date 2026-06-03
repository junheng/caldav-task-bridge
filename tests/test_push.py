from __future__ import annotations

import tempfile
import unittest
from datetime import date

from caldav_client import PutResult
from push import PushService
from state import SyncState
from vault import Note, SyncLogEntry


class FakeCalDav:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    def put_object(self, collection: str, uid: str, ics_text: str, *, if_match: str | None = None) -> PutResult:
        self.puts.append((collection, uid))
        return PutResult(href=f"{collection}{uid}.ics", etag=f'"etag-{len(self.puts)}"', created=True)

    def delete_object(self, collection: str, uid: str, *, if_match: str | None = None) -> bool:
        return False

    def object_href(self, collection: str, uid: str) -> str:
        return f"{collection}{uid}.ics"


class FakeFns:
    vault = "Core"
    client_name = "caldav-bridge"

    def __init__(self) -> None:
        self.notes = {
            "Tasks/Initial.md": _note("Tasks/Initial.md", "待办"),
            "Tasks/Changed.md": _note("Tasks/Changed.md", "进行中", due_date="2026-06-10"),
            "Tasks/Own.md": _note("Tasks/Own.md", "待办"),
        }
        self.initial_called = 0
        self.latest_cursor = {"created_at": "2026-06-03T10:00:00Z", "fingerprints": ["initial"]}
        self.delta_entries: list[SyncLogEntry] = []

    def latest_note_log_cursor(self) -> dict[str, object]:
        return self.latest_cursor

    def iter_task_notes(self):
        self.initial_called += 1
        yield self.notes["Tasks/Initial.md"]

    def note_sync_logs_since(self, cursor: dict[str, object] | None):
        return self.delta_entries, {"created_at": "2026-06-03T10:05:00Z", "fingerprints": ["delta"]}

    def get_note(self, path: str) -> Note:
        return self.notes[path]


class PushServiceTests(unittest.TestCase):
    def test_initial_push_uses_path_scan_and_stores_fns_log_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            fns = FakeFns()
            caldav = FakeCalDav()
            service = _service(fns, caldav, state)

            service.run_once()

            self.assertEqual(fns.initial_called, 1)
            self.assertTrue(state.initial_task_scan_completed())
            self.assertEqual(state.get_fns_note_log_cursor(), fns.latest_cursor)
            self.assertEqual(len(caldav.puts), 1)

    def test_incremental_push_uses_sync_logs_and_skips_own_client_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            state.mark_initial_task_scan_completed()
            state.set_fns_note_log_cursor({"created_at": "2026-06-03T10:00:00Z", "fingerprints": ["old"]})
            fns = FakeFns()
            fns.delta_entries = [
                SyncLogEntry(path="Tasks/Own.md", client_name="caldav-bridge", created_at="2026-06-03T10:04:00Z"),
                SyncLogEntry(path="Tasks/Changed.md", client_name="Obsidian", created_at="2026-06-03T10:05:00Z"),
            ]
            caldav = FakeCalDav()
            service = _service(fns, caldav, state)

            stats = service.run_once()

            self.assertEqual(stats.loop_skipped, 1)
            self.assertEqual(fns.initial_called, 0)
            self.assertEqual(len(caldav.puts), 2)
            self.assertTrue(any(uid.startswith("task-") for _collection, uid in caldav.puts))
            self.assertEqual(state.get_fns_note_log_cursor()["created_at"], "2026-06-03T10:05:00Z")  # type: ignore[index]


def _service(fns: FakeFns, caldav: FakeCalDav, state: SyncState) -> PushService:
    return PushService(
        fns,  # type: ignore[arg-type]
        caldav,  # type: ignore[arg-type]
        state,
        tasks_collection="/diomgis/tasks/",
        events_collection="/diomgis/core-vault/",
    )


def _note(path: str, status: str, *, due_date: str | None = None) -> Note:
    frontmatter = {"task_status": status, "priority": 2}
    if due_date:
        frontmatter["due_date"] = due_date
    return Note(path=path, content="", frontmatter=frontmatter)


if __name__ == "__main__":
    unittest.main()
