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
SuiteId = Annotated[str, StringConstraints(pattern=r"^eval-suite-[0-9a-f]{32}$")]
MemberToken = Annotated[str, StringConstraints(pattern=r"^eval-member-[0-9a-f]{24}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class EvalSuiteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EvalSuiteStatus(StrEnum):
    PREPARED = "prepared"
    FROZEN = "frozen"
    COMPARE_READY = "compare_ready"
    COMPARE_DEGRADED = "compare_degraded"
    BLIND_REVIEW_OPEN = "blind_review_open"
    BLIND_REVIEW_FINALIZED = "blind_review_finalized"


class EvalSuiteAction(StrEnum):
    FREEZE = "freeze"
    COMPARE = "compare"
    BLIND_REVIEW_OPEN = "blind_review_open"
    BLIND_REVIEW_SUBMIT = "blind_review_submit"
    BLIND_REVIEW_FINALIZE = "blind_review_finalize"


class EvalSuiteMemberRequest(EvalSuiteModel):
    project_id: ProjectId
    change_set_id: ChangeSetId
    expected_baseline_receipt_id: str | None = Field(default=None, max_length=160)
    expected_baseline_receipt_hash: Sha256 | None = None
    label: NonEmptyText | None = Field(default=None, max_length=128)


class EvalSuitePrepareRequest(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-prepare-request.v1"] = Field(
        default="zhimeng.eval-suite-prepare-request.v1",
        alias="schema",
    )
    title: NonEmptyText = Field(max_length=200)
    summary: NonEmptyText = Field(max_length=1000)
    members: list[EvalSuiteMemberRequest] = Field(min_length=2, max_length=32)


class EvalSuiteBlindReviewItem(EvalSuiteModel):
    member_token: MemberToken
    score: int = Field(ge=1, le=5)
    notes: NonEmptyText | None = Field(default=None, max_length=1000)


class EvalSuiteActionRequest(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-action-request.v1"] = Field(
        default="zhimeng.eval-suite-action-request.v1",
        alias="schema",
    )
    action: Literal[
        EvalSuiteAction.FREEZE,
        EvalSuiteAction.COMPARE,
        EvalSuiteAction.BLIND_REVIEW_OPEN,
        EvalSuiteAction.BLIND_REVIEW_SUBMIT,
        EvalSuiteAction.BLIND_REVIEW_FINALIZE,
    ]
    expected_status: EvalSuiteStatus | None = None
    expected_state_revision: int | None = Field(default=None, ge=0)
    compare_against_suite_id: SuiteId | None = None
    blind_review_items: list[EvalSuiteBlindReviewItem] = Field(
        default_factory=list,
        max_length=32,
    )


class EvalSuiteMemberRecord(EvalSuiteModel):
    member_token: MemberToken
    project_id: ProjectId
    change_set_id: ChangeSetId
    baseline_receipt_id: NonEmptyText = Field(max_length=160)
    baseline_receipt_hash: Sha256
    export_file_sha256: Sha256
    present_artifact_kinds: list[NonEmptyText] = Field(default_factory=list, max_length=32)
    label: NonEmptyText | None = Field(default=None, max_length=128)
    reveal_after_finalize: Literal[True] = True


class EvalSuiteBlindScore(EvalSuiteModel):
    member_token: MemberToken
    score: int = Field(ge=1, le=5)
    notes: NonEmptyText | None = Field(default=None, max_length=1000)


class EvalSuiteCompareCheck(EvalSuiteModel):
    code: NonEmptyText = Field(max_length=64)
    present: bool
    detail: NonEmptyText = Field(max_length=1024)


class EvalSuiteReceipt(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-receipt.v1"] = Field(
        default="zhimeng.eval-suite-receipt.v1",
        alias="schema",
    )
    suite_id: SuiteId
    title: NonEmptyText = Field(max_length=200)
    summary: NonEmptyText = Field(max_length=1000)
    status: EvalSuiteStatus
    state_revision: int = Field(ge=0)
    members: list[EvalSuiteMemberRecord] = Field(min_length=2, max_length=32)
    member_count: int = Field(ge=2, le=32)
    frozen: bool
    compare_against_suite_id: SuiteId | None = None
    compare_status: Literal["deferred", "non_degraded", "degraded"] = "deferred"
    compare_checks: list[EvalSuiteCompareCheck] = Field(default_factory=list, max_length=32)
    blind_review_open: bool = False
    blind_review_finalized: bool = False
    blind_scores: list[EvalSuiteBlindScore] = Field(default_factory=list, max_length=32)
    reveal_member_identities: bool = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    training_ready: Literal[False] = False
    auto_upgrade: Literal[False] = False
    policy_version: Literal["local-v1-eval-suite-1"] = "local-v1-eval-suite-1"
    detail: NonEmptyText = Field(max_length=1024)
    updated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "EvalSuiteReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("eval suite cannot unlock formal draft")
        if self.quality_assessed is not False:
            raise ValueError("eval suite cannot claim quality assessed")
        if self.training_ready is not False:
            raise ValueError("eval suite cannot claim training ready")
        if self.auto_upgrade is not False:
            raise ValueError("eval suite cannot auto-upgrade")
        if self.member_count != len(self.members):
            raise ValueError("member_count must match members length")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        if self.blind_review_finalized and not self.reveal_member_identities:
            raise ValueError("finalized blind review must reveal member identities for audit")
        if self.blind_review_open and self.reveal_member_identities:
            raise ValueError("open blind review must hide member identities")
        expected = eval_suite_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("eval suite receipt hash mismatch")
        return self


class EvalSuiteBlindMemberView(EvalSuiteModel):
    member_token: MemberToken
    ordinal: int = Field(ge=1, le=32)
    present_artifact_kinds: list[NonEmptyText] = Field(default_factory=list, max_length=32)


class EvalSuiteResponse(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-response.v1"] = Field(
        default="zhimeng.eval-suite-response.v1",
        alias="schema",
    )
    receipt: EvalSuiteReceipt
    history: list[EvalSuiteReceipt] = Field(default_factory=list, max_length=32)
    blind_member_views: list[EvalSuiteBlindMemberView] = Field(
        default_factory=list,
        max_length=32,
    )
    not_claiming: tuple[str, ...] = (
        "formal_draft_authorization_unlock",
        "literary_quality_score",
        "training_dataset_ready",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def eval_suite_receipt_hash(receipt: EvalSuiteReceipt) -> str:
    material = {
        "schema": "zhimeng.eval-suite-receipt-hash.v1",
        "suite_id": receipt.suite_id,
        "title": receipt.title,
        "summary": receipt.summary,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "state_revision": receipt.state_revision,
        "members": [
            {
                "member_token": item.member_token,
                "project_id": item.project_id,
                "change_set_id": item.change_set_id,
                "baseline_receipt_id": item.baseline_receipt_id,
                "baseline_receipt_hash": item.baseline_receipt_hash,
                "export_file_sha256": item.export_file_sha256,
                "present_artifact_kinds": list(item.present_artifact_kinds),
                "label": item.label,
                "reveal_after_finalize": True,
            }
            for item in receipt.members
        ],
        "member_count": receipt.member_count,
        "frozen": receipt.frozen,
        "compare_against_suite_id": receipt.compare_against_suite_id,
        "compare_status": receipt.compare_status,
        "compare_checks": [
            {
                "code": item.code,
                "present": item.present,
                "detail": item.detail,
            }
            for item in receipt.compare_checks
        ],
        "blind_review_open": receipt.blind_review_open,
        "blind_review_finalized": receipt.blind_review_finalized,
        "blind_scores": [
            {
                "member_token": item.member_token,
                "score": item.score,
                "notes": item.notes,
            }
            for item in receipt.blind_scores
        ],
        "reveal_member_identities": receipt.reveal_member_identities,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "training_ready": False,
        "auto_upgrade": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "updated_at": receipt.updated_at.isoformat(),
    }
    return canonical_json_hash(material)




class EvalSuiteListItem(EvalSuiteModel):
    suite_id: SuiteId
    title: NonEmptyText = Field(max_length=200)
    status: EvalSuiteStatus
    member_count: int = Field(ge=2, le=32)
    frozen: bool
    blind_review_finalized: bool
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    updated_at: datetime
    receipt_hash: Sha256


class EvalSuiteListResponse(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-list-response.v1"] = Field(
        default="zhimeng.eval-suite-list-response.v1",
        alias="schema",
    )
    suites: list[EvalSuiteListItem] = Field(default_factory=list, max_length=128)
    not_claiming: tuple[str, ...] = (
        "formal_draft_authorization_unlock",
        "training_dataset_ready",
        "local_v1_exe",
    )


ReportId = Annotated[str, StringConstraints(pattern=r"^eval-suite-provider-fp-[0-9a-f]{32}$")]


class EvalSuiteProviderFingerprintMember(EvalSuiteModel):
    member_token: MemberToken
    project_id: ProjectId
    change_set_id: ChangeSetId
    origin: NonEmptyText = Field(max_length=64)
    source_run_id: str | None = Field(default=None, max_length=160)
    present: bool
    fingerprint: Sha256 | None = None
    requested_models: list[NonEmptyText] = Field(default_factory=list, max_length=32)
    reported_models: list[NonEmptyText] = Field(default_factory=list, max_length=32)
    model_identity_statuses: list[NonEmptyText] = Field(default_factory=list, max_length=32)
    provider_identity_status: NonEmptyText | None = Field(default=None, max_length=64)
    turn_count: int = Field(default=0, ge=0, le=32)
    evidence_kind: Literal[
        "provider_turn_receipts",
        "artifact_sidecar",
        "missing",
    ]
    detail: NonEmptyText = Field(max_length=1024)


class EvalSuiteProviderFingerprintReport(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-provider-fingerprint-report.v1"] = Field(
        default="zhimeng.eval-suite-provider-fingerprint-report.v1",
        alias="schema",
    )
    report_id: ReportId
    suite_id: SuiteId
    suite_receipt_hash: Sha256
    status: Literal["complete", "incomplete"]
    members: list[EvalSuiteProviderFingerprintMember] = Field(min_length=2, max_length=32)
    member_count: int = Field(ge=2, le=32)
    present_count: int = Field(ge=0, le=32)
    missing_member_tokens: list[MemberToken] = Field(default_factory=list, max_length=32)
    all_present: bool
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    training_ready: Literal[False] = False
    policy_version: Literal["local-v1-eval-suite-provider-fp-1"] = (
        "local-v1-eval-suite-provider-fp-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    generated_at: datetime
    report_hash: Sha256

    @model_validator(mode="after")
    def validate_report(self) -> "EvalSuiteProviderFingerprintReport":
        if self.formal_draft_allowed is not False:
            raise ValueError("provider fingerprint report cannot unlock formal draft")
        if self.quality_assessed is not False:
            raise ValueError("provider fingerprint report cannot claim quality assessed")
        if self.training_ready is not False:
            raise ValueError("provider fingerprint report cannot claim training ready")
        if self.member_count != len(self.members):
            raise ValueError("member_count must match members")
        if self.present_count != sum(1 for item in self.members if item.present):
            raise ValueError("present_count must match present members")
        expected_missing = [item.member_token for item in self.members if not item.present]
        if list(self.missing_member_tokens) != expected_missing:
            raise ValueError("missing_member_tokens must match absent members")
        if self.all_present is not (len(expected_missing) == 0):
            raise ValueError("all_present must match missing members")
        expected_status = "complete" if self.all_present else "incomplete"
        if self.status != expected_status:
            raise ValueError("status must match all_present")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        expected = eval_suite_provider_fingerprint_report_hash(self)
        if self.report_hash != expected:
            raise ValueError("provider fingerprint report hash mismatch")
        return self


class EvalSuiteProviderFingerprintResponse(EvalSuiteModel):
    schema_id: Literal["zhimeng.eval-suite-provider-fingerprint-response.v1"] = Field(
        default="zhimeng.eval-suite-provider-fingerprint-response.v1",
        alias="schema",
    )
    report: EvalSuiteProviderFingerprintReport
    not_claiming: tuple[str, ...] = (
        "formal_draft_authorization_unlock",
        "literary_quality_score",
        "training_dataset_ready",
        "quality_assessed",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def eval_suite_provider_fingerprint_report_hash(
    report: EvalSuiteProviderFingerprintReport,
) -> str:
    material = {
        "schema": "zhimeng.eval-suite-provider-fingerprint-report-hash.v1",
        "report_id": report.report_id,
        "suite_id": report.suite_id,
        "suite_receipt_hash": report.suite_receipt_hash,
        "status": report.status,
        "members": [
            {
                "member_token": item.member_token,
                "project_id": item.project_id,
                "change_set_id": item.change_set_id,
                "origin": item.origin,
                "source_run_id": item.source_run_id,
                "present": item.present,
                "fingerprint": item.fingerprint,
                "requested_models": list(item.requested_models),
                "reported_models": list(item.reported_models),
                "model_identity_statuses": list(item.model_identity_statuses),
                "provider_identity_status": item.provider_identity_status,
                "turn_count": item.turn_count,
                "evidence_kind": item.evidence_kind,
                "detail": item.detail,
            }
            for item in report.members
        ],
        "member_count": report.member_count,
        "present_count": report.present_count,
        "missing_member_tokens": list(report.missing_member_tokens),
        "all_present": report.all_present,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "training_ready": False,
        "policy_version": report.policy_version,
        "detail": report.detail,
        "generated_at": report.generated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "EvalSuiteAction",
    "EvalSuiteListItem",
    "EvalSuiteListResponse",
    "EvalSuiteActionRequest",
    "EvalSuiteBlindMemberView",
    "EvalSuiteBlindReviewItem",
    "EvalSuiteBlindScore",
    "EvalSuiteCompareCheck",
    "EvalSuiteMemberRecord",
    "EvalSuiteMemberRequest",
    "EvalSuitePrepareRequest",
    "EvalSuiteProviderFingerprintMember",
    "EvalSuiteProviderFingerprintReport",
    "EvalSuiteProviderFingerprintResponse",
    "EvalSuiteReceipt",
    "EvalSuiteResponse",
    "EvalSuiteStatus",
    "eval_suite_provider_fingerprint_report_hash",
    "eval_suite_receipt_hash",
]
