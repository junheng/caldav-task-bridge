from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Iterable

from caldav_client import CalDavClient, PreconditionFailed
from fns_ws import FnsWebSocketClient
from lifecycle import DEFAULT_ARCHIVE_PREFIXES, Decision, Lifecycle, classify_note
from managed_objects import in_collection, inventory, owned_component
from models import Task, event_uid, normalize_path, task_to_vevent_ics, task_to_vtodo_ics, task_uid
from models import calendar_components, updates_from_caldav_component
from state import SyncState
from vault import FnsClient, FnsError, Note

LOG = logging.getLogger(__name__)
CALDAV_MAPPING_VERSION = 6


@dataclass
class PushStats:
    created: int = 0
    updated: int = 0
    completed: int = 0
    deleted_tasks: int = 0
    deleted_events: int = 0
    conflicts: int = 0
    skipped: int = 0
    review: int = 0


@dataclass(frozen=True)
class PushCandidate:
    path: str
    note: Note | None = None
    deleted: bool = False
    renamed: bool = False


class PushService:
    def __init__(self, fns: FnsClient, caldav: CalDavClient, state: SyncState, *,
                 fns_ws: FnsWebSocketClient | None = None,
                 tasks_collection: str, events_collection: str,
                 archive_prefixes: tuple[str, ...] = DEFAULT_ARCHIVE_PREFIXES) -> None:
        self.fns, self.caldav, self.state = fns, caldav, state
        self.fns_ws = fns_ws
        self.tasks_collection, self.events_collection = tasks_collection, events_collection
        self.archive_prefixes = archive_prefixes
        self.approved_versions: dict[str, str | None] | None = None
        self.approved_source: tuple[str, str] | None = None

    def _check_approved_source(self, note: Note) -> None:
        if self.approved_source and self.approved_source[0] == note.path:
            from migration import fingerprint
            if fingerprint(note) != self.approved_source[1]:
                raise ValueError("source changed during reviewed execution")

    def _check_approved_remote(self, href: str, remote) -> None:
        if self.approved_versions is not None:
            if href not in self.approved_versions or (remote.etag if remote else None) != self.approved_versions[href]:
                raise PreconditionFailed("remote changed during reviewed execution")

    def run_once(self, paths: Iterable[str] | None = None) -> PushStats:
        stats = PushStats()
        try:
            for candidate in self._candidates(paths, stats):
                try:
                    if self._process(candidate, stats):
                        self.state.remove_fns_pending_note_change(candidate.path)
                except PreconditionFailed:
                    stats.conflicts += 1
                    self.state.review(candidate.path, "remote changed during conditional write; retry after reread")
                except Exception:
                    stats.skipped += 1
                    LOG.warning("keeping failed push pending: %s", candidate.path, exc_info=True)
                finally:
                    self.state.save()
            self.state.mark_push_now()
        finally:
            self.state.save()
        LOG.info("push complete: %s", stats)
        return stats

    def _review(self, path: str, reason: str, stats: PushStats) -> bool:
        previous = self.state.data.get("lifecycle", {}).get("reviews", {}).get(path)
        self.state.review(path, reason)
        stats.review += 1
        stats.skipped += 1
        if previous != reason:
            LOG.warning("task requires review: %s: %s", path, reason)
        return False

    def _process(self, candidate: PushCandidate, stats: PushStats) -> bool:
        path = normalize_path(candidate.path)
        try:
            note = candidate.note or self.fns.get_note(path)
        except FnsError as exc:
            # Absence alone is not deletion evidence. Only an explicit FNS
            # deletion paired with a confirmed missing source may retire objects.
            if candidate.deleted and exc.code == 430:
                self.state.note_record(path)["terminal"] = "DELETED"
                self.state.begin_retirement(path, "confirmed FNS deletion", kinds=("VTODO", "VEVENT"))
                self.state.save()
                return self._retire(path, stats)
            raise
        if candidate.deleted:
            return self._review(path, "source still exists after deletion signal; possible rename", stats)
        self._check_approved_source(note)
        decision = classify_note(path, note.frontmatter, archive_prefixes=self.archive_prefixes)
        if decision.kind == Lifecycle.REVIEW and decision.reason == "no explicit task identity":
            from migration import evidence_signature
            exclusion = self.state.data.get("lifecycle", {}).get("approved_exclusions", {}).get(path)
            if exclusion == evidence_signature(note.frontmatter):
                decision = Decision(Lifecycle.OUT_OF_SCOPE, "explicitly reviewed non-task exclusion")
        if decision.kind == Lifecycle.REVIEW:
            # Ordinary unrelated vault edits are not bridge candidates. Tracked
            # records and Tasks-path records must remain visible in the review queue.
            tracked = path in self.state.data["uid_paths"].values() or path in self.state.data.get("lifecycle", {}).get("notes", {})
            if not tracked and not (path.startswith("Tasks/") or "/Tasks/" in path or
                    any(k in note.frontmatter for k in ("task_status", "cancelled_date")) or
                    "type/task" in str(note.frontmatter.get("tags", ""))):
                stats.skipped += 1
                return True
            return self._review(path, decision.reason, stats)
        record = self.state.note_record(path)
        if record.get("identity_review"):
            return self._review(path, "rename identity needs explicit mapping review", stats)
        if decision.kind == Lifecycle.ACTIVE and record.get("terminal"):
            return self._review(path, "terminal task requires explicit restore", stats)
        if not record and not self._adopt_rename(note, stats, required=candidate.renamed):
            return False
        record = self.state.note_record(path)
        record.update(decision.as_dict())
        record["content_hash"] = note.content_hash
        self.state.clear_review(path)
        if decision.retires_task:
            record["terminal"] = decision.kind.value
            self.state.begin_retirement(path, decision.reason, kinds=("VTODO", "VEVENT"))
            self.state.save()
            return self._retire(path, stats)
        if decision.kind == Lifecycle.COMPLETED:
            record["terminal"] = "COMPLETED"
            self.state.begin_retirement(path, "completed task has no active date projection", kinds=("VEVENT",))
            self.state.save()
        task = Task.from_frontmatter(path, note.frontmatter)
        task_ids = self._targets(path, "VTODO")
        event_ids = self._targets(path, "VEVENT")
        if len(task_ids) > 1 or len(event_ids) > 1:
            return self._review(path, "multiple remote UIDs for one source; reconcile duplicates first", stats)
        task = replace(task, remote_task_uid=task_ids[0]["uid"], remote_event_uid=event_ids[0]["uid"])
        # Do not overwrite an object whose incoming edit is waiting for a source read.
        if any(self.state.path_for_uid(obj.get("uid", "")) == path
               for obj in self.state.pending_pull().values()):
            return self._review(path, "incoming remote update is still pending", stats)
        self._put(task, "VTODO", task_to_vtodo_ics(task, self.fns.vault, note_content=note.content),
                  stats, allow_create=decision.kind == Lifecycle.ACTIVE)
        if task.is_completed:
            stats.completed += 1
        vevent = task_to_vevent_ics(task, self.fns.vault, note_content=note.content)
        intent = self.state.retirement(path)
        if intent and decision.kind == Lifecycle.ACTIVE and task.due_date and intent["reason"] == "no active date projection":
            self.state.data["lifecycle"]["retirements"].pop(path, None)
            intent = None
        if vevent and not (intent and "VEVENT" in intent["kinds"]):
            self._put(task, "VEVENT", vevent, stats)
        else:
            if intent is None:
                self.state.begin_retirement(path, "no active date projection", kinds=("VEVENT",))
            self.state.save()
            if not self._retire(path, stats):
                return False
            # A due-less ACTIVE task can acquire a due date later. Completion and
            # explicit event cancellation retain their suppression until restore.
            if decision.kind == Lifecycle.ACTIVE and not task.due_date and self.state.retirement(path)["reason"] == "no active date projection":
                self.state.data["lifecycle"]["retirements"].pop(path, None)
        return True

    def _targets(self, path: str, kind: str) -> list[dict]:
        collection = self.tasks_collection if kind == "VTODO" else self.events_collection
        records = self.state.object_records()
        prefix = "task-" if kind == "VTODO" else "event-"
        uids = {uid for uid, p in self.state.data["uid_paths"].items() if p == path and uid.startswith(prefix)}
        uids.update(uid for uid, r in records.items() if r["path"] == path and r["kind"] == kind)
        if not uids:
            uids = {task_uid(path) if kind == "VTODO" else event_uid(path)}
        targets = [{"uid": uid, "path": path, "kind": kind, "collection": collection,
                 "href": records.get(uid, {}).get("href") or self.caldav.object_href(collection, uid)}
                for uid in sorted(uids)]
        if any(not in_collection(target["href"], collection) for target in targets):
            raise ValueError("stored object href is outside its configured collection")
        return targets

    def _put(self, task: Task, kind: str, ics: str, stats: PushStats, *, allow_create: bool = True) -> None:
        target = self._targets(task.path, kind)[0]
        remote = self.caldav.get_object(target["href"])
        if remote is None and not allow_create:
            return
        self._check_approved_remote(target["href"], remote)
        if remote and self.approved_versions is None and not task.is_completed and remote.etag != self.state.get_etag(target["collection"], target["href"]):
            key = target["collection"] + ":" + target["href"]
            self.state.pending_pull()[key] = {"collection": target["collection"], "href": target["href"],
                                             "etag": remote.etag, "deleted": False, "uid": target["uid"]}
            self.state.save()
            raise PreconditionFailed("unacknowledged phone change must be pulled before push")
        record = self.state.note_record(task.path)
        if remote:
            old_path = record.get("renamed_from", task.path)
            if record.get("renamed_objects", {}).get(target["uid"]) == task.path:
                old_path = task.path
            component = owned_component(remote, path=old_path, uid=target["uid"], kind=kind,
                                        known_uids={**self.state.data["uid_paths"], target["uid"]: old_path})
            if kind == "VTODO" and str(component.get("STATUS")) == "CANCELLED" and str(component.get("X-BRIDGE-POLICY-VERSION")) != "2" and task.status != "阻塞":
                raise ValueError("legacy CANCELLED requires review before overwrite")
            if kind == "VTODO" and task.is_completed and not task.done_date and component.get("COMPLETED"):
                from icalendar import Calendar
                calendar = Calendar.from_ical(ics)
                next(c for c in calendar.walk() if c.name == "VTODO")["COMPLETED"] = component["COMPLETED"]
                ics = calendar.to_ical().decode()
        result = self.caldav.put_href(target["href"], ics, if_match=remote.etag if remote else None)
        self.state.remember_object(target["uid"], task.path, target["collection"], result.href, kind)
        self.state.set_etag(target["collection"], result.href, result.etag)
        self.state.object_records()[target["uid"]]["fields"] = updates_from_caldav_component(calendar_components(ics)[0], current_frontmatter={"done_date": task.done_date} if task.done_date else {})
        # Mark each component separately; a partial rename may leave the event at
        # the old path while the todo has already moved.
        record.setdefault("renamed_objects", {})[target["uid"]] = task.path
        if result.created:
            stats.created += 1
        else:
            stats.updated += 1
        self.state.save()

    def _retire(self, path: str, stats: PushStats) -> bool:
        intent = self.state.retirement(path)
        if not intent:
            return True
        ok = True
        for kind in intent["kinds"]:
            for target in self._targets(path, kind):
                key = target["collection"] + ":" + target["href"]
                try:
                    try:
                        latest = self.fns.get_note(path)
                    except FnsError as exc:
                        if exc.code != 430 or intent["reason"] != "confirmed FNS deletion":
                            raise
                    else:
                        self._check_approved_source(latest)
                        fresh = classify_note(path, latest.frontmatter, archive_prefixes=self.archive_prefixes)
                        if fresh.kind == Lifecycle.REVIEW and fresh.reason == "no explicit task identity":
                            from migration import evidence_signature
                            if self.state.data.get("lifecycle", {}).get("approved_exclusions", {}).get(path) == evidence_signature(latest.frontmatter):
                                fresh = Decision(Lifecycle.OUT_OF_SCOPE, "explicitly reviewed non-task exclusion")
                        if fresh.kind == Lifecycle.REVIEW or fresh.kind.value != self.state.note_record(path).get("classification"):
                            raise ValueError("source lifecycle changed before retirement")
                        if kind == "VTODO" and not fresh.retires_task:
                            raise ValueError("source is not eligible for task retirement")
                        if kind == "VEVENT" and fresh.kind == Lifecycle.ACTIVE and Task.from_frontmatter(path, latest.frontmatter).due_date and intent["reason"] != "explicit cancellation of calendar projection only":
                            raise ValueError("active source still has a date projection")
                    remote = self.caldav.get_object(target["href"])
                    # Missing targets are harmless even when they were never in
                    # the snapshot; a newly appeared object is not approved.
                    if remote is not None:
                        self._check_approved_remote(target["href"], remote)
                    if remote:
                        record = self.state.note_record(path)
                        old_path = record.get("renamed_from", path)
                        if record.get("renamed_objects", {}).get(target["uid"]) == path:
                            old_path = path
                        owned_component(remote, path=old_path, uid=target["uid"], kind=kind,
                                        known_uids={**self.state.data["uid_paths"], target["uid"]: old_path})
                        deleted = self.caldav.delete_href(target["href"], if_match=remote.etag)
                        if self.caldav.get_object(target["href"]) is not None:
                            raise ValueError("delete readback still contains the object")
                        if deleted:
                            if kind == "VTODO": stats.deleted_tasks += 1
                            else: stats.deleted_events += 1
                    self.state.remove_etag(target["collection"], target["href"])
                    intent["progress"][key] = {"state": "done", "uid": target["uid"]}
                except Exception as exc:
                    ok = False
                    intent["progress"][key] = {"state": "pending", "uid": target["uid"], "error": type(exc).__name__}
                    self.state.review(path, "retirement pending: " + str(exc))
                    if isinstance(exc, PreconditionFailed): stats.conflicts += 1
                    else: stats.skipped += 1
                finally:
                    self.state.save()
        intent["settled"] = ok
        return ok

    def _adopt_rename(self, note: Note, stats: PushStats, *, required: bool = False) -> bool:
        if not note.content_hash:
            if required:
                self.state.note_record(note.path)["identity_review"] = True
            return self._review(note.path, "rename identity is unverified", stats) if required else True
        notes = self.state.data.get("lifecycle", {}).get("notes", {})
        matches = []
        for old, record in list(notes.items()):
            if old == note.path or record.get("content_hash") != note.content_hash:
                continue
            try:
                self.fns.get_note(old)
            except FnsError as exc:
                if exc.code == 430:
                    matches.append(old)
                else:
                    return self._review(note.path, "could not verify possible rename", stats)
        if not matches:
            if required:
                self.state.note_record(note.path)["identity_review"] = True
            return self._review(note.path, "rename identity is unverified", stats) if required else True
        if len(matches) != 1:
            self.state.note_record(note.path)["identity_review"] = True
            return self._review(note.path, "ambiguous rename; multiple missing sources have this hash", stats)
        old = matches[0]
        old_record = notes[old]
        if old_record.get("terminal"):
            return self._review(note.path, "renamed terminal task requires explicit restore", stats)
        self.state.note_record(note.path).update({**old_record, "renamed_from": old, "renamed_objects": {}})
        for uid, path in list(self.state.data["uid_paths"].items()):
            if path == old:
                self.state.remember_uid(uid, note.path)
                if uid in self.state.object_records():
                    self.state.object_records()[uid]["path"] = note.path
        self.state.remove_fns_pending_note_change(old)
        notes[old]["moved_to"] = note.path
        self.state.save()
        return True

    def _candidates(self, paths: Iterable[str] | None, stats: PushStats) -> Iterable[PushCandidate]:
        if paths is not None:
            for path in paths:
                self.state.add_fns_pending_note_change(normalize_path(path))
        elif self._needs_initial_task_scan():
            # Reconcile actual objects, not only today's filtered note candidates.
            objects, reviews = inventory(self.caldav, self.state, (self.tasks_collection, self.events_collection))
            for obj in objects:
                self.state.remember_object(obj["uid"], obj["path"], obj["collection"], obj["href"], obj["kind"])
            for obj in reviews:
                self.state.review(obj["href"], obj["reason"])
            for path in set(self.state.data["uid_paths"].values()):
                self.state.add_fns_pending_note_change(path)
            if self.state.get_fns_note_sync_last_time() is None:
                self.state.set_fns_note_sync_last_time(self._fns_ws().current_note_sync_cursor())
            for path in self.fns.iter_note_paths(keyword=self.fns.task_path_keyword, search_content=False, search_mode="path"):
                self.state.add_fns_pending_note_change(path)
                self.state.save()
            self.state.mark_initial_task_scan_completed()
            self.state.set_caldav_mapping_version(CALDAV_MAPPING_VERSION)
        else:
            result = self._fns_ws().note_sync_since(self.state.get_fns_note_sync_last_time() or 0)
            for message in result.messages:
                # Process rename/modify before deletion so a verified rename can
                # carry old UIDs to its new path before any old-object cleanup.
                action = "delete" if message.deleted else "rename" if message.action == "NoteSyncRename" else "modify"
                self.state.add_fns_pending_note_change(message.path, action)
            self.state.set_fns_note_sync_last_time(result.last_time)
        for path, intent in self.state.data.get("lifecycle", {}).get("retirements", {}).items():
            if not intent.get("settled"):
                self.state.add_fns_pending_note_change(path)
        self.state.save()
        changes = self.state.get_fns_pending_note_changes()
        for change in sorted(changes, key=lambda x: (x["action"] == "delete", x["path"])):
            # A prior rename can consume the queued old-path deletion.
            if change not in self.state.get_fns_pending_note_changes():
                continue
            yield PushCandidate(change["path"], deleted=change["action"] == "delete", renamed=change["action"] == "rename")

    def _needs_initial_task_scan(self) -> bool:
        return not self.state.initial_task_scan_completed() or self.state.get_caldav_mapping_version() != CALDAV_MAPPING_VERSION

    def _fns_ws(self) -> FnsWebSocketClient:
        if self.fns_ws is None:
            raise RuntimeError("FNS WebSocket NoteSync is required for incremental push")
        return self.fns_ws
