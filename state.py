from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SyncState:
    def __init__(self, path: str, data: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.data = data or {
            "last_push_timestamp": None,
            "collections": {},
            "fns": {},
            "uid_paths": {},
        }

    @classmethod
    def load(cls, path: str) -> "SyncState":
        state_path = Path(path)
        if not state_path.exists():
            return cls(path)
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("last_push_timestamp", None)
        data.setdefault("collections", {})
        data.setdefault("fns", {})
        data.setdefault("uid_paths", {})
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, self.path)

    def remember_uid(self, uid: str, path: str) -> None:
        self.data["uid_paths"][uid] = path

    def path_for_uid(self, uid: str) -> str | None:
        value = self.data["uid_paths"].get(uid)
        if value is None:
            return None
        return str(value)

    def get_sync_token(self, collection: str) -> str | None:
        value = self._collection(collection).get("sync_token")
        if value is None:
            return None
        return str(value)

    def set_sync_token(self, collection: str, sync_token: str | None) -> None:
        self._collection(collection)["sync_token"] = sync_token

    def get_etag(self, collection: str, href: str) -> str | None:
        value = self._collection(collection)["etags"].get(href)
        if value is None:
            return None
        return str(value)

    def set_etag(self, collection: str, href: str, etag: str | None) -> None:
        if etag:
            self._collection(collection)["etags"][href] = etag

    def remove_etag(self, collection: str, href: str) -> None:
        self._collection(collection)["etags"].pop(href, None)

    def mark_push_now(self) -> None:
        self.data["last_push_timestamp"] = datetime.now(timezone.utc).isoformat()

    def initial_task_scan_completed(self) -> bool:
        return bool(self.data.setdefault("fns", {}).get("initial_task_scan_completed"))

    def mark_initial_task_scan_completed(self) -> None:
        self.data.setdefault("fns", {})["initial_task_scan_completed"] = True

    def get_caldav_mapping_version(self) -> int | None:
        value = self.data.setdefault("fns", {}).get("caldav_mapping_version")
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def set_caldav_mapping_version(self, version: int) -> None:
        self.data.setdefault("fns", {})["caldav_mapping_version"] = version

    def get_fns_note_sync_last_time(self) -> int | None:
        value = self.data.setdefault("fns", {}).get("note_sync_last_time")
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def set_fns_note_sync_last_time(self, last_time: int | None) -> None:
        self.data.setdefault("fns", {})["note_sync_last_time"] = last_time

    def get_fns_pending_note_changes(self) -> list[dict[str, str]]:
        raw = self.data.setdefault("fns", {}).get("pending_note_changes")
        if not isinstance(raw, list):
            return []
        changes: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            action = item.get("action") or "modify"
            if path:
                changes.append({"path": str(path), "action": str(action)})
        return changes

    def add_fns_pending_note_change(self, path: str, action: str = "modify") -> None:
        change = {"path": path, "action": action}
        changes = self.get_fns_pending_note_changes()
        if change not in changes:
            changes.append(change)
        self.data.setdefault("fns", {})["pending_note_changes"] = changes

    def remove_fns_pending_note_change(self, path: str, action: str | None = None) -> None:
        changes = [
            change
            for change in self.get_fns_pending_note_changes()
            if change["path"] != path or (action is not None and change["action"] != action)
        ]
        self.data.setdefault("fns", {})["pending_note_changes"] = changes

    def forget_uid(self, uid: str) -> None:
        self.data["uid_paths"].pop(uid, None)

    def _collection(self, collection: str) -> dict[str, Any]:
        collections = self.data.setdefault("collections", {})
        state = collections.setdefault(collection, {})
        state.setdefault("sync_token", None)
        state.setdefault("etags", {})
        return state
