from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from zhimeng_runtime.context_models import ContextAuthority, canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SearchSourceType(StrEnum):
    PROJECT_FILE = "project_file"
    FORMAL_KNOWLEDGE = "formal_knowledge"
    AUTHORIZED_SOURCE = "authorized_source"


class SearchIndexDocument(SearchModel):
    schema_id: Literal["zhimeng.project-search-document.v1"] = Field(
        default="zhimeng.project-search-document.v1",
        alias="schema",
    )
    source_id: NonEmptyText = Field(max_length=240)
    source_type: SearchSourceType
    authority: ContextAuthority
    title: NonEmptyText = Field(max_length=1024)
    relative_path: str | None = Field(default=None, max_length=2048)
    version: NonEmptyText = Field(max_length=240)
    source_hash: Sha256
    content: str = Field(default="", max_length=8 * 1024 * 1024)
    updated_at: datetime | None = None
    project_revision: int | None = Field(default=None, ge=1)
    card_status: Literal["active"] | None = None
    formalization_status: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_document(self) -> "SearchIndexDocument":
        if self.source_type is SearchSourceType.PROJECT_FILE:
            if self.authority is not ContextAuthority.PROJECT_TRUTH:
                raise ValueError("project files must use project_truth authority")
            if not self.relative_path:
                raise ValueError("project files require a relative_path")
            if self.card_status is not None or self.formalization_status is not None:
                raise ValueError("project files cannot claim knowledge formalization fields")
        elif self.source_type is SearchSourceType.FORMAL_KNOWLEDGE:
            if self.authority is not ContextAuthority.FORMAL_KNOWLEDGE:
                raise ValueError("formal knowledge must use formal_knowledge authority")
            if self.card_status != "active":
                raise ValueError("only active formal knowledge cards can be indexed")
            if self.relative_path is not None:
                raise ValueError("formal knowledge documents cannot carry project relative paths")
        elif self.source_type is SearchSourceType.AUTHORIZED_SOURCE:
            if self.authority is not ContextAuthority.REFERENCE:
                raise ValueError("authorized sources must use reference authority")
            if self.relative_path is not None:
                raise ValueError("authorized sources cannot carry project relative paths")
            if self.card_status is not None or self.formalization_status is not None:
                raise ValueError("authorized sources cannot claim knowledge formalization fields")
        if self.updated_at is not None and (
            self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None
        ):
            raise ValueError("search document updated_at must be timezone-aware")
        return self


class SearchIndexManifest(SearchModel):
    schema_id: Literal["zhimeng.project-search-index.v1"] = Field(
        default="zhimeng.project-search-index.v1",
        alias="schema",
    )
    project_id: ProjectId
    builder_id: Literal["zhimeng.project-search"] = "zhimeng.project-search"
    builder_version: Literal["1"] = "1"
    source_fingerprint: Sha256
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256
    knowledge_fingerprint: Sha256
    document_count: int = Field(ge=0, le=100_000)
    full_text_indexed: bool = True
    vector_index_ready: Literal[False] = False
    rebuilt_at: datetime
    index_hash: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> "SearchIndexManifest":
        if self.rebuilt_at.tzinfo is None or self.rebuilt_at.utcoffset() is None:
            raise ValueError("search index rebuilt_at must be timezone-aware")
        return self


class SearchIndexSnapshot(SearchModel):
    schema_id: Literal["zhimeng.project-search-snapshot.v1"] = Field(
        default="zhimeng.project-search-snapshot.v1",
        alias="schema",
    )
    manifest: SearchIndexManifest
    documents: list[SearchIndexDocument] = Field(max_length=100_000)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SearchIndexSnapshot":
        if len(self.documents) != self.manifest.document_count:
            raise ValueError("search index document_count must match documents")
        source_ids = [item.source_id for item in self.documents]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("search index source_id values must be unique")
        return self


class SearchRebuildRequest(SearchModel):
    schema_id: Literal["zhimeng.project-search-rebuild-request.v1"] = Field(
        default="zhimeng.project-search-rebuild-request.v1",
        alias="schema",
    )
    force: bool = False


class SearchRebuildResult(SearchModel):
    schema_id: Literal["zhimeng.project-search-rebuild-result.v1"] = Field(
        default="zhimeng.project-search-rebuild-result.v1",
        alias="schema",
    )
    project_id: ProjectId
    rebuilt: bool
    status: Literal["current", "rebuilt"]
    manifest: SearchIndexManifest


class SearchQueryRequest(SearchModel):
    schema_id: Literal["zhimeng.project-search-query-request.v1"] = Field(
        default="zhimeng.project-search-query-request.v1",
        alias="schema",
    )
    query: NonEmptyText = Field(max_length=512)
    source_types: list[SearchSourceType] | None = Field(default=None, max_length=8)
    authorities: list[ContextAuthority] | None = Field(default=None, max_length=8)
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)
    auto_rebuild: bool = True

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_filters(self) -> "SearchQueryRequest":
        if self.source_types is not None:
            if not self.source_types:
                raise ValueError("source_types cannot be empty when provided")
            if len(self.source_types) != len(set(self.source_types)):
                raise ValueError("source_types must be unique")
        if self.authorities is not None:
            if not self.authorities:
                raise ValueError("authorities cannot be empty when provided")
            if len(self.authorities) != len(set(self.authorities)):
                raise ValueError("authorities must be unique")
            if ContextAuthority.RISK in self.authorities:
                raise ValueError("risk authority is not searchable in Local v1 project search")
        for name, value in (
            ("updated_after", self.updated_after),
            ("updated_before", self.updated_before),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if (
            self.updated_after is not None
            and self.updated_before is not None
            and self.updated_after > self.updated_before
        ):
            raise ValueError("updated_after cannot be later than updated_before")
        return self


class SearchHit(SearchModel):
    schema_id: Literal["zhimeng.project-search-hit.v1"] = Field(
        default="zhimeng.project-search-hit.v1",
        alias="schema",
    )
    source_id: NonEmptyText = Field(max_length=240)
    source_type: SearchSourceType
    authority: ContextAuthority
    title: NonEmptyText = Field(max_length=1024)
    relative_path: str | None = Field(default=None, max_length=2048)
    version: NonEmptyText = Field(max_length=240)
    source_hash: Sha256
    snippet: str = Field(max_length=1024)
    reason: NonEmptyText = Field(max_length=512)
    score: float = Field(ge=0.0)
    updated_at: datetime | None = None


class SearchQueryResponse(SearchModel):
    schema_id: Literal["zhimeng.project-search-query-response.v1"] = Field(
        default="zhimeng.project-search-query-response.v1",
        alias="schema",
    )
    project_id: ProjectId
    query: NonEmptyText = Field(max_length=512)
    index_hash: Sha256
    source_fingerprint: Sha256
    rebuilt_before_search: bool
    match_mode: Literal["full_text"] = "full_text"
    vector_enhanced: Literal[False] = False
    hit_count: int = Field(ge=0, le=100)
    hits: list[SearchHit] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_response(self) -> "SearchQueryResponse":
        if self.hit_count != len(self.hits):
            raise ValueError("hit_count must match hits length")
        return self


def search_index_hash(
    *,
    project_id: str,
    source_fingerprint: str,
    documents: list[SearchIndexDocument],
) -> str:
    material = {
        "schema": "zhimeng.project-search-index-hash.v1",
        "project_id": project_id,
        "source_fingerprint": source_fingerprint,
        "documents": [
            {
                "source_id": item.source_id,
                "source_type": item.source_type.value,
                "authority": item.authority.value,
                "title": item.title,
                "relative_path": item.relative_path,
                "version": item.version,
                "source_hash": item.source_hash,
                "content_hash": canonical_json_hash({"content": item.content}),
                "updated_at": (
                    item.updated_at.isoformat() if item.updated_at is not None else None
                ),
                "project_revision": item.project_revision,
                "card_status": item.card_status,
                "formalization_status": item.formalization_status,
            }
            for item in sorted(documents, key=lambda document: document.source_id)
        ],
    }
    return canonical_json_hash(material)


__all__ = [
    "SearchHit",
    "SearchIndexDocument",
    "SearchIndexManifest",
    "SearchIndexSnapshot",
    "SearchQueryRequest",
    "SearchQueryResponse",
    "SearchRebuildRequest",
    "SearchRebuildResult",
    "SearchSourceType",
    "search_index_hash",
]
