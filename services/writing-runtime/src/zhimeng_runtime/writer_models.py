from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateOutput,
    agent_candidate_output_envelope_hash,
)
from zhimeng_runtime.change_set_models import (
    ChangeOperation,
    ChangeSetOrigin,
    ChangeSetResponse,
    ChangeSetStatus,
    FileId,
    RelativeProjectPath,
    change_set_payload_hash,
)
from zhimeng_runtime.context_models import (
    ContextPack,
    Sha256,
    canonical_json_hash,
    context_pack_hash,
)
from zhimeng_runtime.execution_protocol import StableId
from zhimeng_runtime.nlcl_models import NlclRouteReceipt, nlcl_route_receipt_hash
from zhimeng_runtime.prewrite_models import (
    PrewriteOperationPackage,
    prewrite_operation_package_hash,
)
from zhimeng_runtime.skill_models import (
    CreationHandoffResult,
)
from zhimeng_runtime.workflow_models import (
    WorkflowPlan,
    WorkflowStepCheckpoint,
    validate_workflow_checkpoint_chain,
)


WriterMode: TypeAlias = Literal[
    "repair_only",
    "author_review_change_proposal",
]
WriterCandidateKind: TypeAlias = Literal["repair_plan", "change_proposal"]

_CREATION_SKILL_CHAIN = (
    "novel-creation-suite.project-audit",
    "novel-creation-suite.prewrite-gate",
    "novel-creation-suite.creation-handoff",
)


def _writer_task_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "the Writer task binds one completed three-step Creation workflow",
        "Writer intent and the presence of bound prose targets determine the mode",
        "an upstream blocked handoff remains risk evidence and does not authorize project writes",
        "author_review_change_proposal requires bound targets; prose targets additionally require an active chapter",
        "selected card IDs and hashes preserve the canonical NLCL route order",
        "formal_draft_allowed and writes_project_truth remain false",
        "task_hash hashes every field except task_hash",
    ]


def _writer_result_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "the result binds one immutable Writer task and Agent candidate by identity and hash",
        "author_review_change_proposal requires exactly one same-Run Agent ChangeSet",
        "the ChangeSet updates only task-bound targets from their bound hashes; prose targets are complete and project-file targets may be a selected subset",
        "repair_only permits only repair_plan and forbids every ChangeSet identity",
        "formal_draft_allowed and writes_project_truth remain false",
        "result_hash hashes every field except result_hash",
    ]


class WriterModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class WriterTargetFile(WriterModel):
    file_id: FileId
    relative_path: RelativeProjectPath
    content_hash: Sha256
    content_role: Literal["prose_body", "project_file"] = "prose_body"

    @model_validator(mode="after")
    def validate_target_path(self) -> "WriterTargetFile":
        if self.content_role == "prose_body" and not self.relative_path.startswith("05_\u6b63\u6587\u5185\u5bb9/"):
            raise ValueError("Writer prose targets must remain inside 05_\u6b63\u6587\u5185\u5bb9")
        if self.content_role == "project_file" and self.relative_path.startswith("05_\u6b63\u6587\u5185\u5bb9/"):
            raise ValueError("Writer prose files must use the prose_body target role")
        allowed_extensions = (".md", ".txt") if self.content_role == "prose_body" else (
            ".json", ".md", ".txt", ".yaml", ".yml"
        )
        if not self.relative_path.casefold().endswith(allowed_extensions):
            raise ValueError("Writer targets must be supported text files")
        return self


class WriterTask(WriterModel):
    model_config = ConfigDict(json_schema_extra=_writer_task_schema_invariants)

    schema_id: Literal["zhimeng.writer-task.v1"] = Field(
        default="zhimeng.writer-task.v1",
        alias="schema",
    )
    writer_task_id: str = Field(pattern=r"^writer-task-[0-9a-f]{32}$")
    mode: WriterMode
    run_id: StableId
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    final_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    final_checkpoint_hash: Sha256
    creation_handoff_hash: Sha256
    creation_handoff_status: Literal["blocked", "risk_draft_only"]
    context_pack_id: StableId
    context_pack_hash: Sha256
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: Sha256
    project_id: StableId
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_goal: str = Field(min_length=1, max_length=100_000)
    failure_signatures: tuple[str, ...] = Field(default=(), max_length=64)
    target_files: tuple[WriterTargetFile, ...] = Field(default=(), max_length=32)
    target_files_hash: Sha256
    knowledge_fingerprint: Sha256 | None = None
    selected_card_ids: tuple[str, ...] = Field(default=(), max_length=24)
    selected_card_hashes: tuple[Sha256, ...] = Field(default=(), max_length=24)
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    requires_author_review: Literal[True] = True
    created_at: datetime
    task_hash: Sha256

    @field_validator("task_goal")
    @classmethod
    def require_non_blank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Writer task goal must not be blank")
        return value

    @field_validator("failure_signatures")
    @classmethod
    def validate_failure_signatures(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 256 for value in values):
            raise ValueError("Writer failure signatures must be bounded non-blank text")
        if len(values) != len(set(values)):
            raise ValueError("Writer failure signatures must be unique")
        return values

    @model_validator(mode="after")
    def validate_task(self) -> "WriterTask":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Writer task created_at must be timezone-aware")
        _validate_target_files(self.target_files)
        if self.target_files_hash != writer_target_files_hash(self.target_files):
            raise ValueError("Writer target-files hash does not match its targets")
        if len(self.selected_card_ids) != len(self.selected_card_hashes):
            raise ValueError("Writer selected card IDs and hashes must pair")
        if self.selected_card_ids != tuple(sorted(self.selected_card_ids)):
            raise ValueError("Writer selected card IDs must use canonical order")
        if len(self.selected_card_ids) != len(set(self.selected_card_ids)):
            raise ValueError("Writer selected card IDs must be unique")
        if self.selected_card_ids and self.knowledge_fingerprint is None:
            raise ValueError("Writer selected cards require a knowledge fingerprint")
        if self.knowledge_fingerprint is None and self.selected_card_hashes:
            raise ValueError("Writer selected card hashes require a knowledge fingerprint")
        if self.mode == "author_review_change_proposal":
            if not self.target_files:
                raise ValueError(
                    "author-review Writer tasks require prose targets or selected project files"
                )
            if (
                self.active_chapter_id is None
                and any(target.content_role == "prose_body" for target in self.target_files)
            ):
                raise ValueError(
                    "author-review Writer prose targets require an active chapter"
                )
        expected_id = writer_task_id(
            run_id=self.run_id,
            final_checkpoint_id=self.final_checkpoint_id,
            creation_handoff_hash=self.creation_handoff_hash,
            mode=self.mode,
            target_files_hash=self.target_files_hash,
        )
        if self.writer_task_id != expected_id:
            raise ValueError("Writer task identity does not match immutable inputs")
        if self.task_hash != writer_task_hash(self):
            raise ValueError("Writer task hash does not match immutable material")
        return self


class WriterCandidateResult(WriterModel):
    model_config = ConfigDict(json_schema_extra=_writer_result_schema_invariants)

    schema_id: Literal["zhimeng.writer-candidate-result.v1"] = Field(
        default="zhimeng.writer-candidate-result.v1",
        alias="schema",
    )
    writer_candidate_result_id: str = Field(
        pattern=r"^writer-candidate-result-[0-9a-f]{32}$"
    )
    mode: WriterMode
    run_id: StableId
    project_id: StableId
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    writer_task_id: str = Field(pattern=r"^writer-task-[0-9a-f]{32}$")
    writer_task_hash: Sha256
    candidate_output_id: str = Field(pattern=r"^agent-candidate-[0-9a-f]{32}$")
    candidate_output_hash: Sha256
    candidate_kind: WriterCandidateKind
    change_set_id: str | None = Field(
        default=None,
        pattern=r"^changeset-[0-9a-f]{32}$",
    )
    change_set_hash: Sha256 | None = None
    target_files: tuple[WriterTargetFile, ...] = Field(default=(), max_length=32)
    target_files_hash: Sha256
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    requires_author_review: Literal[True] = True
    created_at: datetime
    result_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> "WriterCandidateResult":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Writer candidate result created_at must be timezone-aware")
        _validate_target_files(self.target_files)
        if self.target_files_hash != writer_target_files_hash(self.target_files):
            raise ValueError("Writer result target-files hash does not match")
        if (self.change_set_id is None) != (self.change_set_hash is None):
            raise ValueError("Writer ChangeSet identity and hash must pair")
        if self.mode == "repair_only":
            if self.candidate_kind != "repair_plan":
                raise ValueError("repair-only Writer results require a repair_plan")
            if self.change_set_id is not None:
                raise ValueError("repair-only Writer results forbid ChangeSets")
        else:
            if self.candidate_kind != "change_proposal":
                raise ValueError("author-review Writer results require change_proposal")
            if self.change_set_id is None or not self.target_files:
                raise ValueError(
                    "author-review Writer results require a ChangeSet and prose targets or selected project files"
                )
        expected_id = writer_candidate_result_id(
            writer_task_id=self.writer_task_id,
            candidate_output_id=self.candidate_output_id,
            change_set_id=self.change_set_id,
        )
        if self.writer_candidate_result_id != expected_id:
            raise ValueError("Writer candidate result identity does not match its inputs")
        if self.result_hash != writer_candidate_result_hash(self):
            raise ValueError("Writer candidate result hash does not match immutable material")
        return self


def _derived_id(prefix: str, material: object) -> str:
    return f"{prefix}-{canonical_json_hash(material)[:32]}"


def _validate_target_files(target_files: tuple[WriterTargetFile, ...]) -> None:
    canonical = tuple(sorted(target_files, key=lambda item: item.relative_path.casefold()))
    if target_files != canonical:
        raise ValueError("Writer target files must use canonical path order")
    file_ids = tuple(item.file_id for item in target_files)
    paths = tuple(item.relative_path.casefold() for item in target_files)
    if len(file_ids) != len(set(file_ids)):
        raise ValueError("Writer target file IDs must be unique")
    if len(paths) != len(set(paths)):
        raise ValueError("Writer target paths must be unique")


def writer_target_files_hash(target_files: tuple[WriterTargetFile, ...]) -> str:
    return canonical_json_hash(
        [target.model_dump(mode="json", by_alias=True) for target in target_files]
    )


def creation_handoff_hash(handoff: CreationHandoffResult) -> str:
    return canonical_json_hash(handoff.model_dump(mode="json", by_alias=True))


def writer_task_id(
    *,
    run_id: str,
    final_checkpoint_id: str,
    creation_handoff_hash: str,
    mode: WriterMode,
    target_files_hash: str,
) -> str:
    return _derived_id(
        "writer-task",
        {
            "schema": "zhimeng.writer-task.v1",
            "run_id": run_id,
            "final_checkpoint_id": final_checkpoint_id,
            "creation_handoff_hash": creation_handoff_hash,
            "mode": mode,
            "target_files_hash": target_files_hash,
        },
    )


def writer_task_hash(task: WriterTask) -> str:
    return canonical_json_hash(
        task.model_dump(mode="json", by_alias=True, exclude={"task_hash"})
    )


def writer_change_set_hash(change_set: ChangeSetResponse) -> str:
    return change_set_payload_hash(change_set)


def writer_candidate_result_id(
    *,
    writer_task_id: str,
    candidate_output_id: str,
    change_set_id: str | None,
) -> str:
    return _derived_id(
        "writer-candidate-result",
        {
            "schema": "zhimeng.writer-candidate-result.v1",
            "writer_task_id": writer_task_id,
            "candidate_output_id": candidate_output_id,
            "change_set_id": change_set_id,
        },
    )


def writer_candidate_result_hash(result: WriterCandidateResult) -> str:
    return canonical_json_hash(
        result.model_dump(mode="json", by_alias=True, exclude={"result_hash"})
    )


def build_writer_task(
    plan: WorkflowPlan,
    checkpoints: tuple[WorkflowStepCheckpoint, ...],
    *,
    handoff: CreationHandoffResult,
    context_pack: ContextPack,
    route: NlclRouteReceipt,
    operation_package: PrewriteOperationPackage,
    mode: WriterMode,
    target_files: tuple[WriterTargetFile, ...],
    created_at: datetime,
) -> WriterTask:
    validate_workflow_checkpoint_chain(plan, checkpoints)
    if not checkpoints:
        raise ValueError("Writer task requires the completed Creation workflow")
    if tuple(step.skill_id for step in plan.steps) != _CREATION_SKILL_CHAIN:
        raise ValueError("Writer task requires the canonical three-step Creation workflow")
    final_checkpoint = checkpoints[-1]
    if (
        len(checkpoints) != len(plan.steps)
        or final_checkpoint.step_ordinal != len(plan.steps)
        or final_checkpoint.execution_status != "succeeded"
    ):
        raise ValueError("Writer task requires a successful final Creation checkpoint")
    if context_pack_hash(context_pack) != context_pack.pack_hash:
        raise ValueError("Writer task Context Pack hash is invalid")
    if nlcl_route_receipt_hash(route) != route.receipt_hash:
        raise ValueError("Writer task NLCL route hash is invalid")
    if prewrite_operation_package_hash(operation_package) != operation_package.package_hash:
        raise ValueError("Writer task operation package hash is invalid")

    expected_checkpoint_hashes = [
        checkpoint.checkpoint_hash for checkpoint in checkpoints[:-1]
    ]
    if handoff.workflow_checkpoint_hashes != expected_checkpoint_hashes:
        raise ValueError("Creation handoff does not bind the preceding checkpoints")

    project_identity = (
        plan.project_id,
        plan.project_revision,
        plan.project_fingerprint,
    )
    if any(
        identity != project_identity
        for identity in (
            (
                context_pack.project_id,
                context_pack.project_revision,
                context_pack.project_fingerprint,
            ),
            (route.project_id, route.project_revision, route.project_fingerprint),
            (
                operation_package.project_id,
                operation_package.project_revision,
                operation_package.project_fingerprint,
            ),
            (handoff.project_id, handoff.project_revision, handoff.project_fingerprint),
        )
    ):
        raise ValueError("Writer task inputs do not share project revision identity")

    if (
        plan.context_pack_id != context_pack.context_pack_id
        or plan.context_pack_hash != context_pack.pack_hash
        or route.context_pack_id != context_pack.context_pack_id
        or route.context_pack_hash != context_pack.pack_hash
        or operation_package.context_pack_id != context_pack.context_pack_id
        or operation_package.context_pack_hash != context_pack.pack_hash
        or handoff.context_pack_id != context_pack.context_pack_id
        or handoff.context_pack_hash != context_pack.pack_hash
    ):
        raise ValueError("Writer task inputs do not share Context Pack identity")
    if (
        plan.route_receipt_id != route.route_receipt_id
        or plan.route_receipt_hash != route.receipt_hash
        or operation_package.route_receipt_id != route.route_receipt_id
        or operation_package.route_receipt_hash != route.receipt_hash
        or handoff.route_receipt_id != route.route_receipt_id
        or handoff.route_receipt_hash != route.receipt_hash
    ):
        raise ValueError("Writer task inputs do not share NLCL route identity")
    if (
        plan.operation_package_id != operation_package.operation_package_id
        or plan.operation_package_hash != operation_package.package_hash
        or handoff.operation_package_id != operation_package.operation_package_id
        or handoff.operation_package_hash != operation_package.package_hash
    ):
        raise ValueError("Writer task inputs do not share operation-package identity")
    if (
        handoff.workflow_plan_id != plan.workflow_plan_id
        or handoff.workflow_plan_hash != plan.plan_hash
    ):
        raise ValueError("Creation handoff does not bind the Workflow plan")

    active_chapters = {
        context_pack.active_chapter_id,
        route.active_chapter_id,
        operation_package.active_chapter_id,
        handoff.active_chapter_id,
    }
    if len(active_chapters) != 1:
        raise ValueError("Writer task inputs do not share the active chapter")
    knowledge_fingerprints = {
        plan.knowledge_fingerprint,
        context_pack.knowledge_fingerprint,
        route.knowledge_fingerprint,
        handoff.knowledge_fingerprint,
    }
    if len(knowledge_fingerprints) != 1:
        raise ValueError("Writer task inputs do not share the knowledge fingerprint")

    selected_card_ids = tuple(card.card_id for card in route.selected_cards)
    selected_card_hashes = tuple(card.content_hash for card in route.selected_cards)
    if (
        operation_package.style_output_contract.selected_card_ids
        != selected_card_ids
        or operation_package.style_output_contract.selected_card_hashes
        != selected_card_hashes
    ):
        raise ValueError("Writer task operation package does not bind selected cards")
    if operation_package.task_goal != handoff.task_goal:
        raise ValueError("Writer task inputs do not share the task goal")
    if handoff.formal_draft_allowed is not False:
        raise ValueError("Creation handoff cannot authorize formal prose")
    _validate_target_files(target_files)
    targets_hash = writer_target_files_hash(target_files)
    handoff_hash = creation_handoff_hash(handoff)
    material = {
        "schema_id": "zhimeng.writer-task.v1",
        "writer_task_id": writer_task_id(
            run_id=plan.run_id,
            final_checkpoint_id=final_checkpoint.checkpoint_id,
            creation_handoff_hash=handoff_hash,
            mode=mode,
            target_files_hash=targets_hash,
        ),
        "mode": mode,
        "run_id": plan.run_id,
        "workflow_plan_id": plan.workflow_plan_id,
        "workflow_plan_hash": plan.plan_hash,
        "final_checkpoint_id": final_checkpoint.checkpoint_id,
        "final_checkpoint_hash": final_checkpoint.checkpoint_hash,
        "creation_handoff_hash": handoff_hash,
        "creation_handoff_status": handoff.handoff_status,
        "context_pack_id": context_pack.context_pack_id,
        "context_pack_hash": context_pack.pack_hash,
        "route_receipt_id": route.route_receipt_id,
        "route_receipt_hash": route.receipt_hash,
        "operation_package_id": operation_package.operation_package_id,
        "operation_package_hash": operation_package.package_hash,
        "project_id": plan.project_id,
        "project_revision": plan.project_revision,
        "project_fingerprint": plan.project_fingerprint,
        "active_chapter_id": handoff.active_chapter_id,
        "task_goal": handoff.task_goal,
        "failure_signatures": tuple(handoff.failure_signatures),
        "target_files": target_files,
        "target_files_hash": targets_hash,
        "knowledge_fingerprint": handoff.knowledge_fingerprint,
        "selected_card_ids": selected_card_ids,
        "selected_card_hashes": selected_card_hashes,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "requires_author_review": True,
        "created_at": created_at,
    }
    draft = WriterTask.model_construct(**material, task_hash="0" * 64)
    return WriterTask(**material, task_hash=writer_task_hash(draft))


def _validate_candidate_binding(
    task: WriterTask,
    candidate: AgentCandidateOutput,
) -> None:
    if writer_task_hash(task) != task.task_hash:
        raise ValueError("Writer task hash is invalid")
    if agent_candidate_output_envelope_hash(candidate) != candidate.envelope_hash:
        raise ValueError("Agent candidate output hash is invalid")
    if (
        candidate.run_id != task.run_id
        or candidate.workflow_plan_id != task.workflow_plan_id
        or candidate.workflow_plan_hash != task.workflow_plan_hash
        or candidate.final_checkpoint_id != task.final_checkpoint_id
        or candidate.final_checkpoint_hash != task.final_checkpoint_hash
        or candidate.context_pack_id != task.context_pack_id
        or candidate.context_pack_hash != task.context_pack_hash
        or candidate.operation_package_id != task.operation_package_id
        or candidate.operation_package_hash != task.operation_package_hash
        or candidate.project_id != task.project_id
        or candidate.project_revision != task.project_revision
        or candidate.project_fingerprint != task.project_fingerprint
        or candidate.handoff_status != task.creation_handoff_status
    ):
        raise ValueError("Agent candidate output does not bind the Writer task")
    task_references = [
        reference
        for reference in candidate.references
        if reference.reference_kind == "runtime_identity"
        and reference.reference_id == task.writer_task_id
    ]
    if (
        len(task_references) != 1
        or task_references[0].reference_hash != task.task_hash
    ):
        raise ValueError("Agent candidate output requires exactly one Writer-task reference")


def _validate_change_set_binding(
    task: WriterTask,
    candidate: AgentCandidateOutput,
    change_set: ChangeSetResponse,
) -> str:
    change_hash = writer_change_set_hash(change_set)
    change_references = [
        reference
        for reference in candidate.references
        if reference.reference_kind == "change_set"
    ]
    if (
        len(change_references) != 1
        or change_references[0].reference_id != change_set.change_set_id
        or change_references[0].reference_hash != change_hash
    ):
        raise ValueError("Writer candidate requires exactly one matching ChangeSet reference")
    if (
        change_set.origin is not ChangeSetOrigin.AGENT
        or change_set.source_run_id != task.run_id
        or change_set.project_id != task.project_id
        or change_set.base_revision != task.project_revision
        or change_set.base_fingerprint != task.project_fingerprint
        or change_set.status is not ChangeSetStatus.REVIEWING
        or not change_set.gate_required
    ):
        raise ValueError("Writer ChangeSet does not bind the same Run and project revision")

    expected = {target.relative_path.casefold(): target for target in task.target_files}
    has_prose_target = any(
        target.content_role == "prose_body" for target in task.target_files
    )
    changed_paths = {
        change.relative_path.casefold() for change in change_set.changes
    }
    required_prose_paths = {
        path
        for path, target in expected.items()
        if target.content_role == "prose_body"
    }
    if (
        not change_set.changes
        or not required_prose_paths.issubset(changed_paths)
        or len(change_set.changes) > len(expected)
    ):
        raise ValueError("Writer ChangeSet must update the task target files")
    for change in change_set.changes:
        target = expected.get(change.relative_path.casefold())
        if (
            target is None
            or change.operation is not ChangeOperation.UPDATE
            or change.file_id != target.file_id
            or change.before_sha256 != target.content_hash
            or change.post_draft_gate_scope != (
                "prose_body" if target.content_role == "prose_body" else "none"
            )
        ):
            raise ValueError(
                "Writer ChangeSet contains an unbound or stale prose target or project-file target"
            )
    return change_hash


def build_writer_candidate_result(
    task: WriterTask,
    candidate: AgentCandidateOutput,
    *,
    change_set: ChangeSetResponse | None,
    created_at: datetime,
) -> WriterCandidateResult:
    _validate_candidate_binding(task, candidate)
    change_set_id: str | None = None
    change_set_hash: str | None = None
    change_references = [
        reference
        for reference in candidate.references
        if reference.reference_kind == "change_set"
    ]
    if task.mode == "repair_only":
        if candidate.kind != "repair_plan":
            raise ValueError("repair-only Writer mode requires a repair_plan candidate")
        if change_set is not None or change_references:
            raise ValueError("repair-only Writer mode forbids ChangeSets")
    else:
        if candidate.kind != "change_proposal" or change_set is None:
            raise ValueError(
                "author-review Writer mode requires a change_proposal and ChangeSet"
            )
        change_set_id = change_set.change_set_id
        change_set_hash = _validate_change_set_binding(task, candidate, change_set)

    material = {
        "schema_id": "zhimeng.writer-candidate-result.v1",
        "writer_candidate_result_id": writer_candidate_result_id(
            writer_task_id=task.writer_task_id,
            candidate_output_id=candidate.candidate_output_id,
            change_set_id=change_set_id,
        ),
        "mode": task.mode,
        "run_id": task.run_id,
        "project_id": task.project_id,
        "project_revision": task.project_revision,
        "project_fingerprint": task.project_fingerprint,
        "writer_task_id": task.writer_task_id,
        "writer_task_hash": task.task_hash,
        "candidate_output_id": candidate.candidate_output_id,
        "candidate_output_hash": candidate.envelope_hash,
        "candidate_kind": candidate.kind,
        "change_set_id": change_set_id,
        "change_set_hash": change_set_hash,
        "target_files": task.target_files,
        "target_files_hash": task.target_files_hash,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "requires_author_review": True,
        "created_at": created_at,
    }
    draft = WriterCandidateResult.model_construct(**material, result_hash="0" * 64)
    return WriterCandidateResult(
        **material,
        result_hash=writer_candidate_result_hash(draft),
    )


__all__ = [
    "WriterCandidateKind",
    "WriterCandidateResult",
    "WriterMode",
    "WriterTargetFile",
    "WriterTask",
    "build_writer_candidate_result",
    "build_writer_task",
    "creation_handoff_hash",
    "writer_candidate_result_hash",
    "writer_candidate_result_id",
    "writer_change_set_hash",
    "writer_target_files_hash",
    "writer_task_hash",
    "writer_task_id",
]
