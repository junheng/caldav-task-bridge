from __future__ import annotations

import unittest
from datetime import date

from vault import FnsClient, parse_frontmatter


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"status": True, "code": 1, "data": {}}
        self.text = str(self._payload)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()


class SequenceSession:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(payload={"status": True, "code": 1, "data": self.responses.pop(0)})


class VaultTests(unittest.TestCase):
    def test_parse_frontmatter(self) -> None:
        content = """---
task_status: 待办
priority: 2
---

body
"""
        frontmatter = parse_frontmatter(content)
        self.assertEqual(frontmatter["task_status"], "待办")
        self.assertEqual(frontmatter["priority"], 2)

    def test_patch_frontmatter_uses_fns_api_shape(self) -> None:
        session = FakeSession()
        client = FnsClient("https://fns.example.com", "token-1", "Core", session=session)  # type: ignore[arg-type]

        client.patch_frontmatter(
            "Tasks/Done.md",
            {"task_status": "已完成", "done_date": date(2026, 6, 3)},
        )

        call = session.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["url"], "https://fns.example.com/api/note/frontmatter")
        self.assertEqual(call["headers"]["Authorization"], "token-1")  # type: ignore[index]
        self.assertEqual(call["headers"]["X-Client"], "caldav-bridge")  # type: ignore[index]
        self.assertEqual(call["headers"]["X-Client-Name"], "caldav-bridge")  # type: ignore[index]
        self.assertEqual(call["json"]["vault"], "Core")  # type: ignore[index]
        self.assertEqual(call["json"]["path"], "Tasks/Done.md")  # type: ignore[index]
        self.assertEqual(call["json"]["updates"]["task_status"], ["已完成"])  # type: ignore[index]
        self.assertEqual(call["json"]["updates"]["done_date"], ["2026-06-03"])  # type: ignore[index]

    def test_all_fns_requests_have_client_headers(self) -> None:
        session = FakeSession()
        client = FnsClient("https://fns.example.com", "token-1", "Core", session=session)  # type: ignore[arg-type]

        client.list_notes()

        self.assertEqual(session.calls[0]["headers"]["X-Client"], "caldav-bridge")  # type: ignore[index]
        self.assertEqual(session.calls[0]["headers"]["X-Client-Name"], "caldav-bridge")  # type: ignore[index]

    def test_iter_task_notes_uses_path_search_then_filters_note_content(self) -> None:
        session = SequenceSession(
            [
                {
                    "list": [
                        {"path": "Tasks/A.md"},
                        {"path": "Tasks/B.md"},
                        {"path": "Inbox/NotTask.md"},
                    ],
                    "totalRows": 3,
                    "pageSize": 100,
                },
                {
                    "path": "Tasks/A.md",
                    "content": "---\ntask_status: 待办\n---\n",
                },
                {
                    "path": "Tasks/B.md",
                    "content": "---\ntask_status: 进行中\n---\n",
                },
                {
                    "path": "Inbox/NotTask.md",
                    "content": "---\ntitle: Notes\n---\nplain note",
                },
            ]
        )
        client = FnsClient("https://fns.example.com", "token-1", "Core", session=session)  # type: ignore[arg-type]

        notes = list(client.iter_task_notes())

        self.assertEqual([note.path for note in notes], ["Tasks/A.md", "Tasks/B.md"])
        first_call = session.calls[0]
        self.assertEqual(first_call["url"], "https://fns.example.com/api/notes")
        self.assertEqual(first_call["params"]["keyword"], "Tasks")  # type: ignore[index]
        self.assertEqual(first_call["params"]["searchMode"], "path")  # type: ignore[index]
        self.assertFalse(first_call["params"]["searchContent"])  # type: ignore[index]

    def test_note_sync_logs_since_filters_by_cursor_and_returns_new_cursor(self) -> None:
        session = SequenceSession(
            [
                {
                    "list": [
                        {
                            "path": "Tasks/New.md",
                            "clientName": "Obsidian",
                            "createdAt": "2026-06-03T10:05:00Z",
                            "action": "modify",
                            "type": "note",
                        },
                        {
                            "path": "Tasks/Seen.md",
                            "clientName": "Obsidian",
                            "createdAt": "2026-06-03T10:00:00Z",
                            "action": "modify",
                            "type": "note",
                        },
                        {
                            "path": "Tasks/Old.md",
                            "clientName": "Obsidian",
                            "createdAt": "2026-06-03T09:59:00Z",
                            "action": "modify",
                            "type": "note",
                        },
                    ],
                    "totalRows": 3,
                    "pageSize": 100,
                },
            ]
        )
        client = FnsClient("https://fns.example.com", "token-1", "Core", session=session)  # type: ignore[arg-type]
        seen_fingerprint = "2026-06-03T10:00:00Z|Tasks/Seen.md||modify|Obsidian|"

        entries, cursor = client.note_sync_logs_since(
            {"created_at": "2026-06-03T10:00:00Z", "fingerprints": [seen_fingerprint]}
        )

        self.assertEqual([entry.path for entry in entries], ["Tasks/New.md"])
        self.assertEqual(cursor["created_at"], "2026-06-03T10:05:00Z")  # type: ignore[index]
        first_call = session.calls[0]
        self.assertEqual(first_call["url"], "https://fns.example.com/api/sync-logs")
        self.assertEqual(first_call["params"]["type"], "note")  # type: ignore[index]
        self.assertEqual(first_call["params"]["vault"], "Core")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
