from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_provider import AgentProviderCancelled, AgentProviderError
from zhimeng_runtime.agent_store import AgentStoreNotFound, StoredToolCall
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextCreateRequest,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import StoredContextPack
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
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.nlcl_route_execution_models import (
    NlclRouteExecutionReceipt,
    build_nlcl_route_execution_receipt,
)
from zhimeng_runtime.prewrite_models import build_prewrite_operation_package
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_workflow_step_checkpoint,
)
from zhimeng_runtime.workflow_runner import (
    CREATION_HANDOFF_INPUT_SCHEMA,
    PREWRITE_GATE_INPUT_SCHEMA,
    PROJECT_AUDIT_INPUT_SCHEMA,
    WorkflowRunner,
    workflow_provider_tool_call_id,
    workflow_runtime_tool_call_id,
)


PROJECT_ID = "project-0123456789abcdef01234567"
PROJECT_FINGERPRINT = "a" * 64
CREATED_AT = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)


def _run() -> Run:
    executor = ExecutorIdentity(
        executor_id="executor.workflow-runner-test",
        kind=ExecutorKind.EMBEDDED,
        implementation="workflow-runner-test",
        version="1",
        protocol_version="1",
        instance_id=None,
    )
    return Run(
        schema="zhimeng.execution-run.v1",
        run_id="run_workflow_runner_00000001",
        session_id="session_workflow_runner_0001",
        ordinal=1,
        status=RunStatus.RUNNING,
        objective="Audit the project before the next writing task.",
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        started_at=CREATED_AT,
        completed_at=None,
        executor=executor,
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.SKILLS],
            extensions={},
        ),
        project_revision=ProjectRevision(
            project_id=PROJECT_ID,
            revision=1,
            fingerprint=PROJECT_FINGERPRINT,
            observed_at=CREATED_AT,
        ),
        correlation_id="correlation_workflow_runner_01",
        causation_id=None,
        last_activity_sequence=0,
        checkpoint_id=None,
        error=None,
    )


class FakeSkillExecutor:
    def __init__(self) -> None:
        self.skills = {
            "novel-creation-suite.project-audit": self._skill(
                "novel-creation-suite.project-audit",
                "2026.07.20-m3a.1",
                PROJECT_AUDIT_INPUT_SCHEMA,
                "https://zhimeng.local/contracts/creation-project-audit-result.schema.json",
                "1",
            ),
            "novel-creation-suite.prewrite-gate": self._skill(
                "novel-creation-suite.prewrite-gate",
                "2026.07.20-m3e.1",
                PREWRITE_GATE_INPUT_SCHEMA,
                "https://zhimeng.local/contracts/creation-prewrite-gate-result.schema.json",
                "2",
            ),
            "novel-creation-suite.creation-handoff": self._skill(
                "novel-creation-suite.creation-handoff",
                "2026.07.21-m3j.1",
                CREATION_HANDOFF_INPUT_SCHEMA,
                "https://zhimeng.local/contracts/creation-handoff-result.schema.json",
                "3",
            ),
        }

    @staticmethod
    def _skill(
        skill_id: str,
        version: str,
        input_schema: str,
        output_schema: str,
        digit: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            skill_version_id=f"skill-version-{digit * 32}",
            manifest=SimpleNamespace(
                id=skill_id,
                version=version,
                input_schema=input_schema,
                output_schema=output_schema,
            ),
            manifest_fingerprint=digit * 64,
            package_fingerprint=str(int(digit) + 2) * 64,
        )

    def active_skill(self, project_id: str, skill_id: str) -> SimpleNamespace:
        assert project_id == PROJECT_ID
        return self.skills[skill_id]


class FakeContextStore:
    def __init__(self, run: Run) -> None:
        self.pack = compile_context_pack(
            context_pack_id="context-pack-workflow-runner",
            session_id=run.session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=ContextCreateRequest(
                objective=run.objective,
                profile="chapter",
                active_chapter_id="CH001",
                expected_project_revision=1,
                expected_project_fingerprint=PROJECT_FINGERPRINT,
                project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                project_kb_manifest_hash="b" * 64,
            ),
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            knowledge_fingerprint=None,
            created_at=CREATED_AT,
        )
        self.route = build_nlcl_route_receipt(
            self.pack,
            source=NlclRouteSource.UNAVAILABLE,
        )
        self.operation_package = build_prewrite_operation_package(
            self.pack,
            self.route,
        )
        self.store: FakeAgentStore | None = None
        self.receipts: dict[int, NlclRouteExecutionReceipt] = {}
        self.reconcile_calls: list[int] = []

    def get_run_pack(self, run_id: str) -> StoredContextPack:
        return StoredContextPack(pack=self.pack, consumed_by_run_id=run_id)

    def get_nlcl_route(self, context_pack_id: str):
        assert context_pack_id == self.pack.context_pack_id
        return self.route

    def get_prewrite_operation_package(self, context_pack_id: str):
        assert context_pack_id == self.pack.context_pack_id
        return self.operation_package

    def get_route_execution(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int,
    ) -> NlclRouteExecutionReceipt | None:
        assert context_pack_id == self.pack.context_pack_id
        return self.receipts.get(call_ordinal)

    def reconcile_route_execution(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int,
        run_id: str,
        tool_call_id: str,
    ) -> NlclRouteExecutionReceipt:
        assert context_pack_id == self.pack.context_pack_id
        self.reconcile_calls.append(call_ordinal)
        existing = self.receipts.get(call_ordinal)
        if existing is not None:
            return existing
        assert self.store is not None
        tool_call = self.store.get_tool_call(tool_call_id)
        evidence = self.store.evidence[tool_call_id]
        receipt = build_nlcl_route_execution_receipt(
            self.route,
            self.route.chapter_call_list[call_ordinal - 1],
            run_id=run_id,
            tool_call_id=tool_call_id,
            skill_call_id=evidence.skill_call_id,
            skill_receipt_id=evidence.skill_receipt_id,
            skill_receipt_hash=evidence.skill_receipt_hash,
            execution_status=tool_call.status,
            domain_status=(
                evidence.domain_status if tool_call.status == "succeeded" else None
            ),
            output_hash=canonical_json_hash(tool_call.output),
            tool_result_hash=str(tool_call.result_hash),
            created_at=tool_call.finished_at or CREATED_AT,
        )
        self.receipts[call_ordinal] = receipt
        return receipt


class FakeAgentStore:
    def __init__(self, context: FakeContextStore) -> None:
        self.context = context
        context.store = self
        self.plan: WorkflowPlan | None = None
        self.create_plan_calls = 0
        self.checkpoints: list[WorkflowStepCheckpoint | ProviderIoBoundary] = []
        self.tool_calls: dict[str, StoredToolCall] = {}
        self.evidence: dict[str, SimpleNamespace] = {}

    def create_workflow_plan(self, plan: WorkflowPlan) -> WorkflowPlan:
        self.create_plan_calls += 1
        if self.plan is None:
            self.plan = plan
        else:
            assert self.plan == plan
        return self.plan

    def get_workflow_plan(self, run_id: str) -> WorkflowPlan:
        if self.plan is None or self.plan.run_id != run_id:
            raise AgentStoreNotFound("Workflow Plan was not found")
        return self.plan

    def list_workflow_checkpoints(
        self,
        run_id: str,
    ) -> list[WorkflowStepCheckpoint | ProviderIoBoundary]:
        assert self.plan is not None and self.plan.run_id == run_id
        return list(self.checkpoints)

    def append_workflow_step_checkpoint(
        self,
        checkpoint: WorkflowStepCheckpoint,
        *,
        expected_previous_checkpoint_hash: str | None,
    ) -> WorkflowStepCheckpoint:
        previous = self.checkpoints[-1] if self.checkpoints else None
        assert not isinstance(previous, ProviderIoBoundary)
        actual = previous.checkpoint_hash if previous is not None else None
        assert actual == expected_previous_checkpoint_hash
        existing = next(
            (
                item
                for item in self.checkpoints
                if isinstance(item, WorkflowStepCheckpoint)
                and item.checkpoint_id == checkpoint.checkpoint_id
            ),
            None,
        )
        if existing is not None:
            assert existing == checkpoint
            return existing
        self.checkpoints.append(checkpoint)
        return checkpoint

    def get_tool_call(self, tool_call_id: str) -> StoredToolCall:
        try:
            return self.tool_calls[tool_call_id]
        except KeyError as exc:
            raise AgentStoreNotFound("ToolCall was not found") from exc

    def build_workflow_step_checkpoint_from_execution(
        self,
        plan: WorkflowPlan,
        *,
        step_ordinal: int,
        provider_tool_call_id: str | None,
        previous_checkpoint: WorkflowStepCheckpoint | None,
    ) -> WorkflowStepCheckpoint:
        assert provider_tool_call_id is not None
        tool_call_id = workflow_runtime_tool_call_id(
            plan.run_id,
            provider_tool_call_id,
        )
        tool_call = self.get_tool_call(tool_call_id)
        evidence = self.evidence[tool_call_id]
        route_receipt = self.context.receipts[step_ordinal]
        error = tool_call.error.error if tool_call.error is not None else None
        return build_workflow_step_checkpoint(
            plan,
            plan.steps[step_ordinal - 1],
            attempt_ordinal=1,
            previous_checkpoint=previous_checkpoint,
            tool_call_id=tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            skill_call_id=evidence.skill_call_id,
            skill_receipt_id=evidence.skill_receipt_id,
            skill_receipt_hash=evidence.skill_receipt_hash,
            route_execution_receipt_id=route_receipt.execution_receipt_id,
            route_execution_receipt_hash=route_receipt.receipt_hash,
            input_hash=evidence.input_hash,
            output_hash=evidence.output_hash,
            tool_result_hash=tool_call.result_hash,
            execution_status=tool_call.status,
            domain_status=(
                evidence.domain_status if tool_call.status == "succeeded" else None
            ),
            error_code=error.code if error is not None else None,
            error_message=error.message if error is not None else None,
            created_at=tool_call.finished_at or CREATED_AT,
        )


class ScriptedToolExecutor:
    def __init__(
        self,
        store: FakeAgentStore,
        *,
        statuses: tuple[str, ...] = ("succeeded", "succeeded", "succeeded"),
    ) -> None:
        self.store = store
        self.statuses = list(statuses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(
        self,
        run: Run,
        *,
        provider_tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        cancel_event: asyncio.Event,
    ) -> SimpleNamespace:
        assert tool_name == "skill_execute"
        assert not cancel_event.is_set()
        status = self.statuses.pop(0)
        self.calls.append((provider_tool_call_id, arguments))
        runtime_id = workflow_runtime_tool_call_id(run.run_id, provider_tool_call_id)
        skill_id = str(arguments["skill_id"])
        ordinal = {
            "novel-creation-suite.project-audit": 1,
            "novel-creation-suite.prewrite-gate": 2,
            "novel-creation-suite.creation-handoff": 3,
        }[skill_id]
        receipt_id = f"skill-receipt-{ordinal:032x}"
        receipt_hash = f"{ordinal + 2:x}" * 64
        domain_status = "blocked"
        if ordinal == 1:
            domain_payload: dict[str, object] = {
                "audit": {"health": "blocked", "finding_count": 3}
            }
        else:
            domain_payload = {
                (
                    "prewrite_gate"
                    if ordinal == 2
                    else "creation_handoff"
                ): {
                    "gate_status" if ordinal == 2 else "handoff_status": "blocked",
                    "prewrite_gate_status": "blocked",
                    "formal_draft_allowed": False,
                    "failure_signatures": ["formal_writer_contract_unavailable"],
                }
            }
        output = {
            "schema": "zhimeng.tool-result.skill-execute.v1",
            "tool_call_id": runtime_id,
            "skill_id": skill_id,
            "receipt_id": receipt_id,
            "receipt_hash": receipt_hash,
            "execution_status": status,
            "domain_status": domain_status if status == "succeeded" else None,
            **domain_payload,
        }
        error = None
        if status != "succeeded":
            code = "cancelled" if status == "cancelled" else "fixture_skill_failed"
            error = ErrorEnvelope(
                error=ErrorDetail(
                    code=code,
                    message=f"Fixture workflow step {status}.",
                    request_id=runtime_id,
                    retryable=False,
                )
            )
        now = datetime.now(UTC)
        tool_call = StoredToolCall(
            tool_call_id=runtime_id,
            run_id=run.run_id,
            tool_name="skill_execute",
            tool_version="2",
            status=status,
            idempotency_key=runtime_id,
            request_hash="b" * 64,
            arguments=arguments,
            output=output,
            error=error,
            call_activity_id=f"activity-call-{ordinal}",
            result_activity_id=f"activity-result-{ordinal}",
            result_hash=f"{ordinal + 5:x}" * 64,
            duration_ms=5,
            started_at=now,
            finished_at=now,
        )
        self.store.tool_calls[runtime_id] = tool_call
        self.store.evidence[runtime_id] = SimpleNamespace(
            skill_call_id=f"skill-call-{ordinal:032x}",
            skill_receipt_id=receipt_id,
            skill_receipt_hash=receipt_hash,
            input_hash=f"{ordinal + 7:x}" * 64,
            output_hash=f"{ordinal + 8:x}" * 64,
            domain_status=domain_status,
        )
        return SimpleNamespace(tool_call=tool_call, replayed=False)


def _fixture():
    run = _run()
    context = FakeContextStore(run)
    store = FakeAgentStore(context)
    runner = WorkflowRunner(store, context, FakeSkillExecutor())  # type: ignore[arg-type]
    return run, context, store, runner


def test_runner_executes_and_checkpoints_the_fixed_three_step_plan() -> None:
    async def scenario() -> None:
        run, _, store, runner = _fixture()
        executor = ScriptedToolExecutor(store)

        result = await runner.execute_pre_provider(
            run,
            cancel_event=asyncio.Event(),
            execute_tool_call=executor,
        )

        assert [step.skill_id for step in result.plan.steps] == [
            "novel-creation-suite.project-audit",
            "novel-creation-suite.prewrite-gate",
            "novel-creation-suite.creation-handoff",
        ]
        assert [step.input_contract for step in result.plan.steps] == [
            PROJECT_AUDIT_INPUT_SCHEMA,
            PREWRITE_GATE_INPUT_SCHEMA,
            CREATION_HANDOFF_INPUT_SCHEMA,
        ]
        assert result.tool_call_count == 3
        assert len(result.checkpoints) == 3
        assert len(result.provider_messages) == 6
        prewrite = executor.calls[1][1]["input"]
        assert isinstance(prewrite, dict)
        assert prewrite["project_audit_execution_receipt_hash"]
        assert prewrite["project_audit_skill_receipt_hash"]
        handoff = executor.calls[2][1]["input"]
        assert isinstance(handoff, dict)
        assert handoff["project_audit_checkpoint_hash"] == (
            result.checkpoints[0].checkpoint_hash
        )
        assert handoff["prewrite_gate_checkpoint_hash"] == (
            result.checkpoints[1].checkpoint_hash
        )
        assert handoff["prewrite_gate_execution_receipt_hash"]
        assert handoff["prewrite_gate_formal_draft_allowed"] is False
        assert store.plan == result.plan

    asyncio.run(scenario())


def test_runner_resumes_after_audit_without_reexecuting_it() -> None:
    async def scenario() -> None:
        run, _, store, runner = _fixture()
        first_executor = ScriptedToolExecutor(store, statuses=("succeeded",))
        plan = runner._compile_and_persist_plan(run)
        audit_arguments = {
            "skill_id": "novel-creation-suite.project-audit",
            "input": {"schema": "zhimeng.creation-project-audit-request.v1"},
        }
        await first_executor(
            run,
            provider_tool_call_id=workflow_provider_tool_call_id(
                plan.route_receipt_id,
                1,
            ),
            tool_name="skill_execute",
            arguments=audit_arguments,
            cancel_event=asyncio.Event(),
        )
        runner.reconcile_startup(run.run_id)
        resumed_executor = ScriptedToolExecutor(
            store,
            statuses=("succeeded", "succeeded"),
        )

        result = await runner.execute_pre_provider(
            run,
            cancel_event=asyncio.Event(),
            execute_tool_call=resumed_executor,
        )

        assert len(resumed_executor.calls) == 2
        assert [call[1]["skill_id"] for call in resumed_executor.calls] == [
            "novel-creation-suite.prewrite-gate",
            "novel-creation-suite.creation-handoff",
        ]
        assert len(result.provider_messages) == 6

    asyncio.run(scenario())


def test_final_checkpoint_replay_is_zero_execution_and_canonical() -> None:
    async def scenario() -> None:
        run, _, store, runner = _fixture()
        first_executor = ScriptedToolExecutor(store)
        first = await runner.execute_pre_provider(
            run,
            cancel_event=asyncio.Event(),
            execute_tool_call=first_executor,
        )
        replay_executor = ScriptedToolExecutor(store, statuses=())

        replay = await runner.execute_pre_provider(
            run,
            cancel_event=asyncio.Event(),
            execute_tool_call=replay_executor,
        )

        assert replay_executor.calls == []
        assert replay.provider_messages == first.provider_messages
        rendered = json.dumps(replay.provider_messages, ensure_ascii=False)
        assert "replayed" not in rendered

    asyncio.run(scenario())


def test_existing_plan_is_reused_and_skill_version_drift_fails_closed() -> None:
    run, _, store, runner = _fixture()
    plan = runner._compile_and_persist_plan(run)

    assert runner._compile_and_persist_plan(run) is plan
    assert store.create_plan_calls == 1

    skill_executor = runner.skill_executor
    assert isinstance(skill_executor, FakeSkillExecutor)
    skill_executor.skills["novel-creation-suite.project-audit"] = (
        skill_executor._skill(
            "novel-creation-suite.project-audit",
            "2026.07.21-m3i.2",
            PROJECT_AUDIT_INPUT_SCHEMA,
            "https://zhimeng.local/contracts/creation-project-audit-result.schema.json",
            "9",
        )
    )

    with pytest.raises(AgentProviderError) as drift:
        runner._compile_and_persist_plan(run)

    assert drift.value.code == "workflow_plan_skill_changed"
    assert store.plan is plan
    assert store.create_plan_calls == 1


def test_startup_reconcile_repairs_terminal_tool_result_only() -> None:
    async def scenario() -> None:
        run, context, store, runner = _fixture()
        plan = runner._compile_and_persist_plan(run)
        executor = ScriptedToolExecutor(store, statuses=("succeeded",))
        await executor(
            run,
            provider_tool_call_id=workflow_provider_tool_call_id(
                plan.route_receipt_id,
                1,
            ),
            tool_name="skill_execute",
            arguments={
                "skill_id": "novel-creation-suite.project-audit",
                "input": {"schema": "zhimeng.creation-project-audit-request.v1"},
            },
            cancel_event=asyncio.Event(),
        )
        assert context.receipts == {}
        assert store.checkpoints == []

        recovered = runner.reconcile_startup(run.run_id)

        assert recovered.reconciled_count == 1
        assert len(recovered.checkpoints) == 1
        assert list(context.receipts) == [1]
        assert executor.calls and len(executor.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_failed_or_cancelled_step_checkpoints_and_stops(
    terminal_status: str,
) -> None:
    async def scenario() -> None:
        run, _, store, runner = _fixture()
        executor = ScriptedToolExecutor(store, statuses=(terminal_status,))

        expected_error = (
            AgentProviderCancelled
            if terminal_status == "cancelled"
            else AgentProviderError
        )
        with pytest.raises(expected_error):
            await runner.execute_pre_provider(
                run,
                cancel_event=asyncio.Event(),
                execute_tool_call=executor,
            )

        checkpoints = [
            item
            for item in store.checkpoints
            if isinstance(item, WorkflowStepCheckpoint)
        ]
        assert len(executor.calls) == 1
        assert len(checkpoints) == 1
        assert checkpoints[0].execution_status == terminal_status

    asyncio.run(scenario())
