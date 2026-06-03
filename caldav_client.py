from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests


LOG = logging.getLogger(__name__)
DAV_NS = "{DAV:}"


class WebDavError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectionObject:
    href: str
    etag: str | None = None
    deleted: bool = False


@dataclass(frozen=True)
class SyncResult:
    objects: list[CollectionObject]
    sync_token: str | None
    full: bool = False


@dataclass(frozen=True)
class PutResult:
    href: str
    etag: str | None
    created: bool


class CalDavClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = (username, password)
        self.timeout = timeout

    def put_object(self, collection: str, uid: str, ics_text: str) -> PutResult:
        url = self.object_url(collection, uid)
        response = self.session.put(
            url,
            data=ics_text.encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
            timeout=self.timeout,
        )
        if response.status_code not in {200, 201, 204}:
            raise WebDavError(f"PUT {url} failed with HTTP {response.status_code}: {response.text}")
        etag = response.headers.get("ETag") or self._head_etag(url)
        return PutResult(href=_href_from_url(url), etag=etag, created=response.status_code == 201)

    def delete_object(self, collection: str, uid: str) -> bool:
        url = self.object_url(collection, uid)
        response = self.session.delete(url, timeout=self.timeout)
        if response.status_code == 404:
            return False
        if response.status_code not in {200, 202, 204}:
            raise WebDavError(f"DELETE {url} failed with HTTP {response.status_code}: {response.text}")
        return True

    def object_href(self, collection: str, uid: str) -> str:
        return _href_from_url(self.object_url(collection, uid))

    def object_url(self, collection: str, uid: str) -> str:
        filename = quote(f"{uid}.ics", safe="")
        return f"{self.collection_url(collection)}{filename}"

    def collection_url(self, collection: str) -> str:
        return f"{self.base_url}/{collection.strip('/')}/"

    def sync_collection(self, collection: str, sync_token: str | None) -> SyncResult:
        try:
            return self._report_sync_collection(collection, sync_token)
        except WebDavError:
            if sync_token:
                LOG.exception("sync-collection failed for %s; falling back to full PROPFIND", collection)
            return self._propfind_collection(collection)

    def get_href(self, href: str) -> str:
        url = self._href_to_url(href)
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code < 200 or response.status_code >= 300:
            raise WebDavError(f"GET {href} failed with HTTP {response.status_code}: {response.text}")
        return response.text

    def _report_sync_collection(self, collection: str, sync_token: str | None) -> SyncResult:
        token_xml = f"<D:sync-token>{_xml_escape(sync_token)}</D:sync-token>" if sync_token else "<D:sync-token/>"
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<D:sync-collection xmlns:D="DAV:">
  {token_xml}
  <D:sync-level>1</D:sync-level>
  <D:prop>
    <D:getetag/>
  </D:prop>
</D:sync-collection>"""
        response = self.session.request(
            "REPORT",
            self.collection_url(collection),
            data=body.encode("utf-8"),
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            timeout=self.timeout,
        )
        if response.status_code != 207:
            raise WebDavError(
                f"REPORT sync-collection {collection} failed with HTTP {response.status_code}: {response.text}"
            )
        return self._parse_multistatus(response.text, full=False)

    def _propfind_collection(self, collection: str) -> SyncResult:
        body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:getetag/>
    <D:sync-token/>
  </D:prop>
</D:propfind>"""
        response = self.session.request(
            "PROPFIND",
            self.collection_url(collection),
            data=body.encode("utf-8"),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            timeout=self.timeout,
        )
        if response.status_code != 207:
            raise WebDavError(f"PROPFIND {collection} failed with HTTP {response.status_code}: {response.text}")
        result = self._parse_multistatus(response.text, full=True)
        return SyncResult(
            objects=[obj for obj in result.objects if not obj.href.endswith("/")],
            sync_token=result.sync_token,
            full=True,
        )

    def _parse_multistatus(self, xml_text: str, *, full: bool) -> SyncResult:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise WebDavError("Invalid WebDAV XML response") from exc
        sync_token = _find_text(root, f".//{DAV_NS}sync-token")
        objects: list[CollectionObject] = []
        for response in root.findall(f"{DAV_NS}response"):
            href = _find_text(response, f"{DAV_NS}href")
            if not href:
                continue
            status_values = [node.text or "" for node in response.findall(f".//{DAV_NS}status")]
            deleted = any(" 404 " in status or status.endswith(" 404") for status in status_values)
            etag = _find_text(response, f".//{DAV_NS}getetag")
            if full and href.endswith("/"):
                continue
            objects.append(CollectionObject(href=href, etag=etag, deleted=deleted))
        return SyncResult(objects=objects, sync_token=sync_token, full=full)

    def _href_to_url(self, href: str) -> str:
        parsed = urlparse(href)
        if parsed.scheme:
            return href
        return urljoin(f"{self.base_url}/", href.lstrip("/"))

    def _head_etag(self, url: str) -> str | None:
        response = self.session.head(url, timeout=self.timeout)
        if response.status_code >= 200 and response.status_code < 300:
            return response.headers.get("ETag")
        return None


def _find_text(node: ET.Element, path: str) -> str | None:
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return found.text


def _href_from_url(url: str) -> str:
    parsed = urlparse(url)
    href = parsed.path
    if parsed.query:
        href = f"{href}?{parsed.query}"
    return href


def _xml_escape(value: Any) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
