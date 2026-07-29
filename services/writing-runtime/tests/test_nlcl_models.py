from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    canonical_json_hash,
)
from zhimeng_runtime.knowledge_models import knowledge_formalization_receipt_id
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


def _provenance() -> KnowledgeProvenanceRef:
    source_import_id = "source-import-" + "1" * 32
    return KnowledgeProvenanceRef(
        source_import_id=source_import_id,
        source_receipt_id=source_import_receipt_id(source_import_id),
        source_receipt_hash="2" * 64,
        source_packet_fingerprint="3" * 64,
        cards_draft_hash="4" * 64,
        candidate_id="knowledge-candidate-" + "5" * 32,
        candidate_origin_hash="6" * 64,
        card_id="pressure-card",
        version_id="knowledge-version-" + "b" * 32,
        version_content_hash=canonical_json_hash({"card": "pressure"}),
        version_lineage_receipt_id="knowledge-lineage-" + "7" * 32,
        version_lineage_receipt_hash="8" * 64,
        formalization_receipt_id=knowledge_formalization_receipt_id(source_import_id),
        formalization_receipt_hash="9" * 64,
    )


def _pack(*, with_provenance: bool = True):
    content_hash = canonical_json_hash({"card": "pressure"})
    return compile_context_pack(
        context_pack_id="context-pack-nlcl-model",
        session_id="session-nlcl-model",
        project_id="project-nlcl-model",
        history_through_ordinal=0,
        request=ContextCreateRequest(
            objective="Audit the current chapter before drafting.",
            profile="chapter",
            active_chapter_id="CH001",
            expected_project_revision=3,
            expected_project_fingerprint="a" * 64,
            project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            project_kb_manifest_hash="b" * 64,
        ),
        history=(),
        candidates=(
            ContextCandidateFragment(
                source_id="knowledge-card:pressure-card",
                source_type="native_knowledge_card:work",
                title="Pressure escalation",
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                version="knowledge-version-" + "b" * 32,
                source_hash=content_hash,
                content='{"summary":"Escalate observable pressure."}',
                priority=700,
                mandatory=False,
                eligible=True,
                knowledge_provenance=_provenance() if with_provenance else None,
            ),
        ),
        max_characters=20_000,
        max_tokens=20_000,
        knowledge_fingerprint="c" * 64,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _route_card(pack) -> NlclSelectedCard:
    fragment = pack.included[0]
    return NlclSelectedCard(
        card_id="pressure-card",
        card_type="work",
        title="Pressure escalation",
        version_id=fragment.version,
        content_hash=fragment.source_hash,
        route_terms=("pressure", "escalation"),
        recommended_profiles=("chapter",),
        negative_routes=("slice_of_life",),
        safety_summary="Use only the transferable mechanism.",
        feedback_layer="L3_DISTILLATION_RETRIEVAL",
        knowledge_provenance=fragment.knowledge_provenance,
    )


def test_ready_receipt_binds_only_compact_included_cards_and_creation_audit() -> None:
    pack = _pack()
    receipt = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(_route_card(pack),),
    )

    assert receipt.status == "ready"
    assert receipt.formal_draft_allowed is False
    assert receipt.allowed_output == "context_and_creation_audit_only"
    assert receipt.whole_library_load_forbidden is True
    assert [card.card_id for card in receipt.selected_cards] == ["pressure-card"]
    assert receipt.chapter_call_list[0].skill_id == "novel-creation-suite.project-audit"
    assert (
        receipt.chapter_call_list[0].execution_status
        == "requires_runtime_catalog_check"
    )
    assert "content" not in receipt.selected_cards[0].model_dump()


def test_native_receipt_without_formal_provenance_is_only_risk_ready() -> None:
    pack = _pack(with_provenance=False)
    receipt = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(_route_card(pack),),
    )

    assert receipt.status == "risk_ready"
    assert "native_knowledge_lineage_unavailable" in receipt.strict_failures


def test_receipt_rejects_hash_tampering_and_false_formal_authorization() -> None:
    pack = _pack()
    receipt = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(_route_card(pack),),
    )
    payload = receipt.model_dump(mode="json", by_alias=True)
    payload["receipt_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="receipt hash"):
        NlclRouteReceipt.model_validate(payload)

    payload = receipt.model_dump(mode="json", by_alias=True)
    payload["formal_draft_allowed"] = True
    with pytest.raises(ValidationError, match="formal_draft_allowed"):
        NlclRouteReceipt.model_validate(payload)


def test_unavailable_receipt_is_explicit_and_never_claims_route_readiness() -> None:
    receipt = build_nlcl_route_receipt(
        _pack(),
        source=NlclRouteSource.UNAVAILABLE,
    )

    assert receipt.status == "unavailable"
    assert receipt.selected_cards == ()
    assert receipt.strict_failures == ("knowledge_route_unavailable",)
    assert "formal_draft_not_authorized" in receipt.warnings
