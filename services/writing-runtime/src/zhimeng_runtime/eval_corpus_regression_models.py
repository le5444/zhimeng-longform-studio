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
EvalCorpusBaselineReceiptId = Annotated[
    str, StringConstraints(pattern=r"^eval-corpus-baseline-[0-9a-f]{32}$")
]
EvalCorpusRegressionReceiptId = Annotated[
    str, StringConstraints(pattern=r"^eval-corpus-regression-[0-9a-f]{32}$")
]


class EvalCorpusRegressionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EvalCorpusBaselineFreezeRequest(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-baseline-request.v1"] = Field(
        default="zhimeng.eval-corpus-baseline-request.v1",
        alias="schema",
    )
    expected_export_file_sha256: Sha256 | None = None
    label: NonEmptyText | None = Field(default=None, max_length=120)


class EvalCorpusBaselineReceipt(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-baseline-receipt.v1"] = Field(
        default="zhimeng.eval-corpus-baseline-receipt.v1",
        alias="schema",
    )
    receipt_id: EvalCorpusBaselineReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    baseline_version: int = Field(ge=1, le=1_000_000)
    source_export_receipt_id: NonEmptyText = Field(max_length=160)
    source_export_file_sha256: Sha256
    package_schema: Literal["zhimeng.eval-corpus-package.v1"] = (
        "zhimeng.eval-corpus-package.v1"
    )
    present_artifact_kinds: list[NonEmptyText] = Field(min_length=1, max_length=64)
    artifact_count: int = Field(ge=1, le=64)
    label: str | None = Field(default=None, max_length=120)
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    formal_knowledge_contaminated: Literal[False] = False
    training_ready: Literal[False] = False
    policy_version: Literal["local-v1-eval-corpus-baseline-1"] = (
        "local-v1-eval-corpus-baseline-1"
    )
    frozen_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "EvalCorpusBaselineReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("eval corpus baseline cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("eval corpus baseline cannot claim quality assessed")
        if self.formal_knowledge_contaminated is not False:
            raise ValueError("eval corpus baseline cannot contaminate formal knowledge")
        if self.training_ready is not False:
            raise ValueError("eval corpus baseline cannot claim training ready")
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
            raise ValueError("frozen_at must be timezone-aware")
        if self.artifact_count != len(self.present_artifact_kinds):
            raise ValueError("artifact_count must match present_artifact_kinds length")
        expected = eval_corpus_baseline_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("eval corpus baseline receipt hash mismatch")
        return self


class EvalCorpusBaselineResponse(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-baseline-response.v1"] = Field(
        default="zhimeng.eval-corpus-baseline-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: EvalCorpusBaselineReceipt | None = None
    history: list[EvalCorpusBaselineReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "training_dataset_ready",
        "literary_quality_score",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


class EvalCorpusRegressionStatus(StrEnum):
    NON_DEGRADED = "non_degraded"
    DEGRADED = "degraded"
    INCOMPLETE = "incomplete"


class EvalCorpusRegressionCheckCode(StrEnum):
    BASELINE_PRESENT = "baseline_present"
    CURRENT_EXPORT_PRESENT = "current_export_present"
    PACKAGE_SCHEMA_COMPATIBLE = "package_schema_compatible"
    PRESENT_KINDS_SUPERSET = "present_kinds_superset"
    LOCKS_INTACT = "locks_intact"
    HARD_FACT_BLOCK_NON_INCREASE = "hard_fact_block_non_increase"
    LITERARY_HARD_FACT_NON_INCREASE = "literary_hard_fact_non_increase"
    MIN_SAMPLE_ARTIFACTS = "min_sample_artifacts"


class EvalCorpusRegressionEvaluateRequest(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-regression-request.v1"] = Field(
        default="zhimeng.eval-corpus-regression-request.v1",
        alias="schema",
    )
    expected_baseline_receipt_id: NonEmptyText | None = Field(default=None, max_length=160)
    expected_export_file_sha256: Sha256 | None = None


class EvalCorpusRegressionCheck(EvalCorpusRegressionModel):
    code: EvalCorpusRegressionCheckCode
    required: bool
    present: bool
    detail: NonEmptyText = Field(max_length=1024)


class EvalCorpusRegressionReceipt(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-regression-receipt.v1"] = Field(
        default="zhimeng.eval-corpus-regression-receipt.v1",
        alias="schema",
    )
    receipt_id: EvalCorpusRegressionReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    status: EvalCorpusRegressionStatus
    baseline_receipt_id: NonEmptyText | None = Field(default=None, max_length=160)
    baseline_version: int | None = Field(default=None, ge=1, le=1_000_000)
    baseline_export_file_sha256: Sha256 | None = None
    current_export_receipt_id: NonEmptyText | None = Field(default=None, max_length=160)
    current_export_file_sha256: Sha256 | None = None
    checks: list[EvalCorpusRegressionCheck] = Field(min_length=1, max_length=32)
    missing_required: list[EvalCorpusRegressionCheckCode] = Field(
        default_factory=list,
        max_length=32,
    )
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    formal_knowledge_contaminated: Literal[False] = False
    training_ready: Literal[False] = False
    policy_version: Literal["local-v1-eval-corpus-regression-1"] = (
        "local-v1-eval-corpus-regression-1"
    )
    evaluated_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "EvalCorpusRegressionReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("eval corpus regression cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("eval corpus regression cannot claim quality assessed")
        if self.formal_knowledge_contaminated is not False:
            raise ValueError("eval corpus regression cannot contaminate formal knowledge")
        if self.training_ready is not False:
            raise ValueError("eval corpus regression cannot claim training ready")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        required_missing = [
            item.code for item in self.checks if item.required and not item.present
        ]
        if list(self.missing_required) != required_missing:
            raise ValueError("missing_required must match required absent checks")
        if self.status is EvalCorpusRegressionStatus.NON_DEGRADED and required_missing:
            raise ValueError("non_degraded status requires all required checks present")
        if self.status is EvalCorpusRegressionStatus.DEGRADED and not required_missing:
            raise ValueError("degraded status requires at least one missing required check")
        expected = eval_corpus_regression_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("eval corpus regression receipt hash mismatch")
        return self


class EvalCorpusRegressionResponse(EvalCorpusRegressionModel):
    schema_id: Literal["zhimeng.eval-corpus-regression-response.v1"] = Field(
        default="zhimeng.eval-corpus-regression-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: EvalCorpusRegressionReceipt | None = None
    history: list[EvalCorpusRegressionReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "literary_quality_score",
        "training_dataset_ready",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "local_v1_exe",
    )


def eval_corpus_baseline_receipt_hash(receipt: EvalCorpusBaselineReceipt) -> str:
    material = {
        "schema": "zhimeng.eval-corpus-baseline-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "baseline_version": receipt.baseline_version,
        "source_export_receipt_id": receipt.source_export_receipt_id,
        "source_export_file_sha256": receipt.source_export_file_sha256,
        "package_schema": receipt.package_schema,
        "present_artifact_kinds": list(receipt.present_artifact_kinds),
        "artifact_count": receipt.artifact_count,
        "label": receipt.label,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "formal_knowledge_contaminated": False,
        "training_ready": False,
        "policy_version": receipt.policy_version,
        "frozen_at": receipt.frozen_at.isoformat(),
    }
    return canonical_json_hash(material)


def eval_corpus_regression_receipt_hash(receipt: EvalCorpusRegressionReceipt) -> str:
    material = {
        "schema": "zhimeng.eval-corpus-regression-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "status": (
            receipt.status.value if hasattr(receipt.status, "value") else receipt.status
        ),
        "baseline_receipt_id": receipt.baseline_receipt_id,
        "baseline_version": receipt.baseline_version,
        "baseline_export_file_sha256": receipt.baseline_export_file_sha256,
        "current_export_receipt_id": receipt.current_export_receipt_id,
        "current_export_file_sha256": receipt.current_export_file_sha256,
        "checks": [
            {
                "code": item.code.value if hasattr(item.code, "value") else item.code,
                "required": item.required,
                "present": item.present,
                "detail": item.detail,
            }
            for item in receipt.checks
        ],
        "missing_required": [
            item.value if hasattr(item, "value") else item
            for item in receipt.missing_required
        ],
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "formal_knowledge_contaminated": False,
        "training_ready": False,
        "policy_version": receipt.policy_version,
        "evaluated_at": receipt.evaluated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "EvalCorpusBaselineFreezeRequest",
    "EvalCorpusBaselineReceipt",
    "EvalCorpusBaselineResponse",
    "EvalCorpusRegressionCheck",
    "EvalCorpusRegressionCheckCode",
    "EvalCorpusRegressionEvaluateRequest",
    "EvalCorpusRegressionReceipt",
    "EvalCorpusRegressionResponse",
    "EvalCorpusRegressionStatus",
    "eval_corpus_baseline_receipt_hash",
    "eval_corpus_regression_receipt_hash",
]
