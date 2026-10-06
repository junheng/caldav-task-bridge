from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from fns_ws import FnsWebSocketClient, FnsWsError, derive_ws_url


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
    def test_paged_delta_requests_first_and_following_pages(self) -> None:
        ws = FakeWebSocket([
            _frame("Authorization", {"data": {}}),
            _frame("ClientInfo", {"data": {}}),
            _frame("NoteSyncModify", {"context": "another-client", "data": {"path": "Ignore.md"}}),
            _frame("NoteSyncEnd", {"data": {"lastTime": 200, "needModifyCount": 2, "needDeleteCount": 1}}),
            _frame("NoteSyncPage", {"data": {"pageIndex": 0, "totalCount": 2, "isLast": False}}),
            _frame("NoteSyncModify", {"data": {"path": "Tasks/A.md"}}),
            _frame("NoteSyncModify", {"data": {"path": "Tasks/B.md"}}),
            _frame("NoteSyncPage", {"data": {"pageIndex": 1, "totalCount": 1, "isLast": True}}),
            _frame("NoteSyncDelete", {"data": {"path": "Tasks/C.md"}}),
        ])
        client = FnsWebSocketClient("wss://fns.example.com/api/user/sync", "token", "Core", connect=lambda *a, **kw: ws)

        result = client.note_sync_since(100)

        self.assertEqual([m.path for m in result.messages], ["Tasks/A.md", "Tasks/B.md", "Tasks/C.md"])
        request = json.loads(ws.sent[2].split("|", 1)[1])
        acks = [json.loads(s.split("|", 1)[1]) for s in ws.sent if s.startswith("NoteSyncPageAck|")]
        self.assertEqual([a["pageIndex"] for a in acks], [-1, 0])
        self.assertTrue(all(a["context"] == request["context"] and a["vault"] == "Core" for a in acks))
        self.assertTrue(ws.closed)

    def test_empty_delta_does_not_request_a_nonexistent_page(self) -> None:
        ws = FakeWebSocket([
            _frame("Authorization", {"data": {}}),
            _frame("ClientInfo", {"data": {}}),
            _frame("NoteSyncEnd", {"data": {"lastTime": 200}}),
        ])
        client = FnsWebSocketClient("wss://fns.example.com/api/user/sync", "token", "Core", connect=lambda *a, **kw: ws)

        self.assertEqual(client.note_sync_since(100).messages, [])
        self.assertFalse(any(s.startswith("NoteSyncPageAck|") for s in ws.sent))

    def test_incomplete_final_page_fails_without_returning_a_new_cursor(self) -> None:
        ws = FakeWebSocket([
            _frame("Authorization", {"data": {}}),
            _frame("ClientInfo", {"data": {}}),
            _frame("NoteSyncEnd", {"data": {"lastTime": 200, "needModifyCount": 2}}),
            _frame("NoteSyncPage", {"data": {"pageIndex": 0, "totalCount": 1, "isLast": True}}),
            _frame("NoteSyncModify", {"data": {"path": "Tasks/A.md"}}),
        ])
        client = FnsWebSocketClient("wss://fns.example.com/api/user/sync", "token", "Core", connect=lambda *a, **kw: ws)

        with self.assertRaisesRegex(FnsWsError, "final page"):
            client.note_sync_since(100)
        self.assertTrue(ws.closed)

    def test_ping_frames_cannot_extend_the_sync_data_deadline(self) -> None:
        class PingSocket:
            def recv_data(self, *, control_frame: bool):
                self.assert_control = control_frame
                return 9, b"ping"

            def settimeout(self, timeout: float):
                pass

        ws = PingSocket()
        client = FnsWebSocketClient("wss://fns.example.com/api/user/sync", "token", "Core")
        with patch("fns_ws.time.monotonic", side_effect=[0.0, 0.4, 1.2]):
            with self.assertRaisesRegex(FnsWsError, "Timed out"):
                client._recv(ws, deadline=1.0)  # type: ignore[arg-type]
        self.assertTrue(ws.assert_control)

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
