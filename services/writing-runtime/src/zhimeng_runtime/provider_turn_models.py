from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.execution_protocol import MessageUsage, StableId
from zhimeng_runtime.workflow_models import ProviderIoBoundary


ProviderModelIdentityStatus = Literal[
    "exact_match",
    "accepted_alias",
    "unreported",
    "rejected_mismatch",
]
ProviderIdentityAggregateStatus = Literal[
    "pending",
    "verified",
    "unreported",
    "rejected_mismatch",
]
ProviderModelName = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def _receipt_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "receipt_id is derived from run_id, provider_boundary_id, turn_ordinal and request_hash",
        "receipt_hash hashes every field except receipt_hash",
        "model_identity_status is derived only from exact equality or the explicit accepted_reported_models allowlist",
        "unreported is the only status permitted when reported_model is null",
        "turn receipts are append-only and must be persisted before executing response ToolCalls",
    ]


def _receipt_list_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "provider_turn_receipts are ordered, contiguous and bound to one Run and Provider boundary",
        "provider_turn_receipts_hash uses the same ordered binding-chain hash as Agent Candidate v2",
        "provider_identity_status is derived from the complete receipt list",
        "the accepted reported-model allowlist cannot change within one Run",
    ]


class ProviderTurnModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class ProviderTurnReceiptBinding(ProviderTurnModel):
    turn_ordinal: int = Field(ge=1, le=8, strict=True)
    receipt_id: str = Field(pattern=r"^provider-turn-receipt-[0-9a-f]{32}$")
    receipt_hash: Sha256


class ProviderTurnReceipt(ProviderTurnModel):
    model_config = ConfigDict(json_schema_extra=_receipt_schema_invariants)

    schema_id: Literal["zhimeng.provider-turn-receipt.v1"] = Field(
        default="zhimeng.provider-turn-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^provider-turn-receipt-[0-9a-f]{32}$")
    run_id: StableId
    provider_boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    provider_boundary_hash: Sha256
    turn_ordinal: int = Field(ge=1, le=8, strict=True)
    request_hash: Sha256
    requested_model: ProviderModelName | None
    reported_model: ProviderModelName | None
    accepted_reported_models: tuple[ProviderModelName, ...] = Field(max_length=64)
    model_identity_status: ProviderModelIdentityStatus
    finish_reason: str = Field(min_length=1, max_length=128)
    usage: MessageUsage | None
    created_at: datetime
    receipt_hash: Sha256

    @field_validator("requested_model", "reported_model")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider model identity must not be blank")
        return normalized

    @field_validator("accepted_reported_models")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip() for value in values}))
        if any(not value for value in normalized):
            raise ValueError("accepted Provider model aliases must not be blank")
        if tuple(values) != normalized:
            raise ValueError("accepted Provider model aliases must be sorted and unique")
        return normalized

    @field_validator("finish_reason")
    @classmethod
    def normalize_finish_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider finish reason must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_receipt(self) -> "ProviderTurnReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Provider turn receipt time must be timezone-aware")
        expected_id = provider_turn_receipt_id(
            run_id=self.run_id,
            provider_boundary_id=self.provider_boundary_id,
            turn_ordinal=self.turn_ordinal,
            request_hash=self.request_hash,
        )
        if self.receipt_id != expected_id:
            raise ValueError("Provider turn receipt identity does not match immutable input")
        expected_status = provider_model_identity_status(
            requested_model=self.requested_model,
            reported_model=self.reported_model,
            accepted_reported_models=self.accepted_reported_models,
        )
        if self.model_identity_status != expected_status:
            raise ValueError("Provider model identity status does not match reported evidence")
        if self.receipt_hash != provider_turn_receipt_hash(self):
            raise ValueError("Provider turn receipt hash does not match immutable material")
        return self

    def binding(self) -> ProviderTurnReceiptBinding:
        return ProviderTurnReceiptBinding(
            turn_ordinal=self.turn_ordinal,
            receipt_id=self.receipt_id,
            receipt_hash=self.receipt_hash,
        )


class ProviderTurnReceiptListResponse(ProviderTurnModel):
    model_config = ConfigDict(json_schema_extra=_receipt_list_schema_invariants)

    schema_id: Literal["zhimeng.provider-turn-receipt-list.v1"] = Field(
        default="zhimeng.provider-turn-receipt-list.v1",
        alias="schema",
    )
    run_id: StableId
    provider_boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    provider_boundary_hash: Sha256
    requested_model: ProviderModelName | None
    provider_turn_receipts: tuple[ProviderTurnReceipt, ...] = Field(max_length=8)
    provider_turn_receipts_hash: Sha256
    provider_identity_status: ProviderIdentityAggregateStatus

    @field_validator("requested_model")
    @classmethod
    def normalize_requested_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Provider requested model identity must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_receipt_list(self) -> "ProviderTurnReceiptListResponse":
        expected_ordinals = tuple(range(1, len(self.provider_turn_receipts) + 1))
        if tuple(
            receipt.turn_ordinal for receipt in self.provider_turn_receipts
        ) != expected_ordinals:
            raise ValueError("Provider turn receipt list must be contiguous")
        if any(
            receipt.run_id != self.run_id
            or receipt.provider_boundary_id != self.provider_boundary_id
            or receipt.provider_boundary_hash != self.provider_boundary_hash
            or receipt.requested_model != self.requested_model
            for receipt in self.provider_turn_receipts
        ):
            raise ValueError("Provider turn receipt list does not bind its boundary")
        if self.provider_turn_receipts and any(
            receipt.accepted_reported_models
            != self.provider_turn_receipts[0].accepted_reported_models
            for receipt in self.provider_turn_receipts[1:]
        ):
            raise ValueError("Provider turn receipt allowlist changed within the list")
        bindings = tuple(
            receipt.binding() for receipt in self.provider_turn_receipts
        )
        if self.provider_turn_receipts_hash != provider_turn_receipt_chain_hash(
            bindings
        ):
            raise ValueError("Provider turn receipt list hash does not match")
        if self.provider_identity_status != provider_identity_aggregate_status(
            self.provider_turn_receipts
        ):
            raise ValueError("Provider aggregate identity status does not match receipts")
        return self


def provider_model_identity_status(
    *,
    requested_model: str | None,
    reported_model: str | None,
    accepted_reported_models: tuple[str, ...],
) -> ProviderModelIdentityStatus:
    if reported_model is None:
        return "unreported"
    if requested_model is not None and reported_model == requested_model:
        return "exact_match"
    if reported_model in accepted_reported_models:
        return "accepted_alias"
    return "rejected_mismatch"


def provider_identity_aggregate_status(
    receipts: tuple[ProviderTurnReceipt, ...],
) -> ProviderIdentityAggregateStatus:
    if not receipts:
        return "pending"
    if any(
        receipt.model_identity_status == "rejected_mismatch"
        for receipt in receipts
    ):
        return "rejected_mismatch"
    if any(receipt.model_identity_status == "unreported" for receipt in receipts):
        return "unreported"
    return "verified"


def provider_turn_receipt_id(
    *,
    run_id: str,
    provider_boundary_id: str,
    turn_ordinal: int,
    request_hash: str,
) -> str:
    return "provider-turn-receipt-" + canonical_json_hash(
        {
            "schema": "zhimeng.provider-turn-receipt.v1",
            "run_id": run_id,
            "provider_boundary_id": provider_boundary_id,
            "turn_ordinal": turn_ordinal,
            "request_hash": request_hash,
        }
    )[:32]


def provider_turn_receipt_hash(receipt: ProviderTurnReceipt) -> str:
    return canonical_json_hash(
        receipt.model_dump(
            mode="json",
            by_alias=True,
            exclude={"receipt_hash"},
        )
    )


def provider_turn_receipt_chain_hash(
    bindings: tuple[ProviderTurnReceiptBinding, ...],
) -> str:
    return canonical_json_hash(
        [binding.model_dump(mode="json") for binding in bindings]
    )


def build_provider_turn_receipt(
    boundary: ProviderIoBoundary,
    *,
    turn_ordinal: int,
    request_hash: str,
    reported_model: str | None,
    accepted_reported_models: tuple[str, ...],
    finish_reason: str,
    usage: MessageUsage | None,
    created_at: datetime,
) -> ProviderTurnReceipt:
    aliases = tuple(sorted(set(accepted_reported_models)))
    status = provider_model_identity_status(
        requested_model=boundary.provider_model,
        reported_model=reported_model,
        accepted_reported_models=aliases,
    )
    material = {
        "schema_id": "zhimeng.provider-turn-receipt.v1",
        "receipt_id": provider_turn_receipt_id(
            run_id=boundary.run_id,
            provider_boundary_id=boundary.boundary_id,
            turn_ordinal=turn_ordinal,
            request_hash=request_hash,
        ),
        "run_id": boundary.run_id,
        "provider_boundary_id": boundary.boundary_id,
        "provider_boundary_hash": boundary.boundary_hash,
        "turn_ordinal": turn_ordinal,
        "request_hash": request_hash,
        "requested_model": boundary.provider_model,
        "reported_model": reported_model,
        "accepted_reported_models": aliases,
        "model_identity_status": status,
        "finish_reason": finish_reason,
        "usage": usage,
        "created_at": created_at,
    }
    draft = ProviderTurnReceipt.model_construct(**material, receipt_hash="0" * 64)
    return ProviderTurnReceipt(
        **material,
        receipt_hash=provider_turn_receipt_hash(draft),
    )


def build_provider_turn_receipt_list_response(
    boundary: ProviderIoBoundary,
    receipts: tuple[ProviderTurnReceipt, ...],
) -> ProviderTurnReceiptListResponse:
    bindings = tuple(receipt.binding() for receipt in receipts)
    return ProviderTurnReceiptListResponse(
        run_id=boundary.run_id,
        provider_boundary_id=boundary.boundary_id,
        provider_boundary_hash=boundary.boundary_hash,
        requested_model=boundary.provider_model,
        provider_turn_receipts=receipts,
        provider_turn_receipts_hash=provider_turn_receipt_chain_hash(bindings),
        provider_identity_status=provider_identity_aggregate_status(receipts),
    )


__all__ = [
    "ProviderIdentityAggregateStatus",
    "ProviderModelIdentityStatus",
    "ProviderTurnReceipt",
    "ProviderTurnReceiptBinding",
    "ProviderTurnReceiptListResponse",
    "build_provider_turn_receipt",
    "build_provider_turn_receipt_list_response",
    "provider_identity_aggregate_status",
    "provider_model_identity_status",
    "provider_turn_receipt_chain_hash",
    "provider_turn_receipt_hash",
    "provider_turn_receipt_id",
]
