from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DistillationProfile: TypeAlias = Literal[
    "scene",
    "chapter",
    "draft_assist",
    "mechanism_transfer",
    "review",
    "rewrite",
    "volume",
    "deai",
    "style_calibration",
    "story_unit",
    "debt_recall",
    "packaging",
    "benchmark",
]
DISTILLATION_PROFILES = frozenset(
    {
        "scene",
        "chapter",
        "draft_assist",
        "mechanism_transfer",
        "review",
        "rewrite",
        "volume",
        "deai",
        "style_calibration",
        "story_unit",
        "debt_recall",
        "packaging",
        "benchmark",
    }
)
DistillationCardType: TypeAlias = Literal[
    "work",
    "author",
    "genre",
    "scene",
    "voice",
    "trope",
    "platform",
    "packaging",
    "benchmark",
    "validator",
]
DistillationHealth: TypeAlias = Literal["healthy", "degraded", "blocked"]
DistillationValidationMaturity: TypeAlias = Literal[
    "validated",
    "risk_ready",
    "degraded",
]
DistillationUsageMode: TypeAlias = Literal["hard_rule", "risk_ready"]
DistillationWritingGate: TypeAlias = Literal[
    "proceed_with_distillation_context",
    "pause_and_request_benchmark_sources",
    "validate_existing_cards_or_risk_draft",
    "pause_and_request_sources",
    "proceed_with_risk_only",
    "ask_user_or_proceed_with_risk",
]
DistillationNextStep: TypeAlias = Literal[
    "run_context_and_write",
    "validate_existing_cards_or_run_risk_context",
    "run_risk_context_only",
    "ask_user_for_same_type_sources",
    "ask_user_or_run_context_with_risk",
]


class DistillationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DistillationCoverageRequest(DistillationModel):
    query: str = Field(
        min_length=1,
        max_length=1000,
        pattern=r"^[^-]",
        strict=True,
    )
    profile: DistillationProfile

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        if any(ord(character) < 32 and not character.isspace() for character in value):
            raise ValueError("query cannot contain control characters")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must not be blank")
        if normalized.startswith("-"):
            raise ValueError("query cannot start with an option marker")
        return normalized


class DistillationSelectedCard(DistillationModel):
    card_id: str = Field(min_length=1, max_length=300, strict=True)
    card_type: DistillationCardType
    title: str = Field(min_length=1, max_length=500, strict=True)
    summary: str = Field(max_length=1200, strict=True)
    usage_mode: DistillationUsageMode
    hard_rule_eligible: bool = Field(strict=True)
    safety_risk: float = Field(ge=0.0, le=1.0, strict=True)
    tags: list[str] = Field(max_length=24)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 120 for value in values):
            raise ValueError("tags must be non-empty and at most 120 characters")
        return values

    @model_validator(mode="after")
    def validate_usage_mode(self) -> "DistillationSelectedCard":
        expected = "hard_rule" if self.hard_rule_eligible else "risk_ready"
        if self.usage_mode != expected:
            raise ValueError("usage_mode must match hard_rule_eligible")
        return self


class DistillationValidationLadderDebt(DistillationModel):
    risk_ready_cards: int = Field(ge=0, strict=True)
    warn_steps: int = Field(ge=0, strict=True)


class DistillationHealthResponse(DistillationModel):
    schema_id: Literal["zhimeng.distillation-health.v1"] = Field(
        default="zhimeng.distillation-health.v1",
        alias="schema",
    )
    read_only: Literal[True] = True
    health: DistillationHealth
    structural_health: DistillationHealth
    validation_maturity: DistillationValidationMaturity
    card_count: int = Field(ge=0, strict=True)
    counts: dict[DistillationCardType, int]
    validation_ladder_debt: DistillationValidationLadderDebt
    hard_rule_eligible_counts: dict[DistillationCardType, int]
    library_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    tool_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    observed_at: datetime

    @field_validator(
        "counts",
        "hard_rule_eligible_counts",
    )
    @classmethod
    def validate_non_negative_counts(cls, values: dict[str, int]) -> dict[str, int]:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values.values()
        ):
            raise ValueError("count maps must contain non-negative integers")
        return values

    @model_validator(mode="after")
    def validate_health_axes(self) -> "DistillationHealthResponse":
        if self.structural_health == "blocked" and self.health != "blocked":
            raise ValueError("blocked structural health must remain blocked")
        if self.structural_health == "degraded" and self.health == "healthy":
            raise ValueError("degraded structural health cannot be healthy")
        if self.validation_maturity != "validated" and self.health == "healthy":
            raise ValueError("risk-ready validation maturity cannot be healthy")
        return self


class DistillationCoverageResponse(DistillationModel):
    schema_id: Literal["zhimeng.distillation-coverage.v1"] = Field(
        default="zhimeng.distillation-coverage.v1",
        alias="schema",
    )
    read_only: Literal[True] = True
    query: str = Field(min_length=1, max_length=1000, strict=True)
    profile: DistillationProfile
    target_health: DistillationHealth
    writing_gate: DistillationWritingGate
    recommended_next_step: DistillationNextStep
    risk_preview_allowed: bool = Field(strict=True)
    formal_injection_allowed: bool = Field(strict=True)
    source_coverage_ratio: float = Field(ge=0.0, le=1.0, strict=True)
    formal_coverage_ratio: float = Field(ge=0.0, le=1.0, strict=True)
    matched_cards: int = Field(ge=0, strict=True)
    available_matched_cards: int = Field(ge=0, strict=True)
    selected_type_counts: dict[DistillationCardType, int]
    selected_hard_rule_type_counts: dict[DistillationCardType, int]
    selected_risk_ready_type_counts: dict[DistillationCardType, int]
    source_quota_gaps: dict[DistillationCardType, int]
    formal_quota_gaps: dict[DistillationCardType, int]
    missing_types: list[DistillationCardType]
    formal_missing_types: list[DistillationCardType]
    selected_cards: list[DistillationSelectedCard] = Field(max_length=24)
    feed_request: str = Field(max_length=2400, strict=True)
    library_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    tool_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    observed_at: datetime

    @field_validator(
        "selected_type_counts",
        "selected_hard_rule_type_counts",
        "selected_risk_ready_type_counts",
        "source_quota_gaps",
        "formal_quota_gaps",
    )
    @classmethod
    def validate_non_negative_counts(cls, values: dict[str, int]) -> dict[str, int]:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values.values()
        ):
            raise ValueError("count maps must contain non-negative integers")
        return values

    @model_validator(mode="after")
    def validate_coverage_consistency(self) -> "DistillationCoverageResponse":
        selected_counts: dict[str, int] = {}
        hard_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        for card in self.selected_cards:
            selected_counts[card.card_type] = selected_counts.get(card.card_type, 0) + 1
            bucket = hard_counts if card.hard_rule_eligible else risk_counts
            bucket[card.card_type] = bucket.get(card.card_type, 0) + 1
        if selected_counts != self.selected_type_counts:
            raise ValueError("selected_type_counts must match selected_cards")
        if hard_counts != self.selected_hard_rule_type_counts:
            raise ValueError("selected_hard_rule_type_counts must match selected_cards")
        if risk_counts != self.selected_risk_ready_type_counts:
            raise ValueError("selected_risk_ready_type_counts must match selected_cards")
        if self.target_health == "healthy" and self.writing_gate != "proceed_with_distillation_context":
            raise ValueError("healthy target must use the proceed gate")
        if self.target_health == "blocked" and not self.writing_gate.startswith("pause_"):
            raise ValueError("blocked target must use a pause gate")
        if self.target_health != "healthy" and self.writing_gate == "proceed_with_distillation_context":
            raise ValueError("a degraded target cannot use the healthy proceed gate")
        if self.risk_preview_allowed != (self.target_health != "blocked"):
            raise ValueError("risk_preview_allowed must reflect target health")
        expected_formal_injection = (
            self.target_health == "healthy"
            and self.writing_gate == "proceed_with_distillation_context"
        )
        if self.formal_injection_allowed != expected_formal_injection:
            raise ValueError("formal_injection_allowed must reflect the formal gate")
        return self
