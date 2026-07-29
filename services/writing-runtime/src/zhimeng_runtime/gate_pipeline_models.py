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
ChangeSetId = Annotated[str, StringConstraints(pattern=r"^changeset-[0-9a-f]{32}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
GatePipelineReceiptId = Annotated[
    str, StringConstraints(pattern=r"^gate-pipeline-[0-9a-f]{32}$")
]


class GatePipelineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class GatePipelineStatus(StrEnum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"


class GatePipelineStepCode(StrEnum):
    POST_DRAFT_GATE = "post_draft_gate"
    LITERARY_QUALITY_GATE = "literary_quality_gate"
    HARD_FACT_GATE = "hard_fact_gate"
    LLM_LITERARY_SCORE = "llm_literary_score"
    EXTERNAL_BLIND_REVIEW_MATRIX = "external_blind_review_matrix"


class GatePipelineEvaluateRequest(GatePipelineModel):
    schema_id: Literal["zhimeng.gate-pipeline-request.v1"] = Field(
        default="zhimeng.gate-pipeline-request.v1",
        alias="schema",
    )
    expected_result_fingerprint: Sha256 | None = None
    expected_review_revision: int | None = Field(default=None, ge=0)


class GatePipelineStep(GatePipelineModel):
    code: GatePipelineStepCode
    ordinal: int = Field(ge=1, le=8)
    required: bool
    present: bool
    status: NonEmptyText = Field(max_length=64)
    receipt_id: str | None = Field(default=None, max_length=160)
    detail: NonEmptyText = Field(max_length=1024)


class GatePipelineReceipt(GatePipelineModel):
    schema_id: Literal["zhimeng.gate-pipeline-receipt.v1"] = Field(
        default="zhimeng.gate-pipeline-receipt.v1",
        alias="schema",
    )
    receipt_id: GatePipelineReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back", "ready_candidate"]
    result_revision: int | None = Field(default=None, ge=1)
    result_fingerprint: Sha256 | None = None
    review_revision: int = Field(ge=0)
    status: GatePipelineStatus
    steps: list[GatePipelineStep] = Field(min_length=1, max_length=8)
    hard_fact_receipt_id: str | None = Field(default=None, max_length=160)
    literary_quality_receipt_id: str | None = Field(default=None, max_length=160)
    llm_literary_score_receipt_id: str | None = Field(default=None, max_length=160)
    external_blind_review_matrix_ready: bool = False
    post_draft_gate_status: Literal["missing", "passed", "blocked", "stale"] = "missing"
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    policy_version: Literal[
        "local-v1-gate-pipeline-1",
        "local-v1-gate-pipeline-2",
    ] = "local-v1-gate-pipeline-2"
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "GatePipelineReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("gate pipeline cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("gate pipeline cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("gate pipeline cannot claim quality assessed")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        ordinals = [item.ordinal for item in self.steps]
        if ordinals != sorted(ordinals):
            raise ValueError("gate pipeline steps must be ordered by ordinal")
        if self.status is GatePipelineStatus.COMPLETE and not self.authorization_input_ready:
            raise ValueError("complete gate pipeline must mark authorization_input_ready")
        if self.status is GatePipelineStatus.BLOCKED and self.authorization_input_ready:
            raise ValueError("blocked gate pipeline cannot mark authorization_input_ready")
        expected = gate_pipeline_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("gate pipeline receipt hash mismatch")
        return self


class GatePipelineResponse(GatePipelineModel):
    schema_id: Literal["zhimeng.gate-pipeline-response.v1"] = Field(
        default="zhimeng.gate-pipeline-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: GatePipelineReceipt | None = None
    history: list[GatePipelineReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "literary_quality_score",
        "llm_literary_score_quality_unlock",
        "external_blind_review_quality_unlock",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def gate_pipeline_receipt_hash(receipt: GatePipelineReceipt) -> str:
    material = {
        "schema": "zhimeng.gate-pipeline-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "outcome": receipt.outcome,
        "result_revision": receipt.result_revision,
        "result_fingerprint": receipt.result_fingerprint,
        "review_revision": receipt.review_revision,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "steps": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "ordinal": item.ordinal,
                "required": item.required,
                "present": item.present,
                "status": item.status,
                "receipt_id": item.receipt_id,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "hard_fact_receipt_id": receipt.hard_fact_receipt_id,
        "literary_quality_receipt_id": receipt.literary_quality_receipt_id,
        "llm_literary_score_receipt_id": receipt.llm_literary_score_receipt_id,
        "external_blind_review_matrix_ready": receipt.external_blind_review_matrix_ready,
        "post_draft_gate_status": receipt.post_draft_gate_status,
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "GatePipelineEvaluateRequest",
    "GatePipelineReceipt",
    "GatePipelineResponse",
    "GatePipelineStatus",
    "GatePipelineStep",
    "GatePipelineStepCode",
    "gate_pipeline_receipt_hash",
]
