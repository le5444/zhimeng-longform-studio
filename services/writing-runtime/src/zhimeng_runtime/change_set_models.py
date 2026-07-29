from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.post_draft_gate_models import PostDraftGateReceipt


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ProjectId = Annotated[str, Field(pattern=r"^project-[0-9a-f]{24}$")]
FileId = Annotated[str, Field(pattern=r"^file-[0-9a-f]{32}$")]
ChangeSetId = Annotated[str, Field(pattern=r"^changeset-[0-9a-f]{32}$")]
ChangeId = Annotated[str, Field(pattern=r"^change-[0-9a-f]{32}$")]
HunkId = Annotated[str, Field(pattern=r"^hunk-[0-9a-f]{32}$")]
PORTABLE_PATH_SEGMENT_PATTERN = (
    r'[^./\\:<>"|?*\x00-\x1f]'
    r'(?:[^/\\:<>"|?*\x00-\x1f]*[^./\\:<>"|?*\x00-\x1f ])?'
)
RELATIVE_PROJECT_PATH_PATTERN = (
    rf"^0[1-9]_{PORTABLE_PATH_SEGMENT_PATTERN}"
    rf"(?:/{PORTABLE_PATH_SEGMENT_PATTERN})+"
    r"\.(?:json|jsonl|md|txt|yaml|yml)$"
)
RELATIVE_PROJECT_DIRECTORY_PATTERN = (
    rf"^0[1-9]_{PORTABLE_PATH_SEGMENT_PATTERN}"
    rf"(?:/{PORTABLE_PATH_SEGMENT_PATTERN})+$"
)
WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def _validate_portable_relative_path(value: str) -> str:
    for segment in value.split("/"):
        if len(segment) > 255:
            raise ValueError("relative path segments cannot exceed 255 characters")
        device_stem = segment.split(".", 1)[0].casefold()
        if device_stem in WINDOWS_DEVICE_NAMES:
            raise ValueError("relative paths cannot use Windows device names")
    return value


RelativeProjectPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=1024,
        pattern=RELATIVE_PROJECT_PATH_PATTERN,
        json_schema_extra={
            "x-zhimeng-invariants": [
                "path segments cannot use Windows device names",
                "path segments are at most 255 characters",
            ]
        },
    ),
    AfterValidator(_validate_portable_relative_path),
]
RelativeProjectDirectory = Annotated[
    str,
    Field(
        min_length=1,
        max_length=1024,
        pattern=RELATIVE_PROJECT_DIRECTORY_PATTERN,
        json_schema_extra={
            "x-zhimeng-invariants": [
                "path segments cannot use Windows device names",
                "path segments are at most 255 characters",
            ]
        },
    ),
    AfterValidator(_validate_portable_relative_path),
]

MAX_CHANGE_COUNT = 64
MAX_CHANGE_CONTENT_CHARACTERS = 8 * 1024 * 1024
MAX_CHANGE_SET_CONTENT_CHARACTERS = 32 * 1024 * 1024


class ChangeSetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _add_create_request_schema_invariants(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {
                "properties": {"origin": {"const": "skill_workflow"}},
                "required": ["origin"],
            },
            "then": {
                "properties": {"source_skill_ids": {"minItems": 1}},
                "required": ["source_skill_ids"],
            },
        }
    ]
    schema["x-zhimeng-invariants"] = [
        "source_skill_ids are unique",
        "skill_workflow origin requires at least one source_skill_id",
        "aggregate after_content length is at most 32 MiB characters",
    ]


def _add_candidate_change_schema_invariants(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {"properties": {"operation": {"const": "create"}}},
            "then": {
                "properties": {
                    "file_id": {"type": "null"},
                    "before_sha256": {"type": "null"},
                    "proposed_after_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "required": ["proposed_after_sha256"],
            },
        },
        {
            "if": {"properties": {"operation": {"const": "update"}}},
            "then": {
                "properties": {
                    "file_id": {"type": "string", "pattern": "^file-[0-9a-f]{32}$"},
                    "before_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "proposed_after_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "required": ["file_id", "before_sha256", "proposed_after_sha256"],
            },
        },
        {
            "if": {"properties": {"operation": {"const": "delete"}}},
            "then": {
                "properties": {
                    "file_id": {"type": "string", "pattern": "^file-[0-9a-f]{32}$"},
                    "before_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "proposed_after_sha256": {"type": "null"},
                    "post_draft_gate_scope": {"const": "none"},
                },
                "required": ["file_id", "before_sha256"],
            },
        },
    ]
    schema["x-zhimeng-invariants"] = [
        "update before_sha256 and proposed_after_sha256 differ",
        "delete post_draft_gate_scope is none",
        "hunk indexes are contiguous and hunk IDs are unique",
        "hunk ranges are ordered and non-overlapping",
    ]


def _add_writeback_file_schema_invariants(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {"properties": {"operation": {"const": "create"}}},
            "then": {
                "properties": {
                    "before_sha256": {"type": "null"},
                    "after_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "result_file_id": {"type": "string", "pattern": "^file-[0-9a-f]{32}$"},
                    "backup_ref": {"type": "null"},
                },
                "required": ["after_sha256", "result_file_id"],
            },
        },
        {
            "if": {"properties": {"operation": {"const": "update"}}},
            "then": {
                "properties": {
                    "before_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "after_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "result_file_id": {"type": "string", "pattern": "^file-[0-9a-f]{32}$"},
                    "backup_ref": {"type": "string"},
                },
                "required": ["before_sha256", "after_sha256", "result_file_id", "backup_ref"],
            },
        },
        {
            "if": {"properties": {"operation": {"const": "delete"}}},
            "then": {
                "properties": {
                    "before_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "after_sha256": {"type": "null"},
                    "result_file_id": {"type": "null"},
                    "backup_ref": {"type": "string"},
                },
                "required": ["before_sha256", "backup_ref"],
            },
        },
    ]
    schema["x-zhimeng-invariants"] = [
        "update before_sha256 and after_sha256 differ",
        "accepted_hunk_ids and rejected_hunk_ids are disjoint",
    ]


def _add_change_set_response_schema_invariants(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "applied"}}},
            "then": {
                "properties": {
                    "writeback": {
                        "type": "object",
                        "properties": {"outcome": {"const": "applied"}},
                        "required": ["outcome"],
                    }
                },
                "required": ["writeback"],
            },
        },
        {
            "if": {"properties": {"status": {"const": "rolled_back"}}},
            "then": {
                "properties": {
                    "writeback": {
                        "type": "object",
                        "properties": {"outcome": {"const": "applied"}},
                        "required": ["outcome"],
                    },
                    "rollback": {
                        "type": "object",
                        "properties": {"outcome": {"const": "rolled_back"}},
                        "required": ["outcome"],
                    },
                },
                "required": ["writeback", "rollback"],
            },
        },
    ]
    schema["x-zhimeng-invariants"] = [
        "reviewing has at least one pending hunk and every other status has none",
        "source_skill_ids, change IDs, paths, and hunk IDs are unique",
        "skill_workflow origin requires at least one source_skill_id",
        "writeback and rollback identities match the containing change set",
        "hunk text hashes match before_text and after_text",
    ]


class ChangeOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ChangeSetOrigin(StrEnum):
    MANUAL = "manual"
    AGENT = "agent"
    SKILL_WORKFLOW = "skill_workflow"


class HunkDecision(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ChangeSetStatus(StrEnum):
    REVIEWING = "reviewing"
    READY = "ready"
    APPLYING = "applying"
    ROLLING_BACK = "rolling_back"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    DISCARDED = "discarded"
    CONFLICTED = "conflicted"
    FAILED = "failed"


class _FileChangeInput(ChangeSetModel):
    rationale: str = Field(min_length=1, max_length=4000)


class CreateFileChangeInput(_FileChangeInput):
    operation: Literal[ChangeOperation.CREATE]
    relative_path: RelativeProjectPath
    after_content: str = Field(max_length=MAX_CHANGE_CONTENT_CHARACTERS)


class UpdateFileChangeInput(_FileChangeInput):
    operation: Literal[ChangeOperation.UPDATE]
    file_id: FileId
    after_content: str = Field(max_length=MAX_CHANGE_CONTENT_CHARACTERS)


class DeleteFileChangeInput(_FileChangeInput):
    operation: Literal[ChangeOperation.DELETE]
    file_id: FileId


ProposedFileChangeInput = Annotated[
    CreateFileChangeInput | UpdateFileChangeInput | DeleteFileChangeInput,
    Field(discriminator="operation"),
]


class ChangeSetCreateRequest(ChangeSetModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra=_add_create_request_schema_invariants,
    )
    schema_id: Literal["zhimeng.change-set-create.v1"] = Field(
        default="zhimeng.change-set-create.v1",
        alias="schema",
    )
    base_revision: int = Field(ge=1)
    base_fingerprint: Sha256
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=8000)
    origin: ChangeSetOrigin = ChangeSetOrigin.MANUAL
    source_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_request_hash: Sha256 | None = None
    source_skill_ids: list[str] = Field(
        default_factory=list,
        max_length=32,
        json_schema_extra={"uniqueItems": True},
    )
    # Agent-origin proposals are forced to require a post-draft Gate by the
    # Runtime. Manual callers may opt in without changing the legacy path.
    gate_required: bool = False
    changes: list[ProposedFileChangeInput] = Field(
        min_length=1,
        max_length=MAX_CHANGE_COUNT,
    )

    @model_validator(mode="after")
    def validate_request(self) -> "ChangeSetCreateRequest":
        total = sum(len(getattr(item, "after_content", "")) for item in self.changes)
        if total > MAX_CHANGE_SET_CONTENT_CHARACTERS:
            raise ValueError("change set content exceeds the aggregate character limit")
        if len(self.source_skill_ids) != len(set(self.source_skill_ids)):
            raise ValueError("source_skill_ids must be unique")
        if self.origin is ChangeSetOrigin.SKILL_WORKFLOW and not self.source_skill_ids:
            raise ValueError("skill_workflow origin requires source_skill_ids")
        if self.origin is ChangeSetOrigin.AGENT:
            if (
                self.source_run_id is None
                or self.source_tool_call_id is None
                or self.source_request_hash is None
            ):
                raise ValueError(
                    "agent origin requires source_run_id, source_tool_call_id, "
                    "and source_request_hash"
                )
        elif self.source_tool_call_id is not None or self.source_request_hash is not None:
            raise ValueError("tool-call provenance is reserved for agent origin")
        return self


class ChangeHunk(ChangeSetModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra={
            "x-zhimeng-invariants": [
                "before_end - before_start equals the before_text line count",
                "after_end - after_start equals the after_text line count",
                "before_sha256 and after_sha256 hash their corresponding UTF-8 text",
            ]
        },
    )
    hunk_id: HunkId
    index: int = Field(ge=0)
    before_start: int = Field(ge=0)
    before_end: int = Field(ge=0)
    after_start: int = Field(ge=0)
    after_end: int = Field(ge=0)
    before_text: str
    after_text: str
    before_sha256: Sha256
    after_sha256: Sha256
    decision: HunkDecision = HunkDecision.PENDING

    @model_validator(mode="after")
    def validate_ranges(self) -> "ChangeHunk":
        if self.before_end < self.before_start or self.after_end < self.after_start:
            raise ValueError("hunk ranges must be ordered")
        if self.before_end - self.before_start != len(
            self.before_text.splitlines(keepends=True)
        ):
            raise ValueError("before hunk range must match before_text line count")
        if self.after_end - self.after_start != len(
            self.after_text.splitlines(keepends=True)
        ):
            raise ValueError("after hunk range must match after_text line count")
        if hashlib.sha256(self.before_text.encode("utf-8")).hexdigest() != self.before_sha256:
            raise ValueError("before_sha256 must hash before_text")
        if hashlib.sha256(self.after_text.encode("utf-8")).hexdigest() != self.after_sha256:
            raise ValueError("after_sha256 must hash after_text")
        return self


class CandidateFileChange(ChangeSetModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra=_add_candidate_change_schema_invariants,
    )
    change_id: ChangeId
    operation: ChangeOperation
    file_id: FileId | None = None
    relative_path: RelativeProjectPath
    rationale: str = Field(min_length=1, max_length=4000)
    before_sha256: Sha256 | None = None
    proposed_after_sha256: Sha256 | None = None
    preserve_utf8_bom: bool = False
    post_draft_gate_scope: Literal["none", "prose_body"] = "none"
    hunks: list[ChangeHunk] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_hash_transition(self) -> "CandidateFileChange":
        if self.operation is ChangeOperation.CREATE:
            if self.file_id is not None or self.before_sha256 is not None:
                raise ValueError("create cannot carry file_id or before_sha256")
            if self.proposed_after_sha256 is None:
                raise ValueError("create requires proposed_after_sha256")
        elif self.operation is ChangeOperation.UPDATE:
            if (
                self.file_id is None
                or self.before_sha256 is None
                or self.proposed_after_sha256 is None
            ):
                raise ValueError("update requires file_id and both hashes")
            if self.before_sha256 == self.proposed_after_sha256:
                raise ValueError("update must change file content")
        else:
            if self.file_id is None or self.before_sha256 is None:
                raise ValueError("delete requires file_id and before_sha256")
            if self.proposed_after_sha256 is not None:
                raise ValueError("delete cannot carry proposed_after_sha256")
            if self.post_draft_gate_scope != "none":
                raise ValueError("delete cannot require a post-draft body Gate")
        if [item.index for item in self.hunks] != list(range(len(self.hunks))):
            raise ValueError("hunk indexes must be contiguous and zero based")
        if len({item.hunk_id for item in self.hunks}) != len(self.hunks):
            raise ValueError("hunk IDs must be unique within a file change")
        previous_before_end = 0
        previous_after_end = 0
        for hunk in self.hunks:
            if (
                hunk.before_start < previous_before_end
                or hunk.after_start < previous_after_end
            ):
                raise ValueError("hunk ranges must be ordered and non-overlapping")
            previous_before_end = hunk.before_end
            previous_after_end = hunk.after_end
        return self


class WritebackFileResult(ChangeSetModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra=_add_writeback_file_schema_invariants,
    )
    change_id: ChangeId
    operation: ChangeOperation
    relative_path: RelativeProjectPath
    before_sha256: Sha256 | None = None
    after_sha256: Sha256 | None = None
    result_file_id: FileId | None = None
    accepted_hunk_ids: list[HunkId] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    rejected_hunk_ids: list[HunkId] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    backup_ref: str | None = None

    @model_validator(mode="after")
    def validate_transition(self) -> "WritebackFileResult":
        if self.operation is ChangeOperation.CREATE:
            if self.before_sha256 is not None or self.after_sha256 is None:
                raise ValueError("create result requires only after_sha256")
            if self.result_file_id is None or self.backup_ref is not None:
                raise ValueError("create result requires result_file_id and no backup")
        elif self.operation is ChangeOperation.UPDATE:
            if self.before_sha256 is None or self.after_sha256 is None:
                raise ValueError("update result requires both hashes")
            if self.before_sha256 == self.after_sha256:
                raise ValueError("update result must change file content")
            if self.result_file_id is None or self.backup_ref is None:
                raise ValueError("update result requires result_file_id and backup")
        else:
            if self.before_sha256 is None or self.after_sha256 is not None:
                raise ValueError("delete result requires only before_sha256")
            if self.result_file_id is not None or self.backup_ref is None:
                raise ValueError("delete result requires backup and no result_file_id")
        if set(self.accepted_hunk_ids) & set(self.rejected_hunk_ids):
            raise ValueError("accepted and rejected hunk IDs must be disjoint")
        if len(self.accepted_hunk_ids) != len(set(self.accepted_hunk_ids)):
            raise ValueError("accepted hunk IDs must be unique")
        if len(self.rejected_hunk_ids) != len(set(self.rejected_hunk_ids)):
            raise ValueError("rejected hunk IDs must be unique")
        return self


class WritebackManifest(ChangeSetModel):
    schema_id: Literal["zhimeng.writeback-manifest.v1"] = Field(
        default="zhimeng.writeback-manifest.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    project_id: ProjectId
    base_revision: int = Field(ge=1)
    base_fingerprint: Sha256
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    outcome: Literal["applied", "rolled_back"]
    files: list[WritebackFileResult] = Field(min_length=1, max_length=MAX_CHANGE_COUNT)
    created_directories: list[RelativeProjectDirectory] = Field(
        default_factory=list,
        json_schema_extra={"uniqueItems": True},
    )
    created_at: datetime

    @model_validator(mode="after")
    def validate_manifest_sets(self) -> "WritebackManifest":
        if len({item.change_id for item in self.files}) != len(self.files):
            raise ValueError("writeback change IDs must be unique")
        if len({item.relative_path.casefold() for item in self.files}) != len(self.files):
            raise ValueError("writeback paths must be unique")
        if len(self.created_directories) != len(set(self.created_directories)):
            raise ValueError("created_directories must be unique")
        return self



EvidenceReceiptId = Annotated[str, Field(pattern=r"^writeback-evidence-[0-9a-f]{32}$")]


class WritebackEvidenceReceipt(ChangeSetModel):
    """Author-visible writeback evidence after Apply/Rollback.

    This is a product-native receipt for the post-writeback evidence gap. It does
    not implement original NLCL chapter/ops/usage semantics or auto state sync.
    """

    schema_id: Literal["zhimeng.writeback-evidence-receipt.v1"] = Field(
        default="zhimeng.writeback-evidence-receipt.v1",
        alias="schema",
    )
    receipt_id: EvidenceReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back"]
    writeback_manifest_hash: Sha256
    base_revision: int = Field(ge=1)
    base_fingerprint: Sha256
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    context_pack_id: str | None = None
    context_pack_hash: Sha256 | None = None
    route_receipt_id: str | None = None
    route_receipt_hash: Sha256 | None = None
    knowledge_fingerprint: Sha256 | None = None
    selected_card_ids: list[str] = Field(default_factory=list, max_length=64)
    source_run_id: str | None = None
    formal_draft_authorization_receipt_id: str | None = Field(
        default=None,
        pattern=r"^formal-draft-auth-[0-9a-f]{32}$",
    )
    formal_draft_authorization_receipt_hash: Sha256 | None = None
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> "WritebackEvidenceReceipt":
        if len(self.selected_card_ids) != len(set(self.selected_card_ids)):
            raise ValueError("selected_card_ids must be unique")
        if (self.context_pack_id is None) != (self.context_pack_hash is None):
            raise ValueError("context pack id/hash must be paired")
        if (self.route_receipt_id is None) != (self.route_receipt_hash is None):
            raise ValueError("route receipt id/hash must be paired")
        if (
            self.formal_draft_authorization_receipt_id is None
        ) != (self.formal_draft_authorization_receipt_hash is None):
            raise ValueError(
                "formal draft authorization receipt id/hash must be paired"
            )
        if self.formal_draft_allowed is not False:
            raise ValueError("writeback evidence cannot allow formal draft")
        return self


class WritebackEvidenceResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.writeback-evidence-response.v1"] = Field(
        default="zhimeng.writeback-evidence-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: WritebackEvidenceReceipt | None = None
    history: list[WritebackEvidenceReceipt] = Field(default_factory=list, max_length=32)


NlclWritebackSyncReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-writeback-sync-[0-9a-f]{32}$")
]


class NlclWritebackSyncRequest(ChangeSetModel):
    """Author-triggered product-native NLCL writeback evidence sync."""

    expected_writeback_evidence_receipt_hash: Sha256


class NlclWritebackSyncReceipt(ChangeSetModel):
    """Product-native continuity receipt after writeback evidence exists.

    Closes the post-writeback evidence-sync gap for Runtime continuity without
    claiming original NLCL chapter/ops/usage state mutation or formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-writeback-sync-receipt.v1"] = Field(
        default="zhimeng.nlcl-writeback-sync-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclWritebackSyncReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    writeback_evidence_receipt_id: EvidenceReceiptId
    writeback_evidence_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    affected_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    context_pack_id: str | None = None
    context_pack_hash: Sha256 | None = None
    route_receipt_id: str | None = None
    route_receipt_hash: Sha256 | None = None
    knowledge_fingerprint: Sha256 | None = None
    selected_card_ids: list[str] = Field(default_factory=list, max_length=64)
    source_run_id: str | None = None
    sync_scope: Literal["product_native_evidence_continuity"] = (
        "product_native_evidence_continuity"
    )
    original_nlcl_chapter_ops_usage_synced: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_sync_receipt(self) -> "NlclWritebackSyncReceipt":
        if len(self.selected_card_ids) != len(set(self.selected_card_ids)):
            raise ValueError("selected_card_ids must be unique")
        if len(self.affected_relative_paths) != len(
            set(path.casefold() for path in self.affected_relative_paths)
        ):
            raise ValueError("affected_relative_paths must be unique")
        if len(self.chapter_relative_paths) != len(
            set(path.casefold() for path in self.chapter_relative_paths)
        ):
            raise ValueError("chapter_relative_paths must be unique")
        if not set(path.casefold() for path in self.chapter_relative_paths).issubset(
            set(path.casefold() for path in self.affected_relative_paths)
        ):
            raise ValueError("chapter_relative_paths must be subset of affected paths")
        if (self.context_pack_id is None) != (self.context_pack_hash is None):
            raise ValueError("context pack id/hash must be paired")
        if (self.route_receipt_id is None) != (self.route_receipt_hash is None):
            raise ValueError("route receipt id/hash must be paired")
        return self


class NlclWritebackSyncResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-writeback-sync-response.v1"] = Field(
        default="zhimeng.nlcl-writeback-sync-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclWritebackSyncReceipt | None = None
    history: list[NlclWritebackSyncReceipt] = Field(default_factory=list, max_length=32)



NlclCallUsageContinuityReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-call-usage-[0-9a-f]{32}$")
]


class NlclCallUsageContinuityRequest(ChangeSetModel):
    """Author-triggered product-native NLCL call_usage continuity after writeback sync."""

    expected_nlcl_writeback_sync_receipt_hash: Sha256


class NlclCallUsageContinuityReceipt(ChangeSetModel):
    """Product-native call_usage continuity bound to run/route/selected cards.

    Closes the post-writeback usage continuity gap for Runtime without claiming
    original novel-kb-manager call_usage.jsonl mutation or chapter/ops state sync.
    """

    schema_id: Literal["zhimeng.nlcl-call-usage-continuity-receipt.v1"] = Field(
        default="zhimeng.nlcl-call-usage-continuity-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclCallUsageContinuityReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_writeback_sync_receipt_id: NlclWritebackSyncReceiptId
    nlcl_writeback_sync_receipt_hash: Sha256
    writeback_evidence_receipt_id: EvidenceReceiptId
    writeback_evidence_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    context_pack_id: str | None = None
    context_pack_hash: Sha256 | None = None
    route_receipt_id: str | None = None
    route_receipt_hash: Sha256 | None = None
    knowledge_fingerprint: Sha256 | None = None
    selected_card_ids: list[str] = Field(default_factory=list, max_length=64)
    used_card_ids: list[str] = Field(default_factory=list, max_length=64)
    unused_card_ids: list[str] = Field(default_factory=list, max_length=64)
    missed_card_ids: list[str] = Field(default_factory=list, max_length=64)
    source_run_id: str | None = None
    usage_scope: Literal["product_native_selected_card_continuity"] = (
        "product_native_selected_card_continuity"
    )
    record_mode: Literal["product_native_writeback_bound"] = (
        "product_native_writeback_bound"
    )
    original_nlcl_call_usage_recorded: Literal[False] = False
    original_nlcl_chapter_ops_usage_synced: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_usage_receipt(self) -> "NlclCallUsageContinuityReceipt":
        for field_name in (
            "selected_card_ids",
            "used_card_ids",
            "unused_card_ids",
            "missed_card_ids",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if len(self.chapter_relative_paths) != len(
            set(path.casefold() for path in self.chapter_relative_paths)
        ):
            raise ValueError("chapter_relative_paths must be unique")
        selected = set(self.selected_card_ids)
        if not set(self.used_card_ids).issubset(selected):
            raise ValueError("used_card_ids must be subset of selected_card_ids")
        if not set(self.unused_card_ids).issubset(selected):
            raise ValueError("unused_card_ids must be subset of selected_card_ids")
        overlap = set(self.used_card_ids) & set(self.unused_card_ids)
        if overlap:
            raise ValueError("used and unused card IDs must be disjoint")
        if set(self.missed_card_ids) & selected:
            raise ValueError("missed_card_ids cannot overlap selected_card_ids")
        if (self.context_pack_id is None) != (self.context_pack_hash is None):
            raise ValueError("context pack id/hash must be paired")
        if (self.route_receipt_id is None) != (self.route_receipt_hash is None):
            raise ValueError("route receipt id/hash must be paired")
        return self


class NlclCallUsageContinuityResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-call-usage-continuity-response.v1"] = Field(
        default="zhimeng.nlcl-call-usage-continuity-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclCallUsageContinuityReceipt | None = None
    history: list[NlclCallUsageContinuityReceipt] = Field(
        default_factory=list,
        max_length=32,
    )


NlclOriginalCallUsageExportReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-call-usage-[0-9a-f]{32}$")
]


class NlclOriginalCallUsageExportRequest(ChangeSetModel):
    """Author-triggered original-compatible call_usage.jsonl export bridge."""

    expected_nlcl_call_usage_continuity_receipt_hash: Sha256


class NlclOriginalCallUsageExportReceipt(ChangeSetModel):
    """Bridge receipt for writing original-compatible call_usage.jsonl into the novel project.

    This records a product-bound append-only row compatible with novel-kb-manager
    call_usage shape. It does not mutate original chapter/ops state files and does
    not authorize formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-call-usage-export-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-call-usage-export-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalCallUsageExportReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_call_usage_continuity_receipt_id: NlclCallUsageContinuityReceiptId
    nlcl_call_usage_continuity_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    used_card_ids: list[str] = Field(default_factory=list, max_length=64)
    unused_card_ids: list[str] = Field(default_factory=list, max_length=64)
    missed_card_ids: list[str] = Field(default_factory=list, max_length=64)
    source_run_id: str | None = None
    export_relative_path: RelativeProjectPath
    export_absolute_path: str = Field(min_length=1, max_length=4096)
    export_file_sha256: Sha256
    export_byte_offset: int = Field(ge=0)
    export_row_sha256: Sha256
    record_mode: Literal["product_bridge_from_continuity"] = (
        "product_bridge_from_continuity"
    )
    original_nlcl_call_usage_recorded: Literal[True] = True
    original_nlcl_chapter_ops_usage_synced: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class NlclOriginalCallUsageExportResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-call-usage-export-response.v1"] = Field(
        default="zhimeng.nlcl-original-call-usage-export-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalCallUsageExportReceipt | None = None
    history: list[NlclOriginalCallUsageExportReceipt] = Field(
        default_factory=list,
        max_length=32,
    )




NlclOriginalChapterOpsUsageExportReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-chapter-ops-usage-[0-9a-f]{32}$")
]


class NlclOriginalChapterOpsUsageExportRequest(ChangeSetModel):
    """Author-triggered original-compatible chapter/ops usage bridge after call_usage export."""

    expected_nlcl_original_call_usage_export_receipt_hash: Sha256


class NlclOriginalChapterOpsUsageExportReceipt(ChangeSetModel):
    """Bridge receipt for chapter/ops usage continuity into the novel project.

    Records an append-only chapter/ops usage row derived from the original-compatible
    call_usage export. It does not mutate original character/plot/foreshadowing state
    files and does not authorize formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-chapter-ops-usage-export-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-chapter-ops-usage-export-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalChapterOpsUsageExportReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_call_usage_export_receipt_id: NlclOriginalCallUsageExportReceiptId
    nlcl_original_call_usage_export_receipt_hash: Sha256
    nlcl_call_usage_continuity_receipt_id: NlclCallUsageContinuityReceiptId
    nlcl_call_usage_continuity_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    used_card_ids: list[str] = Field(default_factory=list, max_length=64)
    unused_card_ids: list[str] = Field(default_factory=list, max_length=64)
    missed_card_ids: list[str] = Field(default_factory=list, max_length=64)
    source_run_id: str | None = None
    export_relative_path: RelativeProjectPath
    export_absolute_path: str = Field(min_length=1, max_length=4096)
    export_file_sha256: Sha256
    export_byte_offset: int = Field(ge=0)
    export_row_sha256: Sha256
    record_mode: Literal["product_bridge_chapter_ops_from_call_usage_export"] = (
        "product_bridge_chapter_ops_from_call_usage_export"
    )
    original_nlcl_call_usage_recorded: Literal[True] = True
    original_nlcl_chapter_ops_usage_synced: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class NlclOriginalChapterOpsUsageExportResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-chapter-ops-usage-export-response.v1"] = Field(
        default="zhimeng.nlcl-original-chapter-ops-usage-export-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalChapterOpsUsageExportReceipt | None = None
    history: list[NlclOriginalChapterOpsUsageExportReceipt] = Field(
        default_factory=list,
        max_length=32,
    )


NlclOriginalNovelStateBridgeReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-novel-state-bridge-[0-9a-f]{32}$")
]


class NlclNovelStateFileRef(ChangeSetModel):
    """Read-only inventory of a project truth file related to novel state domains."""

    relative_path: RelativeProjectPath
    source_hash: Sha256
    domain: Literal["character", "plot", "foreshadowing", "project_status"]


class NlclOriginalNovelStateBridgeRequest(ChangeSetModel):
    """Author-triggered novel-state inventory bridge after chapter/ops usage export."""

    expected_nlcl_original_chapter_ops_usage_export_receipt_hash: Sha256


class NlclOriginalNovelStateBridgeReceipt(ChangeSetModel):
    """Bridge receipt inventorying character/plot/foreshadow/project_status files.

    Appends an evidence row under nlcl_feedback. It does not mutate original
    character/plot/foreshadowing/project_status state files and keeps
    novel_state_synced=false.
    """

    schema_id: Literal["zhimeng.nlcl-original-novel-state-bridge-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-novel-state-bridge-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalNovelStateBridgeReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_chapter_ops_usage_export_receipt_id: NlclOriginalChapterOpsUsageExportReceiptId
    nlcl_original_chapter_ops_usage_export_receipt_hash: Sha256
    nlcl_original_call_usage_export_receipt_id: NlclOriginalCallUsageExportReceiptId
    nlcl_original_call_usage_export_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    character_state_refs: list[NlclNovelStateFileRef] = Field(
        default_factory=list,
        max_length=64,
    )
    plot_state_refs: list[NlclNovelStateFileRef] = Field(
        default_factory=list,
        max_length=64,
    )
    foreshadowing_state_refs: list[NlclNovelStateFileRef] = Field(
        default_factory=list,
        max_length=64,
    )
    project_status_refs: list[NlclNovelStateFileRef] = Field(
        default_factory=list,
        max_length=64,
    )
    source_run_id: str | None = None
    export_relative_path: RelativeProjectPath
    export_absolute_path: str = Field(min_length=1, max_length=4096)
    export_file_sha256: Sha256
    export_byte_offset: int = Field(ge=0)
    export_row_sha256: Sha256
    record_mode: Literal["product_bridge_novel_state_inventory_from_chapter_ops"] = (
        "product_bridge_novel_state_inventory_from_chapter_ops"
    )
    original_nlcl_call_usage_recorded: Literal[True] = True
    original_nlcl_chapter_ops_usage_synced: Literal[True] = True
    original_nlcl_novel_state_bridge_recorded: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class NlclOriginalNovelStateBridgeResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-novel-state-bridge-response.v1"] = Field(
        default="zhimeng.nlcl-original-novel-state-bridge-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalNovelStateBridgeReceipt | None = None
    history: list[NlclOriginalNovelStateBridgeReceipt] = Field(
        default_factory=list,
        max_length=32,
    )


NlclOriginalNovelStateSyncReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-novel-state-sync-[0-9a-f]{32}$")
]


class NlclOriginalNovelStateSyncRequest(ChangeSetModel):
    """Author-triggered original-compatible novel state file writeback."""

    expected_nlcl_original_novel_state_bridge_receipt_hash: Sha256


class NlclOriginalNovelStateWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:state|07_[^/]+/nlcl_state)/"
                r"(?:project_status|character_state|plot_state|foreshadowing_state|"
                r"truth_character_matrix|truth_foreshadow_lifecycle|truth_info_boundary)\.json$"
            ),
        ),
    ]
    source_hash: Sha256
    domain: Literal[
        "character",
        "plot",
        "foreshadowing",
        "project_status",
        "truth_character_matrix",
        "truth_foreshadow_lifecycle",
        "truth_info_boundary",
    ]
    created: bool


class NlclOriginalNovelStateSyncReceipt(ChangeSetModel):
    """Receipt for writing original-compatible state/*.json after inventory bridge.

    Writes project-root state/project_status.json, character_state.json,
    plot_state.json, foreshadowing_state.json and the truth-matrix triad
    (truth_character_matrix/truth_foreshadow_lifecycle/truth_info_boundary).
    Does not mutate 03/04 truth markdown and does not authorize formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-novel-state-sync-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-novel-state-sync-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalNovelStateSyncReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_novel_state_bridge_receipt_id: NlclOriginalNovelStateBridgeReceiptId
    nlcl_original_novel_state_bridge_receipt_hash: Sha256
    nlcl_original_chapter_ops_usage_export_receipt_id: NlclOriginalChapterOpsUsageExportReceiptId
    nlcl_original_chapter_ops_usage_export_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    chapter_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    written_files: list[NlclOriginalNovelStateWrittenFile] = Field(
        min_length=7,
        max_length=7,
    )
    state_root_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^(?:state|07_[^/]+/nlcl_state)$",
        ),
    ]
    record_mode: Literal["product_bridge_original_state_json_writeback"] = (
        "product_bridge_original_state_json_writeback"
    )
    original_nlcl_call_usage_recorded: Literal[True] = True
    original_nlcl_chapter_ops_usage_synced: Literal[True] = True
    original_nlcl_novel_state_bridge_recorded: Literal[True] = True
    original_nlcl_novel_state_synced: Literal[True] = True
    original_nlcl_truth_matrix_synced: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_written_files(self) -> "NlclOriginalNovelStateSyncReceipt":
        domains = [item.domain for item in self.written_files]
        expected = [
            "character",
            "plot",
            "foreshadowing",
            "project_status",
            "truth_character_matrix",
            "truth_foreshadow_lifecycle",
            "truth_info_boundary",
        ]
        if sorted(domains) != sorted(expected):
            raise ValueError("written_files must cover all seven original state domains")
        paths = [item.relative_path.casefold() for item in self.written_files]
        if len(paths) != len(set(paths)):
            raise ValueError("written_files relative_path values must be unique")
        return self


class NlclOriginalNovelStateSyncResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-novel-state-sync-response.v1"] = Field(
        default="zhimeng.nlcl-original-novel-state-sync-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalNovelStateSyncReceipt | None = None
    history: list[NlclOriginalNovelStateSyncReceipt] = Field(
        default_factory=list,
        max_length=32,
    )



NlclOriginalPrewriteContextReportReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-prewrite-context-report-[0-9a-f]{32}$")
]


class NlclOriginalPrewriteContextReportRequest(ChangeSetModel):
    """Author-triggered original-compatible prewrite context report export."""

    expected_nlcl_original_novel_state_sync_receipt_hash: Sha256


class NlclOriginalPrewriteContextReportWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_prewrite_context_report_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalPrewriteContextReportReceipt(ChangeSetModel):
    """Receipt for writing product-native prewrite context report after state sync.

    Writes chapter_context/*_prewrite_context_report_latest.{json,md} as a compressed
    handoff for the next chapter. Does not authorize formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-prewrite-context-report-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-prewrite-context-report-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalPrewriteContextReportReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_novel_state_sync_receipt_id: NlclOriginalNovelStateSyncReceiptId
    nlcl_original_novel_state_sync_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    written_files: list[NlclOriginalPrewriteContextReportWrittenFile] = Field(
        min_length=4,
        max_length=4,
    )
    report_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_prewrite_context_report_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_prewrite_context_report"] = (
        "product_bridge_prewrite_context_report"
    )
    original_nlcl_novel_state_synced: Literal[True] = True
    original_nlcl_truth_matrix_synced: Literal[True] = True
    prewrite_context_report_synced: Literal[True] = True
    bounded_library_activated: bool = False
    nlcl_original_bounded_library_activation_receipt_id: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^nlcl-original-bounded-library-activation-[0-9a-f]{32}$",
        ),
    ] = None
    nlcl_original_bounded_library_activation_receipt_hash: Sha256 | None = None
    activated_source_count: Annotated[int, Field(ge=0, le=64)] = 0
    whole_library_load_forbidden: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_written_files(self) -> "NlclOriginalPrewriteContextReportReceipt":
        kinds = sorted(item.kind for item in self.written_files)
        if kinds != ["json_latest", "json_stage", "md_latest", "md_stage"]:
            raise ValueError("written_files must cover json/md latest and stage kinds")
        paths = [item.relative_path.casefold() for item in self.written_files]
        if len(paths) != len(set(paths)):
            raise ValueError("written_files relative_path values must be unique")
        has_activation = self.nlcl_original_bounded_library_activation_receipt_id is not None
        has_hash = self.nlcl_original_bounded_library_activation_receipt_hash is not None
        if has_activation != has_hash:
            raise ValueError("activation receipt id and hash must be provided together")
        if self.bounded_library_activated != has_activation:
            raise ValueError("bounded_library_activated must match activation binding")
        if not self.bounded_library_activated and self.activated_source_count != 0:
            raise ValueError("inactive prewrite cannot claim activated sources")
        if not self.whole_library_load_forbidden:
            raise ValueError("prewrite must forbid whole-library load")
        return self


class NlclOriginalPrewriteContextReportResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-prewrite-context-report-response.v1"] = Field(
        default="zhimeng.nlcl-original-prewrite-context-report-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalPrewriteContextReportReceipt | None = None
    history: list[NlclOriginalPrewriteContextReportReceipt] = Field(
        default_factory=list,
        max_length=32,
    )





NlclOriginalTruthContentExtractReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-truth-content-extract-[0-9a-f]{32}$")
]


class NlclOriginalTruthContentExtractRequest(ChangeSetModel):
    """Author-triggered deterministic extraction from project 01-09 into state truth files."""

    expected_nlcl_original_novel_state_sync_receipt_hash: Sha256


class NlclOriginalTruthContentExtractSourceFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=r"^(?:0[1-9]_[^/]+(?:/[^/]+){0,6})\.(?:md|markdown|txt)$",
        ),
    ]
    source_hash: Sha256
    section_index: Annotated[int, Field(ge=1, le=9)]
    extracted_item_count: Annotated[int, Field(ge=0, le=512)]


class NlclOriginalTruthContentExtractWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:state|07_[^/]+/nlcl_state)/"
                r"(?:character_state|plot_state|foreshadowing_state|"
                r"truth_character_matrix|truth_foreshadow_lifecycle|truth_info_boundary)\.json$"
            ),
        ),
    ]
    source_hash: Sha256
    domain: Literal[
        "character",
        "plot",
        "foreshadowing",
        "truth_character_matrix",
        "truth_foreshadow_lifecycle",
        "truth_info_boundary",
    ]
    created: bool
    item_count: Annotated[int, Field(ge=0, le=2048)]


class NlclOriginalTruthContentExtractReceipt(ChangeSetModel):
    """Receipt for deterministic 01-09 truth content extraction into state/*.json.

    Reads project truth markdown under 01-04/06 and merges extracted compact items
    into existing original-compatible state files. Never rewrites 01-09 files and
    never authorizes formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-truth-content-extract-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-truth-content-extract-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalTruthContentExtractReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_novel_state_sync_receipt_id: NlclOriginalNovelStateSyncReceiptId
    nlcl_original_novel_state_sync_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    source_files: list[NlclOriginalTruthContentExtractSourceFile] = Field(
        default_factory=list,
        max_length=128,
    )
    written_files: list[NlclOriginalTruthContentExtractWrittenFile] = Field(
        min_length=6,
        max_length=6,
    )
    extracted_character_count: Annotated[int, Field(ge=0, le=2048)]
    extracted_foreshadow_count: Annotated[int, Field(ge=0, le=2048)]
    extracted_info_boundary_count: Annotated[int, Field(ge=0, le=2048)]
    record_mode: Literal["product_bridge_truth_content_extract_from_01_09"] = (
        "product_bridge_truth_content_extract_from_01_09"
    )
    original_nlcl_novel_state_synced: Literal[True] = True
    original_nlcl_truth_matrix_synced: Literal[True] = True
    truth_content_extracted: Literal[True] = True
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_written_files(self) -> "NlclOriginalTruthContentExtractReceipt":
        domains = sorted(item.domain for item in self.written_files)
        expected = sorted(
            [
                "character",
                "plot",
                "foreshadowing",
                "truth_character_matrix",
                "truth_foreshadow_lifecycle",
                "truth_info_boundary",
            ]
        )
        if domains != expected:
            raise ValueError("written_files must cover all six extract target domains")
        paths = [item.relative_path.casefold() for item in self.written_files]
        if len(paths) != len(set(paths)):
            raise ValueError("written_files relative_path values must be unique")
        return self


class NlclOriginalTruthContentExtractResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-truth-content-extract-response.v1"] = Field(
        default="zhimeng.nlcl-original-truth-content-extract-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalTruthContentExtractReceipt | None = None
    history: list[NlclOriginalTruthContentExtractReceipt] = Field(
        default_factory=list,
        max_length=32,
    )




NlclOriginalMandatoryUsagePlanReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-mandatory-usage-plan-[0-9a-f]{32}$")
]


class NlclOriginalMandatoryUsagePlanRequest(ChangeSetModel):
    """Author-triggered mandatory usage plan scaffold after truth content extract."""

    expected_nlcl_original_truth_content_extract_receipt_hash: Sha256


class NlclOriginalMandatoryUsagePlanItem(ChangeSetModel):
    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal["character", "foreshadow", "plot_thread", "info_boundary", "selected_card"]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    source_relative_path: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    required_action: Annotated[str, Field(min_length=1, max_length=512)]
    status: Literal["pending", "used", "unused", "missed", "suppressed_for_project_truth"] = (
        "pending"
    )


class NlclOriginalMandatoryUsagePlanWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_mandatory_usage_plan_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalMandatoryUsagePlanReceipt(ChangeSetModel):
    """Receipt for product-bridge mandatory usage plan scaffold.

    Builds a pending plan from extracted truth content. Never marks
    mandatory_usage_closure_closed=true and never authorizes formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalMandatoryUsagePlanReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_truth_content_extract_receipt_id: NlclOriginalTruthContentExtractReceiptId
    nlcl_original_truth_content_extract_receipt_hash: Sha256
    nlcl_original_novel_state_sync_receipt_id: NlclOriginalNovelStateSyncReceiptId
    nlcl_original_novel_state_sync_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    items: list[NlclOriginalMandatoryUsagePlanItem] = Field(
        default_factory=list,
        max_length=512,
    )
    written_files: list[NlclOriginalMandatoryUsagePlanWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    plan_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_mandatory_usage_plan_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_mandatory_usage_plan_scaffold"] = (
        "product_bridge_mandatory_usage_plan_scaffold"
    )
    original_nlcl_novel_state_synced: Literal[True] = True
    original_nlcl_truth_matrix_synced: Literal[True] = True
    truth_content_extracted: Literal[True] = True
    mandatory_usage_plan_synced: Literal[True] = True
    mandatory_usage_closure_closed: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256


class NlclOriginalMandatoryUsagePlanResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-mandatory-usage-plan-response.v1"] = Field(
        default="zhimeng.nlcl-original-mandatory-usage-plan-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalMandatoryUsagePlanReceipt | None = None
    history: list[NlclOriginalMandatoryUsagePlanReceipt] = Field(
        default_factory=list,
        max_length=32,
    )



NlclOriginalMandatoryUsageClosureReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-mandatory-usage-closure-[0-9a-f]{32}$")
]


class NlclOriginalMandatoryUsageClosureDecision(ChangeSetModel):
    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["used", "unused", "missed", "suppressed_for_project_truth"]
    note: Annotated[str, Field(default="", max_length=500)] = ""


class NlclOriginalMandatoryUsageClosureRequest(ChangeSetModel):
    """Author decisions against a mandatory usage plan scaffold.

    Closing requires every plan item to receive a non-pending status. Closure
    never authorizes formal prose and never claims route validated.
    """

    expected_nlcl_original_mandatory_usage_plan_receipt_hash: Sha256
    decisions: list[NlclOriginalMandatoryUsageClosureDecision] = Field(
        min_length=1,
        max_length=512,
    )
    finalize: bool = True


class NlclOriginalMandatoryUsageClosureItem(ChangeSetModel):
    item_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal["character", "foreshadow", "plot_thread", "info_boundary", "selected_card"]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    source_relative_path: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    required_action: Annotated[str, Field(min_length=1, max_length=512)]
    status: Literal["used", "unused", "missed", "suppressed_for_project_truth"]
    note: Annotated[str, Field(default="", max_length=500)] = ""


class NlclOriginalMandatoryUsageClosureWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_mandatory_usage_closure_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalMandatoryUsageClosureReceipt(ChangeSetModel):
    """Author-closed mandatory usage closure bound to a plan scaffold.

    May set mandatory_usage_closure_closed=true only when every item is decided.
    Still never sets formal_draft_allowed or quality_assessed.
    """

    schema_id: Literal["zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalMandatoryUsageClosureReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_mandatory_usage_plan_receipt_id: NlclOriginalMandatoryUsagePlanReceiptId
    nlcl_original_mandatory_usage_plan_receipt_hash: Sha256
    nlcl_original_truth_content_extract_receipt_id: NlclOriginalTruthContentExtractReceiptId
    nlcl_original_truth_content_extract_receipt_hash: Sha256
    nlcl_original_novel_state_sync_receipt_id: NlclOriginalNovelStateSyncReceiptId
    nlcl_original_novel_state_sync_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    items: list[NlclOriginalMandatoryUsageClosureItem] = Field(
        min_length=1,
        max_length=512,
    )
    written_files: list[NlclOriginalMandatoryUsageClosureWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    closure_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_mandatory_usage_closure_latest\.json$"
            ),
        ),
    ]
    overall_status: Literal["closed"] = "closed"
    record_mode: Literal["product_bridge_mandatory_usage_closure_author_decided"] = (
        "product_bridge_mandatory_usage_closure_author_decided"
    )
    original_nlcl_novel_state_synced: Literal[True] = True
    original_nlcl_truth_matrix_synced: Literal[True] = True
    truth_content_extracted: Literal[True] = True
    mandatory_usage_plan_synced: Literal[True] = True
    mandatory_usage_closure_closed: Literal[True] = True
    route_validated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_closed_items(self) -> "NlclOriginalMandatoryUsageClosureReceipt":
        if any(item.status == "pending" for item in self.items):  # type: ignore[comparison-overlap]
            raise ValueError("closed closure cannot contain pending items")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("closure item_id values must be unique")
        return self


class NlclOriginalMandatoryUsageClosureResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-mandatory-usage-closure-response.v1"] = Field(
        default="zhimeng.nlcl-original-mandatory-usage-closure-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalMandatoryUsageClosureReceipt | None = None
    history: list[NlclOriginalMandatoryUsageClosureReceipt] = Field(
        default_factory=list,
        max_length=32,
    )



NlclOriginalOperationPackageLikeReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-operation-package-like-[0-9a-f]{32}$")
]


class NlclOriginalOperationPackageLikeRequest(ChangeSetModel):
    """Author-triggered operation_package_like scaffold after mandatory usage closure."""

    expected_nlcl_original_mandatory_usage_closure_receipt_hash: Sha256


class NlclOriginalOperationPackageLikeWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_operation_package_like_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalOperationPackageLikeReceipt(ChangeSetModel):
    """Receipt for product-bridge operation_package_like scaffold.

    readiness is fixed to risk_draft_only. Never authorizes formal prose.
    """

    schema_id: Literal["zhimeng.nlcl-original-operation-package-like-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-operation-package-like-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalOperationPackageLikeReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_mandatory_usage_closure_receipt_id: NlclOriginalMandatoryUsageClosureReceiptId
    nlcl_original_mandatory_usage_closure_receipt_hash: Sha256
    nlcl_original_mandatory_usage_plan_receipt_id: NlclOriginalMandatoryUsagePlanReceiptId
    nlcl_original_mandatory_usage_plan_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    operation_package_id: Annotated[
        str, Field(pattern=r"^product-bridge-operation-package-[0-9a-f]{32}$")
    ]
    package_hash: Sha256
    readiness: Literal["risk_draft_only"] = "risk_draft_only"
    written_files: list[NlclOriginalOperationPackageLikeWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    package_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_operation_package_like_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_operation_package_like_scaffold"] = (
        "product_bridge_operation_package_like_scaffold"
    )
    original_nlcl_novel_state_synced: Literal[True] = True
    truth_content_extracted: Literal[True] = True
    mandatory_usage_plan_synced: Literal[True] = True
    mandatory_usage_closure_closed: Literal[True] = True
    operation_package_like_synced: Literal[True] = True
    route_validated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256


class NlclOriginalOperationPackageLikeResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-operation-package-like-response.v1"] = Field(
        default="zhimeng.nlcl-original-operation-package-like-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalOperationPackageLikeReceipt | None = None
    history: list[NlclOriginalOperationPackageLikeReceipt] = Field(
        default_factory=list,
        max_length=32,
    )






NlclOriginalLibraryLayoutInventoryReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-library-layout-inventory-[0-9a-f]{32}$")
]


class NlclOriginalLibraryLayoutInventoryRequest(ChangeSetModel):
    """Author-triggered library layout inventory after operation_package_like."""

    expected_nlcl_original_operation_package_like_receipt_hash: Sha256


class NlclOriginalLibraryLayoutSlot(ChangeSetModel):
    slot_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal[
        "section_01",
        "section_02",
        "section_03",
        "section_04",
        "section_05",
        "section_06",
        "section_07",
        "section_08",
        "section_09",
        "state_file",
        "chapter_context",
    ]
    label: Annotated[str, Field(min_length=1, max_length=256)]
    relative_path: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    present: bool
    file_count: Annotated[int, Field(ge=0, le=100000)] = 0
    note: Annotated[str, Field(default="", max_length=500)] = ""


class NlclOriginalLibraryLayoutInventoryWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_library_layout_inventory_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalLibraryLayoutInventoryReceipt(ChangeSetModel):
    """Inventory of required library layout slots. Never claims full library complete."""

    schema_id: Literal["zhimeng.nlcl-original-library-layout-inventory-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-library-layout-inventory-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalLibraryLayoutInventoryReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_operation_package_like_receipt_id: NlclOriginalOperationPackageLikeReceiptId
    nlcl_original_operation_package_like_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    slots: list[NlclOriginalLibraryLayoutSlot] = Field(min_length=1, max_length=128)
    present_slot_count: Annotated[int, Field(ge=0, le=128)]
    missing_slot_count: Annotated[int, Field(ge=0, le=128)]
    written_files: list[NlclOriginalLibraryLayoutInventoryWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    inventory_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_library_layout_inventory_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_library_layout_inventory"] = (
        "product_bridge_library_layout_inventory"
    )
    operation_package_like_synced: Literal[True] = True
    library_layout_inventoried: Literal[True] = True
    library_layout_complete: Literal[False] = False
    whole_library_load_forbidden: Literal[True] = True
    route_validated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_counts(self) -> "NlclOriginalLibraryLayoutInventoryReceipt":
        present = sum(1 for item in self.slots if item.present)
        missing = sum(1 for item in self.slots if not item.present)
        if present != self.present_slot_count or missing != self.missing_slot_count:
            raise ValueError("library layout inventory slot counts must match slots")
        if self.library_layout_complete:
            raise ValueError("product bridge must not claim library_layout_complete")
        return self


class NlclOriginalLibraryLayoutInventoryResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-library-layout-inventory-response.v1"] = Field(
        default="zhimeng.nlcl-original-library-layout-inventory-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalLibraryLayoutInventoryReceipt | None = None
    history: list[NlclOriginalLibraryLayoutInventoryReceipt] = Field(
        default_factory=list,
        max_length=32,
    )


NlclOriginalLibraryLayoutCompletenessReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-library-layout-completeness-[0-9a-f]{32}$")
]


class NlclOriginalLibraryLayoutCompletenessRequest(ChangeSetModel):
    """Author-triggered layout completeness assessment after library layout inventory."""

    expected_nlcl_original_library_layout_inventory_receipt_hash: Sha256


class NlclOriginalLibraryLayoutCompletenessWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_library_layout_completeness_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalLibraryLayoutCompletenessReceipt(ChangeSetModel):
    """Assess whether inventoried library layout slots satisfy completeness.

    May set library_layout_complete=true only when all required slots are present.
    Never authorizes whole-library load or formal draft.
    """

    schema_id: Literal["zhimeng.nlcl-original-library-layout-completeness-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-library-layout-completeness-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalLibraryLayoutCompletenessReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_library_layout_inventory_receipt_id: NlclOriginalLibraryLayoutInventoryReceiptId
    nlcl_original_library_layout_inventory_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    slots: list[NlclOriginalLibraryLayoutSlot] = Field(min_length=1, max_length=128)
    required_slot_count: Annotated[int, Field(ge=1, le=128)]
    present_required_slot_count: Annotated[int, Field(ge=0, le=128)]
    missing_required_slot_count: Annotated[int, Field(ge=0, le=128)]
    completeness_status: Literal["complete", "incomplete"]
    written_files: list[NlclOriginalLibraryLayoutCompletenessWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    assessment_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_library_layout_completeness_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_library_layout_completeness"] = (
        "product_bridge_library_layout_completeness"
    )
    library_layout_inventoried: Literal[True] = True
    library_layout_assessed: Literal[True] = True
    library_layout_complete: bool
    whole_library_load_forbidden: Literal[True] = True
    route_validated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_completeness(self) -> "NlclOriginalLibraryLayoutCompletenessReceipt":
        present = sum(1 for item in self.slots if item.present)
        missing = sum(1 for item in self.slots if not item.present)
        if self.required_slot_count != len(self.slots):
            raise ValueError("required_slot_count must equal slots length")
        if present != self.present_required_slot_count or missing != self.missing_required_slot_count:
            raise ValueError("completeness slot counts must match slots")
        expected_complete = missing == 0
        if self.library_layout_complete != expected_complete:
            raise ValueError("library_layout_complete must match missing_required_slot_count==0")
        expected_status = "complete" if expected_complete else "incomplete"
        if self.completeness_status != expected_status:
            raise ValueError("completeness_status must match library_layout_complete")
        if not self.whole_library_load_forbidden:
            raise ValueError("product bridge must forbid whole-library load")
        if self.formal_draft_allowed or self.route_validated or self.quality_assessed:
            raise ValueError("completeness must not unlock formal draft or quality")
        return self


class NlclOriginalLibraryLayoutCompletenessResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-library-layout-completeness-response.v1"] = Field(
        default="zhimeng.nlcl-original-library-layout-completeness-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalLibraryLayoutCompletenessReceipt | None = None
    history: list[NlclOriginalLibraryLayoutCompletenessReceipt] = Field(
        default_factory=list,
        max_length=32,
    )





NlclOriginalBoundedLibraryActivationReceiptId = Annotated[
    str, Field(pattern=r"^nlcl-original-bounded-library-activation-[0-9a-f]{32}$")
]


class NlclOriginalBoundedLibraryActivationRequest(ChangeSetModel):
    """Author-triggered bounded library activation after layout completeness."""

    expected_nlcl_original_library_layout_completeness_receipt_hash: Sha256
    max_sources: Annotated[int, Field(default=16, ge=1, le=64)] = 16


class NlclOriginalBoundedLibraryActivationWrittenFile(ChangeSetModel):
    relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_bounded_library_activation_(?:latest|post_writeback)\.(?:json|md)$"
            ),
        ),
    ]
    source_hash: Sha256
    kind: Literal["json_latest", "md_latest", "json_stage", "md_stage"]
    created: bool


class NlclOriginalBoundedLibraryActivationSource(ChangeSetModel):
    source_id: Annotated[str, Field(min_length=1, max_length=512)]
    slot_id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal[
        "section_01",
        "section_02",
        "section_03",
        "section_04",
        "section_05",
        "section_06",
        "section_07",
        "section_08",
        "section_09",
        "state_file",
        "chapter_context",
    ]
    relative_path: Annotated[str, Field(min_length=1, max_length=512)]
    character_count: Annotated[int, Field(ge=0, le=10_000_000)]
    token_estimate: Annotated[int, Field(ge=0, le=10_000_000)]


class NlclOriginalBoundedLibraryActivationReceipt(ChangeSetModel):
    """Bounded activation of present library slots.

    Never authorizes whole-library load or formal draft.
    """

    schema_id: Literal["zhimeng.nlcl-original-bounded-library-activation-receipt.v1"] = Field(
        default="zhimeng.nlcl-original-bounded-library-activation-receipt.v1",
        alias="schema",
    )
    receipt_id: NlclOriginalBoundedLibraryActivationReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    nlcl_original_library_layout_completeness_receipt_id: NlclOriginalLibraryLayoutCompletenessReceiptId
    nlcl_original_library_layout_completeness_receipt_hash: Sha256
    outcome: Literal["applied", "rolled_back"]
    chapter_id: str = Field(min_length=1, max_length=128)
    canonical_chapter_key: str = Field(min_length=1, max_length=128)
    activated_sources: list[NlclOriginalBoundedLibraryActivationSource] = Field(
        min_length=0,
        max_length=64,
    )
    activated_source_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        min_length=0,
        max_length=64,
    )
    max_sources: Annotated[int, Field(ge=1, le=64)]
    activated_count: Annotated[int, Field(ge=0, le=64)]
    candidate_present_count: Annotated[int, Field(ge=0, le=128)]
    max_characters: Annotated[int, Field(ge=1, le=50_000_000)]
    used_characters: Annotated[int, Field(ge=0, le=50_000_000)]
    max_tokens: Annotated[int, Field(ge=1, le=50_000_000)]
    used_tokens: Annotated[int, Field(ge=0, le=50_000_000)]
    written_files: list[NlclOriginalBoundedLibraryActivationWrittenFile] = Field(
        min_length=2,
        max_length=4,
    )
    activation_relative_path: Annotated[
        str,
        Field(
            min_length=1,
            max_length=512,
            pattern=(
                r"^(?:chapter_context|07_[^/]+/nlcl_state/chapter_context)/"
                r"[A-Za-z0-9._-]+_bounded_library_activation_latest\.json$"
            ),
        ),
    ]
    record_mode: Literal["product_bridge_bounded_library_activation"] = (
        "product_bridge_bounded_library_activation"
    )
    library_layout_inventoried: Literal[True] = True
    library_layout_assessed: Literal[True] = True
    library_layout_complete: bool
    bounded_library_activated: Literal[True] = True
    whole_library_load: Literal[False] = False
    whole_library_load_forbidden: Literal[True] = True
    route_validated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[True] = True
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_activation(self) -> "NlclOriginalBoundedLibraryActivationReceipt":
        if self.activated_count != len(self.activated_sources):
            raise ValueError("activated_count must match activated_sources")
        if self.activated_count != len(self.activated_source_ids):
            raise ValueError("activated_count must match activated_source_ids")
        if self.activated_count > self.max_sources:
            raise ValueError("activated_count cannot exceed max_sources")
        ids = [item.source_id for item in self.activated_sources]
        if ids != self.activated_source_ids:
            raise ValueError("activated_source_ids must follow activated_sources order")
        if len(ids) != len(set(ids)):
            raise ValueError("activated sources must be unique")
        used_chars = sum(item.character_count for item in self.activated_sources)
        used_tokens = sum(item.token_estimate for item in self.activated_sources)
        if used_chars != self.used_characters or used_tokens != self.used_tokens:
            raise ValueError("budget used counts must match activated sources")
        if self.used_characters > self.max_characters or self.used_tokens > self.max_tokens:
            raise ValueError("activation budget overflow")
        if self.whole_library_load or not self.whole_library_load_forbidden:
            raise ValueError("product bridge must forbid whole-library load")
        if self.formal_draft_allowed or self.route_validated or self.quality_assessed:
            raise ValueError("bounded activation must not unlock formal draft or quality")
        return self


class NlclOriginalBoundedLibraryActivationResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.nlcl-original-bounded-library-activation-response.v1"] = Field(
        default="zhimeng.nlcl-original-bounded-library-activation-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: NlclOriginalBoundedLibraryActivationReceipt | None = None
    history: list[NlclOriginalBoundedLibraryActivationReceipt] = Field(
        default_factory=list,
        max_length=32,
    )






ReaderReviewReceiptId = Annotated[
    str, Field(pattern=r"^reader-review-[0-9a-f]{32}$")
]
ReaderFindingId = Annotated[
    str, Field(pattern=r"^reader-finding-[0-9a-f]{16}$")
]


class ReaderReviewRequest(ChangeSetModel):
    """Author-triggered deterministic cold-read Reader review for a ChangeSet candidate."""

    expected_result_fingerprint: Sha256 | None = None


class ReaderFinding(ChangeSetModel):
    finding_id: ReaderFindingId
    kind: Literal["fact_risk", "continuity_risk", "subjective_note"]
    severity: Literal["info", "watch", "concern"]
    summary: str = Field(min_length=1, max_length=500)
    evidence_quote: str = Field(min_length=0, max_length=1000)
    relative_path: RelativeProjectPath | None = None
    overrides_gate: Literal[False] = False


class ReaderReviewReceipt(ChangeSetModel):
    """Cold-read Reader opinion bound to a ChangeSet candidate revision.

    This is an isolated opinion surface. It does not override deterministic Gate
    results, does not authorize formal prose, and does not mutate novel state.
    """

    schema_id: Literal["zhimeng.reader-review-receipt.v1"] = Field(
        default="zhimeng.reader-review-receipt.v1",
        alias="schema",
    )
    receipt_id: ReaderReviewReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    outcome: Literal["applied", "rolled_back"]
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    review_mode: Literal["deterministic_cold_read_v1"] = "deterministic_cold_read_v1"
    evaluator_kind: Literal["deterministic_cold_read_v1"] = "deterministic_cold_read_v1"
    evaluator_id: Literal["zhimeng.reader.deterministic-cold-read"] = (
        "zhimeng.reader.deterministic-cold-read"
    )
    model_id: Literal["none"] = "none"
    prompt_fingerprint: Sha256 | None = None
    context_fingerprint: Sha256 | None = None
    evaluator_fingerprint: Sha256 | None = None
    allowed_input_classes: list[str] = Field(default_factory=list, max_length=16)
    excluded_input_classes: list[str] = Field(default_factory=list, max_length=16)
    candidate_relative_paths: list[RelativeProjectPath] = Field(
        default_factory=list,
        max_length=64,
    )
    findings: list[ReaderFinding] = Field(default_factory=list, max_length=64)
    finding_counts: dict[str, int] = Field(default_factory=dict)
    reader_opinion_recorded: Literal[True] = True
    overrides_gate: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class ReaderReviewResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.reader-review-response.v1"] = Field(
        default="zhimeng.reader-review-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: ReaderReviewReceipt | None = None
    history: list[ReaderReviewReceipt] = Field(default_factory=list, max_length=32)


class ChangeSetResponse(ChangeSetModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra=_add_change_set_response_schema_invariants,
    )
    schema_id: Literal["zhimeng.change-set.v1"] = Field(
        default="zhimeng.change-set.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    project_id: ProjectId
    base_revision: int = Field(ge=1)
    base_fingerprint: Sha256
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=8000)
    origin: ChangeSetOrigin
    source_run_id: str | None = None
    source_tool_call_id: str | None = None
    source_request_hash: Sha256 | None = None
    source_skill_ids: list[str] = Field(
        max_length=32,
        json_schema_extra={"uniqueItems": True},
    )
    gate_required: bool = False
    status: ChangeSetStatus
    review_revision: int = Field(ge=0)
    changes: list[CandidateFileChange] = Field(
        min_length=1,
        max_length=MAX_CHANGE_COUNT,
    )
    post_draft_gate: PostDraftGateReceipt | None = None
    writeback: WritebackManifest | None = None
    rollback: WritebackManifest | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_status(self) -> "ChangeSetResponse":
        decisions = [hunk.decision for change in self.changes for hunk in change.hunks]
        if len(self.source_skill_ids) != len(set(self.source_skill_ids)):
            raise ValueError("source_skill_ids must be unique")
        if self.origin is ChangeSetOrigin.SKILL_WORKFLOW and not self.source_skill_ids:
            raise ValueError("skill_workflow origin requires source_skill_ids")
        if self.origin is ChangeSetOrigin.AGENT:
            if (
                self.source_run_id is None
                or self.source_tool_call_id is None
                or self.source_request_hash is None
            ):
                raise ValueError(
                    "agent origin requires source_run_id, source_tool_call_id, "
                    "and source_request_hash"
                )
        elif self.source_tool_call_id is not None or self.source_request_hash is not None:
            raise ValueError("tool-call provenance is reserved for agent origin")
        if len({change.change_id for change in self.changes}) != len(self.changes):
            raise ValueError("change IDs must be unique within a change set")
        if len({change.relative_path.casefold() for change in self.changes}) != len(
            self.changes
        ):
            raise ValueError("change paths must be unique within a change set")
        hunk_ids = [hunk.hunk_id for change in self.changes for hunk in change.hunks]
        if len(hunk_ids) != len(set(hunk_ids)):
            raise ValueError("hunk IDs must be unique within a change set")
        if self.status is ChangeSetStatus.REVIEWING and not any(
            decision is HunkDecision.PENDING for decision in decisions
        ):
            raise ValueError("reviewing change set must contain pending hunks")
        if self.status is not ChangeSetStatus.REVIEWING and any(
            decision is HunkDecision.PENDING for decision in decisions
        ):
            raise ValueError("only reviewing change sets can contain pending hunks")
        if self.created_at > self.updated_at:
            raise ValueError("change set updated_at cannot precede created_at")
        if self.status in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            if self.writeback is None:
                raise ValueError("applied change set requires a writeback manifest")
        if self.status is ChangeSetStatus.ROLLED_BACK and self.rollback is None:
            raise ValueError("rolled_back change set requires a rollback manifest")
        if self.status is not ChangeSetStatus.ROLLED_BACK and self.rollback is not None:
            raise ValueError("only rolled_back change sets can carry a rollback manifest")
        if self.post_draft_gate is not None:
            if (
                self.post_draft_gate.change_set_id != self.change_set_id
                or self.post_draft_gate.project_id != self.project_id
            ):
                raise ValueError("post-draft Gate receipt identity must match its change set")
        for manifest, expected_outcome in (
            (self.writeback, "applied"),
            (self.rollback, "rolled_back"),
        ):
            if manifest is None:
                continue
            if (
                manifest.change_set_id != self.change_set_id
                or manifest.project_id != self.project_id
            ):
                raise ValueError("writeback identity must match its change set")
            if manifest.outcome != expected_outcome:
                raise ValueError("writeback outcome does not match its response slot")
        return self


def change_set_persisted_payload(record: ChangeSetResponse) -> dict[str, object]:
    """Return the canonical payload kept in the append-only ChangeSet row."""

    return record.model_dump(
        mode="json",
        by_alias=True,
        exclude={"post_draft_gate"},
    )


def change_set_payload_hash(record: ChangeSetResponse) -> str:
    return canonical_json_hash(change_set_persisted_payload(record))


class ChangeSetSummary(ChangeSetModel):
    change_set_id: ChangeSetId
    project_id: ProjectId
    title: str
    origin: ChangeSetOrigin
    source_run_id: str | None = None
    source_tool_call_id: str | None = None
    gate_required: bool = False
    status: ChangeSetStatus
    review_revision: int = Field(ge=0)
    file_count: int = Field(ge=1)
    hunk_count: int = Field(ge=1)
    pending_hunk_count: int = Field(ge=0)
    accepted_hunk_count: int = Field(ge=0)
    rejected_hunk_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_hunk_counts(self) -> "ChangeSetSummary":
        if (
            self.pending_hunk_count
            + self.accepted_hunk_count
            + self.rejected_hunk_count
            != self.hunk_count
        ):
            raise ValueError("summary hunk decision counts must equal hunk_count")
        return self


class ChangeSetListResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.change-set-list.v1"] = Field(
        default="zhimeng.change-set-list.v1",
        alias="schema",
    )
    change_sets: list[ChangeSetSummary]


class HunkDecisionUpdate(ChangeSetModel):
    hunk_id: HunkId
    decision: Literal[HunkDecision.ACCEPTED, HunkDecision.REJECTED]


class ChangeSetDecisionRequest(ChangeSetModel):
    expected_review_revision: int = Field(ge=0)
    decisions: list[HunkDecisionUpdate] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_unique_hunks(self) -> "ChangeSetDecisionRequest":
        ids = [item.hunk_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("decision hunk IDs must be unique")
        return self


class ChangeSetApplyRequest(ChangeSetModel):
    expected_review_revision: int = Field(ge=0)


class ChangeSetRollbackRequest(ChangeSetModel):
    expected_review_revision: int = Field(ge=0)

ReaderReviewDecisionReceiptId = Annotated[
    str, Field(pattern=r"^reader-review-decision-[0-9a-f]{32}$")
]


class ReaderFindingDecision(ChangeSetModel):
    finding_id: ReaderFindingId
    decision: Literal["adopted", "rejected", "deferred"]
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class ReaderReviewDecisionRequest(ChangeSetModel):
    """Author decisions over a cold-read Reader review receipt."""

    expected_reader_review_receipt_hash: Sha256
    decisions: list[ReaderFindingDecision] = Field(min_length=1, max_length=64)
    finalize: bool = True


class ReaderReviewDecisionReceipt(ChangeSetModel):
    """Author resolution of Reader findings; may spawn a follow-up ChangeSet revision."""

    schema_id: Literal["zhimeng.reader-review-decision-receipt.v1"] = Field(
        default="zhimeng.reader-review-decision-receipt.v1",
        alias="schema",
    )
    receipt_id: ReaderReviewDecisionReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    reader_review_receipt_id: ReaderReviewReceiptId
    reader_review_receipt_hash: Sha256
    result_revision: int = Field(ge=1)
    result_fingerprint: Sha256
    decisions: list[ReaderFindingDecision] = Field(min_length=1, max_length=64)
    adopted_finding_ids: list[str] = Field(default_factory=list, max_length=64)
    rejected_finding_ids: list[str] = Field(default_factory=list, max_length=64)
    deferred_finding_ids: list[str] = Field(default_factory=list, max_length=64)
    finalized: bool = True
    follow_up_change_set_id: ChangeSetId | None = None
    parent_reader_valid_for_new_candidate: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class ReaderReviewDecisionResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.reader-review-decision-response.v1"] = Field(
        default="zhimeng.reader-review-decision-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: ReaderReviewDecisionReceipt | None = None
    history: list[ReaderReviewDecisionReceipt] = Field(default_factory=list, max_length=32)
    follow_up_change_set: ChangeSetResponse | None = None

FeedbackCandidateId = Annotated[
    str, Field(pattern=r"^feedback-candidate-[0-9a-f]{32}$")
]
FeedbackCandidateBundleId = Annotated[
    str, Field(pattern=r"^feedback-candidate-bundle-[0-9a-f]{32}$")
]


class FeedbackCandidate(ChangeSetModel):
    """Versioned author-feedback candidate. Not formal knowledge."""

    candidate_id: FeedbackCandidateId
    source_kind: Literal["reader_review_decision"] = "reader_review_decision"
    source_decision: Literal["adopted", "rejected", "deferred"]
    finding_id: str = Field(min_length=1, max_length=128)
    finding_kind: Literal["fact_risk", "continuity_risk", "subjective_note"]
    summary: str = Field(min_length=1, max_length=500)
    evidence_quote: str = Field(default="", max_length=1000)
    relative_path: RelativeProjectPath | None = None
    status: Literal["candidate", "rejected", "experience"] = "candidate"
    promotion_status: Literal[
        "not_promoted",
        "source_verified",
        "conflict_checked",
        "regression_evaluated",
        "author_approved",
        "experience_recorded",
        "rejected",
        "rolled_back",
    ] = "not_promoted"
    confidence: Literal["author_explicit"] = "author_explicit"
    regression_result: Literal[
        "not_run",
        "pass_with_watch",
        "fail_blocked",
        "skipped_rejected",
    ] = "not_run"
    experience_relative_path: RelativeProjectPath | None = None
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False


class FeedbackCandidateBundle(ChangeSetModel):
    """Bundle of feedback candidates produced from one Reader decision receipt."""

    schema_id: Literal["zhimeng.feedback-candidate-bundle.v1"] = Field(
        default="zhimeng.feedback-candidate-bundle.v1",
        alias="schema",
    )
    bundle_id: FeedbackCandidateBundleId
    change_set_id: ChangeSetId
    project_id: ProjectId
    reader_review_decision_receipt_id: ReaderReviewDecisionReceiptId
    reader_review_decision_receipt_hash: Sha256
    follow_up_change_set_id: ChangeSetId | None = None
    candidates: list[FeedbackCandidate] = Field(min_length=1, max_length=64)
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class FeedbackCandidateResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.feedback-candidate-response.v1"] = Field(
        default="zhimeng.feedback-candidate-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    bundle: FeedbackCandidateBundle | None = None
    history: list[FeedbackCandidateBundle] = Field(default_factory=list, max_length=32)


FeedbackCandidatePromotionReceiptId = Annotated[
    str, Field(pattern=r"^feedback-candidate-promotion-[0-9a-f]{32}$")
]


class FeedbackCandidatePromotionDecision(ChangeSetModel):
    candidate_id: FeedbackCandidateId
    action: Literal[
        "verify_source",
        "check_conflicts",
        "evaluate_regression",
        "approve",
        "record_experience",
        "reject",
        "rollback_experience",
    ]


class FeedbackCandidatePromotionRequest(ChangeSetModel):
    """Author-controlled FR-EVO promotion steps over feedback candidates."""

    expected_feedback_candidate_bundle_hash: Sha256
    decisions: list[FeedbackCandidatePromotionDecision] = Field(
        min_length=1,
        max_length=64,
    )


class FeedbackCandidatePromotionReceipt(ChangeSetModel):
    """Receipt for one promotion batch. Does not contaminate formal knowledge."""

    schema_id: Literal["zhimeng.feedback-candidate-promotion-receipt.v1"] = Field(
        default="zhimeng.feedback-candidate-promotion-receipt.v1",
        alias="schema",
    )
    receipt_id: FeedbackCandidatePromotionReceiptId
    change_set_id: ChangeSetId
    project_id: ProjectId
    feedback_candidate_bundle_id: FeedbackCandidateBundleId
    feedback_candidate_bundle_hash: Sha256
    decisions: list[FeedbackCandidatePromotionDecision] = Field(
        min_length=1,
        max_length=64,
    )
    resulting_candidates: list[FeedbackCandidate] = Field(min_length=1, max_length=64)
    experience_relative_path: RelativeProjectPath | None = None
    experience_rows_appended: int = Field(ge=0)
    formal_knowledge_contaminated: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    quality_assessed: Literal[False] = False
    novel_state_synced: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256


class FeedbackCandidatePromotionResponse(ChangeSetModel):
    schema_id: Literal["zhimeng.feedback-candidate-promotion-response.v1"] = Field(
        default="zhimeng.feedback-candidate-promotion-response.v1",
        alias="schema",
    )
    change_set_id: ChangeSetId
    receipt: FeedbackCandidatePromotionReceipt | None = None
    history: list[FeedbackCandidatePromotionReceipt] = Field(
        default_factory=list,
        max_length=32,
    )
    bundle: FeedbackCandidateBundle | None = None

