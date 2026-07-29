from __future__ import annotations

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


class AgentSessionResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-session.v1"] = Field(
        default="zhimeng.agent-session.v1",
        alias="schema",
    )
    session_id: StableId
    project_revision: ProjectRevision
    title: str
    status: SessionStatus
    revision: int = Field(ge=1)
    executor: ExecutorIdentity
    capabilities: CapabilityProfile
    correlation_id: StableId
    last_run_ordinal: int = Field(ge=0)
    last_message_ordinal: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentSessionListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-session-list.v1"] = Field(
        default="zhimeng.agent-session-list.v1",
        alias="schema",
    )
    sessions: list[AgentSessionResponse]


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


class AgentMessageListResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-message-list.v1"] = Field(
        default="zhimeng.agent-message-list.v1",
        alias="schema",
    )
    session_id: StableId
    messages: list[AgentMessageResponse]
    last_ordinal: int = Field(ge=0)


class AgentRunCreateRequest(AgentApiModel):
    message: str = Field(min_length=1, max_length=100_000)
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
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
        if not value:
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def validate_context_pack_reference(self) -> "AgentRunCreateRequest":
        if (self.context_pack_id is None) != (self.expected_context_pack_hash is None):
            raise ValueError(
                "context_pack_id and expected_context_pack_hash must be provided together"
            )
        if self.writer_mode == "author_review_change_proposal" and (
            self.context_pack_id is None or self.active_chapter_id is None
        ):
            raise ValueError(
                "author-review Writer mode requires a Context Pack and active chapter"
            )
        return self


class AgentRunCreateResponse(AgentApiModel):
    schema_id: Literal["zhimeng.agent-run-create.v1"] = Field(
        default="zhimeng.agent-run-create.v1",
        alias="schema",
    )
    run: Run
    user_message: AgentMessageResponse
    replayed: bool = False


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
