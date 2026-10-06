"""Read-only planning and source/version-checked application of a reviewed plan."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from lifecycle import Lifecycle, classify_note
from managed_objects import inventory, owned_component
from models import Task, event_uid, task_uid
from push import PushCandidate, PushStats


EVIDENCE_KEYS = ('type', 'tags', 'task_status', 'cancelled_date', 'done_date', 'due_date', 'scheduled_date', 'deleted')


def fingerprint(note) -> str:
    data = [note.path, note.content_hash, note.content, note.frontmatter]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def evidence_signature(frontmatter) -> str:
    data = {k: frontmatter.get(k) for k in EVIDENCE_KEYS}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def preview(push) -> dict:
    """Never invoke sync services, save state, or accept a new local cursor."""
    objects, unmatched = inventory(push.caldav, push.state, (push.tasks_collection, push.events_collection))
    paths = set(push.state.data['uid_paths'].values()) | {o['path'] for o in objects}
    paths.update(push.fns.iter_note_paths(keyword=push.fns.task_path_keyword, search_content=False, search_mode='path'))
    rows = []
    for path in sorted(paths):
        row = {'path': path, 'objects': [], 'source_fingerprint': None}
        remote_objects = [o for o in objects if o['path'] == path]
        try:
            note = push.fns.get_note(path)
            decision = classify_note(path, note.frontmatter, archive_prefixes=push.archive_prefixes)
            row.update(decision.as_dict())
            row['source_fingerprint'] = fingerprint(note)
            row['evidence'] = {k: note.frontmatter.get(k) for k in EVIDENCE_KEYS}
            if decision.kind == Lifecycle.ACTIVE and push.state.data.get('lifecycle', {}).get('notes', {}).get(path, {}).get('terminal'):
                row.update(classification='REVIEW', reason='terminal task requires explicit restore')
            if len([o for o in remote_objects if o['kind'] == 'VTODO']) > 1 or len([o for o in remote_objects if o['kind'] == 'VEVENT']) > 1:
                row.update(classification='REVIEW', reason='duplicate remote objects require reconciliation')
            if any(o['kind'] == 'VTODO' and o['status'] == 'CANCELLED' and o['policy_version'] != '2' for o in remote_objects) and decision.kind == Lifecycle.ACTIVE and decision.status != '阻塞':
                row.update(classification='REVIEW', reason='ambiguous legacy CANCELLED')
            row['overdue'] = bool(decision.kind == Lifecycle.ACTIVE and Task.from_frontmatter(path, note.frontmatter).due_date and Task.from_frontmatter(path, note.frontmatter).due_date < datetime.now().date())
        except Exception as exc:
            row.update(classification='REVIEW', reason='source read failed: ' + type(exc).__name__)
        for obj in remote_objects:
            item = {k: v for k, v in obj.items() if k not in ('ics_text', 'path')}
            if row['classification'] == 'REVIEW':
                action = 'review'
            elif row['classification'] in ('CANCELLED', 'OUT_OF_SCOPE'):
                action = 'delete'
            elif obj['kind'] == 'VEVENT' and (row['classification'] == 'COMPLETED' or not row['evidence'].get('due_date')):
                action = 'delete'
            elif obj['kind'] == 'VTODO' and row['classification'] == 'COMPLETED':
                action = 'update_completed'
            else:
                action = 'sync_active'
            item['action'] = action
            row['objects'].append(item)
        if row['classification'] == 'ACTIVE':
            task = Task.from_frontmatter(path, note.frontmatter)
            for kind, collection, uid in [('VTODO', push.tasks_collection, task_uid(path)),
                                           ('VEVENT', push.events_collection, event_uid(path))]:
                if not any(o['kind'] == kind for o in remote_objects) and (kind == 'VTODO' or task.due_date):
                    row['objects'].append({'uid': uid, 'kind': kind, 'collection': collection,
                                           'href': push.caldav.object_href(collection, uid), 'etag': None,
                                           'status': None, 'policy_version': None, 'action': 'create'})
        rows.append(row)
    return {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'vault': push.fns.vault,
            'collections': [push.tasks_collection, push.events_collection],
            'archive_prefixes': list(push.archive_prefixes), 'notes': rows, 'unmatched_objects': unmatched,
            'summary': {'notes_by_classification': dict(Counter(r['classification'] for r in rows)),
                        'objects_by_action': dict(Counter(o['action'] for r in rows for o in r['objects'])),
                        'delete_objects_by_kind': dict(Counter(o['kind'] for r in rows for o in r['objects'] if o['action'] == 'delete'))}}


def apply_preview(report: dict, push, backup_dir: str, *, approve_reviews: tuple[str, ...] = ()) -> dict:
    if report.get('schema') != 1 or report.get('collections') != [push.tasks_collection, push.events_collection] or report.get('vault') != push.fns.vault or report.get('archive_prefixes') != list(push.archive_prefixes):
        raise ValueError('preview scope does not match the configured deployment')
    backup = Path(backup_dir)
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Snapshot before saving any lifecycle decisions, using raw bytes where present.
    if push.state.path.exists():
        (backup / 'state.json').write_bytes(push.state.path.read_bytes())
    (backup / 'preview.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    receipts = []
    for row in report['notes']:
        path = row['path']
        receipt = {'path': path, 'result': 'skipped', 'objects': []}
        if row['classification'] == 'REVIEW' and path not in approve_reviews:
            receipt['reason'] = row['reason']
            receipts.append(receipt)
            continue
        try:
            note = push.fns.get_note(path)
            if fingerprint(note) != row['source_fingerprint']:
                raise ValueError('source changed after preview')
            decision = classify_note(path, note.frontmatter, archive_prefixes=push.archive_prefixes)
            if decision.kind.value != row['classification']:
                raise ValueError('source classification changed after preview')
            if row['classification'] == 'REVIEW':
                # Explicit human review can exclude this tracked non-task, but it
                # cannot waive malformed fields or conflicting terminal evidence.
                if decision.reason != 'no explicit task identity':
                    raise ValueError('this review cannot be overridden by retirement approval')
            verified = []
            for obj in row['objects']:
                remote = push.caldav.get_object(obj['href'])
                if obj['etag'] is None and remote is None:
                    continue
                if remote is None and obj['action'] == 'delete':
                    continue
                if remote is None or remote.etag != obj['etag']:
                    raise ValueError('remote changed or disappeared after preview: ' + obj['href'])
                owned_component(remote, path=path, uid=obj['uid'], kind=obj['kind'], known_uids=push.state.data['uid_paths'])
                verified.append((obj, remote))
            # Verify all versions first, then back up and apply only this source.
            for obj, remote in verified:
                (backup / (obj['uid'] + '.ics')).write_text(remote.ics_text)
                push.state.remember_object(obj['uid'], path, obj['collection'], obj['href'], obj['kind'])
            if row['classification'] == 'REVIEW':
                push.state.data.setdefault('lifecycle', {}).setdefault('approved_exclusions', {})[path] = evidence_signature(note.frontmatter)
            stats = PushStats()
            push.state.add_fns_pending_note_change(path)
            push.state.save()
            push.approved_versions = {obj['href']: obj['etag'] for obj in row['objects']}
            push.approved_source = (path, row['source_fingerprint'])
            try:
                complete = push._process(PushCandidate(path), stats)
            finally:
                push.approved_versions = None
                push.approved_source = None
            if complete:
                push.state.remove_fns_pending_note_change(path)
            push.state.save()
            receipt['result'] = 'applied' if complete else 'pending'
            receipt['stats'] = vars(stats)
            for obj, before in verified:
                after = push.caldav.get_object(obj['href'])
                receipt['objects'].append({'href': obj['href'], 'uid': obj['uid'], 'kind': obj['kind'],
                                          'before_etag': before.etag, 'after_etag': after.etag if after else None,
                                          'exists': after is not None})
            latest = push.fns.get_note(path)
            if fingerprint(latest) != row['source_fingerprint']:
                raise ValueError('source changed during migration; inspect before continuing')
            receipt['source_unchanged'] = True
        except Exception as exc:
            receipt['result'] = 'pending'
            receipt['reason'] = str(exc)
            push.state.review(path, str(exc))
            push.state.add_fns_pending_note_change(path)
            push.state.save()
        receipts.append(receipt)
        (backup / 'receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2, default=str) + '\n')
    return {'backup_dir': str(backup), 'summary': dict(Counter(r['result'] for r in receipts)), 'receipts': receipts}
