from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import md5
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

from icalendar import Calendar, Event, Todo


STATUS_TO_CALDAV = {
    "待办": "NEEDS-ACTION",
    "进行中": "IN-PROCESS",
    "已完成": "COMPLETED",
    "阻塞": "CANCELLED",
}

CALDAV_TO_STATUS = {
    "NEEDS-ACTION": "待办",
    "IN-PROCESS": "进行中",
    "COMPLETED": "已完成",
    "CANCELLED": "阻塞",
}

PRIORITY_TO_CALDAV = {1: 1, 2: 5, 3: 9}


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def path_hash(path: str) -> str:
    return md5(normalize_path(path).encode("utf-8")).hexdigest()[:12]


def task_uid(path: str) -> str:
    return f"task-{path_hash(path)}@core-vault"


def event_uid(path: str) -> str:
    return f"event-{path_hash(path)}@core-vault"


def title_from_path(path: str) -> str:
    return PurePosixPath(normalize_path(path)).stem


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return date.fromisoformat(text[:10])
    raise ValueError(f"Unsupported date value: {value!r}")


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return bool(value)


def coerce_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


def normalize_priority(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 2
    if priority <= 1:
        return 1
    if priority >= 3:
        return 3
    return 2


def caldav_priority_to_obsidian(value: Any) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return 2
    if priority <= 3:
        return 1
    if priority >= 7:
        return 3
    return 2


@dataclass(frozen=True)
class Task:
    path: str
    title: str
    status: str = "待办"
    priority: int = 2
    due_date: date | None = None
    scheduled_date: date | None = None
    assignee: str | None = None
    related_project: str | None = None
    tags: list[str] = field(default_factory=list)
    deleted: bool = False

    @classmethod
    def from_frontmatter(cls, path: str, frontmatter: dict[str, Any]) -> "Task":
        normalized = normalize_path(path)
        status = str(frontmatter.get("task_status") or "待办")
        return cls(
            path=normalized,
            title=title_from_path(normalized),
            status=status,
            priority=normalize_priority(frontmatter.get("priority", 2)),
            due_date=parse_date(frontmatter.get("due_date")),
            scheduled_date=parse_date(frontmatter.get("scheduled_date")),
            assignee=_optional_str(frontmatter.get("assignee")),
            related_project=_optional_str(frontmatter.get("related_project")),
            tags=coerce_tags(frontmatter.get("tags")),
            deleted=coerce_bool(frontmatter.get("deleted")),
        )

    @property
    def task_uid(self) -> str:
        return task_uid(self.path)

    @property
    def event_uid(self) -> str:
        return event_uid(self.path)

    @property
    def is_completed(self) -> bool:
        return self.status == "已完成"


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def is_task_note(path: str, content: str, frontmatter: dict[str, Any]) -> bool:
    tags = set(coerce_tags(frontmatter.get("tags")))
    note_type = str(frontmatter.get("type") or "").strip().lower()
    normalized = normalize_path(path)
    return (
        "task_status" in frontmatter
        or "type/task" in tags
        or note_type == "task"
        or normalized.startswith("Tasks/")
        or "/Tasks/" in normalized
        or "type/task" in content
    )


def task_to_vtodo_ics(task: Task, vault_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    calendar = _calendar()
    todo = Todo()
    todo.add("uid", task.task_uid)
    todo.add("summary", task.title)
    todo.add("status", STATUS_TO_CALDAV.get(task.status, "NEEDS-ACTION"))
    todo.add("priority", PRIORITY_TO_CALDAV.get(task.priority, 5))
    todo.add("description", build_description(task, vault_name))
    todo.add("x-obsidian-path", task.path)
    if task.tags:
        todo.add("categories", task.tags)
    if task.due_date:
        todo.add("due", task.due_date)
    if task.scheduled_date:
        todo.add("dtstart", task.scheduled_date)
    if task.is_completed:
        todo.add("completed", now)
    calendar.add_component(todo)
    return calendar.to_ical().decode("utf-8")


def task_to_vevent_ics(task: Task, vault_name: str, today: date | None = None) -> str | None:
    if task.due_date is None:
        return None
    today = today or date.today()
    overdue = task.due_date < today and not task.is_completed
    display_date = today if overdue else task.due_date
    calendar = _calendar()
    event = Event()
    event.add("uid", task.event_uid)
    event.add("dtstart", display_date)
    event.add("dtend", display_date + timedelta(days=1))
    prefix = "\U0001f6a8" if overdue else "\U0001f4cb"
    event.add("summary", f"{prefix} {task.title}")
    event.add("description", obsidian_url(vault_name, task.path))
    event.add("status", "CANCELLED" if task.is_completed else "CONFIRMED")
    event.add("x-obsidian-path", task.path)
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def build_description(task: Task, vault_name: str) -> str:
    parts: list[str] = []
    if task.assignee:
        parts.append(f"Assignee: {task.assignee}")
    if task.related_project:
        parts.append(f"Project: {task.related_project}")
    parts.append(obsidian_url(vault_name, task.path))
    return "\n".join(parts)


def obsidian_url(vault_name: str, path: str) -> str:
    return f"obsidian://open?vault={quote(vault_name)}&file={quote(normalize_path(path))}"


def calendar_components(ics_text: str | bytes) -> list[Any]:
    calendar = Calendar.from_ical(ics_text)
    return [component for component in calendar.walk() if component.name in {"VTODO", "VEVENT"}]


def component_uid(component: Any) -> str | None:
    value = component.get("UID")
    if value is None:
        return None
    return str(value)


def component_obsidian_path(component: Any) -> str | None:
    custom = component.get("X-OBSIDIAN-PATH")
    if custom:
        return normalize_path(str(custom))
    description = component.get("DESCRIPTION")
    if description:
        return path_from_description(str(description))
    return None


def path_from_description(description: str) -> str | None:
    marker = "obsidian://open"
    if marker not in description:
        return None
    for line in description.splitlines():
        if marker not in line:
            continue
        parsed = urlparse(line.strip())
        query = parse_qs(parsed.query)
        values = query.get("file")
        if values:
            return normalize_path(unquote(values[0]))
    return None


def updates_from_caldav_component(component: Any, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    updates: dict[str, Any] = {}
    status = _component_text(component, "STATUS")
    if status:
        mapped_status = CALDAV_TO_STATUS.get(status.upper())
        if mapped_status:
            updates["task_status"] = mapped_status
            if mapped_status == "已完成":
                updates["done_date"] = today.isoformat()

    if component.name == "VTODO":
        priority = component.get("PRIORITY")
        if priority is not None:
            updates["priority"] = str(caldav_priority_to_obsidian(priority))
        due = _component_date(component, "DUE")
        if due:
            updates["due_date"] = due.isoformat()
    elif component.name == "VEVENT":
        start = _component_date(component, "DTSTART")
        if start:
            updates["due_date"] = start.isoformat()
    return updates


def _calendar() -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", "-//caldav-task-bridge//EN")
    calendar.add("version", "2.0")
    return calendar


def _component_text(component: Any, field: str) -> str | None:
    value = component.get(field)
    if value is None:
        return None
    return str(value)


def _component_date(component: Any, field: str) -> date | None:
    value = component.get(field)
    if value is None:
        return None
    raw = getattr(value, "dt", value)
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return parse_date(str(raw))
