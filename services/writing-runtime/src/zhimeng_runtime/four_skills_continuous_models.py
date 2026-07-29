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
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
ContinuousReceiptId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-continuous-[0-9a-f]{32}$")
]


class FourSkillsContinuousModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FourSkillsContinuousStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class FourSkillsContinuousStageCode(StrEnum):
    SOURCE_INGEST = "source_ingest"
    DISTILLATION_FORMALIZATION = "distillation_formalization"
    ADJACENT_O2 = "adjacent_o2"
    CREATION_SUITE = "creation_suite"


class FourSkillsContinuousEvaluateRequest(FourSkillsContinuousModel):
    schema_id: Literal["zhimeng.four-skills-continuous-request.v1"] = Field(
        default="zhimeng.four-skills-continuous-request.v1",
        alias="schema",
    )
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    source_import_id: str | None = Field(default=None, max_length=160)
    adjacent_chain_id: str | None = Field(default=None, max_length=160)
    managed_skill_chain_id: str | None = Field(default=None, max_length=160)


class FourSkillsContinuousStage(FourSkillsContinuousModel):
    code: FourSkillsContinuousStageCode
    ordinal: int = Field(ge=1, le=8)
    required: bool
    present: bool
    linked: bool
    status: NonEmptyText = Field(max_length=64)
    evidence_id: str | None = Field(default=None, max_length=160)
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsContinuousReceipt(FourSkillsContinuousModel):
    schema_id: Literal["zhimeng.four-skills-continuous-receipt.v1"] = Field(
        default="zhimeng.four-skills-continuous-receipt.v1",
        alias="schema",
    )
    receipt_id: ContinuousReceiptId
    project_id: ProjectId
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: Sha256 | None = None
    status: FourSkillsContinuousStatus
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    stages: list[FourSkillsContinuousStage] = Field(min_length=1, max_length=8)
    missing_required: list[FourSkillsContinuousStageCode] = Field(
        default_factory=list, max_length=8
    )
    source_import_id: str | None = Field(default=None, max_length=160)
    formalization_receipt_id: str | None = Field(default=None, max_length=160)
    adjacent_chain_id: str | None = Field(default=None, max_length=160)
    creation_suite_chain_id: str | None = Field(default=None, max_length=160)
    policy_version: Literal["local-v1-four-skills-continuous-1"] = (
        "local-v1-four-skills-continuous-1"
    )
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsContinuousReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("four skills continuous cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("four skills continuous cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("four skills continuous cannot write project truth")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError(
                "four skills continuous cannot claim four_skills_generic_exactly_once"
            )
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        ordinals = [item.ordinal for item in self.stages]
        if ordinals != sorted(ordinals):
            raise ValueError("four skills continuous stages must be ordered")
        expected_ready = self.status is FourSkillsContinuousStatus.COMPLETE
        if self.authorization_input_ready is not expected_ready:
            raise ValueError(
                "authorization_input_ready must match complete four skills continuous status"
            )
        if self.status is FourSkillsContinuousStatus.COMPLETE and self.missing_required:
            raise ValueError("complete continuous receipt cannot miss required stages")
        expected = four_skills_continuous_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("four skills continuous receipt hash mismatch")
        return self


class FourSkillsContinuousResponse(FourSkillsContinuousModel):
    schema_id: Literal["zhimeng.four-skills-continuous-response.v1"] = Field(
        default="zhimeng.four-skills-continuous-response.v1",
        alias="schema",
    )
    receipt: FourSkillsContinuousReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_continuous_receipt_hash(
    receipt: FourSkillsContinuousReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-continuous-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "four_skills_generic_exactly_once": False,
        "stages": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "ordinal": item.ordinal,
                "required": item.required,
                "present": item.present,
                "linked": item.linked,
                "status": item.status,
                "evidence_id": item.evidence_id,
                "detail": item.detail,
            }
            for item in receipt.stages
        ],
        "missing_required": [
            item.value if hasattr(item, "value") else item
            for item in receipt.missing_required
        ],
        "source_import_id": receipt.source_import_id,
        "formalization_receipt_id": receipt.formalization_receipt_id,
        "adjacent_chain_id": receipt.adjacent_chain_id,
        "creation_suite_chain_id": receipt.creation_suite_chain_id,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FourSkillsContinuousEvaluateRequest",
    "FourSkillsContinuousReceipt",
    "FourSkillsContinuousResponse",
    "FourSkillsContinuousStage",
    "FourSkillsContinuousStageCode",
    "FourSkillsContinuousStatus",
    "four_skills_continuous_receipt_hash",
]
