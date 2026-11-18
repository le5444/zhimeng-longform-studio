"""Command provenance for independent reviews, without replaying old output."""
from __future__ import annotations

import json
import sqlite3

from zhimeng_runtime.secret_mask import mask_secrets


def command_evidence(connection: sqlite3.Connection, source_ids: list[str], *, max_characters: int) -> dict:
    placeholders = ','.join('?' for _ in source_ids)
    records = []
    completed = set()
    # A tool may have returned while a command was still running. Its durable
    # completion, including failures, is the authoritative later observation.
    for row in connection.execute(
        f'SELECT * FROM command_completion WHERE owner_run_id IN ({placeholders}) ORDER BY sequence', source_ids,
    ):
        payload = json.loads(row['payload_json'])
        completed.add(row['command_session_id'])
        records.append({
            **_metadata(payload), 'source': 'command_completion', 'receipt_id': row['event_id'],
            'source_run_id': row['owner_run_id'], 'command_session_id': row['command_session_id'],
            'status': row['status'], 'exit_code': row['exit_code'], 'ended_at': row['ended_at'],
        })
    # Synchronous commands have no completion row. Keep their recorded exit
    # facts as well as unfinished/unverified calls; tool success is not exit 0.
    for row in connection.execute(
        'SELECT id,run_id,status,input_json,output_json FROM tool_call '
        f"WHERE run_id IN ({placeholders}) AND tool_name='computer_run_command' ORDER BY started_at,id", source_ids,
    ):
        payload = json.loads(row['output_json']) if row['output_json'] else {}
        arguments = json.loads(row['input_json'])
        command_id = payload.get('session_id')
        if command_id in completed:
            continue
        status = 'unverified'
        if payload.get('cancelled'):
            status = 'cancelled'
        elif payload.get('timed_out'):
            status = 'timed_out'
        elif command_id and payload.get('status'):
            status = payload['status']
        elif type(payload.get('exit_code')) is int:
            status = 'exited'
        records.append({
            **_metadata({**arguments, **payload}), 'source': 'tool_call', 'tool_call_id': row['id'],
            'source_run_id': row['run_id'], 'tool_status': row['status'],
            'command_session_id': command_id, 'status': status, 'exit_code': payload.get('exit_code'),
        })
    result = {'record_count': len(records), 'records': [], 'omitted_count': 0,
              'log_paths_are_not_current_file_verification': True,
              'exit_code_is_not_task_verification': True}
    used = 0
    for item in records:
        if isinstance(item.get('command'), str):
            command = mask_secrets(item['command'])[0]
            limit = min(1200, max(0, max_characters // 4))
            item['command'] = command[:limit]
            if len(command) > limit:
                item['command_omitted_characters'] = len(command) - limit
        encoded = mask_secrets(json.dumps(item, ensure_ascii=False))[0]
        if used + len(encoded) > max_characters:
            result['omitted_count'] += 1
            continue
        result['records'].append(json.loads(encoded))
        used += len(encoded)
    return result


def _metadata(payload: dict) -> dict:
    # No stdout/stderr text, plan claims or previous reasoning is replayed.
    # Log references can be read with the existing read_file tool, including
    # when the command_session tool is unavailable in a read-only review.
    result = {key: payload[key] for key in (
        'command', 'stdout_file', 'stderr_file', 'timed_out', 'cancelled', 'duration_ms', 'output_error',
    ) if key in payload}
    result['workdir'] = payload.get('cwd') or payload.get('workdir') or None
    return result
