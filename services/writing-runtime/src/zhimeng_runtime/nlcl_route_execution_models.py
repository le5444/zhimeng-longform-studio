from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.nlcl_models import NlclChapterCall, NlclModel, NlclRouteReceipt


class NlclRouteExecutionReceipt(NlclModel):
    schema_id: Literal["zhimeng.nlcl-route-execution-receipt.v1"] = Field(
        default="zhimeng.nlcl-route-execution-receipt.v1",
        alias="schema",
    )
    execution_receipt_id: str = Field(pattern=r"^nlcl-route-exec-[0-9a-f]{32}$")
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    context_pack_hash: Sha256
    project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
        strict=True,
    )
    project_kb_manifest_hash: Sha256 | None = None
    call_ordinal: int = Field(ge=1, le=64, strict=True)
    skill_id: str = Field(min_length=3, max_length=128, strict=True)
    input_contract: str = Field(min_length=3, max_length=256, strict=True)
    run_id: str = Field(min_length=1, max_length=128, strict=True)
    tool_call_id: str = Field(min_length=1, max_length=128, strict=True)
    skill_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    skill_receipt_id: str | None = Field(default=None, min_length=1, max_length=128)
    skill_receipt_hash: Sha256 | None = None
    execution_status: Literal["succeeded", "failed", "cancelled"]
    domain_status: Literal["healthy", "degraded", "blocked"] | None = None
    output_hash: Sha256
    tool_result_hash: Sha256
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> NlclRouteExecutionReceipt:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("NLCL route execution receipt created_at must be timezone-aware")
        if (self.project_kb_state_id is None) != (
            self.project_kb_manifest_hash is None
        ):
            raise ValueError("NLCL execution project KB binding must be paired")
        if (self.skill_receipt_id is None) != (self.skill_receipt_hash is None):
            raise ValueError("NLCL Skill receipt identity and hash must be present together")
        if self.execution_status == "succeeded":
            if (
                self.skill_call_id is None
                or self.skill_receipt_id is None
                or self.domain_status is None
            ):
                raise ValueError(
                    "succeeded NLCL route execution requires Skill and domain receipts"
                )
        elif self.domain_status is not None:
            raise ValueError("failed or cancelled route execution cannot claim domain status")
        if self.receipt_hash != nlcl_route_execution_receipt_hash(self):
            raise ValueError(
                "NLCL route execution receipt hash does not match immutable material"
            )
        return self


class NlclRouteExecutionState(NlclModel):
    schema_id: Literal["zhimeng.nlcl-route-execution-state.v1"] = Field(
        default="zhimeng.nlcl-route-execution-state.v1",
        alias="schema",
    )
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    call_ordinal: int = Field(ge=1, le=64, strict=True)
    status: Literal[
        "awaiting_execution",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    receipt: NlclRouteExecutionReceipt | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_state(self) -> NlclRouteExecutionState:
        if self.receipt is not None:
            if (
                self.receipt.route_receipt_id != self.route_receipt_id
                or self.receipt.context_pack_id != self.context_pack_id
                or self.receipt.call_ordinal != self.call_ordinal
                or self.receipt.execution_status != self.status
                or self.receipt.run_id != self.run_id
                or self.receipt.tool_call_id != self.tool_call_id
            ):
                raise ValueError("NLCL route execution state does not match its receipt")
        elif self.status == "succeeded":
            raise ValueError("succeeded NLCL route execution state requires a receipt")
        if self.status == "awaiting_execution" and self.tool_call_id is not None:
            raise ValueError("awaiting NLCL route execution cannot bind a ToolCall")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("NLCL route execution error code and message must pair")
        if self.status not in {"failed", "cancelled"} and self.error_code is not None:
            raise ValueError("only failed or cancelled route execution can carry an error")
        return self


def nlcl_route_execution_receipt_hash(
    receipt: NlclRouteExecutionReceipt,
) -> str:
    payload = receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    if (
        payload.get("project_kb_state_id") is None
        and payload.get("project_kb_manifest_hash") is None
    ):
        payload.pop("project_kb_state_id", None)
        payload.pop("project_kb_manifest_hash", None)
    return canonical_json_hash(payload)


def build_nlcl_route_execution_receipt(
    route: NlclRouteReceipt,
    call: NlclChapterCall,
    *,
    run_id: str,
    tool_call_id: str,
    skill_call_id: str | None,
    skill_receipt_id: str | None,
    skill_receipt_hash: str | None,
    execution_status: Literal["succeeded", "failed", "cancelled"],
    domain_status: Literal["healthy", "degraded", "blocked"] | None,
    output_hash: str,
    tool_result_hash: str,
    created_at: datetime,
) -> NlclRouteExecutionReceipt:
    if call not in route.chapter_call_list:
        raise ValueError("NLCL chapter call is not part of the supplied route receipt")
    if call.context_pack_id != route.context_pack_id:
        raise ValueError("NLCL chapter call does not bind the route Context Pack")
    receipt_id = "nlcl-route-exec-" + hashlib.sha256(
        (
            "zhimeng.nlcl-route-execution-receipt.v1\0"
            f"{route.route_receipt_id}\0{call.ordinal}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    material = {
        "execution_receipt_id": receipt_id,
        "route_receipt_id": route.route_receipt_id,
        "route_receipt_hash": route.receipt_hash,
        "context_pack_id": route.context_pack_id,
        "context_pack_hash": route.context_pack_hash,
        "project_kb_state_id": route.project_kb_state_id,
        "project_kb_manifest_hash": route.project_kb_manifest_hash,
        "call_ordinal": call.ordinal,
        "skill_id": call.skill_id,
        "input_contract": call.input_contract,
        "run_id": run_id,
        "tool_call_id": tool_call_id,
        "skill_call_id": skill_call_id,
        "skill_receipt_id": skill_receipt_id,
        "skill_receipt_hash": skill_receipt_hash,
        "execution_status": execution_status,
        "domain_status": domain_status,
        "output_hash": output_hash,
        "tool_result_hash": tool_result_hash,
        "created_at": created_at,
    }
    draft = NlclRouteExecutionReceipt.model_construct(
        **material,
        receipt_hash="0" * 64,
    )
    return NlclRouteExecutionReceipt(
        **material,
        receipt_hash=nlcl_route_execution_receipt_hash(draft),
    )


__all__ = [
    "NlclRouteExecutionReceipt",
    "NlclRouteExecutionState",
    "build_nlcl_route_execution_receipt",
    "nlcl_route_execution_receipt_hash",
]
