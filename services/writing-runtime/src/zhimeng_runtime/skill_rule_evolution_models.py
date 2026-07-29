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
SkillVersionId = Annotated[
    str, StringConstraints(pattern=r"^skill-version-[0-9a-f]{32}$")
]
EvolutionId = Annotated[
    str, StringConstraints(pattern=r"^skill-rule-evolution-[0-9a-f]{32}$")
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class SkillRuleEvolutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SkillRuleEvolutionStatus(StrEnum):
    PREPARED = "prepared"
    COMPARE_READY = "compare_ready"
    COMPARE_BLOCKED = "compare_blocked"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class SkillRuleEvolutionCompareStatus(StrEnum):
    DEFERRED = "deferred"
    NON_DEGRADED = "non_degraded"
    DEGRADED = "degraded"
    MISSING_BASELINE = "missing_baseline"
    MISSING_EXPORT = "missing_export"


class SkillRuleEvolutionAction(StrEnum):
    PREPARE = "prepare"
    COMPARE = "compare"
    APPLY = "apply"
    ROLLBACK = "rollback"


class SkillRuleEvolutionPrepareRequest(SkillRuleEvolutionModel):
    schema_id: Literal["zhimeng.skill-rule-evolution-prepare-request.v1"] = Field(
        default="zhimeng.skill-rule-evolution-prepare-request.v1",
        alias="schema",
    )
    skill_id: NonEmptyText = Field(max_length=128)
    target_version_id: SkillVersionId
    project_id: ProjectId | None = None
    change_set_id: ChangeSetId | None = None
    expected_current_version_id: SkillVersionId | None = None
    expected_state_revision: int | None = Field(default=None, ge=0)


class SkillRuleEvolutionActionRequest(SkillRuleEvolutionModel):
    schema_id: Literal["zhimeng.skill-rule-evolution-action-request.v1"] = Field(
        default="zhimeng.skill-rule-evolution-action-request.v1",
        alias="schema",
    )
    action: Literal[
        SkillRuleEvolutionAction.COMPARE,
        SkillRuleEvolutionAction.APPLY,
        SkillRuleEvolutionAction.ROLLBACK,
    ]
    expected_status: SkillRuleEvolutionStatus | None = None
    expected_state_revision: int | None = Field(default=None, ge=0)


class SkillRuleEvolutionReceipt(SkillRuleEvolutionModel):
    schema_id: Literal["zhimeng.skill-rule-evolution-receipt.v1"] = Field(
        default="zhimeng.skill-rule-evolution-receipt.v1",
        alias="schema",
    )
    evolution_id: EvolutionId
    skill_id: NonEmptyText = Field(max_length=128)
    project_id: ProjectId | None = None
    change_set_id: ChangeSetId | None = None
    status: SkillRuleEvolutionStatus
    from_version_id: SkillVersionId
    from_version: NonEmptyText = Field(max_length=128)
    from_package_fingerprint: Sha256
    to_version_id: SkillVersionId
    to_version: NonEmptyText = Field(max_length=128)
    to_package_fingerprint: Sha256
    freeze_recorded: Literal[True] = True
    compare_status: SkillRuleEvolutionCompareStatus
    eval_corpus_baseline_receipt_id: str | None = Field(default=None, max_length=160)
    eval_corpus_regression_receipt_id: str | None = Field(default=None, max_length=160)
    activation_receipt_id: str | None = Field(default=None, max_length=160)
    activation_state_revision: int | None = Field(default=None, ge=0)
    auto_upgrade: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    policy_version: Literal["local-v1-skill-rule-evolution-1"] = (
        "local-v1-skill-rule-evolution-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    updated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "SkillRuleEvolutionReceipt":
        if self.auto_upgrade is not False:
            raise ValueError("skill rule evolution cannot auto-upgrade")
        if self.formal_draft_allowed is not False:
            raise ValueError("skill rule evolution cannot unlock formal draft")
        if self.quality_assessed is not False:
            raise ValueError("skill rule evolution cannot claim quality assessed")
        if self.freeze_recorded is not True:
            raise ValueError("skill rule evolution must freeze from/to identities")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        expected = skill_rule_evolution_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("skill rule evolution receipt hash mismatch")
        return self


class SkillRuleEvolutionResponse(SkillRuleEvolutionModel):
    schema_id: Literal["zhimeng.skill-rule-evolution-response.v1"] = Field(
        default="zhimeng.skill-rule-evolution-response.v1",
        alias="schema",
    )
    receipt: SkillRuleEvolutionReceipt
    history: list[SkillRuleEvolutionReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "automatic_skill_rule_upgrade",
        "formal_prose_writeback",
        "local_v1_exe",
    )


def skill_rule_evolution_receipt_hash(receipt: SkillRuleEvolutionReceipt) -> str:
    material = {
        "schema": "zhimeng.skill-rule-evolution-receipt-hash.v1",
        "evolution_id": receipt.evolution_id,
        "skill_id": receipt.skill_id,
        "project_id": receipt.project_id,
        "change_set_id": receipt.change_set_id,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "from_version_id": receipt.from_version_id,
        "from_version": receipt.from_version,
        "from_package_fingerprint": receipt.from_package_fingerprint,
        "to_version_id": receipt.to_version_id,
        "to_version": receipt.to_version,
        "to_package_fingerprint": receipt.to_package_fingerprint,
        "freeze_recorded": True,
        "compare_status": (
            receipt.compare_status.value
            if hasattr(receipt.compare_status, "value")
            else receipt.compare_status
        ),
        "eval_corpus_baseline_receipt_id": receipt.eval_corpus_baseline_receipt_id,
        "eval_corpus_regression_receipt_id": receipt.eval_corpus_regression_receipt_id,
        "activation_receipt_id": receipt.activation_receipt_id,
        "activation_state_revision": receipt.activation_state_revision,
        "auto_upgrade": False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "updated_at": receipt.updated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "SkillRuleEvolutionAction",
    "SkillRuleEvolutionActionRequest",
    "SkillRuleEvolutionCompareStatus",
    "SkillRuleEvolutionPrepareRequest",
    "SkillRuleEvolutionReceipt",
    "SkillRuleEvolutionResponse",
    "SkillRuleEvolutionStatus",
    "skill_rule_evolution_receipt_hash",
]
