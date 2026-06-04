from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from caldav_client import CalDavClient, PreconditionFailed
from fns_ws import FnsWebSocketClient
from models import Task, event_uid, is_task_note, normalize_path, task_to_vevent_ics, task_to_vtodo_ics, task_uid
from state import SyncState
from vault import FnsClient, FnsError, Note


LOG = logging.getLogger(__name__)
CALDAV_MAPPING_VERSION = 4


@dataclass
class PushStats:
    created: int = 0
    updated: int = 0
    completed: int = 0
    deleted_tasks: int = 0
    deleted_events: int = 0
    conflicts: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class PushCandidate:
    path: str
    note: Note | None = None
    deleted: bool = False


class PushService:
    def __init__(
        self,
        fns: FnsClient,
        caldav: CalDavClient,
        state: SyncState,
        *,
        fns_ws: FnsWebSocketClient | None = None,
        tasks_collection: str,
        events_collection: str,
    ) -> None:
        self.fns = fns
        self.fns_ws = fns_ws
        self.caldav = caldav
        self.state = state
        self.tasks_collection = tasks_collection
        self.events_collection = events_collection

    def run_once(self, paths: Iterable[str] | None = None) -> PushStats:
        stats = PushStats()
        for candidate in self._candidates(paths, stats):
            if candidate.deleted:
                if self._delete_task_objects(candidate.path, stats):
                    self.state.remove_fns_pending_note_change(candidate.path, "delete")
                continue
            note = candidate.note or self._get_note_or_skip(candidate.path, stats)
            if note is None:
                continue
            if not is_task_note(note.path, note.content, note.frontmatter):
                stats.skipped += 1
                self.state.remove_fns_pending_note_change(note.path, "modify")
                continue
            task = Task.from_frontmatter(note.path, note.frontmatter)
            if task.deleted:
                if self._delete_task_objects(task.path, stats):
                    self.state.remove_fns_pending_note_change(task.path, "modify")
                continue
            if self._push_task(task, stats, note_content=note.content):
                self.state.remove_fns_pending_note_change(note.path, "modify")
        self.state.mark_push_now()
        self.state.save()
        LOG.info(
            "push complete: created=%s updated=%s completed=%s deleted_tasks=%s deleted_events=%s conflicts=%s skipped=%s",
            stats.created,
            stats.updated,
            stats.completed,
            stats.deleted_tasks,
            stats.deleted_events,
            stats.conflicts,
            stats.skipped,
        )
        return stats

    def _candidates(self, paths: Iterable[str] | None, stats: PushStats) -> Iterable[PushCandidate]:
        if paths is not None:
            for path in paths:
                yield PushCandidate(normalize_path(path))
            return
        if self._needs_initial_task_scan():
            self.state.set_fns_note_sync_last_time(self._fns_ws().current_note_sync_cursor())
            for note in self.fns.iter_task_notes():
                yield PushCandidate(note.path, note=note)
            self.state.mark_initial_task_scan_completed()
            self.state.set_caldav_mapping_version(CALDAV_MAPPING_VERSION)
            return
        yield from self._changed_note_candidates_from_note_sync()

    def _needs_initial_task_scan(self) -> bool:
        return (
            not self.state.initial_task_scan_completed()
            or self.state.get_caldav_mapping_version() != CALDAV_MAPPING_VERSION
        )

    def _get_note_or_skip(self, path: str, stats: PushStats) -> Note | None:
        try:
            return self.fns.get_note(path)
        except FnsError:
            stats.skipped += 1
            LOG.info("skipping FNS note %s because it could not be read", path, exc_info=True)
            return None

    def _changed_note_candidates_from_note_sync(self) -> Iterable[PushCandidate]:
        last_time = self.state.get_fns_note_sync_last_time()
        result = self._fns_ws().note_sync_since(last_time or 0)
        self.state.set_fns_note_sync_last_time(result.last_time)
        for message in result.messages:
            action = "delete" if message.deleted else "modify"
            self.state.add_fns_pending_note_change(message.path, action)

        seen: set[tuple[str, str]] = set()
        for change in self.state.get_fns_pending_note_changes():
            key = (change["path"], change["action"])
            if key in seen:
                continue
            seen.add(key)
            yield PushCandidate(path=change["path"], deleted=change["action"] == "delete")

    def _fns_ws(self) -> FnsWebSocketClient:
        if self.fns_ws is None:
            raise RuntimeError("FNS WebSocket NoteSync is required for incremental push")
        return self.fns_ws

    def _delete_task_objects(self, path: str, stats: PushStats) -> bool:
        normalized = normalize_path(path)
        task_object_uid = task_uid(normalized)
        event_object_uid = event_uid(normalized)
        if not self._delete_caldav_object(self.tasks_collection, task_object_uid, stats, task=True):
            return False
        if not self._delete_caldav_object(self.events_collection, event_object_uid, stats, task=False):
            return False
        self.state.forget_uid(task_object_uid)
        self.state.forget_uid(event_object_uid)
        return True

    def _delete_caldav_object(self, collection: str, uid: str, stats: PushStats, *, task: bool) -> bool:
        href = self.caldav.object_href(collection, uid)
        if_match = self.state.get_etag(collection, href)
        try:
            deleted = self.caldav.delete_object(collection, uid, if_match=if_match)
        except PreconditionFailed:
            stats.conflicts += 1
            LOG.info("skipping CalDAV delete for %s because remote object changed; pull will reconcile it", uid)
            return False
        if deleted:
            if task:
                stats.deleted_tasks += 1
            else:
                stats.deleted_events += 1
        self.state.remove_etag(collection, href)
        return True

    def _push_task(self, task: Task, stats: PushStats, *, note_content: str = "") -> bool:
        self.state.remember_uid(task.task_uid, task.path)
        self.state.remember_uid(task.event_uid, task.path)
        vtodo = task_to_vtodo_ics(task, self.fns.vault, note_content=note_content)
        todo_href = self.caldav.object_href(self.tasks_collection, task.task_uid)
        todo_if_match = self.state.get_etag(self.tasks_collection, todo_href)
        try:
            todo_result = self.caldav.put_object(self.tasks_collection, task.task_uid, vtodo, if_match=todo_if_match)
        except PreconditionFailed:
            stats.conflicts += 1
            LOG.info("skipping push for %s because CalDAV VTODO changed; pull will reconcile it", task.path)
            return False
        self.state.set_etag(self.tasks_collection, todo_result.href, todo_result.etag)
        _count_put(todo_result.created, stats)
        if task.is_completed:
            stats.completed += 1

        vevent = task_to_vevent_ics(task, self.fns.vault, note_content=note_content)
        if vevent:
            event_href = self.caldav.object_href(self.events_collection, task.event_uid)
            event_if_match = self.state.get_etag(self.events_collection, event_href)
            try:
                event_result = self.caldav.put_object(
                    self.events_collection,
                    task.event_uid,
                    vevent,
                    if_match=event_if_match,
                )
            except PreconditionFailed:
                stats.conflicts += 1
                LOG.info("skipping VEVENT push for %s because CalDAV event changed; pull will reconcile it", task.path)
                return False
            self.state.set_etag(self.events_collection, event_result.href, event_result.etag)
            _count_put(event_result.created, stats)
        else:
            event_href = self.caldav.object_href(self.events_collection, task.event_uid)
            event_if_match = self.state.get_etag(self.events_collection, event_href)
            try:
                deleted = self.caldav.delete_object(self.events_collection, task.event_uid, if_match=event_if_match)
            except PreconditionFailed:
                stats.conflicts += 1
                LOG.info("skipping VEVENT delete for %s because CalDAV event changed; pull will reconcile it", task.path)
                return False
            if deleted:
                stats.deleted_events += 1
                self.state.remove_etag(self.events_collection, event_href)
        return True


def _count_put(created: bool, stats: PushStats) -> None:
    if created:
        stats.created += 1
    else:
        stats.updated += 1
