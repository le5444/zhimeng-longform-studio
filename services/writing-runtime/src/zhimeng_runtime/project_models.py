from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FileId = Annotated[str, Field(pattern=r"^file-[0-9a-f]{32}$")]
ChapterId = Annotated[str, Field(pattern=r"^(?:VOL\d{3,}-)?CH\d{3,}$")]
VolumeId = Annotated[str, Field(pattern=r"^VOL\d{3,}$")]


class ProjectApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProjectInspectRequest(ProjectApiModel):
    root_path: str = Field(min_length=1)


class TextProjectImportRequest(ProjectApiModel):
    schema_id: Literal["zhimeng.text-project-import-request.v1"] = Field(
        default="zhimeng.text-project-import-request.v1",
        alias="schema",
    )
    file_name: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=8 * 1024 * 1024)


class ProjectFileReadRequest(ProjectApiModel):
    root_path: str = Field(min_length=1)
    file_id: FileId


class ProjectBindingSummary(ProjectApiModel):
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    display_name: str
    root_path: str
    revision: int = Field(ge=0)
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_ready: bool
    created_at: datetime
    updated_at: datetime


class ProjectBindingListResponse(ProjectApiModel):
    schema_id: Literal["zhimeng.project-bindings.v1"] = Field(
        default="zhimeng.project-bindings.v1",
        alias="schema",
    )
    projects: list[ProjectBindingSummary]


class ProjectSummary(ProjectApiModel):
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    display_name: str
    root_path: str
    format: Literal["01-09"]
    revision: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectSection(ProjectApiModel):
    index: int = Field(ge=1, le=9)
    label: str
    present: bool
    relative_path: str | None


class ProjectFile(ProjectApiModel):
    file_id: FileId
    relative_path: str
    absolute_path: str
    section_index: int | None = Field(default=None, ge=1, le=9)
    chapter_id: ChapterId | None = None
    volume_id: VolumeId | None = None
    kind: Literal[
        "prose",
        "document",
        "control",
        "handoff",
        "context",
        "review",
        "status",
        "structured_state",
        "asset",
    ]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_chapter_volume_identity(self) -> "ProjectFile":
        if self.chapter_id is None:
            return self
        if self.volume_id is None and self.chapter_id.startswith("VOL"):
            raise ValueError("unassigned chapter file cannot use a volume-prefixed ID")
        if self.volume_id is not None and not self.chapter_id.startswith(
            f"{self.volume_id}-"
        ):
            raise ValueError("file chapter_id must match volume_id")
        return self


class ProjectChapter(ProjectApiModel):
    chapter_id: ChapterId
    number: int = Field(ge=0)
    volume_id: VolumeId | None = None
    file_ids: list[FileId]
    prose_file_ids: list[FileId]

    @model_validator(mode="after")
    def validate_file_sets(self) -> "ProjectChapter":
        canonical_chapter = f"CH{self.number:0{max(3, len(str(self.number)))}d}"
        expected_id = (
            canonical_chapter
            if self.volume_id is None
            else f"{self.volume_id}-{canonical_chapter}"
        )
        if self.chapter_id != expected_id:
            raise ValueError("chapter_id must match volume_id and number")
        if len(self.file_ids) != len(set(self.file_ids)):
            raise ValueError("chapter file_ids must be unique")
        if len(self.prose_file_ids) != len(set(self.prose_file_ids)):
            raise ValueError("chapter prose_file_ids must be unique")
        if not set(self.prose_file_ids).issubset(self.file_ids):
            raise ValueError("chapter prose_file_ids must be a subset of file_ids")
        return self


class ProjectVolume(ProjectApiModel):
    volume_id: VolumeId
    number: int = Field(ge=0)
    chapter_ids: list[ChapterId]

    @model_validator(mode="after")
    def validate_chapter_ids(self) -> "ProjectVolume":
        expected_id = f"VOL{self.number:0{max(3, len(str(self.number)))}d}"
        if self.volume_id != expected_id:
            raise ValueError("volume_id must match number")
        if len(self.chapter_ids) != len(set(self.chapter_ids)):
            raise ValueError("volume chapter_ids must be unique")
        return self


class ProjectInspectionResponse(ProjectApiModel):
    schema_id: Literal["zhimeng.project-inspection.v1"] = Field(
        default="zhimeng.project-inspection.v1",
        alias="schema",
    )
    read_only: Literal[True]
    project: ProjectSummary
    sections: list[ProjectSection]
    missing_sections: list[int]
    volumes: list[ProjectVolume]
    chapters: list[ProjectChapter]
    files: list[ProjectFile]
    warnings: list[str]

    @model_validator(mode="after")
    def validate_tree_references(self) -> "ProjectInspectionResponse":
        files = {item.file_id: item for item in self.files}
        chapters = {item.chapter_id: item for item in self.chapters}
        volumes = {item.volume_id: item for item in self.volumes}
        if len(files) != len(self.files):
            raise ValueError("project file IDs must be unique")
        if len(chapters) != len(self.chapters):
            raise ValueError("project chapter IDs must be unique")
        if len(volumes) != len(self.volumes):
            raise ValueError("project volume IDs must be unique")

        for chapter in self.chapters:
            if not set(chapter.file_ids).issubset(files):
                raise ValueError("chapter references an unknown file ID")
            if any(files[file_id].chapter_id != chapter.chapter_id for file_id in chapter.file_ids):
                raise ValueError("chapter contains a file assigned to another chapter")
            if any(files[file_id].kind != "prose" for file_id in chapter.prose_file_ids):
                raise ValueError("chapter prose_file_ids must reference prose files")
            if chapter.volume_id is not None and chapter.volume_id not in volumes:
                raise ValueError("chapter references an unknown volume ID")

        for volume in self.volumes:
            if not set(volume.chapter_ids).issubset(chapters):
                raise ValueError("volume references an unknown chapter ID")
            if any(
                chapters[chapter_id].volume_id != volume.volume_id
                for chapter_id in volume.chapter_ids
            ):
                raise ValueError("volume contains a chapter assigned to another volume")
        return self


class TextProjectImportResponse(ProjectApiModel):
    schema_id: Literal["zhimeng.text-project-import-response.v1"] = Field(
        default="zhimeng.text-project-import-response.v1",
        alias="schema",
    )
    source_file_name: str
    title: str
    chapter_count: int = Field(ge=1, le=10_000)
    inspection: ProjectInspectionResponse


class ProjectFileContentResponse(ProjectApiModel):
    schema_id: Literal["zhimeng.project-file-content.v1"] = Field(
        default="zhimeng.project-file-content.v1",
        alias="schema",
    )
    read_only: Literal[True]
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    snapshot_revision: int = Field(ge=1)
    project_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    file: ProjectFile
    etag: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str
