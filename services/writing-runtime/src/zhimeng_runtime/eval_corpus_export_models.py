from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
ChangeSetId = Annotated[str, StringConstraints(pattern=r"^changeset-[0-9a-f]{32}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
EvalCorpusExportReceiptId = Annotated[
    str, StringConstraints(pattern=r"^eval-corpus-export-[0-9a-f]{32}$")
]


class EvalCorpusExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EvalCorpusExportRequest(EvalCorpusExportModel):
    schema_id: Literal["zhimeng.eval-corpus-export-request.v1"] = Field(
        default="zhimeng.eval-corpus-export-request.v1",
        alias="schema",
    )
    include_feedback_experience: bool = True
    include_formal_knowledge_promotion: bool = True
    include_gates: bool = True
    include_writeback: bool = True


class EvalCorpusArtifactRef(EvalCorpusExportModel):
    kind: Literal[
        "hard_fact_gate",
        "literary_quality_gate",
        "gate_pipeline",
        "feedback_candidate_bundle",
        "feedback_candidate_promotion",
        "formal_knowledge_promotion",
        "writeback_evidence",
        "formal_draft_authorization",
        "reader_review",
        "reader_review_decision",
    ]
    present: bool
    relative_artifact: str | None = Field(default=None, max_length=512)
    content_sha256: Sha256 | None = None
    desensitized: Literal[True] = True


class EvalCorpusExportReceipt(EvalCorpusExportModel):
    schema_id: Literal["zhimeng.eval-corpus-export-receipt.v1"] = Field(
        default="zhimeng.eval-corpus-export-receipt.v1",
        alias="schema",
    )
    receipt_id: EvalCorpusExportReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back", "ready_candidate"]
    export_relative_path: NonEmptyText = Field(max_length=512)
    export_file_sha256: Sha256
    artifact_count: int = Field(ge=0, le=64)
    artifacts: list[EvalCorpusArtifactRef] = Field(default_factory=list, max_length=64)
    redaction_policy: Literal["local-v1-eval-corpus-redact-1"] = (
        "local-v1-eval-corpus-redact-1"
    )
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    formal_knowledge_contaminated: Literal[False] = False
    training_ready: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "EvalCorpusExportReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("eval corpus export cannot allow formal draft")
        if self.quality_assessed is not False:
            raise ValueError("eval corpus export cannot claim quality assessed")
        if self.formal_knowledge_contaminated is not False:
            raise ValueError("eval corpus export cannot contaminate formal knowledge")
        if self.training_ready is not False:
            raise ValueError("eval corpus export is not training-ready by default")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.artifact_count != len(self.artifacts):
            raise ValueError("artifact_count must match artifacts length")
        expected = eval_corpus_export_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("eval corpus export receipt hash mismatch")
        return self


class EvalCorpusExportResponse(EvalCorpusExportModel):
    schema_id: Literal["zhimeng.eval-corpus-export-response.v1"] = Field(
        default="zhimeng.eval-corpus-export-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: EvalCorpusExportReceipt | None = None
    history: list[EvalCorpusExportReceipt] = Field(default_factory=list, max_length=32)
    package: dict[str, Any] | None = None
    not_claiming: tuple[str, ...] = (
        "training_dataset_ready",
        "formal_knowledge_auto_activation",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def eval_corpus_export_receipt_hash(receipt: EvalCorpusExportReceipt) -> str:
    material = {
        "schema": "zhimeng.eval-corpus-export-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "change_set_id": receipt.change_set_id,
        "project_id": receipt.project_id,
        "outcome": receipt.outcome,
        "export_relative_path": receipt.export_relative_path,
        "export_file_sha256": receipt.export_file_sha256,
        "artifact_count": receipt.artifact_count,
        "artifacts": [
            {
                "kind": item.kind,
                "present": item.present,
                "relative_artifact": item.relative_artifact,
                "content_sha256": item.content_sha256,
                "desensitized": True,
            }
            for item in receipt.artifacts
        ],
        "redaction_policy": receipt.redaction_policy,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "formal_knowledge_contaminated": False,
        "training_ready": False,
        "created_at": receipt.created_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "EvalCorpusArtifactRef",
    "EvalCorpusExportReceipt",
    "EvalCorpusExportRequest",
    "EvalCorpusExportResponse",
    "eval_corpus_export_receipt_hash",
]
