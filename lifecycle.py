"""Source evidence shared by push, pull and the read-only migration preview."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from models import normalize_path


class Lifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    REVIEW = "REVIEW"


DEFAULT_ARCHIVE_PREFIXES = ("04 - Archives/",)
VALID_STATUSES = {"待办", "进行中", "阻塞", "已完成", "已取消"}


@dataclass(frozen=True)
class Decision:
    kind: Lifecycle
    reason: str
    status: str | None = None
    cancelled_date: date | None = None
    done_date: date | None = None

    @property
    def retires_task(self) -> bool:
        return self.kind in {Lifecycle.CANCELLED, Lifecycle.OUT_OF_SCOPE}

    def as_dict(self) -> dict[str, Any]:
        return {"classification": self.kind.value, "reason": self.reason,
                "status": self.status,
                "cancelled_date": self.cancelled_date.isoformat() if self.cancelled_date else None,
                "done_date": self.done_date.isoformat() if self.done_date else None}


def scalar(value: Any, name: str) -> Any:
    # FNS sometimes wraps scalar metadata in singleton lists. Multiple values are
    # ambiguous, including ["", "待办"], and must not silently pick the first.
    while isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError(f"{name}: expected one scalar")
        value = value[0]
    if isinstance(value, (dict, set)):
        raise ValueError(f"{name}: expected a scalar")
    return value


def metadata_date(value: Any, name: str) -> date | None:
    value = scalar(value, name)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # Do not accept prefixes such as 2026-09-08nonsense.
        return date.fromisoformat(value.strip())
    raise ValueError(f"{name}: invalid date")


def classify_note(path: str, frontmatter: dict[str, Any], *,
                  archive_prefixes: tuple[str, ...] = DEFAULT_ARCHIVE_PREFIXES) -> Decision:
    try:
        if not isinstance(frontmatter, dict):
            raise ValueError("frontmatter must be a mapping")
        status = scalar(frontmatter.get("task_status"), "task_status")
        if status is not None and not isinstance(status, str):
            raise ValueError("task_status must be text")
        status = status.strip() if status else None
        note_type = scalar(frontmatter.get("type"), "type")
        if note_type is not None and not isinstance(note_type, str):
            raise ValueError("type must be text")
        tags = frontmatter.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError("tags must be text or a list of text")
        explicit_task = note_type == "task" or "type/task" in tags
        reference = note_type == "reference" or "type/reference" in tags
        cancelled = metadata_date(frontmatter.get("cancelled_date"), "cancelled_date")
        done = metadata_date(frontmatter.get("done_date"), "done_date")
        for name in ("due_date", "scheduled_date"):
            metadata_date(frontmatter.get(name), name)
        deleted = scalar(frontmatter.get("deleted"), "deleted")
        if deleted not in (None, "", True, False, "true", "false", "yes", "no", "1", "0", 0, 1):
            raise ValueError("deleted: invalid boolean")
        if explicit_task and reference:
            raise ValueError("both task and reference are declared")
        if (cancelled or status == "已取消") and (done or status == "已完成"):
            raise ValueError("completion and cancellation conflict")
        if status and status not in VALID_STATUSES:
            raise ValueError(f"unknown task status: {status}")
        normalized = normalize_path(path)
        archived = any(normalized.startswith(normalize_path(p).rstrip("/") + "/")
                       for p in archive_prefixes if p.strip())
        # Keep cancellation evidence even when the note has become a reference.
        if cancelled or status == "已取消":
            return Decision(Lifecycle.CANCELLED, "explicit cancellation evidence", status, cancelled, done)
        if reference or archived or deleted in (True, "true", "yes", "1", 1):
            return Decision(Lifecycle.OUT_OF_SCOPE, "reference, archived or explicitly deleted", status, cancelled, done)
        if not (explicit_task or status in VALID_STATUSES):
            return Decision(Lifecycle.REVIEW, "no explicit task identity", status, cancelled, done)
        if not status:
            return Decision(Lifecycle.REVIEW, "task status is missing or empty", status, cancelled, done)
        if done and status != "已完成":
            return Decision(Lifecycle.REVIEW, "completion date conflicts with active status", status, cancelled, done)
        kind = Lifecycle.COMPLETED if status == "已完成" else Lifecycle.ACTIVE
        return Decision(kind, "explicit task identity and valid status", status, cancelled, done)
    except (ValueError, TypeError) as exc:
        return Decision(Lifecycle.REVIEW, str(exc))
