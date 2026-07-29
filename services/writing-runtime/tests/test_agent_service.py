from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderCancelled,
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_models import AgentRunResumeRequest
from zhimeng_runtime.agent_service import (
    AgentService,
    AgentUnavailableError,
    ToolExecutionResult,
)
from zhimeng_runtime.agent_store import (
    AgentContextPackConflict,
    AgentContextPackSourceUnavailable,
    AgentStore,
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
    ContextPackKnowledgeTarget,
    PreparedContextInjection,
    StoredToolCall,
    WorkflowResumeClaim,
)
from zhimeng_runtime.agent_tools import AgentToolError, AgentToolExecution
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import ContextCreateRequest
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.errors import ErrorDetail, ErrorEnvelope
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    ProjectRevision,
    Run,
    RunStatus,
)
from zhimeng_runtime.provider_turn_models import ProviderTurnReceipt
from zhimeng_runtime.project_kb_service import ProjectKbConflictError
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.workflow_models import (
    build_provider_io_boundary,
    build_workflow_plan,
    build_workflow_step,
    build_workflow_step_checkpoint,
)
from zhimeng_runtime.workflow_runner import WorkflowExecutionResult


PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "b" * 64
PROJECT_KB_STATE_ID = "project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROJECT_KB_MANIFEST_HASH = "c" * 64


class CurrentProjectKbVerifier:
    def require_current_binding(
        self,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> SimpleNamespace:
        assert project_id == PROJECT_ID
        assert project_kb_state_id == PROJECT_KB_STATE_ID
        assert project_kb_manifest_hash == PROJECT_KB_MANIFEST_HASH
        return SimpleNamespace(
            knowledge_fingerprint=expected_knowledge_fingerprint,
        )

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        yield self.require_current_binding(project_id, **kwargs)


class SequenceProjectKbVerifier(CurrentProjectKbVerifier):
    def __init__(self, *, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    def require_current_binding(self, *args, **kwargs) -> SimpleNamespace:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise ProjectKbConflictError(
                "project KB state changed during execution",
                details=[{"code": "project_kb_state_stale"}],
                retryable=True,
            )
        return super().require_current_binding(*args, **kwargs)


class TrackingProjectKbVerifier(CurrentProjectKbVerifier):
    def __init__(self) -> None:
        self.lease_active = threading.Event()
        self.lease_released = threading.Event()
        self.expected_knowledge_fingerprints: list[str | None] = []

    def require_current_binding(self, *args, **kwargs) -> SimpleNamespace:
        self.expected_knowledge_fingerprints.append(
            kwargs.get("expected_knowledge_fingerprint")
        )
        return super().require_current_binding(*args, **kwargs)

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        manifest = self.require_current_binding(project_id, **kwargs)
        self.lease_active.set()
        try:
            yield manifest
        finally:
            self.lease_active.clear()
            self.lease_released.set()


class ScriptedProvider:
    def __init__(
        self,
        events: list[ProviderEvent],
        *,
        block_after: bool = False,
        before_events: asyncio.Event | None = None,
    ) -> None:
        self.events = events
        self.block_after = block_after
        self.before_events = before_events
        self.started = asyncio.Event()
        self.start_count = 0
        self.messages: list[dict[str, str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "b" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.scripted-provider",
            kind=ExecutorKind.EMBEDDED,
            implementation="scripted-provider",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        self.start_count += 1
        self.messages = [dict(message) for message in messages]
        self.started.set()
        if self.before_events is not None:
            await self.before_events.wait()
        for event in self.events:
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event
        while self.block_after and not cancel_event.is_set():
            await asyncio.sleep(0.05)


class TurnScriptedProvider(ScriptedProvider):
    def __init__(self, turns: list[list[ProviderEvent]]) -> None:
        super().__init__([])
        self.turns = turns
        self.messages_by_turn: list[list[dict[str, object]]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        self.start_count += 1
        self.started.set()
        self.messages_by_turn.append([dict(message) for message in messages])
        if self.start_count > len(self.turns):
            raise AssertionError("Provider was dispatched for too many turns")
        for event in self.turns[self.start_count - 1]:
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event


class MutatingAliasProvider(ScriptedProvider):
    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.accepted_reported_models = ("late-added-model",)
        async for event in super().stream_chat(
            messages,
            cancel_event=cancel_event,
            tools=tools,
        ):
            yield event


class ShutdownTranslatingProvider(ScriptedProvider):
    def __init__(self, translated_error: str) -> None:
        super().__init__([])
        self.translated_error = translated_error

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del messages, cancel_event, tools
        self.start_count += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            if self.translated_error == "cancelled":
                raise AgentProviderCancelled from exc
            raise AgentProviderError(
                "Provider translated task cancellation",
                code="agent_provider_translated_cancel",
                retryable=True,
            ) from exc
        yield  # pragma: no cover


class ReplayFirstAgentStore(AgentStore):
    def __init__(self, database: SqliteStore) -> None:
        super().__init__(database)
        self.original_created = threading.Event()
        self.replay_returned = threading.Event()

    def create_run(self, **kwargs):
        result = super().create_run(**kwargs)
        if result[2]:
            self.replay_returned.set()
        else:
            self.original_created.set()
            if not self.replay_returned.wait(timeout=5):
                raise AssertionError("concurrent idempotent replay did not arrive")
            time.sleep(0.1)
        return result


class BlockingCreateAgentStore(AgentStore):
    def __init__(self, database: SqliteStore) -> None:
        super().__init__(database)
        self.create_started = threading.Event()
        self.release_create = threading.Event()

    def create_run(self, **kwargs):
        self.create_started.set()
        if not self.release_create.wait(timeout=5):
            raise AssertionError("blocked Run creation was never released")
        return super().create_run(**kwargs)


class BlockingReplayAgentStore(AgentStore):
    def __init__(self, database: SqliteStore) -> None:
        super().__init__(database)
        self.replay_started = threading.Event()
        self.release_replay = threading.Event()

    def create_run(self, **kwargs):
        result = super().create_run(**kwargs)
        if result[2]:
            self.replay_started.set()
            if not self.release_replay.wait(timeout=5):
                raise AssertionError("blocked idempotent replay was never released")
        return result


class BlockingCancelAgentStore(AgentStore):
    def __init__(self, database: SqliteStore) -> None:
        super().__init__(database)
        self.cancel_committed = threading.Event()
        self.release_cancel = threading.Event()

    def cancel_run(self, run_id: str) -> Run:
        run = super().cancel_run(run_id)
        self.cancel_committed.set()
        if not self.release_cancel.wait(timeout=5):
            raise AssertionError("blocked cancel claim was never released")
        return run


class FinalizeBarrierAgentService(AgentService):
    def __init__(self, store: AgentStore, provider: ScriptedProvider) -> None:
        super().__init__(store, provider)
        self.finalized = asyncio.Event()
        self.release_finalize = asyncio.Event()

    async def _finalize_run_start(self, run_id: str) -> Run:
        run = await super()._finalize_run_start(run_id)
        self.finalized.set()
        await self.release_finalize.wait()
        return run


class SequenceKnowledgeProbe:
    def __init__(self, *results: str | BaseException) -> None:
        self.results = list(results)
        self.call_count = 0
        self.project_ids: list[str | None] = []

    def __call__(self, project_id: str | None = None) -> str:
        self.call_count += 1
        self.project_ids.append(project_id)
        if not self.results:
            raise AssertionError("knowledge probe was called too many times")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class BlockingToolExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.definitions = (
            ProviderToolDefinition(
                name="project_read_file",
                version="1",
                description="Read a fixture file",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string"},
                    },
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            ),
        )

    @staticmethod
    def request_hash(tool_name: str, tool_version: str, arguments: dict[str, object]) -> str:
        del tool_name, tool_version, arguments
        return "a" * 64

    async def execute(self, *args, **kwargs) -> AgentToolExecution:
        del args, kwargs
        self.started.set()
        await self.release.wait()
        return AgentToolExecution(output={"ok": True})


def workflow_run(status: RunStatus = RunStatus.PAUSED) -> Run:
    created_at = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    return Run(
        schema="zhimeng.execution-run.v1",
        run_id="run_agent_resume_00000001",
        session_id="session_agent_resume_0001",
        ordinal=1,
        status=status,
        objective="Resume the reviewed prewrite workflow.",
        created_at=created_at,
        updated_at=created_at,
        started_at=created_at,
        completed_at=None,
        executor=ScriptedProvider([]).identity,
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.SKILLS],
            extensions={},
        ),
        project_revision=ProjectRevision(
            project_id=PROJECT_ID,
            revision=1,
            fingerprint=FINGERPRINT,
            observed_at=created_at,
        ),
        correlation_id="correlation_agent_resume_01",
        causation_id=None,
        last_activity_sequence=4,
        checkpoint_id=None,
        error=None,
    )


def workflow_plan(run: Run, *, knowledge_fingerprint: str | None = None):
    step = build_workflow_step(
        ordinal=1,
        skill_id="novel-creation-suite.project-audit",
        expected_skill_version_id=f"skill-version-{'1' * 32}",
        expected_skill_version="1",
        expected_manifest_fingerprint="2" * 64,
        expected_package_fingerprint="3" * 64,
        input_contract=(
            "https://zhimeng.local/contracts/"
            "creation-project-audit-request.schema.json"
        ),
        output_contract=(
            "https://zhimeng.local/contracts/"
            "creation-project-audit-result.schema.json"
        ),
        input_builder_id="zhimeng.workflow-input.project-audit",
        input_builder_version="1",
    )
    return build_workflow_plan(
        run_id=run.run_id,
        project_id=PROJECT_ID,
        project_revision=1,
        project_fingerprint=FINGERPRINT,
        context_pack_id="context-pack-agent-resume",
        context_pack_hash="4" * 64,
        knowledge_fingerprint=knowledge_fingerprint,
        route_receipt_id=f"nlcl-route-{'5' * 32}",
        route_receipt_hash="6" * 64,
        operation_package_id=f"prewrite-package-{'7' * 32}",
        operation_package_hash="8" * 64,
        steps=(step,),
        created_at=run.created_at,
    )


class ResumeStore:
    def __init__(
        self,
        run: Run,
        *,
        knowledge_fingerprint: str | None = None,
        resume_blocker: str | None = None,
    ) -> None:
        self.run = run
        self.plan = workflow_plan(
            run,
            knowledge_fingerprint=knowledge_fingerprint,
        )
        self.chain = []
        self.resume_blocker = resume_blocker
        self.claim_calls = 0
        self.plan_reads = 0

    def get_run(self, run_id: str) -> Run:
        assert run_id == self.run.run_id
        return self.run

    def get_workflow_plan(self, run_id: str):
        assert run_id == self.run.run_id
        self.plan_reads += 1
        return self.plan

    def list_workflow_checkpoints(self, run_id: str):
        assert run_id == self.run.run_id
        return list(self.chain)

    def workflow_resume_blocker(self, run_id: str) -> str | None:
        assert run_id == self.run.run_id
        return self.resume_blocker

    def run_context_pack_knowledge_target(
        self,
        run_id: str,
    ) -> ContextPackKnowledgeTarget:
        assert run_id == self.run.run_id
        return ContextPackKnowledgeTarget(
            context_pack_id=self.plan.context_pack_id,
            pack_hash=self.plan.context_pack_hash,
            knowledge_fingerprint=self.plan.knowledge_fingerprint,
            project_kb_knowledge_fingerprint=self.plan.knowledge_fingerprint,
            project_id=PROJECT_ID,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )

    def claim_workflow_resume(self, run_id: str, **expected) -> WorkflowResumeClaim:
        assert run_id == self.run.run_id
        assert expected == {
            "expected_workflow_plan_id": self.plan.workflow_plan_id,
            "expected_plan_hash": self.plan.plan_hash,
            "expected_checkpoint_id": None,
            "expected_checkpoint_hash": None,
        }
        self.claim_calls += 1
        self.run = self.run.model_copy(update={"status": RunStatus.RUNNING})
        return WorkflowResumeClaim(
            run=self.run,
            plan=self.plan,
            checkpoints=(),
            next_step_ordinal=1,
        )


class CancelRaceResumeStore(ResumeStore):
    def __init__(self, run: Run) -> None:
        super().__init__(run)
        self.claim_started = threading.Event()
        self.release_claim = threading.Event()
        self.cancel_calls = 0

    def claim_workflow_resume(self, run_id: str, **expected) -> WorkflowResumeClaim:
        claim = super().claim_workflow_resume(run_id, **expected)
        self.claim_started.set()
        if not self.release_claim.wait(timeout=5):
            raise AssertionError("blocked Resume claim was never released")
        return claim

    def cancel_run(self, run_id: str) -> Run:
        assert run_id == self.run.run_id
        self.cancel_calls += 1
        self.run = self.run.model_copy(update={"status": RunStatus.CANCELLING})
        return self.run

    def mark_cancelled(self, run_id: str) -> Run:
        assert run_id == self.run.run_id
        self.run = self.run.model_copy(update={"status": RunStatus.CANCELLED})
        return self.run


class ResumeSkillExecutor:
    def __init__(self, *, drifted: bool = False) -> None:
        self.drifted = drifted

    def active_skill(self, project_id: str, skill_id: str) -> SimpleNamespace:
        assert project_id == PROJECT_ID
        assert skill_id == "novel-creation-suite.project-audit"
        return SimpleNamespace(
            skill_version_id=f"skill-version-{'1' * 32}",
            manifest=SimpleNamespace(
                version="1",
                input_schema=(
                    "https://zhimeng.local/contracts/"
                    "creation-project-audit-request.schema.json"
                ),
                output_schema=(
                    "https://zhimeng.local/contracts/"
                    "creation-project-audit-result.schema.json"
                ),
            ),
            manifest_fingerprint="2" * 64,
            package_fingerprint=("f" if self.drifted else "3") * 64,
        )


class ResumeToolExecutor:
    def __init__(
        self,
        *,
        validation_error: AgentToolError | None = None,
        skill_drift: bool = False,
    ) -> None:
        self.definitions = ()
        self.skill_executor = ResumeSkillExecutor(drifted=skill_drift)
        self.validation_error = validation_error
        self.validation_calls = 0

    async def validate_run_project(self, run: Run) -> object:
        self.validation_calls += 1
        if self.validation_error is not None:
            raise self.validation_error
        return SimpleNamespace(project_id=run.project_revision.project_id)


class ProjectRaceResumeToolExecutor(ResumeToolExecutor):
    async def validate_run_project(self, run: Run) -> object:
        self.validation_calls += 1
        if self.validation_calls == 2:
            raise AgentToolError(
                "Project changed after the Resume claim",
                code="agent_tool_project_changed",
                retryable=True,
            )
        return SimpleNamespace(project_id=run.project_revision.project_id)


class CaptureResumeService(AgentService):
    def __init__(self, store: ResumeStore, tool_executor: ResumeToolExecutor, **kwargs):
        kwargs.setdefault("project_kb_service", CurrentProjectKbVerifier())
        super().__init__(
            store,  # type: ignore[arg-type]
            ScriptedProvider([]),
            tool_executor=tool_executor,  # type: ignore[arg-type]
            context_store=object(),  # type: ignore[arg-type]
            **kwargs,
        )
        self.launches: list[tuple[str, bool]] = []

    def _launch(self, run_id: str, *, resumed: bool = False) -> None:
        self.launches.append((run_id, resumed))


class ResumeExecutionStore(ResumeStore):
    def __init__(
        self,
        run: Run,
        *,
        knowledge_fingerprint: str | None = None,
    ) -> None:
        super().__init__(run, knowledge_fingerprint=knowledge_fingerprint)
        self.completed = False
        self.boundary_present = False
        self.pause_calls = 0
        self.interrupt_calls = 0
        self.recover_calls = 0
        self.last_failure: dict[str, object] | None = None

    def prepare_context_injection(self, run_id: str, **kwargs):
        del kwargs
        assert run_id == self.run.run_id
        return PreparedContextInjection(
            context_pack_id=self.plan.context_pack_id,
            pack_hash=self.plan.context_pack_hash,
            injection_hash="9" * 64,
            provider_messages_hash="a" * 64,
            messages=({"role": "user", "content": self.run.objective},),
        )

    def append_assistant_delta(self, run_id: str, **kwargs) -> None:
        del kwargs
        assert run_id == self.run.run_id

    def get_writer_mode(self, run_id: str) -> str:
        assert run_id == self.run.run_id
        return "repair_only"

    def ensure_writer_task(self, run_id: str, *, target_files):
        assert run_id == self.run.run_id
        assert target_files == ()
        return ResumeWriterTask()

    def ensure_creation_instruction_materialization(self, run_id: str):
        assert run_id == self.run.run_id
        return ResumeInstructionMaterialization()

    def complete_run(self, run_id: str, **kwargs) -> Run:
        del kwargs
        assert run_id == self.run.run_id
        self.completed = True
        self.run = self.run.model_copy(update={"status": RunStatus.SUCCEEDED})
        return self.run

    def fail_run(self, run_id: str, **kwargs) -> Run:
        assert run_id == self.run.run_id
        self.last_failure = dict(kwargs)
        self.run = self.run.model_copy(update={"status": RunStatus.FAILED})
        return self.run

    def get_provider_io_boundary(self, run_id: str):
        assert run_id == self.run.run_id
        if not self.boundary_present:
            raise AgentStoreNotFound("Provider I/O boundary was not found")
        return SimpleNamespace(run_id=run_id)

    def pause_workflow_for_restart(self, run_id: str) -> Run:
        assert run_id == self.run.run_id
        self.pause_calls += 1
        self.run = self.run.model_copy(update={"status": RunStatus.PAUSED})
        return self.run

    def mark_interrupted(self, run_id: str, *, detail: str) -> Run:
        del detail
        assert run_id == self.run.run_id
        self.interrupt_calls += 1
        self.run = self.run.model_copy(update={"status": RunStatus.INTERRUPTED})
        return self.run

    def recover_interrupted(self) -> int:
        self.recover_calls += 1
        return 0

    def list_nonterminal_workflow_run_ids(self) -> list[str]:
        if self.run.status in {
            RunStatus.QUEUED,
            RunStatus.STARTING,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
        }:
            return [self.run.run_id]
        return []


class ResumeWriterTask:
    mode = "repair_only"
    writer_task_id = f"writer-task-{'1' * 32}"
    task_hash = "2" * 64

    def model_dump(self, **kwargs) -> dict[str, object]:
        del kwargs
        return {
            "schema": "zhimeng.writer-task.v1",
            "writer_task_id": self.writer_task_id,
            "mode": self.mode,
            "task_hash": self.task_hash,
        }


class ResumeInstructionMaterialization:
    materialization_id = f"creation-instruction-materialization-{'3' * 32}"
    materialization_hash = "4" * 64

    def model_dump(self, **kwargs) -> dict[str, object]:
        del kwargs
        return {
            "schema": "zhimeng.creation-instruction-materialization.v1",
            "materialization_id": self.materialization_id,
            "materialization_hash": self.materialization_hash,
        }


class ResumeContextStore:
    @staticmethod
    def get_prewrite_operation_package(context_pack_id: str) -> SimpleNamespace:
        assert context_pack_id == "context-pack-agent-resume"
        return SimpleNamespace(active_chapter_id=None)


class ResumedWorkflowRunner:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.reconcile_calls = 0

    async def execute_pre_provider(self, *args, **kwargs) -> WorkflowExecutionResult:
        del args, kwargs
        return WorkflowExecutionResult(
            plan=self.plan,
            checkpoints=(),
            provider_messages=(),
            tool_call_count=0,
        )

    def validate_plan_dependencies(self, plan) -> None:
        assert plan == self.plan

    def reconcile_startup(self, run_id: str) -> SimpleNamespace:
        assert run_id == self.plan.run_id
        self.reconcile_calls += 1
        return SimpleNamespace(reconciled_count=0)


class BlockingResumedWorkflowRunner(ResumedWorkflowRunner):
    def __init__(self, plan) -> None:
        super().__init__(plan)
        self.started = asyncio.Event()

    async def execute_pre_provider(self, *args, **kwargs) -> WorkflowExecutionResult:
        del args, kwargs
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking workflow unexpectedly resumed")


class ResumeExecutionService(AgentService):
    def __init__(
        self,
        store: ResumeExecutionStore,
        provider: ScriptedProvider,
        tool_executor: ResumeToolExecutor,
        *,
        knowledge_fingerprint_probe=None,
        project_kb_service=None,
    ) -> None:
        super().__init__(
            store,  # type: ignore[arg-type]
            provider,
            tool_executor=tool_executor,  # type: ignore[arg-type]
            context_store=ResumeContextStore(),  # type: ignore[arg-type]
            knowledge_fingerprint_probe=knowledge_fingerprint_probe,
            project_kb_service=(
                CurrentProjectKbVerifier()
                if project_kb_service is None
                else project_kb_service
            ),
        )
        self.transitions: list[RunStatus] = []
        self.boundary_persisted = False
        self.workflow_runner = ResumedWorkflowRunner(store.plan)

    async def _transition(self, run_id: str, status: RunStatus, detail: str) -> Run:
        del run_id, detail
        self.transitions.append(status)
        raise AssertionError("resumed execution must not perform a fresh transition")

    async def _has_managed_workflow(self, context_pack_id: str) -> bool:
        del context_pack_id
        return True

    def _workflow_runner(self) -> ResumedWorkflowRunner:
        return self.workflow_runner

    async def _persist_provider_io_boundary(self, *args, **kwargs) -> None:
        del args, kwargs
        self.boundary_persisted = True
        self.store.boundary_present = True  # type: ignore[attr-defined]


class ReceiptResumeStore(ResumeExecutionStore):
    def __init__(
        self,
        run: Run,
        *,
        knowledge_fingerprint: str | None = None,
    ) -> None:
        super().__init__(run, knowledge_fingerprint=knowledge_fingerprint)
        self.provider_turn_receipts: list[ProviderTurnReceipt] = []

    def create_provider_turn_receipt(
        self,
        receipt: ProviderTurnReceipt,
    ) -> ProviderTurnReceipt:
        assert receipt.run_id == self.run.run_id
        assert receipt.turn_ordinal == len(self.provider_turn_receipts) + 1
        self.provider_turn_receipts.append(receipt)
        return receipt


class ReceiptResumeService(ResumeExecutionService):
    def __init__(
        self,
        store: ReceiptResumeStore,
        provider: ScriptedProvider,
        tool_executor: ResumeToolExecutor,
    ) -> None:
        super().__init__(store, provider, tool_executor)
        self.receipt_counts_before_tools: list[int] = []

    async def _persist_provider_io_boundary(
        self,
        run: Run,
        workflow: WorkflowExecutionResult,
        *,
        provider_messages: list[dict[str, object]],
        tool_definitions: tuple[ProviderToolDefinition, ...],
        provider: ScriptedProvider,
        accepted_reported_models: tuple[str, ...],
    ) -> SimpleNamespace:
        del workflow
        self.boundary_persisted = True
        self.store.boundary_present = True  # type: ignore[attr-defined]
        request_hash = self._provider_dispatch_material_hash(
            provider_messages=provider_messages,
            tool_definitions=tool_definitions,
            provider=provider,
            dispatch_config_fingerprint=self._require_bound_provider(run, provider),
            accepted_reported_models=accepted_reported_models,
        )
        return SimpleNamespace(
            run_id=run.run_id,
            boundary_id=f"provider-io-{'1' * 32}",
            boundary_hash="2" * 64,
            request_hash=request_hash,
            provider_model=self._provider_requested_model(provider),
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
        del cancel_event
        self.receipt_counts_before_tools.append(
            len(self.store.provider_turn_receipts)  # type: ignore[attr-defined]
        )
        now = datetime.now(UTC)
        return ToolExecutionResult(
            tool_call=StoredToolCall(
                tool_call_id=f"tool_{provider_tool_call_id}",
                run_id=run.run_id,
                tool_name=tool_name,
                tool_version="1",
                status="succeeded",
                idempotency_key=provider_tool_call_id,
                request_hash="3" * 64,
                arguments=arguments,
                output={"ok": True},
                error=None,
                call_activity_id=None,
                result_activity_id=None,
                result_hash="4" * 64,
                duration_ms=1,
                started_at=now,
                finished_at=now,
            ),
            replayed=False,
        )


def build_service(
    tmp_path: Path,
    provider: ScriptedProvider | None,
    *,
    knowledge_probe=None,
    tool_executor=None,
) -> AgentService:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    return AgentService(
        AgentStore(database),
        provider,
        knowledge_fingerprint_probe=knowledge_probe,
        tool_executor=tool_executor,
        project_kb_service=CurrentProjectKbVerifier(),
    )


async def wait_terminal(service: AgentService, run_id: str):
    for _ in range(200):
        run = await asyncio.to_thread(service.store.get_run, run_id)
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("Run did not reach a terminal state")


async def create_turn(service: AgentService):
    session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
    result = await service.start_run(
        session_id=session.session_id,
        message="保留线索，收紧开头。",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key="turn-00000001",
    )
    return session, result


def test_agent_service_persists_stream_before_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [
                ProviderTextDelta("## 建议\n\n"),
                ProviderTextDelta("保留钥匙，并压缩第一段。"),
                ProviderCompleted("stop", None),
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.messages[-1] == {
            "role": "user",
            "content": "保留线索，收紧开头。",
        }
        messages = service.store.list_messages(session.session_id)
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[-1].content == "## 建议\n\n保留钥匙，并压缩第一段。"
        phases = [
            item.root.payload.phase
            for item in service.store.list_activities(run.run_id)
            if item.root.kind.value == "message"
        ]
        assert phases == ["completed", "delta", "delta", "completed"]
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_continues_a_legacy_session_with_bound_provider_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [ProviderTextDelta("继续。"), ProviderCompleted("stop", None)]
        )
        service = build_service(tmp_path, provider)
        legacy_session = await asyncio.to_thread(
            service.store.create_session,
            project_id=PROJECT_ID,
            title="Legacy Writing Agent",
            executor=provider.identity.model_copy(update={"instance_id": None}),
            capabilities=provider.capabilities,
        )

        result = await service.start_run(
            session_id=legacy_session.session_id,
            message="继续上一轮对话。",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id="chapter-001",
            idempotency_key="turn-legacy-provider-0001",
        )
        terminal = await wait_terminal(service, result.run.run_id)

        assert terminal.status is RunStatus.SUCCEEDED
        assert terminal.executor == provider.identity
        assert service.store.get_session(legacy_session.session_id).executor == (
            provider.identity
        )
        assert provider.start_count == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_cancel_during_tool_execution_keeps_run_cancelled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        tool_executor = BlockingToolExecutor()
        provider = ScriptedProvider(
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="call_cancel_tool_0001",
                    tool_name="project_read_file",
                    arguments={"file_id": "file-0123456789abcdef0123456789abcdef"},
                ),
                ProviderCompleted("tool_calls", None),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=tool_executor)
        _, result = await create_turn(service)
        await asyncio.wait_for(tool_executor.started.wait(), timeout=2)
        cancelled, accepted = await service.cancel_run(result.run.run_id)
        assert accepted is True
        assert cancelled.status is RunStatus.CANCELLING
        terminal = await wait_terminal(service, result.run.run_id)
        assert terminal.status is RunStatus.CANCELLED
        runtime_tool_id = service._runtime_tool_call_id(
            result.run.run_id,
            "call_cancel_tool_0001",
        )
        stored = await asyncio.to_thread(
            service.store.get_tool_call,
            runtime_tool_id,
        )
        assert stored.status == "cancelled"
        assert terminal.error is None
        await service.shutdown()

    asyncio.run(scenario())


def test_provider_tool_call_identity_is_scoped_to_its_run() -> None:
    provider_id = "call_provider_reused_0001"
    first = AgentService._runtime_tool_call_id("run_scope_00000001", provider_id)
    replay = AgentService._runtime_tool_call_id("run_scope_00000001", provider_id)
    second = AgentService._runtime_tool_call_id("run_scope_00000002", provider_id)

    assert first == replay
    assert first != second
    assert first.startswith("tool_")


def test_agent_service_marks_incomplete_provider_as_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = build_service(tmp_path, ScriptedProvider([ProviderTextDelta("半段")]))
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)
        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_provider_incomplete_stream"
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_cancel_and_shutdown_have_distinct_terminal_states(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([ProviderTextDelta("首段")], block_after=True)
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        await provider.started.wait()
        _, accepted = await service.cancel_run(result.run.run_id)
        assert accepted is True
        cancelled = await wait_terminal(service, result.run.run_id)
        assert cancelled.status is RunStatus.CANCELLED

        second = await service.start_run(
            session_id=cancelled.session_id,
            message="继续检查。",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=None,
            idempotency_key="turn-00000002",
        )
        await asyncio.sleep(0.03)
        await service.shutdown()
        assert service.store.get_run(second.run.run_id).status is RunStatus.INTERRUPTED

    asyncio.run(scenario())


def test_cancelled_cancel_request_still_signals_the_running_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = BlockingCancelAgentStore(database)
        provider = ScriptedProvider([], block_after=True)
        service = AgentService(store, provider)
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        started = await service.start_run(
            session_id=session.session_id,
            message="等待显式取消。",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=None,
            idempotency_key="turn-cancel-request-disconnect",
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)

        cancel_task = asyncio.create_task(service.cancel_run(started.run.run_id))
        assert await asyncio.to_thread(store.cancel_committed.wait, 2) is True
        cancel_task.cancel()
        await asyncio.sleep(0.05)
        assert cancel_task.done() is False

        store.release_cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await cancel_task

        terminal = await wait_terminal(service, started.run.run_id)
        assert terminal.status is RunStatus.CANCELLED
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_exposes_explicit_unavailable_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = build_service(tmp_path, None)
        assert service.status().available is False
        with pytest.raises(AgentUnavailableError):
            await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        assert service.store.list_sessions(PROJECT_ID) == []

    asyncio.run(scenario())


def test_agent_service_sends_exact_reviewed_context_and_persists_receipt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [ProviderTextDelta("已按项目真值处理。"), ProviderCompleted("stop", None)]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        request = ContextCreateRequest(
            objective="保留线索，收紧开头。",
            profile="rewrite",
            active_chapter_id="chapter-001",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-service-exact",
            session_id=session.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
        )
        ContextStore(service.store.database).create_pack(pack)

        result = await service.start_run(
            session_id=session.session_id,
            message=request.objective,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=request.active_chapter_id,
            idempotency_key="turn-context-service-0001",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
        )
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.messages == [
            message.model_dump(mode="json")
            for message in pack.exact_provider_messages
        ]
        with service.store.database.connection() as connection:
            receipt = connection.execute(
                """
                SELECT content_hash, metadata_json FROM artifact
                WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                """,
                (run.run_id,),
            ).fetchone()
        assert receipt is not None
        assert str(receipt["content_hash"]) == pack.provider_messages_hash
        assert pack.injection_hash in str(receipt["metadata_json"])
        await service.shutdown()

    asyncio.run(scenario())


def test_knowledge_change_after_binding_fails_before_provider_io(tmp_path: Path) -> None:
    async def scenario() -> None:
        knowledge_fingerprint = "d" * 64
        probe = SequenceKnowledgeProbe(knowledge_fingerprint, "e" * 64)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        service = build_service(
            tmp_path,
            provider,
            knowledge_probe=probe,
        )
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        request = ContextCreateRequest(
            objective="按已审阅知识收紧开头。",
            profile="rewrite",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-service-knowledge-changed",
            session_id=session.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
            knowledge_fingerprint=knowledge_fingerprint,
        )
        ContextStore(service.store.database).create_pack(pack)

        result = await service.start_run(
            session_id=session.session_id,
            message=request.objective,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=None,
            idempotency_key="turn-context-service-knowledge-changed",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
        )
        run = await wait_terminal(service, result.run.run_id)

        assert probe.call_count == 2
        assert probe.project_ids == [PROJECT_ID, PROJECT_ID]
        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "context_pack_source_changed"
        assert run.error.error.retryable is True
        assert provider.started.is_set() is False
        with service.store.database.connection() as connection:
            receipt_count = connection.execute(
                """
                SELECT COUNT(*) FROM artifact
                WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                """,
                (run.run_id,),
            ).fetchone()[0]
        assert receipt_count == 0
        await service.shutdown()

    asyncio.run(scenario())


def test_unavailable_knowledge_rejects_first_binding_without_consuming_pack(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        knowledge_fingerprint = "d" * 64
        probe = SequenceKnowledgeProbe(RuntimeError("temporarily unavailable"))
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        service = build_service(
            tmp_path,
            provider,
            knowledge_probe=probe,
        )
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        request = ContextCreateRequest(
            objective="按已审阅知识继续。",
            profile="rewrite",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-service-knowledge-unavailable",
            session_id=session.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
            knowledge_fingerprint=knowledge_fingerprint,
        )
        context_store = ContextStore(service.store.database)
        context_store.create_pack(pack)

        with pytest.raises(AgentContextPackSourceUnavailable) as unavailable:
            await service.start_run(
                session_id=session.session_id,
                message=request.objective,
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id=None,
                idempotency_key="turn-context-service-knowledge-unavailable",
                context_pack_id=pack.context_pack_id,
                expected_context_pack_hash=pack.pack_hash,
            )

        assert unavailable.value.retryable is True
        assert service.store.list_runs(session.session_id) == []
        assert context_store.get_pack(pack.context_pack_id).consumed_by_run_id is None
        assert provider.started.is_set() is False
        await service.shutdown()

    asyncio.run(scenario())


def test_context_integrity_failure_never_starts_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        request = ContextCreateRequest(
            objective="保留线索，收紧开头。",
            profile="rewrite",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-service-tampered",
            session_id=session.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
        )
        ContextStore(service.store.database).create_pack(pack)
        with service.store.database.connection() as connection:
            connection.execute(
                "UPDATE context_pack SET payload_json = '{}' WHERE id = ?",
                (pack.context_pack_id,),
            )
        result = await service.start_run(
            session_id=session.session_id,
            message=request.objective,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=None,
            idempotency_key="turn-context-service-tampered",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
        )

        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "context_injection_integrity_failed"
        assert provider.started.is_set() is False
        await service.shutdown()

    asyncio.run(scenario())


def test_concurrent_idempotent_replay_launches_provider_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = ReplayFirstAgentStore(database)
        release_provider = asyncio.Event()
        provider = ScriptedProvider(
            [ProviderTextDelta("只执行一次。"), ProviderCompleted("stop", None)],
            before_events=release_provider,
        )
        service = AgentService(
            store,
            provider,
            project_kb_service=CurrentProjectKbVerifier(),
        )
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        request = ContextCreateRequest(
            objective="按已经审阅的上下文执行。",
            profile="rewrite",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-concurrent-replay",
            session_id=session.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
        )
        ContextStore(database).create_pack(pack)
        start_kwargs = {
            "session_id": session.session_id,
            "message": request.objective,
            "expected_project_revision": 1,
            "expected_project_fingerprint": FINGERPRINT,
            "active_chapter_id": None,
            "idempotency_key": "turn-context-concurrent-replay",
            "context_pack_id": pack.context_pack_id,
            "expected_context_pack_hash": pack.pack_hash,
        }

        original_task = asyncio.create_task(service.start_run(**start_kwargs))
        original_created = await asyncio.to_thread(
            store.original_created.wait,
            2,
        )
        assert original_created is True
        replay_task = asyncio.create_task(service.start_run(**start_kwargs))
        original, replay = await asyncio.gather(original_task, replay_task)
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        await asyncio.sleep(0.05)

        assert original.replayed is False
        assert replay.replayed is True
        assert original.run.run_id == replay.run.run_id
        assert provider.start_count == 1
        assert service.store.get_run(original.run.run_id).status is RunStatus.RUNNING

        release_provider.set()
        terminal = await wait_terminal(service, original.run.run_id)
        assert terminal.status is RunStatus.SUCCEEDED
        await service.shutdown()

    asyncio.run(scenario())


def test_shutdown_waits_for_inflight_run_creation_before_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = BlockingCreateAgentStore(database)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应发送。"), ProviderCompleted("stop", None)]
        )
        service = AgentService(store, provider)
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )

        start_task = asyncio.create_task(
            service.start_run(
                session_id=session.session_id,
                message="在关闭竞态中创建。",
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id=None,
                idempotency_key="turn-shutdown-create-race",
            )
        )
        assert await asyncio.to_thread(store.create_started.wait, 2) is True
        shutdown_task = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0.05)
        assert shutdown_task.done() is False

        store.release_create.set()
        started = await start_task
        await shutdown_task

        assert provider.start_count == 0
        assert started.run.status is RunStatus.INTERRUPTED
        assert store.get_run(started.run.run_id).status is RunStatus.INTERRUPTED

    asyncio.run(scenario())


def test_cancelled_start_waits_for_transaction_and_launches_durable_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = BlockingCreateAgentStore(database)
        provider = ScriptedProvider(
            [ProviderTextDelta("已持久化。"), ProviderCompleted("stop", None)]
        )
        service = AgentService(store, provider)
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )

        start_task = asyncio.create_task(
            service.start_run(
                session_id=session.session_id,
                message="请求中断也要完成事务协调。",
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id=None,
                idempotency_key="turn-cancelled-create-race",
            )
        )
        assert await asyncio.to_thread(store.create_started.wait, 2) is True
        start_task.cancel()
        await asyncio.sleep(0.05)
        assert start_task.done() is False

        store.release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        runs = store.list_runs(session.session_id)
        assert len(runs) == 1
        terminal = await wait_terminal(service, runs[0].run_id)
        assert terminal.status is RunStatus.SUCCEEDED
        assert provider.start_count == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_start_operation_counter_releases_under_repeated_cancellation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = AgentStore(database)
        provider = ScriptedProvider([], block_after=True)
        service = FinalizeBarrierAgentService(store, provider)
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        start_task = asyncio.create_task(
            service.start_run(
                session_id=session.session_id,
                message="计数器必须最终释放。",
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id=None,
                idempotency_key="turn-start-release-cancellation",
            )
        )
        await asyncio.wait_for(service.finalized.wait(), timeout=2)
        await service._launch_lock.acquire()
        service.release_finalize.set()
        await asyncio.sleep(0.05)
        start_task.cancel()
        start_task.cancel()
        await asyncio.sleep(0.05)
        assert start_task.done() is False
        assert service._start_operations == 1

        service._launch_lock.release()
        started = await start_task
        assert started.run.run_id == store.list_runs(session.session_id)[0].run_id
        assert service._start_operations == 0
        assert service._start_operations_drained.is_set()
        await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_idempotent_replay_does_not_change_shared_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(tmp_path / "novel"),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        store = BlockingReplayAgentStore(database)
        release_provider = asyncio.Event()
        provider = ScriptedProvider(
            [ProviderTextDelta("只执行一次。"), ProviderCompleted("stop", None)],
            before_events=release_provider,
        )
        service = AgentService(store, provider)
        session = await service.create_session(
            project_id=PROJECT_ID,
            title="Writing Agent",
        )
        start_kwargs = {
            "session_id": session.session_id,
            "message": "共享同一个幂等 Run。",
            "expected_project_revision": 1,
            "expected_project_fingerprint": FINGERPRINT,
            "active_chapter_id": None,
            "idempotency_key": "turn-cancelled-replay",
        }

        original = await service.start_run(**start_kwargs)
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        replay_task = asyncio.create_task(service.start_run(**start_kwargs))
        assert await asyncio.to_thread(store.replay_started.wait, 2) is True
        replay_task.cancel()
        store.release_replay.set()
        with pytest.raises(asyncio.CancelledError):
            await replay_task

        assert store.get_run(original.run.run_id).status is RunStatus.RUNNING
        assert provider.start_count == 1
        release_provider.set()
        terminal = await wait_terminal(service, original.run.run_id)
        assert terminal.status is RunStatus.SUCCEEDED
        await service.shutdown()

    asyncio.run(scenario())


def test_resume_run_revalidates_then_cas_claims_before_launch() -> None:
    async def scenario() -> None:
        run = workflow_run()
        store = ResumeStore(run)
        tool_executor = ResumeToolExecutor()
        service = CaptureResumeService(store, tool_executor)

        state = await service.get_workflow_state(run.run_id)
        assert state.resume_state == "resumable"
        assert state.resume_allowed is True
        assert state.next_step_ordinal == 1
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=state.plan.workflow_plan_id,
            expected_plan_hash=state.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        resumed = await service.resume_run(run.run_id, request)

        assert tool_executor.validation_calls == 1
        assert store.claim_calls == 1
        assert store.plan_reads >= 2
        assert service.launches == [(run.run_id, True)]
        assert resumed.run.status is RunStatus.RUNNING
        assert resumed.workflow_plan_id == state.plan.workflow_plan_id
        assert resumed.next_action == "workflow_step"

    asyncio.run(scenario())


def test_cancel_waits_until_resume_claim_is_registered_for_execution() -> None:
    async def scenario() -> None:
        run = workflow_run()
        store = CancelRaceResumeStore(run)
        service = CaptureResumeService(store, ResumeToolExecutor())
        state = await service.get_workflow_state(run.run_id)
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=state.plan.workflow_plan_id,
            expected_plan_hash=state.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        resume_task = asyncio.create_task(service.resume_run(run.run_id, request))
        assert await asyncio.to_thread(store.claim_started.wait, 2) is True
        cancel_task = asyncio.create_task(service.cancel_run(run.run_id))
        await asyncio.sleep(0.05)
        assert cancel_task.done() is False
        assert store.cancel_calls == 0

        store.release_claim.set()
        resumed = await resume_task
        cancelled, accepted = await cancel_task

        assert resumed.accepted is True
        assert resumed.run.status is RunStatus.RUNNING
        assert service.launches == [(run.run_id, True)]
        assert accepted is True
        assert cancelled.status is RunStatus.CANCELLED
        assert store.run.status is RunStatus.CANCELLED

    asyncio.run(scenario())


def test_cancelled_resume_request_still_registers_claimed_run() -> None:
    async def scenario() -> None:
        run = workflow_run()
        store = CancelRaceResumeStore(run)
        service = CaptureResumeService(store, ResumeToolExecutor())
        state = await service.get_workflow_state(run.run_id)
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=state.plan.workflow_plan_id,
            expected_plan_hash=state.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        resume_task = asyncio.create_task(service.resume_run(run.run_id, request))
        assert await asyncio.to_thread(store.claim_started.wait, 2) is True
        resume_task.cancel()
        await asyncio.sleep(0.05)
        assert resume_task.done() is False

        store.release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await resume_task

        assert store.run.status is RunStatus.RUNNING
        assert service.launches == [(run.run_id, True)]

    asyncio.run(scenario())


@pytest.mark.parametrize("execution_status", ["failed", "cancelled"])
def test_workflow_state_hides_next_step_after_terminal_checkpoint(
    execution_status: str,
) -> None:
    async def scenario() -> None:
        run = workflow_run()
        store = ResumeStore(run)
        store.chain = [
            build_workflow_step_checkpoint(
                store.plan,
                store.plan.steps[0],
                attempt_ordinal=1,
                previous_checkpoint=None,
                tool_call_id=None,
                provider_tool_call_id=None,
                skill_call_id=None,
                skill_receipt_id=None,
                skill_receipt_hash=None,
                route_execution_receipt_id=None,
                route_execution_receipt_hash=None,
                input_hash=None,
                output_hash=None,
                tool_result_hash=None,
                execution_status=execution_status,
                domain_status=None,
                error_code=(
                    "workflow_step_failed"
                    if execution_status == "failed"
                    else "cancelled"
                ),
                error_message=f"Workflow step {execution_status}.",
                created_at=run.updated_at,
            )
        ]
        service = CaptureResumeService(store, ResumeToolExecutor())

        state = await service.get_workflow_state(run.run_id)

        assert state.resume_state == "blocked"
        assert state.resume_allowed is False
        assert state.next_step_ordinal is None

    asyncio.run(scenario())


def test_workflow_state_blocks_interrupted_step_without_checkpoint() -> None:
    async def scenario() -> None:
        run = workflow_run()
        store = ResumeStore(run, resume_blocker="workflow_step_interrupted")
        service = CaptureResumeService(store, ResumeToolExecutor())

        state = await service.get_workflow_state(run.run_id)

        assert state.resume_state == "blocked"
        assert state.resume_allowed is False
        assert state.reason_code == "workflow_step_interrupted"
        assert state.next_step_ordinal is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED])
def test_workflow_state_keeps_provider_boundary_uncertainty_for_terminal_run(
    status: RunStatus,
) -> None:
    async def scenario() -> None:
        run = workflow_run().model_copy(
            update={
                "status": status,
                "completed_at": datetime(2026, 7, 21, 10, 1, tzinfo=UTC),
                "error": (
                    ErrorEnvelope(
                        error=ErrorDetail(
                            code="provider_timeout",
                            message="Provider outcome is uncertain.",
                            request_id="run_agent_resume_00000001",
                            retryable=True,
                        )
                    )
                    if status is RunStatus.FAILED
                    else None
                ),
            }
        )
        store = ResumeStore(run)
        checkpoint = build_workflow_step_checkpoint(
            store.plan,
            store.plan.steps[0],
            attempt_ordinal=1,
            previous_checkpoint=None,
            tool_call_id="tool-workflow-terminal-boundary",
            provider_tool_call_id="provider-workflow-terminal-boundary",
            skill_call_id="skill-call-workflow-terminal-boundary",
            skill_receipt_id=f"skill-receipt-{'a' * 32}",
            skill_receipt_hash="b" * 64,
            route_execution_receipt_id=f"nlcl-route-exec-{'c' * 32}",
            route_execution_receipt_hash="d" * 64,
            input_hash="e" * 64,
            output_hash="f" * 64,
            tool_result_hash="1" * 64,
            execution_status="succeeded",
            domain_status="healthy",
            error_code=None,
            error_message=None,
            created_at=run.updated_at,
        )
        boundary = build_provider_io_boundary(
            store.plan,
            (checkpoint,),
            request_hash="2" * 64,
            provider_model=None,
            provider_dispatch_config_fingerprint="b" * 64,
            executor=run.executor,
            created_at=run.updated_at,
        )
        store.chain = [checkpoint, boundary]
        service = CaptureResumeService(store, ResumeToolExecutor())

        state = await service.get_workflow_state(run.run_id)

        assert state.run_status is status
        assert state.resume_state == "provider_dispatch_uncertain"
        assert state.resume_allowed is False
        assert state.reason_code == "provider_dispatch_already_started"
        assert state.next_step_ordinal is None

    asyncio.run(scenario())


def test_resume_run_rejects_project_or_knowledge_drift_before_claim() -> None:
    async def provider_drift() -> None:
        run = workflow_run().model_copy(
            update={
                "executor": ScriptedProvider([]).identity.model_copy(
                    update={"instance_id": f"provider-config-{'f' * 64}"}
                )
            }
        )
        store = ResumeStore(run)
        service = CaptureResumeService(store, ResumeToolExecutor())
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        with pytest.raises(AgentWorkflowCheckpointConflict) as changed:
            await service.resume_run(run.run_id, request)

        assert changed.value.code == "workflow_executor_changed"
        assert store.claim_calls == 0
        assert service.launches == []

    async def project_drift() -> None:
        run = workflow_run()
        store = ResumeStore(run)
        tool_executor = ResumeToolExecutor(
            validation_error=AgentToolError(
                "Project changed after this Run was created",
                code="agent_tool_project_changed",
                retryable=True,
            )
        )
        service = CaptureResumeService(store, tool_executor)
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        with pytest.raises(AgentWorkflowCheckpointConflict) as changed:
            await service.resume_run(run.run_id, request)

        assert changed.value.code == "agent_tool_project_changed"
        assert store.claim_calls == 0
        assert service.launches == []

    async def knowledge_drift() -> None:
        run = workflow_run()
        store = ResumeStore(run, knowledge_fingerprint="d" * 64)
        tool_executor = ResumeToolExecutor()
        probe = SequenceKnowledgeProbe("e" * 64)
        service = CaptureResumeService(
            store,
            tool_executor,
            knowledge_fingerprint_probe=probe,
        )
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        with pytest.raises(AgentContextPackConflict) as changed:
            await service.resume_run(run.run_id, request)

        assert changed.value.code == "context_pack_source_changed"
        assert probe.project_ids == [PROJECT_ID]
        assert store.claim_calls == 0
        assert service.launches == []

    async def skill_drift() -> None:
        run = workflow_run()
        store = ResumeStore(run)
        tool_executor = ResumeToolExecutor(skill_drift=True)
        service = CaptureResumeService(store, tool_executor)
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        with pytest.raises(AgentWorkflowCheckpointConflict) as changed:
            await service.resume_run(run.run_id, request)

        assert changed.value.code == "workflow_plan_skill_changed"
        assert tool_executor.validation_calls == 0
        assert store.claim_calls == 0
        assert service.launches == []

    asyncio.run(provider_drift())
    asyncio.run(project_drift())
    asyncio.run(knowledge_drift())
    asyncio.run(skill_drift())


def test_resumed_execution_never_transitions_running_back_to_starting() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("恢复完成。"), ProviderCompleted("stop", None)]
        )
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
        )

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert service.transitions == []
        assert service.boundary_persisted is True
        assert provider.start_count == 1
        assert store.completed is True
        assert store.run.status is RunStatus.SUCCEEDED

    asyncio.run(scenario())


def test_provider_model_mismatch_is_receipted_before_tool_execution() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ReceiptResumeStore(run)
        provider = MutatingAliasProvider(
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="call_model_mismatch_0001",
                    tool_name="project_read_file",
                    arguments={"file_id": "file-001"},
                ),
                ProviderCompleted("tool_calls", None, "late-added-model"),
            ]
        )
        provider.model = "requested-model"  # type: ignore[attr-defined]
        provider.accepted_reported_models = ()  # type: ignore[attr-defined]
        tool_executor = ResumeToolExecutor()
        tool_executor.definitions = (
            ProviderToolDefinition(
                name="project_read_file",
                version="1",
                description="Read one project file",
                parameters={
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}},
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            ),
        )
        service = ReceiptResumeService(store, provider, tool_executor)

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "agent_provider_model_mismatch", (
            store.last_failure
        )
        assert len(store.provider_turn_receipts) == 1
        assert (
            store.provider_turn_receipts[0].model_identity_status
            == "rejected_mismatch"
        )
        assert store.provider_turn_receipts[0].accepted_reported_models == ()
        assert service.receipt_counts_before_tools == []

    asyncio.run(scenario())


def test_legacy_provider_without_reported_model_is_receipted_as_unreported() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ReceiptResumeStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("兼容完成。"), ProviderCompleted("stop", None)]
        )
        provider.model = "requested-model"  # type: ignore[attr-defined]
        provider.accepted_reported_models = ()  # type: ignore[attr-defined]
        service = ReceiptResumeService(store, provider, ResumeToolExecutor())

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.SUCCEEDED
        assert len(store.provider_turn_receipts) == 1
        receipt = store.provider_turn_receipts[0]
        assert receipt.requested_model == "requested-model"
        assert receipt.reported_model is None
        assert receipt.model_identity_status == "unreported"

    asyncio.run(scenario())


def test_each_provider_turn_has_an_ordered_request_specific_receipt() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ReceiptResumeStore(run)
        provider = TurnScriptedProvider(
            [
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="call_receipt_turn_0001",
                        tool_name="project_read_file",
                        arguments={"file_id": "file-001"},
                    ),
                    ProviderCompleted("tool_calls", None, "requested-model"),
                ],
                [
                    ProviderTextDelta("两轮完成。"),
                    ProviderCompleted("stop", None, "requested-model"),
                ],
            ]
        )
        provider.model = "requested-model"  # type: ignore[attr-defined]
        provider.accepted_reported_models = ()  # type: ignore[attr-defined]
        tool_executor = ResumeToolExecutor()
        tool_executor.definitions = (
            ProviderToolDefinition(
                name="project_read_file",
                version="1",
                description="Read one project file",
                parameters={
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}},
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            ),
        )
        service = ReceiptResumeService(store, provider, tool_executor)

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.SUCCEEDED
        assert [
            receipt.turn_ordinal for receipt in store.provider_turn_receipts
        ] == [1, 2]
        assert all(
            receipt.model_identity_status == "exact_match"
            for receipt in store.provider_turn_receipts
        )
        assert (
            store.provider_turn_receipts[0].request_hash
            != store.provider_turn_receipts[1].request_hash
        )
        assert service.receipt_counts_before_tools == [1]
        assert len(provider.messages_by_turn) == 2
        assert len(provider.messages_by_turn[1]) > len(provider.messages_by_turn[0])

    asyncio.run(scenario())


def test_managed_provider_holds_project_kb_lease_through_completion() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        expected_knowledge_fingerprint = "d" * 64
        store = ReceiptResumeStore(
            run,
            knowledge_fingerprint=expected_knowledge_fingerprint,
        )
        project_root = Path("terminal-project-lock")
        store.database = SimpleNamespace(  # type: ignore[attr-defined]
            get_project_binding=lambda project_id: SimpleNamespace(
                root_path=project_root,
                project_id=project_id,
            )
        )
        release_provider = asyncio.Event()
        provider = ScriptedProvider(
            [ProviderTextDelta("lease 完成。"), ProviderCompleted("stop", None)],
            before_events=release_provider,
        )
        verifier = TrackingProjectKbVerifier()
        service = ReceiptResumeService(store, provider, ResumeToolExecutor())
        service.project_kb_service = verifier  # type: ignore[assignment]
        service.knowledge_fingerprint_probe = (
            lambda _project_id=None: expected_knowledge_fingerprint
        )
        project_locks = ProjectLockRegistry()
        service.project_locks = project_locks
        terminal_lock = project_locks.for_path(project_root)

        execution = asyncio.create_task(
            service._execute(run.run_id, asyncio.Event(), resumed=True)
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        assert await asyncio.to_thread(verifier.lease_active.wait, 2)
        assert store.completed is False
        await terminal_lock.acquire()

        try:
            release_provider.set()
            await asyncio.sleep(0.05)
            assert not execution.done()
            assert store.completed is False
        finally:
            terminal_lock.release()
        await asyncio.wait_for(execution, timeout=2)

        assert store.run.status is RunStatus.SUCCEEDED
        assert verifier.lease_released.is_set()
        assert not verifier.lease_active.is_set()
        assert verifier.expected_knowledge_fingerprints
        assert set(verifier.expected_knowledge_fingerprints) == {
            expected_knowledge_fingerprint
        }

    asyncio.run(scenario())


def test_project_kb_change_during_stream_receipts_remote_call_but_skips_tool() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ReceiptResumeStore(run)
        provider = ScriptedProvider(
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="call_stale_kb_0001",
                    tool_name="project_read_file",
                    arguments={"file_id": "file-001"},
                ),
                ProviderCompleted("tool_calls", None),
            ]
        )
        tool_executor = ResumeToolExecutor()
        tool_executor.definitions = (
            ProviderToolDefinition(
                name="project_read_file",
                version="1",
                description="Read one project file",
                parameters={
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}},
                    "required": ["file_id"],
                    "additionalProperties": False,
                },
            ),
        )
        verifier = SequenceProjectKbVerifier(fail_on_call=5)
        service = ReceiptResumeService(store, provider, tool_executor)
        service.project_kb_service = verifier  # type: ignore[assignment]

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert verifier.calls == 5
        assert len(store.provider_turn_receipts) == 1
        assert service.receipt_counts_before_tools == []
        assert store.completed is False
        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "project_kb_state_stale"
        assert store.last_failure["retryable"] is True

    asyncio.run(scenario())


def test_resumed_execution_rejects_executor_config_drift_before_workflow() -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        run = workflow_run(RunStatus.RUNNING).model_copy(
            update={
                "executor": provider.identity.model_copy(
                    update={"instance_id": f"provider-config-{'f' * 64}"}
                )
            }
        )
        store = ResumeExecutionStore(run)
        service = ResumeExecutionService(store, provider, ResumeToolExecutor())

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "workflow_executor_changed"
        assert service.boundary_persisted is False
        assert provider.start_count == 0

    asyncio.run(scenario())


def test_provider_cannot_call_unadvertised_managed_skill_after_workflow() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="call_unadvertised_skill_0001",
                    tool_name="skill_execute",
                    arguments={
                        "skill_id": "novel-creation-suite.project-audit",
                    },
                ),
                ProviderCompleted("tool_calls", None),
            ]
        )
        service = ResumeExecutionService(store, provider, ResumeToolExecutor())

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "agent_provider_tool_not_advertised"
        assert service.boundary_persisted is True
        assert provider.start_count == 1

    asyncio.run(scenario())


def test_provider_boundary_rechecks_project_and_knowledge_after_workflow() -> None:
    async def project_changes_during_workflow() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        tool_executor = ProjectRaceResumeToolExecutor()
        service = ResumeExecutionService(store, provider, tool_executor)

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "agent_tool_project_changed"
        assert tool_executor.validation_calls == 2
        assert service.boundary_persisted is False
        assert provider.start_count == 0

    async def knowledge_changes_during_workflow() -> None:
        expected = "d" * 64
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run, knowledge_fingerprint=expected)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        probe = SequenceKnowledgeProbe(expected, "e" * 64)
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
            knowledge_fingerprint_probe=probe,
        )

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "context_pack_source_changed"
        assert probe.project_ids == [PROJECT_ID, PROJECT_ID]
        assert service.boundary_persisted is False
        assert provider.start_count == 0

    async def project_kb_changes_during_workflow() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        verifier = SequenceProjectKbVerifier(fail_on_call=2)
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
            project_kb_service=verifier,
        )

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        assert verifier.calls == 2
        assert store.run.status is RunStatus.FAILED
        assert store.last_failure is not None
        assert store.last_failure["code"] == "project_kb_state_stale"
        assert store.last_failure["retryable"] is True
        assert service.boundary_persisted is False
        assert provider.start_count == 0

    asyncio.run(project_changes_during_workflow())
    asyncio.run(knowledge_changes_during_workflow())
    asyncio.run(project_kb_changes_during_workflow())


def test_resume_execution_rechecks_project_and_knowledge_after_claim() -> None:
    async def project_race() -> None:
        run = workflow_run()
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        tool_executor = ProjectRaceResumeToolExecutor()
        service = ResumeExecutionService(store, provider, tool_executor)
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        await service.resume_run(run.run_id, request)
        terminal = await wait_terminal(service, run.run_id)

        assert terminal.status is RunStatus.FAILED
        assert tool_executor.validation_calls == 2
        assert provider.start_count == 0
        assert store.last_failure is not None
        assert store.last_failure["code"] == "agent_tool_project_changed"
        await service.shutdown()

    async def knowledge_race() -> None:
        expected = "d" * 64
        run = workflow_run()
        store = ResumeExecutionStore(run, knowledge_fingerprint=expected)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        tool_executor = ResumeToolExecutor()
        probe = SequenceKnowledgeProbe(expected, "e" * 64)
        service = ResumeExecutionService(
            store,
            provider,
            tool_executor,
            knowledge_fingerprint_probe=probe,
        )
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        await service.resume_run(run.run_id, request)
        terminal = await wait_terminal(service, run.run_id)

        assert terminal.status is RunStatus.FAILED
        assert tool_executor.validation_calls == 2
        assert probe.project_ids == [PROJECT_ID, PROJECT_ID]
        assert provider.start_count == 0
        assert store.last_failure is not None
        assert store.last_failure["code"] == "context_pack_source_changed"
        await service.shutdown()

    async def project_kb_race() -> None:
        run = workflow_run()
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider(
            [ProviderTextDelta("不应到达"), ProviderCompleted("stop", None)]
        )
        verifier = SequenceProjectKbVerifier(fail_on_call=2)
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
            project_kb_service=verifier,
        )
        request = AgentRunResumeRequest(
            expected_workflow_plan_id=store.plan.workflow_plan_id,
            expected_plan_hash=store.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )

        await service.resume_run(run.run_id, request)
        terminal = await wait_terminal(service, run.run_id)

        assert verifier.calls == 2
        assert store.claim_calls == 1
        assert terminal.status is RunStatus.FAILED
        assert provider.start_count == 0
        assert store.last_failure is not None
        assert store.last_failure["code"] == "project_kb_state_stale"
        assert store.last_failure["retryable"] is True
        await service.shutdown()

    asyncio.run(project_race())
    asyncio.run(knowledge_race())
    asyncio.run(project_kb_race())


def test_graceful_shutdown_pauses_workflow_before_provider_boundary() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider([])
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
        )
        blocking_runner = BlockingResumedWorkflowRunner(store.plan)
        service.workflow_runner = blocking_runner
        service._launch(run.run_id, resumed=True)
        await asyncio.wait_for(blocking_runner.started.wait(), timeout=2)

        await service.shutdown()

        assert provider.start_count == 0
        assert blocking_runner.reconcile_calls == 1
        assert store.pause_calls == 1
        assert store.interrupt_calls == 0
        assert store.recover_calls == 1
        assert store.run.status is RunStatus.PAUSED

    asyncio.run(scenario())


def test_graceful_shutdown_interrupts_after_provider_boundary() -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ScriptedProvider([], block_after=True)
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
        )
        service._launch(run.run_id, resumed=True)
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        assert store.boundary_present is True

        await service.shutdown()

        assert service.workflow_runner.reconcile_calls == 0
        assert store.pause_calls == 0
        assert store.interrupt_calls == 1
        assert store.recover_calls == 1
        assert store.run.status is RunStatus.INTERRUPTED

    asyncio.run(scenario())


@pytest.mark.parametrize("translated_error", ["cancelled", "provider_error"])
def test_shutdown_translation_cannot_hide_uncertain_provider_boundary(
    translated_error: str,
) -> None:
    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        provider = ShutdownTranslatingProvider(translated_error)
        service = ResumeExecutionService(
            store,
            provider,
            ResumeToolExecutor(),
        )
        service._launch(run.run_id, resumed=True)
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        assert store.boundary_present is True

        await service.shutdown()

        assert store.pause_calls == 0
        assert store.interrupt_calls == 1
        assert store.run.status is RunStatus.INTERRUPTED

    asyncio.run(scenario())
