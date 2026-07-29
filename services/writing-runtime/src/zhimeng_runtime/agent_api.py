from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from zhimeng_runtime.agent_candidate_models import AgentCandidateOutputRecord
from zhimeng_runtime.agent_provider import AgentProviderError
from zhimeng_runtime.agent_models import (
    AgentActivityListResponse,
    AgentMessageListResponse,
    AgentRunCancelResponse,
    AgentRunCreateRequest,
    AgentRunCreateResponse,
    AgentRunEvent,
    AgentRunListResponse,
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentRunResponse,
    AgentSessionCreateRequest,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentStatusResponse,
    AgentWorkflowStateResponse,
)
from zhimeng_runtime.agent_service import AgentService, AgentUnavailableError
from zhimeng_runtime.agent_store import (
    TERMINAL_RUN_STATUSES,
    AgentContextPackConflict,
    AgentContextPackNotFound,
    AgentContextPackSourceUnavailable,
    AgentStoreConflict,
    AgentStoreError,
    AgentStoreInvalidState,
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
    RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES,
)
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.provider_recovery_models import (
    ProviderRecoveryDecisionRequest,
    ProviderRecoveryResponse,
)
from zhimeng_runtime.provider_turn_models import ProviderTurnReceiptListResponse
from zhimeng_runtime.writer_models import WriterCandidateResult, WriterTask
from zhimeng_runtime.writer_trace_models import WriterTraceResponse


def create_agent_router(
    service: AgentService,
    *,
    heartbeat_seconds: float,
) -> APIRouter:
    router = APIRouter(tags=["agent"])

    def raise_agent_error(exc: Exception) -> NoReturn:
        if isinstance(exc, AgentUnavailableError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = "agent_unavailable"
            retryable = True
        elif isinstance(exc, AgentContextPackNotFound):
            status_code = status.HTTP_404_NOT_FOUND
            code = "context_pack_not_found"
            retryable = False
        elif isinstance(exc, AgentContextPackSourceUnavailable):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = exc.code
            retryable = exc.retryable
        elif isinstance(exc, AgentContextPackConflict):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = exc.code in RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES
        elif isinstance(exc, AgentWorkflowCheckpointConflict):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = exc.code in {
                "workflow_resume_stale",
                "workflow_checkpoint_cas_failed",
            }
        elif isinstance(exc, AgentStoreNotFound):
            status_code = status.HTTP_404_NOT_FOUND
            code = "agent_resource_not_found"
            retryable = False
        elif isinstance(exc, AgentStoreConflict):
            status_code = status.HTTP_409_CONFLICT
            code = getattr(exc, "code", "agent_conflict")
            retryable = getattr(exc, "retryable", True)
        elif isinstance(exc, AgentStoreInvalidState):
            status_code = status.HTTP_409_CONFLICT
            code = "agent_state_invalid"
            retryable = False
        elif isinstance(exc, AgentProviderError):
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.retryable
                else status.HTTP_409_CONFLICT
            )
            code = exc.code
            retryable = exc.retryable
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "agent_store_failed"
            retryable = True
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    async def call_store(callback, /, *args, **kwargs):
        try:
            return await asyncio.to_thread(callback, *args, **kwargs)
        except AgentStoreError as exc:
            raise_agent_error(exc)

    @router.get("/api/v1/agent/status", response_model=AgentStatusResponse)
    async def agent_status() -> AgentStatusResponse:
        return service.status()

    @router.post(
        "/api/v1/projects/{project_id}/agent/sessions",
        response_model=AgentSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        project_id: str,
        request: AgentSessionCreateRequest,
    ) -> AgentSessionResponse:
        try:
            return await service.create_session(project_id=project_id, title=request.title)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/projects/{project_id}/agent/sessions",
        response_model=AgentSessionListResponse,
    )
    async def list_sessions(project_id: str) -> AgentSessionListResponse:
        sessions = await call_store(service.store.list_sessions, project_id)
        return AgentSessionListResponse(sessions=sessions)

    @router.get(
        "/api/v1/agent/sessions/{session_id}",
        response_model=AgentSessionResponse,
    )
    async def get_session(session_id: str) -> AgentSessionResponse:
        return await call_store(service.store.get_session, session_id)

    @router.get(
        "/api/v1/agent/sessions/{session_id}/messages",
        response_model=AgentMessageListResponse,
    )
    async def list_messages(
        session_id: str,
        after_ordinal: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> AgentMessageListResponse:
        session = await call_store(service.store.get_session, session_id)
        messages = await call_store(
            service.store.list_messages,
            session_id,
            after_ordinal=after_ordinal,
            limit=limit,
        )
        return AgentMessageListResponse(
            session_id=session_id,
            messages=messages,
            last_ordinal=session.last_message_ordinal,
        )

    @router.get(
        "/api/v1/agent/sessions/{session_id}/runs",
        response_model=AgentRunListResponse,
    )
    async def list_runs(
        session_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> AgentRunListResponse:
        runs = await call_store(service.store.list_runs, session_id, limit=limit)
        return AgentRunListResponse(session_id=session_id, runs=runs)

    @router.post(
        "/api/v1/agent/sessions/{session_id}/runs",
        response_model=AgentRunCreateResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run(
        session_id: str,
        request: AgentRunCreateRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
            ),
        ],
    ) -> AgentRunCreateResponse:
        try:
            result = await service.start_run(
                session_id=session_id,
                message=request.message,
                expected_project_revision=request.expected_project_revision,
                expected_project_fingerprint=request.expected_project_fingerprint,
                active_chapter_id=request.active_chapter_id,
                writer_mode=request.writer_mode,
                idempotency_key=idempotency_key,
                context_pack_id=request.context_pack_id,
                expected_context_pack_hash=request.expected_context_pack_hash,
            )
        except (AgentUnavailableError, AgentProviderError, AgentStoreError) as exc:
            raise_agent_error(exc)
        return AgentRunCreateResponse(
            run=result.run,
            user_message=result.user_message,
            replayed=result.replayed,
        )

    @router.get(
        "/api/v1/agent/runs/{run_id}",
        response_model=AgentRunResponse,
    )
    async def get_run(run_id: str) -> AgentRunResponse:
        run = await call_store(service.store.get_run, run_id)
        return AgentRunResponse(run=run)

    @router.get(
        "/api/v1/agent/runs/{run_id}/workflow",
        response_model=AgentWorkflowStateResponse,
    )
    async def get_workflow_state(run_id: str) -> AgentWorkflowStateResponse:
        try:
            return await service.get_workflow_state(run_id)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/runs/{run_id}/provider-recovery",
        response_model=ProviderRecoveryResponse,
    )
    async def get_provider_recovery(run_id: str) -> ProviderRecoveryResponse:
        return await call_store(service.store.get_provider_recovery, run_id)

    @router.post(
        "/api/v1/agent/runs/{run_id}/provider-recovery/decision",
        response_model=ProviderRecoveryResponse,
        status_code=status.HTTP_200_OK,
    )
    async def decide_provider_recovery(
        run_id: str,
        decision: ProviderRecoveryDecisionRequest,
    ) -> ProviderRecoveryResponse:
        try:
            return await service.decide_provider_recovery(run_id, decision)
        except (AgentUnavailableError, AgentProviderError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/runs/{run_id}/provider-turn-receipts",
        response_model=ProviderTurnReceiptListResponse,
    )
    async def get_provider_turn_receipts(
        run_id: str,
    ) -> ProviderTurnReceiptListResponse:
        return await call_store(
            service.store.get_provider_turn_receipt_list,
            run_id,
        )

    @router.get(
        "/api/v1/agent/runs/{run_id}/candidate-output",
        response_model=AgentCandidateOutputRecord,
    )
    async def get_candidate_output(run_id: str) -> AgentCandidateOutputRecord:
        return await call_store(service.store.get_agent_candidate_output, run_id)

    @router.get(
        "/api/v1/agent/runs/{run_id}/writer-task",
        response_model=WriterTask,
    )
    async def get_writer_task(run_id: str) -> WriterTask:
        return await call_store(service.store.get_writer_task, run_id)

    @router.get(
        "/api/v1/agent/runs/{run_id}/writer-candidate-result",
        response_model=WriterCandidateResult,
    )
    async def get_writer_candidate_result(run_id: str) -> WriterCandidateResult:
        return await call_store(
            service.store.get_writer_candidate_result,
            run_id,
        )

    @router.get(
        "/api/v1/agent/runs/{run_id}/writer-trace",
        response_model=WriterTraceResponse,
    )
    async def get_writer_trace(run_id: str) -> WriterTraceResponse:
        try:
            return await service.get_writer_trace(run_id)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.post(
        "/api/v1/agent/runs/{run_id}/resume",
        response_model=AgentRunResumeResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def resume_run(
        run_id: str,
        resume_request: AgentRunResumeRequest,
    ) -> AgentRunResumeResponse:
        try:
            return await service.resume_run(run_id, resume_request)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/runs/{run_id}/activities",
        response_model=AgentActivityListResponse,
    )
    async def list_activities(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> AgentActivityListResponse:
        run = await call_store(service.store.get_run, run_id)
        activities = await call_store(
            service.store.list_activities,
            run_id,
            after_sequence=after_sequence,
        )
        return AgentActivityListResponse(
            run_id=run_id,
            activities=activities,
            last_sequence=run.last_activity_sequence,
        )

    @router.post(
        "/api/v1/agent/runs/{run_id}/cancel",
        response_model=AgentRunCancelResponse,
    )
    async def cancel_run(run_id: str) -> AgentRunCancelResponse:
        try:
            run, accepted = await service.cancel_run(run_id)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)
        return AgentRunCancelResponse(run=run, accepted=accepted)

    @router.get("/api/v1/agent/runs/{run_id}/events")
    async def stream_run_events(
        run_id: str,
        request: Request,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        await call_store(service.store.get_run, run_id)
        cursor = after_sequence
        if last_event_id:
            cursor = _parse_event_cursor(run_id, last_event_id)
        return StreamingResponse(
            _iter_run_events(
                request,
                service=service,
                run_id=run_id,
                after_sequence=cursor,
                heartbeat_seconds=heartbeat_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _parse_event_cursor(run_id: str, event_id: str) -> int:
    event_run_id, separator, raw_sequence = event_id.rpartition(":")
    if separator != ":" or event_run_id != run_id:
        raise ApiError(
            "Last-Event-ID does not belong to this Run",
            status_code=status.HTTP_409_CONFLICT,
            code="agent_event_cursor_invalid",
            retryable=False,
        )
    try:
        sequence = int(raw_sequence)
    except ValueError as exc:
        raise ApiError(
            "Last-Event-ID has an invalid sequence",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="agent_event_cursor_invalid",
            retryable=False,
        ) from exc
    if sequence < 0:
        raise ApiError(
            "Last-Event-ID sequence must not be negative",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="agent_event_cursor_invalid",
            retryable=False,
        )
    return sequence


async def _iter_run_events(
    request: Request,
    *,
    service: AgentService,
    run_id: str,
    after_sequence: int,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    cursor = after_sequence
    while not await request.is_disconnected():
        generation = service.broker.snapshot(run_id)
        events = await asyncio.to_thread(
            service.store.list_run_events,
            run_id,
            after_sequence=cursor,
        )
        for stored in events:
            cursor = stored.sequence
            event = AgentRunEvent(
                event_id=f"{run_id}:{cursor}",
                event_type="activity",
                emitted_at=stored.activity.root.created_at,
                data={
                    "activity": stored.activity.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                },
            )
            yield _encode_sse(event)
        run = await asyncio.to_thread(service.store.get_run, run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            done = AgentRunEvent(
                event_id=f"{run_id}:{cursor}",
                event_type="done",
                emitted_at=datetime.now(UTC),
                data={"run": run.model_dump(mode="json", by_alias=True)},
            )
            yield _encode_sse(done)
            return
        changed = await service.broker.wait_for_change(
            run_id,
            generation,
            timeout=heartbeat_seconds,
        )
        if not changed:
            yield ": heartbeat\n\n"


def _encode_sse(event: AgentRunEvent) -> str:
    payload = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type}\n"
        f"data: {payload}\n\n"
    )
