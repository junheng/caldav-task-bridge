from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
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

    def note_record(self, path: str) -> dict[str, Any]:
        return self.data.setdefault("lifecycle", {}).setdefault("notes", {}).setdefault(path, {})

    def review(self, path: str, reason: str) -> None:
        self.data.setdefault("lifecycle", {}).setdefault("reviews", {})[path] = reason

    def clear_review(self, path: str) -> None:
        self.data.setdefault("lifecycle", {}).setdefault("reviews", {}).pop(path, None)

    def object_records(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("lifecycle", {}).setdefault("objects", {})

    def remember_object(self, uid: str, path: str, collection: str, href: str, kind: str) -> None:
        self.remember_uid(uid, path)
        self.object_records().setdefault(uid, {}).update({"path": path, "collection": collection, "href": href, "kind": kind})

    def retirement(self, path: str) -> dict[str, Any] | None:
        return self.data.get("lifecycle", {}).get("retirements", {}).get(path)

    def begin_retirement(self, path: str, reason: str, *, kinds: tuple[str, ...]) -> dict[str, Any]:
        records = self.data.setdefault("lifecycle", {}).setdefault("retirements", {})
        record = records.setdefault(path, {"reason": reason, "kinds": [], "progress": {}})
        record["reason"] = reason
        record["kinds"] = sorted(set(record["kinds"]) | set(kinds))
        return record

    def pending_pull(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("lifecycle", {}).setdefault("pending_pull", {})

    def restore(self, path: str) -> None:
        """Only the explicit restore CLI calls this; normal sync never does."""
        self.data.setdefault("lifecycle", {}).setdefault("retirements", {}).pop(path, None)
        self.note_record(path).pop("terminal", None)
        self.data.setdefault("lifecycle", {}).setdefault("approved_exclusions", {}).pop(path, None)
        self.clear_review(path)
        self.add_fns_pending_note_change(path)

    @contextmanager
    def exclusive(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another bridge process owns state; stop it before maintenance") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _collection(self, collection: str) -> dict[str, Any]:
        collections = self.data.setdefault("collections", {})
        state = collections.setdefault(collection, {})
        state.setdefault("sync_token", None)
        state.setdefault("etags", {})
        return state
