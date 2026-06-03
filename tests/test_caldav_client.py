from __future__ import annotations

import unittest

from caldav_client import CalDavClient


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


if __name__ == "__main__":
    unittest.main()
