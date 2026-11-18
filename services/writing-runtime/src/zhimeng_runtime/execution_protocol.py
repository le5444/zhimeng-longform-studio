from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from zhimeng_runtime.errors import ErrorEnvelope


StableId: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
NonEmptyString: TypeAlias = Annotated[str, StringConstraints(min_length=1)]
ExtensionName: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^x-[a-z0-9][a-z0-9._/-]*$",
    ),
]
Sha256Fingerprint: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)


class ExecutorKind(StrEnum):
    EMBEDDED = "embedded"
    PROCESS = "process"
    REMOTE = "remote"


class CapabilityName(StrEnum):
    STREAMING = "streaming"
    CANCEL = "cancel"
    RESUME = "resume"
    CHECKPOINTS = "checkpoints"
    TOOL_CALLS = "tool_calls"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL = "shell"
    MCP = "mcp"
    SKILLS = "skills"
    SUBAGENTS = "subagents"
    COMPACTION = "compaction"
    APPROVALS = "approvals"
    ARTIFACTS = "artifacts"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSING = "closing"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"


class RunStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActivityKind(StrEnum):
    MESSAGE = "message"
    RUN_STATE = "run_state"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    COMMAND = "command"
    CHANGE_PROPOSAL = "change_proposal"
    APPROVAL_REQUEST = "approval_request"
    SKILL_CALL = "skill_call"
    SUBAGENT = "subagent"
    COMPACTION = "compaction"
    ARTIFACT = "artifact"
    ERROR = "error"


class ExecutorIdentity(StrictModel):
    executor_id: StableId
    kind: ExecutorKind
    implementation: NonEmptyString
    version: NonEmptyString
    protocol_version: NonEmptyString
    instance_id: StableId | None


class CapabilityExtension(StrictModel):
    version: NonEmptyString | None
    config: JsonValue


class CapabilityProfile(StrictModel):
    protocol_version: NonEmptyString
    names: list[CapabilityName]
    extensions: dict[ExtensionName, CapabilityExtension]

    @model_validator(mode="after")
    def ensure_unique_capabilities(self) -> CapabilityProfile:
        if len(self.names) != len(set(self.names)):
            raise ValueError("capability names must be unique")
        return self


class ProjectRevision(StrictModel):
    project_id: StableId
    revision: int = Field(ge=1, strict=True)
    fingerprint: Sha256Fingerprint
    observed_at: datetime


class ExecutionSession(StrictModel):
    schema_id: Literal["zhimeng.execution-session.v1"] = Field(
        alias="schema",
    )
    session_id: StableId
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    executor: ExecutorIdentity
    capabilities: CapabilityProfile
    # 「随便聊」会话不绑定小说项目，project_revision 为空；绑定项目的
    # 会话照旧携带快照身份。
    project_revision: ProjectRevision | None
    correlation_id: StableId

    @model_validator(mode="after")
    def validate_timestamps(self) -> ExecutionSession:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


_TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
}


class Run(StrictModel):
    schema_id: Literal["zhimeng.execution-run.v1"] = Field(
        alias="schema",
    )
    run_id: StableId
    session_id: StableId
    ordinal: int = Field(ge=1)
    status: RunStatus
    objective: NonEmptyString
    origin_kind: Literal["user", "command_completion"] = "user"
    stop_generation: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    executor: ExecutorIdentity
    capabilities: CapabilityProfile
    # 无项目 Run（随便聊）为空；项目 Run 携带快照身份。
    project_revision: ProjectRevision | None
    correlation_id: StableId
    causation_id: StableId | None
    last_activity_sequence: int = Field(ge=0)
    checkpoint_id: StableId | None
    error: ErrorEnvelope | None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Run:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.completed_at is not None and self.started_at is None:
            raise ValueError("completed_at requires started_at")
        if (
            self.completed_at is not None
            and self.started_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must not precede started_at")
        if self.status is RunStatus.QUEUED:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("queued runs cannot have lifecycle timestamps")
        elif self.started_at is None:
            raise ValueError("non-queued runs require started_at")
        if self.status in _TERMINAL_RUN_STATUSES:
            if self.completed_at is None:
                raise ValueError("terminal runs require completed_at")
        elif self.completed_at is not None:
            raise ValueError("non-terminal runs cannot have completed_at")
        if self.status is RunStatus.FAILED and self.error is None:
            raise ValueError("failed runs require an error envelope")
        if self.status is not RunStatus.FAILED and self.error is not None:
            raise ValueError("only failed runs can carry an error envelope")
        return self


class MessageUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    # DeepSeek/OpenAI 兼容线路的前缀缓存命中（input 的子集）。旧数据无此字段。
    cached_input_tokens: int | None = Field(default=None, ge=0)
    # 聚合用量只相加已报告数字；False 表示至少一次调用没有报告该项。
    # 旧记录没有完整性元数据，保留其现有解释。
    input_tokens_reported: bool = True
    output_tokens_reported: bool = True
    cached_input_tokens_complete: bool = True

    @model_validator(mode="after")
    def validate_total(self) -> "MessageUsage":
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must cover input and output tokens")
        return self


class MessagePayload(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: NonEmptyString | None
    name: NonEmptyString | None
    message_id: StableId | None = None
    phase: Literal["delta", "completed"] = "completed"
    delta: NonEmptyString | None = None
    format: Literal["plain", "markdown"] = "plain"
    finish_reason: str | None = None
    usage: MessageUsage | None = None

    @model_validator(mode="after")
    def validate_phase(self) -> "MessagePayload":
        if self.phase == "delta":
            if self.role != "assistant":
                raise ValueError("message deltas are only valid for assistant messages")
            if self.message_id is None or self.delta is None:
                raise ValueError("message deltas require message_id and delta")
            if self.content is not None or self.finish_reason is not None or self.usage is not None:
                raise ValueError("message deltas cannot carry completed message fields")
        else:
            if self.content is None or self.delta is not None:
                raise ValueError("completed messages require content and cannot carry delta")
        return self


class RunStatePayload(StrictModel):
    status: RunStatus
    previous_status: RunStatus | None
    detail: str | None


class ReasoningPayload(StrictModel):
    summary: NonEmptyString
    content: str | None
    visibility: Literal["summary", "full", "redacted"]
    block_id: StableId | None = None
    phase: Literal["delta", "completed", "interrupted"] = "completed"
    delta: str | None = None
    source: Literal["model", "runtime"] = "model"

    @model_validator(mode="after")
    def validate_stream_phase(self) -> ReasoningPayload:
        if self.phase == "delta":
            if self.block_id is None or self.delta is None or self.content is not None:
                raise ValueError("reasoning deltas require block_id, delta and no content")
        elif self.delta is not None:
            raise ValueError("finished reasoning uses a content snapshot, not a delta")
        return self


class ToolCallPayload(StrictModel):
    tool_call_id: StableId
    tool_name: NonEmptyString
    arguments: JsonValue


class ToolResultPayload(StrictModel):
    tool_call_id: StableId
    status: Literal["succeeded", "failed", "cancelled"]
    output: JsonValue
    error: ErrorEnvelope | None
    duration_ms: int | None = Field(ge=0)

    @model_validator(mode="after")
    def validate_error(self) -> ToolResultPayload:
        if self.status == "failed" and self.error is None:
            raise ValueError("failed tool results require an error envelope")
        if self.status != "failed" and self.error is not None:
            raise ValueError("only failed tool results can carry an error envelope")
        return self


class CommandPayload(StrictModel):
    command_id: StableId
    command: NonEmptyString
    cwd: str | None
    status: OperationStatus
    exit_code: int | None
    output_artifact_id: StableId | None


class FileChange(StrictModel):
    file_id: StableId | None
    path: NonEmptyString
    operation: Literal["create", "update", "delete"]
    content_hash_before: NonEmptyString | None
    content_hash_after: NonEmptyString | None

    @model_validator(mode="after")
    def validate_hash_transition(self) -> FileChange:
        if self.operation == "create":
            if self.content_hash_before is not None or self.content_hash_after is None:
                raise ValueError("create requires only content_hash_after")
        elif self.operation == "update":
            if (
                self.file_id is None
                or self.content_hash_before is None
                or self.content_hash_after is None
            ):
                raise ValueError("update requires file_id and both content hashes")
            if self.content_hash_before == self.content_hash_after:
                raise ValueError("update content hashes must differ")
        elif (
            self.file_id is None
            or self.content_hash_before is None
            or self.content_hash_after is not None
        ):
            raise ValueError("delete requires file_id and only content_hash_before")
        return self


class ChangeProposalPayload(StrictModel):
    proposal_id: StableId
    title: NonEmptyString
    summary: NonEmptyString
    base_revision: int = Field(ge=1, strict=True)
    base_fingerprint: Sha256Fingerprint
    files: list[FileChange] = Field(min_length=1)


class ApprovalOption(StrictModel):
    option_id: StableId
    label: NonEmptyString
    effect: Literal["approve", "reject", "cancel", "custom"]


class ApprovalRequestPayload(StrictModel):
    approval_id: StableId
    subject_kind: Literal[
        "tool_call",
        "command",
        "change_proposal",
        "skill_call",
        "subagent",
    ]
    subject_id: StableId
    prompt: NonEmptyString
    options: list[ApprovalOption] = Field(min_length=2)
    status: Literal["pending", "resolved", "expired", "cancelled"]
    selected_option_id: StableId | None

    @model_validator(mode="after")
    def validate_selection(self) -> ApprovalRequestPayload:
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("approval option ids must be unique")
        if self.status == "resolved":
            if self.selected_option_id not in option_ids:
                raise ValueError("resolved approvals require a selected option")
        elif self.selected_option_id is not None:
            raise ValueError("unresolved approvals cannot select an option")
        return self


class SkillCallPayload(StrictModel):
    skill_call_id: StableId
    skill_id: NonEmptyString
    skill_version: NonEmptyString | None
    status: OperationStatus
    input: JsonValue
    output: JsonValue


class SubagentPayload(StrictModel):
    subagent_id: StableId
    name: str | None
    task: NonEmptyString
    status: Literal[
        "requested",
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    ]
    child_run_id: StableId | None


class CompactionPayload(StrictModel):
    from_sequence: int = Field(ge=1)
    to_sequence: int = Field(ge=1)
    strategy: NonEmptyString
    summary: NonEmptyString
    tokens_before: int | None = Field(ge=0)
    tokens_after: int | None = Field(ge=0)
    artifact_id: StableId | None

    @model_validator(mode="after")
    def validate_range(self) -> CompactionPayload:
        if self.to_sequence < self.from_sequence:
            raise ValueError("compaction range must be ordered")
        return self


class ArtifactPayload(StrictModel):
    artifact_id: StableId
    artifact_type: NonEmptyString
    name: NonEmptyString
    uri: NonEmptyString
    media_type: str | None
    content_hash: str | None
    size_bytes: int | None = Field(ge=0)


class ErrorPayload(StrictModel):
    error: ErrorEnvelope
    fatal: bool
    scope: Literal["activity", "run", "session", "runtime"]


class ActivityItemBase(StrictModel):
    schema_id: Literal["zhimeng.activity-item.v1"] = Field(
        alias="schema",
    )
    activity_id: StableId
    session_id: StableId
    run_id: StableId
    sequence: int = Field(ge=1)
    created_at: datetime
    # 无项目 Run（随便聊）的活动没有项目身份。
    project_revision: ProjectRevision | None
    correlation_id: StableId
    causation_id: StableId | None


class MessageActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.MESSAGE]
    payload: MessagePayload


class RunStateActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.RUN_STATE]
    payload: RunStatePayload


class ReasoningActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.REASONING]
    payload: ReasoningPayload


class ToolCallActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.TOOL_CALL]
    payload: ToolCallPayload


class ToolResultActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.TOOL_RESULT]
    payload: ToolResultPayload


class CommandActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.COMMAND]
    payload: CommandPayload


class ChangeProposalActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.CHANGE_PROPOSAL]
    payload: ChangeProposalPayload

    @model_validator(mode="after")
    def validate_base_revision(self) -> ChangeProposalActivityItem:
        if self.payload.base_revision != self.project_revision.revision:
            raise ValueError("proposal base_revision must match project_revision")
        if self.payload.base_fingerprint != self.project_revision.fingerprint:
            raise ValueError("proposal base_fingerprint must match project_revision")
        return self


class ApprovalRequestActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.APPROVAL_REQUEST]
    payload: ApprovalRequestPayload


class SkillCallActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.SKILL_CALL]
    payload: SkillCallPayload


class SubagentActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.SUBAGENT]
    payload: SubagentPayload


class CompactionActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.COMPACTION]
    payload: CompactionPayload

    @model_validator(mode="after")
    def validate_compacted_history(self) -> CompactionActivityItem:
        if self.payload.to_sequence >= self.sequence:
            raise ValueError("compaction cannot cover its own or a future activity")
        return self


class ArtifactActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.ARTIFACT]
    payload: ArtifactPayload


class ErrorActivityItem(ActivityItemBase):
    kind: Literal[ActivityKind.ERROR]
    payload: ErrorPayload


ActivityItemVariant: TypeAlias = Annotated[
    MessageActivityItem
    | RunStateActivityItem
    | ReasoningActivityItem
    | ToolCallActivityItem
    | ToolResultActivityItem
    | CommandActivityItem
    | ChangeProposalActivityItem
    | ApprovalRequestActivityItem
    | SkillCallActivityItem
    | SubagentActivityItem
    | CompactionActivityItem
    | ArtifactActivityItem
    | ErrorActivityItem,
    Field(discriminator="kind"),
]


class ActivityItem(RootModel[ActivityItemVariant]):
    """Validated discriminated activity union with an object-shaped JSON root."""

    model_config = ConfigDict(frozen=True)


def validate_activity_sequence(
    items: Iterable[ActivityItem | ActivityItemVariant],
    *,
    expected_start: int = 1,
    prior_activity_ids: Iterable[str] = (),
    run: Run | None = None,
) -> None:
    """Validate an ordered activity slice and, optionally, its owning Run cursor."""

    if expected_start < 1:
        raise ValueError("expected_start must be at least 1")

    session_id: str | None = None
    run_id: str | None = None
    correlation_id: str | None = None
    project_revision: ProjectRevision | None = None
    activity_ids = set(prior_activity_ids)
    expected_sequence = expected_start

    for wrapped_item in items:
        item = wrapped_item.root if isinstance(wrapped_item, ActivityItem) else wrapped_item
        if session_id is None:
            session_id = item.session_id
            run_id = item.run_id
            correlation_id = item.correlation_id
            project_revision = item.project_revision
        elif item.session_id != session_id or item.run_id != run_id:
            raise ValueError("all activity items must belong to one session and run")
        if item.correlation_id != correlation_id:
            raise ValueError("all activity items must share one correlation id")
        if item.project_revision != project_revision:
            raise ValueError("all activity items must share one project revision")
        if item.activity_id in activity_ids:
            raise ValueError(f"duplicate activity id: {item.activity_id}")
        if item.sequence != expected_sequence:
            raise ValueError(
                f"expected activity sequence {expected_sequence}, got {item.sequence}"
            )
        if item.causation_id is not None and item.causation_id not in activity_ids:
            raise ValueError(
                "activity causation must reference an earlier known activity"
            )
        if (
            isinstance(item, CompactionActivityItem)
            and item.payload.to_sequence >= item.sequence
        ):
            raise ValueError("compaction cannot cover its own or a future activity")
        activity_ids.add(item.activity_id)
        expected_sequence += 1

    if run is not None:
        if session_id is not None:
            if run.session_id != session_id or run.run_id != run_id:
                raise ValueError("activity stream does not belong to the supplied run")
            if run.correlation_id != correlation_id:
                raise ValueError("activity correlation does not match the supplied run")
            if run.project_revision != project_revision:
                raise ValueError("activity revision does not match the supplied run")
        expected_last_sequence = expected_sequence - 1
        if run.last_activity_sequence != expected_last_sequence:
            raise ValueError(
                "run last_activity_sequence does not match the validated stream"
            )
