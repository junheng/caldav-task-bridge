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
        self.assertEqual(call["json"]["vault"], "Core")  # type: ignore[index]
        self.assertEqual(call["json"]["path"], "Tasks/Done.md")  # type: ignore[index]
        self.assertEqual(call["json"]["updates"]["task_status"], ["已完成"])  # type: ignore[index]
        self.assertEqual(call["json"]["updates"]["done_date"], ["2026-06-03"])  # type: ignore[index]

    def test_all_fns_requests_have_client_header(self) -> None:
        session = FakeSession()
        client = FnsClient("https://fns.example.com", "token-1", "Core", session=session)  # type: ignore[arg-type]

        client.list_notes()

        self.assertEqual(session.calls[0]["headers"]["X-Client"], "caldav-bridge")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
