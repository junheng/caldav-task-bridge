from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from caldav_client import CollectionObject, PreconditionFailed, PutResult, RemoteObject, SyncResult
from fns_ws import NoteSyncResult
from models import Task, task_to_vtodo_ics, task_to_vevent_ics
from vault import FnsError, Note

TASKS = '/diomgis/tasks/'
EVENTS = '/diomgis/core-vault/'


def note(path='Tasks/A.md', status='待办', **fields):
    fm = {'type': 'task', 'task_status': status, **fields}
    text = json.dumps(fm, ensure_ascii=False, default=str)
    return Note(path, text, fm, hashlib.sha256(text.encode()).hexdigest())


class MemoryFns:
    vault = 'Core'
    task_path_keyword = 'Tasks'

    def __init__(self, *notes):
        self.notes = {n.path: n for n in notes}
        self.patches = []
        self.fail_paths = set()
        self.scan_fail = False

    def get_note(self, path):
        if path in self.fail_paths:
            raise FnsError('temporary failure')
        if path not in self.notes:
            raise FnsError('Note does not exist', code=430)
        return self.notes[path]

    def iter_note_paths(self, **kwargs):
        for path in self.notes:
            if 'Tasks' in path:
                yield path
                if self.scan_fail:
                    raise FnsError('scan interrupted')

    def patch_frontmatter(self, path, updates):
        self.patches.append((path, updates))
        n = self.notes[path]
        fm = {**n.frontmatter, **updates}
        text = json.dumps(fm, ensure_ascii=False, default=str)
        self.notes[path] = replace(n, frontmatter=fm, content=text, content_hash=hashlib.sha256(text.encode()).hexdigest())
        return {}


class MemoryDav:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.deletes = []
        self.fail_delete = set()
        self.counter = 0
        self.report_objects = None
        self.read_fail = set()

    def object_href(self, collection, uid):
        return f'{collection}{uid}.ics'

    def get_object(self, href):
        if href in self.read_fail:
            raise ConnectionError('read interrupted')
        return self.objects.get(href)

    def sync_collection(self, collection, token):
        objects = self.report_objects if self.report_objects is not None else [CollectionObject(o.href, o.etag) for o in self.objects.values()]
        return SyncResult([o for o in objects if o.href.startswith(collection)], 'token-new')

    def put_href(self, href, ics_text, *, if_match=None):
        old = self.objects.get(href)
        if (old and old.etag != if_match) or (not old and if_match):
            raise PreconditionFailed('changed object')
        self.counter += 1
        etag = f'"etag-{self.counter}"'
        self.objects[href] = RemoteObject(href, etag, ics_text)
        self.puts.append((href, if_match))
        return PutResult(href, etag, old is None)

    def delete_href(self, href, *, if_match):
        if href in self.fail_delete:
            raise PreconditionFailed('concurrent event edit')
        old = self.objects.get(href)
        if not old:
            return False
        if old.etag != if_match:
            raise PreconditionFailed('changed object')
        self.deletes.append((href, if_match))
        del self.objects[href]
        return True

    def seed(self, task, *, todo=True, event=True, href=None):
        for kind, collection, uid, ics in [('VTODO', TASKS, task.task_uid, task_to_vtodo_ics(task, 'Core')),
                                           ('VEVENT', EVENTS, task.event_uid, task_to_vevent_ics(task, 'Core'))]:
            if not ics or (kind == 'VTODO' and not todo) or (kind == 'VEVENT' and not event):
                continue
            target = href if kind == 'VTODO' and href else self.object_href(collection, uid)
            self.counter += 1
            self.objects[target] = RemoteObject(target, f'"etag-{self.counter}"', ics)


class MemoryWs:
    cursor = 1000
    messages = ()

    def current_note_sync_cursor(self):
        return self.cursor

    def note_sync_since(self, last_time):
        return NoteSyncResult(self.cursor, list(self.messages))
