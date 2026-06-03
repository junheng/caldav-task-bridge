from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from caldav_client import CalDavClient  # noqa: E402
from config import Settings  # noqa: E402
from fns_ws import FnsWebSocketClient, derive_ws_url  # noqa: E402
from models import Task  # noqa: E402
from vault import FnsClient  # noqa: E402


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        raise RuntimeError(f"env file does not exist: {path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    args = parse_args()
    if args.env_file:
        load_env_file(args.env_file)

    settings = Settings.from_env()
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
    fns_ws = FnsWebSocketClient(
        settings.fns_ws_url or derive_ws_url(settings.fns_api_url),
        settings.fns_api_token,
        settings.fns_vault,
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

    print(
        "smoke: using FNS note client "
        f"X-Client={settings.fns_client_type} X-Client-Name={settings.fns_client_name}; "
        f"ws={fns_ws.ws_url}"
    )
    smoke_fns_notes(fns, settings.task_path_keyword)
    smoke_fns_note_sync(fns_ws)
    smoke_caldav_collection(caldav, settings.tasks_collection, "tasks")
    smoke_caldav_collection(caldav, settings.events_collection, "events")
    if not args.skip_discovery:
        smoke_task_discovery(fns)
    print("smoke: OK")
    return 0


def smoke_fns_notes(fns: FnsClient, task_path_keyword: str) -> None:
    items, pager = fns.list_notes(keyword=task_path_keyword, search_content=False, search_mode="path", page=1)
    print(f"smoke: FNS path search returned {len(items)} candidate note(s) on first page")
    if not items:
        raise RuntimeError(f"FNS path search found no notes for TASK_PATH_KEYWORD={task_path_keyword!r}")
    first_path = items[0].get("path")
    if not first_path:
        raise RuntimeError("FNS /api/notes returned an item without path")
    note = fns.get_note(str(first_path))
    print(f"smoke: FNS get_note succeeded for first candidate: {note.path}")
    print(f"smoke: FNS notes pager keys: {sorted(pager.keys())}")


def smoke_fns_note_sync(fns_ws: FnsWebSocketClient) -> None:
    result = fns_ws.note_sync_since(int(time.time() * 1000), context="caldav-bridge-smoke")
    print(
        "smoke: FNS WS NoteSync succeeded: "
        f"lastTime={result.last_time}, message(s)={len(result.messages)}"
    )


def smoke_caldav_collection(caldav: CalDavClient, collection: str, label: str) -> None:
    result = caldav.sync_collection(collection, None)
    print(
        f"smoke: CalDAV {label} collection readable: "
        f"{len(result.objects)} object(s), token={'yes' if result.sync_token else 'no'}"
    )


def smoke_task_discovery(fns: FnsClient) -> None:
    notes = list(fns.iter_task_notes())
    if not notes:
        raise RuntimeError("task discovery found no task notes")
    tasks = [Task.from_frontmatter(note.path, note.frontmatter) for note in notes]
    status_counts = Counter(task.status for task in tasks)
    print(f"smoke: task discovery found {len(notes)} task note(s)")
    print(f"smoke: task statuses: {_format_counter(status_counts)}")


def _format_counter(counter: Counter[object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items(), key=lambda item: str(item[0])))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only integration smoke test for real FNS and CalDAV credentials")
    parser.add_argument("--env-file", help="Path to local env file with real credentials, e.g. .env")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip full task discovery over TASK_PATH_KEYWORD")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
