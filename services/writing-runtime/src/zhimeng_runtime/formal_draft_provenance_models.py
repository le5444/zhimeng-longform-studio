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
ProvenanceReceiptId = Annotated[
    str, StringConstraints(pattern=r"^formal-draft-provenance-[0-9a-f]{32}$")
]


class FormalDraftProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FormalDraftProvenanceStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SCOPE_LIMITED = "scope_limited"


class FormalDraftProvenanceCheckCode(StrEnum):
    PROJECT_BOUND = "project_bound"
    CHANGE_SET_BOUND = "change_set_bound"
    WRITEBACK_EVIDENCE = "writeback_evidence"
    FORMAL_DRAFT_AUTH_ON_WRITEBACK = "formal_draft_auth_on_writeback"
    HARD_FACT_GATE_RECEIPT = "hard_fact_gate_receipt"
    LITERARY_QUALITY_GATE_RECEIPT = "literary_quality_gate_receipt"
    GATE_PIPELINE_RECEIPT = "gate_pipeline_receipt"
    AUTHOR_DECISION_RECEIPT = "author_decision_receipt"
    AGENT_CHANGE_SET_PROVENANCE = "agent_change_set_provenance"


class FormalDraftProvenanceEvaluateRequest(FormalDraftProvenanceModel):
    schema_id: Literal["zhimeng.formal-draft-provenance-request.v1"] = Field(
        default="zhimeng.formal-draft-provenance-request.v1",
        alias="schema",
    )
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    change_set_id: str | None = Field(default=None, max_length=160)


class FormalDraftProvenanceCheck(FormalDraftProvenanceModel):
    code: FormalDraftProvenanceCheckCode
    required: bool
    present: bool
    detail: NonEmptyText = Field(max_length=1024)


class FormalDraftProvenanceReceipt(FormalDraftProvenanceModel):
    schema_id: Literal["zhimeng.formal-draft-provenance-receipt.v1"] = Field(
        default="zhimeng.formal-draft-provenance-receipt.v1",
        alias="schema",
    )
    receipt_id: ProvenanceReceiptId
    project_id: ProjectId
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: Sha256 | None = None
    change_set_id: str | None = Field(default=None, max_length=160)
    status: FormalDraftProvenanceStatus
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    checks: list[FormalDraftProvenanceCheck] = Field(min_length=1, max_length=32)
    missing_required: list[FormalDraftProvenanceCheckCode] = Field(
        default_factory=list,
        max_length=32,
    )
    policy_version: Literal["local-v1-provenance-1"] = "local-v1-provenance-1"
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FormalDraftProvenanceReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("formal-draft provenance cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("formal-draft provenance cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("formal-draft provenance cannot allow project truth writes")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        required_missing = [
            item.code for item in self.checks if item.required and not item.present
        ]
        if list(self.missing_required) != required_missing:
            raise ValueError("missing_required must match required absent checks")
        expected_ready = (
            self.status is FormalDraftProvenanceStatus.COMPLETE
            and not required_missing
        )
        if self.authorization_input_ready is not expected_ready:
            raise ValueError(
                "authorization_input_ready must match complete provenance status"
            )
        if self.status is FormalDraftProvenanceStatus.COMPLETE and required_missing:
            raise ValueError("complete provenance cannot miss required checks")
        expected = formal_draft_provenance_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("formal-draft provenance receipt hash mismatch")
        return self


class FormalDraftProvenanceResponse(FormalDraftProvenanceModel):
    schema_id: Literal["zhimeng.formal-draft-provenance-response.v1"] = Field(
        default="zhimeng.formal-draft-provenance-response.v1",
        alias="schema",
    )
    receipt: FormalDraftProvenanceReceipt
    not_claiming: tuple[str, ...] = (
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "literary_quality_gate_complete",
        "local_v1_exe",
    )


def formal_draft_provenance_receipt_hash(
    receipt: FormalDraftProvenanceReceipt,
) -> str:
    material = {
        "schema": "zhimeng.formal-draft-provenance-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "change_set_id": receipt.change_set_id,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "checks": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "required": item.required,
                "present": item.present,
                "detail": item.detail,
            }
            for item in receipt.checks
        ],
        "missing_required": [
            item.value if hasattr(item, "value") else item
            for item in receipt.missing_required
        ],
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FormalDraftProvenanceCheck",
    "FormalDraftProvenanceCheckCode",
    "FormalDraftProvenanceEvaluateRequest",
    "FormalDraftProvenanceReceipt",
    "FormalDraftProvenanceResponse",
    "FormalDraftProvenanceStatus",
    "formal_draft_provenance_receipt_hash",
]
