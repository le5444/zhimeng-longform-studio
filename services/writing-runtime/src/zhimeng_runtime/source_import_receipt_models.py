from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from zhimeng_runtime.source_import_models import (
        LocalSourceImportJob,
        SourceImportJob,
    )


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SourceImportReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SourceImportLineageStatus(StrEnum):
    VERIFIED = "verified"
    LEGACY_UNVERIFIED = "legacy_unverified"


class SourceImportRequestEvidence(SourceImportReceiptModel):
    query: str = Field(min_length=1, max_length=500, strict=True)
    candidate_index: int = Field(ge=1, strict=True)
    book_id: str = Field(pattern=r"^[0-9]{1,32}$", strict=True)
    title: str = Field(min_length=1, max_length=500, strict=True)
    author: str = Field(min_length=1, max_length=300, strict=True)

    @field_validator("query", "title", "author")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("source import request evidence must not be blank")
        return normalized


class LocalSourceImportRequestEvidence(SourceImportReceiptModel):
    source_format: Literal["txt", "epub"] = "txt"
    source_locator_hash: Sha256
    title: str = Field(min_length=1, max_length=500, strict=True)
    author: str = Field(min_length=1, max_length=300, strict=True)
    source_size_bytes: int = Field(ge=1, strict=True)
    source_sha256: Sha256

    @field_validator("title", "author")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("local source request evidence must not be blank")
        return normalized


AnySourceImportRequestEvidence: TypeAlias = (
    SourceImportRequestEvidence | LocalSourceImportRequestEvidence
)


class SourceImportToolEvidence(SourceImportReceiptModel):
    tool_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9._-]*$",
        strict=True,
    )
    tool_version: str = Field(min_length=1, max_length=128, strict=True)
    tool_fingerprint: Sha256

    @field_validator("tool_version")
    @classmethod
    def normalize_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source import tool version must not be blank")
        return normalized


class SourceImportAdapterExecutionIdentity(SourceImportReceiptModel):
    schema_id: Literal["zhimeng.source-import-adapter-identity.v1"] = Field(
        default="zhimeng.source-import-adapter-identity.v1",
        alias="schema",
    )
    prepare_tool: SourceImportToolEvidence
    distillation_tool: SourceImportToolEvidence


class SourceImportArtifactEvidence(SourceImportReceiptModel):
    kind: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
        strict=True,
    )
    relative_path: str = Field(min_length=1, max_length=4096, strict=True)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: Sha256

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ":" in normalized
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("receipt artifact paths must be safe logical relative paths")
        return path.as_posix()


class SourceImportExecutionReceipt(SourceImportReceiptModel):
    schema_id: Literal["zhimeng.source-import-execution-receipt.v1"] = Field(
        default="zhimeng.source-import-execution-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^source-import-run-[0-9a-f]{32}$")
    revision: Literal[1]
    idempotency_key: str = Field(
        pattern=r"^source-import-idempotency-[0-9a-f]{32}$"
    )
    lineage_status: Literal["verified"] = "verified"
    provider: Literal["tomato"] = "tomato"
    request: SourceImportRequestEvidence
    request_hash: Sha256
    retry_of: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    prepare_invocation_id: str = Field(
        pattern=r"^source-import-invocation-[0-9a-f]{32}$"
    )
    prepare_tool: SourceImportToolEvidence
    prepare_input_hash: Sha256
    prepare_output_hash: Sha256
    download_scope: Literal["full"] = "full"
    source_packet_fingerprint: Sha256
    packet_manifest_hash: Sha256
    packet_text_hash: Sha256
    packet_metadata_hash: Sha256
    source_artifacts: tuple[SourceImportArtifactEvidence, ...] = Field(
        min_length=5,
        max_length=32,
    )
    distillation_invocation_id: str = Field(
        pattern=r"^source-import-invocation-[0-9a-f]{32}$"
    )
    distillation_tool: SourceImportToolEvidence
    distillation_input_hash: Sha256
    distillation_output_hash: Sha256
    draft_fingerprint: Sha256
    cards_draft_hash: Sha256
    envelope_hash: Sha256
    envelope_schema: Literal["novel_distillation.source_packet_draft.v1"]
    envelope_status: Literal["draft_ready"]
    card_count: int = Field(ge=1, le=64, strict=True)
    draft_artifacts: tuple[SourceImportArtifactEvidence, ...] = Field(
        min_length=4,
        max_length=32,
    )
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_integrity(self) -> "SourceImportExecutionReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("source import receipt created_at must be timezone-aware")
        if self.prepare_invocation_id == self.distillation_invocation_id:
            raise ValueError("source import stage invocation IDs must be distinct")
        expected_receipt_id = source_import_receipt_id(self.source_import_id)
        if self.receipt_id != expected_receipt_id:
            raise ValueError("source import receipt ID does not match its import")
        if self.run_id != source_import_run_id(self.source_import_id):
            raise ValueError("source import run ID does not match its import")
        if self.idempotency_key != source_import_idempotency_key(
            self.run_id,
            self.revision,
            self.request_hash,
        ):
            raise ValueError("source import idempotency key does not match its execution")
        _validate_artifact_order(self.source_artifacts, label="source")
        _validate_artifact_order(self.draft_artifacts, label="draft")
        source_by_kind = {item.kind: item for item in self.source_artifacts}
        required_source = {
            "packet_manifest",
            "packet_epub",
            "packet_text",
            "packet_metadata",
            "downloaded_epub",
        }
        if not required_source.issubset(source_by_kind):
            raise ValueError("source import receipt is missing required source artifacts")
        draft_by_kind = {item.kind: item for item in self.draft_artifacts}
        required_draft = {
            "cards_draft_json",
            "distillation_report_md",
            "validation_report_md",
            "tool_result_envelope_json",
        }
        if not required_draft.issubset(draft_by_kind):
            raise ValueError("source import receipt is missing required draft artifacts")
        if self.packet_manifest_hash != source_by_kind["packet_manifest"].sha256:
            raise ValueError("packet manifest hash does not match source artifacts")
        if self.packet_text_hash != source_by_kind["packet_text"].sha256:
            raise ValueError("packet text hash does not match source artifacts")
        if self.packet_metadata_hash != source_by_kind["packet_metadata"].sha256:
            raise ValueError("packet metadata hash does not match source artifacts")
        if (
            source_by_kind["downloaded_epub"].sha256
            != source_by_kind["packet_epub"].sha256
            or source_by_kind["downloaded_epub"].size_bytes
            != source_by_kind["packet_epub"].size_bytes
        ):
            raise ValueError(
                "Runtime packet EPUB must be an exact snapshot of the downloaded EPUB"
            )
        if self.cards_draft_hash != draft_by_kind["cards_draft_json"].sha256:
            raise ValueError("cards draft hash does not match draft artifacts")
        if self.envelope_hash != draft_by_kind["tool_result_envelope_json"].sha256:
            raise ValueError("envelope hash does not match draft artifacts")
        if self.request_hash != source_import_request_hash(self.request):
            raise ValueError("source import request hash does not match request evidence")
        if self.prepare_input_hash != source_import_prepare_input_hash(
            self.provider,
            self.request_hash,
        ):
            raise ValueError("source import prepare input hash does not match")
        if self.source_packet_fingerprint != source_import_packet_fingerprint(
            self.request_hash,
            self.download_scope,
            self.source_artifacts,
        ):
            raise ValueError("source packet fingerprint does not match source artifacts")
        if self.prepare_output_hash != source_import_prepare_output_hash(
            self.source_packet_fingerprint,
            self.packet_manifest_hash,
            self.packet_text_hash,
            self.packet_metadata_hash,
        ):
            raise ValueError("source import prepare output hash does not match")
        if self.distillation_input_hash != source_import_distillation_input_hash(
            self.source_packet_fingerprint,
            self.packet_manifest_hash,
            self.packet_text_hash,
            self.packet_metadata_hash,
        ):
            raise ValueError("distillation input hash does not match source packet")
        if self.draft_fingerprint != source_import_draft_fingerprint(
            self.distillation_input_hash,
            self.distillation_tool.tool_fingerprint,
            self.draft_artifacts,
        ):
            raise ValueError("distillation draft fingerprint does not match artifacts")
        if self.distillation_output_hash != source_import_distillation_output_hash(
            self.draft_fingerprint,
            self.cards_draft_hash,
            self.envelope_hash,
            self.card_count,
        ):
            raise ValueError("distillation output hash does not match draft evidence")
        if self.receipt_hash != source_import_execution_receipt_hash(self):
            raise ValueError("source import execution receipt hash does not match")
        return self


class LocalSourceImportExecutionReceipt(SourceImportReceiptModel):
    schema_id: Literal["zhimeng.source-import-execution-receipt.v2"] = Field(
        default="zhimeng.source-import-execution-receipt.v2",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^source-import-run-[0-9a-f]{32}$")
    revision: Literal[1]
    idempotency_key: str = Field(
        pattern=r"^source-import-idempotency-[0-9a-f]{32}$"
    )
    lineage_status: Literal["verified"] = "verified"
    provider: Literal["local_file"] = "local_file"
    request: LocalSourceImportRequestEvidence
    request_hash: Sha256
    retry_of: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    prepare_invocation_id: str = Field(
        pattern=r"^source-import-invocation-[0-9a-f]{32}$"
    )
    prepare_tool: SourceImportToolEvidence
    prepare_input_hash: Sha256
    prepare_output_hash: Sha256
    download_scope: Literal["full"] = "full"
    source_format: Literal["txt", "epub"] = "txt"
    source_locator_hash: Sha256
    source_file_size_bytes: int = Field(ge=1, strict=True)
    source_file_sha256: Sha256
    source_packet_fingerprint: Sha256
    packet_manifest_hash: Sha256
    packet_source_hash: Sha256
    packet_text_hash: Sha256
    packet_metadata_hash: Sha256
    packet_source_size_bytes: int | None = Field(default=None, ge=1, strict=True)
    packet_text_size_bytes: int | None = Field(default=None, ge=1, strict=True)
    source_artifacts: tuple[SourceImportArtifactEvidence, ...] = Field(
        min_length=4,
        max_length=32,
    )
    distillation_invocation_id: str = Field(
        pattern=r"^source-import-invocation-[0-9a-f]{32}$"
    )
    distillation_tool: SourceImportToolEvidence
    distillation_input_hash: Sha256
    distillation_output_hash: Sha256
    draft_fingerprint: Sha256
    cards_draft_hash: Sha256
    envelope_hash: Sha256
    envelope_schema: Literal["novel_distillation.source_packet_draft.v1"]
    envelope_status: Literal["draft_ready"]
    card_count: int = Field(ge=1, le=64, strict=True)
    draft_artifacts: tuple[SourceImportArtifactEvidence, ...] = Field(
        min_length=4,
        max_length=32,
    )
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_integrity(self) -> "LocalSourceImportExecutionReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("source import receipt created_at must be timezone-aware")
        if self.prepare_invocation_id == self.distillation_invocation_id:
            raise ValueError("source import stage invocation IDs must be distinct")
        if self.receipt_id != source_import_receipt_id(self.source_import_id):
            raise ValueError("source import receipt ID does not match its import")
        if self.run_id != source_import_run_id(self.source_import_id):
            raise ValueError("source import run ID does not match its import")
        if self.idempotency_key != source_import_idempotency_key(
            self.run_id,
            self.revision,
            self.request_hash,
        ):
            raise ValueError("source import idempotency key does not match its execution")
        _validate_artifact_order(self.source_artifacts, label="source")
        _validate_artifact_order(self.draft_artifacts, label="draft")
        source_by_kind = {item.kind: item for item in self.source_artifacts}
        required_source = {
            "packet_manifest",
            "packet_source",
            "packet_text",
            "packet_metadata",
        }
        if not required_source.issubset(source_by_kind):
            raise ValueError("local source receipt is missing required artifacts")
        draft_by_kind = {item.kind: item for item in self.draft_artifacts}
        required_draft = {
            "cards_draft_json",
            "distillation_report_md",
            "validation_report_md",
            "tool_result_envelope_json",
        }
        if not required_draft.issubset(draft_by_kind):
            raise ValueError("source import receipt is missing required draft artifacts")
        if self.packet_manifest_hash != source_by_kind["packet_manifest"].sha256:
            raise ValueError("packet manifest hash does not match source artifacts")
        if self.packet_source_hash != source_by_kind["packet_source"].sha256:
            raise ValueError("packet source hash does not match source artifacts")
        if self.packet_text_hash != source_by_kind["packet_text"].sha256:
            raise ValueError("packet text hash does not match source artifacts")
        if self.packet_metadata_hash != source_by_kind["packet_metadata"].sha256:
            raise ValueError("packet metadata hash does not match source artifacts")
        if not (
            self.source_file_sha256 == self.packet_source_hash
            and self.source_file_size_bytes
            == source_by_kind["packet_source"].size_bytes
        ):
            raise ValueError("Runtime packet source must preserve the selected source bytes")
        if not source_by_kind["packet_text"].size_bytes:
            raise ValueError("Runtime packet text must not be empty")
        if self.packet_source_size_bytes is not None and (
            self.packet_source_size_bytes
            != source_by_kind["packet_source"].size_bytes
        ):
            raise ValueError("packet source size does not match source artifacts")
        if self.packet_text_size_bytes is not None and (
            self.packet_text_size_bytes != source_by_kind["packet_text"].size_bytes
        ):
            raise ValueError("packet text size does not match source artifacts")
        if self.source_format == "epub" and (
            self.packet_source_size_bytes is None
            or self.packet_text_size_bytes is None
        ):
            raise ValueError("current EPUB receipts require independent packet sizes")
        if (
            self.source_format == "epub"
            and self.packet_source_hash == self.packet_text_hash
        ):
            raise ValueError("EPUB source and extracted text hashes must be distinct")
        if self.source_format == "txt" and (
            self.source_file_sha256 != self.packet_text_hash
            or self.source_file_size_bytes
            != source_by_kind["packet_text"].size_bytes
        ):
            raise ValueError("Runtime TXT packet must preserve selected source bytes")
        if (
            self.request.source_format != self.source_format
            or self.request.source_locator_hash != self.source_locator_hash
            or self.request.source_size_bytes != self.source_file_size_bytes
            or self.request.source_sha256 != self.source_file_sha256
        ):
            raise ValueError("local source receipt does not match its request evidence")
        if self.cards_draft_hash != draft_by_kind["cards_draft_json"].sha256:
            raise ValueError("cards draft hash does not match draft artifacts")
        if self.envelope_hash != draft_by_kind["tool_result_envelope_json"].sha256:
            raise ValueError("envelope hash does not match draft artifacts")
        if self.request_hash != source_import_request_hash(self.request):
            raise ValueError("source import request hash does not match request evidence")
        if self.prepare_input_hash != source_import_prepare_input_hash(
            self.provider,
            self.request_hash,
        ):
            raise ValueError("source import prepare input hash does not match")
        if self.source_packet_fingerprint != source_import_packet_fingerprint(
            self.request_hash,
            self.download_scope,
            self.source_artifacts,
        ):
            raise ValueError("source packet fingerprint does not match source artifacts")
        if self.prepare_output_hash != source_import_prepare_output_hash(
            self.source_packet_fingerprint,
            self.packet_manifest_hash,
            self.packet_text_hash,
            self.packet_metadata_hash,
        ):
            raise ValueError("source import prepare output hash does not match")
        if self.distillation_input_hash != source_import_distillation_input_hash(
            self.source_packet_fingerprint,
            self.packet_manifest_hash,
            self.packet_text_hash,
            self.packet_metadata_hash,
        ):
            raise ValueError("distillation input hash does not match source packet")
        if self.draft_fingerprint != source_import_draft_fingerprint(
            self.distillation_input_hash,
            self.distillation_tool.tool_fingerprint,
            self.draft_artifacts,
        ):
            raise ValueError("distillation draft fingerprint does not match artifacts")
        if self.distillation_output_hash != source_import_distillation_output_hash(
            self.draft_fingerprint,
            self.cards_draft_hash,
            self.envelope_hash,
            self.card_count,
        ):
            raise ValueError("distillation output hash does not match draft evidence")
        if self.receipt_hash != source_import_execution_receipt_hash(self):
            raise ValueError("source import execution receipt hash does not match")
        return self


AnySourceImportExecutionReceipt: TypeAlias = (
    SourceImportExecutionReceipt | LocalSourceImportExecutionReceipt
)


class SourceImportExecutionReceiptRecord(SourceImportReceiptModel):
    schema_id: Literal["zhimeng.source-import-execution-receipt-record.v1"] = Field(
        default="zhimeng.source-import-execution-receipt-record.v1",
        alias="schema",
    )
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    lineage_status: SourceImportLineageStatus
    receipt: SourceImportExecutionReceipt | LocalSourceImportExecutionReceipt | None = None

    @model_validator(mode="after")
    def validate_record(self) -> "SourceImportExecutionReceiptRecord":
        if self.lineage_status is SourceImportLineageStatus.VERIFIED:
            if self.receipt is None or self.receipt.source_import_id != self.source_import_id:
                raise ValueError("verified source import lineage requires its execution receipt")
        elif self.receipt is not None:
            raise ValueError("legacy-unverified source imports cannot carry a verified receipt")
        return self


class SourceImportAttemptReceipt(SourceImportReceiptModel):
    schema_id: Literal["zhimeng.source-import-attempt-receipt.v1"] = Field(
        default="zhimeng.source-import-attempt-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^source-import-attempt-[0-9a-f]{32}$")
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    attempt_ordinal: Literal[1] = 1
    status: Literal["success", "failed", "cancelled", "interrupted"]
    stage: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
        strict=True,
    )
    provider: Literal["tomato", "local_file"]
    request_hash: Sha256 | None = None
    execution_receipt_id: str | None = Field(
        default=None,
        pattern=r"^source-import-receipt-[0-9a-f]{32}$",
    )
    execution_receipt_hash: Sha256 | None = None
    prepare_tool_fingerprint: Sha256 | None = None
    distillation_tool_fingerprint: Sha256 | None = None
    failure_signature: Sha256 | None = None
    cleanup_status: Literal["clean", "partial", "not_required"]
    cleanup_hash: Sha256
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_attempt(self) -> "SourceImportAttemptReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("source import attempt time must be timezone-aware")
        if self.receipt_id != source_import_attempt_receipt_id(
            self.source_import_id,
            self.attempt_ordinal,
        ):
            raise ValueError("source import attempt receipt ID does not match its attempt")
        if self.status == "success":
            if self.stage != "awaiting_review":
                raise ValueError("successful source import attempts end at awaiting_review")
            if (
                self.request_hash is None
                or self.execution_receipt_id is None
                or self.execution_receipt_hash is None
                or self.prepare_tool_fingerprint is None
                or self.distillation_tool_fingerprint is None
            ):
                raise ValueError("successful source import attempts require complete lineage")
            if self.execution_receipt_id != source_import_receipt_id(
                self.source_import_id
            ):
                raise ValueError("attempt success references another execution receipt")
            if self.failure_signature is not None:
                raise ValueError("successful source import attempts cannot carry failure evidence")
            if self.cleanup_status != "not_required":
                raise ValueError(
                    "successful source import attempts do not clean accepted artifacts"
                )
        else:
            if self.execution_receipt_id is not None or self.execution_receipt_hash is not None:
                raise ValueError("unsuccessful attempts cannot claim an execution receipt")
            if self.failure_signature is None:
                raise ValueError("unsuccessful source import attempts require a failure signature")
            if self.stage != "queued" and self.request_hash is None:
                raise ValueError("post-queue failures require their request hash")

        prepare_stages = {
            "downloading",
            "preparing",
            "source_ready",
            "drafting",
            "distilling",
            "awaiting_review",
            "formalizing",
            "completed",
        }
        distillation_stages = {
            "drafting",
            "distilling",
            "awaiting_review",
            "formalizing",
            "completed",
        }
        if self.stage in prepare_stages and self.prepare_tool_fingerprint is None:
            raise ValueError("source import attempt stage requires prepare tool evidence")
        if self.stage in distillation_stages and self.distillation_tool_fingerprint is None:
            raise ValueError("source import attempt stage requires distillation tool evidence")
        if self.receipt_hash != source_import_attempt_receipt_hash(self):
            raise ValueError("source import attempt receipt hash does not match")
        return self


def canonical_json_hash(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_import_request_hash(request: AnySourceImportRequestEvidence) -> str:
    schema = (
        "zhimeng.local-source-import-request-evidence.v1"
        if isinstance(request, LocalSourceImportRequestEvidence)
        else "zhimeng.source-import-request-evidence.v1"
    )
    return canonical_json_hash(
        {
            "schema": schema,
            **request.model_dump(mode="json"),
        }
    )


def source_import_prepare_input_hash(provider: str, request_hash: str) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-prepare-input.v1",
            "provider": provider,
            "request_hash": request_hash,
        }
    )


def _artifact_identity(
    artifacts: tuple[SourceImportArtifactEvidence, ...],
) -> list[dict[str, object]]:
    return [
        {
            "kind": item.kind,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        for item in artifacts
    ]


def source_import_packet_fingerprint(
    request_hash: str,
    download_scope: str,
    artifacts: tuple[SourceImportArtifactEvidence, ...],
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-packet-fingerprint.v1",
            "request_hash": request_hash,
            "download_scope": download_scope,
            "artifacts": _artifact_identity(artifacts),
        }
    )


def source_import_prepare_output_hash(
    source_packet_fingerprint: str,
    packet_manifest_hash: str,
    packet_text_hash: str,
    packet_metadata_hash: str,
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-prepare-output.v1",
            "source_packet_fingerprint": source_packet_fingerprint,
            "packet_manifest_hash": packet_manifest_hash,
            "packet_text_hash": packet_text_hash,
            "packet_metadata_hash": packet_metadata_hash,
        }
    )


def source_import_distillation_input_hash(
    source_packet_fingerprint: str,
    packet_manifest_hash: str,
    packet_text_hash: str,
    packet_metadata_hash: str,
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-distillation-input.v1",
            "source_packet_fingerprint": source_packet_fingerprint,
            "packet_manifest_hash": packet_manifest_hash,
            "packet_text_hash": packet_text_hash,
            "packet_metadata_hash": packet_metadata_hash,
        }
    )


def source_import_draft_fingerprint(
    distillation_input_hash: str,
    distillation_tool_fingerprint: str,
    artifacts: tuple[SourceImportArtifactEvidence, ...],
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-draft-fingerprint.v1",
            "distillation_input_hash": distillation_input_hash,
            "distillation_tool_fingerprint": distillation_tool_fingerprint,
            "artifacts": _artifact_identity(artifacts),
        }
    )


def source_import_distillation_output_hash(
    draft_fingerprint: str,
    cards_draft_hash: str,
    envelope_hash: str,
    card_count: int,
) -> str:
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-distillation-output.v1",
            "draft_fingerprint": draft_fingerprint,
            "cards_draft_hash": cards_draft_hash,
            "envelope_hash": envelope_hash,
            "card_count": card_count,
        }
    )


def source_import_receipt_id(source_import_id: str) -> str:
    digest = hashlib.sha256(
        f"zhimeng.source-import-execution-receipt.v1\0{source_import_id}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"source-import-receipt-{digest[:32]}"


def source_import_run_id(source_import_id: str) -> str:
    digest = hashlib.sha256(
        f"zhimeng.source-import-run.v1\0{source_import_id}".encode("utf-8")
    ).hexdigest()
    return f"source-import-run-{digest[:32]}"


def source_import_idempotency_key(
    run_id: str,
    revision: int,
    request_hash: str,
) -> str:
    digest = hashlib.sha256(
        (
            "zhimeng.source-import-idempotency.v1\0"
            f"{run_id}\0{revision}\0{request_hash}"
        ).encode("utf-8")
    ).hexdigest()
    return f"source-import-idempotency-{digest[:32]}"


def source_import_request_from_job(
    job: "SourceImportJob | LocalSourceImportJob",
) -> AnySourceImportRequestEvidence:
    if job.provider == "local_file":
        candidate = job.candidate
        return LocalSourceImportRequestEvidence(
            source_format=candidate.source_format,
            source_locator_hash=candidate.source_locator_hash,
            title=candidate.title,
            author=candidate.author,
            source_size_bytes=candidate.source_size_bytes,
            source_sha256=candidate.source_sha256,
        )
    return SourceImportRequestEvidence(
        query=job.query,
        candidate_index=job.candidate.index,
        book_id=job.candidate.book_id,
        title=job.candidate.title,
        author=job.candidate.author,
    )


def source_import_request_from_payload(
    payload: Mapping[str, object],
) -> AnySourceImportRequestEvidence:
    candidate = payload.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("source import job candidate is missing")
    if payload.get("provider") == "local_file":
        return LocalSourceImportRequestEvidence(
            source_format=candidate.get("source_format"),
            source_locator_hash=candidate.get("source_locator_hash"),
            title=candidate.get("title"),
            author=candidate.get("author"),
            source_size_bytes=candidate.get("source_size_bytes"),
            source_sha256=candidate.get("source_sha256"),
        )
    return SourceImportRequestEvidence(
        query=payload.get("query"),
        candidate_index=candidate.get("index"),
        book_id=candidate.get("book_id"),
        title=candidate.get("title"),
        author=candidate.get("author"),
    )


def validate_source_import_receipt_job(
    receipt: AnySourceImportExecutionReceipt,
    job: "SourceImportJob | LocalSourceImportJob",
) -> None:
    if receipt.source_import_id != job.import_id:
        raise ValueError("source import receipt belongs to another job")
    if receipt.provider != job.provider or receipt.retry_of != job.retry_of:
        raise ValueError("source import receipt lifecycle does not match its job")
    if receipt.request != source_import_request_from_job(job):
        raise ValueError("source import receipt request does not match its job")


def source_import_execution_receipt_hash(
    receipt: AnySourceImportExecutionReceipt,
) -> str:
    excluded_fields = {"receipt_hash"}
    if isinstance(receipt, LocalSourceImportExecutionReceipt):
        if receipt.packet_source_size_bytes is None:
            excluded_fields.add("packet_source_size_bytes")
        if receipt.packet_text_size_bytes is None:
            excluded_fields.add("packet_text_size_bytes")
    material = receipt.model_dump(
        mode="json",
        by_alias=True,
        exclude=excluded_fields,
    )
    return canonical_json_hash(material)


def parse_source_import_execution_receipt(
    payload: Mapping[str, object],
) -> AnySourceImportExecutionReceipt:
    if (
        payload.get("provider") == "local_file"
        or payload.get("schema") == "zhimeng.source-import-execution-receipt.v2"
    ):
        return LocalSourceImportExecutionReceipt.model_validate(payload)
    return SourceImportExecutionReceipt.model_validate(payload)


def source_import_attempt_receipt_id(
    source_import_id: str,
    attempt_ordinal: int = 1,
) -> str:
    digest = hashlib.sha256(
        (
            "zhimeng.source-import-attempt-receipt.v1\0"
            f"{source_import_id}\0{attempt_ordinal}"
        ).encode("utf-8")
    ).hexdigest()
    return f"source-import-attempt-{digest[:32]}"


def source_import_attempt_receipt_hash(receipt: SourceImportAttemptReceipt) -> str:
    return canonical_json_hash(
        receipt.model_dump(
            mode="json",
            by_alias=True,
            exclude={"receipt_hash"},
        )
    )


def _validate_artifact_order(
    artifacts: tuple[SourceImportArtifactEvidence, ...],
    *,
    label: str,
) -> None:
    keys = tuple((item.kind, item.relative_path) for item in artifacts)
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{label} artifacts must use canonical order")
    kinds = tuple(item.kind for item in artifacts)
    if len(kinds) != len(set(kinds)):
        raise ValueError(f"{label} artifact kinds must be unique")


__all__ = [
    "AnySourceImportExecutionReceipt",
    "AnySourceImportRequestEvidence",
    "LocalSourceImportExecutionReceipt",
    "LocalSourceImportRequestEvidence",
    "SourceImportAdapterExecutionIdentity",
    "SourceImportArtifactEvidence",
    "SourceImportAttemptReceipt",
    "SourceImportExecutionReceipt",
    "SourceImportExecutionReceiptRecord",
    "SourceImportLineageStatus",
    "SourceImportRequestEvidence",
    "SourceImportToolEvidence",
    "canonical_json_hash",
    "parse_source_import_execution_receipt",
    "source_import_attempt_receipt_hash",
    "source_import_attempt_receipt_id",
    "source_import_distillation_input_hash",
    "source_import_distillation_output_hash",
    "source_import_draft_fingerprint",
    "source_import_execution_receipt_hash",
    "source_import_idempotency_key",
    "source_import_packet_fingerprint",
    "source_import_prepare_input_hash",
    "source_import_prepare_output_hash",
    "source_import_receipt_id",
    "source_import_request_hash",
    "source_import_request_from_job",
    "source_import_request_from_payload",
    "source_import_run_id",
    "validate_source_import_receipt_job",
]
