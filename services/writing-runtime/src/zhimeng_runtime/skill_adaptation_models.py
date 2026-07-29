from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Sha256 = str


class SkillAdaptationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SkillAdaptationPlanRequest(SkillAdaptationModel):
    source_path: str = Field(min_length=1, max_length=4096)

    @field_validator("source_path")
    @classmethod
    def normalize_source_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_path must not be blank")
        return normalized


class SkillAdaptationMaterializeRequest(SkillAdaptationModel):
    expected_plan_id: str = Field(pattern=r"^skill-adaptation-plan-[0-9a-f]{32}$")
    expected_plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    import_after_materialize: bool = True


class SkillAdaptationSource(SkillAdaptationModel):
    source_path: str = Field(min_length=1, max_length=4096)
    skill_md_path: str = Field(min_length=1, max_length=4096)
    skill_md_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    skill_md_bytes: int = Field(ge=1)
    frontmatter_name: str = Field(min_length=1, max_length=128)
    frontmatter_description: str = Field(min_length=1, max_length=16_384)
    locator_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class SkillAdaptationTarget(SkillAdaptationModel):
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    display_name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=16_384)
    kind: Literal["instruction_source"] = "instruction_source"
    version: str = Field(min_length=1, max_length=128)
    capability: Literal["raw-skill.instruction-source.v1"] = (
        "raw-skill.instruction-source.v1"
    )
    resources: tuple[str, ...] = ("instruction-map.json", "resources/SKILL.md")
    formal_draft_allowed: Literal[False] = False
    executable: Literal[False] = False


class SkillAdaptationMissing(SkillAdaptationModel):
    code: str = Field(min_length=2, max_length=128)
    message: str = Field(min_length=1, max_length=2048)


class SkillAdaptationPlan(SkillAdaptationModel):
    schema_id: Literal["zhimeng.skill-adaptation-plan.v1"] = Field(
        default="zhimeng.skill-adaptation-plan.v1",
        alias="schema",
    )
    plan_id: str = Field(pattern=r"^skill-adaptation-plan-[0-9a-f]{32}$")
    adapter_id: Literal["zhimeng.raw-skill-instruction"] = "zhimeng.raw-skill-instruction"
    adapter_version: Literal["1"] = "1"
    adapter_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready_to_materialize"]
    source: SkillAdaptationSource
    target: SkillAdaptationTarget
    missing: tuple[SkillAdaptationMissing, ...]
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "deterministic_tool_auto_generation",
        "formal_prose_writeback",
        "local_v1_exe",
    )
    created_at: datetime
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class SkillAdaptationMaterialization(SkillAdaptationModel):
    schema_id: Literal["zhimeng.skill-adaptation-materialization.v1"] = Field(
        default="zhimeng.skill-adaptation-materialization.v1",
        alias="schema",
    )
    materialization_id: str = Field(
        pattern=r"^skill-adaptation-materialization-[0-9a-f]{32}$"
    )
    plan_id: str = Field(pattern=r"^skill-adaptation-plan-[0-9a-f]{32}$")
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    package_root: str = Field(min_length=1, max_length=4096)
    package_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    skill_id: str = Field(min_length=3, max_length=128)
    skill_version: str = Field(min_length=1, max_length=128)
    kind: Literal["instruction_source"] = "instruction_source"
    formal_draft_allowed: Literal[False] = False
    imported: bool = False
    skill_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    import_receipt_id: str | None = Field(default=None, min_length=1, max_length=256)
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "deterministic_tool_auto_generation",
        "formal_prose_writeback",
        "local_v1_exe",
    )
    created_at: datetime
    materialization_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class SkillAdaptationPlanResponse(SkillAdaptationModel):
    schema_id: Literal["zhimeng.skill-adaptation-plan-response.v1"] = Field(
        default="zhimeng.skill-adaptation-plan-response.v1",
        alias="schema",
    )
    plan: SkillAdaptationPlan


class SkillAdaptationMaterializationResponse(SkillAdaptationModel):
    schema_id: Literal["zhimeng.skill-adaptation-materialization-response.v1"] = Field(
        default="zhimeng.skill-adaptation-materialization-response.v1",
        alias="schema",
    )
    materialization: SkillAdaptationMaterialization
    import_result: dict[str, Any] | None = None


__all__ = [
    "SkillAdaptationMaterialization",
    "SkillAdaptationMaterializationRequest",
    "SkillAdaptationMaterializationResponse",
    "SkillAdaptationPlan",
    "SkillAdaptationPlanRequest",
    "SkillAdaptationPlanResponse",
]
