"""会话历史与压缩的可见状态；只读本机记录，不额外调用模型。"""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from zhimeng_runtime.agent_models import SessionCheckpointRecord
from zhimeng_runtime.agent_store import AgentStoreNotFound
from zhimeng_runtime.errors import ApiError


class RunContextCompaction(BaseModel):
    run_id: str
    state: Literal['running', 'succeeded', 'failed', 'cancelled', 'interrupted']
    folded_results: int = 0
    tokens_before: int | None = None
    tokens_after: int | None = None


class SessionContextState(BaseModel):
    schema_id: Literal['zhimeng.session-context-state.v1'] = Field(default='zhimeng.session-context-state.v1', alias='schema')
    session_id: str
    message_count: int
    recent_message_count: int
    compacted_message_count: int
    compacting: bool
    background_pending: bool
    latest_checkpoint: SessionCheckpointRecord | None
    run_compaction: RunContextCompaction | None = None


def create_context_state_router(service) -> APIRouter:
    router = APIRouter(tags=['agent'])

    def read_history(session_id: str):
        service.store.get_session(session_id)
        checkpoint = service.store.latest_session_checkpoint(session_id)
        through = checkpoint.through_ordinal if checkpoint else 0
        with service.store.database.connection() as connection:
            counts = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN ordinal <= ? THEN 1 ELSE 0 END), 0) "
                "FROM session_message WHERE session_id = ? AND role IN ('user','assistant')", (through, session_id),
            ).fetchone()
            run_ids = [row[0] for row in connection.execute('SELECT id FROM run WHERE session_id = ?', (session_id,))]
            event = connection.execute(
                "SELECT e.run_id, e.payload_json, r.status FROM run_event e JOIN run r ON r.id=e.run_id "
                "WHERE r.id=(SELECT id FROM run WHERE session_id=? ORDER BY created_at DESC, id DESC LIMIT 1) "
                "AND e.event_type='run_context_compaction' ORDER BY e.sequence DESC LIMIT 1", (session_id,),
            ).fetchone()
            compaction = None
            if event:
                payload = json.loads(event['payload_json'])
                phase = payload['state']
                if phase == 'running' and event['status'] not in ('queued', 'starting', 'running', 'waiting_approval'):
                    phase = 'interrupted'
                compaction = RunContextCompaction(run_id=event['run_id'], state=phase,
                    **{key: payload[key] for key in ('folded_results', 'tokens_before', 'tokens_after') if key in payload})
        return checkpoint, int(counts[0]), int(counts[1]), run_ids, compaction

    @router.get('/api/v1/agent/sessions/{session_id}/context-state', response_model=SessionContextState)
    async def context_state(session_id: str) -> SessionContextState:
        # 开始读取前就已在压缩：即使此间结束，也继续轮询一次再取最新摘要。
        compacting_before = session_id in service._compacting_sessions
        pending_before = {run_id for run_id, task in service._tasks.items() if not task.done()}
        try:
            checkpoint, total, folded, run_ids, compaction = await asyncio.to_thread(read_history, session_id)
        except AgentStoreNotFound as exc:
            raise ApiError('没有找到这场对话。', status_code=404, code='agent_resource_not_found', retryable=False) from exc
        compacting = compacting_before or session_id in service._compacting_sessions
        pending = compacting or any(run_id in pending_before or ((task := service._tasks.get(run_id)) is not None and not task.done()) for run_id in run_ids)
        return SessionContextState(session_id=session_id, message_count=total, recent_message_count=total-folded,
            compacted_message_count=folded, compacting=compacting, background_pending=pending,
            latest_checkpoint=checkpoint, run_compaction=compaction)

    return router
