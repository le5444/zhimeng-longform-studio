from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
ContinuousO2ReceiptId = Annotated[
    str, StringConstraints(pattern=r"^formal-draft-continuous-o2-[0-9a-f]{32}$")
]


class FormalDraftContinuousO2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FormalDraftContinuousO2Status(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class FormalDraftContinuousO2CheckCode(StrEnum):
    PROJECT_BOUND = "project_bound"
    FOUR_SKILLS_CONTINUOUS_LINKED = "four_skills_continuous_linked"
    ADJACENT_O2_CHAIN_SUCCEEDED = "adjacent_o2_chain_succeeded"
    MANAGED_SKILL_CHAIN_SUCCEEDED = "managed_skill_chain_succeeded"
    CREATION_SUITE_CONTINUOUS_EVIDENCE = "creation_suite_continuous_evidence"
    INSTRUCTION_CHAIN_HANDOFF_BOUND = "instruction_chain_handoff_bound"


class FormalDraftContinuousO2EvaluateRequest(FormalDraftContinuousO2Model):
    schema_id: Literal["zhimeng.formal-draft-continuous-o2-request.v1"] = Field(
        default="zhimeng.formal-draft-continuous-o2-request.v1",
        alias="schema",
    )
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    adjacent_chain_id: str | None = Field(default=None, max_length=160)
    managed_skill_chain_id: str | None = Field(default=None, max_length=160)
    source_import_id: str | None = Field(default=None, max_length=160)


class FormalDraftContinuousO2Check(FormalDraftContinuousO2Model):
    code: FormalDraftContinuousO2CheckCode
    required: bool
    present: bool
    detail: NonEmptyText = Field(max_length=1024)
    evidence_id: str | None = Field(default=None, max_length=160)


class FormalDraftContinuousO2Receipt(FormalDraftContinuousO2Model):
    schema_id: Literal["zhimeng.formal-draft-continuous-o2-receipt.v1"] = Field(
        default="zhimeng.formal-draft-continuous-o2-receipt.v1",
        alias="schema",
    )
    receipt_id: ContinuousO2ReceiptId
    project_id: ProjectId
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: Sha256 | None = None
    status: FormalDraftContinuousO2Status
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    checks: list[FormalDraftContinuousO2Check] = Field(min_length=1, max_length=32)
    missing_required: list[FormalDraftContinuousO2CheckCode] = Field(
        default_factory=list,
        max_length=32,
    )
    adjacent_chain_id: str | None = Field(default=None, max_length=160)
    managed_skill_chain_id: str | None = Field(default=None, max_length=160)
    creation_suite_chain_id: str | None = Field(default=None, max_length=160)
    source_import_id: str | None = Field(default=None, max_length=160)
    formalization_receipt_id: str | None = Field(default=None, max_length=160)
    four_skills_continuous_receipt_id: str | None = Field(default=None, max_length=160)
    instruction_chain_handoff_binding_id: str | None = Field(default=None, max_length=160)
    instruction_chain_handoff_id: str | None = Field(default=None, max_length=160)
    policy_version: Literal["local-v1-continuous-o2-3"] = "local-v1-continuous-o2-3"
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FormalDraftContinuousO2Receipt":
        if self.authorization_unlock is not False:
            raise ValueError("continuous O2 readiness cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("continuous O2 readiness cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("continuous O2 readiness cannot allow project truth writes")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError(
                "continuous O2 readiness cannot claim four_skills_generic_exactly_once"
            )
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        required_missing = [
            item.code for item in self.checks if item.required and not item.present
        ]
        if list(self.missing_required) != required_missing:
            raise ValueError("missing_required must match required absent checks")
        expected_ready = (
            self.status is FormalDraftContinuousO2Status.COMPLETE
            and not required_missing
        )
        if self.authorization_input_ready is not expected_ready:
            raise ValueError(
                "authorization_input_ready must match complete continuous O2 status"
            )
        if self.status is FormalDraftContinuousO2Status.COMPLETE and required_missing:
            raise ValueError("complete continuous O2 cannot miss required checks")
        expected = formal_draft_continuous_o2_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("formal-draft continuous O2 receipt hash mismatch")
        return self


class FormalDraftContinuousO2Response(FormalDraftContinuousO2Model):
    schema_id: Literal["zhimeng.formal-draft-continuous-o2-response.v1"] = Field(
        default="zhimeng.formal-draft-continuous-o2-response.v1",
        alias="schema",
    )
    receipt: FormalDraftContinuousO2Receipt
    not_claiming: tuple[str, ...] = (
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "literary_quality_gate_complete",
        "local_v1_exe",
    )


def formal_draft_continuous_o2_receipt_hash(
    receipt: FormalDraftContinuousO2Receipt,
) -> str:
    material = {
        "schema": "zhimeng.formal-draft-continuous-o2-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "status": (
            receipt.status.value
            if hasattr(receipt.status, "value")
            else receipt.status
        ),
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "four_skills_generic_exactly_once": False,
        "checks": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "required": item.required,
                "present": item.present,
                "detail": item.detail,
                "evidence_id": item.evidence_id,
            }
            for item in receipt.checks
        ],
        "missing_required": [
            item.value if hasattr(item, "value") else item
            for item in receipt.missing_required
        ],
        "adjacent_chain_id": receipt.adjacent_chain_id,
        "managed_skill_chain_id": receipt.managed_skill_chain_id,
        "creation_suite_chain_id": receipt.creation_suite_chain_id,
        "source_import_id": receipt.source_import_id,
        "formalization_receipt_id": receipt.formalization_receipt_id,
        "four_skills_continuous_receipt_id": receipt.four_skills_continuous_receipt_id,
        "instruction_chain_handoff_binding_id": receipt.instruction_chain_handoff_binding_id,
        "instruction_chain_handoff_id": receipt.instruction_chain_handoff_id,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FormalDraftContinuousO2Check",
    "FormalDraftContinuousO2CheckCode",
    "FormalDraftContinuousO2EvaluateRequest",
    "FormalDraftContinuousO2Receipt",
    "FormalDraftContinuousO2Response",
    "FormalDraftContinuousO2Status",
    "formal_draft_continuous_o2_receipt_hash",
]
