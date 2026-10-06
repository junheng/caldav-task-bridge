"""Only operate on verified bridge objects inside the configured collections."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from caldav_client import RemoteObject
from models import calendar_components, component_obsidian_path, component_uid, event_uid, task_uid


def in_collection(href: str, collection: str) -> bool:
    path = unquote(urlparse(href).path)
    prefix = "/" + collection.strip("/") + "/"
    return path.startswith(prefix) and "/" not in path[len(prefix):]


def owned_component(remote: RemoteObject, *, path: str, uid: str, kind: str,
                    known_uids: dict[str, str]) -> object:
    components = calendar_components(remote.ics_text)
    if len(components) != 1:
        raise ValueError("expected exactly one managed component")
    component = components[0]
    if component.name != kind or component_uid(component) != uid:
        raise ValueError("remote UID or component kind differs")
    remote_path = component_obsidian_path(component)
    if remote_path != path:
        raise ValueError("remote path differs; possible move or unrelated object")
    expected_uid = task_uid(path) if kind == "VTODO" else event_uid(path)
    if uid != expected_uid and known_uids.get(uid) != path:
        raise ValueError("remote ownership could not be verified")
    if not re.fullmatch(r"(?:task|event)-[0-9a-f]{12}@core-vault", uid):
        raise ValueError("UID is not a bridge UID")
    return component


def inventory(caldav, state, collections: tuple[str, str]) -> tuple[list[dict], list[dict]]:
    objects, reviews = [], []
    for collection, kind in zip(collections, ("VTODO", "VEVENT")):
        for obj in caldav.sync_collection(collection, None).objects:
            if obj.deleted:
                continue
            if not in_collection(obj.href, collection):
                reviews.append({"collection": collection, "href": obj.href, "reason": "href outside collection"})
                continue
            remote = caldav.get_object(obj.href)
            if remote is None:
                continue
            try:
                components = calendar_components(remote.ics_text)
                if len(components) != 1:
                    raise ValueError("expected exactly one component")
                component = components[0]
                uid = component_uid(component)
                path = component_obsidian_path(component)
                if not uid or not path:
                    raise ValueError("missing bridge UID or path")
                owned_component(remote, path=path, uid=uid, kind=kind, known_uids=state.data["uid_paths"])
                mapped = state.path_for_uid(uid)
                if mapped and mapped != path:
                    raise ValueError("stored mapping conflicts with remote path")
                objects.append({"path": path, "uid": uid, "collection": collection,
                                "href": remote.href, "etag": remote.etag, "kind": kind,
                                "status": str(component.get("STATUS") or ""),
                                "policy_version": str(component.get("X-BRIDGE-POLICY-VERSION") or ""),
                                "ics_text": remote.ics_text})
            except (ValueError, TypeError) as exc:
                reviews.append({"collection": collection, "href": obj.href, "reason": str(exc)})
    return objects, reviews
