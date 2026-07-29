from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.knowledge_scope_models import KnowledgeScopeRecord, KnowledgeScopeRef
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id
from zhimeng_runtime.provenance_models import knowledge_formalization_receipt_id


Sha256 = str


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class KnowledgeCandidateStatus(StrEnum):
    PENDING = "pending"
    PROMOTED = "promoted"
    DEFERRED = "deferred"
    REJECTED = "rejected"


class KnowledgeCardStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class KnowledgeLineageStatus(StrEnum):
    VERIFIED = "verified"
    LEGACY_UNVERIFIED = "legacy_unverified"


class KnowledgeVersionFormalizationStatus(StrEnum):
    VERIFIED = "verified"
    AWAITING_FORMALIZATION = "awaiting_formalization"
    ABANDONED = "abandoned"
    LEGACY_UNVERIFIED = "legacy_unverified"


class KnowledgeFormalizationStatus(StrEnum):
    VERIFIED = "verified"
    AWAITING_REVIEW = "awaiting_review"
    LEGACY_UNVERIFIED = "legacy_unverified"


class KnowledgeCandidateOrigin(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-candidate-origin.v1"] = Field(
        default="zhimeng.knowledge-candidate-origin.v1",
        alias="schema",
    )
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_packet_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    draft_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cards_draft_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_card_index: int = Field(ge=0)
    source_card_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    origin_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_origin_hash(self) -> "KnowledgeCandidateOrigin":
        if self.origin_hash != knowledge_candidate_origin_hash(self):
            raise ValueError("knowledge candidate origin hash does not match immutable material")
        return self


class KnowledgeReviewIssue(KnowledgeModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    field: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=2000)


class KnowledgeCandidate(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-candidate.v2"] = Field(
        default="zhimeng.knowledge-candidate.v2",
        alias="schema",
    )
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    source_card_index: int = Field(ge=0)
    source_draft_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    lineage_status: KnowledgeLineageStatus = KnowledgeLineageStatus.LEGACY_UNVERIFIED
    origin: KnowledgeCandidateOrigin | None = None
    runtime_distillation_invocation_id: str | None = Field(
        default=None,
        pattern=r"^distillation-invocation-[0-9a-f]{32}$",
    )
    card_id: str = Field(min_length=1, max_length=160)
    card_type: str = Field(min_length=1, max_length=64)
    status: KnowledgeCandidateStatus
    revision: int = Field(ge=1)
    content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    card: dict[str, Any]
    issues: list[KnowledgeReviewIssue] = Field(max_length=128)
    promoted_version_id: str | None = Field(default=None, max_length=128)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "KnowledgeCandidate":
        if self.created_at > self.updated_at:
            raise ValueError("knowledge candidate updated_at cannot precede created_at")
        if self.status is KnowledgeCandidateStatus.PROMOTED and not self.promoted_version_id:
            raise ValueError("promoted knowledge candidate requires a version id")
        if self.status is not KnowledgeCandidateStatus.PROMOTED and self.promoted_version_id:
            raise ValueError("only promoted knowledge candidates can reference a version")
        if self.lineage_status is KnowledgeLineageStatus.VERIFIED:
            if self.origin is None:
                raise ValueError("verified knowledge candidates require immutable origin")
            if self.origin.source_card_index != self.source_card_index:
                raise ValueError("knowledge candidate origin index does not match the candidate")
            if self.origin.cards_draft_hash != self.source_draft_hash:
                raise ValueError("knowledge candidate origin draft hash does not match the candidate")
            if self.origin.source_receipt_id != source_import_receipt_id(
                self.source_import_id
            ):
                raise ValueError("knowledge candidate origin belongs to another source import")
        elif self.origin is not None:
            raise ValueError("legacy-unverified knowledge candidates cannot claim verified origin")
        if self.content_hash != canonical_json_hash(self.card):
            raise ValueError("knowledge candidate content hash does not match its card")
        if (
            str(self.card.get("card_id") or "").strip() != self.card_id
            or str(self.card.get("card_type") or "").strip().lower() != self.card_type
        ):
            raise ValueError("knowledge candidate identity does not match its card")
        return self


class KnowledgeCandidateList(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-candidate-list.v2"] = Field(
        default="zhimeng.knowledge-candidate-list.v2",
        alias="schema",
    )
    candidates: list[KnowledgeCandidate]
    candidate_set_hash: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class KnowledgeCandidateUpdateRequest(KnowledgeModel):
    expected_revision: int = Field(ge=1)
    expected_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    card: dict[str, Any]


class KnowledgePromotionRequest(KnowledgeModel):
    expected_revision: int = Field(ge=1)
    expected_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    replace_active: bool = False


class KnowledgeCandidateDecisionRequest(KnowledgeModel):
    expected_revision: int = Field(ge=1)
    expected_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeVersionLineageReceipt(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-version-lineage-receipt.v1"] = Field(
        default="zhimeng.knowledge-version-lineage-receipt.v1",
        alias="schema",
    )
    lineage_receipt_id: str = Field(pattern=r"^knowledge-lineage-[0-9a-f]{32}$")
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    card_id: str = Field(min_length=1, max_length=160)
    version_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    approved_candidate_revision: int = Field(ge=1)
    approved_candidate_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_origin: KnowledgeCandidateOrigin
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    activation_mode: Literal["initial", "replace"]
    replacement_authorized: bool
    activation_expected_version_id: str = Field(
        pattern=r"^knowledge-version-[0-9a-f]{32}$"
    )
    activation_expected_state_revision: int = Field(ge=1)
    created_at: datetime
    receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "KnowledgeVersionLineageReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("knowledge lineage receipt time must be timezone-aware")
        if self.receipt_hash != knowledge_version_lineage_receipt_hash(self):
            raise ValueError("knowledge lineage receipt hash does not match immutable material")
        if self.approved_candidate_content_hash != self.version_content_hash:
            raise ValueError("approved candidate hash must match the immutable version")
        if self.candidate_origin.source_receipt_id != source_import_receipt_id(
            self.source_import_id
        ):
            raise ValueError("knowledge lineage receipt belongs to another source import")
        if self.activation_mode == "initial":
            if self.activation_expected_version_id != self.version_id:
                raise ValueError("initial knowledge activation must target its own version")
            if self.replacement_authorized:
                raise ValueError("initial knowledge activation is not a replacement")
        elif self.activation_expected_version_id == self.version_id:
            raise ValueError("replacement activation must bind the prior active version")
        elif not self.replacement_authorized:
            raise ValueError("replacement activation requires explicit authorization")
        return self


class KnowledgeVersionLineageReceiptV2(KnowledgeVersionLineageReceipt):
    schema_id: Literal["zhimeng.knowledge-version-lineage-receipt.v2"] = Field(
        default="zhimeng.knowledge-version-lineage-receipt.v2",
        alias="schema",
    )
    scope: KnowledgeScopeRef


class KnowledgeFormalizationRef(KnowledgeModel):
    formalization_receipt_id: str = Field(
        pattern=r"^knowledge-formalization-[0-9a-f]{32}$"
    )
    formalization_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeCardVersion(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-card-version.v2"] = Field(
        default="zhimeng.knowledge-card-version.v2",
        alias="schema",
    )
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    card_id: str = Field(min_length=1, max_length=160)
    version_number: int = Field(ge=1)
    content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    card: dict[str, Any]
    source_candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    lineage_status: KnowledgeLineageStatus = KnowledgeLineageStatus.LEGACY_UNVERIFIED
    lineage: KnowledgeVersionLineageReceipt | KnowledgeVersionLineageReceiptV2 | None = None
    formalization_status: KnowledgeVersionFormalizationStatus = (
        KnowledgeVersionFormalizationStatus.LEGACY_UNVERIFIED
    )
    formalization: KnowledgeFormalizationRef | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_lineage(self) -> "KnowledgeCardVersion":
        if self.content_hash != canonical_json_hash(self.card):
            raise ValueError("knowledge version content hash does not match its card")
        if str(self.card.get("card_id") or "").strip() != self.card_id:
            raise ValueError("knowledge version card identity does not match its payload")
        if self.lineage_status is KnowledgeLineageStatus.VERIFIED:
            if self.lineage is None:
                raise ValueError("verified knowledge versions require a lineage receipt")
            if (
                self.lineage.version_id != self.version_id
                or self.lineage.card_id != self.card_id
                or self.lineage.version_content_hash != self.content_hash
                or self.lineage.candidate_id != self.source_candidate_id
            ):
                raise ValueError("knowledge version lineage does not match its version")
        elif self.lineage is not None:
            raise ValueError("legacy-unverified versions cannot claim a lineage receipt")
        if self.formalization_status is KnowledgeVersionFormalizationStatus.VERIFIED:
            if self.lineage is None or self.formalization is None:
                raise ValueError("formalized versions require lineage and formalization receipts")
        elif self.formalization_status is KnowledgeVersionFormalizationStatus.AWAITING_FORMALIZATION:
            if self.lineage is None or self.formalization is not None:
                raise ValueError("awaiting-formalization versions require lineage only")
        elif self.formalization_status is KnowledgeVersionFormalizationStatus.ABANDONED:
            if self.lineage is None:
                raise ValueError("abandoned versions require their immutable lineage receipt")
        elif self.lineage is not None or self.formalization is not None:
            raise ValueError("legacy versions cannot claim formalization provenance")
        return self


class KnowledgeFormalizationDecision(KnowledgeModel):
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    source_card_index: int = Field(ge=0)
    origin_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["promoted", "rejected", "deferred"]
    revision: int = Field(ge=1)
    content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    promoted_version_id: str | None = Field(
        default=None,
        pattern=r"^knowledge-version-[0-9a-f]{32}$",
    )
    version_lineage_receipt_id: str | None = Field(
        default=None,
        pattern=r"^knowledge-lineage-[0-9a-f]{32}$",
    )
    version_lineage_receipt_hash: Sha256 | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "KnowledgeFormalizationDecision":
        promoted = self.decision == "promoted"
        fields_present = (
            self.promoted_version_id is not None
            and self.version_lineage_receipt_id is not None
            and self.version_lineage_receipt_hash is not None
        )
        fields_absent = (
            self.promoted_version_id is None
            and self.version_lineage_receipt_id is None
            and self.version_lineage_receipt_hash is None
        )
        if promoted and not fields_present:
            raise ValueError("promoted formalization decisions require version lineage")
        if not promoted and not fields_absent:
            raise ValueError("non-promoted decisions cannot reference a version")
        return self


class KnowledgeFormalizationRequest(KnowledgeModel):
    expected_source_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    expected_draft_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    expected_candidate_set_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeFormalizationReceipt(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-formalization-receipt.v1"] = Field(
        default="zhimeng.knowledge-formalization-receipt.v1",
        alias="schema",
    )
    formalization_receipt_id: str = Field(
        pattern=r"^knowledge-formalization-[0-9a-f]{32}$"
    )
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_packet_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    draft_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cards_draft_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_set_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: tuple[KnowledgeFormalizationDecision, ...] = Field(
        min_length=1,
        max_length=64,
    )
    knowledge_state_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_decisions(self) -> "KnowledgeFormalizationReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("knowledge formalization time must be timezone-aware")
        candidate_ids = tuple(item.candidate_id for item in self.decisions)
        indexes = tuple(item.source_card_index for item in self.decisions)
        if candidate_ids != tuple(sorted(candidate_ids)):
            raise ValueError("formalization decisions must be sorted by candidate ID")
        if len(candidate_ids) != len(set(candidate_ids)) or len(indexes) != len(set(indexes)):
            raise ValueError("formalization decisions must be unique")
        if self.receipt_hash != knowledge_formalization_receipt_hash(self):
            raise ValueError("knowledge formalization receipt hash does not match immutable material")
        if self.source_receipt_id != source_import_receipt_id(self.source_import_id):
            raise ValueError("knowledge formalization belongs to another source import")
        if self.formalization_receipt_id != knowledge_formalization_receipt_id(
            self.source_import_id
        ):
            raise ValueError("knowledge formalization identity does not match its source import")
        return self


class KnowledgeFormalizationReceiptV2(KnowledgeFormalizationReceipt):
    schema_id: Literal["zhimeng.knowledge-formalization-receipt.v2"] = Field(
        default="zhimeng.knowledge-formalization-receipt.v2",
        alias="schema",
    )
    scope: KnowledgeScopeRef


class KnowledgeFormalizationRecord(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-formalization-record.v1"] = Field(
        default="zhimeng.knowledge-formalization-record.v1",
        alias="schema",
    )
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    status: KnowledgeFormalizationStatus
    receipt: KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2 | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "KnowledgeFormalizationRecord":
        if (self.status is KnowledgeFormalizationStatus.VERIFIED) != (
            self.receipt is not None
        ):
            raise ValueError("formalization status must match receipt availability")
        return self


class KnowledgeCard(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-card.v2"] = Field(
        default="zhimeng.knowledge-card.v2",
        alias="schema",
    )
    card_id: str = Field(min_length=1, max_length=160)
    status: KnowledgeCardStatus
    state_revision: int = Field(ge=1)
    active_version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    active_version: KnowledgeCardVersion
    versions: list[KnowledgeCardVersion] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_active_version(self) -> "KnowledgeCard":
        if self.created_at > self.updated_at:
            raise ValueError("knowledge card updated_at cannot precede created_at")
        matches = [item for item in self.versions if item.version_id == self.active_version_id]
        if len(matches) != 1 or matches[0] != self.active_version:
            raise ValueError("knowledge card active version must be present exactly once")
        return self


class KnowledgeCardList(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-card-list.v2"] = Field(
        default="zhimeng.knowledge-card-list.v2",
        alias="schema",
    )
    cards: list[KnowledgeCard]
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgeScopedCardList(KnowledgeModel):
    schema_id: Literal["zhimeng.knowledge-scoped-card-list.v1"] = Field(
        default="zhimeng.knowledge-scoped-card-list.v1",
        alias="schema",
    )
    scope: KnowledgeScopeRecord
    includes_application_scope: bool
    cards: list[KnowledgeCard]
    inherited_card_ids: list[
        Annotated[str, Field(min_length=1, max_length=160)]
    ] = Field(max_length=10000)
    fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_inherited_cards(self) -> "KnowledgeScopedCardList":
        inherited = self.inherited_card_ids
        if len(inherited) != len(set(inherited)):
            raise ValueError("inherited knowledge card ids must be unique")
        if inherited != sorted(inherited):
            raise ValueError("inherited knowledge card ids must be sorted")
        if not set(inherited).issubset({card.card_id for card in self.cards}):
            raise ValueError("inherited knowledge card ids must reference visible cards")
        if inherited and not self.includes_application_scope:
            raise ValueError("inherited knowledge cards require application scope inclusion")
        return self


class KnowledgeCardStateRequest(KnowledgeModel):
    expected_state_revision: int = Field(ge=1)
    expected_active_version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")


class KnowledgeRollbackRequest(KnowledgeCardStateRequest):
    target_version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")


def knowledge_candidate_origin_hash(origin: KnowledgeCandidateOrigin) -> str:
    return canonical_json_hash(
        origin.model_dump(mode="json", by_alias=True, exclude={"origin_hash"})
    )


def knowledge_candidate_set_hash(candidates: list[KnowledgeCandidate]) -> str:
    material = [
        {
            "candidate_id": item.candidate_id,
            "source_card_index": item.source_card_index,
            "lineage_status": item.lineage_status.value,
            "origin_hash": item.origin.origin_hash if item.origin is not None else None,
            "status": item.status.value,
            "revision": item.revision,
            "content_hash": item.content_hash,
            "promoted_version_id": item.promoted_version_id,
        }
        for item in sorted(candidates, key=lambda candidate: candidate.candidate_id)
    ]
    return canonical_json_hash(
        {"schema": "zhimeng.knowledge-candidate-set.v1", "candidates": material}
    )


def native_knowledge_state_hash(cards: list[KnowledgeCard]) -> str:
    material = [
        {
            "card_id": item.card_id,
            "status": item.status.value,
            "active_version_id": item.active_version_id,
            "content_hash": item.active_version.content_hash,
            "lineage_status": item.active_version.lineage_status.value,
            "lineage_receipt_id": (
                item.active_version.lineage.lineage_receipt_id
                if item.active_version.lineage is not None
                else None
            ),
            "lineage_receipt_hash": (
                item.active_version.lineage.receipt_hash
                if item.active_version.lineage is not None
                else None
            ),
        }
        for item in sorted(cards, key=lambda value: value.card_id)
        if item.status is KnowledgeCardStatus.ACTIVE
    ]
    return canonical_json_hash(
        {"schema": "zhimeng.native-knowledge-state.v1", "cards": material}
    )


def native_knowledge_fingerprint(cards: list[KnowledgeCard]) -> str:
    material = [
        {
            "card_id": item.card_id,
            "status": item.status.value,
            "active_version_id": item.active_version_id,
            "content_hash": item.active_version.content_hash,
            "lineage_status": item.active_version.lineage_status.value,
            "lineage_receipt_hash": (
                item.active_version.lineage.receipt_hash
                if item.active_version.lineage is not None
                else None
            ),
            "formalization_status": item.active_version.formalization_status.value,
            "formalization_receipt_id": (
                item.active_version.formalization.formalization_receipt_id
                if item.active_version.formalization is not None
                else None
            ),
            "formalization_receipt_hash": (
                item.active_version.formalization.formalization_receipt_hash
                if item.active_version.formalization is not None
                else None
            ),
        }
        for item in sorted(cards, key=lambda value: value.card_id)
        if item.status is KnowledgeCardStatus.ACTIVE
        and item.active_version.formalization_status
        is KnowledgeVersionFormalizationStatus.VERIFIED
    ]
    return canonical_json_hash(
        {"schema": "zhimeng.native-knowledge.v2", "cards": material}
    )


def knowledge_version_lineage_receipt_hash(
    receipt: KnowledgeVersionLineageReceipt | KnowledgeVersionLineageReceiptV2,
) -> str:
    return canonical_json_hash(
        receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    )


def knowledge_formalization_receipt_hash(
    receipt: KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2,
) -> str:
    return canonical_json_hash(
        receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    )


__all__ = [
    "KnowledgeCandidate",
    "KnowledgeCandidateDecisionRequest",
    "KnowledgeCandidateList",
    "KnowledgeCandidateOrigin",
    "KnowledgeCandidateStatus",
    "KnowledgeCandidateUpdateRequest",
    "KnowledgeCard",
    "KnowledgeCardList",
    "KnowledgeScopedCardList",
    "KnowledgeCardStateRequest",
    "KnowledgeCardStatus",
    "KnowledgeCardVersion",
    "KnowledgeFormalizationDecision",
    "KnowledgeFormalizationRef",
    "KnowledgeFormalizationReceipt",
    "KnowledgeFormalizationReceiptV2",
    "KnowledgeFormalizationRecord",
    "KnowledgeFormalizationRequest",
    "KnowledgeFormalizationStatus",
    "KnowledgeLineageStatus",
    "KnowledgePromotionRequest",
    "KnowledgeReviewIssue",
    "KnowledgeRollbackRequest",
    "KnowledgeVersionLineageReceipt",
    "KnowledgeVersionLineageReceiptV2",
    "KnowledgeVersionFormalizationStatus",
    "knowledge_candidate_origin_hash",
    "knowledge_candidate_set_hash",
    "knowledge_formalization_receipt_hash",
    "knowledge_formalization_receipt_id",
    "knowledge_version_lineage_receipt_hash",
    "native_knowledge_fingerprint",
    "native_knowledge_state_hash",
]
