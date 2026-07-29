from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Sha256 = str
ChainStatus = Literal[
    "prepared",
    "approved",
    "rejected",
    "disabled",
    "running",
    "succeeded",
    "failed",
]
StepStatus = Literal[
    "pending",
    "prepared",
    "approved",
    "running",
    "succeeded",
    "failed",
    "skipped",
]
DomainStatus = Literal["healthy", "degraded", "blocked"]


class ManagedSkillChainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ManagedSkillChainStepSpec(ManagedSkillChainModel):
    step_id: str = Field(
        min_length=2,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    title: str = Field(min_length=1, max_length=256)
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    capability: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    input_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class ManagedSkillChainPrepareRequest(ManagedSkillChainModel):
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    chain_key: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
    )
    steps: tuple[ManagedSkillChainStepSpec, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_steps(self) -> "ManagedSkillChainPrepareRequest":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("managed skill chain step_id values must be unique")
        return self


class ManagedSkillChainDecisionRequest(ManagedSkillChainModel):
    expected_chain_id: str = Field(pattern=r"^managed-skill-chain-[0-9a-f]{32}$")


class ManagedSkillChainProjectRef(ManagedSkillChainModel):
    project_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    root_path: str = Field(min_length=1, max_length=4096)


class ManagedSkillChainStep(ManagedSkillChainModel):
    step_id: str = Field(min_length=2, max_length=128)
    ordinal: int = Field(ge=1, le=8)
    title: str = Field(min_length=1, max_length=256)
    skill_id: str = Field(min_length=3, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    status: StepStatus
    input_payload: dict[str, Any] = Field(default_factory=dict)
    input_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    skill_receipt_id: str | None = Field(
        default=None,
        pattern=r"^skill-receipt-[0-9a-f]{32}$",
    )
    skill_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    domain_status: DomainStatus | None = None
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)


class ManagedSkillChainApproval(ManagedSkillChainModel):
    approved: bool
    approved_by: Literal["author"] | None = None
    approved_at: datetime | None = None
    risk_acknowledged: bool = False


class ManagedSkillChainPermissions(ManagedSkillChainModel):
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    network: Literal[False] = False
    secrets: tuple[()] = ()


class ManagedSkillChainSkillRisk(ManagedSkillChainModel):
    step_id: str = Field(min_length=2, max_length=128)
    skill_id: str = Field(min_length=3, max_length=128)
    capability: str = Field(min_length=1, max_length=128)
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_version: str = Field(min_length=1, max_length=128)
    manifest_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    effective_scope: Literal["application", "project"]
    input_contract: str = Field(
        min_length=1,
        max_length=2048,
        pattern=r"^https://zhimeng\.local/contracts/[A-Za-z0-9._/-]+\.schema\.json$",
    )
    output_contract: str = Field(
        min_length=1,
        max_length=2048,
        pattern=r"^https://zhimeng\.local/contracts/[A-Za-z0-9._/-]+\.schema\.json$",
    )


class ManagedSkillChainRiskDossier(ManagedSkillChainModel):
    formal_draft_allowed: Literal[False] = False
    network: Literal[False] = False
    secrets: tuple[()] = ()
    permissions: ManagedSkillChainPermissions
    skills: tuple[ManagedSkillChainSkillRisk, ...] = Field(min_length=1, max_length=8)
    risk_summary: str = Field(min_length=1, max_length=4000)
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
        "formal_prose_writeback",
    )


class ManagedSkillChainRiskApprovalToken(ManagedSkillChainModel):
    token_id: str = Field(pattern=r"^risk-token-[0-9a-f]{32}$")
    chain_id: str = Field(pattern=r"^managed-skill-chain-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=256)
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    dossier: ManagedSkillChainRiskDossier
    approved_by: Literal["author"]
    approved_at: datetime
    consumed: bool = False
    token_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedSkillChainPlanStep(ManagedSkillChainModel):
    workflow_step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    step_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=1, le=8)
    runner: Literal["managed_skill"] = "managed_skill"
    skill_id: str = Field(min_length=3, max_length=128)
    expected_skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    expected_skill_version: str = Field(min_length=1, max_length=128)
    expected_manifest_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    expected_package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    input_contract: str = Field(min_length=1, max_length=2048)
    output_contract: str = Field(min_length=1, max_length=2048)
    input_builder_id: Literal["author-supplied-input"] = "author-supplied-input"
    input_builder_version: Literal["1"] = "1"
    input_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    formal_draft_allowed: Literal[False] = False


class ManagedSkillChainPlan(ManagedSkillChainModel):
    schema_id: Literal["zhimeng.managed-skill-chain-plan.v1"] = Field(
        default="zhimeng.managed-skill-chain-plan.v1",
        alias="schema",
    )
    workflow_plan_id: str = Field(pattern=r"^managed-skill-chain-plan-[0-9a-f]{32}$")
    chain_id: str = Field(pattern=r"^managed-skill-chain-[0-9a-f]{32}$")
    chain_key: str = Field(min_length=2, max_length=64)
    project_id: str = Field(min_length=1, max_length=256)
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    risk_token_id: str = Field(pattern=r"^risk-token-[0-9a-f]{32}$")
    risk_token_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    steps: tuple[ManagedSkillChainPlanStep, ...] = Field(min_length=1, max_length=8)
    formal_draft_allowed: Literal[False] = False
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
        "formal_prose_writeback",
    )
    created_at: datetime
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedSkillChainCheckpoint(ManagedSkillChainModel):
    schema_id: Literal["zhimeng.managed-skill-chain-checkpoint.v1"] = Field(
        default="zhimeng.managed-skill-chain-checkpoint.v1",
        alias="schema",
    )
    checkpoint_id: str = Field(
        pattern=r"^managed-skill-chain-checkpoint-[0-9a-f]{32}$"
    )
    workflow_plan_id: str = Field(pattern=r"^managed-skill-chain-plan-[0-9a-f]{32}$")
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    chain_id: str = Field(pattern=r"^managed-skill-chain-[0-9a-f]{32}$")
    workflow_step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    step_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    step_ordinal: int = Field(ge=1, le=8)
    skill_id: str = Field(min_length=3, max_length=128)
    expected_skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    expected_package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    previous_checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^managed-skill-chain-checkpoint-[0-9a-f]{32}$",
    )
    skill_receipt_id: str | None = Field(
        default=None,
        pattern=r"^skill-receipt-[0-9a-f]{32}$",
    )
    skill_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_status: Literal["succeeded", "failed"]
    domain_status: DomainStatus | None = None
    formal_draft_allowed: Literal[False] = False
    created_at: datetime
    checkpoint_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedSkillChainRunRecord(ManagedSkillChainModel):
    schema_id: Literal["zhimeng.managed-skill-chain-run.v1"] = Field(
        default="zhimeng.managed-skill-chain-run.v1",
        alias="schema",
    )
    chain_id: str = Field(pattern=r"^managed-skill-chain-[0-9a-f]{32}$")
    chain_key: str = Field(min_length=2, max_length=64)
    status: ChainStatus
    project: ManagedSkillChainProjectRef
    steps: tuple[ManagedSkillChainStep, ...] = Field(min_length=1, max_length=8)
    approval: ManagedSkillChainApproval
    permissions: ManagedSkillChainPermissions
    risk_dossier: ManagedSkillChainRiskDossier | None = None
    risk_approval_token: ManagedSkillChainRiskApprovalToken | None = None
    workflow_plan: ManagedSkillChainPlan | None = None
    workflow_checkpoints: tuple[ManagedSkillChainCheckpoint, ...] = ()
    formal_draft_allowed: Literal[False] = False
    current_step_ordinal: int = Field(ge=0, le=8)
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    created_at: datetime
    updated_at: datetime


class ManagedSkillChainResponse(ManagedSkillChainModel):
    schema_id: Literal["zhimeng.managed-skill-chain-response.v1"] = Field(
        default="zhimeng.managed-skill-chain-response.v1",
        alias="schema",
    )
    record: ManagedSkillChainRunRecord
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
        "formal_prose_writeback",
    )


__all__ = [
    "ManagedSkillChainCheckpoint",
    "ManagedSkillChainDecisionRequest",
    "ManagedSkillChainPlan",
    "ManagedSkillChainPrepareRequest",
    "ManagedSkillChainResponse",
    "ManagedSkillChainRiskApprovalToken",
    "ManagedSkillChainRiskDossier",
    "ManagedSkillChainRunRecord",
    "ManagedSkillChainSkillRisk",
    "ManagedSkillChainStepSpec",
]
