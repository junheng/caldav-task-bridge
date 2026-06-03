from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from caldav_client import CalDavClient
from models import Task, task_to_vevent_ics, task_to_vtodo_ics
from state import SyncState
from vault import FnsClient, Note


LOG = logging.getLogger(__name__)


@dataclass
class PushStats:
    created: int = 0
    updated: int = 0
    completed: int = 0
    deleted_events: int = 0
    skipped: int = 0


class PushService:
    def __init__(
        self,
        fns: FnsClient,
        caldav: CalDavClient,
        state: SyncState,
        *,
        tasks_collection: str,
        events_collection: str,
    ) -> None:
        self.fns = fns
        self.caldav = caldav
        self.state = state
        self.tasks_collection = tasks_collection
        self.events_collection = events_collection

    def run_once(self, paths: Iterable[str] | None = None) -> PushStats:
        stats = PushStats()
        notes = self._notes(paths)
        for note in notes:
            task = Task.from_frontmatter(note.path, note.frontmatter)
            if task.deleted:
                stats.skipped += 1
                continue
            self._push_task(task, stats)
        self.state.mark_push_now()
        self.state.save()
        LOG.info(
            "push complete: created=%s updated=%s completed=%s deleted_events=%s skipped=%s",
            stats.created,
            stats.updated,
            stats.completed,
            stats.deleted_events,
            stats.skipped,
        )
        return stats

    def _notes(self, paths: Iterable[str] | None) -> Iterable[Note]:
        if paths is None:
            yield from self.fns.iter_task_notes()
            return
        for path in paths:
            yield self.fns.get_note(path)

    def _push_task(self, task: Task, stats: PushStats) -> None:
        self.state.remember_uid(task.task_uid, task.path)
        self.state.remember_uid(task.event_uid, task.path)
        vtodo = task_to_vtodo_ics(task, self.fns.vault)
        todo_result = self.caldav.put_object(self.tasks_collection, task.task_uid, vtodo)
        self.state.set_etag(self.tasks_collection, todo_result.href, todo_result.etag)
        _count_put(todo_result.created, stats)
        if task.is_completed:
            stats.completed += 1

        vevent = task_to_vevent_ics(task, self.fns.vault)
        if vevent:
            event_result = self.caldav.put_object(self.events_collection, task.event_uid, vevent)
            self.state.set_etag(self.events_collection, event_result.href, event_result.etag)
            _count_put(event_result.created, stats)
        else:
            deleted = self.caldav.delete_object(self.events_collection, task.event_uid)
            if deleted:
                stats.deleted_events += 1
                self.state.remove_etag(self.events_collection, self.caldav.object_href(self.events_collection, task.event_uid))


def _count_put(created: bool, stats: PushStats) -> None:
    if created:
        stats.created += 1
    else:
        stats.updated += 1
