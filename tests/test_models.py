from __future__ import annotations

import unittest
from datetime import date

from icalendar import Calendar

from models import Task, component_obsidian_path, task_to_vevent_ics, task_to_vtodo_ics, updates_from_caldav_component


class ModelMappingTests(unittest.TestCase):
    def test_task_to_vtodo_maps_frontmatter_fields(self) -> None:
        task = Task(
            path="Tasks/Example.md",
            title="Example",
            status="进行中",
            priority=1,
            due_date=date(2026, 6, 10),
            scheduled_date=date(2026, 6, 5),
            assignee="[[Alice]]",
            related_project="[[Project]]",
            tags=["type/task", "work"],
        )

        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")

        self.assertEqual(str(todo["STATUS"]), "IN-PROCESS")
        self.assertEqual(int(todo["PRIORITY"]), 1)
        self.assertEqual(todo["DUE"].dt, date(2026, 6, 10))
        self.assertEqual(todo["DTSTART"].dt, date(2026, 6, 5))
        self.assertEqual(str(todo["X-OBSIDIAN-PATH"]), "Tasks/Example.md")
        self.assertEqual(str(todo["URL"]), "obsidian://open?vault=Core&file=Tasks/Example.md")

    def test_overdue_vevent_is_shown_today(self) -> None:
        task = Task(
            path="Tasks/Late.md",
            title="Late",
            status="待办",
            due_date=date(2026, 6, 1),
        )

        calendar = Calendar.from_ical(task_to_vevent_ics(task, "Core", today=date(2026, 6, 3)))
        event = next(component for component in calendar.walk() if component.name == "VEVENT")

        self.assertEqual(event["DTSTART"].dt, date(2026, 6, 3))
        self.assertEqual(event["DTEND"].dt, date(2026, 6, 4))
        self.assertEqual(str(event["STATUS"]), "CONFIRMED")
        self.assertEqual(str(event["URL"]), "obsidian://open?vault=Core&file=Tasks/Late.md")

    def test_completed_vtodo_maps_to_fns_frontmatter_updates(self) -> None:
        task = Task(path="Tasks/Done.md", title="Done", status="已完成")
        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")

        updates = updates_from_caldav_component(todo, today=date(2026, 6, 3))

        self.assertEqual(updates["task_status"], "已完成")
        self.assertEqual(updates["done_date"], "2026-06-03")

    def test_component_obsidian_path_can_fall_back_to_url(self) -> None:
        task = Task(path="Tasks/Linked.md", title="Linked")
        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")
        del todo["X-OBSIDIAN-PATH"]
        del todo["DESCRIPTION"]

        self.assertEqual(component_obsidian_path(todo), "Tasks/Linked.md")


if __name__ == "__main__":
    unittest.main()
