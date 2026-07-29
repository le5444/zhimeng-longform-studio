from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Iterator, Protocol, TypeVar
from uuid import uuid4

from zhimeng_runtime.agent_broker import AgentEventBroker
from zhimeng_runtime.agent_candidate_models import AgentCandidateSubmission
from zhimeng_runtime.agent_models import (
    AgentMessageResponse,
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentStatusResponse,
    AgentWorkflowStateResponse,
)
from zhimeng_runtime.agent_provider import (
    AgentProvider,
    AgentProviderCancelled,
    AgentProviderError,
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_store import (
    TERMINAL_RUN_STATUSES,
    AgentContextPackConflict,
    AgentContextPackNotFound,
    AgentContextPackSourceUnavailable,
    AgentStore,
    AgentStoreConflict,
    AgentStoreError,
    AgentStoreInvalidState,
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
    ContextPackKnowledgeTarget,
    RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES,
    StoredToolCall,
)
from zhimeng_runtime.agent_tools import (
    CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
    CREATION_HANDOFF_SKILL_ID,
    AgentToolError,
    AgentToolExecutor,
)
from zhimeng_runtime.change_set_models import ChangeSetResponse
from zhimeng_runtime.context_models import (
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreError,
    ContextStoreIntegrity,
    ContextStoreNotFound,
)
from zhimeng_runtime.errors import ErrorDetail, ErrorEnvelope
from zhimeng_runtime.execution_protocol import (
    ArtifactPayload,
    CapabilityName,
    CapabilityProfile,
    ChangeProposalPayload,
    FileChange,
    MessageUsage,
    Run,
    RunStatus,
    SkillCallPayload,
)
from zhimeng_runtime.project_models import ProjectInspectionResponse
from zhimeng_runtime.project_kb_service import ProjectKbService, ProjectKbServiceError
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.provider_recovery_models import (
    ProviderRecoveryDecisionRequest,
    ProviderRecoveryResponse,
)
from zhimeng_runtime.provider_turn_models import build_provider_turn_receipt
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
)
from zhimeng_runtime.workflow_runner import (
    WorkflowExecutionResult,
    WorkflowRunner,
    workflow_runtime_tool_call_id,
)
from zhimeng_runtime.writer_models import WriterTargetFile
from zhimeng_runtime.writer_trace_models import WriterTraceResponse


MAX_PROVIDER_TURNS = 8
MAX_TOOL_CALLS_PER_RUN = 16
MAX_TOOL_FAILURES_PER_RUN = 3
T = TypeVar("T")


class AgentUnavailableError(RuntimeError):
    pass


class KnowledgeFingerprintProbe(Protocol):
    def __call__(self, project_id: str | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class RunStartResult:
    run: Run
    user_message: AgentMessageResponse
    replayed: bool


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    tool_call: StoredToolCall
    replayed: bool
    candidate_submission: AgentCandidateSubmission | None = None
    candidate_tool_output: object | None = None
    candidate_tool_duration_ms: int | None = None


class AgentService:
    def __init__(
        self,
        store: AgentStore,
        provider: AgentProvider | None,
        broker: AgentEventBroker | None = None,
        *,
        knowledge_fingerprint_probe: KnowledgeFingerprintProbe | None = None,
        tool_executor: AgentToolExecutor | None = None,
        context_store: ContextStore | None = None,
        project_kb_service: ProjectKbService | None = None,
        project_locks: ProjectLockRegistry | None = None,
        provider_resolver: Callable[[], AgentProvider | None] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self._provider_resolver = provider_resolver
        self.broker = broker or AgentEventBroker()
        self.knowledge_fingerprint_probe = knowledge_fingerprint_probe
        self.tool_executor = tool_executor
        self.context_store = context_store
        self.project_kb_service = project_kb_service
        self.project_locks = project_locks
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._launch_lock = asyncio.Lock()
        self._start_operations = 0
        self._start_operations_drained = asyncio.Event()
        self._start_operations_drained.set()
        self._closing = False

    def status(self) -> AgentStatusResponse:
        provider = self._resolve_provider()
        if provider is None:
            return AgentStatusResponse(
                available=False,
                executor=None,
                capabilities=None,
                detail="Writing Agent provider is not configured",
            )
        return AgentStatusResponse(
            available=True,
            executor=provider.identity,
            capabilities=self._capabilities(provider),
            detail=(
                "Writing Agent provider and Runtime project tools are configured"
                if self.tool_executor is not None
                else "Writing Agent provider is configured"
            ),
        )

    async def create_session(self, *, project_id: str, title: str):
        provider = self._require_provider()
        return await asyncio.to_thread(
            self.store.create_session,
            project_id=project_id,
            title=title,
            executor=provider.identity,
            capabilities=self._capabilities(provider),
        )

    async def start_run(
        self,
        *,
        session_id: str,
        message: str,
        expected_project_revision: int,
        expected_project_fingerprint: str,
        active_chapter_id: str | None,
        writer_mode: str = "repair_only",
        idempotency_key: str,
        context_pack_id: str | None = None,
        expected_context_pack_hash: str | None = None,
    ) -> RunStartResult:
        provider = self._require_provider()
        self._dispatch_config_fingerprint(provider)
        current_capabilities = self._capabilities(provider)
        knowledge_observation = None
        workflow_plan_builder = None
        if context_pack_id is not None and expected_context_pack_hash is not None:
            await self._require_current_project_kb_for_context(
                context_pack_id,
                expected_context_pack_hash,
            )
            knowledge_observation = await self._observe_context_pack_knowledge(
                context_pack_id,
                expected_context_pack_hash,
            )
            if (
                knowledge_observation.state is KnowledgeObservationState.MATCHED
                and await self._has_managed_workflow(context_pack_id)
            ):
                workflow_runner = self._workflow_runner()
                if workflow_runner is None:
                    raise AgentUnavailableError(
                        "Reviewed Context execution requires the managed workflow executor"
                    )
                workflow_plan_builder = lambda run: workflow_runner.build_plan(
                    run,
                    context_pack_id=context_pack_id,
                )
        async with self._launch_lock:
            if self._closing:
                raise AgentUnavailableError("Writing Agent Runtime is shutting down")
            self._start_operations += 1
            self._start_operations_drained.clear()
        try:
            create_operation = asyncio.create_task(
                asyncio.to_thread(
                    self.store.create_run,
                    session_id=session_id,
                    message=message,
                    expected_project_revision=expected_project_revision,
                    expected_project_fingerprint=expected_project_fingerprint,
                    active_chapter_id=active_chapter_id,
                    writer_mode=writer_mode,
                    idempotency_key=idempotency_key,
                    context_pack_id=context_pack_id,
                    expected_context_pack_hash=expected_context_pack_hash,
                    knowledge_observation=knowledge_observation,
                    current_executor=provider.identity,
                    current_capabilities=current_capabilities,
                    workflow_plan_builder=workflow_plan_builder,
                ),
                name=f"writing-agent-create:{session_id}",
            )
            request_cancelled = False
            while True:
                try:
                    run, user_message, replayed = await asyncio.shield(create_operation)
                    break
                except asyncio.CancelledError:
                    request_cancelled = True
            finalize_operation = asyncio.create_task(
                self._finalize_run_start(run.run_id),
                name=f"writing-agent-finalize:{run.run_id}",
            )
            while True:
                try:
                    run = await asyncio.shield(finalize_operation)
                    break
                except asyncio.CancelledError:
                    request_cancelled = True
            if request_cancelled:
                raise asyncio.CancelledError
            return RunStartResult(
                run=run,
                user_message=user_message,
                replayed=replayed,
            )
        finally:
            release_operation = asyncio.create_task(
                self._release_start_operation(),
                name=f"writing-agent-start-release:{session_id}",
            )
            while True:
                try:
                    await asyncio.shield(release_operation)
                    break
                except asyncio.CancelledError:
                    continue

    async def cancel_run(self, run_id: str) -> tuple[Run, bool]:
        request_cancelled = False
        async with self._launch_lock:
            before = await asyncio.to_thread(self.store.get_run, run_id)
            if before.status in TERMINAL_RUN_STATUSES:
                return before, False
            if self._closing:
                raise AgentUnavailableError("Writing Agent Runtime is shutting down")
            cancel_operation = asyncio.create_task(
                asyncio.to_thread(self.store.cancel_run, run_id),
                name=f"writing-agent-cancel-claim:{run_id}",
            )
            while True:
                try:
                    run = await asyncio.shield(cancel_operation)
                    break
                except asyncio.CancelledError:
                    request_cancelled = True
            cancel_event = self._cancel_events.get(run_id)
            if cancel_event is not None:
                cancel_event.set()
            task = self._tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            else:
                terminal_operation = asyncio.create_task(
                    asyncio.to_thread(self.store.mark_cancelled, run_id),
                    name=f"writing-agent-cancel-terminal:{run_id}",
                )
                while True:
                    try:
                        run = await asyncio.shield(terminal_operation)
                        break
                    except asyncio.CancelledError:
                        request_cancelled = True
        publish_operation = asyncio.create_task(
            self.broker.publish(run_id),
            name=f"writing-agent-cancel-publish:{run_id}",
        )
        while True:
            try:
                await asyncio.shield(publish_operation)
                break
            except asyncio.CancelledError:
                request_cancelled = True
        if request_cancelled:
            raise asyncio.CancelledError
        return run, True

    async def get_workflow_state(self, run_id: str) -> AgentWorkflowStateResponse:
        run, plan, chain = await asyncio.to_thread(
            self._load_workflow_snapshot,
            run_id,
        )
        checkpoints = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )
        boundaries = tuple(
            item for item in chain if isinstance(item, ProviderIoBoundary)
        )
        boundary = boundaries[0] if boundaries else None
        last = checkpoints[-1] if checkpoints else None

        if run.status is RunStatus.SUCCEEDED:
            resume_state = "terminal"
            reason_code = "workflow_run_terminal"
        elif boundary is not None:
            resume_state = "provider_dispatch_uncertain"
            reason_code = "provider_dispatch_already_started"
        elif run.status is RunStatus.PAUSED:
            resume_blocker = await asyncio.to_thread(
                self.store.workflow_resume_blocker,
                run_id,
            )
            resumable = resume_blocker is None and (
                last is None
                or (
                    last.execution_status == "succeeded"
                    and last.domain_status
                    in plan.steps[last.step_ordinal - 1].continue_on_domain_status
                )
            )
            resume_state = "resumable" if resumable else "blocked"
            reason_code = (
                "workflow_resume_available"
                if resumable
                else resume_blocker or "workflow_step_not_resumable"
            )
        elif run.status in TERMINAL_RUN_STATUSES:
            resume_state = "terminal"
            reason_code = "workflow_run_terminal"
        else:
            resume_state = "running"
            reason_code = "workflow_execution_active"

        can_advance = last is None or (
            last.execution_status == "succeeded"
            and last.domain_status
            in plan.steps[last.step_ordinal - 1].continue_on_domain_status
        )
        next_step_ordinal = (
            len(checkpoints) + 1
            if resume_state in {"running", "resumable"}
            and can_advance
            and len(checkpoints) < len(plan.steps)
            else None
        )

        return AgentWorkflowStateResponse(
            run_id=run.run_id,
            run_status=run.status,
            plan=plan,
            checkpoints=checkpoints,
            provider_boundary=boundary,
            last_checkpoint_id=None if last is None else last.checkpoint_id,
            last_checkpoint_hash=None if last is None else last.checkpoint_hash,
            next_step_ordinal=next_step_ordinal,
            resume_state=resume_state,
            resume_allowed=resume_state == "resumable",
            reason_code=reason_code,
            formal_draft_allowed=False,
        )

    async def get_writer_trace(self, run_id: str) -> WriterTraceResponse:
        workflow = await self.get_workflow_state(run_id)

        async def optional(getter):
            try:
                return await asyncio.to_thread(getter, run_id)
            except AgentStoreNotFound:
                return None

        writer_task = await optional(self.store.get_writer_task)
        activation_binding = await optional(
            self.store.get_creation_instruction_activation_binding
        )
        materialization = await optional(
            self.store.get_creation_instruction_materialization
        )
        dispatch_receipt = await optional(
            self.store.get_creation_instruction_dispatch_receipt
        )
        candidate_output = await optional(self.store.get_agent_candidate_output)
        writer_candidate_result = await optional(
            self.store.get_writer_candidate_result
        )
        provider_recovery = await optional(self.store.get_provider_recovery)
        return WriterTraceResponse(
            run_id=run_id,
            workflow=workflow,
            writer_task=writer_task,
            activation_binding=activation_binding,
            materialization=materialization,
            dispatch_receipt=dispatch_receipt,
            candidate_output=candidate_output,
            writer_candidate_result=writer_candidate_result,
            provider_recovery=provider_recovery,
        )

    async def resume_run(
        self,
        run_id: str,
        request: AgentRunResumeRequest,
    ) -> AgentRunResumeResponse:
        provider = self._require_provider()
        async with self._launch_lock:
            if self._closing:
                raise AgentUnavailableError("Writing Agent Runtime is shutting down")
            active_task = self._tasks.get(run_id)
            if active_task is not None and not active_task.done():
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Run is already executing",
                    code="agent_run_already_running",
                )

            state = await self.get_workflow_state(run_id)
            if not state.resume_allowed:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Run is not resumable",
                    code=state.reason_code,
                )
            if (
                request.expected_workflow_plan_id != state.plan.workflow_plan_id
                or request.expected_plan_hash != state.plan.plan_hash
                or request.expected_checkpoint_id != state.last_checkpoint_id
                or request.expected_checkpoint_hash != state.last_checkpoint_hash
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow state changed before Resume",
                    code="workflow_resume_stale",
                )

            workflow_runner = self._workflow_runner()
            tool_executor = self.tool_executor
            if workflow_runner is None or tool_executor is None:
                raise AgentUnavailableError(
                    "Durable workflow execution is not configured"
                )
            try:
                await asyncio.to_thread(
                    workflow_runner.validate_plan_dependencies,
                    state.plan,
                )
            except AgentProviderError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            run = await asyncio.to_thread(self.store.get_run, run_id)
            try:
                self._require_bound_provider(run, provider)
            except AgentProviderError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            try:
                await tool_executor.validate_run_project(run)
            except AgentToolError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            knowledge_observation = (
                await self._observe_run_context_pack_knowledge(run_id)
            )
            self._require_resumable_knowledge(knowledge_observation)
            await self._require_current_project_kb_for_run(run_id)

            claim_operation = asyncio.create_task(
                asyncio.to_thread(
                    self.store.claim_workflow_resume,
                    run_id,
                    expected_workflow_plan_id=request.expected_workflow_plan_id,
                    expected_plan_hash=request.expected_plan_hash,
                    expected_checkpoint_id=request.expected_checkpoint_id,
                    expected_checkpoint_hash=request.expected_checkpoint_hash,
                ),
                name=f"writing-agent-resume-claim:{run_id}",
            )
            request_cancelled = False
            while True:
                try:
                    claim = await asyncio.shield(claim_operation)
                    break
                except asyncio.CancelledError:
                    request_cancelled = True
            self._launch(run_id, resumed=True)
            await self.broker.publish(run_id)

        if request_cancelled:
            raise asyncio.CancelledError
        last = claim.checkpoints[-1] if claim.checkpoints else None
        return AgentRunResumeResponse(
            run=claim.run,
            accepted=True,
            workflow_plan_id=claim.plan.workflow_plan_id,
            resumed_from_checkpoint_id=(
                None if last is None else last.checkpoint_id
            ),
            resumed_from_checkpoint_hash=(
                None if last is None else last.checkpoint_hash
            ),
            next_action=(
                "workflow_step"
                if claim.next_step_ordinal is not None
                else "provider_io"
            ),
        )

    async def decide_provider_recovery(
        self,
        run_id: str,
        request: ProviderRecoveryDecisionRequest,
    ) -> ProviderRecoveryResponse:
        if request.action == "adopt_persisted_candidate":
            lease = await self._acquire_current_project_kb_lease_for_run(run_id)
            try:
                async with self._terminal_project_lock_for_run(run_id):
                    return await self._await_terminal_store_operation(
                        self.store.decide_provider_recovery,
                        run_id,
                        request,
                        current_project_kb_verifier=(
                            self._require_current_project_kb_target_sync
                        ),
                    )
            finally:
                lease.__exit__(None, None, None)
        return await asyncio.to_thread(
            self.store.decide_provider_recovery,
            run_id,
            request,
        )

    async def shutdown(self) -> None:
        async with self._launch_lock:
            self._closing = True
            tasks = list(self._tasks.values())
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            for task in tasks:
                task.cancel()
        await self._start_operations_drained.wait()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        workflow_run_ids = await asyncio.to_thread(
            self.store.list_nonterminal_workflow_run_ids
        )
        for run_id in workflow_run_ids:
            await self._preserve_workflow_on_shutdown(run_id)
        await asyncio.to_thread(self.store.recover_interrupted)

    def _resolve_provider(self) -> AgentProvider | None:
        if self._provider_resolver is not None:
            return self._provider_resolver()
        return self.provider

    def _require_provider(self) -> AgentProvider:
        provider = self._resolve_provider()
        if provider is None:
            raise AgentUnavailableError("Writing Agent provider is not configured")
        return provider

    @staticmethod
    def _dispatch_config_fingerprint(provider: AgentProvider) -> str:
        fingerprint = provider.dispatch_config_fingerprint
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise AgentProviderError(
                "Writing Agent provider has no valid dispatch configuration fingerprint",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        if provider.identity.instance_id != f"provider-config-{fingerprint}":
            raise AgentProviderError(
                "Writing Agent executor identity is not bound to its dispatch configuration",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        return fingerprint

    @classmethod
    def _require_bound_provider(cls, run: Run, provider: AgentProvider) -> str:
        fingerprint = cls._dispatch_config_fingerprint(provider)
        if provider.identity != run.executor:
            raise AgentProviderError(
                "Writing Agent executor configuration changed after this Run was created",
                code="workflow_executor_changed",
                retryable=False,
            )
        return fingerprint

    def _capabilities(
        self,
        provider: AgentProvider | None = None,
    ) -> CapabilityProfile:
        provider = provider or self._require_provider()
        if self.tool_executor is None:
            return provider.capabilities
        names = list(provider.capabilities.names)
        for capability in (
            CapabilityName.TOOL_CALLS,
            CapabilityName.FILE_READ,
            CapabilityName.ARTIFACTS,
        ):
            if capability not in names:
                names.append(capability)
        if (
            any(item.name == "skill_execute" for item in self.tool_executor.definitions)
            and CapabilityName.SKILLS not in names
        ):
            names.append(CapabilityName.SKILLS)
        return provider.capabilities.model_copy(update={"names": names})

    @staticmethod
    def _runtime_tool_call_id(run_id: str, provider_tool_call_id: str) -> str:
        """Keep provider call IDs scoped to one Run before persisting them."""
        return workflow_runtime_tool_call_id(run_id, provider_tool_call_id)

    def _launch(self, run_id: str, *, resumed: bool = False) -> None:
        cancel_event = asyncio.Event()
        self._cancel_events[run_id] = cancel_event
        self._tasks[run_id] = asyncio.create_task(
            self._execute(run_id, cancel_event, resumed=resumed),
            name=f"writing-agent:{run_id}",
        )

    async def _finalize_run_start(self, run_id: str) -> Run:
        """Register or preserve a committed Run despite request cancellation."""

        async with self._launch_lock:
            preserve = self._closing
            run = await asyncio.to_thread(self.store.get_run, run_id)
            active_task = self._tasks.get(run_id)
            if (
                not preserve
                and run.status is RunStatus.QUEUED
                and (active_task is None or active_task.done())
            ):
                self._launch(run_id)
        if preserve and run.status not in TERMINAL_RUN_STATUSES:
            await self._preserve_workflow_on_shutdown(run_id)
            run = await asyncio.to_thread(self.store.get_run, run_id)
        await self.broker.publish(run_id)
        return run

    async def _release_start_operation(self) -> None:
        async with self._launch_lock:
            self._start_operations -= 1
            if self._start_operations == 0:
                self._start_operations_drained.set()

    async def _execute(
        self,
        run_id: str,
        cancel_event: asyncio.Event,
        *,
        resumed: bool = False,
    ) -> None:
        project_kb_lease: AbstractContextManager[object] | None = None
        try:
            if resumed:
                claimed = await asyncio.to_thread(self.store.get_run, run_id)
                if claimed.status is not RunStatus.RUNNING:
                    raise AgentProviderError(
                        "Workflow Resume claim is no longer running",
                        code="workflow_resume_claim_lost",
                        retryable=False,
                    )
                resume_tool_executor = self.tool_executor
                if resume_tool_executor is None:
                    raise AgentProviderError(
                        "Durable workflow execution is not configured",
                        code="workflow_executor_unavailable",
                        retryable=True,
                    )
                try:
                    await resume_tool_executor.validate_run_project(claimed)
                except AgentToolError as exc:
                    raise AgentProviderError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                    ) from exc
            else:
                await self._transition(
                    run_id,
                    RunStatus.STARTING,
                    "Starting provider request",
                )
            knowledge_observation = await self._observe_run_context_pack_knowledge(
                run_id
            )
            await self._require_current_project_kb_for_run(run_id)
            if resumed:
                self._require_resumable_knowledge(knowledge_observation)
            prepared_context = await asyncio.to_thread(
                self.store.prepare_context_injection,
                run_id,
                knowledge_observation=knowledge_observation,
            )
            if prepared_context is None:
                if resumed:
                    raise AgentProviderError(
                        "Durable workflow Resume has no bound Context injection",
                        code="workflow_resume_context_missing",
                        retryable=False,
                    )
                current_run = await asyncio.to_thread(self.store.get_run, run_id)
                provider_messages = await asyncio.to_thread(
                    self.store.provider_history,
                    current_run.session_id,
                )
            else:
                provider_messages = list(prepared_context.messages)
                await self.broker.publish(run_id)
            if not resumed:
                await self._transition(
                    run_id,
                    RunStatus.RUNNING,
                    "Provider stream connected",
                )
            provider = self._require_provider()
            tool_executor = self.tool_executor
            # Candidate submission is a managed-workflow boundary capability;
            # ordinary Runs must never see it in their Provider catalog.
            all_tool_definitions = (
                tool_executor.definitions if tool_executor is not None else ()
            )
            tool_definitions = tuple(
                definition
                for definition in all_tool_definitions
                if definition.name != CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
            )
            current_run = await asyncio.to_thread(self.store.get_run, run_id)
            self._require_bound_provider(current_run, provider)
            workflow_result: WorkflowExecutionResult | None = None
            provider_boundary: ProviderIoBoundary | None = None
            provider_accepted_reported_models: tuple[str, ...] = ()
            candidate_required = False
            total_tool_calls = 0
            if prepared_context is not None and await self._has_managed_workflow(
                prepared_context.context_pack_id
            ):
                workflow_runner = self._workflow_runner()
                if workflow_runner is None:
                    raise AgentProviderError(
                        "Reviewed Context execution requires the managed workflow executor",
                        code="workflow_executor_unavailable",
                        retryable=True,
                    )
                workflow_result = await workflow_runner.execute_pre_provider(
                    current_run,
                    cancel_event=cancel_event,
                    execute_tool_call=self._execute_tool_call,
                )
                candidate_required = bool(
                    workflow_result.plan.steps
                    and workflow_result.plan.steps[-1].skill_id
                    == CREATION_HANDOFF_SKILL_ID
                )
                provider_messages.extend(workflow_result.provider_messages)
                total_tool_calls = workflow_result.tool_call_count
                try:
                    project_snapshot = await tool_executor.validate_run_project(
                        current_run
                    )
                except AgentToolError as exc:
                    raise AgentProviderError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                    ) from exc
                final_knowledge_observation = (
                    await self._observe_run_context_pack_knowledge(run_id)
                )
                self._require_resumable_knowledge(final_knowledge_observation)
                await self._require_current_project_kb_for_run(run_id)
                writer_mode = await asyncio.to_thread(
                    self.store.get_writer_mode,
                    run_id,
                )
                operation_package = await asyncio.to_thread(
                    self.context_store.get_prewrite_operation_package,
                    workflow_result.plan.context_pack_id,
                )
                writer_targets = self._writer_target_files(
                    project_snapshot,
                    mode=writer_mode,
                    active_chapter_id=operation_package.active_chapter_id,
                )
                writer_task = await asyncio.to_thread(
                    self.store.ensure_writer_task,
                    run_id,
                    target_files=writer_targets,
                )
                instruction_materialization = await asyncio.to_thread(
                    self.store.ensure_creation_instruction_materialization,
                    run_id,
                )
                provider_messages.append(
                    self._writer_task_provider_message(writer_task)
                )
                provider_messages.append(
                    self._creation_instruction_provider_message(
                        instruction_materialization
                    )
                )
                blocked_tool_names = {"skill_execute"}
                if writer_task.mode == "repair_only":
                    blocked_tool_names.add("change_set_propose")
                tool_definitions = tuple(
                    definition
                    for definition in all_tool_definitions
                    if definition.name not in blocked_tool_names
                )
                candidate_definition = getattr(
                    tool_executor,
                    "candidate_output_definition",
                    None,
                )
                if candidate_required and candidate_definition is None:
                    raise AgentProviderError(
                        "Managed Writer workflow has no candidate submission capability",
                        code="candidate_output_tool_unavailable",
                        retryable=False,
                    )
                if candidate_required:
                    tool_definitions = (*tool_definitions, candidate_definition)
                provider_accepted_reported_models = (
                    self._provider_accepted_reported_models(provider)
                )
                project_kb_lease = (
                    await self._acquire_current_project_kb_lease_for_run(run_id)
                )
                provider_boundary = await self._persist_provider_io_boundary(
                    current_run,
                    workflow_result,
                    provider_messages=provider_messages,
                    tool_definitions=tool_definitions,
                    provider=provider,
                    accepted_reported_models=provider_accepted_reported_models,
                )
            tool_failures = 0
            total_usage: MessageUsage | None = None
            candidate_submission_for_run: AgentCandidateSubmission | None = None

            for provider_turn in range(1, MAX_PROVIDER_TURNS + 1):
                await self._require_current_project_kb_for_run(run_id)
                message_id = f"message_{uuid4().hex}"
                content_parts: list[str] = []
                tool_calls: list[ProviderToolCall] = []
                completed: ProviderCompleted | None = None
                turn_request_hash = (
                    self._provider_dispatch_material_hash(
                        provider_messages=provider_messages,
                        tool_definitions=tool_definitions,
                        provider=provider,
                        dispatch_config_fingerprint=self._require_bound_provider(
                            current_run,
                            provider,
                        ),
                        accepted_reported_models=(
                            provider_accepted_reported_models
                        ),
                    )
                    if provider_boundary is not None
                    else None
                )
                stream = (
                    provider.stream_chat(
                        provider_messages,
                        cancel_event=cancel_event,
                        tools=tool_definitions,
                    )
                    if tool_executor is not None
                    else provider.stream_chat(
                        provider_messages,
                        cancel_event=cancel_event,
                    )
                )
                async for event in stream:
                    if isinstance(event, ProviderTextDelta):
                        content_parts.append(event.text)
                        await asyncio.to_thread(
                            self.store.append_assistant_delta,
                            run_id,
                            message_id=message_id,
                            delta=event.text,
                        )
                        await self.broker.publish(run_id)
                    elif isinstance(event, ProviderToolCall):
                        tool_calls.append(event)
                    elif isinstance(event, ProviderCompleted):
                        completed = event
                if completed is None:
                    raise AgentProviderError(
                        "Provider stream ended without a completion event",
                        code="agent_provider_incomplete_stream",
                        retryable=True,
                    )
                if provider_boundary is not None:
                    if turn_request_hash is None:  # pragma: no cover - guarded above.
                        raise RuntimeError("Provider turn request hash was not prepared")
                    receipt = build_provider_turn_receipt(
                        provider_boundary,
                        turn_ordinal=provider_turn,
                        request_hash=turn_request_hash,
                        reported_model=completed.reported_model,
                        accepted_reported_models=provider_accepted_reported_models,
                        finish_reason=completed.finish_reason,
                        usage=completed.usage,
                        created_at=datetime.now(UTC),
                    )
                    await asyncio.to_thread(
                        self.store.create_provider_turn_receipt,
                        receipt,
                    )
                    if receipt.model_identity_status == "rejected_mismatch":
                        raise AgentProviderError(
                            "Provider reported a model identity that was not explicitly accepted",
                            code="agent_provider_model_mismatch",
                            retryable=False,
                        )
                total_usage = self._merge_usage(total_usage, completed.usage)

                if tool_calls:
                    if completed.finish_reason not in {"tool_calls", "function_call"}:
                        raise AgentProviderError(
                            "Provider emitted tool calls without a tool-call finish reason",
                            code="agent_provider_protocol_error",
                            retryable=False,
                        )
                    if tool_executor is None:
                        raise AgentProviderError(
                            "Provider requested a tool but this Runtime has no project tools",
                            code="agent_provider_tool_call_unsupported",
                            retryable=False,
                        )
                    advertised_tool_names = {
                        definition.name for definition in tool_definitions
                    }
                    if any(
                        call.tool_name not in advertised_tool_names
                        for call in tool_calls
                    ):
                        raise AgentProviderError(
                            "Provider requested a tool that was not advertised for this turn",
                            code="agent_provider_tool_not_advertised",
                            retryable=False,
                        )
                    candidate_calls = [
                        call
                        for call in tool_calls
                        if call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
                    ]
                    if candidate_calls:
                        if not candidate_required:
                            raise AgentProviderError(
                                "Candidate submission is only allowed after the managed Workflow",
                                code="agent_candidate_output_not_allowed",
                                retryable=False,
                            )
                        if len(candidate_calls) != 1 or len(tool_calls) != 1:
                            raise AgentProviderError(
                                "Candidate submission must be the only final ToolCall",
                                code="agent_candidate_output_not_final_tool",
                                retryable=False,
                            )
                        if "".join(content_parts).strip():
                            raise AgentProviderError(
                                "Candidate submission cannot be mixed with Provider text",
                                code="agent_candidate_output_text_mixed",
                                retryable=False,
                            )
                    total_tool_calls += len(tool_calls)
                    if total_tool_calls > MAX_TOOL_CALLS_PER_RUN:
                        raise AgentProviderError(
                            "Provider exceeded the per-Run tool-call limit",
                            code="agent_tool_call_limit",
                            retryable=False,
                        )
                    provider_messages.append(
                        {
                            "role": "assistant",
                            "content": "".join(content_parts) or None,
                            "tool_calls": [
                                {
                                    "id": call.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": call.tool_name,
                                        "arguments": json.dumps(
                                            call.arguments,
                                            ensure_ascii=False,
                                            sort_keys=True,
                                            separators=(",", ":"),
                                        ),
                                    },
                                }
                                for call in tool_calls
                            ],
                        }
                    )
                    current_run = await asyncio.to_thread(self.store.get_run, run_id)
                    for call in tool_calls:
                        if cancel_event.is_set():
                            raise AgentProviderCancelled()
                        if provider_boundary is not None:
                            await self._require_current_project_kb_for_run(run_id)
                        executed = await self._execute_tool_call(
                            current_run,
                            provider_tool_call_id=call.tool_call_id,
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                            cancel_event=cancel_event,
                        )
                        stored_call = executed.tool_call
                        if stored_call.status == "failed":
                            tool_failures += 1
                        if call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME:
                            pending_atomic_completion = (
                                executed.candidate_tool_output is not None
                            )
                            if (
                                pending_atomic_completion
                                and stored_call.status != "running"
                            ) or (
                                not pending_atomic_completion
                                and stored_call.status != "succeeded"
                            ):
                                raise AgentProviderError(
                                    "Candidate submission ToolCall did not succeed",
                                    code="agent_candidate_output_submit_failed",
                                    retryable=False,
                                )
                            if executed.candidate_submission is None:
                                raise AgentProviderError(
                                    "Candidate submission ToolCall has no validated payload",
                                    code="agent_candidate_output_integrity_failed",
                                    retryable=False,
                                )
                            if candidate_submission_for_run is not None:
                                raise AgentProviderError(
                                    "A Run cannot submit more than one candidate",
                                    code="agent_candidate_output_duplicate",
                                    retryable=False,
                                )
                            candidate_submission_for_run = executed.candidate_submission
                        else:
                            provider_messages.append(
                                self._provider_tool_result_message(
                                    call.tool_call_id,
                                    stored_call,
                                    replayed=executed.replayed,
                                )
                            )
                        if stored_call.status == "cancelled":
                            raise AgentProviderCancelled()
                        if (
                            stored_call.error is not None
                            and stored_call.error.error.retryable
                        ):
                            raise AgentProviderError(
                                stored_call.error.error.message,
                                code=stored_call.error.error.code,
                                retryable=True,
                            )
                        if tool_failures >= MAX_TOOL_FAILURES_PER_RUN:
                            raise AgentProviderError(
                                "Runtime stopped after repeated tool failures",
                                code="agent_tool_failure_limit",
                                retryable=False,
                            )
                    if candidate_submission_for_run is not None:
                        async with self._terminal_project_lock_for_run(run_id):
                            await self._require_current_project_kb_for_run(run_id)
                            candidate = await asyncio.to_thread(
                                self.store.build_agent_candidate_output,
                                run_id,
                                candidate_submission_for_run,
                            )
                            await self._await_terminal_store_operation(
                                self.store.complete_run_with_candidate_output,
                                candidate,
                                candidate_tool_call_id=stored_call.tool_call_id,
                                candidate_tool_output=(
                                    executed.candidate_tool_output
                                    if executed.candidate_tool_output is not None
                                    else stored_call.output
                                ),
                                candidate_tool_duration_ms=(
                                    executed.candidate_tool_duration_ms
                                    if executed.candidate_tool_duration_ms is not None
                                    else stored_call.duration_ms
                                ),
                                message_id=message_id,
                                usage=total_usage,
                                current_project_kb_verifier=(
                                    self._require_current_project_kb_target_sync
                                ),
                            )
                        await self.broker.publish(run_id)
                        break
                    continue

                if completed.finish_reason in {"tool_calls", "function_call"}:
                    raise AgentProviderError(
                        "Provider ended for tool calls without emitting a complete call",
                        code="agent_provider_protocol_error",
                        retryable=False,
                    )
                content = "".join(content_parts)
                if not content.strip():
                    raise AgentProviderError(
                        "Provider completed without a final assistant message",
                        code="agent_provider_empty_response",
                        retryable=True,
                    )
                if candidate_required:
                    raise AgentProviderError(
                        "Managed Workflow Run must finish with exactly one candidate envelope",
                        code="agent_candidate_output_missing",
                        retryable=False,
                    )
                if provider_boundary is None:
                    await asyncio.to_thread(
                        self.store.complete_run,
                        run_id,
                        message_id=message_id,
                        content=content,
                        finish_reason=completed.finish_reason,
                        usage=total_usage,
                    )
                else:
                    async with self._terminal_project_lock_for_run(run_id):
                        await self._require_current_project_kb_for_run(run_id)
                        await self._await_terminal_store_operation(
                            self.store.complete_run,
                            run_id,
                            message_id=message_id,
                            content=content,
                            finish_reason=completed.finish_reason,
                            usage=total_usage,
                            current_project_kb_verifier=(
                                self._require_current_project_kb_target_sync
                            ),
                        )
                await self.broker.publish(run_id)
                break
            else:
                raise AgentProviderError(
                    "Provider exceeded the per-Run turn limit",
                    code="agent_provider_turn_limit",
                    retryable=False,
                )
        except AgentProviderCancelled:
            if self._closing:
                await self._preserve_workflow_on_shutdown(run_id)
            else:
                await self._mark_cancelled(run_id)
        except asyncio.CancelledError:
            if self._closing:
                await asyncio.shield(
                    self._preserve_workflow_on_shutdown(run_id)
                )
            else:
                await asyncio.shield(self._mark_cancelled(run_id))
            raise
        except AgentProviderError as exc:
            if self._closing:
                await self._preserve_workflow_on_shutdown(run_id)
            elif cancel_event.is_set():
                await self._mark_cancelled(run_id)
            else:
                await self._fail(run_id, code=exc.code, message=str(exc), retryable=exc.retryable)
        except AgentContextPackNotFound as exc:
            await self._fail(
                run_id,
                code="context_pack_not_found",
                message=str(exc),
                retryable=False,
            )
        except AgentContextPackSourceUnavailable as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        except AgentContextPackConflict as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.code in RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES,
            )
        except AgentWorkflowCheckpointConflict as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=False,
            )
        except AgentStoreConflict as exc:
            await self._fail(
                run_id,
                code=getattr(exc, "code", "agent_store_conflict"),
                message=str(exc),
                retryable=False,
            )
        except AgentStoreInvalidState as exc:
            await self._fail(
                run_id,
                code="agent_state_invalid",
                message=str(exc),
                retryable=False,
            )
        except Exception as exc:
            await self._fail(
                run_id,
                code="agent_execution_failed",
                message=str(exc) or "Writing Agent execution failed",
                retryable=True,
            )
        finally:
            if project_kb_lease is not None:
                project_kb_lease.__exit__(None, None, None)
            current = asyncio.current_task()
            if self._tasks.get(run_id) is current:
                self._tasks.pop(run_id, None)
                self._cancel_events.pop(run_id, None)

    @staticmethod
    def _writer_target_files(
        snapshot: ProjectInspectionResponse,
        *,
        mode: str,
        active_chapter_id: str | None,
    ) -> tuple[WriterTargetFile, ...]:
        if mode == "repair_only":
            return ()
        if mode != "author_review_change_proposal":
            raise AgentProviderError(
                "Writer intent mode is unsupported",
                code="writer_task_mode_invalid",
                retryable=False,
            )
        if active_chapter_id is None:
            raise AgentProviderError(
                "Author-review Writer mode requires an active chapter",
                code="writer_task_target_missing",
                retryable=False,
            )
        chapters = [
            chapter
            for chapter in snapshot.chapters
            if chapter.chapter_id == active_chapter_id
        ]
        if len(chapters) != 1 or not chapters[0].prose_file_ids:
            raise AgentProviderError(
                "Active chapter has no unambiguous prose target",
                code="writer_task_target_missing",
                retryable=False,
            )
        files_by_id = {item.file_id: item for item in snapshot.files}
        try:
            targets = tuple(
                sorted(
                    (
                        WriterTargetFile(
                            file_id=file_id,
                            relative_path=files_by_id[file_id].relative_path,
                            content_hash=files_by_id[file_id].sha256,
                        )
                        for file_id in chapters[0].prose_file_ids
                    ),
                    key=lambda item: item.relative_path.casefold(),
                )
            )
        except (KeyError, ValueError) as exc:
            raise AgentProviderError(
                "Active chapter prose target failed Runtime validation",
                code="writer_task_target_invalid",
                retryable=False,
            ) from exc
        if len(targets) > 32:
            raise AgentProviderError(
                "Active chapter exceeds the Writer target-file limit",
                code="writer_task_target_limit",
                retryable=False,
            )
        return targets

    @staticmethod
    def _writer_task_provider_message(writer_task) -> dict[str, object]:
        task_payload = writer_task.model_dump(mode="json", by_alias=True)
        dispatch = {
            "schema": "zhimeng.writer-task-dispatch.v1",
            "writer_task": task_payload,
            "runtime_adds_writer_task_reference": True,
            "required_tool_sequence": (
                ["change_set_propose", "candidate_output_submit"]
                if writer_task.mode == "author_review_change_proposal"
                else ["candidate_output_submit"]
            ),
            "required_candidate_kind": (
                "change_proposal"
                if writer_task.mode == "author_review_change_proposal"
                else "repair_plan"
            ),
            "project_truth_write_allowed": False,
        }
        return {
            "role": "system",
            "content": json.dumps(
                dispatch,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _creation_instruction_provider_message(
        materialization,
    ) -> dict[str, object]:
        dispatch = {
            "schema": "zhimeng.creation-instruction-dispatch.v1",
            "materialization": materialization.model_dump(
                mode="json",
                by_alias=True,
            ),
            "runtime_adds_materialization_reference": True,
            "runtime_owns_workflow_and_project_writes": True,
            "formal_draft_allowed": False,
            "requires_author_review": True,
        }
        return {
            "role": "system",
            "content": json.dumps(
                dispatch,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    async def _has_managed_workflow(self, context_pack_id: str) -> bool:
        context_store = self.context_store
        if context_store is None:
            return False
        try:
            await asyncio.to_thread(context_store.get_nlcl_route, context_pack_id)
        except ContextStoreNotFound:
            return False
        except ContextStoreIntegrity as exc:
            raise AgentProviderError(
                str(exc),
                code="nlcl_route_integrity_failed",
                retryable=False,
            ) from exc
        except ContextStoreError as exc:
            raise AgentProviderError(
                str(exc),
                code="nlcl_route_unavailable",
                retryable=True,
            ) from exc
        return True

    def _workflow_runner(self) -> WorkflowRunner | None:
        context_store = self.context_store
        tool_executor = self.tool_executor
        skill_executor = (
            tool_executor.skill_executor if tool_executor is not None else None
        )
        if context_store is None or skill_executor is None:
            return None
        return WorkflowRunner(self.store, context_store, skill_executor)

    def _load_workflow_snapshot(
        self,
        run_id: str,
    ) -> tuple[
        Run,
        WorkflowPlan,
        tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
    ]:
        run = self.store.get_run(run_id)
        plan = self.store.get_workflow_plan(run_id)
        chain = tuple(self.store.list_workflow_checkpoints(run_id))
        return run, plan, chain

    async def _preserve_workflow_on_shutdown(self, run_id: str) -> None:
        """Pause pre-Provider evidence owned by this task during graceful shutdown."""

        try:
            run = await asyncio.to_thread(self.store.get_run, run_id)
            await asyncio.to_thread(self.store.get_workflow_plan, run_id)
        except AgentStoreNotFound:
            await self._mark_interrupted(run_id)
            return
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        if run.status is RunStatus.PAUSED:
            return

        try:
            await asyncio.to_thread(self.store.get_provider_io_boundary, run_id)
        except AgentStoreNotFound:
            pass
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        else:
            await self._mark_interrupted(run_id)
            return

        runner = self._workflow_runner()
        if runner is not None:
            try:
                await asyncio.to_thread(runner.reconcile_startup, run_id)
            except (AgentStoreError, ContextStoreError, AgentProviderError):
                await self._mark_interrupted(run_id)
                return
        try:
            await asyncio.to_thread(self.store.pause_workflow_for_restart, run_id)
        except AgentWorkflowCheckpointConflict:
            # A boundary may win the race while its database write finishes.
            await self._mark_interrupted(run_id)
            return
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        await self.broker.publish(run_id)

    @staticmethod
    def _require_resumable_knowledge(
        observation: KnowledgeFingerprintObservation | None,
    ) -> None:
        if observation is None or (
            observation.state is KnowledgeObservationState.UNAVAILABLE
        ):
            raise AgentContextPackSourceUnavailable(
                "Context Pack knowledge source could not be verified before dispatch"
            )
        if observation.state is KnowledgeObservationState.CHANGED:
            raise AgentContextPackConflict(
                "Context Pack knowledge source changed before dispatch",
                code="context_pack_source_changed",
            )

    async def _persist_provider_io_boundary(
        self,
        run: Run,
        workflow: WorkflowExecutionResult,
        *,
        provider_messages: list[dict[str, object]],
        tool_definitions: tuple[ProviderToolDefinition, ...],
        provider: AgentProvider,
        accepted_reported_models: tuple[str, ...],
    ) -> ProviderIoBoundary:
        dispatch_config_fingerprint = self._require_bound_provider(run, provider)
        provider_model = self._provider_requested_model(provider)
        request_hash = self._provider_dispatch_material_hash(
            provider_messages=provider_messages,
            tool_definitions=tool_definitions,
            provider=provider,
            dispatch_config_fingerprint=dispatch_config_fingerprint,
            accepted_reported_models=accepted_reported_models,
        )
        boundary = build_provider_io_boundary(
            workflow.plan,
            workflow.checkpoints,
            request_hash=request_hash,
            provider_model=provider_model,
            provider_dispatch_config_fingerprint=dispatch_config_fingerprint,
            executor=provider.identity,
            created_at=datetime.now(UTC),
        )
        await asyncio.to_thread(
            self.store.create_provider_io_boundary,
            boundary,
            expected_previous_checkpoint_hash=workflow.checkpoints[-1].checkpoint_hash,
            provider_messages_hash=canonical_json_hash(provider_messages),
            tool_definitions_hash=canonical_json_hash(
                [
                    item.model_dump(mode="json", by_alias=True)
                    for item in tool_definitions
                ]
            ),
        )
        await self.broker.publish(run.run_id)
        return boundary

    @staticmethod
    def _provider_requested_model(provider: AgentProvider) -> str | None:
        value = getattr(provider, "model", None)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
            raise AgentProviderError(
                "Writing Agent provider has no valid requested model identity",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        return value.strip()

    @staticmethod
    def _provider_accepted_reported_models(
        provider: AgentProvider,
    ) -> tuple[str, ...]:
        raw = getattr(provider, "accepted_reported_models", ())
        if not isinstance(raw, (tuple, list)):
            raise AgentProviderError(
                "Writing Agent provider has an invalid reported-model allowlist",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        normalized: list[str] = []
        for value in raw:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
                raise AgentProviderError(
                    "Writing Agent provider has an invalid reported-model allowlist",
                    code="provider_dispatch_config_invalid",
                    retryable=False,
                )
            candidate = value.strip()
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(sorted(normalized))

    @classmethod
    def _provider_dispatch_material_hash(
        cls,
        *,
        provider_messages: list[dict[str, object]],
        tool_definitions: tuple[ProviderToolDefinition, ...],
        provider: AgentProvider,
        dispatch_config_fingerprint: str,
        accepted_reported_models: tuple[str, ...],
    ) -> str:
        return canonical_json_hash(
            {
                "schema": "zhimeng.provider-dispatch-material.v1",
                "messages": provider_messages,
                "tools": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in tool_definitions
                ],
                "provider_model": cls._provider_requested_model(provider),
                "accepted_reported_models": list(accepted_reported_models),
                "provider_dispatch_config_fingerprint": dispatch_config_fingerprint,
                "executor": provider.identity.model_dump(mode="json"),
            }
        )

    async def _execute_tool_call(
        self,
        run: Run,
        *,
        provider_tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        cancel_event: asyncio.Event,
    ) -> ToolExecutionResult:
        tool_executor = self.tool_executor
        if tool_executor is None:
            raise AgentProviderError(
                "Runtime project tools are not configured",
                code="agent_provider_tool_call_unsupported",
                retryable=False,
            )
        tool_definitions = list(tool_executor.definitions)
        candidate_definition = getattr(
            tool_executor,
            "candidate_output_definition",
            None,
        )
        if candidate_definition is not None:
            tool_definitions.append(candidate_definition)
        tool_versions = {item.name: item.version for item in tool_definitions}
        runtime_tool_call_id = self._runtime_tool_call_id(
            run.run_id,
            provider_tool_call_id,
        )
        tool_version = tool_versions.get(tool_name, "1")
        request_hash = tool_executor.request_hash(
            tool_name,
            tool_version,
            arguments,
        )
        stored_call, replayed = await asyncio.to_thread(
            self.store.begin_tool_call,
            run.run_id,
            tool_call_id=runtime_tool_call_id,
            tool_name=tool_name,
            tool_version=tool_version,
            arguments=arguments,
            request_hash=request_hash,
            idempotency_key=runtime_tool_call_id,
        )
        await self.broker.publish(run.run_id)
        if replayed:
            if stored_call.status not in {"succeeded", "failed", "cancelled"}:
                raise AgentProviderError(
                    "Provider repeated an unfinished ToolCall identity",
                    code="agent_tool_call_in_progress",
                    retryable=False,
                )
            candidate_submission = None
            if stored_call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME:
                try:
                    candidate_submission = AgentCandidateSubmission.model_validate_json(
                        json.dumps(
                            stored_call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        strict=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise AgentProviderError(
                        "Persisted candidate submission arguments are invalid",
                        code="agent_candidate_output_integrity_failed",
                        retryable=False,
                    ) from exc
            return ToolExecutionResult(
                tool_call=stored_call,
                replayed=True,
                candidate_submission=candidate_submission,
            )

        started = monotonic()
        output: object = {}
        error: ErrorEnvelope | None = None
        status: str = "succeeded"
        change_proposal: ChangeProposalPayload | None = None
        skill_call: SkillCallPayload | None = None
        artifacts: tuple[ArtifactPayload, ...] = ()
        candidate_submission: AgentCandidateSubmission | None = None
        try:
            result = await tool_executor.execute(
                run,
                tool_call_id=runtime_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                request_hash=request_hash,
                cancel_event=cancel_event,
            )
            output = result.output
            skill_call = result.skill_call
            artifacts = result.artifacts
            candidate_submission = result.candidate_submission
            if result.change_set is not None:
                change_proposal = self._change_proposal(result.change_set)
        except asyncio.CancelledError:
            status = "cancelled"
            output = {"cancelled": True}
            try:
                stored_after_cancel = await asyncio.shield(
                    asyncio.to_thread(
                        self.store.get_tool_call,
                        runtime_tool_call_id,
                    )
                )
                if stored_after_cancel.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    await asyncio.shield(
                        asyncio.to_thread(
                            self.store.finish_tool_call,
                            run.run_id,
                            tool_call_id=runtime_tool_call_id,
                            status="cancelled",
                            output=output,
                            error=None,
                            duration_ms=max(
                                0,
                                int((monotonic() - started) * 1000),
                            ),
                            change_proposal=None,
                        )
                    )
            except AgentStoreConflict:
                stored_after_cancel = await asyncio.shield(
                    asyncio.to_thread(
                        self.store.get_tool_call,
                        runtime_tool_call_id,
                    )
                )
                if stored_after_cancel.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    raise
            await asyncio.shield(self.broker.publish(run.run_id))
            raise
        except AgentToolError as exc:
            status = exc.status
            skill_call = exc.skill_call
            artifacts = exc.artifacts
            if status == "failed":
                error = self._tool_error(
                    run.run_id,
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                )
                output = {}
            else:
                error = None
                output = {"cancelled": True}
        except Exception as exc:
            status = "failed"
            error = self._tool_error(
                run.run_id,
                code="agent_tool_execution_failed",
                message=str(exc) or "Runtime tool execution failed",
                retryable=True,
            )
            output = {}

        duration_ms = max(0, int((monotonic() - started) * 1000))
        if candidate_submission is not None and status == "succeeded":
            return ToolExecutionResult(
                tool_call=stored_call,
                replayed=False,
                candidate_submission=candidate_submission,
                candidate_tool_output=output,
                candidate_tool_duration_ms=duration_ms,
            )

        stored_call, _ = await asyncio.to_thread(
            self.store.finish_tool_call,
            run.run_id,
            tool_call_id=runtime_tool_call_id,
            status=status,
            output=output,
            error=error,
            duration_ms=duration_ms,
            change_proposal=change_proposal,
            skill_call=skill_call,
            artifacts=artifacts,
        )
        await self.broker.publish(run.run_id)
        return ToolExecutionResult(
            tool_call=stored_call,
            replayed=False,
            candidate_submission=candidate_submission,
        )

    @staticmethod
    def _provider_tool_result_message(
        provider_tool_call_id: str,
        tool_call: StoredToolCall,
        *,
        replayed: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": tool_call.status,
            "output": tool_call.output or {},
            "error": (
                tool_call.error.model_dump(mode="json", by_alias=True)
                if tool_call.error is not None
                else None
            ),
        }
        if replayed:
            payload["replayed"] = True
        return {
            "role": "tool",
            "tool_call_id": provider_tool_call_id,
            "content": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _merge_usage(
        accumulated: MessageUsage | None,
        current: MessageUsage | None,
    ) -> MessageUsage | None:
        if current is None:
            return accumulated
        if accumulated is None:
            return current
        input_tokens = accumulated.input_tokens + current.input_tokens
        output_tokens = accumulated.output_tokens + current.output_tokens
        return MessageUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=max(
                accumulated.total_tokens + current.total_tokens,
                input_tokens + output_tokens,
            ),
        )

    @staticmethod
    def _tool_error(
        run_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorDetail(
                code=code,
                message=message,
                request_id=run_id,
                retryable=retryable,
            )
        )

    @staticmethod
    def _change_proposal(change_set: ChangeSetResponse) -> ChangeProposalPayload:
        return ChangeProposalPayload(
            proposal_id=change_set.change_set_id,
            title=change_set.title,
            summary=change_set.summary,
            base_revision=change_set.base_revision,
            base_fingerprint=change_set.base_fingerprint,
            files=[
                FileChange(
                    file_id=change.file_id,
                    path=change.relative_path,
                    operation=change.operation.value,
                    content_hash_before=change.before_sha256,
                    content_hash_after=change.proposed_after_sha256,
                )
                for change in change_set.changes
            ],
        )

    async def _observe_context_pack_knowledge(
        self,
        context_pack_id: str,
        expected_context_pack_hash: str,
    ) -> KnowledgeFingerprintObservation:
        try:
            target = await asyncio.to_thread(
                self.store.context_pack_knowledge_target,
                context_pack_id,
                expected_context_pack_hash=expected_context_pack_hash,
            )
        except AgentStoreError:
            return self._unavailable_knowledge_observation(
                context_pack_id=context_pack_id,
                pack_hash=expected_context_pack_hash,
                expected_fingerprint=None,
            )
        return await self._observe_knowledge_target(target)

    async def _observe_run_context_pack_knowledge(
        self,
        run_id: str,
    ) -> KnowledgeFingerprintObservation | None:
        try:
            target = await asyncio.to_thread(
                self.store.run_context_pack_knowledge_target,
                run_id,
            )
        except AgentStoreError:
            return None
        if target is None:
            return None
        return await self._observe_knowledge_target(target)

    async def _require_current_project_kb_for_context(
        self,
        context_pack_id: str,
        expected_context_pack_hash: str,
    ) -> None:
        target = await asyncio.to_thread(
            self.store.context_pack_knowledge_target,
            context_pack_id,
            expected_context_pack_hash=expected_context_pack_hash,
        )
        await self._require_current_project_kb_target(target)

    async def _require_current_project_kb_for_run(self, run_id: str) -> None:
        target = await asyncio.to_thread(
            self.store.run_context_pack_knowledge_target,
            run_id,
        )
        if target is not None:
            await self._require_current_project_kb_target(target)

    @asynccontextmanager
    async def _terminal_project_lock_for_run(
        self,
        run_id: str,
    ) -> AsyncIterator[None]:
        locks = self.project_locks
        if locks is None:
            yield
            return
        target = await asyncio.to_thread(
            self.store.run_context_pack_knowledge_target,
            run_id,
        )
        if target is None or target.project_id is None:
            raise AgentContextPackConflict(
                "Run has no project binding for terminal validation",
                code="context_pack_project_kb_unbound",
            )
        database = getattr(self.store, "database", None)
        binding_getter = getattr(database, "get_project_binding", None)
        if not callable(binding_getter):
            raise AgentContextPackConflict(
                "Project binding lookup is unavailable for terminal validation",
                code="project_kb_unavailable",
            )
        binding = await asyncio.to_thread(binding_getter, target.project_id)
        if binding is None:
            raise AgentContextPackConflict(
                "Project binding was not found for terminal validation",
                code="project_kb_unavailable",
            )
        async with locks.for_path(binding.root_path):
            yield

    @staticmethod
    async def _await_terminal_store_operation(
        callback: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        operation = asyncio.create_task(
            asyncio.to_thread(callback, *args, **kwargs),
            name="agent-terminal-store-operation",
        )
        request_cancelled = False
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                request_cancelled = True
        if request_cancelled:
            raise asyncio.CancelledError
        return result

    async def _acquire_current_project_kb_lease_for_run(
        self,
        run_id: str,
    ) -> AbstractContextManager[object]:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self._enter_current_project_kb_lease_for_run_sync,
                run_id,
            ),
            name=f"project-kb-lease-acquire:{run_id}",
        )
        request_cancelled = False
        while True:
            try:
                lease = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                request_cancelled = True
        if request_cancelled:
            lease.__exit__(None, None, None)
            raise asyncio.CancelledError
        return lease

    def _enter_current_project_kb_lease_for_run_sync(
        self,
        run_id: str,
    ) -> AbstractContextManager[object]:
        target = self.store.run_context_pack_knowledge_target(run_id)
        if target is None:
            raise AgentContextPackConflict(
                "Run has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        lease = self._current_project_kb_target_lease_sync(target)
        lease.__enter__()
        return lease

    @contextmanager
    def _current_project_kb_target_lease_sync(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> Iterator[object]:
        if (
            target.project_id is None
            or target.project_kb_state_id is None
            or target.project_kb_manifest_hash is None
        ):
            raise AgentContextPackConflict(
                "Context Pack has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        # O2-E packs bind a distillation-index identity, not a published .kb state.
        if self._is_managed_o2e_context_target(target):
            yield {
                "project_id": target.project_id,
                "project_kb_state_id": target.project_kb_state_id,
                "project_kb_manifest_hash": target.project_kb_manifest_hash,
                "source": "external_distillation",
            }
            return
        service = self.project_kb_service
        if service is None:
            raise AgentContextPackConflict(
                "Project KB verification is not configured",
                code="project_kb_unavailable",
            )
        try:
            with service.current_binding_lease(
                target.project_id,
                project_kb_state_id=target.project_kb_state_id,
                project_kb_manifest_hash=target.project_kb_manifest_hash,
                expected_knowledge_fingerprint=(
                    target.project_kb_knowledge_fingerprint
                ),
            ) as manifest:
                yield manifest
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            raise AgentContextPackConflict(
                str(exc),
                code=str(detail.get("code") or "project_kb_unavailable"),
            ) from exc


    @staticmethod
    def _is_managed_o2e_context_target(target: ContextPackKnowledgeTarget) -> bool:
        return (
            target.nlcl_route_source == "external_distillation"
            and target.context_pack_id.startswith("context-pack-o2e-")
            and target.project_id is not None
            and target.project_kb_state_id is not None
            and target.project_kb_manifest_hash is not None
        )

    async def _require_current_project_kb_target(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> None:
        await asyncio.to_thread(self._require_current_project_kb_target_sync, target)

    def _require_current_project_kb_target_sync(
        self,
        target: ContextPackKnowledgeTarget,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (
            target.project_id is None
            or target.project_kb_state_id is None
            or target.project_kb_manifest_hash is None
        ):
            raise AgentContextPackConflict(
                "Context Pack has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        # O2-E managed packs bind a distillation-index identity, not the live .kb snapshot.
        if self._is_managed_o2e_context_target(target):
            return
        service = self.project_kb_service
        if service is None:
            raise AgentContextPackConflict(
                "Project KB verification is not configured",
                code="project_kb_unavailable",
            )
        try:
            connection_verifier = getattr(
                service,
                "require_current_binding_in_connection",
                None,
            )
            if connection is not None and callable(connection_verifier):
                connection_verifier(
                    connection,
                    target.project_id,
                    project_kb_state_id=target.project_kb_state_id,
                    project_kb_manifest_hash=target.project_kb_manifest_hash,
                    expected_knowledge_fingerprint=(
                        target.project_kb_knowledge_fingerprint
                    ),
                )
            else:
                service.require_current_binding(
                    target.project_id,
                    project_kb_state_id=target.project_kb_state_id,
                    project_kb_manifest_hash=target.project_kb_manifest_hash,
                    expected_knowledge_fingerprint=(
                        target.project_kb_knowledge_fingerprint
                    ),
                )
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            raise AgentContextPackConflict(
                str(exc),
                code=str(detail.get("code") or "project_kb_unavailable"),
            ) from exc

    async def _observe_knowledge_target(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> KnowledgeFingerprintObservation:
        observed_at = datetime.now(UTC)
        expected = target.knowledge_fingerprint
        if expected is None:
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=None,
                observed_fingerprint=None,
                state=KnowledgeObservationState.MATCHED,
                observed_at=observed_at,
            )
        # O2-E packs are closed over the managed distillation-index fingerprint.
        if self._is_managed_o2e_context_target(target):
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
                observed_fingerprint=expected,
                state=KnowledgeObservationState.MATCHED,
                observed_at=observed_at,
            )
        probe = self.knowledge_fingerprint_probe
        if probe is None:
            return self._unavailable_knowledge_observation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
            )
        try:
            observed = await asyncio.to_thread(probe, target.project_id)
            state = (
                KnowledgeObservationState.MATCHED
                if observed == expected
                else KnowledgeObservationState.CHANGED
            )
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
                observed_fingerprint=observed,
                state=state,
                observed_at=observed_at,
            )
        except Exception:
            return self._unavailable_knowledge_observation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
            )

    @staticmethod
    def _unavailable_knowledge_observation(
        *,
        context_pack_id: str,
        pack_hash: str,
        expected_fingerprint: str | None,
    ) -> KnowledgeFingerprintObservation:
        return KnowledgeFingerprintObservation(
            context_pack_id=context_pack_id,
            pack_hash=pack_hash,
            expected_fingerprint=expected_fingerprint,
            observed_fingerprint=None,
            state=KnowledgeObservationState.UNAVAILABLE,
            observed_at=datetime.now(UTC),
        )

    async def _transition(self, run_id: str, status: RunStatus, detail: str) -> Run:
        run = await asyncio.to_thread(
            self.store.transition_run,
            run_id,
            status,
            detail=detail,
        )
        await self.broker.publish(run_id)
        return run

    async def _mark_cancelled(self, run_id: str) -> None:
        try:
            await asyncio.to_thread(self.store.mark_cancelled, run_id)
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)

    async def _mark_interrupted(self, run_id: str) -> None:
        try:
            await asyncio.to_thread(
                self.store.mark_interrupted,
                run_id,
                detail="Runtime stopped before the Run reached a terminal state",
            )
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)

    async def _fail(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.store.fail_run,
                run_id,
                code=code,
                message=message,
                retryable=retryable,
            )
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)
