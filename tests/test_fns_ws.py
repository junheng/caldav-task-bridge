from __future__ import annotations

import json
import unittest
from typing import Any

from fns_ws import FnsWebSocketClient, derive_ws_url


class FakeWebSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


class FnsWebSocketClientTests(unittest.TestCase):
    def test_note_sync_uses_raw_authorization_and_reads_messages_after_end(self) -> None:
        ws = FakeWebSocket(
            [
                _frame("Authorization", {"status": True, "code": 1, "data": {}}),
                _frame("ClientInfo", {"status": True, "code": 1, "data": {}}),
                _frame(
                    "NoteSyncEnd",
                    {
                        "status": True,
                        "code": 1,
                        "data": {
                            "lastTime": 200,
                            "needModifyCount": 1,
                            "needDeleteCount": 1,
                            "needSyncMtimeCount": 0,
                            "needUploadCount": 0,
                        },
                    },
                ),
                _frame(
                    "NoteSyncModify",
                    {"status": True, "code": 1, "data": {"path": "Tasks/A.md", "content": "---\\n---\\n"}},
                ),
                _frame("NoteSyncDelete", {"status": True, "code": 1, "data": {"path": "Tasks/B.md"}}),
            ]
        )
        calls: list[dict[str, Any]] = []

        def connect(url: str, **kwargs: Any) -> FakeWebSocket:
            calls.append({"url": url, **kwargs})
            return ws

        client = FnsWebSocketClient(
            "wss://fns.example.com/api/user/sync",
            "token-1",
            "Core",
            client_type="caldav-bridge",
            client_name="caldav-bridge",
            client_version="0.1.3",
            connect=connect,
        )

        result = client.note_sync_since(100)

        self.assertTrue(ws.closed)
        self.assertEqual(calls[0]["url"], "wss://fns.example.com/api/user/sync")
        self.assertIn("X-Client: caldav-bridge", calls[0]["header"])
        self.assertEqual(ws.sent[0], "Authorization|token-1")
        self.assertTrue(ws.sent[1].startswith("ClientInfo|"))
        self.assertTrue(ws.sent[2].startswith("NoteSync|"))
        self.assertEqual(result.last_time, 200)
        self.assertEqual([(message.action, message.path) for message in result.messages], [
            ("NoteSyncModify", "Tasks/A.md"),
            ("NoteSyncDelete", "Tasks/B.md"),
        ])

    def test_derive_ws_url_from_api_url(self) -> None:
        self.assertEqual(
            derive_ws_url("https://fns.example.com/api"),
            "wss://fns.example.com/api/user/sync",
        )
        self.assertEqual(
            derive_ws_url("http://fns.example.com:8080"),
            "ws://fns.example.com:8080/api/user/sync",
        )


def _frame(action: str, payload: dict[str, object]) -> str:
    return f"{action}|{json.dumps(payload, separators=(',', ':'))}"


if __name__ == "__main__":
    unittest.main()
