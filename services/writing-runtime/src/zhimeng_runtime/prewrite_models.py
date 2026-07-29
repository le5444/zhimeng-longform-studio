from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextIncludedFragment,
    ContextPack,
    Sha256,
    canonical_json_hash,
)
from zhimeng_runtime.nlcl_models import NlclRouteReceipt, NlclRouteStatus
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef


class PrewriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PrewriteEvidenceRef(PrewriteModel):
    source_id: str = Field(min_length=1, max_length=512, strict=True)
    source_type: str = Field(min_length=1, max_length=128, strict=True)
    title: str = Field(min_length=1, max_length=500, strict=True)
    authority: ContextAuthority
    version: str = Field(min_length=1, max_length=256, strict=True)
    source_hash: Sha256
    fragment_hash: Sha256
    content_excerpt: str = Field(min_length=1, max_length=4000, strict=True)
    knowledge_provenance: KnowledgeProvenanceRef | None = None


class PrewriteSceneContract(PrewriteModel):
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_goal: str = Field(min_length=1, max_length=100_000, strict=True)
    evidence_source_ids: tuple[str, ...] = Field(default=(), max_length=32)
    status: Literal["bound", "unbound"]


class PrewriteStyleOutputContract(PrewriteModel):
    selected_card_ids: tuple[str, ...] = Field(default=(), max_length=24)
    selected_card_hashes: tuple[Sha256, ...] = Field(default=(), max_length=24)
    selected_card_provenance: tuple[KnowledgeProvenanceRef, ...] = Field(
        default=(),
        max_length=24,
    )
    whole_library_load_forbidden: Literal[True] = True
    status: Literal["bound", "unbound"]

    @model_validator(mode="after")
    def validate_cards(self) -> PrewriteStyleOutputContract:
        if len(self.selected_card_ids) != len(self.selected_card_hashes):
            raise ValueError("prewrite style card IDs and hashes must align")
        if len(self.selected_card_ids) != len(set(self.selected_card_ids)):
            raise ValueError("prewrite style card IDs must be unique")
        if (self.status == "bound") != bool(self.selected_card_ids):
            raise ValueError("prewrite style status must match selected cards")
        if self.selected_card_provenance:
            if len(self.selected_card_provenance) != len(self.selected_card_ids):
                raise ValueError("prewrite style provenance must align with selected cards")
            for card_id, content_hash, provenance in zip(
                self.selected_card_ids,
                self.selected_card_hashes,
                self.selected_card_provenance,
                strict=True,
            ):
                if (
                    provenance.card_id != card_id
                    or provenance.version_content_hash != content_hash
                ):
                    raise ValueError("prewrite style provenance does not match selected cards")
        return self


class PrewriteAcceptanceGate(PrewriteModel):
    required_checks: tuple[str, ...] = Field(min_length=1, max_length=32)
    minimum_review_score: int = Field(default=80, ge=1, le=100, strict=True)
    author_approval_required: Literal[True] = True
    writeback_receipt_required: Literal[True] = True


class PrewriteFailureRoute(PrewriteModel):
    failure_signature: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
        strict=True,
    )
    rewrite_from: str = Field(min_length=2, max_length=128, strict=True)


class PrewriteLibraryBudget(PrewriteModel):
    max_characters: int = Field(ge=1, strict=True)
    used_characters: int = Field(ge=0, strict=True)
    max_tokens: int = Field(ge=1, strict=True)
    used_tokens: int = Field(ge=0, strict=True)
    activated_source_ids: tuple[str, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_budget(self) -> PrewriteLibraryBudget:
        if self.used_characters > self.max_characters:
            raise ValueError("prewrite character budget cannot overflow")
        if self.used_tokens > self.max_tokens:
            raise ValueError("prewrite token budget cannot overflow")
        if len(self.activated_source_ids) != len(set(self.activated_source_ids)):
            raise ValueError("prewrite activated sources must be unique")
        return self


class PrewriteOperationPackage(PrewriteModel):
    schema_id: Literal["zhimeng.prewrite-operation-package.v1"] = Field(
        default="zhimeng.prewrite-operation-package.v1",
        alias="schema",
    )
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    context_pack_hash: Sha256
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256
    project_id: str = Field(min_length=1, max_length=128, strict=True)
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_goal: str = Field(min_length=1, max_length=100_000, strict=True)
    must_obey_truth: tuple[PrewriteEvidenceRef, ...] = Field(default=(), max_length=256)
    current_character_state: tuple[PrewriteEvidenceRef, ...] = Field(
        default=(),
        max_length=256,
    )
    relevant_evidence_refs: tuple[PrewriteEvidenceRef, ...] = Field(
        default=(),
        max_length=256,
    )
    constraints_and_taboos: tuple[str, ...] = Field(min_length=1, max_length=64)
    scene_contract: PrewriteSceneContract
    style_output_contract: PrewriteStyleOutputContract
    acceptance_gate: PrewriteAcceptanceGate
    failure_routing: tuple[PrewriteFailureRoute, ...] = Field(min_length=1, max_length=32)
    library_activation_budget: PrewriteLibraryBudget
    whole_library_load_forbidden: Literal[True] = True
    readiness: Literal["blocked", "risk_draft_only"]
    formal_draft_allowed: Literal[False] = False
    missing_requirements: tuple[str, ...] = Field(default=(), max_length=64)
    warnings: tuple[str, ...] = Field(default=(), max_length=64)
    created_at: datetime
    package_hash: Sha256

    @field_validator("constraints_and_taboos", "missing_requirements", "warnings")
    @classmethod
    def validate_compact_codes_and_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 512 for value in values):
            raise ValueError("prewrite compact lists cannot contain blank or oversized values")
        if len(values) != len(set(values)):
            raise ValueError("prewrite compact lists must be unique")
        return values

    @model_validator(mode="after")
    def validate_package(self) -> PrewriteOperationPackage:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("prewrite package created_at must be timezone-aware")
        relevant_ids = tuple(item.source_id for item in self.relevant_evidence_refs)
        if relevant_ids != tuple(sorted(relevant_ids)) or len(relevant_ids) != len(
            set(relevant_ids)
        ):
            raise ValueError("prewrite evidence refs must be unique and sorted")
        truth_ids = {item.source_id for item in self.must_obey_truth}
        character_ids = {item.source_id for item in self.current_character_state}
        if not truth_ids.issubset(set(relevant_ids)) or not character_ids.issubset(
            truth_ids
        ):
            raise ValueError("prewrite truth and character refs must come from evidence refs")
        if self.active_chapter_id != self.scene_contract.active_chapter_id:
            raise ValueError("prewrite scene contract must bind the active chapter")
        if self.task_goal != self.scene_contract.task_goal:
            raise ValueError("prewrite scene contract must bind the task goal")
        if self.readiness == "blocked" and not self.missing_requirements:
            raise ValueError("blocked prewrite packages require explicit missing requirements")
        if self.package_hash != prewrite_operation_package_hash(self):
            raise ValueError("prewrite operation package hash does not match immutable material")
        return self


def prewrite_operation_package_hash(package: PrewriteOperationPackage) -> str:
    payload = package.model_dump(mode="json", by_alias=True, exclude={"package_hash"})
    for collection in (
        "must_obey_truth",
        "current_character_state",
        "relevant_evidence_refs",
    ):
        for fragment in payload.get(collection, []):
            if fragment.get("knowledge_provenance") is None:
                fragment.pop("knowledge_provenance", None)
    style = payload.get("style_output_contract", {})
    if not style.get("selected_card_provenance"):
        style.pop("selected_card_provenance", None)
    return canonical_json_hash(payload)


def _evidence_ref(fragment: ContextIncludedFragment) -> PrewriteEvidenceRef:
    excerpt = fragment.content.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:3997].rstrip() + "..."
    return PrewriteEvidenceRef(
        source_id=fragment.source_id,
        source_type=fragment.source_type,
        title=fragment.title,
        authority=fragment.authority,
        version=fragment.version,
        source_hash=fragment.source_hash,
        fragment_hash=fragment.fragment_hash,
        content_excerpt=excerpt,
        knowledge_provenance=fragment.knowledge_provenance,
    )


def _is_character_state(ref: PrewriteEvidenceRef) -> bool:
    material = f"{ref.title}\n{ref.source_type}".casefold()
    return any(
        marker in material
        for marker in ("03_", "人物", "character", "角色状态", "character_state")
    )


def _is_control_material(ref: PrewriteEvidenceRef) -> bool:
    material = f"{ref.title}\n{ref.source_type}".casefold()
    return any(
        marker in material
        for marker in ("控制卡", "章纲", "细纲", "chapter brief", "chapter_control")
    )


def build_prewrite_operation_package(
    pack: ContextPack,
    route: NlclRouteReceipt,
    *,
    extra_missing_requirements: tuple[str, ...] = (),
) -> PrewriteOperationPackage:
    if (
        route.context_pack_id != pack.context_pack_id
        or route.context_pack_hash != pack.pack_hash
        or route.project_id != pack.project_id
        or route.project_revision != pack.project_revision
        or route.project_fingerprint != pack.project_fingerprint
    ):
        raise ValueError("prewrite package inputs do not share Context/Route identity")

    evidence = tuple(
        sorted((_evidence_ref(item) for item in pack.included), key=lambda item: item.source_id)
    )
    truth = tuple(item for item in evidence if item.authority is ContextAuthority.PROJECT_TRUTH)
    character = tuple(item for item in truth if _is_character_state(item))
    scene_sources = tuple(
        item.source_id
        for item in truth
        if item.source_type.startswith("active_chapter")
    )
    control_bound = any(_is_control_material(item) for item in truth)
    card_ids = tuple(card.card_id for card in route.selected_cards)
    card_hashes = tuple(card.content_hash for card in route.selected_cards)
    card_provenance = tuple(
        card.knowledge_provenance
        for card in route.selected_cards
        if card.knowledge_provenance is not None
    )

    missing: list[str] = list(extra_missing_requirements)
    if pack.active_chapter_id is None:
        missing.append("active_chapter_unbound")
    if not truth:
        missing.append("project_truth_unbound")
    if not character:
        missing.append("current_character_state_unbound")
    if not scene_sources:
        missing.append("scene_contract_unbound")
    if not control_bound:
        missing.append("chapter_control_contract_unbound")
    if not card_ids:
        missing.append("formal_knowledge_unavailable")
    if route.source.value == "native_knowledge" and len(card_provenance) != len(card_ids):
        missing.append("formal_knowledge_lineage_unavailable")
    if pack.readiness.value == "blocked":
        missing.append("context_pack_blocked")
    if route.status is NlclRouteStatus.UNAVAILABLE:
        missing.append("knowledge_route_unavailable")

    # M3e intentionally cannot authorize prose until the Writer/writeback closure exists.
    warnings = (
        "formal_writer_contract_unavailable",
        "writeback_closure_unavailable",
    )
    critical = {
        "active_chapter_unbound",
        "project_truth_unbound",
        "current_character_state_unbound",
        "scene_contract_unbound",
        "chapter_control_contract_unbound",
        "context_pack_blocked",
        "knowledge_route_unavailable",
        "formal_knowledge_lineage_unavailable",
        "migrated_prewrite_package_requires_recompile",
    }
    missing_tuple = tuple(dict.fromkeys(missing))
    readiness = (
        "blocked"
        if critical.intersection(missing_tuple)
        else "risk_draft_only"
    )
    package_id = "prewrite-package-" + hashlib.sha256(
        f"zhimeng.prewrite-operation-package.v1\0{pack.context_pack_id}".encode("utf-8")
    ).hexdigest()[:32]
    material = {
        "operation_package_id": package_id,
        "context_pack_id": pack.context_pack_id,
        "context_pack_hash": pack.pack_hash,
        "route_receipt_id": route.route_receipt_id,
        "route_receipt_hash": route.receipt_hash,
        "project_id": pack.project_id,
        "project_revision": pack.project_revision,
        "project_fingerprint": pack.project_fingerprint,
        "active_chapter_id": pack.active_chapter_id,
        "task_goal": pack.objective,
        "must_obey_truth": truth,
        "current_character_state": character,
        "relevant_evidence_refs": evidence,
        "constraints_and_taboos": (
            "project_truth_precedence",
            "whole_library_load_forbidden",
            "selected_knowledge_is_reference_not_project_truth",
            "formal_draft_requires_separate_authorization",
        ),
        "scene_contract": PrewriteSceneContract(
            active_chapter_id=pack.active_chapter_id,
            task_goal=pack.objective,
            evidence_source_ids=scene_sources,
            status="bound" if pack.active_chapter_id is not None and scene_sources else "unbound",
        ),
        "style_output_contract": PrewriteStyleOutputContract(
            selected_card_ids=card_ids,
            selected_card_hashes=card_hashes,
            selected_card_provenance=card_provenance,
            status="bound" if card_ids else "unbound",
        ),
        "acceptance_gate": PrewriteAcceptanceGate(
            required_checks=(
                "project_audit",
                "operation_package_integrity",
                "project_truth_binding",
                "chapter_control_contract",
                "context_budget",
                "author_approval",
                "writeback_receipt",
            )
        ),
        "failure_routing": (
            PrewriteFailureRoute(
                failure_signature="project_audit_blocked",
                rewrite_from="L0_PROJECT_AUDIT",
            ),
            PrewriteFailureRoute(
                failure_signature="project_truth_unbound",
                rewrite_from="L1_PROJECT_TRUTH",
            ),
            PrewriteFailureRoute(
                failure_signature="truth_or_state_unbound",
                rewrite_from="L1_PROJECT_STATE",
            ),
            PrewriteFailureRoute(
                failure_signature="operation_package_unreadable_or_missing",
                rewrite_from="L2_CONTROL_CARD",
            ),
            PrewriteFailureRoute(
                failure_signature="retrieval_selection_or_budget_unproven",
                rewrite_from="L3_DISTILLATION_RETRIEVAL",
            ),
            PrewriteFailureRoute(
                failure_signature="formal_writer_contract_unavailable",
                rewrite_from="L4_DRAFT_EXECUTION",
            ),
        ),
        "library_activation_budget": PrewriteLibraryBudget(
            max_characters=pack.budget.max_characters,
            used_characters=pack.budget.used_characters,
            max_tokens=pack.budget.max_tokens,
            used_tokens=pack.budget.used_tokens,
            activated_source_ids=tuple(item.source_id for item in evidence),
        ),
        "whole_library_load_forbidden": True,
        "readiness": readiness,
        "formal_draft_allowed": False,
        "missing_requirements": missing_tuple,
        "warnings": warnings,
        "created_at": pack.created_at,
    }
    draft = PrewriteOperationPackage.model_construct(**material, package_hash="0" * 64)
    return PrewriteOperationPackage(
        **material,
        package_hash=prewrite_operation_package_hash(draft),
    )


__all__ = [
    "PrewriteAcceptanceGate",
    "PrewriteEvidenceRef",
    "PrewriteFailureRoute",
    "PrewriteLibraryBudget",
    "PrewriteOperationPackage",
    "PrewriteSceneContract",
    "PrewriteStyleOutputContract",
    "build_prewrite_operation_package",
    "prewrite_operation_package_hash",
]
