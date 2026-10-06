"""Isolated real-server validation; never writes a source note or production collection."""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import date
from dataclasses import replace

from caldav_client import CalDavClient, PreconditionFailed
from config import Settings
from models import Task, calendar_components, task_to_vtodo_ics, task_to_vevent_ics
from migration import preview
from pull import PullService
from push import PushService
from state import SyncState
from tests.helpers import MemoryFns, MemoryWs, note


def main():
    settings = Settings.from_env()
    client = CalDavClient(settings.radicale_url, settings.radicale_user, settings.radicale_password)
    root = '/' + settings.radicale_user + '/bridge-lifecycle-test-' + uuid.uuid4().hex
    tasks, events = root + '-tasks/', root + '-events/'
    created = []
    checks = []
    try:
        for collection, kind in [(tasks, 'VTODO'), (events, 'VEVENT')]:
            body = f'''<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop><D:displayname>Bridge lifecycle isolated test</D:displayname><C:supported-calendar-component-set><C:comp name="{kind}"/></C:supported-calendar-component-set></D:prop></D:set></C:mkcalendar>'''
            response = client.session.request('MKCALENDAR', client.collection_url(collection), data=body.encode(), headers={'Content-Type': 'application/xml'}, timeout=15)
            assert response.status_code == 201, ('create test collection', response.status_code)
            created.append(collection)
        with tempfile.TemporaryDirectory() as tmp:
            state = SyncState.load(tmp + '/state.json')
            fns = MemoryFns(note('Tasks/Lifecycle-test.md', due_date='2026-10-07'))
            push = PushService(fns, client, state, fns_ws=MemoryWs(), tasks_collection=tasks, events_collection=events)
            pull = PullService(fns, client, state, tasks_collection=tasks, events_collection=events)
            push.run_once()
            task = Task.from_frontmatter('Tasks/Lifecycle-test.md', fns.get_note('Tasks/Lifecycle-test.md').frontmatter)
            todo_href = client.object_href(tasks, task.task_uid)
            event_href = client.object_href(events, task.event_uid)
            assert client.get_object(todo_href) and client.get_object(event_href)
            checks.append('active task creates VTODO and VEVENT')
            before = state.path.read_bytes()
            preview(push)
            assert state.path.read_bytes() == before
            checks.append('read-only preview preserves state bytes')
            old = client.get_object(todo_href)
            from icalendar import Calendar
            cal = Calendar.from_ical(old.ics_text)
            todo = next(c for c in cal.walk() if c.name == 'VTODO')
            todo['STATUS'] = 'COMPLETED'
            todo.add('completed', __import__('datetime').datetime(2026, 10, 6, tzinfo=__import__('datetime').timezone.utc))
            client.put_href(todo_href, cal.to_ical().decode(), if_match=old.etag)
            try:
                client.delete_href(todo_href, if_match=old.etag)
            except PreconditionFailed:
                checks.append('stale ETag deletion returns 412')
            else:
                raise AssertionError('stale deletion must fail')
            pull.run_once()
            assert fns.get_note(task.path).frontmatter['task_status'] == '已完成'
            push.run_once()
            assert client.get_object(event_href) is None
            assert str(calendar_components(client.get_object(todo_href).ics_text)[0]['STATUS']) == 'COMPLETED'
            checks.append('completion writeback preserves VTODO and removes event')
            # A delayed old NEEDS-ACTION from the phone cannot undo completion.
            old = client.get_object(todo_href)
            client.put_href(todo_href, task_to_vtodo_ics(task, 'Core'), if_match=old.etag)
            count = len(fns.patches)
            pull.run_once()
            assert len(fns.patches) == count
            assert fns.get_note(task.path).frontmatter['task_status'] == '已完成'
            checks.append('old phone edit cannot revive completed source')
            cancelled = note('Tasks/Cancelled-test.md', type='reference', cancelled_date='2026-09-08', due_date='2026-10-07')
            fns.notes[cancelled.path] = replace(cancelled, frontmatter={k: v for k, v in cancelled.frontmatter.items() if k != 'task_status'})
            old_task = Task(cancelled.path, 'Cancelled-test', due_date=date(2026, 10, 7))
            client.put_object(tasks, old_task.task_uid, task_to_vtodo_ics(old_task, 'Core'))
            client.put_object(events, old_task.event_uid, task_to_vevent_ics(old_task, 'Core'))
            push.run_once(paths=[cancelled.path])
            assert client.get_object(client.object_href(tasks, old_task.task_uid)) is None
            assert client.get_object(client.object_href(events, old_task.event_uid)) is None
            checks.append('cancelled reference removes both objects without source writes')
            loaded = SyncState.load(str(state.path))
            push = PushService(fns, client, loaded, fns_ws=MemoryWs(), tasks_collection=tasks, events_collection=events)
            loaded.set_caldav_mapping_version(0)
            push.run_once()
            assert client.get_object(client.object_href(tasks, old_task.task_uid)) is None
            checks.append('restart and version rescan do not recreate cancelled task')
    finally:
        for collection in created:
            assert 'bridge-lifecycle-test-' in collection and collection not in (settings.tasks_collection, settings.events_collection)
            response = client.session.delete(client.collection_url(collection), timeout=15)
            if response.status_code not in (200, 204, 404):
                raise RuntimeError('isolated test collection cleanup failed')
    print(json.dumps({'checks': checks, 'test_collections_removed': created}, ensure_ascii=False))


if __name__ == '__main__':
    main()
