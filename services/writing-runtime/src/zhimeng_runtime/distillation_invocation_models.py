from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.distillation_anchor_models import DistillationAnchorBinding
from zhimeng_runtime.execution_protocol import CapabilityProfile, ExecutorIdentity


DISTILLATION_INSTRUCTION_SKILL_ID = "novel-distillation.deep-instruction"
DISTILLATION_OUTPUT_TOOL_NAME = "distillation_output_submit"
DISTILLATION_SEARCH_TOOL_NAME = "distillation_anchor_search"
DISTILLATION_READ_TOOL_NAME = "distillation_anchor_read"
DISTILLATION_ALLOWED_TOOL_NAMES = (
    DISTILLATION_SEARCH_TOOL_NAME,
    DISTILLATION_READ_TOOL_NAME,
    DISTILLATION_OUTPUT_TOOL_NAME,
)
DISTILLATION_CARD_TYPE_ORDER = ("work", "voice", "validator")


class DistillationInvocationStatus(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    OUTPUT_PERSISTED = "output_persisted"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class DistillationCardType(StrEnum):
    WORK = "work"
    VOICE = "voice"
    VALIDATOR = "validator"


class DistillationCandidateState(StrEnum):
    ABSENT = "absent"
    PERSISTED = "persisted"
    INVALID = "invalid"


class DistillationRecoveryAction(StrEnum):
    ADOPT_PERSISTED_CANDIDATE = "adopt_persisted_candidate"
    ABANDON_AND_RECOMPILE = "abandon_and_recompile"


class DistillationInvocationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class DistillationSourceBinding(DistillationInvocationModel):
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256
    source_packet_fingerprint: Sha256
    packet_text_hash: Sha256


class DistillationProjectBinding(DistillationInvocationModel):
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256


class DistillationKnowledgeBinding(DistillationInvocationModel):
    scope_id: str = Field(min_length=1, max_length=160)
    scope_state_revision: int = Field(ge=1, strict=True)
    knowledge_fingerprint: Sha256
    project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
    )
    project_kb_manifest_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_project_kb_pair(self) -> "DistillationKnowledgeBinding":
        if (self.project_kb_state_id is None) != (
            self.project_kb_manifest_hash is None
        ):
            raise ValueError("distillation project-KB identity and hash must pair")
        return self


class DistillationInstructionBinding(DistillationInvocationModel):
    skill_id: Literal["novel-distillation.deep-instruction"]
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    manifest_fingerprint: Sha256
    package_fingerprint: Sha256
    activation_receipt_id: str = Field(pattern=r"^skill-activation-[0-9a-f]{32}$")
    activation_receipt_hash: Sha256
    activation_state_revision: int = Field(ge=1, strict=True)
    effective_scope: Literal["application", "project"]
    instruction_materialization_id: str = Field(
        pattern=r"^distillation-instruction-materialization-[0-9a-f]{32}$"
    )
    instruction_materialization_hash: Sha256
    instruction_hash: Sha256


class DistillationProviderBinding(DistillationInvocationModel):
    executor: ExecutorIdentity
    capabilities: CapabilityProfile
    requested_model: str | None = Field(default=None, min_length=1, max_length=512)
    accepted_reported_models: tuple[str, ...] = Field(default=(), max_length=64)
    dispatch_config_fingerprint: Sha256
    credential_reference: str | None = Field(default=None, min_length=1, max_length=256)
    tool_definitions_hash: Sha256

    @field_validator("requested_model")
    @classmethod
    def normalize_requested_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("distillation Provider model must not be blank")
        return normalized

    @field_validator("accepted_reported_models")
    @classmethod
    def validate_model_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip() for value in values}))
        if any(not value for value in normalized) or values != normalized:
            raise ValueError("distillation Provider aliases must be sorted and unique")
        return values

    @field_validator("credential_reference")
    @classmethod
    def validate_credential_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("credential reference must be a compact non-secret identifier")
        lowered = normalized.casefold()
        secret_prefixes = ("sk-", "bearer:", "bearer-", "api_key=", "apikey=")
        if lowered.startswith(secret_prefixes):
            raise ValueError("credential reference must not contain a Provider secret")
        return normalized


class DistillationSubmittedCard(DistillationInvocationModel):
    card_type: DistillationCardType
    card: dict[str, JsonValue]
    anchor_ids: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("card")
    @classmethod
    def validate_provider_card(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if not value:
            raise ValueError("distillation Provider card must not be empty")
        reserved = {
            "card_id",
            "card_type",
            "candidate_id",
            "origin",
            "origin_hash",
            "runtime_distillation_invocation_id",
            "status",
            "draft_candidate",
            "requires_author_review",
        }
        conflict = reserved.intersection(value)
        if conflict:
            raise ValueError(
                "distillation Provider card contains Runtime-owned fields: "
                + ", ".join(sorted(conflict))
            )
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise ValueError("distillation Provider card exceeds the payload budget")
        return value

    @field_validator("anchor_ids")
    @classmethod
    def validate_anchor_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value.startswith("distillation-anchor-")
            or len(value) != len("distillation-anchor-") + 32
            for value in values
        ):
            raise ValueError("distillation Provider card contains an invalid anchor identity")
        if values != tuple(sorted(set(values))):
            raise ValueError("distillation Provider card anchors must be sorted and unique")
        return values


class DistillationOutputSubmission(DistillationInvocationModel):
    schema_id: Literal["zhimeng.distillation-output-submit.v1"] = Field(
        default="zhimeng.distillation-output-submit.v1",
        alias="schema",
    )
    cards: tuple[DistillationSubmittedCard, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_exact_card_set(self) -> "DistillationOutputSubmission":
        observed = tuple(card.card_type.value for card in self.cards)
        if observed != DISTILLATION_CARD_TYPE_ORDER:
            raise ValueError(
                "distillation output must contain work, voice and validator exactly once "
                "in canonical order"
            )
        return self


class DistillationCandidateCard(DistillationInvocationModel):
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    card_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    )
    output_index: int = Field(ge=0, le=2, strict=True)
    card_type: DistillationCardType
    card: dict[str, JsonValue]
    card_hash: Sha256
    anchors: tuple[DistillationAnchorBinding, ...] = Field(min_length=1, max_length=16)
    status: Literal["pending"] = "pending"
    requires_author_review: Literal[True] = True

    @model_validator(mode="after")
    def validate_candidate(self) -> "DistillationCandidateCard":
        expected_type = DISTILLATION_CARD_TYPE_ORDER[self.output_index]
        if self.card_type.value != expected_type:
            raise ValueError("distillation candidate type does not match canonical index")
        if (
            self.card.get("card_id") != self.card_id
            or self.card.get("card_type") != self.card_type.value
            or self.card.get("draft_candidate") is not True
            or self.card.get("requires_author_review") is not True
        ):
            raise ValueError("distillation candidate card lacks Runtime-owned review fields")
        if self.card_hash != canonical_json_hash(self.card):
            raise ValueError("distillation candidate card hash does not match content")
        anchor_ids = tuple(anchor.anchor_id for anchor in self.anchors)
        if anchor_ids != tuple(sorted(set(anchor_ids))):
            raise ValueError("distillation candidate anchors must be sorted and unique")
        return self


class DistillationCandidateSet(DistillationInvocationModel):
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    cards: tuple[DistillationCandidateCard, ...] = Field(min_length=3, max_length=3)
    candidate_set_hash: Sha256

    @model_validator(mode="after")
    def validate_candidate_set(self) -> "DistillationCandidateSet":
        observed = tuple(card.card_type.value for card in self.cards)
        if observed != DISTILLATION_CARD_TYPE_ORDER:
            raise ValueError("distillation candidate set must contain exactly three card types")
        identities = tuple(card.candidate_id for card in self.cards)
        if len(identities) != len(set(identities)):
            raise ValueError("distillation candidate identities must be unique")
        if self.candidate_set_hash != distillation_candidate_set_hash(self.cards):
            raise ValueError("distillation candidate set hash does not match candidates")
        return self


class DistillationInvocationCreateRequest(DistillationInvocationModel):
    expected_source_receipt_hash: Sha256
    expected_project_revision: int = Field(ge=1, strict=True)
    expected_project_fingerprint: Sha256
    expected_scope_state_revision: int = Field(ge=1, strict=True)
    expected_knowledge_fingerprint: Sha256
    expected_project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
    )
    expected_project_kb_manifest_hash: Sha256 | None = None
    expected_activation_state_revision: int = Field(ge=1, strict=True)
    expected_activation_receipt_hash: Sha256
    idempotency_key: str = Field(pattern=r"^distillation-idempotency-[0-9a-f]{32}$")

    @model_validator(mode="after")
    def validate_project_kb_pair(self) -> "DistillationInvocationCreateRequest":
        if (self.expected_project_kb_state_id is None) != (
            self.expected_project_kb_manifest_hash is None
        ):
            raise ValueError("expected project-KB identity and hash must pair")
        return self


class DistillationInvocationCasRequest(DistillationInvocationModel):
    expected_invocation_revision: int = Field(ge=1, strict=True)
    expected_last_receipt_hash: Sha256


class DistillationInvocationRetryRequest(DistillationInvocationCasRequest):
    idempotency_key: str = Field(pattern=r"^distillation-idempotency-[0-9a-f]{32}$")


class DistillationRecoveryDecisionRequest(DistillationInvocationCasRequest):
    expected_dossier_revision: int = Field(ge=1, strict=True)
    expected_dossier_hash: Sha256
    action: DistillationRecoveryAction


class DistillationInvocation(DistillationInvocationModel):
    schema_id: Literal["zhimeng.distillation-invocation.v1"] = Field(
        default="zhimeng.distillation-invocation.v1",
        alias="schema",
    )
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    source: DistillationSourceBinding
    project: DistillationProjectBinding
    knowledge: DistillationKnowledgeBinding
    instruction: DistillationInstructionBinding
    provider: DistillationProviderBinding
    anchor_catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    anchor_catalog_hash: Sha256
    retry_of: str | None = Field(
        default=None,
        pattern=r"^distillation-invocation-[0-9a-f]{32}$",
    )
    request_hash: Sha256
    idempotency_key: str = Field(pattern=r"^distillation-idempotency-[0-9a-f]{32}$")
    status: DistillationInvocationStatus
    state_revision: int = Field(ge=1, strict=True)
    provider_replay_count: Literal[0] = 0
    last_receipt_ordinal: int = Field(ge=1, strict=True)
    last_receipt_hash: Sha256
    last_dispatch_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    last_dispatch_receipt_hash: Sha256 | None = None
    last_dispatch_turn_ordinal: int | None = Field(default=None, ge=1, strict=True)
    last_turn_result_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    last_turn_result_receipt_hash: Sha256 | None = None
    provider_turn_count: int = Field(default=0, ge=0, le=32, strict=True)
    provider_turn_chain_hash: Sha256 | None = None
    candidate_output_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    candidate_output_receipt_hash: Sha256 | None = None
    recovery_dossier_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    recovery_dossier_revision: int | None = Field(default=None, ge=1, strict=True)
    recovery_dossier_receipt_hash: Sha256 | None = None
    recovery_decision_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    recovery_decision_receipt_hash: Sha256 | None = None
    final_result_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    final_result_receipt_hash: Sha256 | None = None
    candidate_set_hash: Sha256 | None = None
    work_count: int | None = Field(default=None, ge=0, le=1, strict=True)
    voice_count: int | None = Field(default=None, ge=0, le=1, strict=True)
    validator_count: int | None = Field(default=None, ge=0, le=1, strict=True)
    created_at: datetime
    updated_at: datetime
    head_hash: Sha256

    @model_validator(mode="after")
    def validate_head(self) -> "DistillationInvocation":
        if (
            self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise ValueError("distillation invocation times must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("distillation invocation update cannot precede creation")
        if self.state_revision != self.last_receipt_ordinal:
            raise ValueError("distillation invocation revision must match receipt ordinal")
        _require_pair(
            self.last_dispatch_receipt_id,
            self.last_dispatch_receipt_hash,
            label="last dispatch receipt",
        )
        if (self.last_dispatch_receipt_id is None) != (
            self.last_dispatch_turn_ordinal is None
        ):
            raise ValueError("last dispatch receipt and turn ordinal must pair")
        _require_pair(
            self.last_turn_result_receipt_id,
            self.last_turn_result_receipt_hash,
            label="last turn result receipt",
        )
        if self.provider_turn_count == 0:
            if (
                self.last_turn_result_receipt_id is not None
                or self.provider_turn_chain_hash is not None
            ):
                raise ValueError("zero Provider turns cannot claim result evidence")
        elif (
            self.last_turn_result_receipt_id is None
            or self.provider_turn_chain_hash is None
        ):
            raise ValueError("Provider turn count requires result-chain evidence")
        if self.last_dispatch_turn_ordinal is not None and self.last_dispatch_turn_ordinal not in {
            self.provider_turn_count,
            self.provider_turn_count + 1,
        }:
            raise ValueError("last Provider dispatch turn is inconsistent with result count")
        _require_pair(
            self.candidate_output_receipt_id,
            self.candidate_output_receipt_hash,
            label="candidate output receipt",
        )
        dossier_values = (
            self.recovery_dossier_receipt_id,
            self.recovery_dossier_revision,
            self.recovery_dossier_receipt_hash,
        )
        if any(value is None for value in dossier_values) != all(
            value is None for value in dossier_values
        ):
            raise ValueError("recovery dossier identity, revision and hash must pair")
        _require_pair(
            self.recovery_decision_receipt_id,
            self.recovery_decision_receipt_hash,
            label="recovery decision receipt",
        )
        _require_pair(
            self.final_result_receipt_id,
            self.final_result_receipt_hash,
            label="final result receipt",
        )
        count_values = (
            self.candidate_set_hash,
            self.work_count,
            self.voice_count,
            self.validator_count,
        )
        if any(value is None for value in count_values) != all(
            value is None for value in count_values
        ):
            raise ValueError("candidate set hash and type counts must appear together")
        self._validate_status_cutpoints()
        expected_id = distillation_invocation_id(
            source_receipt_id=self.source.source_receipt_id,
            project_id=self.project.project_id,
            idempotency_key=self.idempotency_key,
        )
        if self.invocation_id != expected_id:
            raise ValueError("distillation invocation identity does not match request")
        if self.head_hash != distillation_invocation_head_hash(self):
            raise ValueError("distillation invocation head hash does not match")
        return self

    def _validate_status_cutpoints(self) -> None:
        has_dispatch = self.last_dispatch_receipt_id is not None
        has_result = self.last_turn_result_receipt_id is not None
        has_candidate = self.candidate_output_receipt_id is not None
        has_dossier = self.recovery_dossier_receipt_id is not None
        has_decision = self.recovery_decision_receipt_id is not None
        has_final = self.final_result_receipt_id is not None

        if self.status is DistillationInvocationStatus.PREPARED:
            if any((has_dispatch, has_result, has_candidate, has_dossier, has_decision, has_final)):
                raise ValueError("prepared distillation invocation cannot claim later cutpoints")
        elif self.status is DistillationInvocationStatus.RUNNING:
            if not has_dispatch or any((has_candidate, has_dossier, has_decision, has_final)):
                raise ValueError("running distillation invocation has invalid cutpoints")
        elif self.status is DistillationInvocationStatus.OUTPUT_PERSISTED:
            if not (has_dispatch and has_result and has_candidate) or any(
                (has_dossier, has_decision, has_final)
            ):
                raise ValueError("output-persisted invocation requires only candidate cutpoint")
        elif self.status is DistillationInvocationStatus.INTERRUPTED:
            if not has_dispatch or not has_dossier or has_final:
                raise ValueError("interrupted invocation requires dispatch and recovery dossier")
            if has_decision and self.recovery_dossier_revision is None:
                raise ValueError("recovery decision requires a dossier revision")
        elif self.status is DistillationInvocationStatus.SUCCEEDED:
            if not (has_dispatch and has_result and has_candidate and has_final):
                raise ValueError("successful invocation requires complete Provider and candidate evidence")
            if (self.work_count, self.voice_count, self.validator_count) != (1, 1, 1):
                raise ValueError("successful invocation requires exactly one card of each type")
        elif self.status in {
            DistillationInvocationStatus.FAILED,
            DistillationInvocationStatus.CANCELLED,
        }:
            if not has_final or has_candidate:
                raise ValueError("failed or cancelled invocation requires a candidate-free final result")
        elif self.status is DistillationInvocationStatus.ABANDONED:
            if not (has_dispatch and has_dossier and has_decision and has_final):
                raise ValueError("abandoned invocation requires a recovery decision and final result")


def _require_pair(first: Any, second: Any, *, label: str) -> None:
    if (first is None) != (second is None):
        raise ValueError(f"{label} identity and hash must pair")


def distillation_output_submission_hash(submission: DistillationOutputSubmission) -> str:
    return canonical_json_hash(submission.model_dump(mode="json", by_alias=True))


def distillation_candidate_set_hash(
    cards: tuple[DistillationCandidateCard, ...],
) -> str:
    return canonical_json_hash(
        [
            {
                "candidate_id": card.candidate_id,
                "card_id": card.card_id,
                "card_type": card.card_type.value,
                "card_hash": card.card_hash,
                "anchor_ids": [anchor.anchor_id for anchor in card.anchors],
            }
            for card in cards
        ]
    )


def distillation_invocation_id(
    *,
    source_receipt_id: str,
    project_id: str,
    idempotency_key: str,
) -> str:
    digest = canonical_json_hash(
        {
            "schema": "zhimeng.distillation-invocation-identity.v1",
            "source_receipt_id": source_receipt_id,
            "project_id": project_id,
            "idempotency_key": idempotency_key,
        }
    )
    return f"distillation-invocation-{digest[:32]}"


def distillation_invocation_request_hash(
    *,
    source: DistillationSourceBinding,
    project: DistillationProjectBinding,
    knowledge: DistillationKnowledgeBinding,
    instruction: DistillationInstructionBinding,
    provider: DistillationProviderBinding,
    anchor_catalog_hash: str,
    retry_of: str | None,
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.distillation-invocation-request.v1",
            "source": source.model_dump(mode="json"),
            "project": project.model_dump(mode="json"),
            "knowledge": knowledge.model_dump(mode="json"),
            "instruction": instruction.model_dump(mode="json"),
            "provider": provider.model_dump(mode="json"),
            "anchor_catalog_hash": anchor_catalog_hash,
            "retry_of": retry_of,
        }
    )


def distillation_invocation_head_hash(invocation: DistillationInvocation) -> str:
    return canonical_json_hash(
        invocation.model_dump(mode="json", by_alias=True, exclude={"head_hash"})
    )


__all__ = [
    "DISTILLATION_ALLOWED_TOOL_NAMES",
    "DISTILLATION_CARD_TYPE_ORDER",
    "DISTILLATION_INSTRUCTION_SKILL_ID",
    "DISTILLATION_OUTPUT_TOOL_NAME",
    "DISTILLATION_READ_TOOL_NAME",
    "DISTILLATION_SEARCH_TOOL_NAME",
    "DistillationCandidateCard",
    "DistillationCandidateSet",
    "DistillationCandidateState",
    "DistillationCardType",
    "DistillationInstructionBinding",
    "DistillationInvocation",
    "DistillationInvocationCasRequest",
    "DistillationInvocationCreateRequest",
    "DistillationInvocationRetryRequest",
    "DistillationInvocationStatus",
    "DistillationKnowledgeBinding",
    "DistillationOutputSubmission",
    "DistillationProjectBinding",
    "DistillationProviderBinding",
    "DistillationRecoveryAction",
    "DistillationRecoveryDecisionRequest",
    "DistillationSourceBinding",
    "DistillationSubmittedCard",
    "distillation_candidate_set_hash",
    "distillation_invocation_head_hash",
    "distillation_invocation_id",
    "distillation_invocation_request_hash",
    "distillation_output_submission_hash",
]
