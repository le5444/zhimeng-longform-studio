from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateContent,
    AgentCandidateOutput,
    AgentCandidateSubmission,
    agent_candidate_output_envelope_hash,
    agent_candidate_references_hash,
    build_agent_candidate_output,
)
from zhimeng_runtime.change_set_models import (
    CandidateFileChange,
    ChangeHunk,
    ChangeOperation,
    ChangeSetOrigin,
    ChangeSetResponse,
    ChangeSetStatus,
    HunkDecision,
    change_set_payload_hash,
)
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextPack,
    canonical_json_hash,
)
from zhimeng_runtime.creation_instruction_models import (
    CreationInstructionActivationBinding,
    CreationInstructionActivationScope,
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
    build_creation_instruction_activation_binding,
    build_creation_instruction_dispatch_receipt,
    build_creation_instruction_materialization,
)
from zhimeng_runtime.execution_protocol import ExecutorIdentity, ExecutorKind
from zhimeng_runtime.knowledge_models import knowledge_formalization_receipt_id
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.prewrite_models import (
    PrewriteOperationPackage,
    build_prewrite_operation_package,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef
from zhimeng_runtime.provider_turn_models import (
    ProviderTurnReceipt,
    build_provider_turn_receipt,
)
from zhimeng_runtime.skill_models import CreationHandoffResult
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
    build_workflow_plan,
    build_workflow_step,
    build_workflow_step_checkpoint,
)
from zhimeng_runtime.writer_models import (
    WriterCandidateResult,
    WriterTargetFile,
    WriterTask,
    build_writer_candidate_result,
    build_writer_task,
    creation_handoff_hash,
    writer_candidate_result_hash,
    writer_change_set_hash,
    writer_target_files_hash,
    writer_task_hash,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
PROJECT_ID = "project-" + "a" * 24
PROJECT_FINGERPRINT = canonical_json_hash({"project": "revision-7"})
KNOWLEDGE_FINGERPRINT = canonical_json_hash({"knowledge": "formalized-v1"})
RUN_ID = "run-writer-contract-001"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INSTRUCTION_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-writer-instruction"
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _provenance() -> KnowledgeProvenanceRef:
    source_import_id = "source-import-" + "1" * 32
    return KnowledgeProvenanceRef(
        source_import_id=source_import_id,
        source_receipt_id=source_import_receipt_id(source_import_id),
        source_receipt_hash="2" * 64,
        source_packet_fingerprint="3" * 64,
        cards_draft_hash="4" * 64,
        candidate_id="knowledge-candidate-" + "5" * 32,
        candidate_origin_hash="6" * 64,
        card_id="pressure-card",
        version_id="knowledge-version-" + "b" * 32,
        version_content_hash=_sha("pressure-card-content"),
        version_lineage_receipt_id="knowledge-lineage-" + "7" * 32,
        version_lineage_receipt_hash="8" * 64,
        formalization_receipt_id=knowledge_formalization_receipt_id(source_import_id),
        formalization_receipt_hash="9" * 64,
    )


def _context_pack(*, blocked: bool) -> ContextPack:
    provenance = _provenance()
    control_type = (
        "active_chapter_control"
        if blocked
        else "active_chapter_character_state"
    )
    control_title = (
        "\u672c\u7ae0\u7ae0\u7eb2\u63a7\u5236\u5361"
        if blocked
        else "\u4eba\u7269\u7ae0\u7eb2\u63a7\u5236\u5361"
    )
    request = ContextCreateRequest(
        objective="Draft the active chapter as an author-review-only ChangeSet.",
        profile="chapter",
        active_chapter_id="VOL001-CH001",
        expected_project_revision=7,
        expected_project_fingerprint=PROJECT_FINGERPRINT,
        project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project_kb_manifest_hash="b" * 64,
    )
    return compile_context_pack(
        context_pack_id="context-pack-writer-001",
        session_id="session-writer-001",
        project_id=PROJECT_ID,
        history_through_ordinal=0,
        request=request,
        history=(),
        candidates=(
            ContextCandidateFragment(
                source_id="active-chapter:VOL001-CH001:control",
                source_type=control_type,
                title=control_title,
                authority=ContextAuthority.PROJECT_TRUTH,
                version="revision-7",
                source_hash=_sha("chapter-control"),
                content="The protagonist must refuse the bargain in this scene.",
                priority=1_000,
                mandatory=True,
                eligible=True,
            ),
            ContextCandidateFragment(
                source_id="knowledge-card:pressure-card",
                source_type="native_knowledge_card:work",
                title="Pressure escalation",
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                version=provenance.version_id,
                source_hash=provenance.version_content_hash,
                content='{"summary":"Escalate observable pressure."}',
                priority=700,
                mandatory=False,
                eligible=True,
                knowledge_provenance=provenance,
            ),
        ),
        max_characters=50_000,
        max_tokens=50_000,
        knowledge_fingerprint=KNOWLEDGE_FINGERPRINT,
        created_at=NOW,
    )


def _route(pack: ContextPack) -> NlclRouteReceipt:
    fragment = next(
        item for item in pack.included if item.source_id == "knowledge-card:pressure-card"
    )
    card = NlclSelectedCard(
        card_id="pressure-card",
        card_type="work",
        title="Pressure escalation",
        version_id=fragment.version,
        content_hash=fragment.source_hash,
        route_terms=("pressure",),
        recommended_profiles=("chapter",),
        negative_routes=(),
        safety_summary="Use the transferable mechanism without copying prose.",
        feedback_layer="L3_DISTILLATION_RETRIEVAL",
        knowledge_provenance=fragment.knowledge_provenance,
    )
    return build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(card,),
    )


def _steps():
    skill_ids = (
        "novel-creation-suite.project-audit",
        "novel-creation-suite.prewrite-gate",
        "novel-creation-suite.creation-handoff",
    )
    names = ("project-audit", "prewrite-gate", "creation-handoff")
    return tuple(
        build_workflow_step(
            ordinal=ordinal,
            skill_id=skill_id,
            expected_skill_version_id=f"skill-version-{ordinal:032x}",
            expected_skill_version="1.0.0",
            expected_manifest_fingerprint=_sha(f"manifest-{ordinal}"),
            expected_package_fingerprint=_sha(f"package-{ordinal}"),
            input_contract=f"https://zhimeng.local/contracts/{name}-request.schema.json",
            output_contract=f"https://zhimeng.local/contracts/{name}-result.schema.json",
            input_builder_id=f"{name}-request",
            input_builder_version="1",
        )
        for ordinal, (skill_id, name) in enumerate(zip(skill_ids, names), start=1)
    )


def _plan(
    pack: ContextPack,
    route: NlclRouteReceipt,
    package: PrewriteOperationPackage,
    *,
    run_id: str,
) -> WorkflowPlan:
    return build_workflow_plan(
        run_id=run_id,
        project_id=pack.project_id,
        project_revision=pack.project_revision,
        project_fingerprint=pack.project_fingerprint,
        context_pack_id=pack.context_pack_id,
        context_pack_hash=pack.pack_hash,
        knowledge_fingerprint=pack.knowledge_fingerprint,
        route_receipt_id=route.route_receipt_id,
        route_receipt_hash=route.receipt_hash,
        operation_package_id=package.operation_package_id,
        operation_package_hash=package.package_hash,
        steps=_steps(),
        created_at=NOW + timedelta(seconds=1),
    )


def _checkpoints(plan: WorkflowPlan) -> tuple[WorkflowStepCheckpoint, ...]:
    checkpoints: list[WorkflowStepCheckpoint] = []
    previous: WorkflowStepCheckpoint | None = None
    for ordinal, step in enumerate(plan.steps, start=1):
        checkpoint = build_workflow_step_checkpoint(
            plan,
            step,
            attempt_ordinal=1,
            previous_checkpoint=previous,
            tool_call_id=f"tool-call-writer-{ordinal}",
            provider_tool_call_id=f"provider-tool-call-writer-{ordinal}",
            skill_call_id=f"skill-call-writer-{ordinal}",
            skill_receipt_id=f"skill-receipt-{ordinal:032x}",
            skill_receipt_hash=_sha(f"skill-receipt-{ordinal}"),
            route_execution_receipt_id=f"nlcl-route-exec-{ordinal:032x}",
            route_execution_receipt_hash=_sha(f"route-exec-{ordinal}"),
            input_hash=_sha(f"input-{ordinal}"),
            output_hash=_sha(f"output-{ordinal}"),
            tool_result_hash=_sha(f"tool-result-{ordinal}"),
            execution_status="succeeded",
            domain_status="degraded" if ordinal == 3 else "healthy",
            error_code=None,
            error_message=None,
            created_at=NOW + timedelta(seconds=ordinal + 1),
        )
        checkpoints.append(checkpoint)
        previous = checkpoint
    return tuple(checkpoints)


def _handoff(
    plan: WorkflowPlan,
    checkpoints: tuple[WorkflowStepCheckpoint, ...],
    pack: ContextPack,
    package: PrewriteOperationPackage,
) -> CreationHandoffResult:
    failures = list(package.missing_requirements) if package.readiness == "blocked" else []
    return CreationHandoffResult(
        handoff_status=package.readiness,
        formal_draft_allowed=False,
        allowed_output="repair_plan_or_risk_draft_only",
        workflow_plan_id=plan.workflow_plan_id,
        workflow_plan_hash=plan.plan_hash,
        context_pack_id=pack.context_pack_id,
        context_pack_hash=pack.pack_hash,
        route_receipt_id=package.route_receipt_id,
        route_receipt_hash=package.route_receipt_hash,
        operation_package_id=package.operation_package_id,
        operation_package_hash=package.package_hash,
        project_id=pack.project_id,
        project_revision=pack.project_revision,
        project_fingerprint=pack.project_fingerprint,
        active_chapter_id=pack.active_chapter_id,
        task_goal=package.task_goal,
        knowledge_fingerprint=pack.knowledge_fingerprint,
        evidence_ref_count=len(package.relevant_evidence_refs),
        must_obey_truth_count=len(package.must_obey_truth),
        current_character_state_count=len(package.current_character_state),
        project_audit_execution_receipt_hash=_sha("project-audit-exec"),
        prewrite_gate_execution_receipt_hash=_sha("prewrite-gate-exec"),
        workflow_checkpoint_hashes=[
            checkpoint.checkpoint_hash for checkpoint in checkpoints[:-1]
        ],
        prewrite_gate_status=package.readiness,
        failure_signatures=failures,
        next_actions=["Keep every proposal in author review staging."],
    )


def _target(*, suffix: str = "1") -> WriterTargetFile:
    return WriterTargetFile(
        file_id="file-" + suffix * 32,
        relative_path=(
            f"05_\u6b63\u6587\u5185\u5bb9/\u7b2c\u4e00\u5377/CH00{suffix}_\u6b63\u6587.md"
        ),
        content_hash=_sha(f"chapter-before-{suffix}"),
    )


@dataclass(frozen=True)
class WriterFixture:
    pack: ContextPack
    route: NlclRouteReceipt
    package: PrewriteOperationPackage
    plan: WorkflowPlan
    checkpoints: tuple[WorkflowStepCheckpoint, ...]
    handoff: CreationHandoffResult
    boundary: ProviderIoBoundary
    provider_turn_receipts: tuple[ProviderTurnReceipt, ...]


def _fixture(*, blocked: bool = False, run_id: str = RUN_ID) -> WriterFixture:
    pack = _context_pack(blocked=blocked)
    route = _route(pack)
    package = build_prewrite_operation_package(pack, route)
    assert package.readiness == ("blocked" if blocked else "risk_draft_only")
    plan = _plan(pack, route, package, run_id=run_id)
    checkpoints = _checkpoints(plan)
    handoff = _handoff(plan, checkpoints, pack, package)
    dispatch_fingerprint = _sha("provider-config")
    boundary = build_provider_io_boundary(
        plan,
        checkpoints,
        request_hash=_sha(f"provider-request-{run_id}"),
        provider_model="writer-fixture",
        provider_dispatch_config_fingerprint=dispatch_fingerprint,
        executor=ExecutorIdentity(
            executor_id="executor-writer-fixture",
            kind=ExecutorKind.REMOTE,
            implementation="test-provider",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{dispatch_fingerprint}",
        ),
        created_at=NOW + timedelta(minutes=1),
    )
    provider_turn_receipts = (
        build_provider_turn_receipt(
            boundary,
            turn_ordinal=1,
            request_hash=boundary.request_hash,
            reported_model=boundary.provider_model,
            accepted_reported_models=(),
            finish_reason="tool_calls",
            usage=None,
            created_at=NOW + timedelta(minutes=2),
        ),
    )
    return WriterFixture(
        pack,
        route,
        package,
        plan,
        checkpoints,
        handoff,
        boundary,
        provider_turn_receipts,
    )


def _task(
    fixture: WriterFixture,
    *,
    mode: str = "author_review_change_proposal",
    targets: tuple[WriterTargetFile, ...] = (_target(),),
) -> WriterTask:
    return build_writer_task(
        fixture.plan,
        fixture.checkpoints,
        handoff=fixture.handoff,
        context_pack=fixture.pack,
        route=fixture.route,
        operation_package=fixture.package,
        mode=mode,  # type: ignore[arg-type]
        target_files=targets,
        created_at=NOW + timedelta(seconds=30),
    )


def _instruction_evidence(
    fixture: WriterFixture,
    task: WriterTask,
) -> tuple[
    CreationInstructionActivationBinding,
    CreationInstructionMaterialization,
    CreationInstructionDispatchReceipt,
]:
    instruction_map_bytes = (INSTRUCTION_PACKAGE / "instruction-map.json").read_bytes()
    source_bytes = (
        INSTRUCTION_PACKAGE
        / "references"
        / "bundled-skills"
        / "novel-volume-folder"
        / "SKILL.md"
    ).read_bytes()
    skill_version_id = "skill-version-" + "1" * 32
    activation = build_creation_instruction_activation_binding(
        run_id=fixture.plan.run_id,
        project_id=fixture.plan.project_id,
        workflow_plan_id=fixture.plan.workflow_plan_id,
        workflow_plan_hash=fixture.plan.plan_hash,
        scope_chain=(
            CreationInstructionActivationScope(
                scope_type="project",
                scope_key=f"project:{fixture.plan.project_id}",
                configured_state="unconfigured",
                state_revision=0,
            ),
            CreationInstructionActivationScope(
                scope_type="application",
                scope_key="application",
                configured_state="enabled",
                state_revision=1,
                activation_receipt_id="skill-activation-" + "2" * 32,
                activation_receipt_hash=_sha("writer-instruction-activation"),
                configured_version_id=skill_version_id,
            ),
        ),
        effective_scope="application",
        skill_version_id=skill_version_id,
        skill_version="2026.07.23-o2.2",
        manifest_fingerprint=_sha("writer-instruction-manifest"),
        package_fingerprint=_sha("writer-instruction-package"),
        instruction_map_hash=hashlib.sha256(instruction_map_bytes).hexdigest(),
        created_at=NOW + timedelta(seconds=15),
    )
    materialization = build_creation_instruction_materialization(
        activation,
        task,
        instruction_map_bytes=instruction_map_bytes,
        source_bytes=source_bytes,
        created_at=NOW + timedelta(seconds=45),
    )
    dispatch = build_creation_instruction_dispatch_receipt(
        fixture.boundary,
        materialization,
        provider_messages_hash=_sha("writer-provider-messages"),
        tool_definitions_hash=_sha("writer-tool-definitions"),
        created_at=fixture.boundary.created_at,
    )
    return activation, materialization, dispatch


def _change_set(
    task: WriterTask,
    *,
    run_id: str | None = None,
    target: WriterTargetFile | None = None,
) -> ChangeSetResponse:
    bound_target = target or task.target_files[0]
    before_text = "Old chapter text.\n"
    after_text = "New chapter text under author review.\n"
    before_hunk_hash = hashlib.sha256(before_text.encode("utf-8")).hexdigest()
    after_hunk_hash = hashlib.sha256(after_text.encode("utf-8")).hexdigest()
    return ChangeSetResponse(
        change_set_id="changeset-" + "c" * 32,
        project_id=task.project_id,
        base_revision=task.project_revision,
        base_fingerprint=task.project_fingerprint,
        title="Author-review chapter proposal",
        summary="One bounded prose update; no direct project write.",
        origin=ChangeSetOrigin.AGENT,
        source_run_id=run_id or task.run_id,
        source_tool_call_id="tool-call-change-set-propose",
        source_request_hash=_sha("change-set-propose-request"),
        source_skill_ids=[],
        gate_required=True,
        status=ChangeSetStatus.REVIEWING,
        review_revision=0,
        changes=[
            CandidateFileChange(
                change_id="change-" + "d" * 32,
                operation=ChangeOperation.UPDATE,
                file_id=bound_target.file_id,
                relative_path=bound_target.relative_path,
                rationale="Produce a reviewable prose candidate.",
                before_sha256=bound_target.content_hash,
                proposed_after_sha256=_sha("chapter-after"),
                preserve_utf8_bom=False,
                post_draft_gate_scope="prose_body",
                hunks=[
                    ChangeHunk(
                        hunk_id="hunk-" + "e" * 32,
                        index=0,
                        before_start=0,
                        before_end=1,
                        after_start=0,
                        after_end=1,
                        before_text=before_text,
                        after_text=after_text,
                        before_sha256=before_hunk_hash,
                        after_sha256=after_hunk_hash,
                        decision=HunkDecision.PENDING,
                    )
                ],
            )
        ],
        post_draft_gate=None,
        writeback=None,
        rollback=None,
        error=None,
        created_at=NOW + timedelta(minutes=2),
        updated_at=NOW + timedelta(minutes=2),
    )


def _candidate(
    fixture: WriterFixture,
    task: WriterTask,
    *,
    kind: str,
    change_set: ChangeSetResponse | None = None,
) -> AgentCandidateOutput:
    _, materialization, dispatch = _instruction_evidence(fixture, task)
    return build_agent_candidate_output(
        fixture.plan,
        fixture.checkpoints,
        fixture.boundary,
        handoff=fixture.handoff,
        writer_task=task,
        instruction_materialization=materialization,
        instruction_dispatch_receipt=dispatch,
        provider_turn_receipts=fixture.provider_turn_receipts,
        change_set=change_set,
        submission=AgentCandidateSubmission(
            kind=kind,  # type: ignore[arg-type]
            content=AgentCandidateContent(
                title="Writer candidate",
                summary="Runtime-staged output requiring author review.",
                body="No project truth was written.",
            ),
        ),
        created_at=NOW + timedelta(minutes=3),
    )


def test_writer_task_binds_creation_context_route_knowledge_and_prose_targets() -> None:
    fixture = _fixture()
    target = _target()
    task = _task(fixture, targets=(target,))

    assert task.mode == "author_review_change_proposal"
    assert task.workflow_plan_id == fixture.plan.workflow_plan_id
    assert task.final_checkpoint_hash == fixture.checkpoints[-1].checkpoint_hash
    assert task.creation_handoff_hash == creation_handoff_hash(fixture.handoff)
    assert task.context_pack_hash == fixture.pack.pack_hash
    assert task.route_receipt_hash == fixture.route.receipt_hash
    assert task.operation_package_hash == fixture.package.package_hash
    assert task.knowledge_fingerprint == KNOWLEDGE_FINGERPRINT
    assert task.selected_card_ids == ("pressure-card",)
    assert task.selected_card_hashes == (fixture.route.selected_cards[0].content_hash,)
    assert task.target_files_hash == writer_target_files_hash((target,))
    assert task.formal_draft_allowed is False
    assert task.writes_project_truth is False
    assert task.requires_author_review is True
    assert writer_task_hash(task) == task.task_hash


def test_blocked_handoff_can_still_create_author_review_staging_task() -> None:
    fixture = _fixture(blocked=True)
    task = _task(fixture)

    assert task.creation_handoff_status == "blocked"
    assert task.failure_signatures
    assert task.mode == "author_review_change_proposal"
    assert task.writes_project_truth is False


def test_blocked_project_audit_handoff_can_bind_risk_ready_operation_package() -> None:
    fixture = _fixture(blocked=False)
    payload = fixture.handoff.model_dump(mode="python", by_alias=True)
    payload["handoff_status"] = "blocked"
    payload["prewrite_gate_status"] = "blocked"
    payload["failure_signatures"] = ["project_audit_blocked"]
    handoff = CreationHandoffResult.model_validate(payload)

    task = build_writer_task(
        fixture.plan,
        fixture.checkpoints,
        handoff=handoff,
        context_pack=fixture.pack,
        route=fixture.route,
        operation_package=fixture.package,
        mode="author_review_change_proposal",
        target_files=(_target(),),
        created_at=NOW + timedelta(minutes=2),
    )

    assert fixture.package.readiness == "risk_draft_only"
    assert task.creation_handoff_status == "blocked"
    assert task.mode == "author_review_change_proposal"


def test_writer_task_rejects_tamper_invalid_targets_and_cross_input_binding() -> None:
    fixture = _fixture()
    task = _task(fixture)
    payload = task.model_dump(mode="python", by_alias=True)
    payload["project_revision"] = 8
    with pytest.raises(ValidationError, match="task hash"):
        WriterTask.model_validate(payload)

    payload = task.model_dump(mode="python", by_alias=True)
    payload["writes_project_truth"] = True
    with pytest.raises(ValidationError):
        WriterTask.model_validate(payload)

    with pytest.raises(ValidationError, match="05_"):
        WriterTargetFile(
            file_id="file-" + "f" * 32,
            relative_path="04_\u5927\u7eb2/CH001_\u6b63\u6587.md",
            content_hash=_sha("outside-prose"),
        )

    with pytest.raises(ValueError, match="active chapter and prose targets"):
        _task(fixture, targets=())

    mismatched_route = fixture.route.model_copy(
        update={"context_pack_hash": _sha("wrong-context")}
    )
    with pytest.raises(ValueError, match="NLCL route hash"):
        build_writer_task(
            fixture.plan,
            fixture.checkpoints,
            handoff=fixture.handoff,
            context_pack=fixture.pack,
            route=mismatched_route,
            operation_package=fixture.package,
            mode="author_review_change_proposal",
            target_files=(_target(),),
            created_at=NOW + timedelta(minutes=2),
        )


def test_repair_only_is_explicit_and_forbids_changesets_without_requiring_blocked() -> None:
    fixture = _fixture(blocked=False)
    task = _task(fixture, mode="repair_only", targets=())
    candidate = _candidate(fixture, task, kind="repair_plan")
    result = build_writer_candidate_result(
        task,
        candidate,
        change_set=None,
        created_at=NOW + timedelta(minutes=4),
    )

    assert result.mode == "repair_only"
    assert result.change_set_id is None
    assert result.candidate_kind == "repair_plan"

    change_set = _change_set(_task(fixture))
    with pytest.raises(ValueError, match="forbids ChangeSets"):
        build_writer_candidate_result(
            task,
            candidate,
            change_set=change_set,
            created_at=NOW + timedelta(minutes=4),
        )


def test_writer_candidate_result_binds_one_same_run_changeset_and_exact_targets() -> None:
    fixture = _fixture(blocked=True)
    task = _task(fixture)
    change_set = _change_set(task)
    candidate = _candidate(
        fixture,
        task,
        kind="change_proposal",
        change_set=change_set,
    )
    result = build_writer_candidate_result(
        task,
        candidate,
        change_set=change_set,
        created_at=NOW + timedelta(minutes=4),
    )

    assert result.change_set_id == change_set.change_set_id
    assert result.change_set_hash == writer_change_set_hash(change_set)
    assert result.change_set_hash == change_set_payload_hash(change_set)
    assert result.target_files == task.target_files
    assert result.candidate_output_hash == candidate.envelope_hash
    assert result.writer_task_hash == task.task_hash
    assert result.writes_project_truth is False
    assert writer_candidate_result_hash(result) == result.result_hash


def test_writer_candidate_rejects_missing_duplicate_cross_run_and_stale_target_bindings() -> None:
    fixture = _fixture()
    task = _task(fixture)
    change_set = _change_set(task)

    with pytest.raises(ValueError, match="requires a change proposal and ChangeSet"):
        _candidate(fixture, task, kind="change_proposal")

    valid_candidate = _candidate(
        fixture,
        task,
        kind="change_proposal",
        change_set=change_set,
    )
    duplicate_references = (
        *valid_candidate.references,
        valid_candidate.references[-1],
    )
    duplicate_draft = valid_candidate.model_copy(
        update={
            "references": duplicate_references,
            "references_hash": agent_candidate_references_hash(duplicate_references),
            "envelope_hash": "0" * 64,
        }
    )
    duplicate_candidate = duplicate_draft.model_copy(
        update={
            "envelope_hash": agent_candidate_output_envelope_hash(duplicate_draft)
        }
    )
    with pytest.raises(ValidationError, match="unique|canonical"):
        AgentCandidateOutput.model_validate(
            duplicate_candidate.model_dump(mode="python", by_alias=False)
        )

    cross_run = _change_set(task, run_id="run-writer-contract-other")
    with pytest.raises(ValueError, match="does not bind the Writer project revision"):
        _candidate(
            fixture,
            task,
            kind="change_proposal",
            change_set=cross_run,
        )

    stale_target = task.target_files[0].model_copy(
        update={"content_hash": _sha("stale-before")}
    )
    stale_change_set = _change_set(task, target=stale_target)
    stale_candidate = _candidate(
        fixture,
        task,
        kind="change_proposal",
        change_set=stale_change_set,
    )
    with pytest.raises(ValueError, match="stale prose target"):
        build_writer_candidate_result(
            task,
            stale_candidate,
            change_set=stale_change_set,
            created_at=NOW + timedelta(minutes=4),
        )


def test_writer_candidate_result_rejects_hash_and_mode_tampering() -> None:
    fixture = _fixture()
    task = _task(fixture)
    change_set = _change_set(task)
    candidate = _candidate(
        fixture,
        task,
        kind="change_proposal",
        change_set=change_set,
    )
    result = build_writer_candidate_result(
        task,
        candidate,
        change_set=change_set,
        created_at=NOW + timedelta(minutes=4),
    )

    payload = result.model_dump(mode="python", by_alias=True)
    payload["target_files"][0]["content_hash"] = _sha("tampered-target")
    with pytest.raises(ValidationError, match="target-files hash"):
        WriterCandidateResult.model_validate(payload)

    payload = result.model_dump(mode="python", by_alias=True)
    payload["candidate_kind"] = "repair_plan"
    with pytest.raises(ValidationError, match="change_proposal"):
        WriterCandidateResult.model_validate(payload)

    tampered_candidate = candidate.model_copy(update={"project_revision": 8})
    with pytest.raises(ValueError, match="candidate output hash"):
        build_writer_candidate_result(
            task,
            tampered_candidate,
            change_set=change_set,
            created_at=NOW + timedelta(minutes=4),
        )
