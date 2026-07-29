from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.knowledge_scope_models import KnowledgeScopeRecord, KnowledgeScopeRef


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
ProjectKbStateId = Annotated[
    str,
    StringConstraints(pattern=r"^project-kb-state-[0-9a-f]{32}$"),
]
PROJECT_KB_ARTIFACT_PATHS = frozenset(
    {
        "state/scope.json",
        "knowledge/candidates.jsonl",
        "knowledge/cards.jsonl",
        "receipts/formalization.jsonl",
        "distillation_index/entries.jsonl",
    }
)


class ProjectKbModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProjectKbStatus(StrEnum):
    MISSING = "missing"
    CURRENT = "current"
    STALE = "stale"
    TAMPERED = "tampered"


class ProjectKbProjectRef(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-project-ref.v1"] = Field(
        default="zhimeng.project-kb-project-ref.v1",
        alias="schema",
    )
    project_id: ProjectId
    revision: int = Field(ge=1)
    fingerprint: Sha256


class ProjectKbSourceOfTruth(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-source-of-truth.v1"] = Field(
        default="zhimeng.project-kb-source-of-truth.v1",
        alias="schema",
    )
    project_files: Literal["work_truth"] = "work_truth"
    runtime_sqlite: Literal["run_truth"] = "run_truth"
    derived: Literal[True] = True
    rebuildable: Literal[True] = True


class ProjectKbArtifact(ProjectKbModel):
    logical_path: str = Field(min_length=1, max_length=240)
    size_bytes: int = Field(ge=0, le=512 * 1024 * 1024)
    sha256: Sha256

    @field_validator("logical_path")
    @classmethod
    def validate_logical_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("project KB artifact path must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("project KB artifact path must stay inside its snapshot")
        if value != path.as_posix() or value in {"manifest.json", "current.json"}:
            raise ValueError("project KB artifact path is reserved or non-canonical")
        return value


class ProjectKbCounts(ProjectKbModel):
    candidates: int = Field(ge=0)
    cards: int = Field(ge=0)
    active_cards: int = Field(ge=0)
    formalization_receipts: int = Field(ge=0)
    active_index_entries: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "ProjectKbCounts":
        if self.active_cards > self.cards:
            raise ValueError("active project KB cards cannot exceed all cards")
        if self.active_index_entries > self.active_cards:
            raise ValueError("active project KB index cannot exceed active cards")
        return self


class ProjectKbScopeState(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-scope-state.v1"] = Field(
        default="zhimeng.project-kb-scope-state.v1",
        alias="schema",
    )
    project: ProjectKbProjectRef
    knowledge_scope: KnowledgeScopeRecord
    knowledge_fingerprint: Sha256
    inherited_card_ids: list[str] = Field(default_factory=list, max_length=100_000)
    source_of_truth: ProjectKbSourceOfTruth

    @model_validator(mode="after")
    def validate_binding(self) -> "ProjectKbScopeState":
        if self.knowledge_scope.project_binding_id != self.project.project_id:
            raise ValueError("project KB scope state does not bind its project")
        if self.inherited_card_ids != sorted(set(self.inherited_card_ids)):
            raise ValueError("project KB scope state inherited ids must be sorted and unique")
        return self


class ProjectKbSnapshotManifest(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-state.v1"] = Field(
        default="zhimeng.project-kb-state.v1",
        alias="schema",
    )
    state_id: ProjectKbStateId
    state_hash: Sha256
    project: ProjectKbProjectRef
    knowledge_scope: KnowledgeScopeRef
    knowledge_fingerprint: Sha256
    inherited_card_ids: list[str] = Field(default_factory=list, max_length=100_000)
    source_of_truth: ProjectKbSourceOfTruth
    counts: ProjectKbCounts
    artifacts: list[ProjectKbArtifact] = Field(min_length=5, max_length=32)
    manifest_hash: Sha256

    @field_validator("inherited_card_ids")
    @classmethod
    def validate_inherited_ids(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("inherited project KB card ids must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_integrity(self) -> "ProjectKbSnapshotManifest":
        if self.knowledge_scope.project_binding_id != self.project.project_id:
            raise ValueError("project KB scope does not bind its project")
        paths = [artifact.logical_path for artifact in self.artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("project KB artifacts must be sorted and unique")
        if set(paths) != PROJECT_KB_ARTIFACT_PATHS:
            raise ValueError("project KB manifest does not contain the complete v1 artifact set")
        if self.state_hash != project_kb_state_hash(self):
            raise ValueError("project KB state hash does not match its manifest")
        if self.state_id != project_kb_state_id(self.state_hash):
            raise ValueError("project KB state id does not match its state hash")
        if self.manifest_hash != project_kb_manifest_hash(self):
            raise ValueError("project KB manifest hash does not match its payload")
        return self


class ProjectKbCurrentPointer(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-current-pointer.v1"] = Field(
        default="zhimeng.project-kb-current-pointer.v1",
        alias="schema",
    )
    project_id: ProjectId
    state_id: ProjectKbStateId
    state_hash: Sha256
    manifest_hash: Sha256
    manifest_file_sha256: Sha256
    pointer_hash: Sha256

    @model_validator(mode="after")
    def validate_integrity(self) -> "ProjectKbCurrentPointer":
        if self.pointer_hash != project_kb_pointer_hash(self):
            raise ValueError("project KB pointer hash does not match its payload")
        return self


class ProjectKbPendingPublication(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-pending-publication.v1"] = Field(
        default="zhimeng.project-kb-pending-publication.v1",
        alias="schema",
    )
    project_id: ProjectId
    state_id: ProjectKbStateId
    state_hash: Sha256
    manifest_hash: Sha256
    publication_hash: Sha256

    @model_validator(mode="after")
    def validate_integrity(self) -> "ProjectKbPendingPublication":
        if self.publication_hash != project_kb_publication_hash(self):
            raise ValueError("project KB publication hash does not match its payload")
        return self


class ProjectKbRebuildRequest(ProjectKbModel):
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256
    expected_scope_state_revision: int = Field(ge=1)
    expected_knowledge_fingerprint: Sha256


class ProjectKbStateRecord(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-state-record.v1"] = Field(
        default="zhimeng.project-kb-state-record.v1",
        alias="schema",
    )
    project_id: ProjectId
    status: ProjectKbStatus
    runtime_relative_root: str = Field(min_length=1, max_length=240)
    pointer: ProjectKbCurrentPointer | None = None
    manifest: ProjectKbSnapshotManifest | None = None
    reasons: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_status(self) -> "ProjectKbStateRecord":
        if self.status is ProjectKbStatus.MISSING:
            if self.pointer is not None or self.manifest is not None:
                raise ValueError("missing project KB state cannot expose a snapshot")
        elif self.status is ProjectKbStatus.CURRENT:
            if self.pointer is None or self.manifest is None or self.reasons:
                raise ValueError("current project KB state requires clean evidence")
        elif not self.reasons:
            raise ValueError("non-current project KB state requires reasons")
        return self


class ProjectKbRebuildResult(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-rebuild-result.v1"] = Field(
        default="zhimeng.project-kb-rebuild-result.v1",
        alias="schema",
    )
    reused_snapshot: bool
    record: ProjectKbStateRecord

    @model_validator(mode="after")
    def validate_result(self) -> "ProjectKbRebuildResult":
        if self.record.status is not ProjectKbStatus.CURRENT:
            raise ValueError("project KB rebuild must return a current snapshot")
        return self


class ProjectKbActiveIndexEntry(ProjectKbModel):
    schema_id: Literal["zhimeng.project-kb-active-entry.v1"] = Field(
        default="zhimeng.project-kb-active-entry.v1",
        alias="schema",
    )
    card_id: str = Field(min_length=1, max_length=160)
    card_type: str = Field(min_length=1, max_length=80)
    version_id: str = Field(min_length=1, max_length=160)
    content_hash: Sha256
    origin_scope_id: str = Field(min_length=1, max_length=160)
    formalization_receipt_id: str = Field(min_length=1, max_length=160)
    formalization_receipt_hash: Sha256
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=8_000)
    tags: list[str] = Field(default_factory=list, max_length=256)
    keywords: list[str] = Field(default_factory=list, max_length=256)
    applicable_tasks: list[str] = Field(default_factory=list, max_length=256)


def project_kb_state_hash(
    manifest: ProjectKbSnapshotManifest | dict[str, object],
) -> str:
    payload = (
        manifest.model_dump(mode="json", by_alias=True)
        if isinstance(manifest, ProjectKbSnapshotManifest)
        else dict(manifest)
    )
    for field in ("state_id", "state_hash", "manifest_hash"):
        payload.pop(field, None)
    return canonical_json_hash(
        {
            "schema": "zhimeng.project-kb-state-material.v1",
            "manifest": payload,
        }
    )


def project_kb_state_id(state_hash: str) -> str:
    return f"project-kb-state-{state_hash[:32]}"


def project_kb_manifest_hash(
    manifest: ProjectKbSnapshotManifest | dict[str, object],
) -> str:
    payload = (
        manifest.model_dump(mode="json", by_alias=True)
        if isinstance(manifest, ProjectKbSnapshotManifest)
        else dict(manifest)
    )
    payload.pop("manifest_hash", None)
    return canonical_json_hash(payload)


def project_kb_pointer_hash(
    pointer: ProjectKbCurrentPointer | dict[str, object],
) -> str:
    payload = (
        pointer.model_dump(mode="json", by_alias=True)
        if isinstance(pointer, ProjectKbCurrentPointer)
        else dict(pointer)
    )
    payload.pop("pointer_hash", None)
    return canonical_json_hash(payload)


def project_kb_publication_hash(
    publication: ProjectKbPendingPublication | dict[str, object],
) -> str:
    payload = (
        publication.model_dump(mode="json", by_alias=True)
        if isinstance(publication, ProjectKbPendingPublication)
        else dict(publication)
    )
    payload.pop("publication_hash", None)
    return canonical_json_hash(payload)


__all__ = [
    "ProjectKbActiveIndexEntry",
    "ProjectKbArtifact",
    "ProjectKbCounts",
    "ProjectKbCurrentPointer",
    "ProjectKbPendingPublication",
    "ProjectKbProjectRef",
    "ProjectKbRebuildRequest",
    "ProjectKbRebuildResult",
    "ProjectKbScopeState",
    "ProjectKbSnapshotManifest",
    "ProjectKbSourceOfTruth",
    "ProjectKbStateRecord",
    "ProjectKbStatus",
    "PROJECT_KB_ARTIFACT_PATHS",
    "project_kb_manifest_hash",
    "project_kb_pointer_hash",
    "project_kb_publication_hash",
    "project_kb_state_hash",
    "project_kb_state_id",
]
