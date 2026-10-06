from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from caldav_client import CalDavClient
from config import Settings
from fns_ws import FnsWebSocketClient, derive_ws_url
from pull import PullService
from push import PushService
from state import SyncState
from vault import FnsClient


LOG = logging.getLogger(__name__)
T = TypeVar("T")


def build_services(settings: Settings) -> tuple[PushService, PullService]:
    state = SyncState.load(settings.sync_state_path)
    fns = FnsClient(
        settings.fns_api_url,
        settings.fns_api_token,
        settings.fns_vault,
        task_path_keyword=settings.task_path_keyword,
        client_type=settings.fns_client_type,
        client_name=settings.fns_client_name,
        client_version=settings.fns_client_version,
        user_agent=settings.fns_user_agent,
    )
    caldav = CalDavClient(
        settings.radicale_url,
        settings.radicale_user,
        settings.radicale_password,
    )
    fns_ws = FnsWebSocketClient(
        settings.fns_ws_url or derive_ws_url(settings.fns_api_url),
        settings.fns_api_token,
        settings.fns_vault,
        client_type=settings.fns_client_type,
        client_name=settings.fns_client_name,
        client_version=settings.fns_client_version,
        user_agent=settings.fns_user_agent,
    )
    push_service = PushService(
        fns,
        caldav,
        state,
        fns_ws=fns_ws,
        tasks_collection=settings.tasks_collection,
        events_collection=settings.events_collection,
        archive_prefixes=settings.archive_prefixes,
    )
    pull_service = PullService(
        fns,
        caldav,
        state,
        tasks_collection=settings.tasks_collection,
        events_collection=settings.events_collection,
        archive_prefixes=settings.archive_prefixes,
    )
    return push_service, pull_service


def run_once(mode: str, push_service: PushService, pull_service: PullService) -> None:
    if mode == "both":
        pull_stats = pull_service.run_once()
        if _pull_saw_remote_changes(pull_stats):
            LOG.info("skipping push in this cycle because pull saw CalDAV changes; next cycle will converge")
            return
        push_service.run_once()
        return
    if mode == "push":
        push_service.run_once()
    elif mode == "pull":
        pull_service.run_once()


def run_forever(settings: Settings, push_service: PushService, pull_service: PullService) -> None:
    next_push = 0.0
    next_pull = 0.0
    while True:
        now = time.monotonic()
        pull_saw_remote_changes = False
        if now >= next_pull:
            pull_stats = _guarded("pull", pull_service.run_once)
            pull_saw_remote_changes = bool(pull_stats and _pull_saw_remote_changes(pull_stats))
            next_pull = now + settings.pull_interval
        if now >= next_push:
            if pull_saw_remote_changes:
                defer_for = max(1, min(settings.push_interval, settings.pull_interval))
                LOG.info("deferring push for %s seconds because pull saw CalDAV changes", defer_for)
                next_push = now + defer_for
            else:
                _guarded("push", push_service.run_once)
                next_push = now + settings.push_interval
        sleep_for = max(1.0, min(next_push, next_pull) - time.monotonic())
        time.sleep(sleep_for)


def _guarded(name: str, fn: Callable[[], T]) -> T | None:
    try:
        return fn()
    except Exception:
        LOG.exception("%s sync failed", name)
        return None


def _pull_saw_remote_changes(stats: object) -> bool:
    return any(int(getattr(stats, name, 0)) > 0 for name in ("changed", "written", "deleted", "skipped_unmatched"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Obsidian FNS <-> CalDAV task bridge")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", choices=["push", "pull", "both"], help="Run one sync cycle and exit")
    modes.add_argument("--preview", action="store_true", help="Print a read-only lifecycle plan as JSON")
    modes.add_argument("--apply-preview", metavar="JSON_FILE", help="Apply a reviewed lifecycle plan during maintenance")
    modes.add_argument("--restore", metavar="NOTE_PATH", help="Explicitly restore a source already marked ACTIVE")
    parser.add_argument("--backup-dir", help="New backup directory required by --apply-preview")
    parser.add_argument("--approve-review", action="append", default=[], metavar="NOTE_PATH", help="Explicitly retire a reviewed non-task from the preview")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    settings = Settings.from_env()
    push_service, pull_service = build_services(settings)
    if args.preview:
        from migration import preview
        print(json.dumps(preview(push_service), ensure_ascii=False, indent=2, default=str))
        return
    if settings.radicale_rabbitmq_url:
        LOG.info("RADICALE_RABBITMQ_URL is configured but RabbitMQ hook consumption is not implemented in this MVP")
    with push_service.state.exclusive():
        push_service.state.data = SyncState.load(settings.sync_state_path).data
        if args.apply_preview:
            from migration import apply_preview
            if not args.backup_dir:
                raise ValueError("--backup-dir is required for a reviewed migration")
            with open(args.apply_preview, encoding="utf-8") as handle:
                report = json.load(handle)
            result = apply_preview(report, push_service, args.backup_dir, approve_reviews=tuple(args.approve_review))
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.restore:
            from lifecycle import Lifecycle, classify_note
            from models import normalize_path
            path = normalize_path(args.restore)
            note = push_service.fns.get_note(path)
            if classify_note(path, note.frontmatter, archive_prefixes=settings.archive_prefixes).kind != Lifecycle.ACTIVE:
                raise ValueError("First explicitly restore valid task identity/status and resolve cancellation evidence in Obsidian")
            push_service.state.restore(path)
            push_service.state.save()
            push_service.run_once(paths=[path])
        elif args.once:
            run_once(args.once, push_service, pull_service)
        else:
            run_forever(settings, push_service, pull_service)


if __name__ == "__main__":
    main()
