from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Sha256 = str
ChainStatus = Literal[
    "awaiting_upstream",
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


class AdjacentChainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AdjacentChainPrepareRequest(AdjacentChainModel):
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_import_id: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    profile: str = Field(
        default="chapter",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    stage: str = Field(
        default="draft",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    chapter_id: str = Field(default="CH001", min_length=1, max_length=256)
    task_intent: str = Field(
        default="把 O2-D distillation_index 连续装配成写前 Context Pack；不写正文。",
        min_length=1,
        max_length=4000,
    )

    @field_validator("chapter_id", "task_intent")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("field must not be blank")
        return normalized


class AdjacentChainDecisionRequest(AdjacentChainModel):
    expected_chain_id: str = Field(pattern=r"^adjacent-chain-[0-9a-f]{32}$")


class AdjacentChainProjectRef(AdjacentChainModel):
    project_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class AdjacentChainStep(AdjacentChainModel):
    step_id: str = Field(min_length=3, max_length=128)
    ordinal: int = Field(ge=1, le=8)
    title: str = Field(min_length=1, max_length=256)
    skill_id: str = Field(min_length=3, max_length=128)
    capability: str = Field(min_length=3, max_length=128)
    status: StepStatus
    child_id: str | None = None
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)


class AdjacentChainApproval(AdjacentChainModel):
    approved: bool
    approved_by: Literal["author"] | None = None
    approved_at: datetime | None = None
    risk_acknowledged: bool = False


class AdjacentChainPermissions(AdjacentChainModel):
    read: tuple[Literal["bound_project_snapshot", "run_staging"], ...]
    write: tuple[Literal["run_staging", "runtime_context_store"], ...]
    network: Literal[False]
    secrets: tuple[str, ...] = ()


class AdjacentChainRoute(AdjacentChainModel):
    profile: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    stage: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    chapter_id: str = Field(min_length=1, max_length=256)
    task_intent: str = Field(min_length=1, max_length=4000)



class AdjacentChainSkillRisk(AdjacentChainModel):
    step_id: str = Field(min_length=3, max_length=128)
    skill_id: str = Field(min_length=3, max_length=128)
    capability: str = Field(min_length=3, max_length=128)
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


class AdjacentChainRiskDossier(AdjacentChainModel):
    formal_draft_allowed: Literal[False] = False
    network: Literal[False] = False
    secrets: tuple[()] = ()
    permissions: AdjacentChainPermissions
    skills: tuple[AdjacentChainSkillRisk, AdjacentChainSkillRisk]
    risk_summary: str = Field(min_length=1, max_length=4000)
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
        "formal_prose_writeback",
    )


class AdjacentChainRiskApprovalToken(AdjacentChainModel):
    token_id: str = Field(pattern=r"^risk-token-[0-9a-f]{32}$")
    chain_id: str = Field(pattern=r"^adjacent-chain-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=256)
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    dossier: AdjacentChainRiskDossier
    approved_by: Literal["author"]
    approved_at: datetime
    consumed: bool = False
    token_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")




class AdjacentWorkflowPlanStep(AdjacentChainModel):
    workflow_step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    step_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=1, le=2)
    runner: Literal["managed_skill"] = "managed_skill"
    skill_id: str = Field(min_length=3, max_length=128)
    expected_skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    expected_skill_version: str = Field(min_length=1, max_length=128)
    expected_manifest_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    expected_package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    input_contract: str = Field(min_length=1, max_length=2048)
    output_contract: str = Field(min_length=1, max_length=2048)
    input_builder_id: str = Field(min_length=3, max_length=128)
    input_builder_version: str = Field(min_length=1, max_length=128)
    formal_draft_allowed: Literal[False] = False


class AdjacentWorkflowPlan(AdjacentChainModel):
    schema_id: Literal["zhimeng.adjacent-workflow-plan.v1"] = Field(
        default="zhimeng.adjacent-workflow-plan.v1",
        alias="schema",
    )
    workflow_plan_id: str = Field(pattern=r"^adjacent-workflow-plan-[0-9a-f]{32}$")
    chain_id: str = Field(pattern=r"^adjacent-chain-[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=256)
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    risk_token_id: str = Field(pattern=r"^risk-token-[0-9a-f]{32}$")
    risk_token_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    steps: tuple[AdjacentWorkflowPlanStep, AdjacentWorkflowPlanStep]
    formal_draft_allowed: Literal[False] = False
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
        "formal_prose_writeback",
    )
    created_at: datetime
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class AdjacentWorkflowCheckpoint(AdjacentChainModel):
    schema_id: Literal["zhimeng.adjacent-workflow-checkpoint.v1"] = Field(
        default="zhimeng.adjacent-workflow-checkpoint.v1",
        alias="schema",
    )
    checkpoint_id: str = Field(pattern=r"^adjacent-workflow-checkpoint-[0-9a-f]{32}$")
    workflow_plan_id: str = Field(pattern=r"^adjacent-workflow-plan-[0-9a-f]{32}$")
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    chain_id: str = Field(pattern=r"^adjacent-chain-[0-9a-f]{32}$")
    workflow_step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    step_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    step_ordinal: int = Field(ge=1, le=2)
    skill_id: str = Field(min_length=3, max_length=128)
    expected_skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    expected_package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    previous_checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^adjacent-workflow-checkpoint-[0-9a-f]{32}$",
    )
    child_handoff_id: str | None = None
    skill_receipt_id: str | None = Field(
        default=None,
        pattern=r"^skill-receipt-[0-9a-f]{32}$",
    )
    skill_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_status: Literal["succeeded", "failed"]
    formal_draft_allowed: Literal[False] = False
    created_at: datetime
    checkpoint_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class AdjacentChainRunRecord(AdjacentChainModel):
    schema_id: Literal["zhimeng.adjacent-skill-chain-run.v1"] = Field(
        default="zhimeng.adjacent-skill-chain-run.v1",
        alias="schema",
    )
    chain_id: str = Field(pattern=r"^adjacent-chain-[0-9a-f]{32}$")
    chain_key: Literal["novel-distillation-kb-to-context"]
    status: ChainStatus
    project: AdjacentChainProjectRef
    route: AdjacentChainRoute
    steps: tuple[AdjacentChainStep, AdjacentChainStep]
    approval: AdjacentChainApproval
    permissions: AdjacentChainPermissions
    risk_dossier: AdjacentChainRiskDossier | None = None
    risk_approval_token: AdjacentChainRiskApprovalToken | None = None
    workflow_plan: AdjacentWorkflowPlan | None = None
    workflow_checkpoints: tuple[AdjacentWorkflowCheckpoint, ...] = ()
    formal_draft_allowed: Literal[False]
    source_import_id: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    skill_handoff_id: str | None = Field(
        default=None,
        pattern=r"^skill-handoff-[0-9a-f]{32}$",
    )
    context_handoff_id: str | None = Field(
        default=None,
        pattern=r"^context-handoff-[0-9a-f]{32}$",
    )
    context_pack_id: str | None = Field(default=None, min_length=1, max_length=128)
    context_pack_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    route_receipt_id: str | None = Field(
        default=None,
        pattern=r"^nlcl-route-[0-9a-f]{32}$",
    )
    route_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    current_step_ordinal: int = Field(ge=0, le=2)
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    created_at: datetime
    updated_at: datetime


class AdjacentChainResponse(AdjacentChainModel):
    schema_id: Literal["zhimeng.adjacent-skill-chain-response.v1"] = Field(
        default="zhimeng.adjacent-skill-chain-response.v1",
        alias="schema",
    )
    record: AdjacentChainRunRecord
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "creation_suite_drafting",
        "reader_gate",
        "local_v1_exe",
    )


__all__ = [
    "AdjacentChainDecisionRequest",
    "AdjacentChainPrepareRequest",
    "AdjacentChainResponse",
    "AdjacentChainRiskApprovalToken",
    "AdjacentChainRiskDossier",
    "AdjacentChainRunRecord",
    "AdjacentChainSkillRisk",
    "AdjacentWorkflowCheckpoint",
    "AdjacentWorkflowPlan",
    "AdjacentWorkflowPlanStep",
]
