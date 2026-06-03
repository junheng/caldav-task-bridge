from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from caldav_client import CalDavClient, CollectionObject
from models import (
    calendar_components,
    component_obsidian_path,
    component_uid,
    updates_from_caldav_component,
)
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


class PullService:
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
        self.collections = (tasks_collection, events_collection)

    def run_once(self) -> PullStats:
        stats = PullStats()
        for collection in self.collections:
            self._pull_collection(collection, stats)
        self.state.save()
        LOG.info(
            "pull complete: changed=%s written=%s deleted=%s skipped_echo=%s skipped_unmatched=%s",
            stats.changed,
            stats.written,
            stats.deleted,
            stats.skipped_echo,
            stats.skipped_unmatched,
        )
        return stats

    def _pull_collection(self, collection: str, stats: PullStats) -> None:
        result = self.caldav.sync_collection(collection, self.state.get_sync_token(collection))
        for obj in result.objects:
            self._handle_object(collection, obj, stats)
        self.state.set_sync_token(collection, result.sync_token)

    def _handle_object(self, collection: str, obj: CollectionObject, stats: PullStats) -> None:
        if obj.deleted:
            self.state.remove_etag(collection, obj.href)
            stats.deleted += 1
            return
        if obj.etag and self.state.get_etag(collection, obj.href) == obj.etag:
            stats.skipped_echo += 1
            return
        stats.changed += 1
        ics_text = self.caldav.get_href(obj.href)
        wrote_any = False
        for component in calendar_components(ics_text):
            uid = component_uid(component)
            if not uid:
                continue
            path = self.state.path_for_uid(uid) or component_obsidian_path(component)
            if not path:
                stats.skipped_unmatched += 1
                LOG.warning("Could not map CalDAV UID %s back to an Obsidian path", uid)
                continue
            updates = updates_from_caldav_component(component, today=date.today())
            if not updates:
                continue
            self.fns.patch_frontmatter(path, updates)
            self.state.remember_uid(uid, path)
            wrote_any = True
        if obj.etag:
            self.state.set_etag(collection, obj.href, obj.etag)
        if wrote_any:
            stats.written += 1
