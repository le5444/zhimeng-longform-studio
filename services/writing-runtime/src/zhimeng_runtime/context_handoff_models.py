from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Sha256 = str
ContextHandoffStatus = Literal[
    "awaiting_upstream",
    "prepared",
    "approved",
    "rejected",
    "disabled",
    "running",
    "succeeded",
    "failed",
]


class ContextHandoffModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContextHandoffPrepareRequest(ContextHandoffModel):
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    profile: str = Field(default="chapter", min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    stage: str = Field(default="draft", min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    chapter_id: str = Field(default="CH001", min_length=1, max_length=256)
    task_intent: str = Field(
        default="为当前章节装配写前 Context Pack；只生成待审写作上下文，不写正文。",
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


class ContextHandoffDecisionRequest(ContextHandoffModel):
    expected_context_handoff_id: str = Field(pattern=r"^context-handoff-[0-9a-f]{32}$")


class ContextHandoffProjectRef(ContextHandoffModel):
    project_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class ContextHandoffWorkflowRef(ContextHandoffModel):
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(pattern=r"^workflow-checkpoint-[0-9a-f]{32}$")
    checkpoint_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    tool_call_id: str = Field(min_length=1, max_length=256)
    skill_call_id: str = Field(min_length=1, max_length=256)


class ContextHandoffRouteRequest(ContextHandoffModel):
    profile: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    stage: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    chapter_id: str = Field(min_length=1, max_length=256)
    task_intent: str = Field(min_length=1, max_length=4000)
    route_terms: list[str] = Field(default_factory=list, max_length=64)


class ContextHandoffIndexEntry(ContextHandoffModel):
    card_id: str = Field(min_length=1, max_length=160)
    card_type: Literal["work", "voice", "validator"]
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=512)
    route_terms: list[str] = Field(default_factory=list, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=32)
    safety_summary: str = Field(default="", max_length=2000)


class ContextHandoffUpstream(ContextHandoffModel):
    source_handoff_id: str = Field(pattern=r"^skill-handoff-[0-9a-f]{32}$")
    source_handoff_key: Literal["novel-distillation-to-kb-manager"] = "novel-distillation-to-kb-manager"
    source_skill_receipt_id: str = Field(pattern=r"^skill-receipt-[0-9a-f]{32}$")
    source_skill_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_result_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    distillation_index_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    distillation_index_artifact_path: str = Field(min_length=1, max_length=4096)
    distillation_index_artifact_file_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_project_revision: int = Field(ge=1)
    source_project_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_activation_skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    entries: tuple[ContextHandoffIndexEntry, ContextHandoffIndexEntry, ContextHandoffIndexEntry]
    no_full_card_text_copied: Literal[True]


class ContextHandoffDownstream(ContextHandoffModel):
    target_skill_id: Literal["novel-kb-manager"] = "novel-kb-manager"
    capability: Literal["assemble-context"] = "assemble-context"
    contract: Literal[
        "distillation_index entries become controlled Context Pack input; route_health ready is not validated"
    ] = "distillation_index entries become controlled Context Pack input; route_health ready is not validated"


class ContextHandoffActivation(ContextHandoffModel):
    managed_skill_id: Literal["novel-kb.context-handoff"] = "novel-kb.context-handoff"
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_version: str = Field(min_length=1, max_length=128)
    manifest_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    effective_scope: Literal["application", "project"]


class ContextHandoffApproval(ContextHandoffModel):
    approved: bool
    approved_by: Literal["author"] | None = None
    approved_at: datetime | None = None
    risk_acknowledged: bool = False


class ContextHandoffPermissions(ContextHandoffModel):
    read: tuple[Literal["bound_project_snapshot", "run_staging"], ...]
    write: tuple[Literal["run_staging", "runtime_context_store"], ...]
    network: Literal[False]
    secrets: tuple[str, ...] = ()


class ContextHandoffRunRecord(ContextHandoffModel):
    schema_id: Literal["zhimeng.context-handoff-run.v1"] = Field(
        default="zhimeng.context-handoff-run.v1",
        alias="schema",
    )
    context_handoff_id: str = Field(pattern=r"^context-handoff-[0-9a-f]{32}$")
    handoff_key: Literal["novel-kb-import-index-to-assemble-context"]
    status: ContextHandoffStatus
    project: ContextHandoffProjectRef
    workflow: ContextHandoffWorkflowRef | None = None
    upstream: ContextHandoffUpstream | None = None
    downstream: ContextHandoffDownstream
    route: ContextHandoffRouteRequest
    activation: ContextHandoffActivation | None = None
    approval: ContextHandoffApproval
    permissions: ContextHandoffPermissions
    constraints: dict
    formal_draft_allowed: Literal[False]
    skill_receipt_id: str | None = Field(default=None, pattern=r"^skill-receipt-[0-9a-f]{32}$")
    skill_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_artifact_path: str | None = None
    context_pack_id: str | None = Field(default=None, min_length=1, max_length=128)
    context_pack_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    route_receipt_id: str | None = Field(default=None, pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result: dict | None = None
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    created_at: datetime
    updated_at: datetime


class ContextHandoffResponse(ContextHandoffModel):
    schema_id: Literal["zhimeng.context-handoff-response.v1"] = Field(
        default="zhimeng.context-handoff-response.v1",
        alias="schema",
    )
    record: ContextHandoffRunRecord


__all__ = [
    "ContextHandoffDecisionRequest",
    "ContextHandoffPrepareRequest",
    "ContextHandoffResponse",
    "ContextHandoffRunRecord",
]
