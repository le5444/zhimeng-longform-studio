from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderCancelled,
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_models import AgentRunResumeRequest
from zhimeng_runtime.agent_service import (
    MAX_CONSECUTIVE_STREAM_RESUMES,
    MAX_TURN_STREAM_RETRIES,
    _SESSION_TITLE_PROMPT,
    AgentService,
    AgentUnavailableError,
    ToolExecutionResult,
    _author_facing_provider_failure,
)
from zhimeng_runtime.project_kb_models import ProjectKbRebuildRequest, ProjectKbStatus
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_reader import ProjectReader
from zhimeng_runtime.context_models import canonical_json_hash
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
    MessageUsage,
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


def test_quota_403_is_not_reported_as_invalid_api_key() -> None:
    error = AgentProviderError(
        "HTTP 403", code="agent_provider_http_error", retryable=False,
        details={"upstream_status": 403,
                 "upstream_excerpt": '{"error":{"code":"insufficient_user_quota"}}'},
    )
    message = _author_facing_provider_failure(error)
    assert "额度不足" in message
    assert "密钥被拒" not in message


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


def _is_session_naming_request(messages: Sequence[Mapping[str, object]]) -> bool:
    """第一轮收尾后服务会额外打一次「给这段对话起名字」的请求。

    它不是对话回合：不计进 start_count / messages / messages_by_turn 这些
    「对话向 provider 要了什么」的账本，只单独记 naming_request_count，
    谁在起名上出问题可以查到它。
    """
    return any(
        message.get("role") == "system" and message.get("content") == _SESSION_TITLE_PROMPT
        for message in messages
    )


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
        self.naming_request_count = 0
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
        if _is_session_naming_request(messages):
            self.naming_request_count += 1
            yield ProviderTextDelta("测试会话起名")
            yield ProviderCompleted("stop", None)
            return
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
        if _is_session_naming_request(messages):
            self.naming_request_count += 1
            yield ProviderTextDelta("测试会话起名")
            yield ProviderCompleted("stop", None)
            return
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


class TruncationThenContinuationProvider(TurnScriptedProvider):
    """第一轮正常吐字后被 token 上限掐断（provider 层 raise），
    第二轮应当收到截断前文 + 续写指令，并把后半段补完。"""

    def __init__(self, turns: list[list[ProviderEvent]]) -> None:
        super().__init__(turns)

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        if _is_session_naming_request(messages):
            yield ProviderTextDelta("测试会话起名")
            yield ProviderCompleted("stop", None)
            return
        self.start_count += 1
        self.started.set()
        self.messages_by_turn.append([dict(message) for message in messages])
        if self.start_count == 1:
            yield ProviderTextDelta("故事从旧港开始，")
            raise AgentProviderError(
                "Provider stopped because the output token limit was reached",
                code="agent_provider_output_truncated",
                retryable=True,
            )
        continuation_prompt = [
            dict(message)
            for message in messages
            if message.get("role") == "user" and "被截断" in str(message.get("content", ""))
        ]
        assert continuation_prompt, "截断后续写指令没有回喂给模型"
        assistant_prefix = [
            dict(message)
            for message in messages
            if message.get("role") == "assistant"
            and "故事从旧港开始" in str(message.get("content", ""))
        ]
        assert assistant_prefix, "截断前文没有回喂给模型"
        for event in self.turns[self.start_count - 1]:
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event


def test_output_truncation_continues_instead_of_failing_the_run(tmp_path: Path) -> None:
    # 输出打满 token 上限时，已生成的字都在作者屏幕上；整轮报废等于白写。
    # 现在把前半段落成完整消息、自动续写后半段，Run 照常成功。
    async def scenario() -> None:
        provider = TruncationThenContinuationProvider(
            [
                [],
                [
                    ProviderTextDelta("然后风向变了，故事继续。"),
                    ProviderCompleted("stop", None),
                ],
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.start_count == 2
        truncated = [
            payload
            for kind, payload in lifecycle_events(service, run.run_id)
            if kind == "model_turn_output_truncated"
        ]
        assert len(truncated) == 1
        assert truncated[0]["continued"] is True
        assistant_messages = [
            message
            for message in service.store.list_messages(session.session_id)
            if message.role == "assistant"
        ]
        assert [message.content for message in assistant_messages] == [
            "故事从旧港开始，",
            "然后风向变了，故事继续。",
        ]
        await service.shutdown()

    asyncio.run(scenario())


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

    @staticmethod
    def request_hash(tool_name: str, tool_version: str, arguments: dict[str, object]) -> str:
        del tool_name, tool_version, arguments
        return "a" * 64

    async def execute(
        self,
        run: Run,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        request_hash: str,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        del run, tool_call_id, tool_name, arguments, request_hash, cancel_event
        return AgentToolExecution(output={"ok": True})

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
    def get_run_goal(self, run_id: str):
        return None

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
        self.tool_calls: dict[str, StoredToolCall] = {}
        self.transcript: list[dict[str, object]] = []

    def save_run_transcript(self, run_id: str, messages: list[dict[str, object]]) -> None:
        assert run_id == self.run.run_id
        self.transcript = list(messages)

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

    def begin_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, object],
        request_hash: str,
        idempotency_key: str | None = None,
    ) -> tuple[StoredToolCall, bool]:
        assert run_id == self.run.run_id
        existing = self.tool_calls.get(tool_call_id)
        if existing is not None:
            return existing, True
        stored = StoredToolCall(
            tool_call_id=tool_call_id,
            run_id=run_id,
            tool_name=tool_name,
            tool_version=tool_version,
            status="running",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            arguments=arguments,
            output=None,
            error=None,
            call_activity_id=None,
            result_activity_id=None,
            result_hash=None,
            duration_ms=None,
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        self.tool_calls[tool_call_id] = stored
        return stored, False

    def finish_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        status: str,
        output: object,
        error: ErrorEnvelope | None,
        duration_ms: int | None,
        change_proposal: object = None,
        skill_call: object = None,
        artifacts: tuple[object, ...] = (),
    ) -> tuple[StoredToolCall, bool]:
        del change_proposal, skill_call, artifacts
        assert run_id == self.run.run_id
        stored = self.tool_calls[tool_call_id]
        finished = replace(
            stored,
            status=status,
            output=output,
            error=error,
            result_hash="4" * 64,
            duration_ms=duration_ms,
            finished_at=datetime.now(UTC),
        )
        self.tool_calls[tool_call_id] = finished
        return finished, True

    def get_tool_call(self, tool_call_id: str) -> StoredToolCall:
        return self.tool_calls[tool_call_id]


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

    async def _finalize_prepared_tool_call(
        self,
        run: Run,
        prepared,
        cancel_event: asyncio.Event,
    ) -> ToolExecutionResult:
        # 只读工具如今走并行段（begin/finalize 拆分），不再经过
        # _execute_tool_call；在执行前记录回执数，语义与旧覆盖一致。
        self.receipt_counts_before_tools.append(
            len(self.store.provider_turn_receipts)  # type: ignore[attr-defined]
        )
        return await super()._finalize_prepared_tool_call(run, prepared, cancel_event)


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


def test_stale_project_kb_is_rebuilt_automatically(tmp_path: Path) -> None:
    """作者在外部编辑器手改一个字 → KB 落后一代。

    以前这会让整轮对话被 project_kb_state_stale 拒掉，逼作者先去点重建；
    现在校验入口先自动重建一次（inspect + 推进绑定 + rebuild），校验照常通过。
    """
    async def scenario() -> None:
        library = tmp_path / "library"
        project = library / "novel"
        volume = project / "05_正文内容" / "第一卷"
        volume.mkdir(parents=True)
        chapter = volume / "CH001_正文.md"
        chapter.write_text("第一章。\n", encoding="utf-8")
        reader = ProjectReader((library,))
        snapshot = reader.inspect(project)
        identity = snapshot["project"]
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        binding = database.sync_project_binding(
            project_id=str(identity["project_id"]),
            root_path=str(identity["root_path"]),
            display_name=str(identity["display_name"]),
            fingerprint=str(identity["fingerprint"]),
        )
        kb = ProjectKbService(database, reader, tmp_path / "project-kb")
        kb.store.ensure_project_scope(binding.project_id)
        initial = kb.store.project_state_snapshot(binding.project_id)
        kb.rebuild(
            binding.project_id,
            ProjectKbRebuildRequest(
                expected_project_revision=initial.project.revision,
                expected_project_fingerprint=initial.project.fingerprint,
                expected_scope_state_revision=initial.scope.state_revision,
                expected_knowledge_fingerprint=canonical_json_hash(
                    initial.fingerprint_material
                ),
            ),
        )
        current = kb.get_state(binding.project_id)
        assert current.status is ProjectKbStatus.CURRENT
        assert current.pointer is not None and current.manifest is not None
        target = ContextPackKnowledgeTarget(
            context_pack_id="context-pack-kb-auto",
            pack_hash="0" * 64,
            knowledge_fingerprint=None,
            project_id=binding.project_id,
            project_kb_state_id=current.pointer.state_id,
            project_kb_manifest_hash=current.pointer.manifest_hash,
        )
        service = AgentService(AgentStore(database), None, project_kb_service=kb)

        # 作者在外面改了一个字：旧校验直接拒（stale）。
        chapter.write_text("第一章改了一个字。\n", encoding="utf-8")
        with pytest.raises(AgentContextPackConflict) as stale_error:
            await asyncio.to_thread(
                service._require_current_project_kb_target_sync, target
            )
        assert stale_error.value.code == "project_kb_state_stale"

        # 新校验：自动重建后照常通过，KB 回到 CURRENT。
        await service._require_current_project_kb_target(target)
        assert kb.get_state(binding.project_id).status is ProjectKbStatus.CURRENT
        await service.shutdown()

    asyncio.run(scenario())


def test_chat_profiles_skip_the_managed_prose_workflow(tmp_path: Path) -> None:
    class ProfiledContextStore:
        def __init__(self, profile: str) -> None:
            self.profile = profile

        def get_nlcl_route(self, context_pack_id: str) -> SimpleNamespace:
            assert context_pack_id == "context-pack-chat"
            return SimpleNamespace(profile=self.profile)

    async def scenario() -> None:
        service = build_service(tmp_path, None)
        service.context_store = ProfiledContextStore("chat")  # type: ignore[assignment]
        assert await service._has_managed_workflow("context-pack-chat") is False
        service.context_store = ProfiledContextStore("chat_isolated")  # type: ignore[assignment]
        assert await service._has_managed_workflow("context-pack-chat") is False
        service.context_store = ProfiledContextStore("chat_book_setup")  # type: ignore[assignment]
        assert await service._has_managed_workflow("context-pack-chat") is False
        # 受管流水线已退役：哪怕资料包 profile 是 chapter 也不再走它。
        service.context_store = ProfiledContextStore("chapter")  # type: ignore[assignment]
        assert await service._has_managed_workflow("context-pack-chat") is False

    asyncio.run(scenario())


def test_writer_targets_follow_explicitly_selected_project_file() -> None:
    character_file_id = "file-" + "d" * 32
    snapshot = SimpleNamespace(
        files=(
            SimpleNamespace(
                file_id=character_file_id,
                relative_path="03_人物设定/主角档案.md",
                sha256="e" * 64,
                kind="document",
            ),
        ),
    )

    targets = AgentService._writer_target_files(
        snapshot,
        mode="author_review_change_proposal",
        active_chapter_id=None,
        manual_file_ids=(character_file_id,),
    )

    assert len(targets) == 1
    assert targets[0].file_id == character_file_id
    assert targets[0].content_role == "project_file"
    assert targets[0].relative_path == "03_人物设定/主角档案.md"


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


def test_agent_service_persists_reasoning_without_mixing_it_into_answer_or_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [
                ProviderReasoningDelta("先核对人物动机，再收紧开头节奏。"),
                ProviderTextDelta("已经按人物动机改好开头。"),
                ProviderCompleted("stop", None),
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        messages = service.store.list_messages(session.session_id)
        assert messages[-1].content == "已经按人物动机改好开头。"
        assert "先核对人物动机" not in messages[-1].content
        assert provider.messages[-1]["content"] == "保留线索，收紧开头。"

        reasoning = [
            item.root
            for item in service.store.list_activities(run.run_id)
            if item.root.kind.value == "reasoning"
        ]
        assert [item.payload.phase for item in reasoning] == ["delta", "completed"]
        assert reasoning[0].payload.block_id == reasoning[1].payload.block_id
        assert reasoning[1].payload.summary == "先核对人物动机，再收紧开头节奏。"
        assert reasoning[1].payload.content == "先核对人物动机，再收紧开头节奏。"
        assert reasoning[1].payload.visibility == "full"
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_splits_relay_think_tags_before_persisting_answer(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [
                ProviderTextDelta("<think>先判断作者只是提问，不需要修改正文。"),
                ProviderTextDelta("</think><think>再把结论压成一句话。</think>\n"),
                ProviderTextDelta("最怕开篇堆满设定，让读者进不了故事。"),
                ProviderCompleted("stop", None),
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        messages = service.store.list_messages(session.session_id)
        assert messages[-1].content == "最怕开篇堆满设定，让读者进不了故事。"
        assert "<think>" not in messages[-1].content

        reasoning = [
            item.root
            for item in service.store.list_activities(run.run_id)
            if item.root.kind.value == "reasoning"
        ]
        assert len(reasoning) == 1
        assert reasoning[0].payload.content == (
            "先判断作者只是提问，不需要修改正文。\n\n"
            "再把结论压成一句话。"
        )
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
    # 每次都吐一点就断：自动接着写连着 3 次（MAX_CONSECUTIVE_STREAM_RESUMES）还是断，照旧整轮失败，不无限续下去。
    async def scenario() -> None:
        provider = ScriptedProvider([ProviderTextDelta("半段")])
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)
        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_provider_incomplete_stream"
        assert provider.start_count == 1 + MAX_CONSECUTIVE_STREAM_RESUMES
        resumed = [kind for kind, _ in lifecycle_events(service, run.run_id) if kind == "model_turn_stream_resumed"]
        assert len(resumed) == MAX_CONSECUTIVE_STREAM_RESUMES
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_retries_incomplete_turn_when_no_text_was_streamed(
    tmp_path: Path,
) -> None:
    # 中转站在长生成中途掐断流时，这一轮作者屏幕上还什么都没有
    # （只有公开思考内容）：保留中断块并自动重发，Run 照常成功，回答不重复。
    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                [ProviderReasoningDelta("还在想，流就断了。")],
                [
                    ProviderTextDelta("重发后的完整回答。"),
                    ProviderCompleted("stop", None),
                ],
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.start_count == 2
        messages = service.store.list_messages(session.session_id)
        assert messages[-1].content == "重发后的完整回答。"
        # 断流那次的公开思考保留，末态清楚地标成中断。
        reasoning = [
            item.root
            for item in service.store.list_activities(run.run_id)
            if item.root.kind.value == "reasoning"
        ]
        assert [item.payload.phase for item in reasoning] == ["delta", "interrupted"]
        assert reasoning[-1].payload.content == "还在想，流就断了。"
        retries = [
            payload
            for kind, payload in lifecycle_events(service, run.run_id)
            if kind == "model_turn_stream_retry"
        ]
        assert len(retries) == 1
        assert retries[0]["error_code"] == "agent_provider_incomplete_stream"
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_resumes_incomplete_turn_after_text_was_streamed(
    tmp_path: Path,
) -> None:
    # 已经吐字后断流：重发会把已显示的文字再吐一遍，所以不重发；已写的留着，
    # 让模型从断点接着写（9-27 起，照 Qwen Code #7832；以前整轮失败、作者点「重发这条」）。
    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                [ProviderTextDelta("半段")],
                [ProviderTextDelta("接上的后半段。"), ProviderCompleted("stop", None)],
            ]
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.start_count == 2
        assert [
            message.content
            for message in service.store.list_messages(session.session_id)
            if message.role == "assistant"
        ] == ["半段", "接上的后半段。"]
        await service.shutdown()

    asyncio.run(scenario())


def test_agent_service_turn_stream_retry_exhaustion_keeps_failure(
    tmp_path: Path,
) -> None:
    # 连续断流打满重试次数后如实失败，不无限重发。
    async def scenario() -> None:
        provider = TurnScriptedProvider([[], [], []])
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_provider_incomplete_stream"
        assert provider.start_count == 1 + MAX_TURN_STREAM_RETRIES
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
    # This case has a readable project with no additional instructions. A
    # missing directory correctly adds an unavailable-context message instead.
    (tmp_path / "novel").mkdir()

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
        # 资料包逐字节送出；前面只多一段环境事实（纠偏指令 3.2），不进资料包、不改它的哈希。
        assert provider.messages[0]["role"] == "system"
        assert provider.messages[0]["content"].startswith("系统：")
        assert provider.messages[1:] == [
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
    """未通告的工具不再立刻终止 Run：失败结果回给模型自我纠正；
    连续失败多少次都不判死、也不切收尾轮（2026-09-24 起没有连败上限，
    停止键归作者）。技能执行器全程不被触碰。"""

    repeats = 8

    async def scenario() -> None:
        run = workflow_run(RunStatus.RUNNING)
        store = ResumeExecutionStore(run)
        bad_turn = [
            ProviderToolCall(
                index=0,
                tool_call_id="call_unadvertised_skill_0001",
                tool_name="skill_execute",
                arguments={
                    "skill_id": "novel-creation-suite.project-audit",
                },
            ),
            # 中转站把 finish_reason 报成 stop 也不影响拒绝路径。
            ProviderCompleted("stop", None),
        ]
        provider = TurnScriptedProvider(
            [list(bad_turn) for _ in range(repeats)]
            + [
                [
                    ProviderTextDelta("这一步走不通，已经试过的方法都告诉作者。"),
                    ProviderCompleted("stop", None),
                ]
            ]
        )
        service = ResumeExecutionService(store, provider, ResumeToolExecutor())

        await service._execute(run.run_id, asyncio.Event(), resumed=True)

        # 不再整轮失败：8 次拒绝回喂之后模型自己给出文本，Run 成功收尾。
        assert store.run.status is RunStatus.SUCCEEDED
        assert store.last_failure is None
        assert service.boundary_persisted is True
        assert provider.start_count == repeats + 1
        # 最后一轮带着全量历史：8 次拒绝回喂都在里面；没有任何「收尾轮」指令。
        closing = provider.messages_by_turn[-1]
        rejected = [
            message
            for message in closing
            if message.get("role") == "tool"
            and "agent_tool_not_advertised" in str(message.get("content"))
        ]
        assert len(rejected) == repeats
        assert not any(
            message.get("role") == "user"
            and "用中文向作者交代" in str(message.get("content"))
            for message in closing
        )

    asyncio.run(scenario())


def test_invalid_tool_arguments_are_fed_back_instead_of_failing_the_run(
    tmp_path: Path,
) -> None:
    """模型写出的工具参数不是合法 JSON：不再整轮作废。这次调用以失败结果
    回喂（请重发这次调用），模型下一轮重发或照常回答，Run 成功收尾。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="call_bad_arguments_0001",
                        tool_name="project_read_file",
                        arguments={},
                        arguments_error=(
                            "Provider tool arguments are not strict JSON"
                            "（Expecting property name @ 1：…{…）"
                        ),
                    ),
                    ProviderCompleted("tool_calls", None),
                ],
                [
                    ProviderTextDelta("重发之前先给出结论。"),
                    ProviderCompleted("stop", None),
                ],
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=ResumeToolExecutor())
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.start_count == 2
        fed_back = [
            message
            for message in provider.messages_by_turn[1]
            if message.get("role") == "tool"
        ]
        assert len(fed_back) == 1
        assert "请重发这次调用" in str(fed_back[0].get("content"))
        assert "agent_provider_tool_arguments_invalid" in str(fed_back[0].get("content"))
        await service.shutdown()

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


def test_rejected_tool_result_message_reports_a_failed_tool_result() -> None:
    message = AgentService._rejected_tool_result_message(
        "call_reject_0001",
        code="agent_tool_not_advertised",
        message="这个工具在本回合不可用",
    )
    assert message["role"] == "tool"
    assert message["tool_call_id"] == "call_reject_0001"
    payload = json.loads(str(message["content"]))
    assert payload["status"] == "failed"
    assert payload["error"]["error"]["code"] == "agent_tool_not_advertised"
    assert payload["error"]["error"]["retryable"] is False


LONG_NOTE_OUTPUT = {"text": "很长的结果" * 400}


class ImmediateNotesToolExecutor:
    """立即返回长诊断结果的工具替身。"""

    def __init__(self, output: dict[str, object] | None = None) -> None:
        self.definitions = (
            ProviderToolDefinition(
                name="project_notes",
                version="1",
                description="Read a long diagnostic note",
                parameters={
                    "type": "object",
                    "properties": {"note_id": {"type": "string"}},
                    "required": ["note_id"],
                    "additionalProperties": False,
                },
            ),
        )
        self.output = LONG_NOTE_OUTPUT if output is None else output
        self.calls = 0

    @staticmethod
    def request_hash(tool_name: str, tool_version: str, arguments: dict[str, object]) -> str:
        del tool_name, tool_version, arguments
        return "a" * 64

    async def execute(self, *args, **kwargs) -> AgentToolExecution:
        del args, kwargs
        self.calls += 1
        return AgentToolExecution(output=self.output)


class ToolAwareTurnProvider(TurnScriptedProvider):
    def __init__(
        self,
        turns: list[list[ProviderEvent]],
        *,
        clock=None,
        advance_after_turn: float = 0.0,
    ) -> None:
        super().__init__(turns)
        self.tools_by_turn: list[tuple[str, ...]] = []
        self.clock = clock
        self.advance_after_turn = advance_after_turn

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if _is_session_naming_request(messages):
            async for event in super().stream_chat(
                messages,
                cancel_event=cancel_event,
                tools=(),
            ):
                yield event
            return
        self.tools_by_turn.append(tuple(definition.name for definition in tools))
        async for event in super().stream_chat(
            messages,
            cancel_event=cancel_event,
            tools=tools,
        ):
            yield event
        if self.clock is not None and self.advance_after_turn:
            self.clock.advance(self.advance_after_turn)


class FakeMonotonicClock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def notes_loop_turns(
    tool_turns: int,
    final_text: str,
    *,
    usage: MessageUsage | None = None,
) -> list[list[ProviderEvent]]:
    turns: list[list[ProviderEvent]] = []
    for index in range(tool_turns):
        turns.append(
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id=f"call_notes_{index:04d}xxxx",
                    tool_name="project_notes",
                    arguments={"note_id": f"note-{index}"},
                ),
                ProviderCompleted("tool_calls", usage),
            ]
        )
    turns.append([ProviderTextDelta(final_text), ProviderCompleted("stop", usage)])
    return turns


def lifecycle_events(
    service: AgentService, run_id: str
) -> list[tuple[str, dict[str, object]]]:
    with service.store.database.connection() as connection:
        rows = connection.execute(
            """
            SELECT event_type, payload_json FROM run_event
            WHERE run_id = ? AND event_type != 'activity'
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
    return [
        (str(row["event_type"]), json.loads(str(row["payload_json"])))
        for row in rows
    ]


def unfolded_note_results(messages: list[dict[str, object]]) -> int:
    return sum(
        1
        for message in messages
        if message.get("role") == "tool"
        and isinstance(message.get("content"), str)
        and not str(message["content"]).startswith('{"folded"')
    )


def test_each_model_turn_records_how_much_of_the_previous_request_it_kept(
    tmp_path: Path,
) -> None:
    """缓存诊断（cache_shape.py，9-27）：一轮里来回调工具，每次请求都是上一次原样加在后面，记成 append_only。"""

    async def scenario() -> None:
        provider = ToolAwareTurnProvider(notes_loop_turns(3, "三轮查完。"))
        service = build_service(
            tmp_path, provider, tool_executor=ImmediateNotesToolExecutor()
        )
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        shapes = [
            payload["cache_shape"]
            for kind, payload in lifecycle_events(service, run.run_id)
            if kind == "model_turn_started"
        ]
        assert len(shapes) == 4
        assert shapes[0]["reason"] == "first_request"
        assert [shape["reason"] for shape in shapes[1:]] == ["append_only"] * 3
        assert all(
            later["kept_messages"] == earlier["messages"]
            for earlier, later in zip(shapes, shapes[1:])
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_model_turn_events_and_prefix_stability_across_turns(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ToolAwareTurnProvider(notes_loop_turns(6, "六轮查完，结论不变。"))
        service = build_service(
            tmp_path, provider, tool_executor=ImmediateNotesToolExecutor()
        )
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        events = lifecycle_events(service, run.run_id)
        started = [payload for kind, payload in events if kind == "model_turn_started"]
        completed = [
            payload for kind, payload in events if kind == "model_turn_completed"
        ]
        assert [payload["turn"] for payload in started] == [1, 2, 3, 4, 5, 6, 7]
        assert [payload["turn"] for payload in completed] == [1, 2, 3, 4, 5, 6, 7]
        assert all(payload["wrap_up"] is False for payload in started)
        assert completed[-1]["finish_reason"] == "stop"
        assert completed[0]["tool_calls"] == 1

        # 不超预算就不折叠：消息序列逐字节不变，前缀稳定（上游提示缓存的前提）。
        assert not [
            payload for kind, payload in events if kind == "run_context_compaction"
        ]
        turns = provider.messages_by_turn
        head = json.dumps(turns[0], ensure_ascii=False, sort_keys=True)
        for later in turns[1:]:
            # 每一轮的头部与第 1 轮逐字节相同（前面只追加、不改写）
            assert json.dumps(later[: len(turns[0])], ensure_ascii=False, sort_keys=True) == head
        # 工具定义按名字排序后发出，每轮相同
        tools_turn_1 = provider.tools_by_turn[0]
        assert list(tools_turn_1) == sorted(tools_turn_1)
        for later_tools in provider.tools_by_turn[1:]:
            assert later_tools == tools_turn_1

        # SSE 活动流只捞 event_type='activity'，新事件类型不混进去。
        assert len(service.store.list_run_events(run.run_id)) == len(
            service.store.list_activities(run.run_id)
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_normal_budget_never_touches_the_message_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ToolAwareTurnProvider(notes_loop_turns(6, "默认预算收尾。"))
        service = build_service(
            tmp_path, provider, tool_executor=ImmediateNotesToolExecutor()
        )
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        events = lifecycle_events(service, run.run_id)
        assert not [
            payload
            for kind, payload in events
            if kind == "run_context_compaction"
        ]
        # 不超预算时完全不折叠：6 条工具结果全部完整保留。
        assert unfolded_note_results(provider.messages_by_turn[-1]) == 6
        await service.shutdown()

    asyncio.run(scenario())


def test_unknown_exception_fails_the_run_as_a_program_error_not_retryable(
    tmp_path: Path,
) -> None:
    """A bug inside the runtime is reported as a program error, never as retryable (review 2.7)."""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="call_boom_0001",
                        tool_name="project_read_file",
                        arguments={"file_id": "file-" + "a" * 32},
                    ),
                    ProviderCompleted("tool_calls", None),
                ]
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=ResumeToolExecutor())

        async def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        service._execute_tool_call = boom  # type: ignore[method-assign]
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)
        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_execution_failed"
        assert run.error.error.retryable is False
        assert run.error.error.message.startswith("\u7a0b\u5e8f\u51fa\u9519\u4e86")
        await service.shutdown()

    asyncio.run(scenario())
