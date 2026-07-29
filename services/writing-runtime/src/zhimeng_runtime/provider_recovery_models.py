from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.execution_protocol import RunStatus, StableId


RecoveryAction = Literal["adopt_persisted_candidate", "abandon_and_recompile"]
RecoveryCandidateState = Literal["persisted", "absent", "invalid"]
RecoveryDecisionStatus = Literal["pending", "adopted", "abandoned"]


def _dossier_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "dossier_id is derived from run_id and provider_boundary_id",
        "dossier_hash hashes every field except dossier_hash",
        "persisted candidate_state requires candidate_output_id and candidate_output_hash",
        "absent or invalid candidate_state forbids candidate identity fields",
        "workflow, boundary, context, package and project identities bind one persisted Run",
        "the dossier binds the immutable Creation instruction dispatch receipt",
        "one dossier per Run is stored append-only",
        "remote_result remains unknown and provider_replay_count remains zero",
    ]


def _decision_request_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "expected_dossier_revision and expected_dossier_hash form one CAS precondition",
        "adopt_persisted_candidate requires a valid persisted candidate",
        "neither recovery action authorizes Provider replay",
    ]


def _decision_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "only a pending dossier can be decided",
        "new_dossier_revision equals previous_dossier_revision plus one",
        "decision_id is derived from dossier_id, action and previous_dossier_revision",
        "decision_hash hashes every field except decision_hash",
        "the decision preserves the dossier hash and instruction dispatch receipt CAS input",
        "adopt produces a succeeded Run and abandon produces a failed Run",
        "one decision per dossier is stored append-only",
    ]


def _response_schema_invariants(schema: dict[str, object]) -> None:
    schema["x-zhimeng-invariants"] = [
        "decision dossier_id and run_id match the enclosed dossier",
        "decision previous revision matches the enclosed dossier revision",
        "decision created_at is not earlier than the enclosed dossier created_at",
        "a null decision represents a pending recovery dossier",
    ]


class ProviderRecoveryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class ProviderRecoveryDossier(ProviderRecoveryModel):
    model_config = ConfigDict(json_schema_extra=_dossier_schema_invariants)

    schema_id: Literal["zhimeng.provider-recovery-dossier.v1"] = Field(
        default="zhimeng.provider-recovery-dossier.v1",
        alias="schema",
    )
    dossier_id: str = Field(pattern=r"^provider-recovery-[0-9a-f]{32}$")
    run_id: StableId
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    final_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    final_checkpoint_hash: Sha256
    provider_boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    provider_boundary_hash: Sha256
    provider_request_hash: Sha256
    instruction_dispatch_receipt_id: str = Field(
        pattern=r"^creation-instruction-dispatch-[0-9a-f]{32}$"
    )
    instruction_dispatch_receipt_hash: Sha256
    context_pack_id: StableId
    context_pack_hash: Sha256
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: Sha256
    project_id: StableId
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    run_status_at_capture: RunStatus
    candidate_state: RecoveryCandidateState
    candidate_output_id: str | None = Field(
        default=None,
        pattern=r"^agent-candidate-[0-9a-f]{32}$",
    )
    candidate_output_hash: Sha256 | None = None
    remote_result: Literal["unknown"] = "unknown"
    provider_replay_count: Literal[0] = 0
    dossier_revision: int = Field(default=1, ge=1, strict=True)
    created_at: datetime
    dossier_hash: Sha256

    @model_validator(mode="after")
    def validate_dossier(self) -> "ProviderRecoveryDossier":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Provider recovery dossier time must be timezone-aware")
        expected_id = provider_recovery_dossier_id(
            run_id=self.run_id,
            provider_boundary_id=self.provider_boundary_id,
        )
        if self.dossier_id != expected_id:
            raise ValueError("Provider recovery dossier identity does not match boundary")
        if self.candidate_state == "persisted":
            if self.candidate_output_id is None or self.candidate_output_hash is None:
                raise ValueError("persisted candidate recovery requires candidate identity")
        elif self.candidate_output_id is not None or self.candidate_output_hash is not None:
            raise ValueError("absent or invalid candidate recovery cannot expose identity")
        if self.dossier_hash != provider_recovery_dossier_hash(self):
            raise ValueError("Provider recovery dossier hash does not match immutable material")
        return self


class ProviderRecoveryDecisionRequest(ProviderRecoveryModel):
    model_config = ConfigDict(json_schema_extra=_decision_request_schema_invariants)

    schema_id: Literal["zhimeng.provider-recovery-decision-request.v1"] = Field(
        default="zhimeng.provider-recovery-decision-request.v1",
        alias="schema",
    )
    expected_dossier_revision: int = Field(ge=1, strict=True)
    expected_dossier_hash: Sha256
    action: RecoveryAction


class ProviderRecoveryDecision(ProviderRecoveryModel):
    model_config = ConfigDict(json_schema_extra=_decision_schema_invariants)

    schema_id: Literal["zhimeng.provider-recovery-decision.v1"] = Field(
        default="zhimeng.provider-recovery-decision.v1",
        alias="schema",
    )
    decision_id: str = Field(pattern=r"^provider-recovery-decision-[0-9a-f]{32}$")
    dossier_id: str = Field(pattern=r"^provider-recovery-[0-9a-f]{32}$")
    run_id: StableId
    previous_dossier_hash: Sha256
    instruction_dispatch_receipt_id: str = Field(
        pattern=r"^creation-instruction-dispatch-[0-9a-f]{32}$"
    )
    instruction_dispatch_receipt_hash: Sha256
    action: RecoveryAction
    previous_status: RecoveryDecisionStatus
    resulting_run_status: RunStatus
    previous_dossier_revision: int = Field(ge=1, strict=True)
    new_dossier_revision: int = Field(ge=2, strict=True)
    created_at: datetime
    decision_hash: Sha256

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Provider recovery decision time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> "ProviderRecoveryDecision":
        if self.previous_status != "pending":
            raise ValueError("only a pending recovery dossier can be decided")
        if self.new_dossier_revision != self.previous_dossier_revision + 1:
            raise ValueError("recovery dossier revisions must advance by one")
        expected_status = (
            RunStatus.SUCCEEDED
            if self.action == "adopt_persisted_candidate"
            else RunStatus.FAILED
        )
        if self.resulting_run_status is not expected_status:
            raise ValueError("recovery action does not match its resulting Run status")
        expected_id = provider_recovery_decision_id(
            dossier_id=self.dossier_id,
            previous_dossier_hash=self.previous_dossier_hash,
            instruction_dispatch_receipt_id=self.instruction_dispatch_receipt_id,
            instruction_dispatch_receipt_hash=self.instruction_dispatch_receipt_hash,
            action=self.action,
            previous_dossier_revision=self.previous_dossier_revision,
        )
        if self.decision_id != expected_id:
            raise ValueError("Provider recovery decision identity does not match CAS input")
        if self.decision_hash != provider_recovery_decision_hash(self):
            raise ValueError("Provider recovery decision hash does not match immutable material")
        return self


class ProviderRecoveryResponse(ProviderRecoveryModel):
    model_config = ConfigDict(json_schema_extra=_response_schema_invariants)

    schema_id: Literal["zhimeng.provider-recovery.v1"] = Field(
        default="zhimeng.provider-recovery.v1",
        alias="schema",
    )
    dossier: ProviderRecoveryDossier
    decision: ProviderRecoveryDecision | None = None

    @model_validator(mode="after")
    def validate_response_binding(self) -> "ProviderRecoveryResponse":
        decision = self.decision
        if decision is None:
            return self
        if (
            decision.dossier_id != self.dossier.dossier_id
            or decision.run_id != self.dossier.run_id
            or decision.previous_dossier_hash != self.dossier.dossier_hash
            or decision.instruction_dispatch_receipt_id
            != self.dossier.instruction_dispatch_receipt_id
            or decision.instruction_dispatch_receipt_hash
            != self.dossier.instruction_dispatch_receipt_hash
            or decision.previous_dossier_revision
            != self.dossier.dossier_revision
            or decision.created_at < self.dossier.created_at
        ):
            raise ValueError("Provider recovery decision does not bind its dossier")
        return self


def _derived_id(prefix: str, material: object) -> str:
    return f"{prefix}-{canonical_json_hash(material)[:32]}"


def provider_recovery_dossier_id(*, run_id: str, provider_boundary_id: str) -> str:
    return _derived_id(
        "provider-recovery",
        {
            "schema": "zhimeng.provider-recovery-dossier.v1",
            "run_id": run_id,
            "provider_boundary_id": provider_boundary_id,
        },
    )


def provider_recovery_dossier_hash(dossier: ProviderRecoveryDossier) -> str:
    return canonical_json_hash(
        dossier.model_dump(mode="json", by_alias=True, exclude={"dossier_hash"})
    )


def provider_recovery_decision_id(
    *,
    dossier_id: str,
    previous_dossier_hash: str,
    instruction_dispatch_receipt_id: str,
    instruction_dispatch_receipt_hash: str,
    action: RecoveryAction,
    previous_dossier_revision: int,
) -> str:
    return _derived_id(
        "provider-recovery-decision",
        {
            "schema": "zhimeng.provider-recovery-decision.v1",
            "dossier_id": dossier_id,
            "previous_dossier_hash": previous_dossier_hash,
            "instruction_dispatch_receipt_id": instruction_dispatch_receipt_id,
            "instruction_dispatch_receipt_hash": instruction_dispatch_receipt_hash,
            "action": action,
            "previous_dossier_revision": previous_dossier_revision,
        },
    )


def provider_recovery_decision_hash(decision: ProviderRecoveryDecision) -> str:
    return canonical_json_hash(
        decision.model_dump(mode="json", by_alias=True, exclude={"decision_hash"})
    )


__all__ = [
    "ProviderRecoveryDecision",
    "ProviderRecoveryDecisionRequest",
    "ProviderRecoveryDossier",
    "ProviderRecoveryResponse",
    "RecoveryAction",
    "RecoveryCandidateState",
    "RecoveryDecisionStatus",
    "provider_recovery_decision_hash",
    "provider_recovery_decision_id",
    "provider_recovery_dossier_hash",
    "provider_recovery_dossier_id",
]
