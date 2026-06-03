from __future__ import annotations

import tempfile
import unittest
from datetime import date

from caldav_client import PutResult
from fns_ws import NoteSyncMessage, NoteSyncResult
from push import PushService
from state import SyncState
from vault import FnsError, Note


class FakeCalDav:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def put_object(self, collection: str, uid: str, ics_text: str, *, if_match: str | None = None) -> PutResult:
        self.puts.append((collection, uid))
        return PutResult(href=f"{collection}{uid}.ics", etag=f'"etag-{len(self.puts)}"', created=True)

    def delete_object(self, collection: str, uid: str, *, if_match: str | None = None) -> bool:
        self.deletes.append((collection, uid))
        return True

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
            "Inbox/Note.md": Note(path="Inbox/Note.md", content="plain note", frontmatter={}),
        }
        self.initial_called = 0
        self.fail_paths: set[str] = set()

    def iter_task_notes(self):
        self.initial_called += 1
        yield self.notes["Tasks/Initial.md"]

    def get_note(self, path: str) -> Note:
        if path in self.fail_paths:
            raise FnsError("temporary failure")
        return self.notes[path]


class FakeFnsWs:
    def __init__(self) -> None:
        self.cursor = 1000
        self.messages: list[NoteSyncMessage] = []
        self.calls: list[int] = []

    def current_note_sync_cursor(self) -> int:
        return self.cursor

    def note_sync_since(self, last_time: int | None, *, context: str = "caldav-bridge") -> NoteSyncResult:
        self.calls.append(int(last_time or 0))
        return NoteSyncResult(last_time=self.cursor, messages=self.messages)


class PushServiceTests(unittest.TestCase):
    def test_initial_push_uses_path_scan_and_stores_ws_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            fns = FakeFns()
            fns_ws = FakeFnsWs()
            fns_ws.cursor = 12345
            caldav = FakeCalDav()
            service = _service(fns, caldav, state, fns_ws)

            service.run_once()

            self.assertEqual(fns.initial_called, 1)
            self.assertTrue(state.initial_task_scan_completed())
            self.assertEqual(state.get_fns_note_sync_last_time(), 12345)
            self.assertEqual(len(caldav.puts), 1)

    def test_incremental_push_uses_ws_note_sync_filters_non_tasks_and_deletes_removed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            state.mark_initial_task_scan_completed()
            state.set_fns_note_sync_last_time(100)
            fns = FakeFns()
            fns_ws = FakeFnsWs()
            fns_ws.cursor = 200
            fns_ws.messages = [
                NoteSyncMessage(action="NoteSyncModify", path="Inbox/Note.md"),
                NoteSyncMessage(action="NoteSyncModify", path="Tasks/Changed.md"),
                NoteSyncMessage(action="NoteSyncDelete", path="Tasks/Own.md"),
            ]
            caldav = FakeCalDav()
            service = _service(fns, caldav, state, fns_ws)

            stats = service.run_once()

            self.assertEqual(fns_ws.calls, [100])
            self.assertEqual(fns.initial_called, 0)
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(stats.deleted_tasks, 1)
            self.assertEqual(stats.deleted_events, 1)
            self.assertEqual(len(caldav.puts), 2)
            self.assertEqual(len(caldav.deletes), 2)
            self.assertTrue(any(uid.startswith("task-") for _collection, uid in caldav.puts))
            self.assertEqual(state.get_fns_note_sync_last_time(), 200)
            self.assertEqual(state.get_fns_pending_note_changes(), [])

    def test_failed_note_read_stays_pending_after_cursor_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(f"{tmp}/state.json")
            state.mark_initial_task_scan_completed()
            state.set_fns_note_sync_last_time(100)
            fns = FakeFns()
            fns.fail_paths.add("Tasks/Changed.md")
            fns_ws = FakeFnsWs()
            fns_ws.cursor = 200
            fns_ws.messages = [NoteSyncMessage(action="NoteSyncModify", path="Tasks/Changed.md")]
            caldav = FakeCalDav()
            service = _service(fns, caldav, state, fns_ws)

            stats = service.run_once()

            self.assertEqual(stats.skipped, 1)
            self.assertEqual(caldav.puts, [])
            self.assertEqual(state.get_fns_note_sync_last_time(), 200)
            self.assertEqual(state.get_fns_pending_note_changes(), [{"path": "Tasks/Changed.md", "action": "modify"}])

            fns.fail_paths.clear()
            fns_ws.messages = []
            service.run_once()

            self.assertEqual(state.get_fns_pending_note_changes(), [])
            self.assertEqual(len(caldav.puts), 2)


def _service(fns: FakeFns, caldav: FakeCalDav, state: SyncState, fns_ws: FakeFnsWs) -> PushService:
    return PushService(
        fns,  # type: ignore[arg-type]
        caldav,  # type: ignore[arg-type]
        state,
        fns_ws=fns_ws,  # type: ignore[arg-type]
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
