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
LiteraryQualityReceiptId = Annotated[
    str, StringConstraints(pattern=r"^literary-quality-[0-9a-f]{32}$")
]


class LiteraryQualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LiteraryQualityDecision(StrEnum):
    ADVISORY_ONLY = "advisory_only"


class LiteraryQualityDimensionCode(StrEnum):
    CANDIDATE_PRESENT = "candidate_present"
    PLACEHOLDER_SCAN = "placeholder_scan"
    AI_SMELL_MARKERS = "ai_smell_markers"
    HARD_FACT_SIGNAL = "hard_fact_signal"
    DIALOGUE_BALANCE = "dialogue_balance"
    SENSORY_DENSITY = "sensory_density"
    PACING_VARIATION = "pacing_variation"
    HOOK_OPENING = "hook_opening"
    REPETITION_RISK = "repetition_risk"
    DETERMINISTIC_RUBRIC = "deterministic_rubric"
    SUBJECTIVE_LITERARY_SCORE = "subjective_literary_score"
    AUTHORIZATION_UNLOCK = "authorization_unlock"


class LiteraryQualityDimensionStatus(StrEnum):
    PASS = "pass"
    WATCH = "watch"
    FAIL = "fail"
    NOT_SCORED = "not_scored"


class LiteraryQualityEvaluateRequest(LiteraryQualityModel):
    schema_id: Literal["zhimeng.literary-quality-gate-request.v1"] = Field(
        default="zhimeng.literary-quality-gate-request.v1",
        alias="schema",
    )
    expected_result_fingerprint: Sha256 | None = None
    expected_review_revision: int | None = Field(default=None, ge=0)


class LiteraryQualityDimension(LiteraryQualityModel):
    code: LiteraryQualityDimensionCode
    status: LiteraryQualityDimensionStatus
    detail: NonEmptyText = Field(max_length=1024)


class LiteraryQualityFinding(LiteraryQualityModel):
    finding_id: str = Field(pattern=r"^lq-finding-[0-9a-f]{12}$")
    kind: Literal["structural", "hard_fact_signal", "subjective_note", "rubric_note"]
    severity: Literal["info", "watch", "concern"]
    summary: NonEmptyText = Field(max_length=500)
    evidence_quote: str = Field(default="", max_length=1000)
    relative_path: str | None = Field(default=None, max_length=512)


class LiteraryRubricScore(LiteraryQualityModel):
    """Deterministic literary rubric axis score. Advisory only; not quality_assessed."""

    code: Literal[
        "dialogue_balance",
        "sensory_density",
        "pacing_variation",
        "hook_opening",
        "repetition_risk",
    ]
    score: int = Field(ge=0, le=100)
    weight: int = Field(ge=1, le=100)
    weighted_score: int = Field(ge=0, le=10000)
    detail: NonEmptyText = Field(max_length=1024)

    @model_validator(mode="after")
    def validate_weighted(self) -> "LiteraryRubricScore":
        expected = self.score * self.weight
        if self.weighted_score != expected:
            raise ValueError("weighted_score must equal score * weight")
        return self


class LiteraryQualityGateReceipt(LiteraryQualityModel):
    schema_id: Literal["zhimeng.literary-quality-gate-receipt.v1"] = Field(
        default="zhimeng.literary-quality-gate-receipt.v1",
        alias="schema",
    )
    receipt_id: LiteraryQualityReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back", "ready_candidate"]
    result_revision: int | None = Field(default=None, ge=1)
    result_fingerprint: Sha256 | None = None
    review_revision: int = Field(ge=0)
    gate_mode: Literal[
        "deterministic_structural_v1",
        "deterministic_structural_rubric_v2",
    ] = "deterministic_structural_rubric_v2"
    decision: Literal[LiteraryQualityDecision.ADVISORY_ONLY] = (
        LiteraryQualityDecision.ADVISORY_ONLY
    )
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    structural_checks_run: Literal[True] = True
    deterministic_rubric_scored: Literal[True] = True
    dimensions: list[LiteraryQualityDimension] = Field(min_length=1, max_length=32)
    findings: list[LiteraryQualityFinding] = Field(default_factory=list, max_length=64)
    finding_counts: dict[str, int] = Field(default_factory=dict)
    rubric_scores: list[LiteraryRubricScore] = Field(default_factory=list, max_length=16)
    rubric_total_score: int = Field(default=0, ge=0, le=100)
    policy_version: Literal["local-v1-advisory-1", "local-v1-advisory-2"] = (
        "local-v1-advisory-2"
    )
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "LiteraryQualityGateReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("literary quality gate cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("literary quality gate cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("literary quality gate cannot claim literary quality assessed")
        if self.decision is not LiteraryQualityDecision.ADVISORY_ONLY:
            raise ValueError("Local v1 literary quality gate is advisory only")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if any(
            item.code is LiteraryQualityDimensionCode.SUBJECTIVE_LITERARY_SCORE
            and item.status is not LiteraryQualityDimensionStatus.NOT_SCORED
            for item in self.dimensions
        ):
            raise ValueError("subjective literary score must remain not_scored in Local v1")
        if any(
            item.code is LiteraryQualityDimensionCode.AUTHORIZATION_UNLOCK
            and item.status is LiteraryQualityDimensionStatus.PASS
            for item in self.dimensions
        ):
            raise ValueError("authorization unlock dimension cannot pass")
        if self.deterministic_rubric_scored is not True:
            raise ValueError("Local v1 literary gate must run deterministic rubric")
        if self.rubric_scores:
            total_weight = sum(item.weight for item in self.rubric_scores)
            if total_weight <= 0:
                raise ValueError("rubric total weight must be positive")
            expected_total = round(
                sum(item.weighted_score for item in self.rubric_scores) / total_weight
            )
            if self.rubric_total_score != expected_total:
                raise ValueError("rubric_total_score must match weighted average")
        elif self.rubric_total_score != 0:
            raise ValueError("empty rubric_scores requires rubric_total_score=0")
        expected = literary_quality_gate_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("literary quality gate receipt hash mismatch")
        return self


class LiteraryQualityGateResponse(LiteraryQualityModel):
    schema_id: Literal["zhimeng.literary-quality-gate-response.v1"] = Field(
        default="zhimeng.literary-quality-gate-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: LiteraryQualityGateReceipt | None = None
    history: list[LiteraryQualityGateReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "literary_quality_score",
        "llm_literary_score",
        "subjective_literary_score",
        "quality_assessed",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def literary_quality_gate_receipt_hash(receipt: LiteraryQualityGateReceipt) -> str:
    material = {
        "schema": "zhimeng.literary-quality-gate-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "outcome": receipt.outcome,
        "result_revision": receipt.result_revision,
        "result_fingerprint": receipt.result_fingerprint,
        "review_revision": receipt.review_revision,
        "gate_mode": receipt.gate_mode,
        "decision": receipt.decision.value if hasattr(receipt.decision, "value") else receipt.decision,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "structural_checks_run": True,
        "deterministic_rubric_scored": True,
        "dimensions": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "status": item.status.value if hasattr(item.status, "value") else item.status,
                "detail": item.detail,
            }
            for item in receipt.dimensions
        ],
        "findings": [
            {
                "finding_id": item.finding_id,
                "kind": item.kind,
                "severity": item.severity,
                "summary": item.summary,
                "evidence_quote": item.evidence_quote,
                "relative_path": item.relative_path,
            }
            for item in receipt.findings
        ],
        "finding_counts": receipt.finding_counts,
        "rubric_scores": [
            {
                "code": item.code,
                "score": item.score,
                "weight": item.weight,
                "weighted_score": item.weighted_score,
                "detail": item.detail,
            }
            for item in receipt.rubric_scores
        ],
        "rubric_total_score": receipt.rubric_total_score,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "LiteraryQualityDecision",
    "LiteraryQualityDimension",
    "LiteraryQualityDimensionCode",
    "LiteraryQualityDimensionStatus",
    "LiteraryQualityEvaluateRequest",
    "LiteraryQualityFinding",
    "LiteraryQualityGateReceipt",
    "LiteraryQualityGateResponse",
    "LiteraryRubricScore",
    "literary_quality_gate_receipt_hash",
]
