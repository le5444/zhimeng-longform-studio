from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from zhimeng_runtime.agent_provider import (
    AgentProvider,
    AgentProviderCancelled,
    AgentProviderError,
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.distillation_anchor_models import (
    DistillationAnchorBinding,
    DistillationAnchorCatalog,
    DistillationAnchorReadRequest,
    DistillationAnchorSearchMatch,
    DistillationAnchorSearchRequest,
    DistillationAnchorSearchResult,
    build_distillation_anchor_read_result,
    distillation_anchor_binding,
    distillation_anchor_read_result_hash,
    distillation_anchor_search_request_hash,
    distillation_anchor_search_result_hash,
)
from zhimeng_runtime.distillation_invocation_models import (
    DISTILLATION_OUTPUT_TOOL_NAME,
    DISTILLATION_READ_TOOL_NAME,
    DISTILLATION_SEARCH_TOOL_NAME,
    DistillationCandidateCard,
    DistillationCandidateSet,
    DistillationCandidateState,
    DistillationCardType,
    DistillationInvocationStatus,
    DistillationOutputSubmission,
    distillation_candidate_set_hash,
    distillation_output_submission_hash,
)
from zhimeng_runtime.distillation_invocation_store import (
    DistillationInvocationSnapshot,
    DistillationInvocationStore,
)
from zhimeng_runtime.distillation_receipt_models import (
    DistillationCandidateOutputPayload,
    DistillationFinalCandidateBinding,
    DistillationFinalResultPayload,
    DistillationInvocationReceipt,
    DistillationPreparedPayload,
    DistillationProviderDispatchPayload,
    DistillationProviderToolCall,
    DistillationProviderTurnResultPayload,
    DistillationReceiptKind,
    DistillationRecoveryDossierPayload,
    DistillationToolResultBinding,
    build_distillation_receipt,
    distillation_anchor_binding_set_hash,
    distillation_provider_tool_calls_hash,
    distillation_provider_turn_chain_hash,
    distillation_tool_result_binding_hash,
)
from zhimeng_runtime.provider_turn_models import provider_model_identity_status


MAX_DISTILLATION_PROVIDER_TURNS = 32


class DistillationInvocationServiceError(RuntimeError):
    pass


class DistillationInvocationServiceConflict(DistillationInvocationServiceError):
    pass


class DistillationInvocationServiceUnavailable(DistillationInvocationServiceError):
    pass


@dataclass(frozen=True, slots=True)
class DistillationRunResult:
    snapshot: DistillationInvocationSnapshot
    terminal_reason: str


class DistillationInvocationService:
    """Execute a prepared deep-distillation invocation under Runtime ownership.

    The Provider is only allowed to call the three Runtime tools.  Every remote
    dispatch is receipted before the HTTP/SSE boundary is crossed, every complete
    Provider turn is receipted before executing the requested tool, and the only
    success path is a single ``distillation_output_submit`` that is persisted as
    three pending author-reviewed Knowledge candidates before the final success
    receipt is appended.
    """

    def __init__(
        self,
        *,
        invocation_store: DistillationInvocationStore,
        provider: AgentProvider | None,
        provider_resolver: Callable[[], AgentProvider | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.invocation_store = invocation_store
        self.provider = provider
        self._provider_resolver = provider_resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self.tool_definitions = build_distillation_tool_definitions()
        self.tool_definitions_hash = distillation_provider_tool_definitions_hash(
            self.tool_definitions
        )

    async def run_prepared(
        self,
        invocation_id: str,
        *,
        anchor_catalog: DistillationAnchorCatalog,
        text_by_anchor_id: Mapping[str, str],
        cancel_event: asyncio.Event | None = None,
    ) -> DistillationRunResult:
        provider = self._resolve_provider()
        if provider is None:
            raise DistillationInvocationServiceUnavailable(
                "Deep distillation Provider is not configured"
            )
        active_cancel_event = cancel_event or asyncio.Event()
        snapshot = self.invocation_store.get(invocation_id)
        prepared = _prepared_receipt(snapshot)
        prepared_payload = prepared.payload
        assert isinstance(prepared_payload, DistillationPreparedPayload)
        if snapshot.invocation.status is not DistillationInvocationStatus.PREPARED:
            raise DistillationInvocationServiceConflict(
                "Deep distillation invocation must be prepared before Runtime execution"
            )
        if (
            prepared_payload.provider.tool_definitions_hash
            != self.tool_definitions_hash
        ):
            raise DistillationInvocationServiceConflict(
                "Prepared distillation Provider tool hash does not match Runtime tools"
            )
        if (
            provider.dispatch_config_fingerprint
            != prepared_payload.provider.dispatch_config_fingerprint
        ):
            raise DistillationInvocationServiceConflict(
                "Configured deep-distillation Provider changed after prepare; prepare again"
            )
        if (
            anchor_catalog.invocation_id != invocation_id
            or anchor_catalog.catalog_id != prepared_payload.anchor_catalog_id
            or anchor_catalog.catalog_hash != prepared_payload.anchor_catalog_hash
        ):
            raise DistillationInvocationServiceConflict(
                "Distillation anchor catalog does not match prepared invocation"
            )

        messages: list[dict[str, Any]] = _initial_provider_messages(
            snapshot=snapshot,
            prepared=prepared,
            anchor_catalog=anchor_catalog,
        )
        prior_tool_result: DistillationToolResultBinding | None = None
        prior_turn_result: DistillationInvocationReceipt | None = None

        while snapshot.invocation.status is DistillationInvocationStatus.PREPARED or (
            snapshot.invocation.status is DistillationInvocationStatus.RUNNING
        ):
            turn_ordinal = snapshot.invocation.provider_turn_count + 1
            if turn_ordinal > MAX_DISTILLATION_PROVIDER_TURNS:
                return self._append_failed_final(
                    snapshot,
                    failure_code="distillation_provider_turn_limit_exceeded",
                    detail="Provider exceeded the deep-distillation turn budget",
                )
            dispatch = self._append_provider_dispatch(
                snapshot=snapshot,
                prepared=prepared,
                messages=messages,
                turn_ordinal=turn_ordinal,
                prior_turn_result=prior_turn_result,
                prior_tool_result=prior_tool_result,
            )
            snapshot = self.invocation_store.get(invocation_id)
            try:
                turn = await self._collect_provider_turn(
                    provider=provider,
                    messages=messages,
                    cancel_event=active_cancel_event,
                )
            except (AgentProviderCancelled, AgentProviderError) as exc:
                snapshot = self._append_recovery_dossier(
                    snapshot,
                    prepared=prepared,
                    last_dispatch=dispatch,
                    failure_code=getattr(
                        exc,
                        "code",
                        "distillation_provider_unknown_result",
                    ),
                    detail=str(exc),
                )
                return DistillationRunResult(
                    snapshot=snapshot,
                    terminal_reason="recovery_dossier",
                )

            if turn.failure_code is not None:
                return self._append_failed_final(
                    snapshot,
                    failure_code=turn.failure_code,
                    detail=turn.failure_detail,
                )

            assert turn.completed is not None
            assert turn.tool_call is not None
            try:
                provider_turn_receipt = self._append_provider_turn_result(
                    snapshot=snapshot,
                    dispatch=dispatch,
                    turn_ordinal=turn_ordinal,
                    assistant_text=turn.assistant_text,
                    tool_call=turn.tool_call,
                    completed=turn.completed,
                )
            except (ValueError, ValidationError) as exc:
                return self._append_failed_final(
                    snapshot,
                    failure_code="distillation_provider_turn_result_invalid",
                    detail=str(exc),
                )
            snapshot = self.invocation_store.get(invocation_id)

            result_payload = provider_turn_receipt.payload
            assert isinstance(result_payload, DistillationProviderTurnResultPayload)
            if result_payload.model_identity_status == "rejected_mismatch":
                return self._append_failed_final(
                    snapshot,
                    failure_code="distillation_provider_model_mismatch",
                    detail="Provider reported a model outside the accepted identity allowlist",
                )

            runtime_call = result_payload.tool_calls[0]
            messages.append(_assistant_tool_call_message(runtime_call))
            if runtime_call.tool_name == DISTILLATION_SEARCH_TOOL_NAME:
                try:
                    tool_result_payload, prior_tool_result = _execute_anchor_search(
                        anchor_catalog=anchor_catalog,
                        text_by_anchor_id=text_by_anchor_id,
                        provider_tool_call_id=runtime_call.provider_tool_call_id,
                        arguments=runtime_call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    return self._append_failed_final(
                        snapshot,
                        failure_code="distillation_anchor_search_invalid",
                        detail=str(exc),
                    )
                messages.append(_tool_result_message(runtime_call, tool_result_payload))
                prior_turn_result = provider_turn_receipt
                continue
            if runtime_call.tool_name == DISTILLATION_READ_TOOL_NAME:
                try:
                    tool_result_payload, prior_tool_result = _execute_anchor_read(
                        anchor_catalog=anchor_catalog,
                        text_by_anchor_id=text_by_anchor_id,
                        provider_tool_call_id=runtime_call.provider_tool_call_id,
                        arguments=runtime_call.arguments,
                    )
                except (ValueError, ValidationError) as exc:
                    return self._append_failed_final(
                        snapshot,
                        failure_code="distillation_anchor_read_invalid",
                        detail=str(exc),
                    )
                messages.append(_tool_result_message(runtime_call, tool_result_payload))
                prior_turn_result = provider_turn_receipt
                continue

            if runtime_call.tool_name == DISTILLATION_OUTPUT_TOOL_NAME:
                try:
                    submission = _parse_output_submission(runtime_call.arguments)
                    candidate_snapshot = self._append_candidate_output(
                        snapshot=snapshot,
                        prepared=prepared,
                        final_turn_result=provider_turn_receipt,
                        submission=submission,
                    )
                except (ValueError, ValidationError) as exc:
                    return self._append_failed_final(
                        snapshot,
                        failure_code="distillation_output_submit_invalid",
                        detail=str(exc),
                    )
                final = self._append_success_final(candidate_snapshot)
                return DistillationRunResult(
                    snapshot=final,
                    terminal_reason="succeeded",
                )

        raise DistillationInvocationServiceConflict(
            "Deep distillation invocation reached a non-executable status"
        )

    def _append_provider_dispatch(
        self,
        *,
        snapshot: DistillationInvocationSnapshot,
        prepared: DistillationInvocationReceipt,
        messages: Sequence[Mapping[str, Any]],
        turn_ordinal: int,
        prior_turn_result: DistillationInvocationReceipt | None,
        prior_tool_result: DistillationToolResultBinding | None,
    ) -> DistillationInvocationReceipt:
        provider_messages_hash = canonical_json_hash([dict(item) for item in messages])
        payload = DistillationProviderDispatchPayload(
            turn_ordinal=turn_ordinal,
            prepared_receipt_id=prepared.receipt_id,
            prepared_receipt_hash=prepared.receipt_hash,
            request_hash=canonical_json_hash(
                {
                    "schema": "zhimeng.distillation-provider-dispatch-request.v1",
                    "invocation_id": snapshot.invocation.invocation_id,
                    "turn_ordinal": turn_ordinal,
                    "provider_messages_hash": provider_messages_hash,
                    "tool_definitions_hash": self.tool_definitions_hash,
                    "prior_turn_result_receipt_id": (
                        prior_turn_result.receipt_id
                        if prior_turn_result is not None
                        else None
                    ),
                    "prior_tool_result_hash": (
                        prior_tool_result.binding_hash
                        if prior_tool_result is not None
                        else None
                    ),
                }
            ),
            provider_messages_hash=provider_messages_hash,
            tool_definitions_hash=self.tool_definitions_hash,
            prior_turn_result_receipt_id=(
                prior_turn_result.receipt_id if prior_turn_result is not None else None
            ),
            prior_turn_result_receipt_hash=(
                prior_turn_result.receipt_hash if prior_turn_result is not None else None
            ),
            prior_tool_result=prior_tool_result,
            provider_replay_count=0,
        )
        receipt = _next_receipt(snapshot, payload, self._now())
        self.invocation_store.append_receipt(receipt)
        return receipt

    async def _collect_provider_turn(
        self,
        *,
        provider: AgentProvider,
        messages: Sequence[Mapping[str, Any]],
        cancel_event: asyncio.Event,
    ) -> "_CollectedProviderTurn":
        text_parts: list[str] = []
        tool_calls: list[ProviderToolCall] = []
        completed: ProviderCompleted | None = None
        async for event in provider.stream_chat(
            messages,
            cancel_event=cancel_event,
            tools=self.tool_definitions,
        ):
            if isinstance(event, ProviderTextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ProviderToolCall):
                tool_calls.append(event)
            elif isinstance(event, ProviderCompleted):
                completed = event
        assistant_text = "".join(text_parts)
        if completed is None:
            return _CollectedProviderTurn.failure(
                "distillation_provider_incomplete_turn",
                "Provider stream ended without a completion event",
            )
        if completed.finish_reason not in {"tool_calls", "function_call"}:
            return _CollectedProviderTurn.failure(
                "distillation_provider_missing_submit_tool",
                f"Provider ended with finish_reason={completed.finish_reason}",
            )
        if len(tool_calls) != 1:
            return _CollectedProviderTurn.failure(
                "distillation_provider_tool_call_count_invalid",
                "Provider must emit exactly one deep-distillation ToolCall per turn",
            )
        tool_call = tool_calls[0]
        if tool_call.tool_name not in {
            DISTILLATION_SEARCH_TOOL_NAME,
            DISTILLATION_READ_TOOL_NAME,
            DISTILLATION_OUTPUT_TOOL_NAME,
        }:
            return _CollectedProviderTurn.failure(
                "distillation_provider_tool_not_advertised",
                f"Provider requested unadvertised tool {tool_call.tool_name}",
            )
        if tool_call.tool_name == DISTILLATION_OUTPUT_TOOL_NAME and assistant_text.strip():
            return _CollectedProviderTurn.failure(
                "distillation_provider_mixed_text_and_submit",
                "Provider mixed assistant text with distillation_output_submit",
            )
        return _CollectedProviderTurn(
            assistant_text=assistant_text or None,
            tool_call=tool_call,
            completed=completed,
            failure_code=None,
            failure_detail=None,
        )

    def _resolve_provider(self) -> AgentProvider | None:
        if self._provider_resolver is not None:
            return self._provider_resolver()
        return self.provider

    def _append_provider_turn_result(
        self,
        *,
        snapshot: DistillationInvocationSnapshot,
        dispatch: DistillationInvocationReceipt,
        turn_ordinal: int,
        assistant_text: str | None,
        tool_call: ProviderToolCall,
        completed: ProviderCompleted,
    ) -> DistillationInvocationReceipt:
        runtime_tool_call = DistillationProviderToolCall(
            provider_tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,  # type: ignore[arg-type]
            arguments=_json_roundtrip(tool_call.arguments),
            arguments_hash=canonical_json_hash(tool_call.arguments),
        )
        provider = snapshot.invocation.provider
        payload = DistillationProviderTurnResultPayload(
            turn_ordinal=turn_ordinal,
            dispatch_receipt_id=dispatch.receipt_id,
            dispatch_receipt_hash=dispatch.receipt_hash,
            requested_model=provider.requested_model,
            reported_model=completed.reported_model,
            accepted_reported_models=provider.accepted_reported_models,
            model_identity_status=provider_model_identity_status(
                requested_model=provider.requested_model,
                reported_model=completed.reported_model,
                accepted_reported_models=provider.accepted_reported_models,
            ),
            assistant_text=assistant_text,
            assistant_text_hash=(
                hashlib.sha256(assistant_text.encode("utf-8")).hexdigest()
                if assistant_text is not None
                else None
            ),
            tool_calls=(runtime_tool_call,),
            tool_calls_hash=distillation_provider_tool_calls_hash(
                (runtime_tool_call,)
            ),
            finish_reason=(
                "function_call"
                if completed.finish_reason == "function_call"
                else "tool_calls"
            ),
            usage=completed.usage,
        )
        receipt = _next_receipt(snapshot, payload, self._now())
        self.invocation_store.append_receipt(receipt)
        return receipt

    def _append_candidate_output(
        self,
        *,
        snapshot: DistillationInvocationSnapshot,
        prepared: DistillationInvocationReceipt,
        final_turn_result: DistillationInvocationReceipt,
        submission: DistillationOutputSubmission,
    ) -> DistillationInvocationSnapshot:
        prepared_payload = prepared.payload
        assert isinstance(prepared_payload, DistillationPreparedPayload)
        result_receipts = tuple(
            receipt
            for receipt in snapshot.receipts
            if receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT
        )
        read_anchors = _read_anchors_from_receipts(snapshot.receipts)
        candidate_set = _candidate_set_from_submission(
            invocation_id=snapshot.invocation.invocation_id,
            submission=submission,
            read_anchors=read_anchors,
        )
        payload = DistillationCandidateOutputPayload(
            final_turn_result_receipt_id=final_turn_result.receipt_id,
            final_turn_result_receipt_hash=final_turn_result.receipt_hash,
            provider_turn_count=len(result_receipts),
            provider_turn_chain_hash=distillation_provider_turn_chain_hash(
                result_receipts
            ),
            source_receipt_id=prepared_payload.source.source_receipt_id,
            source_receipt_hash=prepared_payload.source.source_receipt_hash,
            instruction_materialization_id=(
                prepared_payload.instruction.instruction_materialization_id
            ),
            instruction_materialization_hash=(
                prepared_payload.instruction.instruction_materialization_hash
            ),
            anchor_catalog_id=prepared_payload.anchor_catalog_id,
            anchor_catalog_hash=prepared_payload.anchor_catalog_hash,
            read_anchors=read_anchors,
            read_anchors_hash=distillation_anchor_binding_set_hash(read_anchors),
            submission=submission,
            submission_hash=distillation_output_submission_hash(submission),
            candidate_set=candidate_set,
        )
        receipt = _next_receipt(snapshot, payload, self._now())
        return self.invocation_store.append_receipt(receipt)

    def _append_success_final(
        self,
        snapshot: DistillationInvocationSnapshot,
    ) -> DistillationInvocationSnapshot:
        candidate_receipt = snapshot.receipts[-1]
        assert candidate_receipt.kind is DistillationReceiptKind.CANDIDATE_OUTPUT
        candidate_payload = candidate_receipt.payload
        assert isinstance(candidate_payload, DistillationCandidateOutputPayload)
        result_receipts = tuple(
            receipt
            for receipt in snapshot.receipts
            if receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT
        )
        payload = DistillationFinalResultPayload(
            outcome="succeeded",
            provider_turn_count=len(result_receipts),
            provider_turn_chain_hash=distillation_provider_turn_chain_hash(
                result_receipts
            ),
            candidate_output_receipt_id=candidate_receipt.receipt_id,
            candidate_output_receipt_hash=candidate_receipt.receipt_hash,
            candidates=tuple(
                DistillationFinalCandidateBinding(
                    candidate_id=card.candidate_id,
                    card_type=card.card_type.value,  # type: ignore[arg-type]
                    content_hash=card.card_hash,
                )
                for card in candidate_payload.candidate_set.cards
            ),
            candidate_set_hash=candidate_payload.candidate_set.candidate_set_hash,
            work_count=1,
            voice_count=1,
            validator_count=1,
            failure_signature=None,
            provider_replay_count=0,
        )
        receipt = _next_receipt(snapshot, payload, self._now())
        return self.invocation_store.append_receipt(receipt)

    def _append_failed_final(
        self,
        snapshot: DistillationInvocationSnapshot,
        *,
        failure_code: str,
        detail: str | None,
    ) -> DistillationRunResult:
        payload = _failed_final_payload(snapshot, failure_code=failure_code, detail=detail)
        receipt = _next_receipt(snapshot, payload, self._now())
        final = self.invocation_store.append_receipt(receipt)
        return DistillationRunResult(snapshot=final, terminal_reason=failure_code)

    def _append_recovery_dossier(
        self,
        snapshot: DistillationInvocationSnapshot,
        *,
        prepared: DistillationInvocationReceipt,
        last_dispatch: DistillationInvocationReceipt,
        failure_code: str,
        detail: str | None,
    ) -> DistillationInvocationSnapshot:
        candidate = _last_candidate_receipt(snapshot)
        last_result = _last_provider_result_receipt(snapshot)
        payload = DistillationRecoveryDossierPayload(
            prepared_receipt_id=prepared.receipt_id,
            prepared_receipt_hash=prepared.receipt_hash,
            last_dispatch_receipt_id=last_dispatch.receipt_id,
            last_dispatch_receipt_hash=last_dispatch.receipt_hash,
            last_turn_result_receipt_id=(
                last_result.receipt_id if last_result is not None else None
            ),
            last_turn_result_receipt_hash=(
                last_result.receipt_hash if last_result is not None else None
            ),
            candidate_state=(
                DistillationCandidateState.PERSISTED
                if candidate is not None
                else DistillationCandidateState.ABSENT
            ),
            candidate_output_receipt_id=(
                candidate.receipt_id if candidate is not None else None
            ),
            candidate_output_receipt_hash=(
                candidate.receipt_hash if candidate is not None else None
            ),
            remote_result="unknown",
            provider_replay_count=0,
            dossier_revision=1,
            failure_signature=_failure_signature(failure_code, detail),
        )
        receipt = _next_receipt(snapshot, payload, self._now())
        return self.invocation_store.append_receipt(receipt)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _CollectedProviderTurn:
    assistant_text: str | None
    tool_call: ProviderToolCall | None
    completed: ProviderCompleted | None
    failure_code: str | None
    failure_detail: str | None

    @classmethod
    def failure(cls, code: str, detail: str) -> "_CollectedProviderTurn":
        return cls(
            assistant_text=None,
            tool_call=None,
            completed=None,
            failure_code=code,
            failure_detail=detail,
        )


def build_distillation_tool_definitions() -> tuple[ProviderToolDefinition, ...]:
    return (
        ProviderToolDefinition(
            name=DISTILLATION_SEARCH_TOOL_NAME,
            version="1",
            description=(
                "Search the Runtime-owned distillation anchor catalog. "
                "Returns anchor ids and short previews only; it does not count as read evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "string",
                        "const": "zhimeng.distillation-anchor-search-request.v1",
                    },
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "levels": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["small", "big", "super"]},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        ProviderToolDefinition(
            name=DISTILLATION_READ_TOOL_NAME,
            version="1",
            description=(
                "Read exact Runtime-owned source anchors. "
                "Only anchors returned by this tool may be cited by distillation_output_submit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "string",
                        "const": "zhimeng.distillation-anchor-read-request.v1",
                    },
                    "anchor_ids": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "pattern": "^distillation-anchor-[0-9a-f]{32}$",
                        },
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                    },
                },
                "required": ["anchor_ids"],
                "additionalProperties": False,
            },
        ),
        ProviderToolDefinition(
            name=DISTILLATION_OUTPUT_TOOL_NAME,
            version="1",
            description=(
                "Submit exactly three draft Knowledge cards in canonical order: "
                "work, voice, validator. The Runtime will add ids, hashes, review state, "
                "and author-approval requirements."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "string",
                        "const": "zhimeng.distillation-output-submit.v1",
                    },
                    "cards": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "card_type": {
                                    "type": "string",
                                    "enum": ["work", "voice", "validator"],
                                },
                                "card": {
                                    "type": "object",
                                    "minProperties": 1,
                                    "additionalProperties": True,
                                },
                                "anchor_ids": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "pattern": "^distillation-anchor-[0-9a-f]{32}$",
                                    },
                                    "minItems": 1,
                                    "maxItems": 16,
                                    "uniqueItems": True,
                                },
                            },
                            "required": ["card_type", "card", "anchor_ids"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["cards"],
                "additionalProperties": False,
            },
        ),
    )


def distillation_provider_tool_definitions_hash(
    tools: Sequence[ProviderToolDefinition],
) -> str:
    return canonical_json_hash(
        [tool.model_dump(mode="json", by_alias=True) for tool in tools]
    )


def _initial_provider_messages(
    *,
    snapshot: DistillationInvocationSnapshot,
    prepared: DistillationInvocationReceipt,
    anchor_catalog: DistillationAnchorCatalog,
) -> list[dict[str, Any]]:
    prepared_payload = prepared.payload
    assert isinstance(prepared_payload, DistillationPreparedPayload)
    return [
        {
            "role": "system",
            "content": _json(
                {
                    "schema": "zhimeng.distillation-runtime-dispatch.v1",
                    "invocation_id": snapshot.invocation.invocation_id,
                    "prepared_receipt_id": prepared.receipt_id,
                    "prepared_receipt_hash": prepared.receipt_hash,
                    "source_receipt_id": prepared_payload.source.source_receipt_id,
                    "source_receipt_hash": prepared_payload.source.source_receipt_hash,
                    "anchor_catalog_id": anchor_catalog.catalog_id,
                    "anchor_catalog_hash": anchor_catalog.catalog_hash,
                    "allowed_tools": [
                        DISTILLATION_SEARCH_TOOL_NAME,
                        DISTILLATION_READ_TOOL_NAME,
                        DISTILLATION_OUTPUT_TOOL_NAME,
                    ],
                    "required_output_card_order": ["work", "voice", "validator"],
                    "formal_draft_allowed": False,
                    "project_truth_write_allowed": False,
                    "provider_replay_count": 0,
                }
            ),
        },
        {
            "role": "user",
            "content": (
                "从 Runtime 工具读取来源锚点后，提交 work、voice、validator 三张候选卡。"
                "不要输出正式正文，不要写项目文件，只能使用 distillation_output_submit 完成。"
            ),
        },
    ]


def _assistant_tool_call_message(tool_call: DistillationProviderToolCall) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call.provider_tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_call.tool_name,
                    "arguments": _json(tool_call.arguments),
                },
            }
        ],
    }


def _tool_result_message(
    tool_call: DistillationProviderToolCall,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.provider_tool_call_id,
        "name": tool_call.tool_name,
        "content": _json(payload),
    }


def _execute_anchor_search(
    *,
    anchor_catalog: DistillationAnchorCatalog,
    text_by_anchor_id: Mapping[str, str],
    provider_tool_call_id: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], DistillationToolResultBinding]:
    request = DistillationAnchorSearchRequest.model_validate_json(_json(arguments))
    query_terms = tuple(sorted({term.casefold() for term in request.query.split() if term}))
    matches: list[DistillationAnchorSearchMatch] = []
    requested_levels = set(request.levels)
    for anchor in anchor_catalog.anchors:
        if anchor.level not in requested_levels:
            continue
        text = text_by_anchor_id.get(anchor.anchor_id)
        if text is None:
            continue
        lower = text.casefold()
        if query_terms and not any(term in lower for term in query_terms):
            continue
        matched = tuple(term for term in query_terms if term in lower)
        matches.append(
            DistillationAnchorSearchMatch(
                rank=len(matches) + 1,
                anchor=distillation_anchor_binding(anchor),
                preview=text[:1000] or text,
                matched_terms=matched,
            )
        )
        if len(matches) >= request.limit:
            break
    material = {
        "invocation_id": anchor_catalog.invocation_id,
        "catalog_id": anchor_catalog.catalog_id,
        "catalog_hash": anchor_catalog.catalog_hash,
        "request_hash": distillation_anchor_search_request_hash(request),
        "matches": tuple(matches),
    }
    draft = DistillationAnchorSearchResult.model_construct(
        **material,
        result_hash="0" * 64,
    )
    result = DistillationAnchorSearchResult(
        **material,
        result_hash=distillation_anchor_search_result_hash(draft),
    )
    binding = _tool_result_binding(
        provider_tool_call_id=provider_tool_call_id,
        tool_name=DISTILLATION_SEARCH_TOOL_NAME,
        result_hash=result.result_hash,
        anchor_catalog=anchor_catalog,
        read_anchors=(),
    )
    return result.model_dump(mode="json", by_alias=True), binding


def _execute_anchor_read(
    *,
    anchor_catalog: DistillationAnchorCatalog,
    text_by_anchor_id: Mapping[str, str],
    provider_tool_call_id: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], DistillationToolResultBinding]:
    request = DistillationAnchorReadRequest.model_validate_json(_json(arguments))
    result = build_distillation_anchor_read_result(
        catalog=anchor_catalog,
        request=request,
        text_by_anchor_id=text_by_anchor_id,
    )
    read_anchors = tuple(
        sorted(
            (item.anchor for item in result.items),
            key=lambda anchor: anchor.anchor_id,
        )
    )
    binding = _tool_result_binding(
        provider_tool_call_id=provider_tool_call_id,
        tool_name=DISTILLATION_READ_TOOL_NAME,
        result_hash=distillation_anchor_read_result_hash(result),
        anchor_catalog=anchor_catalog,
        read_anchors=read_anchors,
    )
    return result.model_dump(mode="json", by_alias=True), binding


def _tool_result_binding(
    *,
    provider_tool_call_id: str,
    tool_name: str,
    result_hash: str,
    anchor_catalog: DistillationAnchorCatalog,
    read_anchors: tuple[DistillationAnchorBinding, ...],
) -> DistillationToolResultBinding:
    material = {
        "provider_tool_call_id": provider_tool_call_id,
        "tool_name": tool_name,
        "result_hash": result_hash,
        "anchor_catalog_id": anchor_catalog.catalog_id,
        "anchor_catalog_hash": anchor_catalog.catalog_hash,
        "read_anchors": read_anchors,
    }
    draft = DistillationToolResultBinding.model_construct(
        **material,
        binding_hash="0" * 64,
    )
    return DistillationToolResultBinding(
        **material,
        binding_hash=distillation_tool_result_binding_hash(draft),
    )


def _parse_output_submission(arguments: Mapping[str, Any]) -> DistillationOutputSubmission:
    return DistillationOutputSubmission.model_validate_json(_json(arguments))


def _candidate_set_from_submission(
    *,
    invocation_id: str,
    submission: DistillationOutputSubmission,
    read_anchors: tuple[DistillationAnchorBinding, ...],
) -> DistillationCandidateSet:
    anchors_by_id = {anchor.anchor_id: anchor for anchor in read_anchors}
    cards: list[DistillationCandidateCard] = []
    for index, submitted in enumerate(submission.cards):
        try:
            anchors = tuple(anchors_by_id[anchor_id] for anchor_id in submitted.anchor_ids)
        except KeyError as exc:
            raise ValueError("distillation output references an unread anchor") from exc
        card_id = f"distilled:{submitted.card_type.value}:{invocation_id[-8:]}"
        card = {
            **submitted.card,
            "card_id": card_id,
            "card_type": submitted.card_type.value,
            "draft_candidate": True,
            "requires_author_review": True,
        }
        card_hash = canonical_json_hash(card)
        candidate_id = "knowledge-candidate-" + canonical_json_hash(
            {
                "schema": "zhimeng.distillation-knowledge-candidate-id.v1",
                "invocation_id": invocation_id,
                "output_index": index,
                "card_type": submitted.card_type.value,
                "card_hash": card_hash,
                "anchor_ids": submitted.anchor_ids,
            }
        )[:32]
        cards.append(
            DistillationCandidateCard(
                candidate_id=candidate_id,
                card_id=card_id,
                output_index=index,
                card_type=DistillationCardType(submitted.card_type),
                card=card,
                card_hash=card_hash,
                anchors=anchors,
            )
        )
    card_tuple = tuple(cards)
    return DistillationCandidateSet(
        invocation_id=invocation_id,
        cards=card_tuple,
        candidate_set_hash=distillation_candidate_set_hash(card_tuple),
    )


def _read_anchors_from_receipts(
    receipts: Sequence[DistillationInvocationReceipt],
) -> tuple[DistillationAnchorBinding, ...]:
    anchors: dict[str, DistillationAnchorBinding] = {}
    for receipt in receipts:
        if receipt.kind is not DistillationReceiptKind.PROVIDER_DISPATCH:
            continue
        payload = receipt.payload
        if not isinstance(payload, DistillationProviderDispatchPayload):
            continue
        result = payload.prior_tool_result
        if result is None:
            continue
        for anchor in result.read_anchors:
            anchors[anchor.anchor_id] = anchor
    return tuple(anchors[key] for key in sorted(anchors))


def _prepared_receipt(
    snapshot: DistillationInvocationSnapshot,
) -> DistillationInvocationReceipt:
    prepared = snapshot.receipts[0]
    if prepared.kind is not DistillationReceiptKind.PREPARED:
        raise DistillationInvocationServiceConflict(
            "Deep distillation invocation has no prepared root receipt"
        )
    return prepared


def _last_candidate_receipt(
    snapshot: DistillationInvocationSnapshot,
) -> DistillationInvocationReceipt | None:
    for receipt in reversed(snapshot.receipts):
        if receipt.kind is DistillationReceiptKind.CANDIDATE_OUTPUT:
            return receipt
    return None


def _last_provider_result_receipt(
    snapshot: DistillationInvocationSnapshot,
) -> DistillationInvocationReceipt | None:
    for receipt in reversed(snapshot.receipts):
        if receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT:
            return receipt
    return None


def _next_receipt(
    snapshot: DistillationInvocationSnapshot,
    payload: Any,
    created_at: datetime,
) -> DistillationInvocationReceipt:
    return build_distillation_receipt(
        invocation_id=snapshot.invocation.invocation_id,
        ordinal=snapshot.invocation.state_revision + 1,
        parent_receipt_id=snapshot.last_receipt_id,
        expected_invocation_revision=snapshot.invocation.state_revision,
        payload=payload,
        created_at=created_at,
    )


def _failed_final_payload(
    snapshot: DistillationInvocationSnapshot,
    *,
    failure_code: str,
    detail: str | None,
) -> DistillationFinalResultPayload:
    result_receipts = tuple(
        receipt
        for receipt in snapshot.receipts
        if receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT
    )
    provider_turn_count = len(result_receipts)
    return DistillationFinalResultPayload(
        outcome="failed",
        provider_turn_count=provider_turn_count,
        provider_turn_chain_hash=(
            distillation_provider_turn_chain_hash(result_receipts)
            if provider_turn_count
            else None
        ),
        candidates=(),
        work_count=0,
        voice_count=0,
        validator_count=0,
        failure_signature=_failure_signature(failure_code, detail),
        provider_replay_count=0,
    )


def _failure_signature(code: str, detail: str | None) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.distillation-failure-signature.v1",
            "code": code,
            "detail_hash": (
                hashlib.sha256(detail.encode("utf-8")).hexdigest()
                if detail is not None
                else None
            ),
        }
    )


def _json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_roundtrip(payload: Mapping[str, Any]) -> dict[str, Any]:
    loaded = json.loads(_json(payload))
    if not isinstance(loaded, dict):
        raise ValueError("Provider ToolCall arguments must be a JSON object")
    return loaded


__all__ = [
    "DistillationInvocationService",
    "DistillationInvocationServiceConflict",
    "DistillationInvocationServiceError",
    "DistillationInvocationServiceUnavailable",
    "DistillationRunResult",
    "build_distillation_tool_definitions",
    "distillation_provider_tool_definitions_hash",
]
