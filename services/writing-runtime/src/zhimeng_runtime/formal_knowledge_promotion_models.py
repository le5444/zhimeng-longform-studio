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
FeedbackCandidateId = Annotated[
    str, StringConstraints(pattern=r"^feedback-candidate-[0-9a-f]{32}$")
]
FormalKnowledgePromotionReceiptId = Annotated[
    str, StringConstraints(pattern=r"^formal-knowledge-promotion-[0-9a-f]{32}$")
]
FormalKnowledgePromotionPackageId = Annotated[
    str, StringConstraints(pattern=r"^formal-knowledge-promotion-package-[0-9a-f]{32}$")
]


class FormalKnowledgePromotionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FormalKnowledgePromotionStatus(StrEnum):
    READY = "ready"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    STAGED = "staged"
    MATERIALIZED = "materialized"
    AUTHOR_FORMALIZATION_REQUESTED = "author_formalization_requested"
    AUTHOR_FORMALIZATION_WITHHELD = "author_formalization_withheld"
    AUTHOR_FORMALIZATION_COMMITTED = "author_formalization_committed"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class FormalKnowledgePromotionCheckCode(StrEnum):
    CHANGE_SET_TERMINAL = "change_set_terminal"
    FEEDBACK_BUNDLE_PRESENT = "feedback_bundle_present"
    EXPERIENCE_RECORDED = "experience_recorded"
    REGRESSION_PASS = "regression_pass"
    EVAL_CORPUS_BASELINE_GATE = "eval_corpus_baseline_gate"
    NO_FORMAL_CONTAMINATION = "no_formal_contamination"
    FORMAL_DRAFT_LOCKED = "formal_draft_locked"


class FormalKnowledgePromotionAction(StrEnum):
    STAGE_FOR_REVIEW = "stage_for_review"
    MATERIALIZE_FOR_REVIEW = "materialize_for_review"
    AUTHOR_REQUEST_FORMALIZATION = "author_request_formalization"
    AUTHOR_WITHHOLD_FORMALIZATION = "author_withhold_formalization"
    COMMIT_AUTHOR_ATTESTED_FORMAL_KNOWLEDGE = "commit_author_attested_formal_knowledge"
    ROLLBACK_AUTHOR_ATTESTED_FORMAL_KNOWLEDGE = "rollback_author_attested_formal_knowledge"
    REJECT = "reject"
    ROLLBACK_STAGE = "rollback_stage"


class FormalKnowledgePromotionEvaluateRequest(FormalKnowledgePromotionModel):
    schema_id: Literal["zhimeng.formal-knowledge-promotion-request.v1"] = Field(
        default="zhimeng.formal-knowledge-promotion-request.v1",
        alias="schema",
    )
    expected_feedback_candidate_bundle_hash: Sha256 | None = None
    candidate_ids: list[FeedbackCandidateId] = Field(default_factory=list, max_length=64)


class FormalKnowledgePromotionDecision(FormalKnowledgePromotionModel):
    candidate_id: FeedbackCandidateId
    action: FormalKnowledgePromotionAction


class FormalKnowledgePromotionActionRequest(FormalKnowledgePromotionModel):
    schema_id: Literal["zhimeng.formal-knowledge-promotion-action-request.v1"] = Field(
        default="zhimeng.formal-knowledge-promotion-action-request.v1",
        alias="schema",
    )
    expected_feedback_candidate_bundle_hash: Sha256
    decisions: list[FormalKnowledgePromotionDecision] = Field(
        min_length=1,
        max_length=64,
    )


class FormalKnowledgePromotionCheck(FormalKnowledgePromotionModel):
    code: FormalKnowledgePromotionCheckCode
    required: bool
    present: bool
    detail: NonEmptyText = Field(max_length=1024)
    candidate_id: FeedbackCandidateId | None = None


class FormalKnowledgePromotionItem(FormalKnowledgePromotionModel):
    candidate_id: FeedbackCandidateId
    finding_id: NonEmptyText = Field(max_length=128)
    finding_kind: Literal["fact_risk", "continuity_risk", "subjective_note"]
    summary: NonEmptyText = Field(max_length=500)
    evidence_quote: str = Field(default="", max_length=1000)
    relative_path: str | None = Field(default=None, max_length=512)
    experience_relative_path: str | None = Field(default=None, max_length=512)
    promotion_status: Literal[
        "experience_recorded",
        "formal_promotion_staged",
        "formal_promotion_materialized",
        "formal_promotion_author_requested",
        "formal_promotion_author_withheld",
        "formal_promotion_author_committed",
        "formal_promotion_rejected",
        "formal_promotion_rolled_back",
    ]
    regression_result: Literal[
        "not_run",
        "pass_with_watch",
        "fail_blocked",
        "skipped_rejected",
    ]
    staged_package_id: FormalKnowledgePromotionPackageId | None = None
    materialized_source_import_id: str | None = Field(default=None, max_length=160)
    materialized_knowledge_candidate_id: str | None = Field(default=None, max_length=160)
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_formal_knowledge: bool = False
    committed_knowledge_card_id: str | None = Field(default=None, max_length=160)
    committed_knowledge_version_id: str | None = Field(default=None, max_length=160)


class FormalKnowledgePromotionPackage(FormalKnowledgePromotionModel):
    schema_id: Literal["zhimeng.formal-knowledge-promotion-package.v1"] = Field(
        default="zhimeng.formal-knowledge-promotion-package.v1",
        alias="schema",
    )
    package_id: FormalKnowledgePromotionPackageId
    change_set_id: ChangeSetId
    project_id: ProjectId
    candidate_id: FeedbackCandidateId
    finding_id: NonEmptyText = Field(max_length=128)
    finding_kind: Literal["fact_risk", "continuity_risk", "subjective_note"]
    summary: NonEmptyText = Field(max_length=500)
    evidence_quote: str = Field(default="", max_length=1000)
    relative_path: str | None = Field(default=None, max_length=512)
    proposed_card_type: Literal["validator"] = "validator"
    proposed_card_title: NonEmptyText = Field(max_length=256)
    proposed_card_body: NonEmptyText = Field(max_length=4000)
    source_kind: Literal["feedback_experience"] = "feedback_experience"
    experience_relative_path: NonEmptyText = Field(max_length=512)
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_formal_knowledge: Literal[False] = False
    quality_assessed: Literal[False] = False
    staged_at: datetime
    materialized_source_import_id: str | None = Field(default=None, max_length=160)
    materialized_knowledge_candidate_id: str | None = Field(default=None, max_length=160)
    package_hash: Sha256

    @model_validator(mode="after")
    def validate_package(self) -> "FormalKnowledgePromotionPackage":
        if self.formal_knowledge_contaminated is not False:
            raise ValueError("formal knowledge promotion package cannot contaminate formal knowledge")
        if self.formal_draft_allowed is not False:
            raise ValueError("formal knowledge promotion package cannot allow formal draft")
        if self.writes_formal_knowledge is not False:
            raise ValueError("formal knowledge promotion package cannot write formal knowledge")
        if self.quality_assessed is not False:
            raise ValueError("formal knowledge promotion package cannot claim quality assessed")
        if self.staged_at.tzinfo is None or self.staged_at.utcoffset() is None:
            raise ValueError("staged_at must be timezone-aware")
        expected = formal_knowledge_promotion_package_hash(self)
        if self.package_hash != expected:
            raise ValueError("formal knowledge promotion package hash mismatch")
        return self


class FormalKnowledgePromotionReceipt(FormalKnowledgePromotionModel):
    schema_id: Literal["zhimeng.formal-knowledge-promotion-receipt.v1"] = Field(
        default="zhimeng.formal-knowledge-promotion-receipt.v1",
        alias="schema",
    )
    receipt_id: FormalKnowledgePromotionReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    feedback_candidate_bundle_id: str | None = Field(default=None, max_length=160)
    feedback_candidate_bundle_hash: Sha256 | None = None
    status: FormalKnowledgePromotionStatus
    authorization_input_ready: bool
    author_formalization_intent: Literal["none", "request", "withhold"] = "none"
    author_formalization_effective: bool = False
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_formal_knowledge: bool = False
    quality_assessed: Literal[False] = False
    checks: list[FormalKnowledgePromotionCheck] = Field(min_length=1, max_length=64)
    missing_required: list[FormalKnowledgePromotionCheckCode] = Field(
        default_factory=list,
        max_length=32,
    )
    items: list[FormalKnowledgePromotionItem] = Field(default_factory=list, max_length=64)
    packages: list[FormalKnowledgePromotionPackage] = Field(
        default_factory=list,
        max_length=64,
    )
    queue_relative_path: str | None = Field(default=None, max_length=512)
    queue_rows_appended: int = Field(default=0, ge=0)
    policy_version: Literal["local-v1-formal-knowledge-promotion-1"] = (
        "local-v1-formal-knowledge-promotion-1"
    )
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FormalKnowledgePromotionReceipt":
        if self.formal_knowledge_contaminated is not False:
            raise ValueError("formal knowledge promotion cannot contaminate formal knowledge")
        if self.formal_draft_allowed is not False:
            raise ValueError("formal knowledge promotion cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("formal knowledge promotion cannot claim quality assessed")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        committed = (
            self.status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
        )
        if committed:
            if self.writes_formal_knowledge is not True:
                raise ValueError(
                    "author-committed formal knowledge promotion must write formal knowledge"
                )
            if self.author_formalization_effective is not True:
                raise ValueError(
                    "author-committed formal knowledge promotion must mark author formalization effective"
                )
            if self.author_formalization_intent != "request":
                raise ValueError(
                    "author-committed formal knowledge promotion requires request intent"
                )
            if not any(
                item.promotion_status == "formal_promotion_author_committed"
                and item.writes_formal_knowledge is True
                for item in self.items
            ):
                raise ValueError(
                    "author-committed formal knowledge promotion requires a committed item write"
                )
        else:
            if self.writes_formal_knowledge is not False:
                raise ValueError(
                    "formal knowledge promotion cannot write formal knowledge before author commit"
                )
            if self.author_formalization_effective is not False:
                raise ValueError(
                    "author formalization cannot become effective before author commit"
                )
        required_missing = [
            item.code for item in self.checks if item.required and not item.present
        ]
        if list(self.missing_required) != required_missing:
            raise ValueError("missing_required must match required absent checks")
        expected_ready = (
            self.status in {
                FormalKnowledgePromotionStatus.READY,
                FormalKnowledgePromotionStatus.STAGED,
                FormalKnowledgePromotionStatus.MATERIALIZED,
                FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED,
                FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD,
                FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED,
            }
            and not required_missing
        )
        if self.authorization_input_ready is not expected_ready:
            raise ValueError(
                "authorization_input_ready must match ready/staged formal knowledge promotion status"
            )
        if self.status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED:
            if self.author_formalization_intent != "request":
                raise ValueError("author formalization requested status requires request intent")
        if self.status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD:
            if self.author_formalization_intent != "withhold":
                raise ValueError("author formalization withheld status requires withhold intent")
        expected = formal_knowledge_promotion_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("formal knowledge promotion receipt hash mismatch")
        return self


class FormalKnowledgePromotionResponse(FormalKnowledgePromotionModel):
    schema_id: Literal["zhimeng.formal-knowledge-promotion-response.v1"] = Field(
        default="zhimeng.formal-knowledge-promotion-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: FormalKnowledgePromotionReceipt | None = None
    history: list[FormalKnowledgePromotionReceipt] = Field(
        default_factory=list,
        max_length=32,
    )
    not_claiming: tuple[str, ...] = (
        "formal_knowledge_auto_activation",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def formal_knowledge_promotion_package_hash(
    package: FormalKnowledgePromotionPackage,
) -> str:
    material = {
        "schema": "zhimeng.formal-knowledge-promotion-package-hash.v1",
        "package_id": package.package_id,
        "change_set_id": package.change_set_id,
        "project_id": package.project_id,
        "candidate_id": package.candidate_id,
        "finding_id": package.finding_id,
        "finding_kind": package.finding_kind,
        "summary": package.summary,
        "evidence_quote": package.evidence_quote,
        "relative_path": package.relative_path,
        "proposed_card_type": package.proposed_card_type,
        "proposed_card_title": package.proposed_card_title,
        "proposed_card_body": package.proposed_card_body,
        "source_kind": package.source_kind,
        "experience_relative_path": package.experience_relative_path,
        "formal_knowledge_contaminated": False,
        "formal_draft_allowed": False,
        "writes_formal_knowledge": False,
        "quality_assessed": False,
        "materialized_source_import_id": package.materialized_source_import_id,
        "materialized_knowledge_candidate_id": package.materialized_knowledge_candidate_id,
        "staged_at": package.staged_at.isoformat(),
    }
    return canonical_json_hash(material)


def formal_knowledge_promotion_receipt_hash(
    receipt: FormalKnowledgePromotionReceipt,
) -> str:
    material = {
        "schema": "zhimeng.formal-knowledge-promotion-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "feedback_candidate_bundle_id": receipt.feedback_candidate_bundle_id,
        "feedback_candidate_bundle_hash": receipt.feedback_candidate_bundle_hash,
        "status": (
            receipt.status.value if hasattr(receipt.status, "value") else receipt.status
        ),
        "authorization_input_ready": receipt.authorization_input_ready,
        "author_formalization_intent": receipt.author_formalization_intent,
        "author_formalization_effective": bool(receipt.author_formalization_effective),
        "formal_knowledge_contaminated": False,
        "formal_draft_allowed": False,
        "writes_formal_knowledge": bool(receipt.writes_formal_knowledge),
        "quality_assessed": False,
        "checks": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "required": item.required,
                "present": item.present,
                "detail": item.detail,
                "candidate_id": item.candidate_id,
            }
            for item in receipt.checks
        ],
        "missing_required": [
            item.value if hasattr(item, "value") else item
            for item in receipt.missing_required
        ],
        "items": [
            {
                "candidate_id": item.candidate_id,
                "finding_id": item.finding_id,
                "finding_kind": item.finding_kind,
                "summary": item.summary,
                "evidence_quote": item.evidence_quote,
                "relative_path": item.relative_path,
                "experience_relative_path": item.experience_relative_path,
                "promotion_status": item.promotion_status,
                "regression_result": item.regression_result,
                "staged_package_id": item.staged_package_id,
                "materialized_source_import_id": item.materialized_source_import_id,
                "materialized_knowledge_candidate_id": item.materialized_knowledge_candidate_id,
                "formal_knowledge_contaminated": False,
                "formal_draft_allowed": False,
                "writes_formal_knowledge": bool(item.writes_formal_knowledge),
                "committed_knowledge_card_id": item.committed_knowledge_card_id,
                "committed_knowledge_version_id": item.committed_knowledge_version_id,
            }
            for item in receipt.items
        ],
        "packages": [
            {
                "package_id": item.package_id,
                "package_hash": item.package_hash,
                "candidate_id": item.candidate_id,
            }
            for item in receipt.packages
        ],
        "queue_relative_path": receipt.queue_relative_path,
        "queue_rows_appended": receipt.queue_rows_appended,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FormalKnowledgePromotionAction",
    "FormalKnowledgePromotionActionRequest",
    "FormalKnowledgePromotionCheck",
    "FormalKnowledgePromotionCheckCode",
    "FormalKnowledgePromotionDecision",
    "FormalKnowledgePromotionEvaluateRequest",
    "FormalKnowledgePromotionItem",
    "FormalKnowledgePromotionPackage",
    "FormalKnowledgePromotionReceipt",
    "FormalKnowledgePromotionResponse",
    "FormalKnowledgePromotionStatus",
    "formal_knowledge_promotion_package_hash",
    "formal_knowledge_promotion_receipt_hash",
]
