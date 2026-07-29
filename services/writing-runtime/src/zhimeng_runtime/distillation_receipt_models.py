from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.distillation_anchor_models import DistillationAnchorBinding
from zhimeng_runtime.distillation_invocation_models import (
    DISTILLATION_ALLOWED_TOOL_NAMES,
    DISTILLATION_OUTPUT_TOOL_NAME,
    DISTILLATION_READ_TOOL_NAME,
    DISTILLATION_SEARCH_TOOL_NAME,
    DistillationCandidateSet,
    DistillationCandidateState,
    DistillationInstructionBinding,
    DistillationInvocationStatus,
    DistillationKnowledgeBinding,
    DistillationOutputSubmission,
    DistillationProjectBinding,
    DistillationProviderBinding,
    DistillationRecoveryAction,
    DistillationSourceBinding,
    distillation_output_submission_hash,
)
from zhimeng_runtime.execution_protocol import MessageUsage
from zhimeng_runtime.provider_turn_models import provider_model_identity_status


class DistillationReceiptKind(StrEnum):
    PREPARED = "prepared"
    PROVIDER_DISPATCH = "provider_dispatch"
    PROVIDER_TURN_RESULT = "provider_turn_result"
    CANDIDATE_OUTPUT = "candidate_output"
    RECOVERY_DOSSIER = "recovery_dossier"
    RECOVERY_DECISION = "recovery_decision"
    FINAL_RESULT = "final_result"


class DistillationReceiptModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class DistillationPreparedPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-prepared.v1"] = Field(
        default="zhimeng.distillation-prepared.v1",
        alias="schema",
    )
    source: DistillationSourceBinding
    project: DistillationProjectBinding
    knowledge: DistillationKnowledgeBinding
    instruction: DistillationInstructionBinding
    provider: DistillationProviderBinding
    anchor_catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    anchor_catalog_hash: Sha256
    anchor_count: int = Field(ge=1, le=100_000, strict=True)
    output_contract_hash: Sha256
    allowed_tools: tuple[str, ...]
    request_hash: Sha256
    retry_of: str | None = Field(
        default=None,
        pattern=r"^distillation-invocation-[0-9a-f]{32}$",
    )

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != DISTILLATION_ALLOWED_TOOL_NAMES:
            raise ValueError("deep distillation must expose only its three Runtime tools")
        return values


class DistillationProviderToolCall(DistillationReceiptModel):
    provider_tool_call_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    tool_name: Literal[
        "distillation_anchor_search",
        "distillation_anchor_read",
        "distillation_output_submit",
    ]
    arguments: dict[str, JsonValue]
    arguments_hash: Sha256

    @model_validator(mode="after")
    def validate_arguments(self) -> "DistillationProviderToolCall":
        if self.arguments_hash != canonical_json_hash(self.arguments):
            raise ValueError("distillation Provider ToolCall arguments hash does not match")
        if self.tool_name == DISTILLATION_OUTPUT_TOOL_NAME:
            DistillationOutputSubmission.model_validate_json(
                json.dumps(
                    self.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        return self


class DistillationToolResultBinding(DistillationReceiptModel):
    provider_tool_call_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    tool_name: Literal[
        "distillation_anchor_search",
        "distillation_anchor_read",
    ]
    result_hash: Sha256
    anchor_catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    anchor_catalog_hash: Sha256
    read_anchors: tuple[DistillationAnchorBinding, ...] = Field(max_length=8)
    binding_hash: Sha256

    @model_validator(mode="after")
    def validate_binding(self) -> "DistillationToolResultBinding":
        anchor_ids = tuple(anchor.anchor_id for anchor in self.read_anchors)
        if anchor_ids != tuple(sorted(set(anchor_ids))):
            raise ValueError("distillation ToolResult anchors must be sorted and unique")
        if self.tool_name == DISTILLATION_READ_TOOL_NAME and not self.read_anchors:
            raise ValueError("distillation anchor read result requires read evidence")
        if self.tool_name == DISTILLATION_SEARCH_TOOL_NAME and self.read_anchors:
            raise ValueError("distillation anchor search cannot claim read evidence")
        if self.binding_hash != distillation_tool_result_binding_hash(self):
            raise ValueError("distillation ToolResult binding hash does not match")
        return self


class DistillationProviderDispatchPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-provider-dispatch.v1"] = Field(
        default="zhimeng.distillation-provider-dispatch.v1",
        alias="schema",
    )
    turn_ordinal: int = Field(ge=1, le=32, strict=True)
    prepared_receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    prepared_receipt_hash: Sha256
    request_hash: Sha256
    provider_messages_hash: Sha256
    tool_definitions_hash: Sha256
    prior_turn_result_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    prior_turn_result_receipt_hash: Sha256 | None = None
    prior_tool_result: DistillationToolResultBinding | None = None
    provider_replay_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_dispatch_chain(self) -> "DistillationProviderDispatchPayload":
        result_pair = (
            self.prior_turn_result_receipt_id,
            self.prior_turn_result_receipt_hash,
        )
        if (result_pair[0] is None) != (result_pair[1] is None):
            raise ValueError("prior Provider turn result identity and hash must pair")
        if self.turn_ordinal == 1:
            if result_pair[0] is not None or self.prior_tool_result is not None:
                raise ValueError("first Provider dispatch cannot claim a prior turn")
        elif result_pair[0] is None or self.prior_tool_result is None:
            raise ValueError("later Provider dispatch requires prior turn and ToolResult evidence")
        return self


class DistillationProviderTurnResultPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-provider-turn-result.v1"] = Field(
        default="zhimeng.distillation-provider-turn-result.v1",
        alias="schema",
    )
    turn_ordinal: int = Field(ge=1, le=32, strict=True)
    dispatch_receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    dispatch_receipt_hash: Sha256
    requested_model: str | None = Field(default=None, min_length=1, max_length=512)
    reported_model: str | None = Field(default=None, min_length=1, max_length=512)
    accepted_reported_models: tuple[str, ...] = Field(default=(), max_length=64)
    model_identity_status: Literal[
        "exact_match",
        "accepted_alias",
        "unreported",
        "rejected_mismatch",
    ]
    assistant_text: str | None = Field(default=None, max_length=500_000)
    assistant_text_hash: Sha256 | None = None
    tool_calls: tuple[DistillationProviderToolCall, ...] = Field(
        min_length=1,
        max_length=1,
    )
    tool_calls_hash: Sha256
    finish_reason: Literal["tool_calls", "function_call"]
    usage: MessageUsage | None

    @field_validator("requested_model", "reported_model")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("distillation Provider model identity must not be blank")
        return normalized

    @field_validator("accepted_reported_models")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip() for value in values}))
        if any(not value for value in normalized) or values != normalized:
            raise ValueError("distillation Provider aliases must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_turn_result(self) -> "DistillationProviderTurnResultPayload":
        if (self.assistant_text is None) != (self.assistant_text_hash is None):
            raise ValueError("Provider assistant text and hash must pair")
        if self.assistant_text is not None:
            actual_hash = hashlib.sha256(self.assistant_text.encode("utf-8")).hexdigest()
            if self.assistant_text_hash != actual_hash:
                raise ValueError("Provider assistant text hash does not match")
        expected_identity = provider_model_identity_status(
            requested_model=self.requested_model,
            reported_model=self.reported_model,
            accepted_reported_models=self.accepted_reported_models,
        )
        if self.model_identity_status != expected_identity:
            raise ValueError("distillation Provider model identity status does not match")
        if self.tool_calls_hash != distillation_provider_tool_calls_hash(self.tool_calls):
            raise ValueError("distillation Provider ToolCall set hash does not match")
        tool_call = self.tool_calls[0]
        if (
            tool_call.tool_name == DISTILLATION_OUTPUT_TOOL_NAME
            and self.assistant_text is not None
            and self.assistant_text.strip()
        ):
            raise ValueError("distillation output submission cannot be mixed with Provider text")
        return self


class DistillationCandidateOutputPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-candidate-output.v1"] = Field(
        default="zhimeng.distillation-candidate-output.v1",
        alias="schema",
    )
    final_turn_result_receipt_id: str = Field(
        pattern=r"^distillation-receipt-[0-9a-f]{32}$"
    )
    final_turn_result_receipt_hash: Sha256
    provider_turn_count: int = Field(ge=1, le=32, strict=True)
    provider_turn_chain_hash: Sha256
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256
    instruction_materialization_id: str = Field(
        pattern=r"^distillation-instruction-materialization-[0-9a-f]{32}$"
    )
    instruction_materialization_hash: Sha256
    anchor_catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    anchor_catalog_hash: Sha256
    read_anchors: tuple[DistillationAnchorBinding, ...] = Field(min_length=1, max_length=128)
    read_anchors_hash: Sha256
    submission: DistillationOutputSubmission
    submission_hash: Sha256
    candidate_set: DistillationCandidateSet
    formal_draft_allowed: Literal[False] = False
    requires_author_review: Literal[True] = True

    @model_validator(mode="after")
    def validate_candidate_output(self) -> "DistillationCandidateOutputPayload":
        read_ids = tuple(anchor.anchor_id for anchor in self.read_anchors)
        if read_ids != tuple(sorted(set(read_ids))):
            raise ValueError("distillation read-anchor evidence must be sorted and unique")
        if self.read_anchors_hash != distillation_anchor_binding_set_hash(
            self.read_anchors
        ):
            raise ValueError("distillation read-anchor evidence hash does not match")
        if self.submission_hash != distillation_output_submission_hash(self.submission):
            raise ValueError("distillation output submission hash does not match")
        for submitted, candidate in zip(
            self.submission.cards,
            self.candidate_set.cards,
            strict=True,
        ):
            if submitted.card_type != candidate.card_type:
                raise ValueError("distillation submitted and Runtime candidate types differ")
            candidate_anchor_ids = tuple(anchor.anchor_id for anchor in candidate.anchors)
            if candidate_anchor_ids != submitted.anchor_ids:
                raise ValueError("distillation candidate anchors differ from submission")
            if not set(candidate_anchor_ids).issubset(read_ids):
                raise ValueError("distillation candidate references an unread anchor")
            provider_owned = {
                key: value
                for key, value in candidate.card.items()
                if key
                not in {
                    "card_id",
                    "card_type",
                    "draft_candidate",
                    "requires_author_review",
                }
            }
            if provider_owned != submitted.card:
                raise ValueError("Runtime candidate changed Provider-owned card content")
        return self


class DistillationRecoveryDossierPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-recovery-dossier.v1"] = Field(
        default="zhimeng.distillation-recovery-dossier.v1",
        alias="schema",
    )
    prepared_receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    prepared_receipt_hash: Sha256
    last_dispatch_receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    last_dispatch_receipt_hash: Sha256
    last_turn_result_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    last_turn_result_receipt_hash: Sha256 | None = None
    candidate_state: DistillationCandidateState
    candidate_output_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    candidate_output_receipt_hash: Sha256 | None = None
    remote_result: Literal["unknown"] = "unknown"
    provider_replay_count: Literal[0] = 0
    dossier_revision: Literal[1] = 1
    failure_signature: Sha256

    @model_validator(mode="after")
    def validate_dossier(self) -> "DistillationRecoveryDossierPayload":
        if (self.last_turn_result_receipt_id is None) != (
            self.last_turn_result_receipt_hash is None
        ):
            raise ValueError("last Provider turn result identity and hash must pair")
        if (self.candidate_output_receipt_id is None) != (
            self.candidate_output_receipt_hash is None
        ):
            raise ValueError("candidate output identity and hash must pair")
        has_candidate = self.candidate_output_receipt_id is not None
        if (self.candidate_state is DistillationCandidateState.PERSISTED) != has_candidate:
            raise ValueError("only persisted recovery state can expose candidate output")
        return self


class DistillationRecoveryDecisionPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-recovery-decision.v1"] = Field(
        default="zhimeng.distillation-recovery-decision.v1",
        alias="schema",
    )
    dossier_receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    dossier_receipt_hash: Sha256
    previous_dossier_revision: int = Field(ge=1, strict=True)
    new_dossier_revision: int = Field(ge=2, strict=True)
    action: DistillationRecoveryAction
    candidate_output_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    candidate_output_receipt_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> "DistillationRecoveryDecisionPayload":
        if self.new_dossier_revision != self.previous_dossier_revision + 1:
            raise ValueError("distillation recovery dossier revision must advance by one")
        if (self.candidate_output_receipt_id is None) != (
            self.candidate_output_receipt_hash is None
        ):
            raise ValueError("recovery decision candidate identity and hash must pair")
        has_candidate = self.candidate_output_receipt_id is not None
        if (
            self.action is DistillationRecoveryAction.ADOPT_PERSISTED_CANDIDATE
        ) != has_candidate:
            raise ValueError("only adopt recovery can reference persisted candidate output")
        return self


class DistillationFinalCandidateBinding(DistillationReceiptModel):
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    card_type: Literal["work", "voice", "validator"]
    content_hash: Sha256


class DistillationFinalResultPayload(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-final-result.v1"] = Field(
        default="zhimeng.distillation-final-result.v1",
        alias="schema",
    )
    outcome: Literal["succeeded", "failed", "cancelled", "abandoned"]
    provider_turn_count: int = Field(ge=0, le=32, strict=True)
    provider_turn_chain_hash: Sha256 | None = None
    candidate_output_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    candidate_output_receipt_hash: Sha256 | None = None
    candidates: tuple[DistillationFinalCandidateBinding, ...] = Field(max_length=3)
    candidate_set_hash: Sha256 | None = None
    work_count: int = Field(ge=0, le=1, strict=True)
    voice_count: int = Field(ge=0, le=1, strict=True)
    validator_count: int = Field(ge=0, le=1, strict=True)
    failure_signature: Sha256 | None = None
    provider_replay_count: Literal[0] = 0
    formal_knowledge_written: Literal[False] = False
    project_truth_written: Literal[False] = False
    project_kb_written: Literal[False] = False

    @model_validator(mode="after")
    def validate_final_result(self) -> "DistillationFinalResultPayload":
        if self.provider_turn_count == 0:
            if self.provider_turn_chain_hash is not None:
                raise ValueError("zero Provider turns cannot claim a chain hash")
        elif self.provider_turn_chain_hash is None:
            raise ValueError("Provider turns require a chain hash")
        if (self.candidate_output_receipt_id is None) != (
            self.candidate_output_receipt_hash is None
        ):
            raise ValueError("final candidate output identity and hash must pair")
        if self.outcome == "succeeded":
            if (
                self.provider_turn_count < 1
                or self.candidate_output_receipt_id is None
                or self.failure_signature is not None
                or len(self.candidates) != 3
                or self.candidate_set_hash is None
                or (self.work_count, self.voice_count, self.validator_count) != (1, 1, 1)
            ):
                raise ValueError("successful distillation final result lacks exact candidate evidence")
            observed = tuple(candidate.card_type for candidate in self.candidates)
            if observed != ("work", "voice", "validator"):
                raise ValueError("successful final candidates must use canonical type order")
        else:
            if self.candidates or self.candidate_set_hash is not None or any(
                (self.work_count, self.voice_count, self.validator_count)
            ):
                raise ValueError("non-success final result cannot claim knowledge candidates")
            if self.failure_signature is None:
                raise ValueError("non-success final result requires a failure signature")
            if self.outcome != "abandoned" and self.candidate_output_receipt_id is not None:
                raise ValueError("only abandoned recovery can reference discarded candidate output")
        return self


DistillationReceiptPayload: TypeAlias = (
    DistillationPreparedPayload
    | DistillationProviderDispatchPayload
    | DistillationProviderTurnResultPayload
    | DistillationCandidateOutputPayload
    | DistillationRecoveryDossierPayload
    | DistillationRecoveryDecisionPayload
    | DistillationFinalResultPayload
)


_PAYLOAD_KIND = {
    DistillationPreparedPayload: DistillationReceiptKind.PREPARED,
    DistillationProviderDispatchPayload: DistillationReceiptKind.PROVIDER_DISPATCH,
    DistillationProviderTurnResultPayload: DistillationReceiptKind.PROVIDER_TURN_RESULT,
    DistillationCandidateOutputPayload: DistillationReceiptKind.CANDIDATE_OUTPUT,
    DistillationRecoveryDossierPayload: DistillationReceiptKind.RECOVERY_DOSSIER,
    DistillationRecoveryDecisionPayload: DistillationReceiptKind.RECOVERY_DECISION,
    DistillationFinalResultPayload: DistillationReceiptKind.FINAL_RESULT,
}


class DistillationInvocationReceipt(DistillationReceiptModel):
    schema_id: Literal["zhimeng.distillation-invocation-receipt.v1"] = Field(
        default="zhimeng.distillation-invocation-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^distillation-receipt-[0-9a-f]{32}$")
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    ordinal: int = Field(ge=1, strict=True)
    kind: DistillationReceiptKind
    turn_ordinal: int | None = Field(default=None, ge=1, le=32, strict=True)
    parent_receipt_id: str | None = Field(
        default=None,
        pattern=r"^distillation-receipt-[0-9a-f]{32}$",
    )
    expected_invocation_revision: int = Field(ge=0, strict=True)
    resulting_invocation_revision: int = Field(ge=1, strict=True)
    resulting_status: DistillationInvocationStatus
    idempotency_key: str = Field(
        pattern=r"^distillation-receipt-idempotency-[0-9a-f]{32}$"
    )
    payload: DistillationReceiptPayload
    payload_hash: Sha256
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "DistillationInvocationReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("distillation receipt time must be timezone-aware")
        expected_kind = _PAYLOAD_KIND[type(self.payload)]
        if self.kind is not expected_kind:
            raise ValueError("distillation receipt kind does not match its typed payload")
        expected_turn = getattr(self.payload, "turn_ordinal", None)
        if self.turn_ordinal != expected_turn:
            raise ValueError("distillation receipt turn does not match its payload")
        if self.resulting_invocation_revision != self.expected_invocation_revision + 1:
            raise ValueError("distillation receipt CAS revision must advance by one")
        if self.ordinal != self.resulting_invocation_revision:
            raise ValueError("distillation receipt ordinal must equal resulting revision")
        if self.kind is DistillationReceiptKind.PREPARED:
            if (
                self.ordinal != 1
                or self.parent_receipt_id is not None
                or self.expected_invocation_revision != 0
            ):
                raise ValueError("prepared receipt must be the root CAS transition")
        elif self.parent_receipt_id is None:
            raise ValueError("non-root distillation receipts require a parent")
        expected_status = _resulting_status_for_payload(self.payload)
        if self.resulting_status is not expected_status:
            raise ValueError("distillation receipt resulting status does not match payload")
        if self.payload_hash != distillation_receipt_payload_hash(self.payload):
            raise ValueError("distillation receipt payload hash does not match")
        expected_id = distillation_receipt_id(
            invocation_id=self.invocation_id,
            ordinal=self.ordinal,
            kind=self.kind,
            payload_hash=self.payload_hash,
        )
        if self.receipt_id != expected_id:
            raise ValueError("distillation receipt identity does not match immutable material")
        expected_idempotency = distillation_receipt_idempotency_key(
            invocation_id=self.invocation_id,
            ordinal=self.ordinal,
            kind=self.kind,
            payload_hash=self.payload_hash,
        )
        if self.idempotency_key != expected_idempotency:
            raise ValueError("distillation receipt idempotency key does not match")
        if self.receipt_hash != distillation_receipt_hash(self):
            raise ValueError("distillation receipt hash does not match immutable material")
        return self


def _resulting_status_for_payload(
    payload: DistillationReceiptPayload,
) -> DistillationInvocationStatus:
    if isinstance(payload, DistillationPreparedPayload):
        return DistillationInvocationStatus.PREPARED
    if isinstance(
        payload,
        (DistillationProviderDispatchPayload, DistillationProviderTurnResultPayload),
    ):
        return DistillationInvocationStatus.RUNNING
    if isinstance(payload, DistillationCandidateOutputPayload):
        return DistillationInvocationStatus.OUTPUT_PERSISTED
    if isinstance(
        payload,
        (DistillationRecoveryDossierPayload, DistillationRecoveryDecisionPayload),
    ):
        return DistillationInvocationStatus.INTERRUPTED
    if isinstance(payload, DistillationFinalResultPayload):
        return DistillationInvocationStatus(payload.outcome)
    raise TypeError("unsupported distillation receipt payload")


def distillation_provider_tool_calls_hash(
    calls: tuple[DistillationProviderToolCall, ...],
) -> str:
    return canonical_json_hash(
        [call.model_dump(mode="json", by_alias=True) for call in calls]
    )


def distillation_tool_result_binding_hash(
    binding: DistillationToolResultBinding,
) -> str:
    return canonical_json_hash(
        binding.model_dump(mode="json", by_alias=True, exclude={"binding_hash"})
    )


def distillation_anchor_binding_set_hash(
    anchors: tuple[DistillationAnchorBinding, ...],
) -> str:
    return canonical_json_hash(
        [anchor.model_dump(mode="json") for anchor in anchors]
    )


def distillation_provider_turn_chain_hash(
    receipts: tuple[DistillationInvocationReceipt, ...],
) -> str:
    bindings = [
        {
            "turn_ordinal": receipt.turn_ordinal,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": receipt.receipt_hash,
        }
        for receipt in receipts
        if receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT
    ]
    return canonical_json_hash(bindings)


def distillation_receipt_payload_hash(payload: DistillationReceiptPayload) -> str:
    return canonical_json_hash(payload.model_dump(mode="json", by_alias=True))


def distillation_receipt_id(
    *,
    invocation_id: str,
    ordinal: int,
    kind: DistillationReceiptKind,
    payload_hash: str,
) -> str:
    digest = canonical_json_hash(
        {
            "schema": "zhimeng.distillation-invocation-receipt-identity.v1",
            "invocation_id": invocation_id,
            "ordinal": ordinal,
            "kind": kind.value,
            "payload_hash": payload_hash,
        }
    )
    return f"distillation-receipt-{digest[:32]}"


def distillation_receipt_idempotency_key(
    *,
    invocation_id: str,
    ordinal: int,
    kind: DistillationReceiptKind,
    payload_hash: str,
) -> str:
    digest = canonical_json_hash(
        {
            "schema": "zhimeng.distillation-receipt-idempotency.v1",
            "invocation_id": invocation_id,
            "ordinal": ordinal,
            "kind": kind.value,
            "payload_hash": payload_hash,
        }
    )
    return f"distillation-receipt-idempotency-{digest[:32]}"


def distillation_receipt_hash(receipt: DistillationInvocationReceipt) -> str:
    return canonical_json_hash(
        receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    )


def build_distillation_receipt(
    *,
    invocation_id: str,
    ordinal: int,
    parent_receipt_id: str | None,
    expected_invocation_revision: int,
    payload: DistillationReceiptPayload,
    created_at: datetime,
) -> DistillationInvocationReceipt:
    kind = _PAYLOAD_KIND[type(payload)]
    payload_hash = distillation_receipt_payload_hash(payload)
    material = {
        "receipt_id": distillation_receipt_id(
            invocation_id=invocation_id,
            ordinal=ordinal,
            kind=kind,
            payload_hash=payload_hash,
        ),
        "invocation_id": invocation_id,
        "ordinal": ordinal,
        "kind": kind,
        "turn_ordinal": getattr(payload, "turn_ordinal", None),
        "parent_receipt_id": parent_receipt_id,
        "expected_invocation_revision": expected_invocation_revision,
        "resulting_invocation_revision": expected_invocation_revision + 1,
        "resulting_status": _resulting_status_for_payload(payload),
        "idempotency_key": distillation_receipt_idempotency_key(
            invocation_id=invocation_id,
            ordinal=ordinal,
            kind=kind,
            payload_hash=payload_hash,
        ),
        "payload": payload,
        "payload_hash": payload_hash,
        "created_at": created_at,
    }
    draft = DistillationInvocationReceipt.model_construct(
        **material,
        receipt_hash="0" * 64,
    )
    return DistillationInvocationReceipt(
        **material,
        receipt_hash=distillation_receipt_hash(draft),
    )


def validate_distillation_receipt_chain(
    receipts: tuple[DistillationInvocationReceipt, ...],
) -> None:
    if not receipts:
        raise ValueError("distillation receipt chain must not be empty")
    invocation_id = receipts[0].invocation_id
    prepared = receipts[0]
    if prepared.kind is not DistillationReceiptKind.PREPARED:
        raise ValueError("distillation receipt chain must start with prepared")
    prepared_payload = prepared.payload
    assert isinstance(prepared_payload, DistillationPreparedPayload)

    result_receipts: list[DistillationInvocationReceipt] = []
    read_bindings: dict[str, DistillationAnchorBinding] = {}
    candidate_receipt: DistillationInvocationReceipt | None = None
    dossier_receipt: DistillationInvocationReceipt | None = None
    decision_receipt: DistillationInvocationReceipt | None = None
    final_seen = False

    for index, receipt in enumerate(receipts):
        if receipt.invocation_id != invocation_id:
            raise ValueError("distillation receipt chain crosses invocations")
        if receipt.ordinal != index + 1:
            raise ValueError("distillation receipt chain ordinals must be contiguous")
        if index:
            previous = receipts[index - 1]
            if receipt.parent_receipt_id != previous.receipt_id:
                raise ValueError("distillation receipt parent chain is broken")
            if receipt.expected_invocation_revision != previous.resulting_invocation_revision:
                raise ValueError("distillation receipt CAS chain is broken")
        if final_seen:
            raise ValueError("distillation receipt chain cannot continue after final result")

        if receipt.kind is DistillationReceiptKind.PROVIDER_DISPATCH:
            payload = receipt.payload
            assert isinstance(payload, DistillationProviderDispatchPayload)
            expected_turn = len(result_receipts) + 1
            if payload.turn_ordinal != expected_turn:
                raise ValueError("distillation Provider dispatch turn is not contiguous")
            if payload.prepared_receipt_id != prepared.receipt_id or (
                payload.prepared_receipt_hash != prepared.receipt_hash
            ):
                raise ValueError("distillation Provider dispatch does not bind prepared evidence")
            if payload.tool_definitions_hash != prepared_payload.provider.tool_definitions_hash:
                raise ValueError("distillation Provider tool definitions changed after prepare")
            if expected_turn > 1:
                prior = result_receipts[-1]
                if (
                    payload.prior_turn_result_receipt_id != prior.receipt_id
                    or payload.prior_turn_result_receipt_hash != prior.receipt_hash
                ):
                    raise ValueError("distillation Provider dispatch does not bind prior result")
                prior_payload = prior.payload
                assert isinstance(prior_payload, DistillationProviderTurnResultPayload)
                tool_result = payload.prior_tool_result
                assert tool_result is not None
                prior_call = prior_payload.tool_calls[0]
                if (
                    prior_call.tool_name == DISTILLATION_OUTPUT_TOOL_NAME
                    or tool_result.provider_tool_call_id != prior_call.provider_tool_call_id
                    or tool_result.tool_name != prior_call.tool_name
                    or tool_result.anchor_catalog_id != prepared_payload.anchor_catalog_id
                    or tool_result.anchor_catalog_hash != prepared_payload.anchor_catalog_hash
                ):
                    raise ValueError("distillation ToolResult does not bind prior Provider call")
                for anchor in tool_result.read_anchors:
                    existing = read_bindings.get(anchor.anchor_id)
                    if existing is not None and existing != anchor:
                        raise ValueError("distillation read anchor identity changed within invocation")
                    read_bindings[anchor.anchor_id] = anchor
        elif receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT:
            payload = receipt.payload
            assert isinstance(payload, DistillationProviderTurnResultPayload)
            if index == 0 or receipts[index - 1].kind is not DistillationReceiptKind.PROVIDER_DISPATCH:
                raise ValueError("distillation Provider result must immediately follow dispatch")
            dispatch = receipts[index - 1]
            dispatch_payload = dispatch.payload
            assert isinstance(dispatch_payload, DistillationProviderDispatchPayload)
            if (
                payload.turn_ordinal != dispatch_payload.turn_ordinal
                or payload.dispatch_receipt_id != dispatch.receipt_id
                or payload.dispatch_receipt_hash != dispatch.receipt_hash
            ):
                raise ValueError("distillation Provider result does not bind its dispatch")
            result_receipts.append(receipt)
        elif receipt.kind is DistillationReceiptKind.CANDIDATE_OUTPUT:
            payload = receipt.payload
            assert isinstance(payload, DistillationCandidateOutputPayload)
            if index == 0 or receipts[index - 1].kind is not DistillationReceiptKind.PROVIDER_TURN_RESULT:
                raise ValueError("distillation candidate output must follow final Provider result")
            final_turn = receipts[index - 1]
            final_turn_payload = final_turn.payload
            assert isinstance(final_turn_payload, DistillationProviderTurnResultPayload)
            if final_turn_payload.tool_calls[0].tool_name != DISTILLATION_OUTPUT_TOOL_NAME:
                raise ValueError("distillation candidate output requires final submit ToolCall")
            if (
                payload.final_turn_result_receipt_id != final_turn.receipt_id
                or payload.final_turn_result_receipt_hash != final_turn.receipt_hash
                or payload.provider_turn_count != len(result_receipts)
                or payload.provider_turn_chain_hash
                != distillation_provider_turn_chain_hash(tuple(result_receipts))
                or payload.source_receipt_id != prepared_payload.source.source_receipt_id
                or payload.source_receipt_hash != prepared_payload.source.source_receipt_hash
                or payload.instruction_materialization_id
                != prepared_payload.instruction.instruction_materialization_id
                or payload.instruction_materialization_hash
                != prepared_payload.instruction.instruction_materialization_hash
                or payload.anchor_catalog_id != prepared_payload.anchor_catalog_id
                or payload.anchor_catalog_hash != prepared_payload.anchor_catalog_hash
            ):
                raise ValueError("distillation candidate output does not bind frozen evidence")
            expected_read = tuple(sorted(read_bindings.values(), key=lambda item: item.anchor_id))
            if payload.read_anchors != expected_read:
                raise ValueError("distillation candidate output read evidence is incomplete")
            if payload.candidate_set.invocation_id != invocation_id:
                raise ValueError("distillation candidate set belongs to another invocation")
            candidate_receipt = receipt
        elif receipt.kind is DistillationReceiptKind.RECOVERY_DOSSIER:
            payload = receipt.payload
            assert isinstance(payload, DistillationRecoveryDossierPayload)
            if payload.prepared_receipt_id != prepared.receipt_id or (
                payload.prepared_receipt_hash != prepared.receipt_hash
            ):
                raise ValueError("distillation recovery dossier lost prepared evidence")
            if payload.candidate_state is DistillationCandidateState.PERSISTED:
                if candidate_receipt is None or (
                    payload.candidate_output_receipt_id != candidate_receipt.receipt_id
                    or payload.candidate_output_receipt_hash != candidate_receipt.receipt_hash
                ):
                    raise ValueError("distillation recovery dossier lost candidate cutpoint")
            dossier_receipt = receipt
        elif receipt.kind is DistillationReceiptKind.RECOVERY_DECISION:
            payload = receipt.payload
            assert isinstance(payload, DistillationRecoveryDecisionPayload)
            if dossier_receipt is None or receipts[index - 1] is not dossier_receipt:
                raise ValueError("distillation recovery decision must follow its dossier")
            if (
                payload.dossier_receipt_id != dossier_receipt.receipt_id
                or payload.dossier_receipt_hash != dossier_receipt.receipt_hash
            ):
                raise ValueError("distillation recovery decision does not bind its dossier")
            decision_receipt = receipt
        elif receipt.kind is DistillationReceiptKind.FINAL_RESULT:
            payload = receipt.payload
            assert isinstance(payload, DistillationFinalResultPayload)
            if payload.outcome == "succeeded":
                if candidate_receipt is None:
                    raise ValueError("successful distillation final result has no candidate cutpoint")
                if decision_receipt is None:
                    if receipts[index - 1] is not candidate_receipt:
                        raise ValueError("normal success must immediately finalize candidate output")
                else:
                    decision_payload = decision_receipt.payload
                    assert isinstance(decision_payload, DistillationRecoveryDecisionPayload)
                    if (
                        receipts[index - 1] is not decision_receipt
                        or decision_payload.action
                        is not DistillationRecoveryAction.ADOPT_PERSISTED_CANDIDATE
                    ):
                        raise ValueError("recovered success requires adopt decision")
                if (
                    payload.candidate_output_receipt_id != candidate_receipt.receipt_id
                    or payload.candidate_output_receipt_hash != candidate_receipt.receipt_hash
                ):
                    raise ValueError("successful final result does not bind candidate output")
            elif payload.outcome == "abandoned":
                if decision_receipt is None or receipts[index - 1] is not decision_receipt:
                    raise ValueError("abandoned final result requires a recovery decision")
                decision_payload = decision_receipt.payload
                assert isinstance(decision_payload, DistillationRecoveryDecisionPayload)
                if decision_payload.action is not DistillationRecoveryAction.ABANDON_AND_RECOMPILE:
                    raise ValueError("abandoned final result requires abandon decision")
            final_seen = True


__all__ = [
    "DistillationCandidateOutputPayload",
    "DistillationFinalCandidateBinding",
    "DistillationFinalResultPayload",
    "DistillationInvocationReceipt",
    "DistillationPreparedPayload",
    "DistillationProviderDispatchPayload",
    "DistillationProviderToolCall",
    "DistillationProviderTurnResultPayload",
    "DistillationReceiptKind",
    "DistillationRecoveryDecisionPayload",
    "DistillationRecoveryDossierPayload",
    "DistillationToolResultBinding",
    "build_distillation_receipt",
    "distillation_anchor_binding_set_hash",
    "distillation_provider_tool_calls_hash",
    "distillation_provider_turn_chain_hash",
    "distillation_receipt_hash",
    "distillation_receipt_id",
    "distillation_receipt_idempotency_key",
    "distillation_receipt_payload_hash",
    "distillation_tool_result_binding_hash",
    "validate_distillation_receipt_chain",
]
