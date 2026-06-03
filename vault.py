from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterator

import requests
import yaml

from models import is_task_note, normalize_path


LOG = logging.getLogger(__name__)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
DEFAULT_CLIENT_NAME = "caldav-bridge"


class FnsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Note:
    path: str
    content: str
    frontmatter: dict[str, Any]
    content_hash: str | None = None
    mtime: int | None = None
    raw: dict[str, Any] | None = None


def parse_frontmatter(content: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}
    loaded = yaml.safe_load(match.group(1)) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


class FnsClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        vault: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30,
        task_path_keyword: str = "Tasks",
        client_name: str = DEFAULT_CLIENT_NAME,
    ) -> None:
        base_url = api_url.rstrip("/")
        if not base_url.endswith("/api"):
            base_url = f"{base_url}/api"
        self.api_url = base_url
        self.token = token
        self.vault = vault
        self.session = session or requests.Session()
        self.timeout = timeout
        self.task_path_keyword = task_path_keyword
        self.client_name = client_name

    def list_notes(
        self,
        *,
        keyword: str | None = None,
        search_content: bool = False,
        search_mode: str = "content",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        params: dict[str, Any] = {
            "vault": self.vault,
            "page": page,
            "pageSize": page_size,
            "isRecycle": False,
            "sortBy": "mtime",
            "sortOrder": "desc",
        }
        if keyword:
            params["keyword"] = keyword
            params["searchContent"] = search_content
            params["searchMode"] = search_mode
        data = self._request("GET", "/notes", params=params)
        return _extract_list(data), _extract_pager(data)

    def iter_note_paths(
        self,
        *,
        keyword: str | None = None,
        search_content: bool = False,
        search_mode: str = "content",
    ) -> Iterator[str]:
        page = 1
        while True:
            items, pager = self.list_notes(
                keyword=keyword,
                search_content=search_content,
                search_mode=search_mode,
                page=page,
                page_size=100,
            )
            for item in items:
                path = item.get("path")
                if path:
                    yield normalize_path(str(path))
            if not _has_next_page(page, len(items), pager):
                break
            page += 1

    def get_note(self, path: str) -> Note:
        data = self._request(
            "GET",
            "/note",
            params={"vault": self.vault, "path": normalize_path(path), "isRecycle": False},
        )
        note_data = _extract_note_data(data)
        content = str(note_data.get("content") or "")
        note_path = normalize_path(str(note_data.get("path") or path))
        return Note(
            path=note_path,
            content=content,
            frontmatter=parse_frontmatter(content),
            content_hash=_optional_str(note_data.get("contentHash")),
            mtime=_optional_int(note_data.get("mtime")),
            raw=note_data,
        )

    def iter_task_notes(self) -> Iterator[Note]:
        seen: set[str] = set()
        for path in self.iter_note_paths(keyword=self.task_path_keyword, search_content=False, search_mode="path"):
            if path in seen:
                continue
            seen.add(path)
            note = self.get_note(path)
            if is_task_note(note.path, note.content, note.frontmatter):
                yield note

    def patch_frontmatter(
        self,
        path: str,
        updates: dict[str, Any],
        *,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "vault": self.vault,
            "path": normalize_path(path),
            "updates": {key: _fns_value_list(value) for key, value in updates.items()},
            "remove": remove or [],
        }
        data = self._request("PATCH", "/note/frontmatter", json=payload)
        if isinstance(data, dict):
            return data
        return {"data": data}

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        headers = {
            "Authorization": self.token,
            "token": self.token,
            "X-Client": self.client_name,
            **headers,
        }
        response = self.session.request(
            method,
            f"{self.api_url}{endpoint}",
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise FnsError(f"FNS {method} {endpoint} failed with HTTP {response.status_code}: {response.text}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FnsError(f"FNS {method} {endpoint} returned non-JSON response") from exc
        if isinstance(payload, dict):
            if payload.get("status") is False or payload.get("code") == 0:
                raise FnsError(f"FNS {method} {endpoint} failed: {payload.get('message') or payload}")
            return payload.get("data", payload)
        return payload


def _extract_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "items", "notes"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get("data")
    if nested is not data:
        return _extract_list(nested)
    return []


def _extract_pager(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if any(key in data for key in ("totalRows", "total", "pageSize", "page_size", "page", "pageNo")):
            return data
        pager = data.get("pager")
        if isinstance(pager, dict):
            return pager
        nested = data.get("data")
        if isinstance(nested, dict) and nested is not data:
            return _extract_pager(nested)
    return {}


def _extract_note_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if "content" in data or "path" in data:
            return data
        note = data.get("note")
        if isinstance(note, dict):
            merged = dict(note)
            if "content" in data:
                merged["content"] = data["content"]
            return merged
        nested = data.get("data")
        if isinstance(nested, dict) and nested is not data:
            return _extract_note_data(nested)
    raise FnsError(f"Unexpected FNS note response: {data!r}")


def _has_next_page(page: int, item_count: int, pager: dict[str, Any]) -> bool:
    if item_count == 0:
        return False
    total = _optional_int(pager.get("totalRows")) or _optional_int(pager.get("total"))
    page_size = _optional_int(pager.get("pageSize")) or _optional_int(pager.get("page_size")) or item_count
    if total is None:
        return item_count >= page_size
    return page * page_size < total


def _fns_value_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return [str(_format_value(item)) for item in value]
    return [str(_format_value(value))]


def _format_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
