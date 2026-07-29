from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Sha256 = str
HandoffStatus = Literal[
    "awaiting_upstream",
    "prepared",
    "approved",
    "rejected",
    "disabled",
    "running",
    "succeeded",
    "failed",
]


class SkillHandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SkillHandoffPrepareRequest(SkillHandoffModel):
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_import_id: str | None = Field(default=None, pattern=r"^source-import-[0-9a-f]{32}$")


class SkillHandoffDecisionRequest(SkillHandoffModel):
    expected_handoff_id: str = Field(pattern=r"^skill-handoff-[0-9a-f]{32}$")


class SkillHandoffProjectRef(SkillHandoffModel):
    project_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class SkillHandoffWorkflowRef(SkillHandoffModel):
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(pattern=r"^workflow-checkpoint-[0-9a-f]{32}$")
    checkpoint_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1, max_length=256)
    skill_call_id: str = Field(min_length=1, max_length=256)


class SkillHandoffCardRef(SkillHandoffModel):
    card_id: str = Field(min_length=1, max_length=160)
    card_type: Literal["work", "voice", "validator"]
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=2000)
    route_terms: list[str] = Field(max_length=32)
    tags: list[str] = Field(max_length=32)
    safety_summary: str = Field(max_length=2000)


class SkillHandoffUpstream(SkillHandoffModel):
    source_skill_id: Literal["novel-distillation"] = "novel-distillation"
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    formalization_receipt_id: str = Field(pattern=r"^knowledge-formalization-[0-9a-f]{32}$")
    formalization_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cards_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cards: tuple[SkillHandoffCardRef, SkillHandoffCardRef, SkillHandoffCardRef]


class SkillHandoffDownstream(SkillHandoffModel):
    target_skill_id: Literal["novel-kb-manager"] = "novel-kb-manager"
    capability: Literal["import-distillation-index"] = "import-distillation-index"
    contract: Literal[
        "distillation_index active-card entries only; no full card text copied"
    ] = "distillation_index active-card entries only; no full card text copied"


class SkillHandoffActivation(SkillHandoffModel):
    managed_skill_id: Literal["novel-distillation.kb-handoff"] = "novel-distillation.kb-handoff"
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_version: str = Field(min_length=1, max_length=128)
    manifest_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    effective_scope: Literal["application", "project"]


class SkillHandoffApproval(SkillHandoffModel):
    approved: bool
    approved_by: Literal["author"] | None = None
    approved_at: datetime | None = None
    risk_acknowledged: bool = False


class SkillHandoffPermissions(SkillHandoffModel):
    read: tuple[Literal["bound_project_snapshot"], ...]
    write: tuple[Literal["run_staging"], ...]
    network: Literal[False]
    secrets: tuple[str, ...] = ()


class SkillHandoffRunRecord(SkillHandoffModel):
    schema_id: Literal["zhimeng.skill-handoff-run.v1"] = Field(
        default="zhimeng.skill-handoff-run.v1",
        alias="schema",
    )
    handoff_id: str = Field(pattern=r"^skill-handoff-[0-9a-f]{32}$")
    handoff_key: Literal["novel-distillation-to-kb-manager"]
    status: HandoffStatus
    project: SkillHandoffProjectRef
    workflow: SkillHandoffWorkflowRef | None = None
    upstream: SkillHandoffUpstream | None = None
    downstream: SkillHandoffDownstream
    activation: SkillHandoffActivation | None = None
    approval: SkillHandoffApproval
    permissions: SkillHandoffPermissions
    formal_draft_allowed: Literal[False]
    skill_receipt_id: str | None = Field(default=None, pattern=r"^skill-receipt-[0-9a-f]{32}$")
    skill_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_artifact_path: str | None = None
    result: dict | None = None
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    created_at: datetime
    updated_at: datetime


class SkillHandoffResponse(SkillHandoffModel):
    schema_id: Literal["zhimeng.skill-handoff-response.v1"] = Field(
        default="zhimeng.skill-handoff-response.v1",
        alias="schema",
    )
    record: SkillHandoffRunRecord


__all__ = [
    "SkillHandoffPrepareRequest",
    "SkillHandoffDecisionRequest",
    "SkillHandoffResponse",
    "SkillHandoffRunRecord",
]
