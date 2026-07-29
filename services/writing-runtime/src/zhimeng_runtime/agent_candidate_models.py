from __future__ import annotations

from datetime import datetime
import json
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from zhimeng_runtime.change_set_models import (
    ChangeSetResponse,
    change_set_payload_hash,
)
from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.creation_instruction_models import (
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
)
from zhimeng_runtime.execution_protocol import StableId
from zhimeng_runtime.skill_models import CreationHandoffResult
from zhimeng_runtime.provider_turn_models import (
    ProviderTurnReceipt,
    ProviderTurnReceiptBinding,
    provider_turn_receipt_chain_hash,
)
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    validate_workflow_checkpoint_chain,
)

if TYPE_CHECKING:
    from zhimeng_runtime.writer_models import WriterTask


AgentCandidateKind = Literal[
    "repair_plan",
    "risk_draft",
    "change_proposal",
    "next_step_advice",
]
AgentCandidateReferenceKind = Literal[
    "workflow_checkpoint",
    "runtime_identity",
    "artifact",
    "change_set",
]
CandidateFailureSignature = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
]


def _candidate_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "candidate_output_id is derived from run_id and provider_boundary_id",
        "content_hash hashes the canonical content object",
        "references_hash hashes the ordered canonical references",
        "reference identities are unique within one envelope",
        "references must resolve to Runtime identities or same-Run persisted evidence",
        "envelope_hash hashes every field except envelope_hash",
        "Runtime-owned identities must match one persisted managed workflow chain",
        "v3 candidates bind the Runtime Writer task, instruction materialization and atomic dispatch receipt",
        "v3 references contain exactly one Runtime Writer-task identity and one instruction artifact",
    ]


def _legacy_candidate_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "legacy v1 records bind only the pre-dispatch Provider boundary and request hash",
        "legacy v1 records are read-only and their actual response model identity is unverified",
        "envelope_hash hashes every field except envelope_hash",
    ]


class AgentCandidateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class AgentCandidateContent(AgentCandidateModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=8_000)
    body: str = Field(min_length=1, max_length=500_000)
    format: Literal["markdown"] = "markdown"

    @field_validator("title", "summary", "body")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate content must not be blank")
        return value


class AgentCandidateReference(AgentCandidateModel):
    reference_kind: AgentCandidateReferenceKind
    reference_id: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    reference_hash: Sha256


class AgentCandidateSubmission(AgentCandidateModel):
    """Provider-owned fields accepted by the future Runtime ToolCall."""

    kind: AgentCandidateKind
    content: AgentCandidateContent


class AgentCandidateOutputV1(AgentCandidateModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        json_schema_extra=_legacy_candidate_schema_invariants,
    )

    schema_id: Literal["zhimeng.agent-candidate-output.v1"] = Field(
        alias="schema",
    )
    candidate_output_id: str = Field(pattern=r"^agent-candidate-[0-9a-f]{32}$")
    run_id: StableId
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    final_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    final_checkpoint_hash: Sha256
    provider_boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    provider_boundary_hash: Sha256
    provider_request_hash: Sha256
    context_pack_id: StableId
    context_pack_hash: Sha256
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: Sha256
    project_id: StableId
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    handoff_status: Literal["blocked", "risk_draft_only"]
    allowed_output: Literal["repair_plan_or_risk_draft_only"]
    failure_signatures: tuple[CandidateFailureSignature, ...] = Field(max_length=64)
    kind: AgentCandidateKind
    content: AgentCandidateContent
    content_hash: Sha256
    references: tuple[AgentCandidateReference, ...] = Field(
        min_length=1,
        max_length=64,
    )
    references_hash: Sha256
    formal_draft_allowed: Literal[False]
    requires_author_review: Literal[True]
    created_at: datetime
    envelope_hash: Sha256

    @model_validator(mode="after")
    def validate_envelope(self) -> "AgentCandidateOutput":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("candidate output created_at must be timezone-aware")
        expected_id = agent_candidate_output_id(
            run_id=self.run_id,
            provider_boundary_id=self.provider_boundary_id,
            schema_id=self.schema_id,
        )
        if self.candidate_output_id != expected_id:
            raise ValueError("candidate output identity does not match its Runtime boundary")
        identities = [
            (reference.reference_kind, reference.reference_id)
            for reference in self.references
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("candidate reference identities must be unique")
        if len(self.failure_signatures) != len(set(self.failure_signatures)):
            raise ValueError("candidate failure signatures must be unique")
        if any(
            not signature.strip() or len(signature) > 256
            for signature in self.failure_signatures
        ):
            raise ValueError("candidate failure signatures must be bounded non-blank text")
        if self.handoff_status == "blocked" and not self.failure_signatures:
            raise ValueError("blocked candidate outputs require failure signatures")
        if self.content_hash != agent_candidate_content_hash(self.content):
            raise ValueError("candidate content hash does not match content")
        if self.references_hash != agent_candidate_references_hash(self.references):
            raise ValueError("candidate references hash does not match references")
        if self.envelope_hash != agent_candidate_output_envelope_hash(self):
            raise ValueError("candidate envelope hash does not match immutable material")
        return self


class AgentCandidateOutputV2(AgentCandidateOutputV1):
    """Legacy Candidate envelope with Runtime-observed Provider identity evidence."""

    model_config = ConfigDict(json_schema_extra=_candidate_schema_invariants)

    schema_id: Literal["zhimeng.agent-candidate-output.v2"] = Field(
        alias="schema",
    )
    provider_turn_receipts: tuple[ProviderTurnReceiptBinding, ...] = Field(
        min_length=1,
        max_length=8,
    )
    provider_turn_receipts_hash: Sha256
    provider_identity_status: Literal["verified", "unreported"]

    @model_validator(mode="after")
    def validate_provider_receipt_chain(self) -> "AgentCandidateOutputV2":
        expected_ordinals = tuple(range(1, len(self.provider_turn_receipts) + 1))
        if tuple(item.turn_ordinal for item in self.provider_turn_receipts) != expected_ordinals:
            raise ValueError("candidate Provider turn receipt order must be contiguous")
        if len({item.receipt_id for item in self.provider_turn_receipts}) != len(
            self.provider_turn_receipts
        ):
            raise ValueError("candidate Provider turn receipt identities must be unique")
        if self.provider_turn_receipts_hash != provider_turn_receipt_chain_hash(
            self.provider_turn_receipts
        ):
            raise ValueError("candidate Provider turn receipt chain hash does not match")
        return self


class AgentCandidateOutput(AgentCandidateOutputV2):
    """Current Writer Candidate bound to Runtime instruction dispatch evidence."""

    model_config = ConfigDict(json_schema_extra=_candidate_schema_invariants)

    schema_id: Literal["zhimeng.agent-candidate-output.v3"] = Field(
        alias="schema",
    )
    writer_task_id: str = Field(pattern=r"^writer-task-[0-9a-f]{32}$")
    writer_task_hash: Sha256
    instruction_materialization_id: str = Field(
        pattern=r"^creation-instruction-materialization-[0-9a-f]{32}$"
    )
    instruction_materialization_hash: Sha256
    instruction_dispatch_receipt_id: str = Field(
        pattern=r"^creation-instruction-dispatch-[0-9a-f]{32}$"
    )
    instruction_dispatch_receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_writer_instruction_references(self) -> "AgentCandidateOutput":
        expected_prefix = (
            AgentCandidateReference(
                reference_kind="runtime_identity",
                reference_id=self.writer_task_id,
                reference_hash=self.writer_task_hash,
            ),
            AgentCandidateReference(
                reference_kind="artifact",
                reference_id=self.instruction_materialization_id,
                reference_hash=self.instruction_materialization_hash,
            ),
        )
        if self.references[:2] != expected_prefix:
            raise ValueError(
                "v3 candidate references must start with Runtime Writer and instruction evidence"
            )
        if len(self.references) not in {2, 3}:
            raise ValueError(
                "v3 candidate references must use the canonical Runtime-owned shape"
            )
        change_references = tuple(
            item for item in self.references if item.reference_kind == "change_set"
        )
        if self.kind == "change_proposal":
            if len(self.references) != 3 or len(change_references) != 1:
                raise ValueError("change-proposal candidates require one ChangeSet reference")
            if self.references[2] != change_references[0]:
                raise ValueError("v3 ChangeSet reference must be last")
        elif len(self.references) != 2 or change_references:
            raise ValueError("non-change candidates cannot reference a ChangeSet")
        return self


AgentCandidateOutputRecord: TypeAlias = (
    AgentCandidateOutput | AgentCandidateOutputV2 | AgentCandidateOutputV1
)


def _derived_id(prefix: str, payload: object) -> str:
    return f"{prefix}-{canonical_json_hash(payload)[:32]}"


def agent_candidate_output_id(
    *,
    run_id: str,
    provider_boundary_id: str,
    schema_id: str = "zhimeng.agent-candidate-output.v3",
) -> str:
    return _derived_id(
        "agent-candidate",
        {
            "schema": schema_id,
            "run_id": run_id,
            "provider_boundary_id": provider_boundary_id,
        },
    )


def agent_candidate_content_hash(content: AgentCandidateContent) -> str:
    return canonical_json_hash(content.model_dump(mode="json", by_alias=True))


def agent_candidate_references_hash(
    references: tuple[AgentCandidateReference, ...],
) -> str:
    return canonical_json_hash(
        [reference.model_dump(mode="json", by_alias=True) for reference in references]
    )


def agent_candidate_output_envelope_hash(output: AgentCandidateOutputRecord) -> str:
    return canonical_json_hash(
        output.model_dump(
            mode="json",
            by_alias=True,
            exclude={"envelope_hash"},
        )
    )


def agent_candidate_output_idempotency_hash(output: AgentCandidateOutputRecord) -> str:
    """Ignore persistence time while retaining every semantic and Runtime binding."""

    return canonical_json_hash(
        output.model_dump(
            mode="json",
            by_alias=True,
            exclude={"created_at", "envelope_hash"},
        )
    )


# Short aliases for integrations that call the immutable envelope a candidate.
agent_candidate_output_hash = agent_candidate_output_envelope_hash
candidate_output_hash = agent_candidate_output_envelope_hash


def build_agent_candidate_output(
    plan: WorkflowPlan,
    checkpoints: tuple[WorkflowStepCheckpoint, ...],
    provider_boundary: ProviderIoBoundary,
    *,
    handoff: CreationHandoffResult,
    writer_task: "WriterTask",
    instruction_materialization: CreationInstructionMaterialization,
    instruction_dispatch_receipt: CreationInstructionDispatchReceipt,
    provider_turn_receipts: tuple[ProviderTurnReceipt, ...],
    change_set: ChangeSetResponse | None,
    submission: AgentCandidateSubmission,
    created_at: datetime,
) -> AgentCandidateOutput:
    validate_workflow_checkpoint_chain(plan, checkpoints)
    if not checkpoints:
        raise ValueError("candidate output requires workflow checkpoints")
    final_checkpoint = checkpoints[-1]
    if final_checkpoint.step_ordinal != len(plan.steps):
        raise ValueError("candidate output requires the final workflow checkpoint")
    if final_checkpoint.execution_status != "succeeded":
        raise ValueError("candidate output requires a successful final checkpoint")
    if (
        provider_boundary.workflow_plan_id != plan.workflow_plan_id
        or provider_boundary.plan_hash != plan.plan_hash
        or provider_boundary.run_id != plan.run_id
        or provider_boundary.last_step_checkpoint_id
        != final_checkpoint.checkpoint_id
        or provider_boundary.last_step_checkpoint_hash
        != final_checkpoint.checkpoint_hash
    ):
        raise ValueError("candidate output Provider boundary does not bind the workflow")
    if (
        handoff.workflow_plan_id != plan.workflow_plan_id
        or handoff.workflow_plan_hash != plan.plan_hash
        or handoff.context_pack_id != plan.context_pack_id
        or handoff.context_pack_hash != plan.context_pack_hash
        or handoff.operation_package_id != plan.operation_package_id
        or handoff.operation_package_hash != plan.operation_package_hash
        or handoff.project_id != plan.project_id
        or handoff.project_revision != plan.project_revision
        or handoff.project_fingerprint != plan.project_fingerprint
        or handoff.formal_draft_allowed is not False
    ):
        raise ValueError("candidate output handoff does not bind the workflow")
    if (
        writer_task.run_id != plan.run_id
        or writer_task.workflow_plan_id != plan.workflow_plan_id
        or writer_task.workflow_plan_hash != plan.plan_hash
        or writer_task.final_checkpoint_id != final_checkpoint.checkpoint_id
        or writer_task.final_checkpoint_hash != final_checkpoint.checkpoint_hash
        or writer_task.context_pack_id != plan.context_pack_id
        or writer_task.context_pack_hash != plan.context_pack_hash
        or writer_task.operation_package_id != plan.operation_package_id
        or writer_task.operation_package_hash != plan.operation_package_hash
        or writer_task.project_id != plan.project_id
        or writer_task.project_revision != plan.project_revision
        or writer_task.project_fingerprint != plan.project_fingerprint
    ):
        raise ValueError("candidate output Writer task does not bind the workflow")
    if (
        instruction_materialization.run_id != plan.run_id
        or instruction_materialization.workflow_plan_id != plan.workflow_plan_id
        or instruction_materialization.workflow_plan_hash != plan.plan_hash
        or instruction_materialization.final_checkpoint_id
        != final_checkpoint.checkpoint_id
        or instruction_materialization.final_checkpoint_hash
        != final_checkpoint.checkpoint_hash
        or instruction_materialization.writer_task_id != writer_task.writer_task_id
        or instruction_materialization.writer_task_hash != writer_task.task_hash
    ):
        raise ValueError("candidate output instruction materialization does not bind Writer")
    if (
        instruction_dispatch_receipt.run_id != plan.run_id
        or instruction_dispatch_receipt.workflow_plan_id != plan.workflow_plan_id
        or instruction_dispatch_receipt.workflow_plan_hash != plan.plan_hash
        or instruction_dispatch_receipt.final_checkpoint_id
        != final_checkpoint.checkpoint_id
        or instruction_dispatch_receipt.final_checkpoint_hash
        != final_checkpoint.checkpoint_hash
        or instruction_dispatch_receipt.writer_task_id != writer_task.writer_task_id
        or instruction_dispatch_receipt.writer_task_hash != writer_task.task_hash
        or instruction_dispatch_receipt.materialization_id
        != instruction_materialization.materialization_id
        or instruction_dispatch_receipt.materialization_hash
        != instruction_materialization.materialization_hash
        or instruction_dispatch_receipt.provider_boundary_id
        != provider_boundary.boundary_id
        or instruction_dispatch_receipt.provider_boundary_hash
        != provider_boundary.boundary_hash
        or instruction_dispatch_receipt.provider_request_hash
        != provider_boundary.request_hash
        or instruction_dispatch_receipt.automatic_provider_replay_count != 0
    ):
        raise ValueError("candidate output dispatch receipt does not bind Provider boundary")
    if not provider_turn_receipts:
        raise ValueError("candidate output requires Provider turn receipts")
    expected_ordinals = tuple(range(1, len(provider_turn_receipts) + 1))
    if tuple(item.turn_ordinal for item in provider_turn_receipts) != expected_ordinals:
        raise ValueError("candidate Provider turn receipts must be contiguous")
    if any(
        item.run_id != plan.run_id
        or item.provider_boundary_id != provider_boundary.boundary_id
        or item.provider_boundary_hash != provider_boundary.boundary_hash
        for item in provider_turn_receipts
    ):
        raise ValueError("candidate Provider turn receipts do not bind the workflow")
    if any(
        item.model_identity_status == "rejected_mismatch"
        for item in provider_turn_receipts
    ):
        raise ValueError("candidate output cannot bind a rejected Provider model")
    receipt_bindings = tuple(item.binding() for item in provider_turn_receipts)
    provider_identity_status = (
        "unreported"
        if any(item.model_identity_status == "unreported" for item in provider_turn_receipts)
        else "verified"
    )
    runtime_references = (
        AgentCandidateReference(
            reference_kind="runtime_identity",
            reference_id=writer_task.writer_task_id,
            reference_hash=writer_task.task_hash,
        ),
        AgentCandidateReference(
            reference_kind="artifact",
            reference_id=instruction_materialization.materialization_id,
            reference_hash=instruction_materialization.materialization_hash,
        ),
    )
    if writer_task.mode == "repair_only":
        if submission.kind != "repair_plan" or change_set is not None:
            raise ValueError("repair-only Writer mode requires a repair plan without ChangeSet")
        normalized_references = runtime_references
    elif writer_task.mode == "author_review_change_proposal":
        if submission.kind != "change_proposal" or change_set is None:
            raise ValueError(
                "author-review Writer mode requires a change proposal and ChangeSet"
            )
        if (
            change_set.source_run_id != plan.run_id
            or change_set.project_id != plan.project_id
            or change_set.base_revision != plan.project_revision
            or change_set.base_fingerprint != plan.project_fingerprint
        ):
            raise ValueError("candidate ChangeSet does not bind the Writer project revision")
        normalized_references = runtime_references + (
            AgentCandidateReference(
                reference_kind="change_set",
                reference_id=change_set.change_set_id,
                reference_hash=change_set_payload_hash(change_set),
            ),
        )
    else:  # pragma: no cover - WriterTask validation closes this branch.
        raise ValueError("candidate Writer mode is unsupported")
    material = {
        "schema_id": "zhimeng.agent-candidate-output.v3",
        "candidate_output_id": agent_candidate_output_id(
            run_id=plan.run_id,
            provider_boundary_id=provider_boundary.boundary_id,
            schema_id="zhimeng.agent-candidate-output.v3",
        ),
        "run_id": plan.run_id,
        "workflow_plan_id": plan.workflow_plan_id,
        "workflow_plan_hash": plan.plan_hash,
        "final_checkpoint_id": final_checkpoint.checkpoint_id,
        "final_checkpoint_hash": final_checkpoint.checkpoint_hash,
        "provider_boundary_id": provider_boundary.boundary_id,
        "provider_boundary_hash": provider_boundary.boundary_hash,
        "provider_request_hash": provider_boundary.request_hash,
        "provider_turn_receipts": receipt_bindings,
        "provider_turn_receipts_hash": provider_turn_receipt_chain_hash(
            receipt_bindings
        ),
        "provider_identity_status": provider_identity_status,
        "writer_task_id": writer_task.writer_task_id,
        "writer_task_hash": writer_task.task_hash,
        "instruction_materialization_id": (
            instruction_materialization.materialization_id
        ),
        "instruction_materialization_hash": (
            instruction_materialization.materialization_hash
        ),
        "instruction_dispatch_receipt_id": (
            instruction_dispatch_receipt.dispatch_receipt_id
        ),
        "instruction_dispatch_receipt_hash": instruction_dispatch_receipt.receipt_hash,
        "context_pack_id": plan.context_pack_id,
        "context_pack_hash": plan.context_pack_hash,
        "operation_package_id": plan.operation_package_id,
        "operation_package_hash": plan.operation_package_hash,
        "project_id": plan.project_id,
        "project_revision": plan.project_revision,
        "project_fingerprint": plan.project_fingerprint,
        "handoff_status": handoff.handoff_status,
        "allowed_output": handoff.allowed_output,
        "failure_signatures": tuple(handoff.failure_signatures),
        "kind": submission.kind,
        "content": submission.content,
        "content_hash": agent_candidate_content_hash(submission.content),
        "references": normalized_references,
        "references_hash": agent_candidate_references_hash(
            normalized_references
        ),
        "formal_draft_allowed": False,
        "requires_author_review": True,
        "created_at": created_at,
    }
    draft = AgentCandidateOutput.model_construct(
        **material,
        envelope_hash="0" * 64,
    )
    return AgentCandidateOutput(
        **material,
        envelope_hash=agent_candidate_output_envelope_hash(draft),
    )


def parse_agent_candidate_output_json(payload: str) -> AgentCandidateOutputRecord:
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("candidate output is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("candidate output must be a JSON object")
    schema_id = raw.get("schema")
    if schema_id == "zhimeng.agent-candidate-output.v1":
        return AgentCandidateOutputV1.model_validate_json(payload, strict=True)
    if schema_id == "zhimeng.agent-candidate-output.v2":
        return AgentCandidateOutputV2.model_validate_json(payload, strict=True)
    if schema_id == "zhimeng.agent-candidate-output.v3":
        return AgentCandidateOutput.model_validate_json(payload, strict=True)
    raise ValueError("candidate output schema is unsupported")


__all__ = [
    "AgentCandidateContent",
    "AgentCandidateKind",
    "AgentCandidateOutput",
    "AgentCandidateOutputRecord",
    "AgentCandidateOutputV1",
    "AgentCandidateOutputV2",
    "AgentCandidateReference",
    "AgentCandidateReferenceKind",
    "AgentCandidateSubmission",
    "agent_candidate_content_hash",
    "agent_candidate_output_envelope_hash",
    "agent_candidate_output_hash",
    "agent_candidate_output_id",
    "agent_candidate_output_idempotency_hash",
    "agent_candidate_references_hash",
    "candidate_output_hash",
    "build_agent_candidate_output",
    "parse_agent_candidate_output_json",
]
