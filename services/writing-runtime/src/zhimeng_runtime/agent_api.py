from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from zhimeng_runtime.agent_candidate_models import AgentCandidateOutputRecord
from zhimeng_runtime.agent_provider import AgentProviderError
from zhimeng_runtime.agent_images import create_agent_image_router
from zhimeng_runtime.command_api import create_command_router
from zhimeng_runtime.command_background_api import create_command_background_router
from zhimeng_runtime.memory_changes_api import create_memory_changes_router
from zhimeng_runtime.session_source_api import create_session_source_router
from zhimeng_runtime.session_plan_api import create_session_plan_router
from zhimeng_runtime.session_goal import GoalStore, SessionGoalResponse, GoalEndRequest
from zhimeng_runtime.run_artifacts_api import create_run_artifacts_router
from zhimeng_runtime.run_usage import read_run_usage
from zhimeng_runtime.agent_queue import AgentQueueService, QueueInput, QueueUpdate, QueueState, QueueDispatchResponse, QueueSteerRequest, QueueSteerResponse, QueueInputError
from zhimeng_runtime.agent_models import (
    AgentActivityListResponse,
    AgentMessageListResponse,
    AgentRunCancelResponse,
    AgentRunContinuationResponse,
    AgentRunCreateRequest,
    AgentRunCreateResponse,
    AgentRunAdmissionWithdrawResponse,
    AgentRunEvent,
    AgentRunListResponse,
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentRunResponse,
    AgentSessionCreateRequest,
    AgentSessionForkRequest,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentSessionRebindRequest,
    AgentSessionUpdateRequest,
    AgentStatusResponse,
    AgentWorkflowStateResponse,
    SessionCheckpointRecord,
)
from zhimeng_runtime.agent_service import AgentService, AgentUnavailableError
from zhimeng_runtime.agent_store import (
    TERMINAL_RUN_STATUSES,
    AgentContextPackConflict,
    AgentContextPackNotFound,
    AgentContextPackSourceUnavailable,
    AgentStoreConflict,
    AgentAdmissionWithdrawn,
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
    queue_service: AgentQueueService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["agent"])
    router.include_router(create_agent_image_router(lambda: service.store.database))
    router.include_router(create_command_router(service))
    router.include_router(create_command_background_router(service))
    router.include_router(create_memory_changes_router(service))
    # The production AgentService always owns a store, but small API fixtures
    # may implement only workflow operations. Session source recall needs the
    # database at router construction time, so mount it only when that
    # dependency is present instead of breaking unrelated endpoints.
    if getattr(service, "store", None) is not None:
        router.include_router(create_session_source_router(service))
    router.include_router(create_session_plan_router(service))
    router.include_router(create_run_artifacts_router(service))

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
        elif isinstance(exc, QueueInputError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "agent_queue_input_invalid"
            retryable = False
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

    queue_service = queue_service or AgentQueueService(service)

    @router.get("/api/v1/agent/sessions/{session_id}/queue", response_model=QueueState)
    async def read_queue(session_id: str) -> QueueState:
        return await call_store(queue_service.store.read, session_id)

    @router.post("/api/v1/agent/sessions/{session_id}/queue", response_model=QueueState)
    async def enqueue_message(session_id: str, request: QueueInput) -> QueueState:
        async with queue_service.lock_for(session_id):
            return await call_store(queue_service.store.enqueue, session_id, request)

    @router.patch("/api/v1/agent/sessions/{session_id}/queue", response_model=QueueState)
    async def update_queue(session_id: str, request: QueueUpdate) -> QueueState:
        async with queue_service.lock_for(session_id):
            return await call_store(queue_service.store.update, session_id, request)

    @router.post("/api/v1/agent/sessions/{session_id}/queue/{item_id}/dispatch", response_model=QueueDispatchResponse)
    async def dispatch_queue(session_id: str, item_id: str) -> QueueDispatchResponse:
        try:
            return await queue_service.dispatch(session_id, item_id)
        except (AgentUnavailableError, AgentProviderError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.post("/api/v1/agent/sessions/{session_id}/queue/{item_id}/steer", response_model=QueueSteerResponse)
    async def steer_queue(session_id: str, item_id: str, request: QueueSteerRequest) -> QueueSteerResponse:
        async with queue_service.lock_for(session_id):
            if request.input is not None:
                if request.input.item_id != item_id:
                    raise_agent_error(QueueInputError("插入消息编号与请求路径不一致"))
                await call_store(queue_service.store.enqueue, session_id, request.input)
            try:
                queue = await asyncio.to_thread(queue_service.store.steer, session_id, item_id, request.expected_run_id)
            except AgentStoreError as exc:
                await asyncio.to_thread(queue_service.store.steering_failed, session_id, item_id)
                raise_agent_error(exc)
        await service.broker.publish(request.expected_run_id)
        return QueueSteerResponse(queue=queue)

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

    @router.post(
        "/api/v1/agent/sessions",
        response_model=AgentSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_unbound_session(
        request: AgentSessionCreateRequest,
    ) -> AgentSessionResponse:
        """「随便聊」：不绑定小说项目的 Agent 会话，与项目会话同一个核心。"""
        try:
            return await service.create_session(project_id=None, title=request.title)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/sessions",
        response_model=AgentSessionListResponse,
    )
    async def list_unbound_sessions(
        scope: Annotated[Literal["unbound", "all"], Query()] = "unbound",
    ) -> AgentSessionListResponse:
        """默认只列不绑书的纯对话；scope=all 连各部小说下的对话一起列，供界面左栏分组渲染。"""
        if scope == "all":
            sessions = await call_store(service.store.list_all_sessions)
        else:
            sessions = await call_store(service.store.list_unbound_sessions)
        return AgentSessionListResponse(sessions=sessions)

    @router.put(
        "/api/v1/agent/sessions/{session_id}/book",
        response_model=AgentSessionResponse,
    )
    async def rebind_session_book(
        session_id: str,
        request: AgentSessionRebindRequest,
    ) -> AgentSessionResponse:
        """把这条对话改挂到另一部小说下；project_id 传 null 就解绑成纯对话。"""
        return await call_store(
            service.store.rebind_session,
            session_id,
            project_id=request.project_id,
        )

    @router.get(
        "/api/v1/agent/sessions/{session_id}",
        response_model=AgentSessionResponse,
    )
    async def get_session(session_id: str) -> AgentSessionResponse:
        return await call_store(service.store.get_session, session_id)

    @router.patch(
        "/api/v1/agent/sessions/{session_id}",
        response_model=AgentSessionResponse,
    )
    async def update_session(
        session_id: str,
        request: AgentSessionUpdateRequest,
    ) -> AgentSessionResponse:
        try:
            return await service.update_session(
                session_id,
                title=request.title,
                pinned=request.pinned,
                rebind_provider=request.rebind_provider,
                provider_id=request.provider_id,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
            )
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.delete(
        "/api/v1/agent/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session(session_id: str) -> None:
        await call_store(service.store.close_session, session_id)

    @router.post(
        "/api/v1/agent/sessions/{session_id}/fork",
        response_model=AgentSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def fork_session(
        session_id: str,
        request: AgentSessionForkRequest,
    ) -> AgentSessionResponse:
        try:
            return await service.fork_session(
                source_session_id=session_id,
                up_to_message_id=request.up_to_message_id,
                title=request.title,
            )
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.post(
        "/api/v1/agent/sessions/{session_id}/compact",
        response_model=SessionCheckpointRecord,
        status_code=status.HTTP_200_OK,
    )
    async def compact_session(
        session_id: str,
    ) -> SessionCheckpointRecord:
        try:
            return await service.compact_session(session_id)
        except (AgentUnavailableError, AgentProviderError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/sessions/{session_id}/checkpoint",
        response_model=SessionCheckpointRecord | None,
    )
    async def get_latest_checkpoint(
        session_id: str,
    ) -> SessionCheckpointRecord | None:
        try:
            return await call_store(service.store.latest_session_checkpoint, session_id)
        except (AgentUnavailableError, AgentStoreError) as exc:
            raise_agent_error(exc)

    @router.get(
        "/api/v1/agent/sessions/{session_id}/messages",
        response_model=AgentMessageListResponse,
    )
    async def list_messages(
        session_id: str,
        after_ordinal: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        before_ordinal: Annotated[int | None, Query(ge=1)] = None,
        latest: bool = False,
    ) -> AgentMessageListResponse:
        session = await call_store(service.store.get_session, session_id)
        messages = await call_store(
            service.store.list_messages,
            session_id,
            after_ordinal=after_ordinal,
            limit=limit,
            before_ordinal=before_ordinal,
            latest=latest,
        )
        has_earlier = bool(messages) and await call_store(
            service.store.has_messages_between,
            session_id,
            after_ordinal=after_ordinal,
            before_ordinal=messages[0].ordinal,
        )
        summarize = getattr(service.store, "run_process_summaries", None)
        summaries = await call_store(
            summarize, session_id, [message.run_id for message in messages],
        ) if callable(summarize) else {}
        usage = await call_store(
            read_run_usage, service.store.database, session_id, [message.run_id for message in messages],
        ) if getattr(service.store, "database", None) is not None else {}
        messages = [message.model_copy(update={"run_usage": usage.get(message.run_id)}) for message in messages]
        return AgentMessageListResponse(
            session_id=session_id,
            messages=messages,
            last_ordinal=session.last_message_ordinal,
            has_earlier=has_earlier,
            run_process_summaries=summaries,
            run_origins=await call_store(service.store.message_run_origins, [message.run_id for message in messages]),
            review_sources=await call_store(service.store.message_review_sources, [message.run_id for message in messages]),
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

    @router.get(
        "/api/v1/agent/sessions/{session_id}/runs/admission",
        response_model=AgentRunCreateResponse,
    )
    async def get_run_admission(
        session_id: str,
        response: Response,
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
        # A missing receipt describes this read, not a promise that an in-flight
        # POST cannot commit later. Retries keep the original key and payload.
        await call_store(service.store.get_session, session_id)
        try:
            admitted = await asyncio.to_thread(
                service.store.find_run_by_idempotency_key, session_id, idempotency_key,
            )
        except AgentAdmissionWithdrawn as exc:
            raise ApiError(
                str(exc), status_code=status.HTTP_410_GONE, code=exc.code,
                headers={"Cache-Control": "no-store", "Vary": "Idempotency-Key"},
                retryable=False,
            ) from exc
        except AgentStoreError as exc:
            raise_agent_error(exc)
        if admitted is None:
            raise ApiError(
                "这条消息暂未查到受理记录",
                status_code=status.HTTP_404_NOT_FOUND,
                code="agent_admission_not_found",
                headers={"Cache-Control": "no-store", "Vary": "Idempotency-Key"},
                retryable=False,
            )
        run, user_message = admitted
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Idempotency-Key"
        return AgentRunCreateResponse(
            run=run, user_message=user_message, replayed=True,
        )

    @router.post(
        "/api/v1/agent/sessions/{session_id}/runs/admission/withdraw",
        response_model=AgentRunAdmissionWithdrawResponse,
    )
    async def withdraw_run_admission(
        session_id: str,
        response: Response,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=8, max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
            ),
        ],
    ) -> AgentRunAdmissionWithdrawResponse:
        admitted = await call_store(service.store.withdraw_run_admission, session_id, idempotency_key)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Idempotency-Key"
        if admitted is None:
            return AgentRunAdmissionWithdrawResponse(state="withdrawn")
        run, user_message = admitted
        return AgentRunAdmissionWithdrawResponse(
            state="accepted",
            accepted=AgentRunCreateResponse(run=run, user_message=user_message, replayed=True),
        )

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
                open_file_path=request.open_file_path,
                writer_mode=request.writer_mode,
                idempotency_key=idempotency_key,
                context_pack_id=request.context_pack_id,
                expected_context_pack_hash=request.expected_context_pack_hash,
                continuation_source_run_id=request.continuation_source_run_id,
                review_source_run_id=request.review_source_run_id,
                manual_file_ids=request.manual_file_ids,
                manual_skill_names=request.manual_skill_names,
                image_ids=request.image_ids,
                **({"goal": request.goal} if request.goal else {}),
            )
        except (AgentUnavailableError, AgentProviderError, AgentStoreError) as exc:
            raise_agent_error(exc)
        return AgentRunCreateResponse(
            run=result.run,
            user_message=result.user_message,
            replayed=result.replayed,
        )

    @router.get("/api/v1/agent/sessions/{session_id}/goal", response_model=SessionGoalResponse)
    async def read_goal(session_id: str):
        return await call_store(GoalStore(service.store.database).read, session_id)

    @router.post("/api/v1/agent/sessions/{session_id}/goal/end", response_model=SessionGoalResponse)
    async def end_goal(session_id: str, request: GoalEndRequest):
        return await call_store(GoalStore(service.store.database).end, session_id, request)

    @router.get(
        "/api/v1/agent/runs/{run_id}",
        response_model=AgentRunResponse,
    )
    async def get_run(run_id: str) -> AgentRunResponse:
        run = await call_store(service.store.get_run, run_id)
        return AgentRunResponse(run=run)

    @router.get(
        "/api/v1/agent/runs/{run_id}/continuation",
        response_model=AgentRunContinuationResponse,
    )
    async def get_run_continuation(run_id: str) -> AgentRunContinuationResponse:
        return await call_store(service.store.get_run_continuation, run_id)

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
        limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
        through_sequence: Annotated[int | None, Query(ge=0)] = None,
    ) -> AgentActivityListResponse:
        return await call_store(
            service.store.read_activity_page,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            through_sequence=through_sequence,
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
    page_size = 250
    while not await request.is_disconnected():
        generation = service.broker.snapshot(run_id)
        # 先读 Run，再查事件：看到终态后，后一次查询就能读到同事务提交的尾部活动。
        # 两套序号不同（run_event 还含内部事件），不能拿 SSE 游标和活动序号比较。
        run = await asyncio.to_thread(service.store.get_run, run_id)
        events = await asyncio.to_thread(
            service.store.list_run_events,
            run_id,
            after_sequence=cursor,
            limit=page_size,
        )
        if await request.is_disconnected():
            return
        frames: list[str] = []
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
            frames.append(_encode_sse(event))
        if frames:
            # 每帧仍有独立事件编号；一页共用一次网络写入，避免大量小包回放。
            yield "".join(frames)
        if run.status in TERMINAL_RUN_STATUSES and not events:
            done = AgentRunEvent(
                event_id=f"{run_id}:{cursor}",
                event_type="done",
                emitted_at=datetime.now(UTC),
                data={"run": run.model_dump(mode="json", by_alias=True)},
            )
            yield _encode_sse(done)
            return
        if len(events) == page_size or run.status in TERMINAL_RUN_STATUSES:
            # 先排空已保存的分页，再等新事件；运行中也不能每页等一次心跳。
            continue
        changed = await service.broker.wait_for_change(
            run_id,
            generation,
            timeout=heartbeat_seconds,
        )
        if not changed and not events:
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
