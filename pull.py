from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from caldav_client import CalDavClient, CollectionObject
from lifecycle import DEFAULT_ARCHIVE_PREFIXES, Lifecycle, classify_note
from managed_objects import in_collection, owned_component
from models import calendar_components, component_obsidian_path, component_uid, updates_from_caldav_component
from state import SyncState
from vault import FnsClient

LOG = logging.getLogger(__name__)


@dataclass
class PullStats:
    changed: int = 0
    written: int = 0
    deleted: int = 0
    skipped_echo: int = 0
    skipped_unmatched: int = 0
    protected: int = 0
    failed: int = 0


class PullService:
    def __init__(self, fns: FnsClient, caldav: CalDavClient, state: SyncState, *,
                 tasks_collection: str, events_collection: str,
                 archive_prefixes: tuple[str, ...] = DEFAULT_ARCHIVE_PREFIXES) -> None:
        self.fns, self.caldav, self.state = fns, caldav, state
        self.collections = (tasks_collection, events_collection)
        self.archive_prefixes = archive_prefixes

    def run_once(self) -> PullStats:
        stats = PullStats()
        for collection in self.collections:
            self._pull_collection(collection, stats)
        self.state.save()
        LOG.info("pull complete: %s", stats)
        return stats

    def _pull_collection(self, collection: str, stats: PullStats) -> None:
        result = self.caldav.sync_collection(collection, self.state.get_sync_token(collection))
        pending = self.state.pending_pull()
        fresh = set()
        for obj in result.objects:
            key = collection + ":" + obj.href
            pending[key] = {"collection": collection, "href": obj.href, "etag": obj.etag,
                            "deleted": obj.deleted, "uid": self._known_uid(obj.href)}
            fresh.add(key)
        # Persist each incoming object before accepting the server's new cursor.
        self.state.save()
        self.state.set_sync_token(collection, result.sync_token)
        self.state.save()
        for key, obj in list(pending.items()):
            if obj["collection"] != collection:
                continue
            try:
                self._handle_object(collection, CollectionObject(obj["href"], obj["etag"], obj["deleted"]),
                                    stats, count_change=key in fresh)
                pending.pop(key, None)
            except Exception:
                stats.failed += 1
                LOG.warning("keeping failed remote read/write pending: %s", obj["href"], exc_info=True)
            finally:
                self.state.save()

    def _known_uid(self, href: str) -> str | None:
        return next((uid for uid, obj in self.state.object_records().items() if obj["href"] == href), None)

    def _handle_object(self, collection: str, obj: CollectionObject, stats: PullStats, *, count_change: bool = True) -> None:
        if obj.deleted:
            self.state.remove_etag(collection, obj.href)
            stats.deleted += 1
            return  # Deleting a remote item is not cancellation of its source.
        if obj.etag and self.state.get_etag(collection, obj.href) == obj.etag:
            stats.skipped_echo += 1
            return
        if count_change:
            stats.changed += 1
        if not in_collection(obj.href, collection):
            stats.skipped_unmatched += 1
            return
        remote = self.caldav.get_object(obj.href)
        if remote is None:
            self.state.remove_etag(collection, obj.href)
            return
        components = calendar_components(remote.ics_text)
        if len(components) != 1:
            self.state.review(obj.href, "expected exactly one bridge component")
            return
        component = components[0]
        uid = component_uid(component)
        path = component_obsidian_path(component)
        mapped = self.state.path_for_uid(uid) if uid else None
        kind = "VTODO" if collection == self.collections[0] else "VEVENT"
        if not uid or not path or (mapped and mapped != path):
            stats.skipped_unmatched += 1
            self.state.review(obj.href, "missing or conflicting source mapping")
            return
        try:
            owned_component(remote, path=path, uid=uid, kind=kind, known_uids=self.state.data["uid_paths"])
        except ValueError as exc:
            stats.skipped_unmatched += 1
            self.state.review(obj.href, str(exc))
            return
        baseline = self.state.object_records().get(uid, {}).get("fields")
        self.state.remember_object(uid, path, collection, remote.href, kind)
        pending = self.state.pending_pull().get(collection + ":" + obj.href)
        if pending is not None:
            pending["uid"] = uid
        # This read is mandatory even for a familiar UID and a valid incoming STATUS.
        note = self.fns.get_note(path)
        decision = classify_note(path, note.frontmatter, archive_prefixes=self.archive_prefixes)
        record = self.state.note_record(path)
        intent = self.state.retirement(path)
        protected = decision.kind != Lifecycle.ACTIVE or bool(record.get("terminal")) or bool(intent and kind in intent["kinds"])
        if protected:
            stats.protected += 1
            if decision.kind == Lifecycle.REVIEW:
                self.state.review(path, decision.reason)
            else:
                self.state.add_fns_pending_note_change(path)
            self.state.set_etag(collection, remote.href, remote.etag)
            return
        status = str(component.get("STATUS") or "").upper()
        if kind == "VEVENT" and status == "CANCELLED":
            self.state.begin_retirement(path, "explicit cancellation of calendar projection only", kinds=("VEVENT",))
            self.state.add_fns_pending_note_change(path)
            self.state.review(path, "calendar cancelled; task status and due date retained")
            self.state.set_etag(collection, remote.href, remote.etag)
            return
        if kind == "VTODO" and status not in {"NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"}:
            self.state.review(path, "missing or unknown remote task status")
            self.state.set_etag(collection, remote.href, remote.etag)
            return
        try:
            updates = updates_from_caldav_component(component, today=date.today(), current_frontmatter=note.frontmatter)
        except ValueError as exc:
            self.state.review(path, str(exc))
            self.state.set_etag(collection, remote.href, remote.etag)
            return
        remote_fields = dict(updates)
        if baseline is not None:
            updates = {k: v for k, v in updates.items() if k not in baseline or baseline[k] != v}
        updates = {k: v for k, v in updates.items() if note.frontmatter.get(k) != v}
        if updates:
            # FNS has no atomic frontmatter compare-and-swap. Detect intervening
            # changes immediately before PATCH rather than trusting a cached note.
            latest = self.fns.get_note(path)
            if latest.content_hash != note.content_hash or latest.frontmatter != note.frontmatter:
                raise ValueError("source changed before writeback; reread on next cycle")
            self.fns.patch_frontmatter(path, updates)
            stats.written += 1
            merged = {**note.frontmatter, **updates}
            final = classify_note(path, merged, archive_prefixes=self.archive_prefixes)
            record.update(final.as_dict())
            if final.kind in {Lifecycle.COMPLETED, Lifecycle.CANCELLED}:
                record["terminal"] = final.kind.value
                kinds = ("VTODO", "VEVENT") if final.kind == Lifecycle.CANCELLED else ("VEVENT",)
                self.state.begin_retirement(path, "remote task entered terminal state", kinds=kinds)
            self.state.add_fns_pending_note_change(path)
        elif status == "CANCELLED" and decision.status == "阻塞":
            self.state.add_fns_pending_note_change(path)  # normalize legacy blocked representation
        self.state.set_etag(collection, remote.href, remote.etag)
        self.state.object_records()[uid]["fields"] = remote_fields
