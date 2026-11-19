"""Read current files named by durable receipts; never infer work from prose.

Shared by requested reviews and the on-demand file-results view. Automatic
review excerpts are bounded; the results view streams file hashes without
loading text. Neither is a verdict that the user's requirements were met.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

from zhimeng_runtime.secret_mask import mask_secrets
from zhimeng_runtime.task_review_commands import command_evidence
from zhimeng_runtime.text_encoding import decode_project_text


SNAPSHOT_BYTES = 512_000


def _signature(value: os.stat_result) -> tuple:
    # Windows stat/fstat disagree about ctime (creation versus metadata time).
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def observe_file(path: Path, *, include_text: bool = True, cancelled: threading.Event | None = None) -> dict:
    """One read, with identity checks; absence and I/O failures stay distinct."""
    try:
        if not path.is_absolute():
            return {'state': 'unverified', 'reason': 'path_is_not_absolute'}
        # A historical path may now point elsewhere. Leave link resolution to
        # explicit tools instead of silently attaching a different file.
        for part in reversed((path, *path.parents)):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return {'state': 'unverified', 'reason': 'path_is_link_or_reparse_point'}
        before = path.stat()
        if stat.S_ISDIR(before.st_mode):
            return {'state': 'directory'}
        if not stat.S_ISREG(before.st_mode):
            return {'state': 'unverified', 'reason': 'not_a_regular_file'}
        if include_text and before.st_size > SNAPSHOT_BYTES:
            return {'state': 'file', 'bytes': before.st_size, 'reason': 'automatic_snapshot_size_limit'}
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            data = b''
            count = 0
            while chunk := stream.read(SNAPSHOT_BYTES):
                if cancelled is not None and cancelled.is_set():
                    return {'state': 'unverified', 'reason': 'cancelled'}
                count += len(chunk)
                digest.update(chunk)
                if include_text:
                    if count > SNAPSHOT_BYTES:
                        return {'state': 'unverified', 'reason': 'changed_during_read'}
                    data += chunk
            after_read = os.fstat(stream.fileno())
        if (count != opened.st_size or _signature(before) != _signature(opened)
                or _signature(opened) != _signature(after_read)
                or _signature(after_read) != _signature(path.stat())):
            return {'state': 'unverified', 'reason': 'changed_during_read'}
        result = {'state': 'file', 'bytes': count, 'sha256': digest.hexdigest()}
        if not include_text:
            return result
        try:
            text, encoding, _bom = decode_project_text(data)
            masked, count = mask_secrets(text)
            result.update(encoding=encoding, text_excerpt=masked[:2400],
                          text_characters=len(masked), text_omitted_characters=max(0, len(masked) - 2400),
                          masked_secrets=count)
        except ValueError:
            result['reason'] = 'binary_or_unrecognized_text_encoding'
        return result
    except FileNotFoundError:
        # Disappearance during a read is not a stable observation of absence.
        return {'state': 'unverified', 'reason': 'changed_during_read'} if 'before' in locals() else {'state': 'missing'}
    except OSError as exc:
        return {'state': 'unverified', 'reason': type(exc).__name__}


def execution_summary(connection: sqlite3.Connection, source_ids: list[str], *, max_characters: int = 20000) -> dict:
    placeholders = ','.join('?' for _ in source_ids)
    rows = [dict(row) for row in connection.execute(
        f'SELECT tool_name,status,COUNT(*) AS count FROM tool_call WHERE run_id IN ({placeholders}) '
        'GROUP BY tool_name,status ORDER BY tool_name,status', source_ids,
    )]
    return {'tool_call_count': sum(row['count'] for row in rows), 'tools': rows,
            'commands': command_evidence(connection, source_ids, max_characters=max_characters),
            'tool_status_is_not_outcome_verification': True}


def _receipts(connection: sqlite3.Connection, source_ids: list[str]) -> list[dict]:
    placeholders = ','.join('?' for _ in source_ids)
    records = []
    for row in connection.execute(
        f'SELECT * FROM file_change WHERE run_id IN ({placeholders}) ORDER BY created_at,seq', source_ids,
    ):
        records.append({
            'receipt_id': row['change_id'], 'source': 'file_change', 'sequence': row['seq'],
            'record_state': 'undone' if row['undone_at'] else row['apply_phase'],
            'operation': row['operation'], 'path': row['path'], 'target_path': row['target_path'],
            'before_sha256': row['before_sha256'], 'after_sha256': row['after_sha256'],
            'state_at': row['undone_at'] or row['created_at'],
        })
    for row in connection.execute(
        'SELECT c.id,c.status,c.created_at,c.updated_at,c.payload_json,p.root_path FROM change_set c '
        'JOIN project_binding p ON p.id=c.project_binding_id '
        f"WHERE json_extract(c.payload_json,'$.source_run_id') IN ({placeholders}) ORDER BY c.created_at,c.id", source_ids,
    ):
        data = json.loads(row['payload_json'])
        undone = row['status'] == 'rolled_back'
        # Only manifests describe bytes actually committed. Proposed content is
        # not a receipt. A rollback manifest already describes the inverse op.
        manifest = data.get('rollback' if undone else 'writeback') or {}
        root = Path(row['root_path'])
        for item in manifest.get('files', []):
            relative = Path(item['relative_path'])
            target = Path(item['target_relative_path']) if item.get('target_relative_path') else None
            if any(p.is_absolute() or p.drive or '..' in p.parts for p in (relative, target) if p is not None):
                raise ValueError('文件修改记录包含无效路径，无法核对。')
            if undone and item['operation'] == 'move':
                relative, target = target, relative
            records.append({
                'receipt_id': row['id'], 'source': 'change_set', 'operation': item['operation'],
                'record_state': 'undone' if undone else row['status'],
                'path': str(root / relative), 'target_path': str(root / target) if target else None,
                'before_sha256': item.get('before_sha256'), 'after_sha256': item.get('after_sha256'),
                'sequence': len(records), 'state_at': manifest.get('created_at') or row['updated_at'],
            })
    return sorted(records, key=lambda item: (datetime.fromisoformat(item['state_at']), item['receipt_id'], item['sequence']))


def artifact_targets(records: list[dict]) -> list[dict]:
    """Latest durable state for each touched path, including inverse operations.

    Comparing every historical write to today's bytes falsely marks an earlier
    write as broken when the same task successfully edited the file again.
    """
    latest = {}
    for record in records:
        operation = record['operation']
        reversed_journal = record['source'] == 'file_change' and record['record_state'] == 'undone'
        expected = record['before_sha256' if reversed_journal else 'after_sha256']
        if operation == 'move':
            targets = [(record['path'], 'file' if reversed_journal else 'missing', expected),
                       (record['target_path'], 'missing' if reversed_journal else 'file', expected)]
        else:
            absent = operation in ('create', 'mkdir') if reversed_journal else operation == 'delete'
            kind = 'missing' if absent else 'directory' if operation == 'mkdir' else 'file'
            targets = [(record['path'], kind, expected)]
        for raw, kind, digest in targets:
            if not raw:
                continue
            # Pending/failed records name paths, but do not certify a commit.
            if record['record_state'] not in ('applied', 'undone', 'legacy'):
                kind, digest = 'unknown', None
            key = os.path.normcase(os.path.abspath(raw))
            latest[key] = {**{key: record[key] for key in ('receipt_id', 'source', 'record_state', 'operation')},
                           'path': raw, 'expected_state': kind, 'expected_sha256': digest if kind == 'file' else None}
    return list(latest.values())


def observe_artifact(target: dict, *, include_text: bool = True, cancelled: threading.Event | None = None) -> dict:
    current = observe_file(Path(target['path']), include_text=include_text, cancelled=cancelled)
    return {**target, 'current': current,
            'matches_recorded_state': _matches(current, target['expected_state'], target['expected_sha256'])}


def _matches(current: dict, kind: str, digest: str | None) -> bool | None:
    if current['state'] == 'unverified' or kind == 'unknown':
        return None
    if current['state'] != kind:
        return False
    if kind != 'file':
        return True
    if not digest or not current.get('sha256'):
        return None
    return current['sha256'] == digest


def current_artifacts(connection: sqlite3.Connection, source_ids: list[str], *, max_characters: int) -> dict:
    records = _receipts(connection, source_ids)
    result = {'observed_at': datetime.now(UTC).isoformat(), 'record_count': len(records),
              'scope': 'receipt_paths_only', 'observations': [], 'omitted_count': 0}
    used = 0
    for target in artifact_targets(records):
        # Bound automatic evidence, not the task. Every omitted receipt remains
        # accessible via the source run and existing file/change tools.
        if len(result['observations']) >= 32 or max_characters - used < 900:
            result['omitted_count'] += 1
            continue
        item = observe_artifact(target)
        current = item['current']
        # Retain metadata first, trim text explicitly to the remaining budget.
        text = current.pop('text_excerpt', None)
        size = len(json.dumps(item, ensure_ascii=False))
        room = max(0, max_characters - used - size - 100)
        if text is not None:
            current['text_excerpt'] = text[:room]
            current['text_omitted_characters'] += max(0, len(text) - room)
        size = len(json.dumps(item, ensure_ascii=False))
        # JSON escapes newlines/quotes, so a character allowance alone can
        # still overflow. Shrink the excerpt, retaining the observation.
        while text is not None and used + size > max_characters and current['text_excerpt']:
            excess = used + size - max_characters
            current['text_excerpt'] = current['text_excerpt'][:-excess]
            current['text_omitted_characters'] = current['text_characters'] - len(current['text_excerpt'])
            size = len(json.dumps(item, ensure_ascii=False))
        if used + size > max_characters:
            result['omitted_count'] += 1
            continue
        used += size
        result['observations'].append(item)
    return result
