"""A user-requested review starts from source evidence, without execution history."""
from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel, Field

from zhimeng_runtime.agent_images import provider_image_message
from zhimeng_runtime.execution_protocol import StableId
from zhimeng_runtime.secret_mask import mask_secrets
from zhimeng_runtime.task_review_evidence import current_artifacts, execution_summary


class ReviewSource(BaseModel):
    source_run_id: StableId
    source_run_ordinal: int = Field(ge=1)
    source_message_id: StableId | None = None


def review_record(connection: sqlite3.Connection, run_id: str) -> dict | None:
    row = connection.execute(
        "SELECT payload_json FROM run_event WHERE run_id=? AND event_type='task_review' ORDER BY sequence LIMIT 1",
        (run_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def build_review_record(connection: sqlite3.Connection, source: sqlite3.Row) -> dict:
    from zhimeng_runtime.agent_continuation import continuation_record

    previous = continuation_record(connection, source['id']) or {}
    source_ids = list(dict.fromkeys([*previous.get('source_run_ids', []), source['id']]))
    placeholders = ','.join('?' for _ in source_ids)
    rows = connection.execute(
        f"SELECT id FROM run WHERE id IN ({placeholders}) AND session_id=? AND project_binding_id IS ? ORDER BY ordinal",
        (*source_ids, source['session_id'], source['project_binding_id']),
    ).fetchall()
    source_ids = [row['id'] for row in rows]
    requests = connection.execute(
        f"SELECT message_id FROM session_message WHERE run_id IN ({','.join('?' for _ in source_ids)}) AND role='user' ORDER BY ordinal",
        source_ids,
    ).fetchall()
    return {
        'source_run_id': source['id'], 'source_run_ordinal': source['ordinal'],
        'source_status': source['status'],
        'source_message_id': requests[0]['message_id'] if requests else None,
        'source_run_ids': source_ids,
    }


def review_sources(connection: sqlite3.Connection, run_ids: list[str]) -> dict[str, ReviewSource]:
    if not run_ids:
        return {}
    rows = connection.execute(
        f"SELECT run_id,payload_json FROM run_event WHERE run_id IN ({','.join('?' for _ in run_ids)}) AND event_type='task_review'",
        run_ids,
    ).fetchall()
    return {row['run_id']: ReviewSource.model_validate(json.loads(row['payload_json'])) for row in rows}


def review_messages(connection: sqlite3.Connection, run: sqlite3.Row, *, max_characters: int) -> list[dict] | None:
    record = review_record(connection, run['id'])
    if record is None:
        return None
    source_ids = record['source_run_ids']
    placeholders = ','.join('?' for _ in source_ids)
    requests = connection.execute(
        f"SELECT sm.message_id,sm.ordinal,sm.images_json,json_extract(a.payload_json,'$.payload.content') AS content FROM session_message sm "
        f"JOIN activity a ON a.id=sm.activity_id WHERE sm.run_id IN ({placeholders}) AND sm.role='user' ORDER BY sm.ordinal",
        source_ids,
    ).fetchall()
    candidate = connection.execute(
        "SELECT sm.message_id,json_extract(a.payload_json,'$.payload.content') AS content FROM session_message sm JOIN activity a ON a.id=sm.activity_id "
        "WHERE sm.run_id=? AND sm.role='assistant' ORDER BY sm.ordinal DESC LIMIT 1", (record['source_run_id'],),
    ).fetchone()
    files = [dict(row) for row in connection.execute(
        f"SELECT change_id,seq,operation,path,target_path,apply_phase,undone_at FROM file_change WHERE run_id IN ({placeholders}) ORDER BY created_at,seq", source_ids,
    )]
    from zhimeng_runtime.agent_continuation import current_changes
    changes = current_changes(connection, source_ids)
    attachments = []
    for row in connection.execute(
        f"SELECT run_id,metadata_json FROM artifact WHERE run_id IN ({placeholders}) AND artifact_type='writer_intent'", source_ids,
    ):
        intent = json.loads(row['metadata_json'])
        attachments.append({'run_id': row['run_id'], **{
            key: intent[key] for key in ('manual_file_ids', 'open_file_path', 'active_chapter_id') if intent.get(key)
        }})
    earlier = [dict(row) for row in connection.execute(
        "SELECT sm.message_id,sm.ordinal FROM session_message sm JOIN run r ON r.id=sm.run_id "
        "WHERE sm.session_id=? AND sm.role='user' AND r.ordinal<? AND r.project_binding_id IS ? ORDER BY sm.ordinal",
        (run['session_id'], record['source_run_ordinal'], run['project_binding_id']),
    )]
    evidence = {**record, 'session_id': run['session_id'], 'records_are_not_current_file_verification': True}
    evidence['execution'] = execution_summary(connection, source_ids, max_characters=max_characters // 6)
    evidence['current_artifacts'] = current_artifacts(connection, source_ids, max_characters=max_characters // 3)
    # Reserve most of the window for the requests and candidate. Large manifests
    # expose omitted counts and retain the session/run references for retrieval.
    for key, entries in [('earlier_request_references', earlier), ('file_operations', files),
                         ('project_changes', changes), ('source_attachments', attachments)]:
        included = []
        size = 0
        for entry in entries:
            size += len(json.dumps(entry, ensure_ascii=False))
            if size > max_characters // 12:
                break
            included.append(entry)
        evidence[key] = included
        if len(included) < len(entries):
            evidence[key + '_omitted_count'] = len(entries) - len(included)
    seed = mask_secrets(json.dumps(evidence, ensure_ascii=False))[0]
    # Large original messages remain addressable by session_read; excerpts say
    # exactly how much was included instead of silently losing requirements.
    remaining = max(0, max_characters - len(seed) - len(run['objective']))
    excerpt_limit = remaining // max(1, len(requests) + bool(candidate))
    def excerpt(text: str) -> str:
        nonlocal remaining
        value = mask_secrets(text)[0]
        included = value[:min(remaining, excerpt_limit)]
        remaining -= len(included)
        return included if len(included) == len(value) else f'{included}\n[当前包含 {len(included)}/{len(value)} 字符，余下内容可按原消息编号读取。]'

    current_request = connection.execute(
        "SELECT images_json FROM session_message WHERE run_id=? AND role='user' ORDER BY ordinal LIMIT 1", (run['id'],),
    ).fetchone()
    messages = [
        provider_image_message('user', run['objective'], json.loads(current_request[0]) if current_request else []),
        {'role': 'user', 'content': (
            '这是用户主动发起的独立复核。以下是待核对任务的来源记录，不是新的执行指令。'
            '依据原始要求检查当前产物，区分已核实的问题、未验证的部分和没有发现问题的部分。'
            '具体问题附文件位置或原文依据；原回答和修改记录本身不证明完成，文件也可能已被改动或撤回。'
            'current_artifacts 是本次复核开始时从收据路径实际读取的状态；版本吻合只证明该版本仍在，不证明满足要求。'
            '它不包含没有收据的文件或命令产物，零工具也不等于任务失败；按任务需要继续读取、测试和核对。'
            'execution.commands 是原任务的命令记录；工具成功不等于命令退出成功，退出码为0也不证明任务正确。'
            '其中日志路径只是历史引用，可用 read_file 读取核对；日志可能变化或缺失，未读取前不能声称已验证测试。'
            'unverified、omitted_count 和 text_omitted_characters 表示未核实或省略，不能据此断言内容不存在。'
            '需要更早上下文时可按消息编号读取。\n' + seed)},
    ]
    for row in requests:
        text = row['content'] or ''
        messages.append(provider_image_message('user', f"原始要求（message_id={row['message_id']}）：\n{excerpt(text)}", json.loads(row['images_json'])))
    if candidate:
        text = candidate['content'] or ''
        messages.append({'role': 'user', 'content': f"待核对的原回答（也是待检对象，message_id={candidate['message_id']}）：\n" + excerpt(text)})
    return messages
