from __future__ import annotations

from zhimeng_runtime.agent_images import AgentImage
from zhimeng_runtime.task_review import ReviewSource
from zhimeng_runtime.run_usage import RunUsage
from zhimeng_runtime.session_goal import GoalAdmission

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.execution_protocol import (
    ActivityItem,
    CapabilityProfile,
    ExecutorIdentity,
    ProjectRevision,
    Run,
    RunStatus,
    SessionStatus,
    StableId,
)
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
)


class AgentApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AgentStatusResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-status.v1"] = Field(
        default="zhimeng.agent-status.v1",
        alias="schema",
    )
    available: bool
    executor: ExecutorIdentity | None
    capabilities: CapabilityProfile | None
    detail: str


class AgentSessionCreateRequest(AgentApiModel):
    title: str = Field(default="Writing Agent", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class AgentSessionForkRequest(AgentApiModel):
    up_to_message_id: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=120)

    @field_validator("up_to_message_id")
    @classmethod
    def normalize_message_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("up_to_message_id must not be blank")
        return normalized

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AgentSessionUpdateRequest(AgentApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None
    rebind_provider: bool = False
    provider_id: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: Literal[
        "auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"
    ] | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> AgentSessionUpdateRequest:
        if (
            self.title is None
            and self.pinned is None
            and not self.rebind_provider
            and self.provider_id is None
            and self.model is None
            and self.reasoning_effort is None
        ):
            raise ValueError("at least one session field must be provided")
        return self


class AgentSessionResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-session.v1"] = Field(
        default="zhimeng.agent-session.v1",
        alias="schema",
    )
    session_id: StableId
    # 这条对话挂在哪本小说下；None = 不绑书的纯对话。
    # project_revision 只有在那本书已经有快照时才有，不能拿它当归属判断。
    project_id: StableId | None = None
    # 「随便聊」会话不绑定项目，project_revision 为空。
    project_revision: ProjectRevision | None
    title: str
    pinned: bool = False
    status: SessionStatus
    revision: int = Field(ge=1)
    executor: ExecutorIdentity
    capabilities: CapabilityProfile
    correlation_id: StableId
    last_run_ordinal: int = Field(ge=0)
    last_message_ordinal: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentSessionRebindRequest(AgentApiModel):
    """把一条对话改挂到哪部小说下；None 表示解绑成不针对任何书的纯对话。"""

    project_id: StableId | None = None


class AgentSessionListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-session-list.v1"] = Field(
        default="zhimeng.agent-session-list.v1",
        alias="schema",
    )
    sessions: list[AgentSessionResponse]


class SessionCheckpointRecord(AgentApiModel):
    """一份已落库的会话摘要。

    落库后逐字节不再变化：注入时引用的就是这一份，所以同一次注入的重新校验
    结果稳定。``through_ordinal`` 是这份摘要覆盖到的最后一条消息序号，之后
    的消息仍原样留在历史窗口里。
    """

    schema_id: Literal["zhimeng.session-checkpoint.v1"] = Field(
        default="zhimeng.session-checkpoint.v1",
        alias="schema",
    )
    checkpoint_id: StableId
    session_id: StableId
    run_id: StableId | None
    through_ordinal: int = Field(ge=2)
    source_message_count: int = Field(ge=2)
    content: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reported_model: str | None
    created_at: datetime


class AgentMessageResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-message.v1"] = Field(
        default="zhimeng.agent-message.v1",
        alias="schema",
    )
    message_id: StableId
    session_id: StableId
    run_id: StableId
    ordinal: int = Field(ge=1)
    role: Literal["system", "user", "assistant", "tool"]
    format: Literal["plain", "markdown"]
    content: str = Field(min_length=1)
    created_at: datetime
    # 这一轮的用量（含缓存命中 cached_input_tokens）；没有就不带。
    usage: dict[str, int | bool | None] | None = None
    run_usage: RunUsage | None = None
    images: list[AgentImage] = Field(default_factory=list)


class AgentRunProcessSummary(AgentApiModel):
    """Small persisted facts for deciding whether a conversation has details."""

    has_process: bool
    tool_count: int = Field(ge=0)


class AgentMessageListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-message-list.v1"] = Field(
        default="zhimeng.agent-message-list.v1",
        alias="schema",
    )
    session_id: StableId
    messages: list[AgentMessageResponse]
    last_ordinal: int = Field(ge=0)
    # 这一页前面还有更早的消息（界面据此显示「显示更早的消息」）。
    has_earlier: bool = False
    # Only runs represented in this page. Missing on old clients/fixtures means
    # unknown, while has_process=False is a verified empty process.
    run_process_summaries: dict[str, AgentRunProcessSummary] = Field(default_factory=dict)
    run_origins: dict[str, Literal["user", "command_completion"]] = Field(default_factory=dict)
    review_sources: dict[str, ReviewSource] = Field(default_factory=dict)


class AgentRunCreateRequest(AgentApiModel):
    goal: GoalAdmission | None = None
    message: str = Field(default="", max_length=100_000)
    image_ids: list[str] = Field(default_factory=list)
    continuation_source_run_id: StableId | None = None
    review_source_run_id: StableId | None = None
    # 无项目 Run（随便聊）两项都留空；绑定项目的 Run 必须成对提供。
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    # 用户这时在右栏开着的文件（打开的文件夹里的相对路径）：后端照实告诉模型开着哪个文件，内容模型要用自己读。
    open_file_path: str | None = Field(default=None, min_length=1, max_length=1024)
    manual_file_ids: list[StableId] = Field(default_factory=list, max_length=64)
    # 作者在输入框用 / 点名的技能（引用标签）：只在这条 Run 生效，不进历史。
    manual_skill_names: list[str] = Field(default_factory=list, max_length=16)
    writer_mode: Literal[
        "repair_only",
        "author_review_change_proposal",
    ] = "repair_only"
    context_pack_id: StableId | None = None
    expected_context_pack_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        return value

    @field_validator("open_file_path")
    @classmethod
    def normalize_open_file_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().replace("\\", "/")
        if not value or any(char in value for char in "\r\n\t\x00"):
            raise ValueError("open_file_path must be one relative path")
        return value

    @model_validator(mode="after")
    def validate_context_pack_reference(self) -> "AgentRunCreateRequest":
        if self.goal and (self.context_pack_id or self.review_source_run_id):
            raise ValueError("持续目标使用普通 Agent 执行，不与专业工作流或独立复核混合。")
        if not self.message:
            if not self.image_ids:
                raise ValueError("message must not be blank without images")
            self.message = "[图片]"
        self.image_ids = list(dict.fromkeys(self.image_ids))
        if (self.context_pack_id is None) != (self.expected_context_pack_hash is None):
            raise ValueError(
                "context_pack_id and expected_context_pack_hash must be provided together"
            )
        if (self.expected_project_revision is None) != (
            self.expected_project_fingerprint is None
        ):
            raise ValueError(
                "expected_project_revision and expected_project_fingerprint "
                "must be provided together"
            )
        return self


class AgentRunContinuationResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-continuation.v1"] = Field(
        default="zhimeng.agent-run-continuation.v1", alias="schema",
    )
    run_id: StableId
    can_continue: bool
    reason_code: str
    reason: str
    stop_reason: str | None
    objective: str
    continued_by_run_id: StableId | None
    completed_tool_count: int = Field(ge=0)
    uncertain_tool_count: int = Field(ge=0)


class AgentRunCreateResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-create.v1"] = Field(
        default="zhimeng.agent-run-create.v1",
        alias="schema",
    )
    run: Run
    user_message: AgentMessageResponse
    replayed: bool = False


class AgentRunAdmissionWithdrawResponse(AgentApiModel):
    state: Literal["accepted", "withdrawn"]
    accepted: AgentRunCreateResponse | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> "AgentRunAdmissionWithdrawResponse":
        if (self.state == "accepted") != (self.accepted is not None):
            raise ValueError("accepted state requires the original admission receipt")
        return self


class AgentRunResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-state.v1"] = Field(
        default="zhimeng.agent-run-state.v1",
        alias="schema",
    )
    run: Run


class AgentRunListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-list.v1"] = Field(
        default="zhimeng.agent-run-list.v1",
        alias="schema",
    )
    session_id: StableId
    runs: list[Run]


class AgentActivityListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-activity-list.v1"] = Field(
        default="zhimeng.agent-activity-list.v1",
        alias="schema",
    )
    run_id: StableId
    activities: list[ActivityItem]
    last_sequence: int = Field(ge=0)
    next_after_sequence: int | None = Field(default=None, ge=0)
    # Activity sequence numbers and SSE run_event cursors are different.
    last_event_sequence: int | None = Field(default=None, ge=0)


class AgentRunCancelResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-cancel.v1"] = Field(
        default="zhimeng.agent-run-cancel.v1",
        alias="schema",
    )
    run: Run
    accepted: bool


WorkflowResumeState = Literal[
    "running",
    "resumable",
    "blocked",
    "provider_dispatch_uncertain",
    "terminal",
]


class AgentWorkflowStateResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-workflow-state.v1"] = Field(
        default="zhimeng.agent-workflow-state.v1",
        alias="schema",
    )
    run_id: StableId
    run_status: RunStatus
    plan: WorkflowPlan
    checkpoints: tuple[WorkflowStepCheckpoint, ...]
    provider_boundary: ProviderIoBoundary | None
    last_checkpoint_id: str | None = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    last_checkpoint_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    next_step_ordinal: int | None = Field(default=None, ge=1, le=32)
    resume_state: WorkflowResumeState
    resume_allowed: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=128)
    formal_draft_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_workflow_cursor(self) -> "AgentWorkflowStateResponse":
        if (self.last_checkpoint_id is None) != (self.last_checkpoint_hash is None):
            raise ValueError("workflow checkpoint identity and hash must be paired")
        if self.resume_allowed != (self.resume_state == "resumable"):
            raise ValueError("resume_allowed must match the derived workflow state")
        if self.provider_boundary is not None and self.resume_allowed:
            raise ValueError("a dispatched Provider boundary cannot be resumed")
        return self


class AgentRunResumeRequest(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-resume-request.v1"] = Field(
        default="zhimeng.agent-run-resume-request.v1",
        alias="schema",
    )
    expected_workflow_plan_id: str = Field(
        pattern=r"^workflow-plan-[0-9a-f]{32}$"
    )
    expected_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_checkpoint_id: str | None = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    expected_checkpoint_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_expected_checkpoint(self) -> "AgentRunResumeRequest":
        if (self.expected_checkpoint_id is None) != (
            self.expected_checkpoint_hash is None
        ):
            raise ValueError("expected checkpoint identity and hash must be paired")
        return self


class AgentRunResumeResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-resume.v1"] = Field(
        default="zhimeng.agent-run-resume.v1",
        alias="schema",
    )
    run: Run
    accepted: Literal[True] = True
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    resumed_from_checkpoint_id: str | None = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    resumed_from_checkpoint_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    next_action: Literal["workflow_step", "provider_io"]

    @model_validator(mode="after")
    def validate_resume_cursor(self) -> "AgentRunResumeResponse":
        if (self.resumed_from_checkpoint_id is None) != (
            self.resumed_from_checkpoint_hash is None
        ):
            raise ValueError("resumed checkpoint identity and hash must be paired")
        return self


class AgentRunEvent(AgentApiModel):
    schema_id: Literal["zhimeng.run-event.v1"] = Field(
        default="zhimeng.run-event.v1",
        alias="schema",
    )
    event_id: str
    event_type: Literal["activity", "done"]
    emitted_at: datetime
    data: dict[str, object]
