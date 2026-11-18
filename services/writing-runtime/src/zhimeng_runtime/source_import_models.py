from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceImportProvider: TypeAlias = Literal["tomato", "local_file", "feedback_experience"]
LocalSourceFormat: TypeAlias = Literal["txt", "epub"]
MAX_UPLOADED_SOURCE_CHARACTERS = 8 * 1024 * 1024


class SourceImportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceImportStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    SOURCE_READY = "source_ready"
    DRAFTING = "drafting"
    AWAITING_REVIEW = "awaiting_review"
    FORMALIZING = "formalizing"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_SOURCE_IMPORT_STATUSES = frozenset(
    {
        SourceImportStatus.QUEUED,
        SourceImportStatus.DOWNLOADING,
        SourceImportStatus.SOURCE_READY,
        SourceImportStatus.DRAFTING,
        SourceImportStatus.FORMALIZING,
        SourceImportStatus.CANCEL_REQUESTED,
    }
)


class TomatoSearchCandidate(SourceImportModel):
    index: int = Field(ge=1)
    book_id: str = Field(pattern=r"^[0-9]{1,32}$")
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=300)


def local_source_locator_hash(source_path: str) -> str:
    normalized = source_path.strip().replace("\\", "/")
    return hashlib.sha256(
        f"zhimeng.local-source-locator.v1\0{normalized}".encode("utf-8")
    ).hexdigest()


class LocalFileSourceCandidate(SourceImportModel):
    index: Literal[1] = 1
    source_path: str = Field(min_length=1, max_length=4096, strict=True)
    source_format: LocalSourceFormat = "txt"
    title: str = Field(min_length=1, max_length=500, strict=True)
    author: str = Field(default="unknown", min_length=1, max_length=300, strict=True)
    source_size_bytes: int = Field(ge=1, strict=True)
    source_sha256: Sha256
    source_locator_hash: Sha256

    @field_validator("source_path", "title", "author")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("local source text fields must not be blank or contain controls")
        return normalized

    @model_validator(mode="after")
    def validate_locator(self) -> "LocalFileSourceCandidate":
        if self.source_locator_hash != local_source_locator_hash(self.source_path):
            raise ValueError("local source locator hash does not match source_path")
        return self


class TomatoSearchRequest(SourceImportModel):
    query: str = Field(min_length=1, max_length=500)


class TomatoResolveRequest(SourceImportModel):
    """Resolve a Tomato candidate by explicit book_id or URL.

    This is separate from name-search. Title/author are author-supplied metadata
    bound into the candidate used by create/prepare.
    """

    book_id: str | None = Field(default=None, pattern=r"^[0-9]{1,32}$")
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=300)

    @field_validator("title", "author")
    @classmethod
    def normalize_metadata(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("title/author must not be blank or contain controls")
        return normalized

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("url must not be blank or contain controls")
        return normalized

    @model_validator(mode="after")
    def validate_locator(self) -> "TomatoResolveRequest":
        if (self.book_id is None) == (self.url is None):
            raise ValueError("exactly one of book_id or url is required")
        return self


class TomatoResolveResponse(SourceImportModel):
    schema_id: Literal["zhimeng.tomato-resolve.v1"] = Field(
        default="zhimeng.tomato-resolve.v1",
        alias="schema",
    )
    locator_kind: Literal["book_id", "url"]
    source_locator: str = Field(min_length=1, max_length=2048)
    resolution: Literal["resolved"] = "resolved"
    candidate: TomatoSearchCandidate


class LocalSourceInspectRequest(SourceImportModel):
    source_path: str = Field(min_length=1, max_length=4096, strict=True)

    @field_validator("source_path")
    @classmethod
    def normalize_source_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("local source path must not be blank or contain controls")
        return normalized


class LocalTextSourceUploadRequest(SourceImportModel):
    schema_id: Literal["zhimeng.local-text-source-upload-request.v1"] = Field(
        default="zhimeng.local-text-source-upload-request.v1",
        alias="schema",
    )
    file_name: str = Field(min_length=1, max_length=255, strict=True)
    content: str = Field(
        min_length=1,
        max_length=MAX_UPLOADED_SOURCE_CHARACTERS,
        strict=True,
    )

    @field_validator("file_name")
    @classmethod
    def normalize_file_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("uploaded source file name is invalid")
        return normalized


class TomatoSearchResponse(SourceImportModel):
    schema_id: Literal["zhimeng.tomato-search.v1"] = Field(
        default="zhimeng.tomato-search.v1",
        alias="schema",
    )
    query: str
    resolution: Literal["auto_selected", "selection_required", "not_found"]
    candidates: list[TomatoSearchCandidate] = Field(max_length=100)
    selected_candidate: TomatoSearchCandidate | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "TomatoSearchResponse":
        if self.resolution == "auto_selected":
            if self.selected_candidate is None or self.selected_candidate not in self.candidates:
                raise ValueError("auto_selected search requires a listed selected_candidate")
        elif self.selected_candidate is not None:
            raise ValueError("only auto_selected search can carry selected_candidate")
        if self.resolution == "not_found" and self.candidates:
            raise ValueError("not_found search cannot carry candidates")
        if self.resolution == "selection_required" and len(self.candidates) < 2:
            raise ValueError("selection_required search needs at least two candidates")
        return self


class SourceArtifact(SourceImportModel):
    kind: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=4096)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PreparedSourcePacket(SourceImportModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=300)
    download_scope: str = Field(min_length=1, max_length=100)
    packet_dir: str = Field(min_length=1, max_length=4096)
    packet_manifest_path: str = Field(min_length=1, max_length=4096)
    epub_path: str = Field(min_length=1, max_length=4096)
    downloaded_epub_path: str = Field(min_length=1, max_length=4096)
    text_path: str = Field(min_length=1, max_length=4096)
    metadata_path: str = Field(min_length=1, max_length=4096)
    char_count: int = Field(ge=1)
    chapter_marker_count: int = Field(ge=0)
    artifacts: list[SourceArtifact] = Field(min_length=4, max_length=32)


class PreparedLocalSourcePacket(SourceImportModel):
    source_backend: Literal["generic_local_file"] = "generic_local_file"
    source_format: LocalSourceFormat = "txt"
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=300)
    download_scope: Literal["full"] = "full"
    packet_dir: str = Field(min_length=1, max_length=4096)
    packet_manifest_path: str = Field(min_length=1, max_length=4096)
    packet_source_path: str = Field(min_length=1, max_length=4096)
    text_path: str = Field(min_length=1, max_length=4096)
    metadata_path: str = Field(min_length=1, max_length=4096)
    char_count: int = Field(ge=1)
    chapter_marker_count: int = Field(ge=0)
    source_file_size_bytes: int = Field(ge=1, strict=True)
    source_file_sha256: Sha256
    packet_source_sha256: Sha256
    text_sha256: Sha256
    artifacts: list[SourceArtifact] = Field(min_length=4, max_length=32)

    @model_validator(mode="after")
    def validate_artifact_contract(self) -> "PreparedLocalSourcePacket":
        by_kind = {artifact.kind: artifact for artifact in self.artifacts}
        if len(by_kind) != len(self.artifacts):
            raise ValueError("local source packet artifact kinds must be unique")
        required = {
            "packet_manifest",
            "packet_source",
            "packet_text",
            "packet_metadata",
        }
        if not required.issubset(by_kind):
            raise ValueError("local source packet is missing required artifacts")
        field_bindings = {
            "packet_manifest_path": "packet_manifest",
            "packet_source_path": "packet_source",
            "text_path": "packet_text",
            "metadata_path": "packet_metadata",
        }
        for field_name, artifact_kind in field_bindings.items():
            artifact = by_kind[artifact_kind]
            if _logical_path(getattr(self, field_name)) != _logical_path(artifact.path):
                raise ValueError(
                    f"local source packet field does not match artifact: {field_name}"
                )
            if artifact.sha256 is None or artifact.size_bytes is None:
                raise ValueError("local source packet artifacts require size and SHA-256")
        packet_source = by_kind["packet_source"]
        packet_text = by_kind["packet_text"]
        if not (
            self.source_file_sha256 == self.packet_source_sha256
            and packet_source.sha256 == self.packet_source_sha256
            and packet_source.size_bytes == self.source_file_size_bytes
        ):
            raise ValueError("local packet source does not preserve selected source bytes")
        if packet_text.sha256 != self.text_sha256 or not packet_text.size_bytes:
            raise ValueError("local packet hashes differ from their artifact bytes")
        source_path = _logical_path(self.packet_source_path)
        text_path = _logical_path(self.text_path)
        if self.source_format == "txt":
            if (
                self.text_sha256 != self.source_file_sha256
                or packet_text.size_bytes != self.source_file_size_bytes
                or source_path != text_path
            ):
                raise ValueError("local TXT packet must preserve selected source bytes")
        elif source_path == text_path or self.text_sha256 == self.packet_source_sha256:
            raise ValueError("local EPUB packet source and extracted text must be distinct")
        return self


class DistillationDraft(SourceImportModel):
    status: Literal["draft_ready"] = "draft_ready"
    output_dir: str = Field(min_length=1, max_length=4096)
    cards_path: str = Field(min_length=1, max_length=4096)
    report_path: str = Field(min_length=1, max_length=4096)
    validation_report_path: str = Field(min_length=1, max_length=4096)
    envelope_path: str = Field(min_length=1, max_length=4096)
    card_count: int = Field(ge=1)
    requires_review: Literal[True] = True
    artifacts: list[SourceArtifact] = Field(min_length=4, max_length=32)


class SourceImportProgress(SourceImportModel):
    stage: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    saved_chapters: int | None = Field(default=None, ge=0)
    total_chapters: int | None = Field(default=None, ge=0)
    percent: int | None = Field(default=None, ge=0, le=100)


class SourceImportCreateRequest(SourceImportModel):
    provider: Literal["tomato"] = "tomato"
    query: str = Field(min_length=1, max_length=500)
    candidate: TomatoSearchCandidate


class LocalSourceImportCreateRequest(SourceImportModel):
    provider: Literal["local_file"] = "local_file"
    query: str = Field(min_length=1, max_length=4096)
    candidate: LocalFileSourceCandidate

    @model_validator(mode="after")
    def validate_candidate_path(self) -> "LocalSourceImportCreateRequest":
        if _logical_path(self.query) != _logical_path(self.candidate.source_path):
            raise ValueError("local source query must match the inspected source path")
        return self


AnySourceImportCreateRequest: TypeAlias = (
    SourceImportCreateRequest | LocalSourceImportCreateRequest
)


class SourceImportJob(SourceImportModel):
    schema_id: Literal["zhimeng.source-import.v1"] = Field(
        default="zhimeng.source-import.v1",
        alias="schema",
    )
    import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    provider: Literal["tomato"] = "tomato"
    query: str = Field(min_length=1, max_length=500)
    candidate: TomatoSearchCandidate
    status: SourceImportStatus
    retry_of: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    progress: SourceImportProgress
    source_packet: PreparedSourcePacket | None = None
    distillation_draft: DistillationDraft | None = None
    error: str | None = Field(default=None, max_length=8000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "SourceImportJob":
        if self.created_at > self.updated_at:
            raise ValueError("source import updated_at cannot precede created_at")
        if self.status in {
            SourceImportStatus.SOURCE_READY,
            SourceImportStatus.DRAFTING,
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.COMPLETED,
        } and self.source_packet is None:
            raise ValueError(f"{self.status.value} import requires a source packet")
        if self.status in {
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.COMPLETED,
        } and self.distillation_draft is None:
            raise ValueError(f"{self.status.value} import requires a distillation draft")
        if self.status is SourceImportStatus.FAILED and not self.error:
            raise ValueError("failed source import requires an error")
        return self


class LocalSourceImportJob(SourceImportModel):
    schema_id: Literal["zhimeng.source-import.v2"] = Field(
        default="zhimeng.source-import.v2",
        alias="schema",
    )
    import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    provider: Literal["local_file"] = "local_file"
    query: str = Field(min_length=1, max_length=4096)
    candidate: LocalFileSourceCandidate
    status: SourceImportStatus
    retry_of: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    progress: SourceImportProgress
    source_packet: PreparedLocalSourcePacket | None = None
    distillation_draft: DistillationDraft | None = None
    error: str | None = Field(default=None, max_length=8000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "LocalSourceImportJob":
        if self.created_at > self.updated_at:
            raise ValueError("source import updated_at cannot precede created_at")
        if _logical_path(self.query) != _logical_path(self.candidate.source_path):
            raise ValueError("local source job query must match its candidate path")
        if self.source_packet is not None and (
            self.source_packet.source_format != self.candidate.source_format
            or self.source_packet.source_file_size_bytes
            != self.candidate.source_size_bytes
            or self.source_packet.source_file_sha256 != self.candidate.source_sha256
        ):
            raise ValueError("local source packet does not match its inspected candidate")
        if self.status in {
            SourceImportStatus.SOURCE_READY,
            SourceImportStatus.DRAFTING,
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.COMPLETED,
        } and self.source_packet is None:
            raise ValueError(f"{self.status.value} import requires a source packet")
        if self.status in {
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.COMPLETED,
        } and self.distillation_draft is None:
            raise ValueError(f"{self.status.value} import requires a distillation draft")
        if self.status is SourceImportStatus.FAILED and not self.error:
            raise ValueError("failed source import requires an error")
        return self



class FeedbackExperienceCandidate(SourceImportModel):
    index: Literal[1] = 1
    package_id: str = Field(
        pattern=r"^formal-knowledge-promotion-package-[0-9a-f]{32}$"
    )
    change_set_id: str = Field(pattern=r"^changeset-[0-9a-f]{32}$")
    feedback_candidate_id: str = Field(pattern=r"^feedback-candidate-[0-9a-f]{32}$")
    package_hash: Sha256
    title: str = Field(min_length=1, max_length=500)


class FeedbackSourceImportJob(SourceImportModel):
    """Synthetic source lineage for feedback-derived knowledge review candidates.

    This is not a Tomato/local book import. It only exists so staged formal-knowledge
    packages can become pending knowledge candidates without writing formal knowledge.
    """

    schema_id: Literal["zhimeng.source-import.v3"] = Field(
        default="zhimeng.source-import.v3",
        alias="schema",
    )
    import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    provider: Literal["feedback_experience"] = "feedback_experience"
    query: str = Field(min_length=1, max_length=500)
    candidate: FeedbackExperienceCandidate
    status: SourceImportStatus
    retry_of: str | None = Field(
        default=None,
        pattern=r"^source-import-[0-9a-f]{32}$",
    )
    progress: SourceImportProgress
    source_packet: None = None
    distillation_draft: None = None
    error: str | None = Field(default=None, max_length=8000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "FeedbackSourceImportJob":
        if self.created_at > self.updated_at:
            raise ValueError("source import updated_at cannot precede created_at")
        if self.status not in {
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
        }:
            raise ValueError(
                "feedback_experience source imports only support awaiting_review/cancelled/failed"
            )
        if self.status is SourceImportStatus.FAILED and not self.error:
            raise ValueError("failed source import requires an error")
        if self.source_packet is not None or self.distillation_draft is not None:
            raise ValueError(
                "feedback_experience source imports cannot claim book source packets or drafts"
            )
        return self


AnySourceImportJob: TypeAlias = (
    SourceImportJob | LocalSourceImportJob | FeedbackSourceImportJob
)



class SourceImportListResponse(SourceImportModel):
    schema_id: Literal["zhimeng.source-import-list.v1"] = Field(
        default="zhimeng.source-import-list.v1",
        alias="schema",
    )
    imports: list[SourceImportJob | LocalSourceImportJob | FeedbackSourceImportJob]


def parse_source_import_create_request(
    payload: Mapping[str, object],
) -> AnySourceImportCreateRequest:
    if payload.get("provider", "tomato") == "local_file":
        return LocalSourceImportCreateRequest.model_validate(payload)
    return SourceImportCreateRequest.model_validate(payload)


def parse_source_import_job(payload: Mapping[str, object]) -> AnySourceImportJob:
    provider = payload.get("provider")
    if provider == "local_file":
        return LocalSourceImportJob.model_validate(payload)
    if provider == "feedback_experience":
        return FeedbackSourceImportJob.model_validate(payload)
    return SourceImportJob.model_validate(payload)


def _logical_path(value: str) -> str:
    return value.strip().replace("\\", "/")


__all__ = [
    "ACTIVE_SOURCE_IMPORT_STATUSES",
    "AnySourceImportCreateRequest",
    "AnySourceImportJob",
    "DistillationDraft",
    "LocalFileSourceCandidate",
    "LocalSourceFormat",
    "LocalSourceInspectRequest",
    "LocalTextSourceUploadRequest",
    "LocalSourceImportCreateRequest",
    "LocalSourceImportJob",
    "MAX_UPLOADED_SOURCE_CHARACTERS",
    "PreparedLocalSourcePacket",
    "PreparedSourcePacket",
    "SourceArtifact",
    "SourceImportCreateRequest",
    "SourceImportJob",
    "SourceImportListResponse",
    "SourceImportProgress",
    "SourceImportProvider",
    "SourceImportStatus",
    "TomatoSearchCandidate",
    "TomatoSearchRequest",
    "TomatoSearchResponse",
    "local_source_locator_hash",
    "parse_source_import_create_request",
    "parse_source_import_job",
]
