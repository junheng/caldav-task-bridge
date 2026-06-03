from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


@dataclass(frozen=True)
class Settings:
    radicale_url: str
    radicale_user: str
    radicale_password: str
    fns_api_url: str
    fns_api_token: str
    fns_vault: str
    sync_state_path: str = "./data/state.json"
    push_interval: int = 900
    pull_interval: int = 300
    tasks_collection: str = "/diomgis/tasks/"
    events_collection: str = "/diomgis/core-vault/"
    fns_ws_url: str | None = None
    radicale_rabbitmq_url: str | None = None
    radicale_rabbitmq_topic: str | None = None
    task_path_keyword: str = "Tasks"
    fns_client_type: str = "caldav-bridge"
    fns_client_name: str = "caldav-bridge"
    fns_client_version: str | None = None
    fns_user_agent: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            radicale_url=_required("RADICALE_URL"),
            radicale_user=_required("RADICALE_USER"),
            radicale_password=_required("RADICALE_PASSWORD"),
            fns_api_url=_required("FNS_API_URL"),
            fns_api_token=_required("FNS_API_TOKEN"),
            fns_vault=_required("FNS_VAULT"),
            sync_state_path=_optional("SYNC_STATE_PATH", "./data/state.json") or "./data/state.json",
            push_interval=_optional_int("PUSH_INTERVAL", 900),
            pull_interval=_optional_int("PULL_INTERVAL", 300),
            tasks_collection=_optional("TASKS_COLLECTION", "/diomgis/tasks/") or "/diomgis/tasks/",
            events_collection=_optional("EVENTS_COLLECTION", "/diomgis/core-vault/") or "/diomgis/core-vault/",
            fns_ws_url=_optional("FNS_WS_URL"),
            radicale_rabbitmq_url=_optional("RADICALE_RABBITMQ_URL"),
            radicale_rabbitmq_topic=_optional("RADICALE_RABBITMQ_TOPIC"),
            task_path_keyword=_optional("TASK_PATH_KEYWORD", "Tasks") or "Tasks",
            fns_client_type=_optional("FNS_CLIENT_TYPE", "caldav-bridge") or "caldav-bridge",
            fns_client_name=_optional("FNS_CLIENT_NAME", "caldav-bridge") or "caldav-bridge",
            fns_client_version=_optional("FNS_CLIENT_VERSION"),
            fns_user_agent=_optional("FNS_USER_AGENT"),
        )
