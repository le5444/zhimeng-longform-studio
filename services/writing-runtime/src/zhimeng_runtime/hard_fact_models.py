from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
ChangeSetId = Annotated[str, StringConstraints(pattern=r"^changeset-[0-9a-f]{32}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
HardFactReceiptId = Annotated[
    str, StringConstraints(pattern=r"^hard-fact-gate-[0-9a-f]{32}$")
]


class HardFactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class HardFactStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    MISSING_INPUTS = "missing_inputs"


class HardFactSignalCode(StrEnum):
    PLACEHOLDER_TOKEN = "placeholder_token"
    UNRESOLVED_FACT_MARKER = "unresolved_fact_marker"
    POST_DRAFT_BLOCKED = "post_draft_blocked"
    LITERARY_HARD_FACT_SIGNAL = "literary_hard_fact_signal"
    CANDIDATE_MISSING = "candidate_missing"


class HardFactEvaluateRequest(HardFactModel):
    schema_id: Literal["zhimeng.hard-fact-gate-request.v1"] = Field(
        default="zhimeng.hard-fact-gate-request.v1",
        alias="schema",
    )
    expected_result_fingerprint: Sha256 | None = None
    expected_review_revision: int | None = Field(default=None, ge=0)


class HardFactSignal(HardFactModel):
    code: HardFactSignalCode
    severity: Literal["info", "watch", "block"]
    summary: NonEmptyText = Field(max_length=500)
    evidence_quote: str = Field(default="", max_length=1000)
    relative_path: str | None = Field(default=None, max_length=512)
    source: Literal[
        "candidate_scan",
        "post_draft_gate",
        "literary_quality_gate",
        "runtime",
    ]


class HardFactGateReceipt(HardFactModel):
    schema_id: Literal["zhimeng.hard-fact-gate-receipt.v1"] = Field(
        default="zhimeng.hard-fact-gate-receipt.v1",
        alias="schema",
    )
    receipt_id: HardFactReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back", "ready_candidate"]
    result_revision: int | None = Field(default=None, ge=1)
    result_fingerprint: Sha256 | None = None
    review_revision: int = Field(ge=0)
    gate_mode: Literal["deterministic_hard_fact_v1"] = "deterministic_hard_fact_v1"
    status: HardFactStatus
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    post_draft_gate_status: Literal["missing", "passed", "blocked", "stale"] = "missing"
    literary_quality_receipt_id: str | None = Field(default=None, max_length=160)
    signals: list[HardFactSignal] = Field(default_factory=list, max_length=64)
    signal_counts: dict[str, int] = Field(default_factory=dict)
    policy_version: Literal["local-v1-hard-fact-1"] = "local-v1-hard-fact-1"
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "HardFactGateReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("hard-fact gate cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("hard-fact gate cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("hard-fact gate cannot claim literary quality assessed")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        expected_ready = self.status is HardFactStatus.PASSED
        if self.authorization_input_ready is not expected_ready:
            raise ValueError(
                "authorization_input_ready must match hard-fact structural pass status"
            )
        if self.status is HardFactStatus.PASSED and self.signals:
            # pass may still carry info-level signals only
            if any(item.severity == "block" for item in self.signals):
                raise ValueError("passed hard-fact gate cannot carry block signals")
        expected = hard_fact_gate_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("hard-fact gate receipt hash mismatch")
        return self


class HardFactGateResponse(HardFactModel):
    schema_id: Literal["zhimeng.hard-fact-gate-response.v1"] = Field(
        default="zhimeng.hard-fact-gate-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: HardFactGateReceipt | None = None
    history: list[HardFactGateReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "literary_quality_score",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def hard_fact_gate_receipt_hash(receipt: HardFactGateReceipt) -> str:
    material = {
        "schema": "zhimeng.hard-fact-gate-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "outcome": receipt.outcome,
        "result_revision": receipt.result_revision,
        "result_fingerprint": receipt.result_fingerprint,
        "review_revision": receipt.review_revision,
        "gate_mode": receipt.gate_mode,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "post_draft_gate_status": receipt.post_draft_gate_status,
        "literary_quality_receipt_id": receipt.literary_quality_receipt_id,
        "signals": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "severity": item.severity,
                "summary": item.summary,
                "evidence_quote": item.evidence_quote,
                "relative_path": item.relative_path,
                "source": item.source,
            }
            for item in receipt.signals
        ],
        "signal_counts": receipt.signal_counts,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "HardFactEvaluateRequest",
    "HardFactGateReceipt",
    "HardFactGateResponse",
    "HardFactSignal",
    "HardFactSignalCode",
    "HardFactStatus",
    "hard_fact_gate_receipt_hash",
]
