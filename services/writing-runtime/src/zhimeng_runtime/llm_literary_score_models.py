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
LlmLiteraryScoreReceiptId = Annotated[
    str, StringConstraints(pattern=r"^llm-literary-score-[0-9a-f]{32}$")
]


class LlmLiteraryScoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LlmLiteraryScorerMode(StrEnum):
    OFFLINE_STUB = "offline_stub"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_LIVE = "provider_live"


class LlmLiteraryScoreEvaluateRequest(LlmLiteraryScoreModel):
    schema_id: Literal["zhimeng.llm-literary-score-request.v1"] = Field(
        default="zhimeng.llm-literary-score-request.v1",
        alias="schema",
    )
    expected_result_fingerprint: Sha256 | None = None
    expected_review_revision: int | None = Field(default=None, ge=0)
    prefer_provider: bool = False


class LlmLiteraryScoreAxis(LlmLiteraryScoreModel):
    code: Literal[
        "immersion",
        "character_voice",
        "emotional_arc",
        "prose_craft",
        "suspense_pull",
    ]
    score: int = Field(ge=0, le=100)
    weight: int = Field(ge=1, le=100)
    weighted_score: int = Field(ge=0, le=10000)
    rationale: NonEmptyText = Field(max_length=1024)

    @model_validator(mode="after")
    def validate_weighted(self) -> "LlmLiteraryScoreAxis":
        if self.weighted_score != self.score * self.weight:
            raise ValueError("weighted_score must equal score * weight")
        return self


class LlmLiteraryScoreReceipt(LlmLiteraryScoreModel):
    schema_id: Literal["zhimeng.llm-literary-score-receipt.v1"] = Field(
        default="zhimeng.llm-literary-score-receipt.v1",
        alias="schema",
    )
    receipt_id: LlmLiteraryScoreReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back", "ready_candidate"]
    result_revision: int | None = Field(default=None, ge=1)
    result_fingerprint: Sha256 | None = None
    review_revision: int = Field(ge=0)
    scorer_mode: LlmLiteraryScorerMode
    advisory_only: Literal[True] = True
    llm_score_recorded: bool
    quality_assessed: Literal[False] = False
    subjective_quality_unlocked: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    authorization_unlock: Literal[False] = False
    provider_model: str | None = Field(default=None, max_length=160)
    literary_quality_receipt_id: str | None = Field(default=None, max_length=160)
    axes: list[LlmLiteraryScoreAxis] = Field(min_length=5, max_length=5)
    total_score: int = Field(ge=0, le=100)
    detail: NonEmptyText = Field(max_length=1024)
    policy_version: Literal["local-v1-llm-literary-score-1"] = (
        "local-v1-llm-literary-score-1"
    )
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "LlmLiteraryScoreReceipt":
        if self.quality_assessed is not False:
            raise ValueError("LLM literary score cannot claim quality_assessed")
        if self.subjective_quality_unlocked is not False:
            raise ValueError("LLM literary score cannot unlock subjective quality")
        if self.formal_draft_allowed is not False:
            raise ValueError("LLM literary score cannot unlock formal draft")
        if self.authorization_unlock is not False:
            raise ValueError("LLM literary score cannot unlock authorization")
        if self.advisory_only is not True:
            raise ValueError("LLM literary score must remain advisory_only")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        total_weight = sum(item.weight for item in self.axes)
        if total_weight <= 0:
            raise ValueError("axis total weight must be positive")
        expected_total = round(
            sum(item.weighted_score for item in self.axes) / total_weight
        )
        if self.total_score != expected_total:
            raise ValueError("total_score must match weighted average")
        if self.scorer_mode is LlmLiteraryScorerMode.PROVIDER_LIVE:
            if not self.provider_model:
                raise ValueError("provider_live requires provider_model")
        if self.scorer_mode is not LlmLiteraryScorerMode.PROVIDER_LIVE:
            if self.provider_model is not None:
                raise ValueError("non-live scorer must not set provider_model")
        if self.scorer_mode is LlmLiteraryScorerMode.PROVIDER_UNAVAILABLE:
            if self.llm_score_recorded is not False:
                raise ValueError("provider_unavailable cannot record LLM score")
        elif self.llm_score_recorded is not True:
            raise ValueError("offline_stub/provider_live must record LLM score")
        expected = llm_literary_score_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("LLM literary score receipt hash mismatch")
        return self


class LlmLiteraryScoreResponse(LlmLiteraryScoreModel):
    schema_id: Literal["zhimeng.llm-literary-score-response.v1"] = Field(
        default="zhimeng.llm-literary-score-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: LlmLiteraryScoreReceipt | None = None
    history: list[LlmLiteraryScoreReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "quality_assessed",
        "subjective_literary_score",
        "literary_quality_score",
        "formal_draft_authorization_unlock",
        "formal_prose_writeback",
        "external_blind_review_matrix",
        "local_v1_exe",
    )


def llm_literary_score_receipt_hash(receipt: LlmLiteraryScoreReceipt) -> str:
    material = {
        "schema": "zhimeng.llm-literary-score-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "outcome": receipt.outcome,
        "result_revision": receipt.result_revision,
        "result_fingerprint": receipt.result_fingerprint,
        "review_revision": receipt.review_revision,
        "scorer_mode": (
            receipt.scorer_mode.value
            if hasattr(receipt.scorer_mode, "value")
            else receipt.scorer_mode
        ),
        "advisory_only": True,
        "llm_score_recorded": receipt.llm_score_recorded,
        "quality_assessed": False,
        "subjective_quality_unlocked": False,
        "formal_draft_allowed": False,
        "authorization_unlock": False,
        "provider_model": receipt.provider_model,
        "literary_quality_receipt_id": receipt.literary_quality_receipt_id,
        "axes": [
            {
                "code": item.code,
                "score": item.score,
                "weight": item.weight,
                "weighted_score": item.weighted_score,
                "rationale": item.rationale,
            }
            for item in receipt.axes
        ],
        "total_score": receipt.total_score,
        "detail": receipt.detail,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "LlmLiteraryScoreAxis",
    "LlmLiteraryScoreEvaluateRequest",
    "LlmLiteraryScoreReceipt",
    "LlmLiteraryScoreResponse",
    "LlmLiteraryScorerMode",
    "llm_literary_score_receipt_hash",
]
