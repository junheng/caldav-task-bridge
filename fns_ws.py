from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse, urlunparse

from models import normalize_path


class FnsWsError(RuntimeError):
    pass


class WebSocketLike(Protocol):
    def send(self, payload: str) -> Any: ...

    def recv(self) -> str | bytes: ...

    def close(self) -> Any: ...


ConnectFn = Callable[..., WebSocketLike]


@dataclass(frozen=True)
class NoteSyncMessage:
    action: str
    path: str
    content: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def deleted(self) -> bool:
        return self.action == "NoteSyncDelete"


@dataclass(frozen=True)
class NoteSyncResult:
    last_time: int
    messages: list[NoteSyncMessage]


class FnsWebSocketClient:
    def __init__(
        self,
        ws_url: str,
        token: str,
        vault: str,
        *,
        timeout: float = 30,
        client_type: str = "caldav-bridge",
        client_name: str = "caldav-bridge",
        client_version: str | None = None,
        user_agent: str | None = None,
        connect: ConnectFn | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.token = token
        self.vault = vault
        self.timeout = timeout
        self.client_type = client_type
        self.client_name = client_name
        self.client_version = client_version
        self.user_agent = user_agent
        self._connect_fn = connect

    def current_note_sync_cursor(self) -> int:
        return self.note_sync_since(_now_millis(), context="caldav-bridge-bootstrap").last_time

    def note_sync_since(self, last_time: int | None, *, context: str = "caldav-bridge") -> NoteSyncResult:
        ws = self._connect()
        try:
            self._send_raw(ws, "Authorization", self.token)
            self._expect_success(ws, "Authorization")
            self._send_json(
                ws,
                "ClientInfo",
                {
                    "name": self.client_name,
                    "version": self.client_version or "",
                    "type": self.client_type,
                },
            )
            self._expect_success(ws, "ClientInfo")
            self._send_json(
                ws,
                "NoteSync",
                {
                    "context": context,
                    "vault": self.vault,
                    "lastTime": int(last_time or 0),
                    "notes": [],
                    "delNotes": [],
                    "missingNotes": [],
                },
            )
            return self._read_note_sync_result(ws)
        finally:
            ws.close()

    def _connect(self) -> WebSocketLike:
        connect = self._connect_fn
        if connect is None:
            try:
                import websocket
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise FnsWsError("websocket-client is required for FNS NoteSync") from exc
            connect = websocket.create_connection
        return connect(self.ws_url, header=self._headers(), timeout=self.timeout)

    def _headers(self) -> list[str]:
        headers = [
            f"X-Client: {self.client_type}",
            f"X-Client-Name: {self.client_name}",
        ]
        if self.client_version:
            headers.append(f"X-Client-Version: {self.client_version}")
        if self.user_agent:
            headers.append(f"User-Agent: {self.user_agent}")
        return headers

    def _read_note_sync_result(self, ws: WebSocketLike) -> NoteSyncResult:
        messages: list[NoteSyncMessage] = []
        end_data: dict[str, Any] | None = None
        while end_data is None:
            action, data = self._recv_success(ws)
            if action == "NoteSyncEnd":
                end_data = data
                break
            message = _note_sync_message(action, data)
            if message:
                messages.append(message)
        expected_after_end = _note_sync_message_count(end_data)
        for _ in range(expected_after_end):
            action, data = self._recv_success(ws)
            message = _note_sync_message(action, data)
            if message:
                messages.append(message)
        last_time = _optional_int(end_data.get("lastTime"))
        if last_time is None:
            raise FnsWsError(f"FNS NoteSyncEnd did not include lastTime: {end_data!r}")
        return NoteSyncResult(last_time=last_time, messages=messages)

    def _expect_success(self, ws: WebSocketLike, expected_action: str) -> dict[str, Any]:
        action, data = self._recv_success(ws)
        if action != expected_action:
            raise FnsWsError(f"Expected FNS WS action {expected_action}, got {action}")
        return data

    def _recv_success(self, ws: WebSocketLike) -> tuple[str, dict[str, Any]]:
        action, payload = self._recv(ws)
        if not isinstance(payload, dict):
            raise FnsWsError(f"FNS WS action {action} returned non-object payload")
        if payload.get("status") is False or payload.get("code") == 0:
            raise FnsWsError(f"FNS WS action {action} failed: {payload.get('message') or payload}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise FnsWsError(f"FNS WS action {action} returned non-object data")
        return action, data

    def _recv(self, ws: WebSocketLike) -> tuple[str, Any]:
        raw = ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if "|" not in raw:
            raise FnsWsError(f"Malformed FNS WS frame: {raw!r}")
        action, payload = raw.split("|", 1)
        try:
            return action, json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FnsWsError(f"FNS WS action {action} returned invalid JSON") from exc

    def _send_json(self, ws: WebSocketLike, action: str, payload: dict[str, Any]) -> None:
        self._send_raw(ws, action, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _send_raw(self, ws: WebSocketLike, action: str, payload: str) -> None:
        ws.send(f"{action}|{payload}")


def derive_ws_url(api_url: str) -> str:
    parsed = urlparse(api_url.rstrip("/"))
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise FnsWsError(f"Unsupported FNS URL scheme: {parsed.scheme}")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]
    return urlunparse((scheme, parsed.netloc, f"{path}/api/user/sync", "", "", ""))


def _note_sync_message(action: str, data: dict[str, Any]) -> NoteSyncMessage | None:
    if action not in {"NoteSyncModify", "NoteSyncDelete", "NoteSyncRename", "NoteSyncMtime"}:
        return None
    path = data.get("path")
    if not path:
        return None
    return NoteSyncMessage(
        action=action,
        path=normalize_path(str(path)),
        content=str(data["content"]) if "content" in data and data["content"] is not None else None,
        raw=data,
    )


def _note_sync_message_count(data: dict[str, Any]) -> int:
    return sum(
        _optional_int(data.get(key)) or 0
        for key in ("needModifyCount", "needDeleteCount", "needSyncMtimeCount", "needUploadCount")
    )


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_millis() -> int:
    return int(time.time() * 1000)
