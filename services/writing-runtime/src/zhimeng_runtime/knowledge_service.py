from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhimeng_runtime.context_models import (
    FormalKnowledgeSelector,
    canonical_json,
    canonical_json_hash,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCandidateDecisionRequest,
    KnowledgeCandidateList,
    KnowledgeCandidateOrigin,
    KnowledgeCandidateStatus,
    KnowledgeCandidateUpdateRequest,
    KnowledgeCard,
    KnowledgeCardList,
    KnowledgeScopedCardList,
    KnowledgeCardStateRequest,
    KnowledgeCardStatus,
    KnowledgeFormalizationReceipt,
    KnowledgeFormalizationReceiptV2,
    KnowledgeFormalizationRecord,
    KnowledgeFormalizationRequest,
    KnowledgeFormalizationStatus,
    KnowledgePromotionRequest,
    KnowledgeReviewIssue,
    KnowledgeRollbackRequest,
    KnowledgeLineageStatus,
    KnowledgeVersionFormalizationStatus,
    knowledge_candidate_origin_hash,
    knowledge_candidate_set_hash,
    native_knowledge_fingerprint,
)
from zhimeng_runtime.knowledge_scope_models import (
    APPLICATION_KNOWLEDGE_SCOPE_ID,
    KnowledgeScopeRecord,
)
from zhimeng_runtime.knowledge_store import (
    KnowledgeScopeSnapshot,
    KnowledgeStore,
    KnowledgeStoreConflict,
    KnowledgeStoreError,
    KnowledgeStoreNotFound,
)
from zhimeng_runtime.source_import_models import (
    AnySourceImportJob,
    SourceImportStatus,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_receipt_models import (
    AnySourceImportExecutionReceipt,
    parse_source_import_execution_receipt,
    validate_source_import_receipt_job,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef
from zhimeng_runtime.nlcl_models import NlclRouteReceipt
from zhimeng_runtime.post_draft_gate_models import (
    KnowledgeValidatorBinding,
    KnowledgeValidatorBundle,
    KnowledgeValidatorRule,
    knowledge_validator_bundle_hash,
    knowledge_validator_rules_hash,
)


MAX_DRAFT_BYTES = 4 * 1024 * 1024
MAX_CARDS_PER_DRAFT = 64
CARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{1,160}$")
PLACEHOLDER_PATTERN = re.compile(
    r"(?:\bpending(?:_[a-z]+)*\b|\bunknown_until_review\b|\bnot yet validated\b|待验证|待补|未验证)",
    re.IGNORECASE,
)
WORK_REQUIRED_TAXONOMY = ("genre", "scene_function", "emotion", "craft")
WORK_REQUIRED_PAYLOAD = (
    "premise_pattern",
    "core_reader_promise",
    "opening_contract",
    "progression_map",
    "recurring_scene_loops",
    "transfer_limits",
)
SUPPORTED_PROMOTION_CARD_TYPES = frozenset({"work", "voice", "validator"})
VOICE_REQUIRED_TAXONOMY = ("style", "pov", "content_boundary")
VOICE_REQUIRED_TEXT_PAYLOAD = (
    "pov_distance",
    "sentence_rhythm",
    "dialogue_behavior",
    "description_behavior",
    "emotion_handling",
    "human_web_prose",
    "sentence_variety",
)
VOICE_REQUIRED_LIST_PAYLOAD = (
    "diction_pool",
    "scene_anchor_rules",
    "dialogue_pressure_rules",
    "anti_ai_failure_signatures",
    "readability_acceptance_tests",
    "do_not_copy",
)
VALIDATOR_REQUIRED_TAXONOMY = ("problem_symptom", "content_boundary")
VALIDATOR_REQUIRED_LIST_PAYLOAD = (
    "applies_to",
    "checks",
    "reject_conditions",
    "rewrite_routes",
)
VALIDATOR_LAYER_IDS = frozenset(
    {
        "L0_INPUT",
        "L1_PROJECT_TRUTH",
        "L2_CONTROL_CARD",
        "L3_DISTILLATION_RETRIEVAL",
        "L4_DRAFT_EXECUTION",
        "L5_DEAI_STYLE",
        "L6_REVIEW_ACCEPTANCE",
        "L7_WRITEBACK",
    }
)
LADDER_STEPS = (
    "internal_consistency",
    "predictive_validation",
    "blind_sample_validation",
    "extreme_scenario_stress_test",
)


class KnowledgeServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: object | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = details
        self.retryable = retryable


class KnowledgeNotFoundError(KnowledgeServiceError):
    pass


class KnowledgeConflictError(KnowledgeServiceError):
    pass


class KnowledgeInvalidSourceError(KnowledgeServiceError):
    pass


class KnowledgeReviewRequiredError(KnowledgeServiceError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeContextCard:
    card_id: str
    card_type: str
    title: str
    content: str
    version_id: str
    content_hash: str
    score: int
    route_terms: tuple[str, ...]
    recommended_profiles: tuple[str, ...]
    negative_routes: tuple[str, ...]
    safety_summary: str | None
    feedback_layer: str | None
    provenance: KnowledgeProvenanceRef


@dataclass(frozen=True, slots=True)
class KnowledgeContextSelection:
    fingerprint: str
    cards: tuple[KnowledgeContextCard, ...]


class KnowledgeService:
    def __init__(self, database: SqliteStore, source_state_root: Path) -> None:
        self.database = database
        self.store = KnowledgeStore(database)
        self.source_state_root = source_state_root.resolve(strict=False)

    def import_source_candidates(
        self,
        source_import_id: str,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCandidateList:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        job = self._source_job(source_import_id)
        if job.status not in {
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.COMPLETED,
        }:
            raise KnowledgeConflictError(
                f"source import is not ready for knowledge review: {job.status.value}"
            )
        source_receipt = self._verified_source_receipt(job)
        if job.status is SourceImportStatus.COMPLETED:
            try:
                existing = self.store.list_candidates(
                    source_import_id,
                    scope_id=resolved_scope_id,
                )
            except KnowledgeStoreError as exc:
                raise self._store_error(exc) from exc
            if not existing:
                raise KnowledgeConflictError(
                    "completed source import has no immutable candidate set"
                )
            return KnowledgeCandidateList(
                candidates=existing,
                candidate_set_hash=knowledge_candidate_set_hash(existing),
            )
        draft = job.distillation_draft
        if draft is None:
            raise KnowledgeInvalidSourceError("source import has no distillation draft")
        artifact = next(
            (
                item
                for item in draft.artifacts
                if item.kind == "cards_draft_json"
                and Path(item.path).resolve(strict=False)
                == Path(draft.cards_path).resolve(strict=False)
            ),
            None,
        )
        if artifact is None or artifact.sha256 is None:
            raise KnowledgeInvalidSourceError("distillation draft has no hashed cards artifact")
        path = self._trusted_draft_path(draft.cards_path)
        try:
            before = path.stat()
            if before.st_size > MAX_DRAFT_BYTES:
                raise KnowledgeInvalidSourceError(
                    "distillation draft exceeds the supported size"
                )
            raw_payload = path.read_bytes()
            after = path.stat()
        except KnowledgeInvalidSourceError:
            raise
        except OSError as exc:
            raise KnowledgeConflictError(
                "distillation draft disappeared while it was being read",
                retryable=True,
            ) from exc
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw_payload) != after.st_size
        ):
            raise KnowledgeConflictError(
                "distillation draft changed while it was being read",
                retryable=True,
            )
        actual_hash = hashlib.sha256(raw_payload).hexdigest()
        if actual_hash != artifact.sha256:
            raise KnowledgeConflictError(
                "distillation draft changed after the source import receipt was recorded",
                retryable=True,
            )
        if actual_hash != source_receipt.cards_draft_hash:
            raise KnowledgeConflictError(
                "distillation draft no longer matches the Runtime execution receipt",
                retryable=True,
            )
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise KnowledgeInvalidSourceError("distillation draft is not valid UTF-8 JSON") from exc
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_CARDS_PER_DRAFT:
            raise KnowledgeInvalidSourceError("distillation draft must contain 1-64 cards")
        if len(payload) != draft.card_count or len(payload) != source_receipt.card_count:
            raise KnowledgeConflictError(
                "distillation draft card count does not match its execution receipt",
                retryable=True,
            )

        records: list[KnowledgeCandidate] = []
        observed_at = datetime.now(UTC)
        for index, raw_card in enumerate(payload):
            if not isinstance(raw_card, dict):
                raise KnowledgeInvalidSourceError(
                    f"distillation draft card {index} is not an object"
                )
            card = deepcopy(raw_card)
            card_id = str(card.get("card_id") or "").strip()
            card_type = str(card.get("card_type") or "").strip().lower()
            if not CARD_ID_PATTERN.fullmatch(card_id) or not card_type:
                raise KnowledgeInvalidSourceError(
                    f"distillation draft card {index} has no safe card identity"
                )
            if card.get("draft_candidate") is not True:
                raise KnowledgeInvalidSourceError(
                    f"distillation draft card {index} is not marked as a draft candidate"
                )
            source_card_hash = canonical_json_hash(raw_card)
            origin_material = {
                "source_receipt_id": source_receipt.receipt_id,
                "source_receipt_hash": source_receipt.receipt_hash,
                "source_packet_fingerprint": source_receipt.source_packet_fingerprint,
                "draft_fingerprint": source_receipt.draft_fingerprint,
                "cards_draft_hash": source_receipt.cards_draft_hash,
                "source_card_index": index,
                "source_card_hash": source_card_hash,
            }
            provisional_origin = KnowledgeCandidateOrigin.model_construct(
                **origin_material,
                origin_hash="0" * 64,
            )
            origin = KnowledgeCandidateOrigin(
                **origin_material,
                origin_hash=knowledge_candidate_origin_hash(provisional_origin),
            )
            candidate_id = self._candidate_id(origin.origin_hash)
            issues = review_issues(card)
            status = (
                KnowledgeCandidateStatus.PENDING
                if card_type in SUPPORTED_PROMOTION_CARD_TYPES
                else KnowledgeCandidateStatus.DEFERRED
            )
            if status is KnowledgeCandidateStatus.DEFERRED:
                issues = [
                    KnowledgeReviewIssue(
                        code="card_type_not_supported",
                        field="card_type",
                        message=(
                            f"{card_type} cards remain traceable but cannot be promoted "
                            "by the current native knowledge contract"
                        ),
                    ),
                    *[item for item in issues if item.code != "card_type_not_supported"],
                ]
            record = KnowledgeCandidate(
                candidate_id=candidate_id,
                source_import_id=source_import_id,
                source_card_index=index,
                source_draft_hash=actual_hash,
                lineage_status=KnowledgeLineageStatus.VERIFIED,
                origin=origin,
                card_id=card_id,
                card_type=card_type,
                status=status,
                revision=1,
                content_hash=canonical_json_hash(card),
                card=card,
                issues=issues,
                created_at=observed_at,
                updated_at=observed_at,
            )
            records.append(record)
        try:
            persisted = self.store.create_candidates(records, scope_id=resolved_scope_id)
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc
        return KnowledgeCandidateList(
            candidates=persisted,
            candidate_set_hash=knowledge_candidate_set_hash(persisted),
        )

    def list_candidates(
        self,
        source_import_id: str | None = None,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCandidateList:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            records = self.store.list_candidates(source_import_id, scope_id=resolved_scope_id)
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc
        return KnowledgeCandidateList(
            candidates=records,
            candidate_set_hash=knowledge_candidate_set_hash(records),
        )

    def update_candidate(
        self,
        candidate_id: str,
        request: KnowledgeCandidateUpdateRequest,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCandidate:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        card = deepcopy(request.card)
        self._validate_card_size(card)
        card_id = str(card.get("card_id") or "").strip()
        card_type = str(card.get("card_type") or "").strip().lower()
        if not CARD_ID_PATTERN.fullmatch(card_id) or not card_type:
            raise KnowledgeReviewRequiredError(
                "reviewed card has no safe identity",
                details=[
                    KnowledgeReviewIssue(
                        code="invalid_card_identity",
                        field="card_id",
                        message="card_id and card_type must form a safe stable identity",
                    ).model_dump(mode="json")
                ],
            )
        issues = review_issues(card)
        try:
            return self.store.update_candidate(
                candidate_id,
                expected_revision=request.expected_revision,
                expected_content_hash=request.expected_content_hash,
                card_id=card_id,
                card_type=card_type,
                content_hash=canonical_json_hash(card),
                card=card,
                issues=issues,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def promote_candidate(
        self,
        candidate_id: str,
        request: KnowledgePromotionRequest,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCard:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            candidate = self.store.get_candidate(candidate_id, scope_id=resolved_scope_id)
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc
        if (
            candidate.revision != request.expected_revision
            or candidate.content_hash != request.expected_content_hash
        ):
            raise KnowledgeConflictError(
                "knowledge candidate changed before promotion",
                retryable=True,
            )
        issues = review_issues(candidate.card)
        if issues:
            raise KnowledgeReviewRequiredError(
                "knowledge candidate still requires review",
                details=[item.model_dump(mode="json") for item in issues],
            )
        try:
            return self.store.promote_candidate(
                candidate_id,
                expected_revision=request.expected_revision,
                expected_content_hash=request.expected_content_hash,
                replace_active=request.replace_active,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def reject_candidate(
        self,
        candidate_id: str,
        request: KnowledgeCandidateDecisionRequest,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCandidate:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            return self.store.reject_candidate(
                candidate_id,
                expected_revision=request.expected_revision,
                expected_content_hash=request.expected_content_hash,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def get_formalization(
        self,
        source_import_id: str,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeFormalizationRecord:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        job = self._source_job(source_import_id)
        try:
            source_receipt_payload = self.database.get_source_import_execution_receipt(
                source_import_id
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise KnowledgeInvalidSourceError(
                "source import execution receipt is invalid"
            ) from exc
        if source_receipt_payload is None:
            return KnowledgeFormalizationRecord(
                source_import_id=source_import_id,
                status=KnowledgeFormalizationStatus.LEGACY_UNVERIFIED,
                receipt=None,
            )
        self._verified_source_receipt(job, payload=source_receipt_payload)
        try:
            receipt = self.store.get_formalization_receipt(
                source_import_id,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc
        if receipt is not None:
            return KnowledgeFormalizationRecord(
                source_import_id=source_import_id,
                status=KnowledgeFormalizationStatus.VERIFIED,
                receipt=receipt,
            )
        return KnowledgeFormalizationRecord(
            source_import_id=source_import_id,
            status=KnowledgeFormalizationStatus.AWAITING_REVIEW,
            receipt=None,
        )

    def finalize_source_import(
        self,
        source_import_id: str,
        request: KnowledgeFormalizationRequest,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            return self.store.finalize_source_import(
                source_import_id,
                expected_source_receipt_hash=request.expected_source_receipt_hash,
                expected_draft_hash=request.expected_draft_hash,
                expected_candidate_set_hash=request.expected_candidate_set_hash,
                source_state_root=self.source_state_root,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def list_cards(
        self,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCardList | KnowledgeScopedCardList:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        include_application = resolved_scope_id != APPLICATION_KNOWLEDGE_SCOPE_ID
        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=include_application,
        )
        fingerprint = self._fingerprint_from_scope_read(
            snapshot.cards,
            snapshot.fingerprint_material,
        )
        if project_id is None and resolved_scope_id == APPLICATION_KNOWLEDGE_SCOPE_ID:
            return KnowledgeCardList(cards=snapshot.cards, fingerprint=fingerprint)
        return KnowledgeScopedCardList(
            scope=snapshot.scope,
            includes_application_scope=include_application,
            cards=snapshot.cards,
            inherited_card_ids=list(snapshot.inherited_card_ids),
            fingerprint=fingerprint,
        )

    def set_card_status(
        self,
        card_id: str,
        request: KnowledgeCardStateRequest,
        *,
        status: KnowledgeCardStatus,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCard:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            return self.store.set_card_status(
                card_id,
                status=status,
                expected_state_revision=request.expected_state_revision,
                expected_active_version_id=request.expected_active_version_id,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def rollback_card(
        self,
        card_id: str,
        request: KnowledgeRollbackRequest,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeCard:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            return self.store.rollback_card(
                card_id,
                target_version_id=request.target_version_id,
                expected_state_revision=request.expected_state_revision,
                expected_active_version_id=request.expected_active_version_id,
                scope_id=resolved_scope_id,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def has_active_cards(
        self,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> bool:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        include_application = resolved_scope_id != APPLICATION_KNOWLEDGE_SCOPE_ID
        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=include_application,
            active_only=True,
        )
        return any(
            card.active_version.formalization_status
            is KnowledgeVersionFormalizationStatus.VERIFIED
            for card in snapshot.cards
        )

    def observe_knowledge_fingerprint(
        self,
        project_id: str | None = None,
        *,
        scope_id: str | None = None,
    ) -> str:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        include_application = resolved_scope_id != APPLICATION_KNOWLEDGE_SCOPE_ID
        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=include_application,
            active_only=True,
        )
        return self._fingerprint_from_scope_read(
            snapshot.cards,
            snapshot.fingerprint_material,
        )

    def select_for_context(
        self,
        query: str,
        profile: str,
        *,
        limit: int = 8,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeContextSelection:
        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        include_application = resolved_scope_id != APPLICATION_KNOWLEDGE_SCOPE_ID
        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=include_application,
            active_only=True,
        )
        fingerprint = self._fingerprint_from_scope_read(
            snapshot.cards,
            snapshot.fingerprint_material,
        )
        normalized_query = " ".join(query.lower().split())
        selected: list[KnowledgeContextCard] = []
        for record in snapshot.cards:
            version = record.active_version
            if (
                version.lineage_status is not KnowledgeLineageStatus.VERIFIED
                or version.lineage is None
                or version.formalization_status
                is not KnowledgeVersionFormalizationStatus.VERIFIED
                or version.formalization is None
            ):
                continue
            payload = version.card
            score = self._relevance_score(payload, normalized_query, profile)
            if score <= 0:
                continue
            selected.append(self._context_card(record, score=score))
        selected.sort(key=lambda item: (-item.score, item.card_id))
        return KnowledgeContextSelection(
            fingerprint=fingerprint,
            cards=tuple(selected[: max(1, min(limit, 24))]),
        )

    def select_formalized_batch_for_context(
        self,
        selector: FormalKnowledgeSelector,
        *,
        project_id: str,
    ) -> KnowledgeContextSelection:
        """Resolve one author-selected formalization batch without lexical routing."""

        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=None)
        formalization = self.get_formalization(
            selector.source_import_id,
            project_id=project_id,
        )
        receipt = formalization.receipt
        if (
            formalization.status is not KnowledgeFormalizationStatus.VERIFIED
            or receipt is None
        ):
            raise KnowledgeConflictError(
                "selected knowledge source import is not formally verified"
            )
        if (
            receipt.formalization_receipt_id
            != selector.formalization_receipt_id
            or receipt.receipt_hash != selector.formalization_receipt_hash
        ):
            raise KnowledgeConflictError(
                "selected knowledge formalization receipt is stale or invalid"
            )

        promoted = {
            decision.promoted_version_id: decision
            for decision in receipt.decisions
            if decision.decision == "promoted"
            and decision.promoted_version_id is not None
        }
        selected_version_ids = {item.version_id for item in selector.cards}
        if not promoted or selected_version_ids != set(promoted):
            raise KnowledgeConflictError(
                "formal knowledge selector must contain the complete promoted batch"
            )

        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=True,
            active_only=True,
        )
        fingerprint = self._fingerprint_from_scope_read(
            snapshot.cards,
            snapshot.fingerprint_material,
        )
        if fingerprint != selector.expected_knowledge_fingerprint:
            raise KnowledgeConflictError(
                "formal knowledge changed after the batch was selected"
            )

        cards_by_id = {record.card_id: record for record in snapshot.cards}
        selected_cards: list[KnowledgeContextCard] = []
        for selected in selector.cards:
            record = cards_by_id.get(selected.card_id)
            if record is None or record.status is not KnowledgeCardStatus.ACTIVE:
                raise KnowledgeConflictError(
                    f"selected formal knowledge card is no longer active: {selected.card_id}"
                )
            version = record.active_version
            decision = promoted[selected.version_id]
            if (
                version.version_id != selected.version_id
                or version.content_hash != selected.content_hash
                or version.lineage_status is not KnowledgeLineageStatus.VERIFIED
                or version.lineage is None
                or version.formalization_status
                is not KnowledgeVersionFormalizationStatus.VERIFIED
                or version.formalization is None
                or version.lineage.source_import_id != selector.source_import_id
                or version.lineage.candidate_id != decision.candidate_id
                or version.lineage.lineage_receipt_id
                != decision.version_lineage_receipt_id
                or version.lineage.receipt_hash
                != decision.version_lineage_receipt_hash
                or version.formalization.formalization_receipt_id
                != selector.formalization_receipt_id
                or version.formalization.formalization_receipt_hash
                != selector.formalization_receipt_hash
            ):
                raise KnowledgeConflictError(
                    f"selected formal knowledge card version is stale: {selected.card_id}"
                )
            selected_cards.append(self._context_card(record, score=300))

        return KnowledgeContextSelection(
            fingerprint=fingerprint,
            cards=tuple(selected_cards),
        )

    def build_gate_validator_bundle(
        self,
        project_id: str,
        route: NlclRouteReceipt,
    ) -> KnowledgeValidatorBundle | None:
        """Resolve the exact formal validator versions selected by one NLCL route."""

        selected_validators = tuple(
            item
            for item in route.selected_cards
            if item.card_type == "validator" and item.knowledge_provenance is not None
        )
        if not selected_validators:
            return None
        if route.project_id != project_id or route.knowledge_fingerprint is None:
            raise KnowledgeConflictError(
                "NLCL validator route does not match the bound project knowledge"
            )

        resolved_scope_id = self._resolve_scope_id(
            project_id=project_id,
            scope_id=None,
        )
        snapshot = self._read_scope_cards(
            resolved_scope_id,
            include_application=True,
            active_only=True,
        )
        current_fingerprint = self._fingerprint_from_scope_read(
            snapshot.cards,
            snapshot.fingerprint_material,
        )
        if current_fingerprint != route.knowledge_fingerprint:
            raise KnowledgeConflictError(
                "formal validator knowledge changed after the NLCL route was reviewed"
            )
        cards_by_id = {record.card_id: record for record in snapshot.cards}
        bindings: list[KnowledgeValidatorBinding] = []
        remaining_rule_budget = 16
        for selected in selected_validators:
            record = cards_by_id.get(selected.card_id)
            if record is None or record.status is not KnowledgeCardStatus.ACTIVE:
                raise KnowledgeConflictError(
                    f"selected formal validator is no longer active: {selected.card_id}"
                )
            version = record.active_version
            if (
                version.version_id != selected.version_id
                or version.content_hash != selected.content_hash
                or version.lineage_status is not KnowledgeLineageStatus.VERIFIED
                or version.lineage is None
                or version.formalization_status
                is not KnowledgeVersionFormalizationStatus.VERIFIED
                or version.formalization is None
            ):
                raise KnowledgeConflictError(
                    f"selected formal validator version drifted: {selected.card_id}"
                )
            current_provenance = KnowledgeProvenanceRef(
                source_import_id=version.lineage.source_import_id,
                source_receipt_id=version.lineage.candidate_origin.source_receipt_id,
                source_receipt_hash=version.lineage.candidate_origin.source_receipt_hash,
                source_packet_fingerprint=(
                    version.lineage.candidate_origin.source_packet_fingerprint
                ),
                cards_draft_hash=version.lineage.candidate_origin.cards_draft_hash,
                candidate_id=version.lineage.candidate_id,
                candidate_origin_hash=version.lineage.candidate_origin.origin_hash,
                card_id=record.card_id,
                version_id=version.version_id,
                version_content_hash=version.content_hash,
                version_lineage_receipt_id=version.lineage.lineage_receipt_id,
                version_lineage_receipt_hash=version.lineage.receipt_hash,
                formalization_receipt_id=(
                    version.formalization.formalization_receipt_id
                ),
                formalization_receipt_hash=(
                    version.formalization.formalization_receipt_hash
                ),
            )
            if current_provenance != selected.knowledge_provenance:
                raise KnowledgeConflictError(
                    f"selected formal validator provenance drifted: {selected.card_id}"
                )

            payload = version.card.get("type_payload")
            payload = payload if isinstance(payload, dict) else {}
            raw_rules = payload.get("execution_rules")
            rules: list[KnowledgeValidatorRule] = []
            configuration_errors: set[str] = set()
            if not isinstance(raw_rules, list) or not raw_rules:
                configuration_errors.add("validator_execution_rules_missing")
            elif len(raw_rules) > 16:
                configuration_errors.add("validator_execution_rules_limit_exceeded")
            if isinstance(raw_rules, list):
                for raw_rule in raw_rules[:16]:
                    try:
                        rules.append(
                            KnowledgeValidatorRule.model_validate_json(
                                json.dumps(raw_rule, ensure_ascii=False),
                                strict=True,
                            )
                        )
                    except (TypeError, ValueError):
                        configuration_errors.add("validator_execution_rule_invalid")
            rules.sort(key=lambda item: item.rule_id)
            if len({item.rule_id for item in rules}) != len(rules):
                configuration_errors.add("validator_execution_rule_duplicate")
                unique: dict[str, KnowledgeValidatorRule] = {}
                for rule in rules:
                    unique.setdefault(rule.rule_id, rule)
                rules = list(unique.values())
            if len({item.failure_signature for item in rules}) != len(rules):
                configuration_errors.add("validator_failure_signature_duplicate")
                unique_signatures: dict[str, KnowledgeValidatorRule] = {}
                for rule in rules:
                    unique_signatures.setdefault(rule.failure_signature, rule)
                rules = list(unique_signatures.values())
            if len(rules) > remaining_rule_budget:
                configuration_errors.add("validator_bundle_rule_limit_exceeded")
                rules = rules[:remaining_rule_budget]
            remaining_rule_budget -= len(rules)

            validator_name = str(payload.get("validator_name") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", validator_name):
                validator_name = "invalid_validator_configuration"
                configuration_errors.add("validator_name_invalid")
            errors = tuple(sorted(configuration_errors))
            canonical_rules = tuple(rules)
            material = {
                "card_id": record.card_id,
                "version_id": version.version_id,
                "content_hash": version.content_hash,
                "validator_name": validator_name,
                "provenance": current_provenance,
                "rules": canonical_rules,
                "configuration_errors": errors,
            }
            bindings.append(
                KnowledgeValidatorBinding(
                    **material,
                    rules_hash=knowledge_validator_rules_hash(
                        canonical_rules,
                        errors,
                    ),
                )
            )

        bindings.sort(key=lambda item: item.card_id)
        bundle_material = {
            "context_pack_id": route.context_pack_id,
            "context_pack_hash": route.context_pack_hash,
            "route_receipt_id": route.route_receipt_id,
            "route_receipt_hash": route.receipt_hash,
            "knowledge_fingerprint": route.knowledge_fingerprint,
            "validators": tuple(bindings),
        }
        draft = KnowledgeValidatorBundle.model_construct(
            **bundle_material,
            bundle_hash="0" * 64,
        )
        return KnowledgeValidatorBundle(
            **bundle_material,
            bundle_hash=knowledge_validator_bundle_hash(draft),
        )

    def _source_job(self, source_import_id: str) -> AnySourceImportJob:
        try:
            payload = self.database.get_source_import_job(source_import_id)
            if payload is None:
                raise KnowledgeNotFoundError("source import was not found")
            return parse_source_import_job(payload)
        except KnowledgeNotFoundError:
            raise
        except Exception as exc:
            raise KnowledgeInvalidSourceError("source import receipt is invalid") from exc

    def _verified_source_receipt(
        self,
        job: AnySourceImportJob,
        *,
        payload: dict[str, object] | None = None,
    ) -> AnySourceImportExecutionReceipt:
        try:
            if payload is None:
                payload = self.database.get_source_import_execution_receipt(job.import_id)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise KnowledgeInvalidSourceError(
                "source import execution receipt is invalid"
            ) from exc
        if payload is None:
            raise KnowledgeInvalidSourceError(
                "source import has no verified Runtime execution receipt"
            )
        try:
            receipt = parse_source_import_execution_receipt(payload)
            validate_source_import_receipt_job(receipt, job)
        except Exception as exc:
            raise KnowledgeInvalidSourceError(
                "source import job request does not match its execution receipt"
            ) from exc
        return receipt

    def _trusted_draft_path(self, raw_path: str) -> Path:
        try:
            path = Path(raw_path).resolve(strict=True)
            path.relative_to(self.source_state_root)
        except (OSError, ValueError) as exc:
            raise KnowledgeInvalidSourceError(
                "distillation draft is outside the Runtime-owned source state"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise KnowledgeInvalidSourceError("distillation draft path is not a regular file")
        return path

    @staticmethod
    def _candidate_id(origin_hash: str) -> str:
        digest = hashlib.sha256(
            f"zhimeng.knowledge-candidate.v2\x00{origin_hash}".encode("utf-8")
        ).hexdigest()
        return f"knowledge-candidate-{digest[:32]}"

    def get_scope(
        self,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> KnowledgeScopeRecord:
        """Resolve a durable scope without allowing caller-supplied aliases."""

        resolved_scope_id = self._resolve_scope_id(project_id=project_id, scope_id=scope_id)
        try:
            return self.store.get_scope(resolved_scope_id)
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def _resolve_scope_id(
        self,
        *,
        project_id: str | None,
        scope_id: str | None,
    ) -> str:
        if project_id is not None and scope_id is not None:
            try:
                project_scope = self.store.ensure_project_scope(project_id)
            except KnowledgeStoreError as exc:
                raise self._store_error(exc) from exc
            if project_scope.scope_id != scope_id:
                raise KnowledgeConflictError(
                    "project_id and scope_id resolve to different knowledge scopes"
                )
            return scope_id
        try:
            if project_id is not None:
                return self.store.ensure_project_scope(project_id).scope_id
            if scope_id is not None:
                return self.store.get_scope(scope_id).scope_id
            return self.store.ensure_application_scope().scope_id
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def _read_scope_cards(
        self,
        scope_id: str,
        *,
        include_application: bool,
        active_only: bool = False,
    ) -> KnowledgeScopeSnapshot:
        """Read cards and fingerprint material from one scope snapshot.

        The legacy application scope deliberately keeps its established read
        path and native fingerprint. Project scopes use the paired store
        snapshot so cards and the fingerprint cannot observe different SQLite
        revisions.
        """

        if scope_id == APPLICATION_KNOWLEDGE_SCOPE_ID and not include_application:
            try:
                cards = self.store.list_scope_cards(
                    scope_id,
                    include_application=False,
                    active_only=active_only,
                )
                return KnowledgeScopeSnapshot(
                    scope=self.store.ensure_application_scope(),
                    cards=cards,
                    fingerprint_material=None,
                    inherited_card_ids=(),
                )
            except KnowledgeStoreError as exc:
                raise self._store_error(exc) from exc
        try:
            return self.store.scope_snapshot(
                scope_id,
                include_application=include_application,
                active_only=active_only,
            )
        except KnowledgeStoreError as exc:
            raise self._store_error(exc) from exc

    def _fingerprint_from_scope_read(
        self,
        cards: list[KnowledgeCard],
        material: dict[str, object] | None,
    ) -> str:
        if material is None:
            return self._fingerprint(cards)
        return canonical_json_hash(material)

    @staticmethod
    def _fingerprint(cards: list[KnowledgeCard]) -> str:
        return native_knowledge_fingerprint(cards)

    @classmethod
    def _context_card(
        cls,
        record: KnowledgeCard,
        *,
        score: int,
    ) -> KnowledgeContextCard:
        version = record.active_version
        if (
            version.lineage_status is not KnowledgeLineageStatus.VERIFIED
            or version.lineage is None
            or version.formalization_status
            is not KnowledgeVersionFormalizationStatus.VERIFIED
            or version.formalization is None
        ):
            raise KnowledgeConflictError(
                f"knowledge card is not formally verified: {record.card_id}"
            )
        payload = version.card
        route_hints = (
            payload.get("nlcl_route_hints")
            if isinstance(payload.get("nlcl_route_hints"), dict)
            else {}
        )
        return KnowledgeContextCard(
            card_id=record.card_id,
            card_type=str(payload.get("card_type") or "unknown"),
            title=str(payload.get("title") or record.card_id),
            content=cls._context_content(payload),
            version_id=version.version_id,
            content_hash=version.content_hash,
            score=score,
            route_terms=cls._compact_route_values(route_hints.get("route_terms")),
            recommended_profiles=cls._compact_route_values(
                route_hints.get("recommended_profiles")
            ),
            negative_routes=cls._compact_route_values(
                route_hints.get("negative_routes")
            ),
            safety_summary=cls._compact_optional_text(
                route_hints.get("safety_summary"),
                limit=2000,
            ),
            feedback_layer=cls._compact_optional_text(
                route_hints.get("feedback_default_layer"),
                limit=128,
            ),
            provenance=KnowledgeProvenanceRef(
                source_import_id=version.lineage.source_import_id,
                source_receipt_id=version.lineage.candidate_origin.source_receipt_id,
                source_receipt_hash=(
                    version.lineage.candidate_origin.source_receipt_hash
                ),
                source_packet_fingerprint=(
                    version.lineage.candidate_origin.source_packet_fingerprint
                ),
                cards_draft_hash=(
                    version.lineage.candidate_origin.cards_draft_hash
                ),
                candidate_id=version.lineage.candidate_id,
                candidate_origin_hash=version.lineage.candidate_origin.origin_hash,
                card_id=record.card_id,
                version_id=version.version_id,
                version_content_hash=version.content_hash,
                version_lineage_receipt_id=version.lineage.lineage_receipt_id,
                version_lineage_receipt_hash=version.lineage.receipt_hash,
                formalization_receipt_id=(
                    version.formalization.formalization_receipt_id
                ),
                formalization_receipt_hash=(
                    version.formalization.formalization_receipt_hash
                ),
            ),
        )

    @staticmethod
    def _relevance_score(card: dict[str, Any], query: str, profile: str) -> int:
        searchable = [
            str(card.get("title") or ""),
            str(card.get("summary") or ""),
            *[str(item) for item in card.get("tags") or []],
            *[str(item) for item in card.get("keywords") or []],
            *[str(item) for item in card.get("applicable_tasks") or []],
        ]
        route_hints = card.get("nlcl_route_hints")
        if isinstance(route_hints, dict):
            searchable.extend(str(item) for item in route_hints.get("route_terms") or [])
            profiles = {str(item) for item in route_hints.get("recommended_profiles") or []}
        else:
            profiles = set()
        score = 4 if profile in profiles else 0
        for term in searchable:
            normalized = " ".join(term.lower().split())
            if normalized and normalized in query:
                score += 3
            elif normalized and any(token and token in query for token in normalized.split()):
                score += 1
        return score

    @staticmethod
    def _context_content(card: dict[str, Any]) -> str:
        safety = card.get("safety") if isinstance(card.get("safety"), dict) else {}
        return canonical_json(
            {
                "summary": card.get("summary"),
                "reader_promise": card.get("reader_promise"),
                "type_payload": card.get("type_payload"),
                "mechanics": card.get("mechanics"),
                "validation_tests": card.get("validation_tests"),
                "do_not_reuse": safety.get("do_not_reuse"),
            }
        )

    @staticmethod
    def _compact_route_values(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        normalized = [" ".join(str(item).split())[:160] for item in value]
        return tuple(dict.fromkeys(item for item in normalized if item))[:32]

    @staticmethod
    def _compact_optional_text(value: Any, *, limit: int) -> str | None:
        normalized = " ".join(str(value or "").split())
        return normalized[:limit] or None

    @staticmethod
    def _validate_card_size(card: dict[str, Any]) -> None:
        if len(canonical_json(card).encode("utf-8")) > 512 * 1024:
            raise KnowledgeReviewRequiredError("reviewed card exceeds 512 KiB")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _store_error(exc: KnowledgeStoreError) -> KnowledgeServiceError:
        if isinstance(exc, KnowledgeStoreNotFound):
            return KnowledgeNotFoundError(str(exc))
        if isinstance(exc, KnowledgeStoreConflict):
            return KnowledgeConflictError(str(exc), retryable=True)
        return KnowledgeServiceError(str(exc), retryable=True)


def review_issues(card: dict[str, Any]) -> list[KnowledgeReviewIssue]:
    issues: list[KnowledgeReviewIssue] = []

    def add(code: str, field: str, message: str) -> None:
        key = (code, field)
        if any((item.code, item.field) == key for item in issues):
            return
        issues.append(KnowledgeReviewIssue(code=code, field=field, message=message))

    card_id = str(card.get("card_id") or "").strip()
    if not CARD_ID_PATTERN.fullmatch(card_id):
        add("invalid_card_identity", "card_id", "Use a stable safe card_id")
    card_type = str(card.get("card_type") or "").strip().lower()
    if card_type not in SUPPORTED_PROMOTION_CARD_TYPES:
        add(
            "card_type_not_supported",
            "card_type",
            "Native promotion currently accepts reviewed work, voice, and validator cards",
        )
        return issues
    if card.get("draft_candidate") is not False:
        add("draft_flag_present", "draft_candidate", "Clear the draft candidate flag after review")
    if card.get("requires_human_distillation") is not False:
        add(
            "human_review_flag_present",
            "requires_human_distillation",
            "Confirm human review before promotion",
        )
    _require_text(card, "title", 8, add)
    _require_text(card, "summary", 40, add)
    _require_text(card, "reader_promise", 20, add)

    source = card.get("source")
    if not isinstance(source, dict):
        add("missing_source", "source", "Retain the source and evidence boundary")
    else:
        quality = str(source.get("evidence_quality") or "").lower()
        if quality not in {"strong", "medium"}:
            add(
                "evidence_quality_too_weak",
                "source.evidence_quality",
                "Formal cards require medium or strong reviewed evidence",
            )
        _require_nested_text(source, "scope", "source.scope", 12, add)
        _require_nested_text(source, "notes", "source.notes", 12, add)

    taxonomy = card.get("taxonomy")
    if not isinstance(taxonomy, dict):
        add("missing_taxonomy", "taxonomy", "Add routing taxonomy")
    else:
        required_taxonomy = {
            "work": WORK_REQUIRED_TAXONOMY,
            "voice": VOICE_REQUIRED_TAXONOMY,
            "validator": VALIDATOR_REQUIRED_TAXONOMY,
        }[card_type]
        for key in required_taxonomy:
            _require_string_list(taxonomy, key, f"taxonomy.{key}", 1, add)
        if card_type in {"voice", "validator"} and not any(
            _string_list(taxonomy.get(key))
            for key in ("platform", "audience", "genre", "subgenre")
        ):
            add(
                "missing_route_identity",
                "taxonomy",
                "High-risk cards require a platform, audience, genre, or subgenre route",
            )
    _require_string_list(card, "tags", "tags", 6 if card_type != "work" else 3, add)
    _require_string_list(card, "keywords", "keywords", 2, add)
    _require_string_list(card, "applicable_tasks", "applicable_tasks", 1, add)

    payload = card.get("type_payload")
    if not isinstance(payload, dict):
        add(
            "missing_type_payload",
            "type_payload",
            f"Add reviewed {card_type}-card mechanics",
        )
    elif card_type == "work":
        for key in WORK_REQUIRED_PAYLOAD:
            value = payload.get(key)
            field = f"type_payload.{key}"
            if isinstance(value, str):
                if len(value.strip()) < 12:
                    add("field_too_shallow", field, "Replace the draft placeholder with a concrete mechanism")
            elif isinstance(value, list):
                if not value or any(len(str(item).strip()) < 8 for item in value):
                    add("field_too_shallow", field, "Add concrete reviewed mechanism entries")
            else:
                add("missing_required_field", field, "This reviewed work-card field is required")
    elif card_type == "voice":
        for key in VOICE_REQUIRED_TEXT_PAYLOAD:
            _require_nested_text(
                payload,
                key,
                f"type_payload.{key}",
                12,
                add,
            )
        for key in VOICE_REQUIRED_LIST_PAYLOAD:
            _require_string_list(
                payload,
                key,
                f"type_payload.{key}",
                3 if key in {"anti_ai_failure_signatures", "do_not_copy"} else 1,
                add,
                min_item=6,
            )
    else:
        _require_nested_text(
            payload,
            "validator_name",
            "type_payload.validator_name",
            8,
            add,
        )
        for key in VALIDATOR_REQUIRED_LIST_PAYLOAD:
            _require_string_list(
                payload,
                key,
                f"type_payload.{key}",
                2 if key in {"checks", "reject_conditions"} else 1,
                add,
                min_item=8,
            )
        layer_id = str(payload.get("layer_id") or "").strip()
        if layer_id not in VALIDATOR_LAYER_IDS:
            add(
                "invalid_validator_layer",
                "type_payload.layer_id",
                "Validator cards require one known NLCL execution layer",
            )
        validator_name = str(payload.get("validator_name") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", validator_name):
            add(
                "invalid_validator_name",
                "type_payload.validator_name",
                "Validator names must use a stable snake_case identifier",
            )
        execution_rules = payload.get("execution_rules")
        if not isinstance(execution_rules, list) or not execution_rules:
            add(
                "missing_validator_execution_rules",
                "type_payload.execution_rules",
                "Add at least one bounded deterministic validator rule",
            )
        elif len(execution_rules) > 16:
            add(
                "validator_execution_rules_limit_exceeded",
                "type_payload.execution_rules",
                "Validator cards may define at most 16 execution rules",
            )
        else:
            parsed_rules: list[KnowledgeValidatorRule] = []
            for index, raw_rule in enumerate(execution_rules):
                try:
                    parsed_rules.append(
                        KnowledgeValidatorRule.model_validate_json(
                            json.dumps(raw_rule, ensure_ascii=False),
                            strict=True,
                        )
                    )
                except (TypeError, ValueError):
                    add(
                        "invalid_validator_execution_rule",
                        f"type_payload.execution_rules.{index}",
                        "Use the bounded deterministic validator rule contract",
                    )
            rule_ids = [item.rule_id for item in parsed_rules]
            if rule_ids != sorted(rule_ids) or len(rule_ids) != len(set(rule_ids)):
                add(
                    "invalid_validator_execution_rule_order",
                    "type_payload.execution_rules",
                    "Validator execution rules must be unique and sorted by rule_id",
                )
            signatures = [item.failure_signature for item in parsed_rules]
            if len(signatures) != len(set(signatures)):
                add(
                    "duplicate_validator_failure_signature",
                    "type_payload.execution_rules",
                    "Validator execution rules require unique failure signatures",
                )

    mechanics = card.get("mechanics")
    if not isinstance(mechanics, list) or not mechanics:
        add("missing_mechanics", "mechanics", "Add at least one transferable mechanism")
    else:
        for index, item in enumerate(mechanics):
            if not isinstance(item, dict):
                add("invalid_mechanic", f"mechanics.{index}", "Mechanics must be structured objects")
                continue
            for key in ("name", "use_when", "rule", "failure_mode"):
                if len(str(item.get(key) or "").strip()) < 8:
                    add("field_too_shallow", f"mechanics.{index}.{key}", "Add a concrete mechanism value")

    _require_string_list(card, "validation_tests", "validation_tests", 2, add, min_item=12)
    ladder = card.get("validation_ladder")
    if not isinstance(ladder, dict):
        add("missing_validation_ladder", "validation_ladder", "Record all four validation steps")
    else:
        for step in LADDER_STEPS:
            entry = ladder.get(step)
            if not isinstance(entry, dict):
                add("missing_validation_step", f"validation_ladder.{step}", "This validation step is required")
                continue
            if str(entry.get("status") or "").lower() != "pass":
                add("validation_not_passed", f"validation_ladder.{step}.status", "Promotion requires an evidenced pass")
            _require_string_list(entry, "evidence", f"validation_ladder.{step}.evidence", 1, add, min_item=8)

    reader_effect = card.get("reader_effect")
    if not isinstance(reader_effect, dict):
        add("missing_reader_effect", "reader_effect", "Record the observed reader effect")
    else:
        for key in (
            "author_intent",
            "observed_reader_effect",
            "effect_failure_boundary",
            "misread_risk",
            "when_it_turns_bad",
        ):
            _require_nested_text(reader_effect, key, f"reader_effect.{key}", 12, add)
        _require_string_list(reader_effect, "effect_evidence", "reader_effect.effect_evidence", 1, add, min_item=12)

    scores = card.get("scores")
    thresholds = {
        "evidence": (0.6, None),
        "specificity": (0.6, None),
        "generativity": (0.6, None),
        "retrieval": (0.5, None),
        "safety_risk": (None, 0.45),
    }
    if not isinstance(scores, dict):
        add("missing_scores", "scores", "Add reviewed evidence, utility and safety scores")
    else:
        for key, (minimum, maximum) in thresholds.items():
            value = scores.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                add("invalid_score", f"scores.{key}", "Score must be a number between 0 and 1")
                continue
            number = float(value)
            if not 0 <= number <= 1 or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
                add("score_gate_failed", f"scores.{key}", "Score does not meet the formal promotion gate")

    safety = card.get("safety")
    if not isinstance(safety, dict):
        add("missing_safety", "safety", "Retain an explicit anti-contamination boundary")
    else:
        _require_nested_text(safety, "copyright_boundary", "safety.copyright_boundary", 12, add)
        _require_string_list(safety, "do_not_reuse", "safety.do_not_reuse", 3, add)
        if card_type == "voice":
            _require_string_list(
                payload if isinstance(payload, dict) else {},
                "do_not_copy",
                "type_payload.do_not_copy",
                3,
                add,
                min_item=6,
            )

    route_hints = card.get("nlcl_route_hints")
    if not isinstance(route_hints, dict):
        add("missing_route_hints", "nlcl_route_hints", "Add NLCL routing hints")
    else:
        _require_string_list(route_hints, "recommended_profiles", "nlcl_route_hints.recommended_profiles", 1, add)
        _require_string_list(route_hints, "route_terms", "nlcl_route_hints.route_terms", 2, add)
        _require_string_list(route_hints, "negative_routes", "nlcl_route_hints.negative_routes", 1, add)
        feedback_layer = str(route_hints.get("feedback_default_layer") or "").strip()
        layers = set(_string_list(route_hints.get("nlcl_layers")))
        if card_type == "voice":
            if feedback_layer != "L5_DEAI_STYLE":
                add(
                    "invalid_feedback_layer",
                    "nlcl_route_hints.feedback_default_layer",
                    "Voice cards must route feedback to L5_DEAI_STYLE",
                )
            if not {"style", "deai"}.issubset(layers):
                add(
                    "missing_route_layer",
                    "nlcl_route_hints.nlcl_layers",
                    "Voice cards must declare style and deai NLCL layers",
                )
        elif card_type == "validator":
            expected_layer = (
                str(payload.get("layer_id") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            if feedback_layer != expected_layer:
                add(
                    "invalid_feedback_layer",
                    "nlcl_route_hints.feedback_default_layer",
                    "Validator feedback must return to its declared execution layer",
                )
            if "review" not in layers:
                add(
                    "missing_route_layer",
                    "nlcl_route_hints.nlcl_layers",
                    "Validator cards must declare the review NLCL layer",
                )

    placeholder_fields = {
        "summary": card.get("summary"),
        "reader_promise": card.get("reader_promise"),
        "type_payload": card.get("type_payload"),
        "mechanics": card.get("mechanics"),
        "reader_effect": card.get("reader_effect"),
        "validation_tests": card.get("validation_tests"),
        "validation_ladder": card.get("validation_ladder"),
    }
    for field, value in placeholder_fields.items():
        if PLACEHOLDER_PATTERN.search(canonical_json(value)):
            add("placeholder_remains", field, "Replace every pending or unvalidated placeholder")
    return sorted(issues, key=lambda item: (item.field, item.code))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _require_text(
    value: dict[str, Any],
    key: str,
    minimum: int,
    add: Any,
) -> None:
    if len(str(value.get(key) or "").strip()) < minimum:
        add("field_too_shallow", key, f"Provide at least {minimum} meaningful characters")


def _require_nested_text(
    value: dict[str, Any],
    key: str,
    field: str,
    minimum: int,
    add: Any,
) -> None:
    if len(str(value.get(key) or "").strip()) < minimum:
        add("field_too_shallow", field, f"Provide at least {minimum} meaningful characters")


def _require_string_list(
    value: dict[str, Any],
    key: str,
    field: str,
    minimum: int,
    add: Any,
    *,
    min_item: int = 2,
) -> None:
    items = value.get(key)
    if (
        not isinstance(items, list)
        or len(items) < minimum
        or any(len(str(item).strip()) < min_item for item in items)
    ):
        add("field_too_shallow", field, f"Provide at least {minimum} concrete entries")


__all__ = [
    "KnowledgeConflictError",
    "KnowledgeContextCard",
    "KnowledgeContextSelection",
    "KnowledgeInvalidSourceError",
    "KnowledgeNotFoundError",
    "KnowledgeReviewRequiredError",
    "KnowledgeService",
    "KnowledgeServiceError",
    "review_issues",
]
