from __future__ import annotations

import unittest
from datetime import date

from icalendar import Calendar

from models import (
    Task,
    component_obsidian_path,
    display_metadata_value,
    note_body_excerpt,
    path_from_description,
    task_to_vevent_ics,
    task_to_vtodo_ics,
    updates_from_caldav_component,
)


class ModelMappingTests(unittest.TestCase):
    def test_task_from_frontmatter_accepts_single_value_lists_from_fns(self) -> None:
        task = Task.from_frontmatter(
            "Tasks/ListValues.md",
            {
                "task_status": ["进行中"],
                "priority": ["1"],
                "due_date": ["2026-04-04"],
                "scheduled_date": ["2026-04-01"],
                "assignee": ["[[Alice]]"],
                "related_project": [["[[Project]]"]],
                "deleted": ["false"],
            },
        )

        self.assertEqual(task.status, "进行中")
        self.assertEqual(task.priority, 1)
        self.assertEqual(task.due_date, date(2026, 4, 4))
        self.assertEqual(task.scheduled_date, date(2026, 4, 1))
        self.assertEqual(task.assignee, "[[Alice]]")
        self.assertEqual(task.related_project, "[[Project]]")
        self.assertFalse(task.deleted)

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

        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core", note_content="---\ntitle: x\n---\n\nBody line"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")

        self.assertEqual(str(todo["STATUS"]), "IN-PROCESS")
        self.assertEqual(int(todo["PRIORITY"]), 1)
        self.assertEqual(todo["DUE"].dt, date(2026, 6, 10))
        self.assertEqual(todo["DTSTART"].dt, date(2026, 6, 5))
        self.assertEqual(str(todo["X-OBSIDIAN-PATH"]), "Tasks/Example.md")
        self.assertEqual(str(todo["URL"]), "obsidian://open?vault=Core&file=Tasks/Example.md")
        self.assertEqual(str(todo["ATTACH"]), "obsidian://open?vault=Core&file=Tasks/Example.md")
        self.assertIn("Assignee: Alice", str(todo["DESCRIPTION"]))
        self.assertIn("Project: Project", str(todo["DESCRIPTION"]))
        self.assertNotIn("[[Alice]]", str(todo["DESCRIPTION"]))
        self.assertIn("Body line", str(todo["DESCRIPTION"]))

    def test_display_metadata_value_unwraps_obsidian_wikilinks(self) -> None:
        self.assertEqual(display_metadata_value("[[People/Alice|Alice A.]]"), "Alice A.")
        self.assertEqual(display_metadata_value("[[Project]] / [[People/Bob]]"), "Project / Bob")

    def test_overdue_vevent_keeps_real_due_date(self) -> None:
        task = Task(
            path="Tasks/Late.md",
            title="Late",
            status="待办",
            due_date=date(2026, 6, 1),
        )

        calendar = Calendar.from_ical(task_to_vevent_ics(task, "Core", today=date(2026, 6, 3), note_content="Event body"))
        event = next(component for component in calendar.walk() if component.name == "VEVENT")

        self.assertEqual(event["DTSTART"].dt, date(2026, 6, 1))
        self.assertEqual(event["DTEND"].dt, date(2026, 6, 2))
        self.assertTrue(str(event["SUMMARY"]).startswith("\U0001f6a8"))
        self.assertEqual(str(event["STATUS"]), "CONFIRMED")
        self.assertEqual(str(event["URL"]), "obsidian://open?vault=Core&file=Tasks/Late.md")
        self.assertEqual(str(event["ATTACH"]), "obsidian://open?vault=Core&file=Tasks/Late.md")
        self.assertIn("Event body", str(event["DESCRIPTION"]))

    def test_vevent_pull_uses_dtstart_as_due_date(self) -> None:
        task = Task(path="Tasks/Moved.md", title="Moved", due_date=date(2026, 6, 10))
        calendar = Calendar.from_ical(task_to_vevent_ics(task, "Core", today=date(2026, 6, 3)))
        event = next(component for component in calendar.walk() if component.name == "VEVENT")

        updates = updates_from_caldav_component(event, today=date(2026, 6, 3))

        self.assertEqual(updates["due_date"], "2026-06-10")

    def test_completed_vtodo_maps_to_fns_frontmatter_updates(self) -> None:
        task = Task(path="Tasks/Done.md", title="Done", status="已完成", priority=1)
        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")

        updates = updates_from_caldav_component(todo, today=date(2026, 6, 3))

        self.assertEqual(updates["task_status"], "已完成")
        self.assertEqual(updates["done_date"], "2026-06-03")
        self.assertEqual(updates["priority"], 1)

    def test_component_obsidian_path_can_fall_back_to_url(self) -> None:
        task = Task(path="Tasks/Linked.md", title="Linked")
        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")
        del todo["X-OBSIDIAN-PATH"]
        del todo["DESCRIPTION"]

        self.assertEqual(component_obsidian_path(todo), "Tasks/Linked.md")

    def test_component_obsidian_path_can_fall_back_to_attach(self) -> None:
        task = Task(path="Tasks/Attached.md", title="Attached")
        calendar = Calendar.from_ical(task_to_vtodo_ics(task, "Core"))
        todo = next(component for component in calendar.walk() if component.name == "VTODO")
        del todo["X-OBSIDIAN-PATH"]
        del todo["URL"]
        del todo["DESCRIPTION"]

        self.assertEqual(component_obsidian_path(todo), "Tasks/Attached.md")

    def test_note_body_excerpt_strips_frontmatter_and_truncates(self) -> None:
        content = "---\ntitle: hidden\n---\n\n" + ("x" * 20)

        self.assertEqual(note_body_excerpt(content, limit=10), "xxxxxxxxxx\n...[truncated]")

    def test_path_from_description_accepts_labeled_obsidian_link(self) -> None:
        self.assertEqual(
            path_from_description("Body\n\nObsidian: obsidian://open?vault=Core&file=Tasks/Linked.md"),
            "Tasks/Linked.md",
        )


if __name__ == "__main__":
    unittest.main()
