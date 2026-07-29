from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
from threading import Barrier, Event
import time

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from zhimeng_runtime.agent_store import (
    AgentContextPackConflict,
    AgentStore,
    AgentWorkflowCheckpointConflict,
    CreationInstructionConflict,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_tools import AgentToolExecutor
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextPack,
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.errors import ErrorDetail
from zhimeng_runtime.execution_protocol import (
    ActivityKind,
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    Run,
    RunStatus,
)
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillService,
    ManagedSkillVersion,
)
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    NlclRouteSource,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.prewrite_models import PrewriteOperationPackage
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_kb_models import (
    ProjectKbRebuildRequest,
    ProjectKbSnapshotManifest,
)
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
    build_workflow_plan,
    build_workflow_step,
    build_workflow_step_checkpoint,
)
from zhimeng_runtime.workflow_runner import WorkflowRunner


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROJECT_AUDIT_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-project-audit"
)
PREWRITE_GATE_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-prewrite-gate"
)
CREATION_HANDOFF_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-handoff"
)
CREATION_WRITER_INSTRUCTION_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-writer-instruction"
)
KNOWLEDGE_FINGERPRINT = "d" * 64
PROVIDER_MESSAGES_HASH = "a" * 64
TOOL_DEFINITIONS_HASH = "b" * 64


@dataclass(slots=True)
class WorkflowFixture:
    database: SqliteStore
    store: AgentStore
    context_store: ContextStore
    tool_executor: AgentToolExecutor
    run: Run
    pack: ContextPack
    route: NlclRouteReceipt
    operation_package: PrewriteOperationPackage
    versions: dict[str, ManagedSkillVersion]
    plan: WorkflowPlan
    project_kb_manifest: ProjectKbSnapshotManifest | None = None
    project_kb_current_path: Path | None = None
    project_kb_current_bytes: bytes | None = None


class StartupTrapProvider:
    def __init__(self) -> None:
        self.call_count = 0

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "f" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return executor_identity()

    @property
    def capabilities(self) -> CapabilityProfile:
        return capabilities()

    async def stream_chat(self, *args, **kwargs):
        del args, kwargs
        self.call_count += 1
        raise AssertionError("startup recovery must not call the Provider")
        yield  # pragma: no cover


class RecordingResumeProvider:
    model = "fixture-resume-model"

    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.started = Event()
        self.release = Event()
        self.call_count = 0
        self.messages: list[dict[str, object]] = []
        self.tools: tuple[object, ...] = ()

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "f" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return executor_identity()

    @property
    def capabilities(self) -> CapabilityProfile:
        return capabilities()

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.call_count += 1
        self.messages = [dict(message) for message in messages]
        self.tools = tuple(tools)
        self.started.set()
        while self.blocked and not self.release.is_set():
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.005)
        if cancel_event.is_set():
            return
        if any(tool.name == "candidate_output_submit" for tool in tools):
            _assert_runtime_candidate_reference_injection(messages)
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_resume_candidate_0001",
                tool_name="candidate_output_submit",
                arguments={
                    "kind": "repair_plan",
                    "content": {
                        "title": "Resume workflow candidate",
                        "summary": "The managed workflow completed; author review remains required.",
                        "body": "Retain this result as a risk draft and do not write formal prose.",
                        "format": "markdown",
                    },
                },
            )
            yield ProviderCompleted("tool_calls", None)
            return
        yield ProviderTextDelta("Resume completed.")
        yield ProviderCompleted("stop", None)


def _assert_runtime_candidate_reference_injection(messages) -> None:
    writer_dispatch = None
    instruction_dispatch = None
    for message in messages:
        if message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schema") == "zhimeng.writer-task-dispatch.v1":
            writer_dispatch = payload
        elif payload.get("schema") == "zhimeng.creation-instruction-dispatch.v1":
            instruction_dispatch = payload
    assert writer_dispatch is not None
    assert instruction_dispatch is not None
    assert writer_dispatch["runtime_adds_writer_task_reference"] is True
    assert instruction_dispatch["runtime_adds_materialization_reference"] is True


def executor_identity() -> ExecutorIdentity:
    return ExecutorIdentity(
        executor_id="executor.workflow-store",
        kind=ExecutorKind.EMBEDDED,
        implementation="workflow-store-tests",
        version="1",
        protocol_version="1",
        instance_id=f"provider-config-{'f' * 64}",
    )


def capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        protocol_version="1",
        names=[
            CapabilityName.STREAMING,
            CapabilityName.TOOL_CALLS,
            CapabilityName.SKILLS,
            CapabilityName.CHECKPOINTS,
        ],
        extensions={},
    )


def knowledge_observation(
    pack: ContextPack,
    *,
    state: KnowledgeObservationState = KnowledgeObservationState.MATCHED,
    observed_fingerprint: str | None = None,
) -> KnowledgeFingerprintObservation:
    if state is KnowledgeObservationState.MATCHED:
        observed_fingerprint = pack.knowledge_fingerprint
    return KnowledgeFingerprintObservation(
        context_pack_id=pack.context_pack_id,
        pack_hash=pack.pack_hash,
        expected_fingerprint=pack.knowledge_fingerprint,
        observed_fingerprint=observed_fingerprint,
        state=state,
        observed_at=datetime.now(UTC),
    )


def _copy_and_register_skills(
    database: SqliteStore,
    tmp_path: Path,
) -> tuple[ManagedSkillService, dict[str, ManagedSkillVersion]]:
    managed_root = tmp_path / "managed-skills"
    managed_root.mkdir()
    service = ManagedSkillService(database, managed_root)
    versions: dict[str, ManagedSkillVersion] = {}
    for source in (
        PROJECT_AUDIT_PACKAGE,
        PREWRITE_GATE_PACKAGE,
        CREATION_HANDOFF_PACKAGE,
        CREATION_WRITER_INSTRUCTION_PACKAGE,
    ):
        target = managed_root / source.name
        shutil.copytree(source, target)
        registered = service.register_builtin(target)
        versions[registered.manifest.id] = registered
    return service, versions


def _context_candidates() -> tuple[ContextCandidateFragment, ...]:
    return (
        ContextCandidateFragment(
            source_id="project:character-state",
            source_type="project_section",
            title="03_人物设定/人物状态.md",
            authority=ContextAuthority.PROJECT_TRUTH,
            version="revision-1",
            source_hash=canonical_json_hash({"source": "character-state"}),
            content="Lin refuses to sacrifice the witness.",
            priority=900,
            mandatory=True,
            eligible=True,
        ),
        ContextCandidateFragment(
            source_id="project:active-chapter",
            source_type="active_chapter",
            title="05_正文内容/第一卷/CH001_正文.md",
            authority=ContextAuthority.PROJECT_TRUTH,
            version="revision-1",
            source_hash=canonical_json_hash({"source": "active-chapter"}),
            content="The witness left a blood-stained key.",
            priority=900,
            mandatory=True,
            eligible=True,
        ),
        ContextCandidateFragment(
            source_id="project:chapter-control",
            source_type="chapter_control",
            title="04_剧情大纲/CH001_控制卡.md",
            authority=ContextAuthority.PROJECT_TRUTH,
            version="revision-1",
            source_hash=canonical_json_hash({"source": "chapter-control"}),
            content="Recover the witness without losing the key clue.",
            priority=900,
            mandatory=True,
            eligible=True,
        ),
    )


def build_fixture(
    tmp_path: Path,
    *,
    prepare_injection: bool = True,
    create_plan: bool = True,
    atomic_plan: bool = False,
    transition_to_running: bool = True,
    knowledge_fingerprint: str | None = KNOWLEDGE_FINGERPRINT,
    publish_project_kb: bool = False,
) -> WorkflowFixture:
    if atomic_plan and not create_plan:
        raise ValueError("atomic_plan requires create_plan")
    library = tmp_path / "library"
    project = library / "novel"
    project.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (project / f"{index:02d}_{label}").mkdir()
    chapter = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    chapter.parent.mkdir()
    chapter.write_text("The witness left a blood-stained key.\n", encoding="utf-8")

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
    project_kb_manifest: ProjectKbSnapshotManifest | None = None
    project_kb_current_path: Path | None = None
    project_kb_current_bytes: bytes | None = None
    if publish_project_kb:
        project_kb = ProjectKbService(database, reader, tmp_path / "project-kb")
        project_kb.store.ensure_project_scope(binding.project_id)
        knowledge = project_kb.store.project_state_snapshot(binding.project_id)
        rebuilt = project_kb.rebuild(
            binding.project_id,
            ProjectKbRebuildRequest(
                expected_project_revision=binding.revision,
                expected_project_fingerprint=binding.fingerprint,
                expected_scope_state_revision=knowledge.scope.state_revision,
                expected_knowledge_fingerprint=canonical_json_hash(
                    knowledge.fingerprint_material
                ),
            ),
        )
        assert rebuilt.record.manifest is not None
        project_kb_manifest = rebuilt.record.manifest
        project_kb_current_path = (
            tmp_path / "project-kb" / f"{binding.project_id}.kb" / "current.json"
        )
        assert project_kb_current_path.is_file()
        project_kb_current_bytes = project_kb_current_path.read_bytes()

    managed_service, versions = _copy_and_register_skills(database, tmp_path)
    store = AgentStore(database)
    session = store.create_session(
        project_id=binding.project_id,
        title="Durable workflow",
        executor=executor_identity(),
        capabilities=capabilities(),
    )
    history_ordinal, history = store.completed_history(session.session_id)
    pack = compile_context_pack(
        context_pack_id="context-pack-workflow-store",
        session_id=session.session_id,
        project_id=binding.project_id,
        history_through_ordinal=history_ordinal,
        request=ContextCreateRequest(
            objective="Run the reviewed project audit and prewrite gate.",
            profile="chapter",
            active_chapter_id="VOL001-CH001",
            expected_project_revision=binding.revision,
            expected_project_fingerprint=binding.fingerprint,
            project_kb_state_id=(
                project_kb_manifest.state_id
                if project_kb_manifest is not None
                else "project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            project_kb_manifest_hash=(
                project_kb_manifest.manifest_hash
                if project_kb_manifest is not None
                else "b" * 64
            ),
        ),
        history=history,
        candidates=_context_candidates(),
        max_characters=20_000,
        max_tokens=20_000,
        created_at=datetime.now(UTC),
        knowledge_fingerprint=(
            project_kb_manifest.knowledge_fingerprint
            if project_kb_manifest is not None
            else knowledge_fingerprint
        ),
    )
    route = build_nlcl_route_receipt(pack, source=NlclRouteSource.NATIVE_KNOWLEDGE)
    context_store = ContextStore(database)
    context_store.create_pack(pack, nlcl_route=route)
    operation_package = context_store.get_prewrite_operation_package(
        pack.context_pack_id
    )
    tool_executor = AgentToolExecutor(
        reader,
        database,
        ProjectLockRegistry(),
        ChangeSetService(
            reader,
            database,
            tmp_path / "change-sets",
        ),
        skill_executor=SkillExecutor(managed_service),
    )
    workflow_runner = WorkflowRunner(
        store,
        context_store,
        tool_executor.skill_executor,
    )
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=pack.objective,
        expected_project_revision=pack.project_revision,
        expected_project_fingerprint=pack.project_fingerprint,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="workflow-store-turn-0001",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
        knowledge_observation=knowledge_observation(pack),
        workflow_plan_builder=(
            (
                lambda run: workflow_runner.build_plan(
                    run,
                    context_pack_id=pack.context_pack_id,
                )
            )
            if atomic_plan
            else None
        ),
    )
    if transition_to_running:
        store.transition_run(run.run_id, RunStatus.STARTING, detail="Prepare workflow")
        if prepare_injection:
            store.prepare_context_injection(
                run.run_id,
                knowledge_observation=knowledge_observation(pack),
            )
        run = store.transition_run(run.run_id, RunStatus.RUNNING, detail="Run workflow")

    if atomic_plan:
        plan = store.get_workflow_plan(run.run_id)
    else:
        steps = tuple(
            build_workflow_step(
                ordinal=call.ordinal,
                skill_id=call.skill_id,
                expected_skill_version_id=versions[call.skill_id].skill_version_id,
                expected_skill_version=versions[call.skill_id].manifest.version,
                expected_manifest_fingerprint=versions[call.skill_id].manifest_fingerprint,
                expected_package_fingerprint=versions[call.skill_id].package_fingerprint,
                input_contract=versions[call.skill_id].manifest.input_schema,
                output_contract=versions[call.skill_id].manifest.output_schema,
                input_builder_id=f"workflow.{call.skill_id.rsplit('.', 1)[-1]}-input",
                input_builder_version="1.0.0",
                max_attempts=1,
            )
            for call in route.chapter_call_list
        )
        plan = build_workflow_plan(
            run_id=run.run_id,
            project_id=pack.project_id,
            project_revision=pack.project_revision,
            project_fingerprint=pack.project_fingerprint,
            context_pack_id=pack.context_pack_id,
            context_pack_hash=pack.pack_hash,
            knowledge_fingerprint=pack.knowledge_fingerprint,
            route_receipt_id=route.route_receipt_id,
            route_receipt_hash=route.receipt_hash,
            operation_package_id=operation_package.operation_package_id,
            operation_package_hash=operation_package.package_hash,
            steps=steps,
            created_at=datetime.now(UTC),
        )
        if create_plan:
            plan = store.create_workflow_plan(plan)
    return WorkflowFixture(
        database=database,
        store=store,
        context_store=context_store,
        tool_executor=tool_executor,
        run=run,
        pack=pack,
        route=route,
        operation_package=operation_package,
        versions=versions,
        plan=plan,
        project_kb_manifest=project_kb_manifest,
        project_kb_current_path=project_kb_current_path,
        project_kb_current_bytes=project_kb_current_bytes,
    )


def app_settings(fixture: WorkflowFixture) -> RuntimeSettings:
    state_root = fixture.database.path.parent
    return RuntimeSettings(
        environment="test",
        database_path=fixture.database.path,
        trusted_project_roots=((state_root / "library").resolve(),),
        managed_skill_root=(state_root / "managed-skills").resolve(),
        allowed_origins=(),
    )


def assert_published_project_kb_fixture(
    fixture: WorkflowFixture,
) -> tuple[Path, bytes]:
    manifest = fixture.project_kb_manifest
    current_path = fixture.project_kb_current_path
    current_bytes = fixture.project_kb_current_bytes
    assert manifest is not None
    assert current_path is not None
    assert current_bytes is not None
    assert fixture.pack.project_kb_state_id == manifest.state_id
    assert fixture.pack.project_kb_manifest_hash == manifest.manifest_hash
    assert fixture.pack.knowledge_fingerprint == manifest.knowledge_fingerprint
    assert current_path.is_file()
    assert current_path.read_bytes() == current_bytes
    return current_path, current_bytes


def resume_request(workflow_state: dict[str, object]) -> dict[str, object]:
    plan = workflow_state["plan"]
    assert isinstance(plan, dict)
    return {
        "expected_workflow_plan_id": plan["workflow_plan_id"],
        "expected_plan_hash": plan["plan_hash"],
        "expected_checkpoint_id": workflow_state["last_checkpoint_id"],
        "expected_checkpoint_hash": workflow_state["last_checkpoint_hash"],
    }


def wait_for_terminal_run(client: TestClient, run_id: str) -> dict[str, object]:
    # A resumed three-step workflow still revalidates the project, reconstructs
    # the Writer task, and commits the candidate transaction. Windows CI can
    # exceed five seconds while other Runtime suites are active.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/agent/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()["run"]
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.01)
    raise AssertionError("resumed workflow Run did not reach a terminal state")


def parse_sse_payloads(response_text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def provider_tool_call_id(plan: WorkflowPlan, step_ordinal: int) -> str:
    digest = hashlib.sha256(
        f"{plan.route_receipt_id}\0{step_ordinal}".encode("utf-8")
    ).hexdigest()
    return f"nlcl_call_{digest[:24]}"


def runtime_tool_call_id(run_id: str, provider_call_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\0{provider_call_id}".encode("utf-8")).hexdigest()
    return f"tool_{digest[:32]}"


def _step_arguments(fixture: WorkflowFixture, step_ordinal: int) -> dict[str, object]:
    call = fixture.route.chapter_call_list[step_ordinal - 1]
    arguments: dict[str, object] = {
        "skill_id": call.skill_id,
        "input": {"schema": call.input_contract},
    }
    if step_ordinal == 2:
        audit = fixture.context_store.get_route_execution(
            fixture.pack.context_pack_id,
            call_ordinal=1,
        )
        assert audit is not None
        audit_call = fixture.store.get_tool_call(audit.tool_call_id)
        assert isinstance(audit_call.output, dict)
        audit_summary = audit_call.output["audit"]
        assert isinstance(audit_summary, dict)
        arguments["input"] = {
            "schema": call.input_contract,
            "operation_package": fixture.operation_package.model_dump(
                mode="json",
                by_alias=True,
            ),
            "project_audit_execution_receipt_hash": audit.receipt_hash,
            "project_audit_skill_receipt_hash": audit.skill_receipt_hash,
            "project_audit_tool_result_hash": audit.tool_result_hash,
            "project_audit_domain_status": audit.domain_status,
            "project_audit_finding_count": audit_summary["finding_count"],
        }
    elif step_ordinal == 3:
        audit = fixture.context_store.get_route_execution(
            fixture.pack.context_pack_id,
            call_ordinal=1,
        )
        prewrite = fixture.context_store.get_route_execution(
            fixture.pack.context_pack_id,
            call_ordinal=2,
        )
        assert audit is not None
        assert prewrite is not None
        audit_call = fixture.store.get_tool_call(audit.tool_call_id)
        prewrite_call = fixture.store.get_tool_call(prewrite.tool_call_id)
        assert isinstance(audit_call.output, dict)
        assert isinstance(prewrite_call.output, dict)
        audit_summary = audit_call.output["audit"]
        prewrite_gate = prewrite_call.output["prewrite_gate"]
        assert isinstance(audit_summary, dict)
        assert isinstance(prewrite_gate, dict)
        checkpoints = fixture.store.list_workflow_checkpoints(fixture.run.run_id)
        audit_checkpoint = next(item for item in checkpoints if item.step_ordinal == 1)
        prewrite_checkpoint = next(item for item in checkpoints if item.step_ordinal == 2)
        arguments["input"] = {
            "schema": call.input_contract,
            "workflow_plan_id": fixture.plan.workflow_plan_id,
            "workflow_plan_hash": fixture.plan.plan_hash,
            "knowledge_fingerprint": fixture.plan.knowledge_fingerprint,
            "operation_package": fixture.operation_package.model_dump(
                mode="json",
                by_alias=True,
            ),
            "project_audit_execution_receipt_hash": audit.receipt_hash,
            "project_audit_skill_receipt_hash": audit.skill_receipt_hash,
            "project_audit_tool_result_hash": audit.tool_result_hash,
            "project_audit_checkpoint_hash": audit_checkpoint.checkpoint_hash,
            "project_audit_domain_status": audit.domain_status,
            "project_audit_finding_count": audit_summary["finding_count"],
            "prewrite_gate_execution_receipt_hash": prewrite.receipt_hash,
            "prewrite_gate_skill_receipt_hash": prewrite.skill_receipt_hash,
            "prewrite_gate_tool_result_hash": prewrite.tool_result_hash,
            "prewrite_gate_checkpoint_hash": prewrite_checkpoint.checkpoint_hash,
            "prewrite_gate_domain_status": prewrite.domain_status,
            "prewrite_gate_status": prewrite_gate["gate_status"],
            "prewrite_gate_formal_draft_allowed": prewrite_gate[
                "formal_draft_allowed"
            ],
            "prewrite_gate_failure_signatures": prewrite_gate[
                "failure_signatures"
            ],
        }
    return arguments


def execute_step(
    fixture: WorkflowFixture,
    *,
    step_ordinal: int,
    previous_checkpoint: WorkflowStepCheckpoint | None,
) -> WorkflowStepCheckpoint:
    async def scenario() -> WorkflowStepCheckpoint:
        provider_id = provider_tool_call_id(fixture.plan, step_ordinal)
        runtime_id = runtime_tool_call_id(fixture.run.run_id, provider_id)
        arguments = _step_arguments(fixture, step_ordinal)
        request_hash = fixture.tool_executor.request_hash("skill_execute", "2", arguments)
        fixture.store.begin_tool_call(
            fixture.run.run_id,
            tool_call_id=runtime_id,
            tool_name="skill_execute",
            tool_version="2",
            arguments=arguments,
            request_hash=request_hash,
            idempotency_key=runtime_id,
        )
        result = await fixture.tool_executor.execute(
            fixture.store.get_run(fixture.run.run_id),
            tool_call_id=runtime_id,
            tool_name="skill_execute",
            arguments=arguments,
            request_hash=request_hash,
            cancel_event=asyncio.Event(),
        )
        fixture.store.finish_tool_call(
            fixture.run.run_id,
            tool_call_id=runtime_id,
            status="succeeded",
            output=result.output,
            error=None,
            duration_ms=1,
            skill_call=result.skill_call,
            artifacts=result.artifacts,
        )
        fixture.context_store.reconcile_route_execution(
            fixture.pack.context_pack_id,
            call_ordinal=step_ordinal,
            run_id=fixture.run.run_id,
            tool_call_id=runtime_id,
        )
        return fixture.store.build_workflow_step_checkpoint_from_execution(
            fixture.plan,
            step_ordinal=step_ordinal,
            provider_tool_call_id=provider_id,
            previous_checkpoint=previous_checkpoint,
        )

    return asyncio.run(scenario())


def finish_step_without_reconciliation(
    fixture: WorkflowFixture,
    *,
    step_ordinal: int,
) -> None:
    async def scenario() -> None:
        provider_id = provider_tool_call_id(fixture.plan, step_ordinal)
        runtime_id = runtime_tool_call_id(fixture.run.run_id, provider_id)
        arguments = _step_arguments(fixture, step_ordinal)
        request_hash = fixture.tool_executor.request_hash(
            "skill_execute",
            "2",
            arguments,
        )
        fixture.store.begin_tool_call(
            fixture.run.run_id,
            tool_call_id=runtime_id,
            tool_name="skill_execute",
            tool_version="2",
            arguments=arguments,
            request_hash=request_hash,
            idempotency_key=runtime_id,
        )
        result = await fixture.tool_executor.execute(
            fixture.store.get_run(fixture.run.run_id),
            tool_call_id=runtime_id,
            tool_name="skill_execute",
            arguments=arguments,
            request_hash=request_hash,
            cancel_event=asyncio.Event(),
        )
        fixture.store.finish_tool_call(
            fixture.run.run_id,
            tool_call_id=runtime_id,
            status="succeeded",
            output=result.output,
            error=None,
            duration_ms=1,
            skill_call=result.skill_call,
            artifacts=result.artifacts,
        )

    asyncio.run(scenario())


def append_full_chain(
    fixture: WorkflowFixture,
) -> tuple[WorkflowStepCheckpoint, WorkflowStepCheckpoint, WorkflowStepCheckpoint]:
    first = execute_step(fixture, step_ordinal=1, previous_checkpoint=None)
    fixture.store.append_workflow_step_checkpoint(
        first,
        expected_previous_checkpoint_hash=None,
    )
    second = execute_step(fixture, step_ordinal=2, previous_checkpoint=first)
    fixture.store.append_workflow_step_checkpoint(
        second,
        expected_previous_checkpoint_hash=first.checkpoint_hash,
    )
    third = execute_step(fixture, step_ordinal=3, previous_checkpoint=second)
    fixture.store.append_workflow_step_checkpoint(
        third,
        expected_previous_checkpoint_hash=second.checkpoint_hash,
    )
    return first, second, third


def _prepare_creation_writer_dispatch(fixture: WorkflowFixture) -> None:
    fixture.store.ensure_writer_task(fixture.run.run_id, target_files=())
    fixture.store.ensure_creation_instruction_materialization(fixture.run.run_id)


def test_real_workflow_plan_is_atomic_and_rejects_unbound_sources(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, create_plan=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        plans = list(
            pool.map(lambda _: fixture.store.create_workflow_plan(fixture.plan), range(4))
        )
    assert plans == [fixture.plan] * 4
    assert fixture.store.get_workflow_plan(fixture.run.run_id) == fixture.plan

    other = build_fixture(tmp_path / "other", create_plan=False)
    forged = build_workflow_plan(
        run_id=other.plan.run_id,
        project_id=other.plan.project_id,
        project_revision=other.plan.project_revision,
        project_fingerprint=other.plan.project_fingerprint,
        context_pack_id=other.plan.context_pack_id,
        context_pack_hash="0" * 64,
        knowledge_fingerprint=other.plan.knowledge_fingerprint,
        route_receipt_id=other.plan.route_receipt_id,
        route_receipt_hash=other.plan.route_receipt_hash,
        operation_package_id=other.plan.operation_package_id,
        operation_package_hash=other.plan.operation_package_hash,
        steps=other.plan.steps,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(AgentWorkflowCheckpointConflict) as source:
        other.store.create_workflow_plan(forged)
    assert source.value.code == "workflow_plan_source_mismatch"


def test_plan_read_fails_closed_after_managed_package_drift(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    version = fixture.versions[fixture.plan.steps[0].skill_id]
    version.entrypoint_path.write_text("print('drift')\n", encoding="utf-8")

    with pytest.raises(AgentWorkflowCheckpointConflict) as drift:
        fixture.store.get_workflow_plan(fixture.run.run_id)
    assert drift.value.code == "workflow_plan_skill_integrity_failed"


def test_plan_read_fails_closed_after_project_effective_skill_is_disabled(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    first_step = fixture.plan.steps[0]
    assert fixture.tool_executor.skill_executor is not None
    service = fixture.tool_executor.skill_executor._service

    service.activate(
        first_step.skill_id,
        ManagedSkillActivationRequest(
            action="disable",
            expected_state_revision=0,
            expected_version_id=None,
        ),
        project_id=fixture.plan.project_id,
    )

    with pytest.raises(AgentWorkflowCheckpointConflict) as disabled:
        fixture.store.get_workflow_plan(fixture.run.run_id)
    assert disabled.value.code == "workflow_plan_skill_changed"


def test_factory_and_store_bind_real_execution_and_provider_boundary(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first, second, third = append_full_chain(fixture)
    _prepare_creation_writer_dispatch(fixture)
    assert fixture.store.list_workflow_checkpoints(fixture.run.run_id) == [
        first,
        second,
        third,
    ]
    workflow_progress = [
        activity.root.payload.detail
        for activity in fixture.store.list_activities(fixture.run.run_id)
        if activity.root.kind is ActivityKind.RUN_STATE
        and activity.root.payload.previous_status is RunStatus.RUNNING
        and activity.root.payload.status is RunStatus.RUNNING
    ]
    assert workflow_progress[-2:] == [
        "Workflow step 2/3 checkpoint persisted",
        "Workflow step 3/3 checkpoint persisted",
    ]

    boundary = build_provider_io_boundary(
        fixture.plan,
        (first, second, third),
        request_hash="1" * 64,
        provider_model="fixture-model",
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    assert fixture.store.create_provider_io_boundary(
        boundary,
        expected_previous_checkpoint_hash=third.checkpoint_hash,
        provider_messages_hash=PROVIDER_MESSAGES_HASH,
        tool_definitions_hash=TOOL_DEFINITIONS_HASH,
    ) == boundary
    workflow_progress = [
        activity.root.payload.detail
        for activity in fixture.store.list_activities(fixture.run.run_id)
        if activity.root.kind is ActivityKind.RUN_STATE
        and activity.root.payload.previous_status is RunStatus.RUNNING
        and activity.root.payload.status is RunStatus.RUNNING
    ]
    assert (
        workflow_progress[-1]
        == "Provider boundary and instruction dispatch receipt persisted"
    )
    fixture.store.fail_run(
        fixture.run.run_id,
        code="fixture_terminal",
        message="Terminal after the persisted Provider boundary",
        retryable=False,
    )
    with pytest.raises(CreationInstructionConflict) as duplicate:
        fixture.store.create_provider_io_boundary(
            boundary,
            expected_previous_checkpoint_hash=third.checkpoint_hash,
            provider_messages_hash=PROVIDER_MESSAGES_HASH,
            tool_definitions_hash=TOOL_DEFINITIONS_HASH,
        )
    assert duplicate.value.code == "creation_instruction_dispatch_already_committed"
    assert sum(
        activity.root.kind is ActivityKind.RUN_STATE
        and activity.root.payload.detail
        == "Provider boundary and instruction dispatch receipt persisted"
        for activity in fixture.store.list_activities(fixture.run.run_id)
    ) == 1


def test_forged_execution_receipt_and_terminal_boundary_are_rejected(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    provider_id = provider_tool_call_id(fixture.plan, 1)
    runtime_id = runtime_tool_call_id(fixture.run.run_id, provider_id)
    forged = build_workflow_step_checkpoint(
        fixture.plan,
        fixture.plan.steps[0],
        attempt_ordinal=1,
        previous_checkpoint=None,
        tool_call_id=runtime_id,
        provider_tool_call_id=provider_id,
        skill_call_id="skill-call-forged",
        skill_receipt_id="skill-receipt-" + "1" * 32,
        skill_receipt_hash="2" * 64,
        route_execution_receipt_id="nlcl-route-exec-" + "3" * 32,
        route_execution_receipt_hash="4" * 64,
        input_hash="5" * 64,
        output_hash="6" * 64,
        tool_result_hash="7" * 64,
        execution_status="succeeded",
        domain_status="healthy",
        error_code=None,
        error_message=None,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(AgentWorkflowCheckpointConflict) as evidence:
        fixture.store.append_workflow_step_checkpoint(
            forged,
            expected_previous_checkpoint_hash=None,
        )
    assert evidence.value.code == "workflow_checkpoint_evidence_missing"

    first, second, third = append_full_chain(fixture)
    _prepare_creation_writer_dispatch(fixture)
    boundary = build_provider_io_boundary(
        fixture.plan,
        (first, second, third),
        request_hash="8" * 64,
        provider_model="fixture-model",
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    fixture.store.fail_run(
        fixture.run.run_id,
        code="fixture_terminal",
        message="No Provider boundary was created",
        retryable=False,
    )
    with pytest.raises(AgentWorkflowCheckpointConflict) as terminal:
        fixture.store.create_provider_io_boundary(
            boundary,
            expected_previous_checkpoint_hash=third.checkpoint_hash,
            provider_messages_hash=PROVIDER_MESSAGES_HASH,
            tool_definitions_hash=TOOL_DEFINITIONS_HASH,
        )
    assert terminal.value.code == "workflow_checkpoint_run_not_running"


def test_competing_valid_pre_dispatch_checkpoints_use_sqlite_cas(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first = fixture.store.build_workflow_step_checkpoint_from_execution(
        fixture.plan,
        step_ordinal=1,
        provider_tool_call_id=None,
        previous_checkpoint=None,
        pre_dispatch_error=ErrorDetail(
            code="workflow_dependency_unavailable",
            message="The first dependency was unavailable.",
            request_id=fixture.run.run_id,
            retryable=False,
        ),
    )
    competing = fixture.store.build_workflow_step_checkpoint_from_execution(
        fixture.plan,
        step_ordinal=1,
        provider_tool_call_id=None,
        previous_checkpoint=None,
        pre_dispatch_error=ErrorDetail(
            code="workflow_dependency_changed",
            message="The first dependency changed.",
            request_id=fixture.run.run_id,
            retryable=False,
        ),
    )
    assert first.checkpoint_id == competing.checkpoint_id
    assert first.checkpoint_hash != competing.checkpoint_hash

    def append(candidate: WorkflowStepCheckpoint):
        try:
            return fixture.store.append_workflow_step_checkpoint(
                candidate,
                expected_previous_checkpoint_hash=None,
            )
        except AgentWorkflowCheckpointConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(append, (first, competing)))
    assert sum(isinstance(item, WorkflowStepCheckpoint) for item in outcomes) == 1
    assert "workflow_checkpoint_identity_conflict" in outcomes


def test_restart_and_upstream_tamper_are_detected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first = execute_step(fixture, step_ordinal=1, previous_checkpoint=None)
    fixture.store.append_workflow_step_checkpoint(
        first,
        expected_previous_checkpoint_hash=None,
    )
    restarted_database = SqliteStore(fixture.database.path)
    restarted_database.initialize()
    restarted = AgentStore(restarted_database)
    assert restarted.list_workflow_checkpoints(fixture.run.run_id) == [first]

    with restarted.database.connection() as connection:
        connection.execute(
            "UPDATE tool_call SET result_hash = ? WHERE id = ?",
            ("0" * 64, first.tool_call_id),
        )
    with pytest.raises(AgentWorkflowCheckpointConflict) as integrity:
        restarted.list_workflow_checkpoints(fixture.run.run_id)
    assert integrity.value.code in {
        "workflow_checkpoint_evidence_integrity_failed",
        "workflow_checkpoint_evidence_mismatch",
    }


def test_invalid_tool_error_payload_is_reported_as_checkpoint_conflict(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    first = execute_step(fixture, step_ordinal=1, previous_checkpoint=None)
    fixture.store.append_workflow_step_checkpoint(
        first,
        expected_previous_checkpoint_hash=None,
    )
    with fixture.database.connection() as connection:
        connection.execute(
            "UPDATE tool_call SET error_json = ? WHERE id = ?",
            ("{", first.tool_call_id),
        )

    with pytest.raises(AgentWorkflowCheckpointConflict) as integrity:
        fixture.store.list_workflow_checkpoints(fixture.run.run_id)
    assert integrity.value.code == "workflow_checkpoint_evidence_integrity_failed"


def test_running_context_reload_requires_receipt_and_rechecks_knowledge(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path / "with-receipt")
    with pytest.raises(AgentContextPackConflict) as stale:
        fixture.store.prepare_context_injection(
            fixture.run.run_id,
            knowledge_observation=knowledge_observation(
                fixture.pack,
                state=KnowledgeObservationState.CHANGED,
                observed_fingerprint="e" * 64,
            ),
        )
    assert stale.value.code == "context_pack_source_changed"

    missing = build_fixture(
        tmp_path / "without-receipt",
        prepare_injection=False,
        create_plan=False,
    )
    with pytest.raises(AgentContextPackConflict) as absent:
        missing.store.prepare_context_injection(
            missing.run.run_id,
            knowledge_observation=knowledge_observation(missing.pack),
        )
    assert absent.value.code == "context_injection_receipt_missing"


def test_workflow_restart_pause_and_resume_claim_use_checkpoint_cas(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)

    paused = fixture.store.pause_workflow_for_restart(fixture.run.run_id)
    assert paused.status is RunStatus.PAUSED
    assert paused.completed_at is None
    assert fixture.store.list_nonterminal_workflow_run_ids() == [fixture.run.run_id]
    assert fixture.store.recover_interrupted() == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED

    with pytest.raises(AgentWorkflowCheckpointConflict) as stale:
        fixture.store.claim_workflow_resume(
            fixture.run.run_id,
            expected_workflow_plan_id=fixture.plan.workflow_plan_id,
            expected_plan_hash=fixture.plan.plan_hash,
            expected_checkpoint_id="workflow-checkpoint-" + "0" * 32,
            expected_checkpoint_hash="0" * 64,
        )
    assert stale.value.code == "workflow_resume_stale"

    claim = fixture.store.claim_workflow_resume(
        fixture.run.run_id,
        expected_workflow_plan_id=fixture.plan.workflow_plan_id,
        expected_plan_hash=fixture.plan.plan_hash,
        expected_checkpoint_id=None,
        expected_checkpoint_hash=None,
    )
    assert claim.run.status is RunStatus.RUNNING
    assert claim.checkpoints == ()
    assert claim.next_step_ordinal == 1

    with pytest.raises(AgentWorkflowCheckpointConflict) as competing:
        fixture.store.claim_workflow_resume(
            fixture.run.run_id,
            expected_workflow_plan_id=fixture.plan.workflow_plan_id,
            expected_plan_hash=fixture.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )
    assert competing.value.code == "agent_run_not_paused"


def test_interrupted_tool_call_without_checkpoint_blocks_resume(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    provider_id = provider_tool_call_id(fixture.plan, 1)
    runtime_id = runtime_tool_call_id(fixture.run.run_id, provider_id)
    arguments = _step_arguments(fixture, 1)
    request_hash = fixture.tool_executor.request_hash(
        "skill_execute",
        "2",
        arguments,
    )
    fixture.store.begin_tool_call(
        fixture.run.run_id,
        tool_call_id=runtime_id,
        tool_name="skill_execute",
        tool_version="2",
        arguments=arguments,
        request_hash=request_hash,
        idempotency_key=runtime_id,
    )

    WorkflowRunner(
        fixture.store,
        fixture.context_store,
        fixture.tool_executor.skill_executor,
    ).reconcile_startup(fixture.run.run_id)
    fixture.store.pause_workflow_for_restart(fixture.run.run_id)

    assert fixture.store.get_tool_call(runtime_id).status == "failed"
    assert fixture.store.list_workflow_checkpoints(fixture.run.run_id) == []
    assert (
        fixture.store.workflow_resume_blocker(fixture.run.run_id)
        == "workflow_step_interrupted"
    )
    with pytest.raises(AgentWorkflowCheckpointConflict) as interrupted:
        fixture.store.claim_workflow_resume(
            fixture.run.run_id,
            expected_workflow_plan_id=fixture.plan.workflow_plan_id,
            expected_plan_hash=fixture.plan.plan_hash,
            expected_checkpoint_id=None,
            expected_checkpoint_hash=None,
        )
    assert interrupted.value.code == "workflow_step_interrupted"
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED


def test_final_workflow_checkpoint_resumes_only_to_provider_boundary(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    first, second, third = append_full_chain(fixture)
    fixture.store.pause_workflow_for_restart(fixture.run.run_id)

    claim = fixture.store.claim_workflow_resume(
        fixture.run.run_id,
        expected_workflow_plan_id=fixture.plan.workflow_plan_id,
        expected_plan_hash=fixture.plan.plan_hash,
        expected_checkpoint_id=third.checkpoint_id,
        expected_checkpoint_hash=third.checkpoint_hash,
    )

    assert claim.checkpoints == (first, second, third)
    assert claim.next_step_ordinal is None
    assert claim.run.status is RunStatus.RUNNING


def test_provider_boundary_can_never_be_paused_for_replay(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    first, second, third = append_full_chain(fixture)
    _prepare_creation_writer_dispatch(fixture)
    boundary = build_provider_io_boundary(
        fixture.plan,
        (first, second, third),
        request_hash="1" * 64,
        provider_model=None,
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    fixture.store.create_provider_io_boundary(
        boundary,
        expected_previous_checkpoint_hash=third.checkpoint_hash,
        provider_messages_hash=PROVIDER_MESSAGES_HASH,
        tool_definitions_hash=TOOL_DEFINITIONS_HASH,
    )

    with pytest.raises(AgentWorkflowCheckpointConflict) as dispatched:
        fixture.store.pause_workflow_for_restart(fixture.run.run_id)
    assert dispatched.value.code == "provider_dispatch_already_started"


def test_app_startup_reconciles_terminal_step_before_pausing_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path)
    finish_step_without_reconciliation(fixture, step_ordinal=1)
    assert fixture.context_store.get_route_execution(
        fixture.pack.context_pack_id,
        call_ordinal=1,
    ) is None

    events: list[str] = []
    original_register = ManagedSkillService.register_builtin
    original_change_set_recovery = ChangeSetService.recover_interrupted
    original_reconcile = WorkflowRunner.reconcile_startup
    original_pause = AgentStore.pause_workflow_for_restart
    original_legacy_recovery = AgentStore.recover_interrupted
    original_source_recovery = SqliteStore.recover_interrupted_source_imports

    def tracked_register(self, package_root):
        registered = original_register(self, package_root)
        events.append(f"managed:{Path(package_root).name}")
        return registered

    def tracked_change_set_recovery(self):
        events.append("change-set")
        return original_change_set_recovery(self)

    def tracked_reconcile(self, run_id):
        assert self.store.get_run(run_id).status is RunStatus.RUNNING
        events.append("workflow-reconcile")
        return original_reconcile(self, run_id)

    def tracked_pause(self, run_id):
        events.append("workflow-pause")
        return original_pause(self, run_id)

    def tracked_legacy_recovery(self):
        events.append("legacy-run")
        return original_legacy_recovery(self)

    def tracked_source_recovery(self):
        events.append("source-import")
        return original_source_recovery(self)

    async def unexpected_skill_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("startup recovery must not execute a Skill")

    monkeypatch.setattr(ManagedSkillService, "register_builtin", tracked_register)
    monkeypatch.setattr(
        ChangeSetService,
        "recover_interrupted",
        tracked_change_set_recovery,
    )
    monkeypatch.setattr(WorkflowRunner, "reconcile_startup", tracked_reconcile)
    monkeypatch.setattr(AgentStore, "pause_workflow_for_restart", tracked_pause)
    monkeypatch.setattr(AgentStore, "recover_interrupted", tracked_legacy_recovery)
    monkeypatch.setattr(
        SqliteStore,
        "recover_interrupted_source_imports",
        tracked_source_recovery,
    )
    monkeypatch.setattr(SkillExecutor, "execute", unexpected_skill_execute)

    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        startup_events = list(events)

    assert startup_events == [
        "managed:creation-suite-project-audit",
        "managed:creation-suite-prewrite-gate",
        "managed:creation-suite-handoff",
        "managed:creation-suite-writer-instruction",
        "change-set",
        "workflow-reconcile",
        "workflow-pause",
        "legacy-run",
        "source-import",
    ]
    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED
    checkpoints = fixture.store.list_workflow_checkpoints(fixture.run.run_id)
    assert len(checkpoints) == 1
    assert isinstance(checkpoints[0], WorkflowStepCheckpoint)
    assert checkpoints[0].step_ordinal == 1
    assert fixture.context_store.get_route_execution(
        fixture.pack.context_pack_id,
        call_ordinal=1,
    ) is not None


@pytest.mark.parametrize("initial_status", [RunStatus.QUEUED, RunStatus.STARTING])
def test_run_and_plan_commit_together_before_preexecution_startup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_status: RunStatus,
) -> None:
    fixture = build_fixture(
        tmp_path,
        prepare_injection=False,
        atomic_plan=True,
        transition_to_running=False,
    )
    if initial_status is RunStatus.STARTING:
        fixture.run = fixture.store.transition_run(
            fixture.run.run_id,
            RunStatus.STARTING,
            detail="Simulate a crash before execution begins",
        )
    assert fixture.run.status is initial_status
    assert fixture.store.get_workflow_plan(fixture.run.run_id) == fixture.plan

    async def unexpected_skill_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("queued startup recovery must not execute a Skill")

    monkeypatch.setattr(SkillExecutor, "execute", unexpected_skill_execute)
    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}/workflow"
        )
        assert response.status_code == 200
        assert response.json()["resume_allowed"] is True

    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED
    assert fixture.store.get_workflow_plan(fixture.run.run_id) == fixture.plan
    assert fixture.store.list_workflow_checkpoints(fixture.run.run_id) == []


def test_failed_startup_preserves_preexecution_workflow_for_later_resume(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(
        tmp_path,
        prepare_injection=False,
        atomic_plan=True,
        transition_to_running=False,
    )
    settings = app_settings(fixture).model_copy(update={"managed_skill_root": None})
    provider = StartupTrapProvider()
    app = create_app(settings, agent_provider=provider)

    with TestClient(app) as client:
        readiness = client.get("/health/ready")
        assert readiness.status_code == 503
        assert "Managed Skill executor is unavailable" in readiness.text
        assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED

    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED
    assert fixture.store.get_workflow_plan(fixture.run.run_id) == fixture.plan
    assert fixture.store.workflow_resume_blocker(fixture.run.run_id) is None


def test_paused_terminal_step_reconciles_after_executor_is_restored(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path)
    finish_step_without_reconciliation(fixture, step_ordinal=1)
    unavailable_settings = app_settings(fixture).model_copy(
        update={"managed_skill_root": None}
    )

    unavailable_app = create_app(
        unavailable_settings,
        agent_provider=StartupTrapProvider(),
    )
    with TestClient(unavailable_app) as client:
        assert client.get("/health/ready").status_code == 503
        assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED
        assert (
            fixture.store.workflow_resume_blocker(fixture.run.run_id)
            == "workflow_step_interrupted"
        )
        assert fixture.store.list_workflow_checkpoints(fixture.run.run_id) == []

    restored_app = create_app(
        app_settings(fixture),
        agent_provider=StartupTrapProvider(),
    )
    with TestClient(restored_app) as client:
        assert client.get("/health/ready").status_code == 200
        workflow = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}/workflow"
        )
        assert workflow.status_code == 200
        assert workflow.json()["resume_allowed"] is True

    checkpoints = fixture.store.list_workflow_checkpoints(fixture.run.run_id)
    assert len(checkpoints) == 1
    assert isinstance(checkpoints[0], WorkflowStepCheckpoint)
    assert checkpoints[0].step_ordinal == 1
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED


def test_generic_recovery_excludes_an_unlaunched_workflow_plan(tmp_path: Path) -> None:
    fixture = build_fixture(
        tmp_path,
        prepare_injection=False,
        atomic_plan=True,
        transition_to_running=False,
    )

    assert fixture.store.recover_interrupted() == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.QUEUED
    assert fixture.store.get_workflow_plan(fixture.run.run_id) == fixture.plan


def test_app_startup_preserves_explicitly_paused_workflow_without_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path)
    fixture.store.pause_workflow_for_restart(fixture.run.run_id)
    activities_before = fixture.store.list_activities(fixture.run.run_id)

    async def unexpected_skill_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("startup recovery must not execute a Skill")

    monkeypatch.setattr(SkillExecutor, "execute", unexpected_skill_execute)
    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.PAUSED
    assert fixture.store.list_activities(fixture.run.run_id) == activities_before


def test_app_startup_leaves_anomalous_workflow_state_to_legacy_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path)
    fixture.store.transition_run(
        fixture.run.run_id,
        RunStatus.AWAITING_APPROVAL,
        detail="Simulate an anomalous startup state",
    )

    def unexpected_reconcile(*args, **kwargs):
        del args, kwargs
        raise AssertionError("only a running workflow may be reconciled")

    async def unexpected_skill_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("startup recovery must not execute a Skill")

    monkeypatch.setattr(WorkflowRunner, "reconcile_startup", unexpected_reconcile)
    monkeypatch.setattr(SkillExecutor, "execute", unexpected_skill_execute)
    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED


def test_app_startup_interrupts_provider_boundary_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path)
    first, second, third = append_full_chain(fixture)
    _prepare_creation_writer_dispatch(fixture)
    boundary = build_provider_io_boundary(
        fixture.plan,
        (first, second, third),
        request_hash="1" * 64,
        provider_model="fixture-model",
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    fixture.store.create_provider_io_boundary(
        boundary,
        expected_previous_checkpoint_hash=third.checkpoint_hash,
        provider_messages_hash=PROVIDER_MESSAGES_HASH,
        tool_definitions_hash=TOOL_DEFINITIONS_HASH,
    )

    def unexpected_reconcile(*args, **kwargs):
        del args, kwargs
        raise AssertionError("a Provider boundary must never be replayed")

    async def unexpected_skill_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("startup recovery must not execute a Skill")

    monkeypatch.setattr(WorkflowRunner, "reconcile_startup", unexpected_reconcile)
    monkeypatch.setattr(SkillExecutor, "execute", unexpected_skill_execute)
    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200

    assert provider.call_count == 0
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED
    assert fixture.store.get_provider_io_boundary(fixture.run.run_id) == boundary
    assert fixture.store.list_nonterminal_workflow_run_ids() == []
    recovery = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert recovery.dossier.remote_result == "unknown"
    assert recovery.dossier.provider_replay_count == 0
    with fixture.database.connection() as connection:
        dossier_count = connection.execute(
            """
            SELECT COUNT(*) FROM run_event
            WHERE run_id = ? AND event_type = 'provider_recovery_dossier'
            """,
            (fixture.run.run_id,),
        ).fetchone()
    assert dossier_count is not None
    assert int(dossier_count[0]) == 1


def test_final_checkpoints_resume_across_app_restart_and_bind_provider_request(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(
        tmp_path,
        knowledge_fingerprint=None,
        publish_project_kb=True,
    )
    current_path, current_bytes = assert_published_project_kb_fixture(fixture)
    first, second, third = append_full_chain(fixture)
    assert fixture.store.list_workflow_checkpoints(fixture.run.run_id) == [
        first,
        second,
        third,
    ]

    provider = RecordingResumeProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)
    run_id = fixture.run.run_id
    with TestClient(app) as client:
        assert current_path.read_bytes() == current_bytes
        assert provider.call_count == 0
        assert fixture.store.get_run(run_id).status is RunStatus.PAUSED

        workflow_response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")
        assert workflow_response.status_code == 200, workflow_response.text
        workflow_state = workflow_response.json()
        assert workflow_state["run_status"] == "paused"
        assert workflow_state["resume_state"] == "resumable"
        assert workflow_state["resume_allowed"] is True
        assert workflow_state["provider_boundary"] is None
        assert workflow_state["next_step_ordinal"] is None
        assert len(workflow_state["checkpoints"]) == 3
        assert provider.call_count == 0

        resumed = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            json=resume_request(workflow_state),
        )
        assert resumed.status_code == 202, resumed.text
        assert resumed.json()["next_action"] == "provider_io"
        assert current_path.read_bytes() == current_bytes

        terminal = wait_for_terminal_run(client, run_id)
        assert terminal["status"] == "succeeded"
        assert provider.call_count == 1
        assert provider.started.is_set()
        assert current_path.read_bytes() == current_bytes

        boundary = fixture.store.get_provider_io_boundary(run_id)
        expected_request_hash = canonical_json_hash(
            {
                "schema": "zhimeng.provider-dispatch-material.v1",
                "messages": provider.messages,
                "tools": [
                    tool.model_dump(mode="json", by_alias=True)
                    for tool in provider.tools
                ],
                "provider_model": provider.model,
                "accepted_reported_models": [],
                "provider_dispatch_config_fingerprint": (
                    provider.dispatch_config_fingerprint
                ),
                "executor": provider.identity.model_dump(mode="json"),
            }
        )
        assert boundary.request_hash == expected_request_hash
        assert boundary.provider_model == provider.model
        assert boundary.provider_dispatch_config_fingerprint == (
            provider.dispatch_config_fingerprint
        )
        assert boundary.executor == provider.identity

        final_workflow = client.get(
            f"/api/v1/agent/runs/{run_id}/workflow"
        ).json()
        assert final_workflow["run_status"] == "succeeded"
        assert final_workflow["provider_boundary"] == boundary.model_dump(
            mode="json",
            by_alias=True,
        )

        activities_response = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        )
        assert activities_response.status_code == 200, activities_response.text
        activities = activities_response.json()["activities"]
        run_states = [item for item in activities if item["kind"] == "run_state"]
        restart_pairs = [
            (paused, running)
            for paused, running in zip(run_states, run_states[1:])
            if paused["payload"]["status"] == "paused"
            and running["payload"]["status"] == "running"
            and running["sequence"] == paused["sequence"] + 1
        ]
        assert len(restart_pairs) == 1
        paused_activity, running_activity = restart_pairs[0]
        assert running_activity["payload"]["previous_status"] == "paused"

        sse_response = client.get(f"/api/v1/agent/runs/{run_id}/events")
        assert sse_response.status_code == 200, sse_response.text
        sse_activities = [
            event["data"]["activity"]
            for event in parse_sse_payloads(sse_response.text)
            if event["event_type"] == "activity"
        ]
        assert paused_activity in sse_activities
        assert running_activity in sse_activities
        sse_run_states = [
            item for item in sse_activities if item["kind"] == "run_state"
        ]
        assert any(
            paused["payload"]["status"] == "paused"
            and running["payload"]["status"] == "running"
            and running["payload"]["previous_status"] == "paused"
            and running["sequence"] == paused["sequence"] + 1
            for paused, running in zip(sse_run_states, sse_run_states[1:])
        )


def test_concurrent_identical_resume_posts_claim_once_and_call_provider_once(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(
        tmp_path,
        knowledge_fingerprint=None,
        publish_project_kb=True,
    )
    current_path, current_bytes = assert_published_project_kb_fixture(fixture)
    append_full_chain(fixture)
    provider = RecordingResumeProvider(blocked=True)
    app = create_app(app_settings(fixture), agent_provider=provider)
    run_id = fixture.run.run_id

    with TestClient(app) as client:
        assert current_path.read_bytes() == current_bytes
        workflow_response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")
        assert workflow_response.status_code == 200, workflow_response.text
        workflow_state = workflow_response.json()
        assert workflow_state["run_status"] == "paused"
        assert provider.call_count == 0
        request = resume_request(workflow_state)
        start = Barrier(2)

        def post_resume() -> tuple[int, dict[str, object]]:
            start.wait(timeout=2)
            response = client.post(
                f"/api/v1/agent/runs/{run_id}/resume",
                json=request,
            )
            return response.status_code, response.json()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: post_resume(), range(2)))
            assert sorted(status_code for status_code, _ in results) == [202, 409]
            conflict = next(payload for status_code, payload in results if status_code == 409)
            assert conflict["error"]["code"] == "agent_run_already_running"
            assert provider.started.wait(timeout=5)
            assert provider.call_count == 1
            fixture.store.get_provider_io_boundary(run_id)
            assert current_path.read_bytes() == current_bytes
        finally:
            provider.release.set()

        terminal = wait_for_terminal_run(client, run_id)
        assert terminal["status"] == "succeeded"
        assert provider.call_count == 1
        assert current_path.read_bytes() == current_bytes
