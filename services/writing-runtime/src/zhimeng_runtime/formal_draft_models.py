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


class FormalDraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FormalDraftDecision(StrEnum):
    DENIED = "denied"


class FormalDraftPrerequisiteCode(StrEnum):
    PROJECT_BOUND = "project_bound"
    FOUR_SKILLS_CONTINUOUS_O2 = "four_skills_continuous_o2"
    LITERARY_QUALITY_GATE = "literary_quality_gate"
    AUTHOR_EXPLICIT_AUTHORIZATION = "author_explicit_authorization"
    HARD_FACT_GATE_PASS = "hard_fact_gate_pass"
    GATE_PIPELINE_COMPLETE = "gate_pipeline_complete"
    PROVENANCE_COMPLETE = "provenance_complete"


class FormalDraftDenialCode(StrEnum):
    LOCAL_V1_POLICY_LOCKED = "local_v1_policy_locked"
    AUTHORIZATION_CONTRACT_UNPROVEN = "authorization_contract_unproven"
    FOUR_SKILLS_CHAIN_INCOMPLETE = "four_skills_chain_incomplete"
    LITERARY_QUALITY_GATE_MISSING = "literary_quality_gate_missing"
    HARD_FACT_GATE_INCOMPLETE = "hard_fact_gate_incomplete"
    AUTHOR_AUTHORIZATION_MISSING = "author_authorization_missing"
    GATE_PIPELINE_INCOMPLETE = "gate_pipeline_incomplete"
    PROVENANCE_INCOMPLETE = "provenance_incomplete"


class FormalDraftEvaluateRequest(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-authorization-request.v1"] = Field(
        default="zhimeng.formal-draft-authorization-request.v1",
        alias="schema",
    )
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    context_pack_id: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=160)
    change_set_id: str | None = Field(default=None, max_length=160)


class FormalDraftPrerequisite(FormalDraftModel):
    code: FormalDraftPrerequisiteCode
    satisfied: bool
    detail: NonEmptyText = Field(max_length=1024)


class FormalDraftDenial(FormalDraftModel):
    code: FormalDraftDenialCode
    message: NonEmptyText = Field(max_length=1024)


class FormalDraftAuthorizationReceipt(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-authorization-receipt.v1"] = Field(
        default="zhimeng.formal-draft-authorization-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^formal-draft-auth-[0-9a-f]{32}$")
    project_id: ProjectId
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: Sha256 | None = None
    context_pack_id: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=160)
    change_set_id: str | None = Field(default=None, max_length=160)
    decision: Literal[FormalDraftDecision.DENIED] = FormalDraftDecision.DENIED
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    prerequisites: list[FormalDraftPrerequisite] = Field(min_length=1, max_length=32)
    denials: list[FormalDraftDenial] = Field(min_length=1, max_length=32)
    policy_version: Literal["local-v1-locked-1"] = "local-v1-locked-1"
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_denied(self) -> "FormalDraftAuthorizationReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("formal draft authorization receipt cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("formal draft authorization receipt cannot allow project truth writes")
        if self.decision is not FormalDraftDecision.DENIED:
            raise ValueError("Local v1 formal draft authorization can only deny")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if any(item.satisfied for item in self.prerequisites if item.code is not FormalDraftPrerequisiteCode.PROJECT_BOUND):
            # Future: when real gates exist, satisfied prerequisites are fine while overall still denied.
            pass
        expected = formal_draft_authorization_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("formal draft authorization receipt hash mismatch")
        return self


class FormalDraftAuthorizationResponse(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-authorization-response.v1"] = Field(
        default="zhimeng.formal-draft-authorization-response.v1",
        alias="schema",
    )
    receipt: FormalDraftAuthorizationReceipt
    not_claiming: tuple[str, ...] = (
        "formal_prose_writeback",
        "four_skills_generic_exactly_once",
        "literary_quality_gate_complete",
        "local_v1_exe",
    )



class FormalDraftAuthorIntent(StrEnum):
    REQUEST_AUTHORIZATION = "request_authorization"
    WITHHOLD_AUTHORIZATION = "withhold_authorization"
    REVOKE_AUTHORIZATION = "revoke_authorization"


class FormalDraftAuthorDecisionRequest(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-author-decision-request.v1"] = Field(
        default="zhimeng.formal-draft-author-decision-request.v1",
        alias="schema",
    )
    intent: FormalDraftAuthorIntent
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    change_set_id: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=2000)


class FormalDraftAuthorDecisionReceipt(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-author-decision-receipt.v1"] = Field(
        default="zhimeng.formal-draft-author-decision-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^formal-draft-author-[0-9a-f]{32}$")
    project_id: ProjectId
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: Sha256 | None = None
    change_set_id: str | None = Field(default=None, max_length=160)
    intent: FormalDraftAuthorIntent
    note: str | None = Field(default=None, max_length=2000)
    authorization_effective: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-locked-1"] = "local-v1-locked-1"
    decided_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_author_decision(self) -> "FormalDraftAuthorDecisionReceipt":
        if self.authorization_effective is not False:
            raise ValueError("author decision cannot make formal draft effective in Local v1")
        if self.formal_draft_allowed is not False:
            raise ValueError("author decision cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("author decision cannot allow project truth writes")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        expected = formal_draft_author_decision_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("formal draft author decision receipt hash mismatch")
        return self


class FormalDraftAuthorDecisionResponse(FormalDraftModel):
    schema_id: Literal["zhimeng.formal-draft-author-decision-response.v1"] = Field(
        default="zhimeng.formal-draft-author-decision-response.v1",
        alias="schema",
    )
    receipt: FormalDraftAuthorDecisionReceipt | None = None
    history: list[FormalDraftAuthorDecisionReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "formal_prose_writeback",
        "formal_draft_authorization_grant",
        "four_skills_generic_exactly_once",
        "literary_quality_gate_complete",
        "local_v1_exe",
    )


def formal_draft_author_decision_receipt_hash(
    receipt: FormalDraftAuthorDecisionReceipt,
) -> str:
    material = {
        "schema": "zhimeng.formal-draft-author-decision-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "change_set_id": receipt.change_set_id,
        "intent": receipt.intent.value if hasattr(receipt.intent, "value") else receipt.intent,
        "note": receipt.note,
        "authorization_effective": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "decided_at": receipt.decided_at.isoformat(),
    }
    return canonical_json_hash(material)

def formal_draft_authorization_receipt_hash(
    receipt: FormalDraftAuthorizationReceipt,
) -> str:
    material = {
        "schema": "zhimeng.formal-draft-authorization-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "context_pack_id": receipt.context_pack_id,
        "run_id": receipt.run_id,
        "change_set_id": receipt.change_set_id,
        "decision": receipt.decision.value,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "prerequisites": [
            {
                "code": item.code.value,
                "satisfied": item.satisfied,
                "detail": item.detail,
            }
            for item in receipt.prerequisites
        ],
        "denials": [
            {"code": item.code.value, "message": item.message}
            for item in receipt.denials
        ],
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FormalDraftAuthorizationReceipt",
    "FormalDraftAuthorizationResponse",
    "FormalDraftAuthorDecisionReceipt",
    "FormalDraftAuthorDecisionRequest",
    "FormalDraftAuthorDecisionResponse",
    "FormalDraftAuthorIntent",
    "FormalDraftDecision",
    "FormalDraftDenial",
    "FormalDraftDenialCode",
    "FormalDraftEvaluateRequest",
    "FormalDraftPrerequisite",
    "FormalDraftPrerequisiteCode",
    "formal_draft_authorization_receipt_hash",
    "formal_draft_author_decision_receipt_hash",
]
