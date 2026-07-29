from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextPack,
    Sha256,
    canonical_json_hash,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef


class NlclModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class NlclRouteSource(StrEnum):
    NATIVE_KNOWLEDGE = "native_knowledge"
    EXTERNAL_DISTILLATION = "external_distillation"
    UNAVAILABLE = "unavailable"


class NlclRouteStatus(StrEnum):
    READY = "ready"
    RISK_READY = "risk_ready"
    UNAVAILABLE = "unavailable"


class NlclSelectedCard(NlclModel):
    card_id: str = Field(min_length=1, max_length=300, strict=True)
    card_type: str = Field(min_length=1, max_length=128, strict=True)
    title: str = Field(min_length=1, max_length=500, strict=True)
    version_id: str = Field(min_length=1, max_length=256, strict=True)
    content_hash: Sha256
    route_terms: tuple[str, ...] = Field(default=(), max_length=32)
    recommended_profiles: tuple[str, ...] = Field(default=(), max_length=16)
    negative_routes: tuple[str, ...] = Field(default=(), max_length=32)
    safety_summary: str | None = Field(default=None, min_length=1, max_length=2000)
    feedback_layer: str | None = Field(default=None, min_length=1, max_length=128)
    knowledge_provenance: KnowledgeProvenanceRef | None = None

    @field_validator(
        "card_id",
        "card_type",
        "title",
        "version_id",
        "safety_summary",
        "feedback_layer",
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("NLCL selected-card text must not be blank")
        return normalized

    @field_validator("route_terms", "recommended_profiles", "negative_routes")
    @classmethod
    def validate_compact_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(value.split()) for value in values)
        if any(not value or len(value) > 160 for value in normalized):
            raise ValueError("NLCL route hints must be non-empty and at most 160 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("NLCL route hints must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_provenance(self) -> "NlclSelectedCard":
        if self.knowledge_provenance is not None and (
            self.knowledge_provenance.card_id != self.card_id
            or self.knowledge_provenance.version_id != self.version_id
            or self.knowledge_provenance.version_content_hash != self.content_hash
        ):
            raise ValueError("NLCL knowledge provenance does not match selected card")
        return self


class NlclChapterCall(NlclModel):
    ordinal: int = Field(ge=1, le=64, strict=True)
    skill_id: str = Field(min_length=3, max_length=128, strict=True)
    purpose: str = Field(min_length=1, max_length=1000, strict=True)
    execution_status: Literal["requires_runtime_catalog_check"] = (
        "requires_runtime_catalog_check"
    )
    input_contract: str = Field(min_length=3, max_length=256, strict=True)
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    selected_card_ids: tuple[str, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def validate_call(self) -> NlclChapterCall:
        if len(self.selected_card_ids) != len(set(self.selected_card_ids)):
            raise ValueError("NLCL chapter call card IDs must be unique")
        return self


class NlclRouteReceipt(NlclModel):
    schema_id: Literal["zhimeng.nlcl-route-receipt.v1"] = Field(
        default="zhimeng.nlcl-route-receipt.v1",
        alias="schema",
    )
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    context_pack_hash: Sha256
    project_id: str = Field(min_length=1, max_length=128, strict=True)
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
        strict=True,
    )
    project_kb_manifest_hash: Sha256 | None = None
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    profile: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
        strict=True,
    )
    objective_hash: Sha256
    knowledge_fingerprint: Sha256 | None = None
    source: NlclRouteSource
    status: NlclRouteStatus
    selected_cards: tuple[NlclSelectedCard, ...] = Field(default=(), max_length=24)
    chapter_call_list: tuple[NlclChapterCall, ...] = Field(min_length=1, max_length=16)
    whole_library_load_forbidden: Literal[True] = True
    project_truth_precedence: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    allowed_output: Literal["context_and_creation_audit_only"] = (
        "context_and_creation_audit_only"
    )
    warnings: tuple[str, ...] = Field(default=(), max_length=64)
    strict_failures: tuple[str, ...] = Field(default=(), max_length=64)
    created_at: datetime
    receipt_hash: Sha256

    @field_validator("warnings", "strict_failures")
    @classmethod
    def validate_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value
            or len(value) > 128
            or not value.replace("_", "").isalnum()
            or value[0].isdigit()
            for value in values
        ):
            raise ValueError("NLCL warning and failure codes must use snake_case identifiers")
        if len(values) != len(set(values)):
            raise ValueError("NLCL warning and failure codes must be unique")
        return values

    @model_validator(mode="after")
    def validate_receipt(self) -> NlclRouteReceipt:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("NLCL route receipt created_at must be timezone-aware")
        if (self.project_kb_state_id is None) != (
            self.project_kb_manifest_hash is None
        ):
            raise ValueError("NLCL project KB identity and manifest hash must be paired")
        card_ids = tuple(card.card_id for card in self.selected_cards)
        if card_ids != tuple(sorted(card_ids)) or len(card_ids) != len(set(card_ids)):
            raise ValueError("NLCL selected cards must be unique and sorted by card_id")
        if self.selected_cards and self.knowledge_fingerprint is None:
            raise ValueError("NLCL selected cards require a knowledge fingerprint")
        if self.source is NlclRouteSource.UNAVAILABLE:
            if self.status is not NlclRouteStatus.UNAVAILABLE or self.selected_cards:
                raise ValueError("unavailable NLCL routes cannot select cards or claim readiness")
        elif self.status is NlclRouteStatus.UNAVAILABLE:
            raise ValueError("available NLCL sources cannot use unavailable route status")
        if self.source is NlclRouteSource.EXTERNAL_DISTILLATION and any(
            card.knowledge_provenance is not None for card in self.selected_cards
        ):
            raise ValueError("external NLCL cards cannot claim native provenance")
        if self.status is NlclRouteStatus.READY:
            if not self.selected_cards or self.strict_failures:
                raise ValueError("ready NLCL routes require cards and no strict failures")
        ordinals = tuple(call.ordinal for call in self.chapter_call_list)
        if ordinals != tuple(range(1, len(ordinals) + 1)):
            raise ValueError("NLCL chapter calls must use contiguous ordinals")
        selected = set(card_ids)
        for call in self.chapter_call_list:
            if call.context_pack_id != self.context_pack_id:
                raise ValueError("NLCL chapter calls must bind the same Context Pack")
            if not set(call.selected_card_ids).issubset(selected):
                raise ValueError("NLCL chapter calls can reference only selected cards")
        if self.receipt_hash != nlcl_route_receipt_hash(self):
            raise ValueError("NLCL route receipt hash does not match immutable material")
        return self


def nlcl_route_receipt_hash(receipt: NlclRouteReceipt) -> str:
    payload = receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    for card in payload.get("selected_cards", []):
        if card.get("knowledge_provenance") is None:
            card.pop("knowledge_provenance", None)
    if (
        payload.get("project_kb_state_id") is None
        and payload.get("project_kb_manifest_hash") is None
    ):
        payload.pop("project_kb_state_id", None)
        payload.pop("project_kb_manifest_hash", None)
    return canonical_json_hash(payload)


def build_nlcl_route_receipt(
    pack: ContextPack,
    *,
    source: NlclRouteSource,
    route_cards: tuple[NlclSelectedCard, ...] = (),
    extra_warnings: tuple[str, ...] = (),
) -> NlclRouteReceipt:
    included_formal_ids = {
        item.source_id.removeprefix("knowledge-card:")
        for item in pack.included
        if item.authority is ContextAuthority.FORMAL_KNOWLEDGE
        and item.source_id.startswith("knowledge-card:")
    }
    selected_cards = tuple(
        sorted(
            (card for card in route_cards if card.card_id in included_formal_ids),
            key=lambda card: card.card_id,
        )
    )
    warnings = list(
        dict.fromkeys(
            (
                "creation_runtime_catalog_check_required",
                "formal_draft_not_authorized",
                *extra_warnings,
            )
        )
    )
    strict_failures: list[str] = []
    if source is NlclRouteSource.UNAVAILABLE:
        status = NlclRouteStatus.UNAVAILABLE
        strict_failures.append("knowledge_route_unavailable")
    elif not selected_cards or pack.readiness.value == "blocked":
        status = NlclRouteStatus.RISK_READY
        if not selected_cards:
            warnings.append("no_knowledge_cards_included")
        if pack.readiness.value == "blocked":
            strict_failures.append("context_pack_blocked")
    else:
        status = NlclRouteStatus.READY
    if source is NlclRouteSource.NATIVE_KNOWLEDGE and any(
        card.knowledge_provenance is None for card in selected_cards
    ):
        status = NlclRouteStatus.RISK_READY
        strict_failures.append("native_knowledge_lineage_unavailable")
    if pack.active_chapter_id is None:
        warnings.append("active_chapter_not_bound")

    receipt_id = "nlcl-route-" + hashlib.sha256(
        f"zhimeng.nlcl-route-receipt.v1\0{pack.context_pack_id}".encode("utf-8")
    ).hexdigest()[:32]
    card_ids = tuple(card.card_id for card in selected_cards)
    material = {
        "route_receipt_id": receipt_id,
        "context_pack_id": pack.context_pack_id,
        "context_pack_hash": pack.pack_hash,
        "project_id": pack.project_id,
        "project_revision": pack.project_revision,
        "project_fingerprint": pack.project_fingerprint,
        "project_kb_state_id": pack.project_kb_state_id,
        "project_kb_manifest_hash": pack.project_kb_manifest_hash,
        "active_chapter_id": pack.active_chapter_id,
        "profile": pack.profile,
        "objective_hash": canonical_json_hash({"objective": pack.objective}),
        "knowledge_fingerprint": pack.knowledge_fingerprint,
        "source": source,
        "status": status,
        "selected_cards": selected_cards,
        "chapter_call_list": (
            NlclChapterCall(
                ordinal=1,
                skill_id="novel-creation-suite.project-audit",
                purpose=(
                    "Audit project truth and Creation readiness before any formal prose path."
                ),
                input_contract="zhimeng.creation-project-audit-request.v1",
                context_pack_id=pack.context_pack_id,
                selected_card_ids=card_ids,
            ),
            NlclChapterCall(
                ordinal=2,
                skill_id="novel-creation-suite.prewrite-gate",
                purpose=(
                    "Evaluate the immutable operation package and prior project audit "
                    "before any formal prose path."
                ),
                input_contract="zhimeng.creation-prewrite-gate-request.v1",
                context_pack_id=pack.context_pack_id,
                selected_card_ids=card_ids,
            ),
            NlclChapterCall(
                ordinal=3,
                skill_id="novel-creation-suite.creation-handoff",
                purpose=(
                    "Materialize a Creation-consumable handoff from the verified "
                    "Context, Route, Prewrite package and prior execution receipts "
                    "without authorizing formal prose."
                ),
                input_contract="zhimeng.creation-handoff-request.v1",
                context_pack_id=pack.context_pack_id,
                selected_card_ids=card_ids,
            ),
        ),
        "whole_library_load_forbidden": True,
        "project_truth_precedence": True,
        "formal_draft_allowed": False,
        "allowed_output": "context_and_creation_audit_only",
        "warnings": tuple(dict.fromkeys(warnings)),
        "strict_failures": tuple(dict.fromkeys(strict_failures)),
        "created_at": pack.created_at,
    }
    draft = NlclRouteReceipt.model_construct(**material, receipt_hash="0" * 64)
    return NlclRouteReceipt(
        **material,
        receipt_hash=nlcl_route_receipt_hash(draft),
    )


__all__ = [
    "NlclChapterCall",
    "NlclRouteReceipt",
    "NlclRouteSource",
    "NlclRouteStatus",
    "NlclSelectedCard",
    "build_nlcl_route_receipt",
    "nlcl_route_receipt_hash",
]
