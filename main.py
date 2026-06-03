from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable

from caldav_client import CalDavClient
from config import Settings
from pull import PullService
from push import PushService
from state import SyncState
from vault import FnsClient


LOG = logging.getLogger(__name__)


def build_services(settings: Settings) -> tuple[PushService, PullService]:
    state = SyncState.load(settings.sync_state_path)
    fns = FnsClient(
        settings.fns_api_url,
        settings.fns_api_token,
        settings.fns_vault,
        task_search_keyword=settings.task_search_keyword,
    )
    caldav = CalDavClient(
        settings.radicale_url,
        settings.radicale_user,
        settings.radicale_password,
    )
    push_service = PushService(
        fns,
        caldav,
        state,
        tasks_collection=settings.tasks_collection,
        events_collection=settings.events_collection,
    )
    pull_service = PullService(
        fns,
        caldav,
        state,
        tasks_collection=settings.tasks_collection,
        events_collection=settings.events_collection,
    )
    return push_service, pull_service


def run_once(mode: str, push_service: PushService, pull_service: PullService) -> None:
    if mode in {"push", "both"}:
        push_service.run_once()
    if mode in {"pull", "both"}:
        pull_service.run_once()


def run_forever(settings: Settings, push_service: PushService, pull_service: PullService) -> None:
    next_push = 0.0
    next_pull = 0.0
    while True:
        now = time.monotonic()
        if now >= next_push:
            _guarded("push", push_service.run_once)
            next_push = now + settings.push_interval
        if now >= next_pull:
            _guarded("pull", pull_service.run_once)
            next_pull = now + settings.pull_interval
        sleep_for = max(1.0, min(next_push, next_pull) - time.monotonic())
        time.sleep(sleep_for)


def _guarded(name: str, fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        LOG.exception("%s sync failed", name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Obsidian FNS <-> CalDAV task bridge")
    parser.add_argument("--once", choices=["push", "pull", "both"], help="Run one sync cycle and exit")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    settings = Settings.from_env()
    push_service, pull_service = build_services(settings)
    if settings.fns_ws_url:
        LOG.info("FNS_WS_URL is configured but event streaming is not implemented in this MVP")
    if settings.radicale_rabbitmq_url:
        LOG.info("RADICALE_RABBITMQ_URL is configured but RabbitMQ hook consumption is not implemented in this MVP")
    if args.once:
        run_once(args.once, push_service, pull_service)
    else:
        run_forever(settings, push_service, pull_service)


if __name__ == "__main__":
    main()
