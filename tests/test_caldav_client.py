from __future__ import annotations

import unittest

from caldav_client import CalDavClient, PreconditionFailed


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""
        self.headers: dict[str, str] = {}


class FakeSession:
    def __init__(self) -> None:
        self.auth: tuple[str, str] | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []

    def put(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append(("put", kwargs))
        return FakeResponse(412)

    def delete(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append(("delete", kwargs))
        return FakeResponse(412)


class CalDavClientTests(unittest.TestCase):
    def test_parse_multistatus_extracts_sync_token_etags_and_deletes(self) -> None:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/diomgis/tasks/task-a.ics</D:href>
    <D:propstat>
      <D:prop><D:getetag>"etag-a"</D:getetag></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/diomgis/tasks/task-b.ics</D:href>
    <D:status>HTTP/1.1 404 Not Found</D:status>
  </D:response>
  <D:sync-token>http://radicale/sync/42</D:sync-token>
</D:multistatus>
"""
        client = CalDavClient("http://radicale:5232", "user", "pass")

        result = client._parse_multistatus(xml, full=False)

        self.assertEqual(result.sync_token, "http://radicale/sync/42")
        self.assertEqual(len(result.objects), 2)
        self.assertEqual(result.objects[0].etag, '"etag-a"')
        self.assertFalse(result.objects[0].deleted)
        self.assertTrue(result.objects[1].deleted)

    def test_put_object_uses_if_match_and_surfaces_precondition_failure(self) -> None:
        session = FakeSession()
        client = CalDavClient("http://radicale:5232", "user", "pass", session=session)  # type: ignore[arg-type]

        with self.assertRaises(PreconditionFailed):
            client.put_object("/diomgis/tasks/", "task-abc@core-vault", "BEGIN:VCALENDAR\r\nEND:VCALENDAR", if_match='"old"')

        self.assertEqual(session.calls[0][1]["headers"]["If-Match"], '"old"')  # type: ignore[index]

    def test_delete_object_uses_if_match_and_surfaces_precondition_failure(self) -> None:
        session = FakeSession()
        client = CalDavClient("http://radicale:5232", "user", "pass", session=session)  # type: ignore[arg-type]

        with self.assertRaises(PreconditionFailed):
            client.delete_object("/diomgis/core-vault/", "event-abc@core-vault", if_match='"old"')

        self.assertEqual(session.calls[0][1]["headers"]["If-Match"], '"old"')  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
