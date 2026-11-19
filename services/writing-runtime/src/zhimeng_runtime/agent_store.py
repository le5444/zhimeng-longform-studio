from __future__ import annotations

from zhimeng_runtime.session_goal import GoalAdmission, GoalStore

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zhimeng_runtime.agent_images import claim_images, fork_images, provider_image_message

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateOutput,
    AgentCandidateOutputRecord,
    AgentCandidateOutputV2,
    AgentCandidateReference,
    AgentCandidateSubmission,
    agent_candidate_output_idempotency_hash,
    build_agent_candidate_output as build_candidate_output_envelope,
    parse_agent_candidate_output_json,
)
from zhimeng_runtime.agent_models import (
    AgentActivityListResponse,
    AgentMessageResponse,
    AgentRunProcessSummary,
    AgentRunContinuationResponse,
    AgentSessionResponse,
    SessionCheckpointRecord,
)
from zhimeng_runtime.agent_tools import (
    CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
    CREATION_HANDOFF_SKILL_ID,
)
from zhimeng_runtime.change_set_models import (
    ChangeSetResponse,
    change_set_payload_hash,
    change_set_reference_hash_from_payload,
)
from zhimeng_runtime.context_models import (
    CompletedHistoryMessage,
    ContextPack,
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
    conservative_token_estimate,
)
from zhimeng_runtime.context_store import ContextStore, ContextStoreError
from zhimeng_runtime.creation_instruction_models import (
    CREATION_INSTRUCTION_MAP_PATH,
    CREATION_INSTRUCTION_SKILL_ID,
    CreationInstructionActivationBinding,
    CreationInstructionActivationScope,
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
    build_creation_instruction_activation_binding,
    build_creation_instruction_dispatch_receipt,
    build_creation_instruction_materialization,
    creation_instruction_activation_binding_hash,
    creation_instruction_dispatch_receipt_hash,
    creation_instruction_materialization_hash,
    parse_creation_instruction_map,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.errors import ErrorDetail, ErrorEnvelope
from zhimeng_runtime.execution_protocol import (
    ActivityItem,
    ActivityKind,
    ArtifactPayload,
    CapabilityProfile,
    ChangeProposalPayload,
    ErrorPayload,
    ExecutorIdentity,
    FileChange,
    MessagePayload,
    MessageUsage,
    ProjectRevision,
    ReasoningPayload,
    Run,
    RunStatePayload,
    RunStatus,
    SessionStatus,
    SkillCallPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from zhimeng_runtime.managed_skill_service import (
    MANIFEST_NAME,
    ManagedSkillError,
    ManagedSkillService,
)
from zhimeng_runtime.nlcl_models import NlclRouteReceipt
from zhimeng_runtime.prewrite_models import PrewriteOperationPackage
from zhimeng_runtime.provider_recovery_models import (
    ProviderRecoveryDecision,
    ProviderRecoveryDecisionRequest,
    ProviderRecoveryDossier,
    ProviderRecoveryResponse,
    provider_recovery_decision_hash,
    provider_recovery_decision_id,
    provider_recovery_dossier_hash,
    provider_recovery_dossier_id,
)
from zhimeng_runtime.provider_turn_models import (
    ProviderTurnReceipt,
    ProviderTurnReceiptListResponse,
    build_provider_turn_receipt_list_response,
)
from zhimeng_runtime.session_checkpoint import (
    checkpoint_content_hash,
    render_checkpoint_block,
)
from zhimeng_runtime.skill_models import (
    CreationHandoffResult,
    ManagedSkillActivationReceipt,
    SkillExecutionReceipt,
    SkillManifest,
)
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStep,
    WorkflowStepCheckpoint,
    build_workflow_step_checkpoint,
    validate_workflow_checkpoint_chain,
)
from zhimeng_runtime.writer_models import (
    WriterCandidateResult,
    WriterTargetFile,
    WriterTask,
    build_writer_candidate_result,
    build_writer_task,
    writer_candidate_result_hash,
    writer_task_hash,
)


TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
}

WORKFLOW_PLAN_CHECKPOINT_TYPE = "workflow_plan"
WORKFLOW_STEP_CHECKPOINT_TYPE = "workflow_step"
PROVIDER_IO_BOUNDARY_CHECKPOINT_TYPE = "provider_io_boundary"

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {
        RunStatus.STARTING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.STARTING: {
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.RUNNING: {
        RunStatus.AWAITING_APPROVAL,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.SUCCEEDED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.AWAITING_APPROVAL: {
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.PAUSED: {
        RunStatus.RUNNING,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.CANCELLING,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.CANCELLING: {
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
    },
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.INTERRUPTED: set(),
}


@dataclass(frozen=True, slots=True)
class StoredRunEvent:
    sequence: int
    activity: ActivityItem


@dataclass(frozen=True, slots=True)
class PreparedContextInjection:
    context_pack_id: str
    pack_hash: str
    injection_hash: str
    provider_messages_hash: str
    messages: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ContextPackKnowledgeTarget:
    context_pack_id: str
    pack_hash: str
    knowledge_fingerprint: str | None
    project_kb_knowledge_fingerprint: str | None = None
    project_id: str | None = None
    project_kb_state_id: str | None = None
    project_kb_manifest_hash: str | None = None
    nlcl_route_source: str | None = None


CurrentProjectKbVerifier = Callable[
    [ContextPackKnowledgeTarget, sqlite3.Connection],
    None,
]


@dataclass(frozen=True, slots=True)
class StoredToolCall:
    tool_call_id: str
    run_id: str
    tool_name: str
    tool_version: str
    status: str
    idempotency_key: str | None
    request_hash: str
    arguments: Any
    output: Any
    error: ErrorEnvelope | None
    call_activity_id: str | None
    result_activity_id: str | None
    result_hash: str | None
    duration_ms: int | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkflowResumeClaim:
    run: Run
    plan: WorkflowPlan
    checkpoints: tuple[WorkflowStepCheckpoint, ...]
    next_step_ordinal: int | None


# 拼对话历史时从库里最多读这么多条（摘要之后的）。只是读库的保险：压缩线（256K 6 万字、1M 24 万字）
# 平时早就先到了。以前是 500，再往前还有一道 64 条的窗口（9-27 去掉）。
PROVIDER_HISTORY_ROW_LIMIT = 5_000


def _provider_message_characters(message: dict[str, object]) -> int:
    """一条发给模型的消息有多长：正文 + 工具调用的参数（按字数）。"""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    calls = message.get("tool_calls")
    return (len(content) if isinstance(content, str) else 0) + (
        len(json.dumps(calls, ensure_ascii=False)) if calls else 0
    )


def _provider_message_tokens(message: dict[str, object]) -> int:
    """同上，按估算的 token（和一轮里算预算的 ``_estimate_provider_messages_tokens`` 同一个口径）。"""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    calls = message.get("tool_calls")
    return (
        4
        + conservative_token_estimate(str(message.get("role") or ""))
        + (conservative_token_estimate(content) if isinstance(content, str) and content else 0)
        + (conservative_token_estimate(json.dumps(calls, ensure_ascii=False, default=str)) if calls else 0)
    )


class AgentStoreError(RuntimeError):
    pass


class AgentStoreNotFound(AgentStoreError):
    pass


class AgentStoreConflict(AgentStoreError):
    pass


class AgentAdmissionWithdrawn(AgentStoreConflict):
    code = "agent_admission_withdrawn"
    retryable = False


class AgentSteeringPending(AgentStoreConflict):
    """A user input was admitted before completion; the same Run must consume it."""

    code = "agent_steering_pending"


class AgentExecutorConflict(AgentStoreConflict):
    code = "workflow_executor_changed"
    retryable = False


class AgentStoreInvalidState(AgentStoreError):
    pass


class AgentWorkflowCheckpointConflict(AgentStoreConflict):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AgentCandidateOutputConflict(AgentStoreConflict):
    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderRecoveryConflict(AgentStoreConflict):
    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderTurnReceiptConflict(AgentStoreConflict):
    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class CreationInstructionConflict(AgentStoreConflict):
    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AgentContextPackNotFound(AgentStoreNotFound):
    pass


RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES = frozenset(
    {
        "context_pack_stale",
        "context_pack_source_changed",
        "project_kb_state_missing",
        "project_kb_state_stale",
        "project_kb_binding_changed",
        "project_kb_knowledge_fingerprint_mismatch",
    }
)


class AgentContextPackConflict(AgentStoreConflict):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = code in RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES


class AgentContextPackSourceUnavailable(AgentStoreError):
    code = "context_pack_source_unavailable"
    retryable = True


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


_TITLE_PLACEHOLDER_PREFIXES = ("新对话 ·", "新对话:")
# 「写作对话」：9-27 以前在书里直接打字开的对话都叫这个（前端写死），一直没被改名；现在当占位，下次说话收尾时补上名字。
_TITLE_PLACEHOLDERS = {"Writing Agent", "随便聊聊", "写作助手", "写作对话"}


def _title_is_placeholder(title: str) -> bool:
    """占位标题才允许被第一轮对话自动覆盖；作者起过名字就绝不动。"""
    normalized = title.strip()
    if normalized in _TITLE_PLACEHOLDERS:
        return True
    return any(normalized.startswith(prefix) for prefix in _TITLE_PLACEHOLDER_PREFIXES)


def _derive_session_title(message: str) -> str | None:
    """从第一句话提炼对话标题：首行、去口语前缀、压到 24 字。"""
    first_line = next(
        (line.strip() for line in message.splitlines() if line.strip()),
        "",
    )
    if not first_line:
        return None
    cleaned = re.sub(
        r"^(?:帮我|请|我想|我们要?|麻烦|请问|大家好|你好[呀啊]?,?)",
        "",
        first_line,
    ).strip()
    if not cleaned:
        cleaned = first_line
    trimmed = cleaned[:24]
    if len(cleaned) > 24:
        trimmed = trimmed.rstrip("，。、；：,.:; ") + "…"
    return trimmed or None


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class AgentStore:
    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    def create_session(
        self,
        *,
        project_id: str | None,
        title: str,
        executor: ExecutorIdentity,
        capabilities: CapabilityProfile,
    ) -> AgentSessionResponse:
        created_at = _now()
        session_id = _new_id("session")
        correlation_id = _new_id("correlation")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if project_id is None:
                    # 「随便聊」会话：不绑定小说项目，照常持久化历史。
                    fingerprint = None
                    revision = None
                else:
                    project = connection.execute(
                        "SELECT * FROM project_binding WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                    if project is None:
                        raise AgentStoreNotFound("Project binding was not found")
                    fingerprint = str(project["fingerprint"])
                    revision = int(project["revision"])
                    if revision < 1 or len(fingerprint) != 64:
                        raise AgentStoreConflict(
                            "Project must have a current inspected snapshot before creating a session"
                        )
                timestamp = created_at.isoformat()
                connection.execute(
                    """
                    INSERT INTO execution_session(
                        id, project_binding_id, title, status, revision,
                        latest_project_revision, latest_project_fingerprint,
                        executor_json, capabilities_json, correlation_id,
                        last_run_ordinal, last_message_ordinal, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', 1, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        session_id,
                        project_id,
                        title,
                        revision,
                        fingerprint,
                        _json(executor.model_dump(mode="json")),
                        _json(capabilities.model_dump(mode="json")),
                        correlation_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:  # pragma: no cover - transaction invariant.
            raise RuntimeError("session transaction produced no row")
        return self._session_from_row(row)

    def fork_session(
        self,
        source_session_id: str,
        *,
        up_to_message_id: str,
        title: str | None = None,
    ) -> AgentSessionResponse:
        """Copy a transcript boundary, never an execution or a file operation.

        session_message requires a Run FK. conversation_fork rows are inert,
        interrupted history containers, excluded from the execution list. Their
        activities are rebuilt with local identities and contain only messages.
        """
        created_at = _now()
        timestamp = created_at.isoformat()
        new_session_id = _new_id("session")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?", (source_session_id,)
                ).fetchone()
                if source is None:
                    raise AgentStoreNotFound("Source agent session was not found")
                target = connection.execute(
                    "SELECT * FROM session_message WHERE session_id = ? AND message_id = ?",
                    (source_session_id, up_to_message_id),
                ).fetchone()
                if target is None:
                    raise AgentStoreNotFound("Target message was not found in source session")
                if target["role"] not in ("user", "assistant"):
                    raise AgentStoreConflict("只能从作者消息或完整回答新建分支")
                messages = connection.execute(
                    """
                    SELECT sm.*, a.payload_json FROM session_message sm
                    JOIN activity a ON a.id = sm.activity_id
                    WHERE sm.session_id = ? AND sm.ordinal <= ?
                      AND sm.role IN ('user', 'assistant')
                    ORDER BY sm.ordinal
                    """,
                    (source_session_id, int(target["ordinal"])),
                ).fetchall()
                source_runs = list(dict.fromkeys(str(m["run_id"]) for m in messages))
                # 没给名字就用源对话此刻的名字（第一轮答完后台会自动改名，前端手里那份可能还是旧的）；
                # 从分支再分叉不叠成「（分支）（分支）」。
                source_title = str(source["title"])
                default_title = source_title if source_title.endswith("（分支）") else f"{source_title}（分支）"
                connection.execute(
                    """
                    INSERT INTO execution_session(
                        id, project_binding_id, title, status, revision,
                        latest_project_revision, latest_project_fingerprint,
                        executor_json, capabilities_json, correlation_id,
                        last_run_ordinal, last_message_ordinal, created_at, updated_at
                    ) VALUES (?, ?, ?, 'idle', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (new_session_id, source["project_binding_id"],
                     (title.strip() if title and title.strip() else default_title)[:120],
                     source["latest_project_revision"], source["latest_project_fingerprint"],
                     source["executor_json"], source["capabilities_json"], _new_id("correlation"),
                     len(source_runs), len(messages), timestamp, timestamp),
                )
                run_map: dict[str, str] = {}
                for ordinal, old_id in enumerate(source_runs, 1):
                    new_id = _new_id("run")
                    run_map[old_id] = new_id
                    source_run = connection.execute("SELECT * FROM run WHERE id = ?", (old_id,)).fetchone()
                    message_count = sum(str(m["run_id"]) == old_id for m in messages)
                    connection.execute(
                        """
                        INSERT INTO run(
                            id, session_id, project_binding_id, context_pack_id, ordinal,
                            run_type, status, objective, idempotency_key, request_hash,
                            project_revision, project_fingerprint, executor_json, capabilities_json,
                            correlation_id, causation_id, last_activity_sequence, error_json,
                            created_at, started_at, updated_at, completed_at
                        ) VALUES (?, ?, ?, NULL, ?, 'conversation_fork', 'interrupted', ?, ?, ?,
                                  ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)
                        """,
                        (new_id, new_session_id, source_run["project_binding_id"], ordinal,
                         source_run["objective"], f"fork:{source_session_id}:{old_id}",
                         hashlib.sha256(f"{source_session_id}:{old_id}:{up_to_message_id}".encode()).hexdigest(),
                         source_run["project_revision"], source_run["project_fingerprint"],
                         source_run["executor_json"], source_run["capabilities_json"],
                         _new_id("correlation"), message_count, timestamp, timestamp, timestamp, timestamp),
                    )
                    connection.execute("UPDATE run SET origin_kind=? WHERE id=?", (source_run["origin_kind"], new_id))
                sequences: dict[str, int] = {}
                ordinal_map: dict[int, int] = {}
                for ordinal, old in enumerate(messages, 1):
                    new_run_id = run_map[str(old["run_id"])]
                    sequences[new_run_id] = sequences.get(new_run_id, 0) + 1
                    new_message_id = _new_id("message")
                    source_activity = ActivityItem.model_validate_json(str(old["payload_json"])).root
                    payload = source_activity.payload.model_dump(mode="json")
                    payload.update(message_id=new_message_id, usage=None, finish_reason=None)
                    row = connection.execute("SELECT * FROM run WHERE id = ?", (new_run_id,)).fetchone()
                    activity = self._insert_activity(
                        connection, row, sequence=sequences[new_run_id], kind=ActivityKind.MESSAGE,
                        payload=payload, causation_id=None, created_at=created_at,
                    ).root
                    connection.execute(
                        """INSERT INTO session_message(
                            activity_id, session_id, run_id, message_id, ordinal, role, format, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (activity.activity_id, new_session_id, new_run_id, new_message_id, ordinal,
                         old["role"], old["format"], old["created_at"]),
                    )
                    ordinal_map[int(old["ordinal"])] = ordinal
                    old_images = json.loads(old["images_json"]) if "images_json" in old.keys() else []
                    if old_images:
                        inherited_images = fork_images(connection, old_images, new_session_id)
                        connection.execute("UPDATE session_message SET images_json=? WHERE message_id=?", (_json(inherited_images), new_message_id))
                # Inherited summary must end before the selected message; later
                # summaries would leak the abandoned future into this branch.
                checkpoint = connection.execute(
                    """SELECT * FROM session_checkpoint WHERE session_id = ? AND through_ordinal <= ?
                       ORDER BY through_ordinal DESC LIMIT 1""",
                    (source_session_id, int(target["ordinal"])),
                ).fetchone()
                if checkpoint is not None and int(checkpoint["through_ordinal"]) in ordinal_map:
                    through = ordinal_map[int(checkpoint["through_ordinal"])]
                    connection.execute(
                        """INSERT INTO session_checkpoint(
                            id, session_id, run_id, through_ordinal, source_message_count,
                            content, content_hash, reported_model, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                        (_new_id("checkpoint"), new_session_id, through,
                         min(through, int(checkpoint["source_message_count"])), checkpoint["content"],
                         checkpoint["content_hash"], checkpoint["reported_model"], timestamp),
                    )
                row = connection.execute("SELECT * FROM execution_session WHERE id = ?", (new_session_id,)).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._session_from_row(row)

    def fork_context_messages(self, session_id: str, *, after_ordinal: int = 0) -> list[AgentMessageResponse]:
        """Unanswered author messages inherited by a fork, without invented replies."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT sm.*, a.payload_json FROM session_message sm
                   JOIN activity a ON a.id = sm.activity_id JOIN run r ON r.id = sm.run_id
                   WHERE sm.session_id = ? AND sm.ordinal > ? AND sm.role = 'user'
                     AND r.run_type = 'conversation_fork'
                     AND NOT EXISTS (SELECT 1 FROM session_message reply
                                     WHERE reply.run_id = r.id AND reply.role = 'assistant')
                   ORDER BY sm.ordinal""", (session_id, after_ordinal),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def get_session(self, session_id: str) -> AgentSessionResponse:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise AgentStoreNotFound("Agent session was not found")
        return self._session_from_row(row)

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        executor: ExecutorIdentity | None = None,
        capabilities: CapabilityProfile | None = None,
    ) -> AgentSessionResponse:
        if (executor is None) != (capabilities is None):
            raise AgentStoreInvalidState(
                "executor and capabilities must be provided together"
            )
        if title is None and pinned is None and executor is None:
            raise AgentStoreInvalidState("No session changes were provided")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or str(row["status"]) == "closed":
                    raise AgentStoreNotFound("Agent session was not found")
                if executor is not None:
                    active_run = connection.execute(
                        """
                        SELECT id FROM run
                        WHERE session_id = ?
                          AND status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                        LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    if active_run is not None:
                        raise AgentStoreConflict(
                            "当前对话还有未结束的任务，请先停止或等待任务完成"
                        )
                next_title = title.strip() if title is not None else str(row["title"])
                if not next_title:
                    raise AgentStoreInvalidState("Agent session title must not be blank")
                next_pinned = int(pinned) if pinned is not None else int(row["pinned"])
                next_executor_json = (
                    _json(executor.model_dump(mode="json"))
                    if executor is not None
                    else str(row["executor_json"])
                )
                next_capabilities_json = (
                    _json(capabilities.model_dump(mode="json"))
                    if capabilities is not None
                    else str(row["capabilities_json"])
                )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET title = ?, pinned = ?, executor_json = ?, capabilities_json = ?,
                        revision = revision + 1
                    WHERE id = ?
                    """,
                    (
                        next_title,
                        next_pinned,
                        next_executor_json,
                        next_capabilities_json,
                        session_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if updated is None:  # pragma: no cover - transaction invariant.
            raise RuntimeError("session update produced no row")
        return self._session_from_row(updated)

    def first_session_objective(self, session_id: str) -> str | None:
        """这段对话的第一句话（建第一个 Run 时的 objective），没有则为 None。"""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT objective FROM run WHERE session_id = ? ORDER BY ordinal ASC LIMIT 1",
                (session_id,),
            ).fetchone()
        return None if row is None else str(row["objective"])

    def maybe_update_auto_session_title(
        self,
        session_id: str,
        *,
        first_objective: str,
        new_title: str,
    ) -> bool:
        """把第一轮的占位/提炼标题换成 AI 总结的名字。

        只允许覆盖两种现状：还是占位标题，或正好等于从第一句话提炼出来的
        那个标题——两者都是系统起的。作者自己改过的名字绝不覆盖。
        """
        cleaned = new_title.strip()
        if not cleaned:
            return False
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT title FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                current = str(row["title"])
                if not _title_is_placeholder(current) and current != _derive_session_title(first_objective):
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE execution_session SET title = ?, revision = revision + 1 WHERE id = ?",
                    (cleaned, session_id),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def close_session(self, session_id: str) -> None:
        timestamp = _now().isoformat()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise AgentStoreNotFound("Agent session was not found")
                if str(row["status"]) == "closed":
                    connection.commit()
                    return
                active_run = connection.execute(
                    """
                    SELECT id FROM run
                    WHERE session_id = ?
                      AND status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if active_run is not None:
                    raise AgentStoreConflict(
                        "当前对话还有未结束的任务，请先停止或等待任务完成"
                    )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET status = 'closed', pinned = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, session_id),
                )
                from zhimeng_runtime.command_completion import CommandCompletionStore
                CommandCompletionStore(self.database).bump_stop_generation(session_id, connection=connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_sessions(self, project_id: str) -> list[AgentSessionResponse]:
        with self.database.connection() as connection:
            project = connection.execute(
                "SELECT id FROM project_binding WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise AgentStoreNotFound("Project binding was not found")
            rows = connection.execute(
                """
                SELECT * FROM execution_session
                WHERE project_binding_id = ? AND status != 'closed'
                ORDER BY pinned DESC, updated_at DESC, id ASC
                """,
                (project_id,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def list_all_sessions(self) -> list[AgentSessionResponse]:
        """全部对话：绑在各部小说下的 + 不绑书的纯对话。

        界面左栏按「独立对话 / 小说分组」两段渲染，一部小说下面挂它自己的对话；
        一次列全，免得每本书各发一次请求。绑在哪部小说看 project_revision.project_id。
        """
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_session
                WHERE status != 'closed'
                ORDER BY pinned DESC, updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def rebind_session(
        self,
        session_id: str,
        *,
        project_id: str | None,
    ) -> AgentSessionResponse:
        """把一条对话改挂到另一部小说下，或解绑成不针对任何书的纯对话。

        正在跑的对话不许改绑：上下文已经按原来那本书装配好了，中途换书会让这一轮的
        结果落到别的书上。历史消息原样保留，只换归属。
        """
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None or str(row["status"]) == "closed":
                    raise AgentStoreNotFound("Agent session was not found")
                active_run = connection.execute(
                    """
                    SELECT id FROM run
                    WHERE session_id = ?
                      AND status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if active_run is not None:
                    raise AgentStoreConflict(
                        "当前对话还有未结束的任务，请先停止或等待任务完成"
                    )
                if project_id is None:
                    revision: int | None = None
                    fingerprint: str | None = None
                else:
                    project = connection.execute(
                        "SELECT * FROM project_binding WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                    if project is None:
                        raise AgentStoreNotFound("Project binding was not found")
                    fingerprint = str(project["fingerprint"])
                    revision = int(project["revision"])
                    if revision < 1 or len(fingerprint) != 64:
                        raise AgentStoreConflict(
                            "Project must have a current inspected snapshot before creating a session"
                        )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET project_binding_id = ?,
                        latest_project_revision = ?,
                        latest_project_fingerprint = ?,
                        revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (project_id, revision, fingerprint, _now().isoformat(), session_id),
                )
                if row["project_binding_id"] != project_id:
                    from zhimeng_runtime.command_completion import CommandCompletionStore
                    CommandCompletionStore(self.database).bump_stop_generation(session_id, connection=connection)
                updated = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if updated is None:  # pragma: no cover - transaction invariant.
            raise RuntimeError("session rebind transaction produced no row")
        return self._session_from_row(updated)

    def list_unbound_sessions(self) -> list[AgentSessionResponse]:
        """「随便聊」会话清单：不绑定任何小说项目的 Agent 会话。"""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_session
                WHERE project_binding_id IS NULL AND status != 'closed'
                ORDER BY pinned DESC, updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def context_pack_knowledge_target(
        self,
        context_pack_id: str,
        *,
        expected_context_pack_hash: str,
    ) -> ContextPackKnowledgeTarget:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT context_pack.id, context_pack.project_binding_id,
                       context_pack.pack_hash, context_pack.knowledge_fingerprint,
                       context_pack.project_kb_state_id,
                       context_pack.project_kb_manifest_hash,
                       nlcl_route_receipt.source AS nlcl_route_source
                FROM context_pack
                LEFT JOIN nlcl_route_receipt
                  ON nlcl_route_receipt.context_pack_id = context_pack.id
                WHERE context_pack.id = ?
                """,
                (context_pack_id,),
            ).fetchone()
        if row is None:
            raise AgentContextPackNotFound("Context Pack was not found")
        if str(row["pack_hash"]) != expected_context_pack_hash:
            raise AgentContextPackConflict(
                "Context Pack hash does not match the reviewed Pack",
                code="context_pack_hash_mismatch",
            )
        return self._knowledge_target_from_row(row)

    def run_context_pack_knowledge_target(
        self,
        run_id: str,
    ) -> ContextPackKnowledgeTarget | None:
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT context_pack_id FROM run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise AgentStoreNotFound("Agent Run was not found")
            if run["context_pack_id"] is None:
                return None
            row = connection.execute(
                """
                SELECT context_pack.id, context_pack.project_binding_id,
                       context_pack.pack_hash, context_pack.knowledge_fingerprint,
                       context_pack.project_kb_state_id,
                       context_pack.project_kb_manifest_hash,
                       nlcl_route_receipt.source AS nlcl_route_source
                FROM context_pack
                LEFT JOIN nlcl_route_receipt
                  ON nlcl_route_receipt.context_pack_id = context_pack.id
                WHERE context_pack.id = ?
                """,
                (str(run["context_pack_id"]),),
            ).fetchone()
        if row is None:
            raise AgentContextPackNotFound("Context Pack was not found")
        return self._knowledge_target_from_row(row)

    def create_run(
        self,
        *,
        session_id: str,
        message: str,
        expected_project_revision: int | None,
        expected_project_fingerprint: str | None,
        active_chapter_id: str | None,
        writer_mode: Literal[
            "repair_only",
            "author_review_change_proposal",
        ] = "repair_only",
        idempotency_key: str,
        context_pack_id: str | None = None,
        expected_context_pack_hash: str | None = None,
        knowledge_observation: KnowledgeFingerprintObservation | None = None,
        current_executor: ExecutorIdentity | None = None,
        current_capabilities: CapabilityProfile | None = None,
        workflow_plan_builder: Callable[[Run], WorkflowPlan] | None = None,
        continuation_source_run_id: str | None = None,
        review_source_run_id: str | None = None,
        manual_file_ids: list[str] | None = None,
        open_file_path: str | None = None,
        manual_skill_names: list[str] | None = None,
        image_ids: list[str] | None = None,
        completion_event_ids: list[str] | None = None,
        goal: GoalAdmission | None = None,
    ) -> tuple[Run, AgentMessageResponse | None, bool]:
        if goal and (context_pack_id or review_source_run_id or completion_event_ids):
            raise AgentStoreInvalidState("持续目标使用普通 Agent 执行。")
        if (context_pack_id is None) != (expected_context_pack_hash is None):
            raise AgentStoreInvalidState(
                "context_pack_id and expected_context_pack_hash must be provided together"
            )
        if (current_executor is None) != (current_capabilities is None):
            raise AgentStoreInvalidState(
                "current executor and capabilities must be provided together"
            )
        request_hash = hashlib.sha256(
            _json(
                {
                    "message": message,
                    "expected_project_revision": expected_project_revision,
                    "expected_project_fingerprint": expected_project_fingerprint,
                    "active_chapter_id": active_chapter_id,
                    "writer_mode": writer_mode,
                    "context_pack_id": context_pack_id,
                    "expected_context_pack_hash": expected_context_pack_hash,
                    **({"manual_file_ids": sorted(set(manual_file_ids))} if manual_file_ids else {}),
                    **({"manual_skill_names": list(manual_skill_names)} if manual_skill_names else {}),
                    **({"image_ids": list(dict.fromkeys(image_ids))} if image_ids else {}),
                    **({"completion_event_ids": completion_event_ids} if completion_event_ids else {}),
                    **({"goal": goal.model_dump(mode="json")} if goal else {}),
                    **({"open_file_path": open_file_path} if open_file_path else {}),
                    **({"continuation_source_run_id": continuation_source_run_id}
                       if continuation_source_run_id is not None else {}),
                    **({"review_source_run_id": review_source_run_id} if review_source_run_id is not None else {}),
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                context_pack: sqlite3.Row | None = None
                session = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise AgentStoreNotFound("Agent session was not found")
                existing = connection.execute(
                    "SELECT * FROM run WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_hash"]) != request_hash:
                        raise AgentStoreConflict(
                            "Idempotency key was already used with a different request"
                        )
                    message_row = (None if existing["origin_kind"] == "command_completion" else
                                   self._message_row_for_run(connection, str(existing["id"]), "user", first=True))
                    connection.commit()
                    return (
                        self._run_from_row(existing),
                        self._message_from_row(message_row) if message_row is not None else None,
                        True,
                    )

                if connection.execute(
                    "SELECT 1 FROM session_admission_withdrawal WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, idempotency_key),
                ).fetchone() is not None:
                    raise AgentAdmissionWithdrawn("这条尚未受理的发送已取回，可以编辑后重新发送")

                if str(session["status"]) not in {"active", "idle"}:
                    raise AgentStoreInvalidState("Agent session is not active")
                from zhimeng_runtime.command_completion import CommandCompletionStore
                completion_store = CommandCompletionStore(self.database)
                completion_events = []
                if completion_event_ids:
                    eligible = {item["event_id"]: item for item in completion_store.pending(session_id, connection=connection)}
                    if any(event_id not in eligible for event_id in completion_event_ids):
                        raise AgentStoreConflict("后台命令结果已处理或已经停止接续")
                    queue = connection.execute("SELECT state_json FROM session_queue WHERE session_id=?", (session_id,)).fetchone()
                    if queue and json.loads(queue["state_json"]).get("items"):
                        raise AgentStoreConflict("用户排队消息先处理")
                    completion_events = [eligible[event_id] for event_id in completion_event_ids]
                images = claim_images(connection, session_id, image_ids)
                continuation_handoff = None
                goal_to_bind = goal
                review_context = None
                if review_source_run_id is not None:
                    from zhimeng_runtime.task_review import build_review_record
                    source = self._require_run_row(connection, review_source_run_id)
                    if source['session_id'] != session_id or source['project_binding_id'] != session['project_binding_id']:
                        raise AgentStoreConflict('复核任务必须属于当前对话和文件夹')
                    if source['status'] not in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                        raise AgentStoreConflict('原任务还在执行，请结束后再复核')
                    if context_pack_id or continuation_source_run_id or completion_event_ids or writer_mode != 'repair_only':
                        raise AgentStoreConflict('独立复核使用普通只读任务，不能同时接续或执行专用流程')
                    review_context = build_review_record(connection, source)
                if continuation_source_run_id is not None:
                    from zhimeng_runtime.agent_continuation import build_handoff, inspect_continuation
                    source = self._require_run_row(connection, continuation_source_run_id)
                    if source["session_id"] != session_id:
                        raise AgentStoreConflict("接续任务必须属于同一对话")
                    if workflow_plan_builder is not None:
                        raise AgentStoreConflict("受管流程不能使用普通任务接续")
                    continuation_state = inspect_continuation(connection, source)
                    if not continuation_state.can_continue:
                        raise AgentStoreConflict(continuation_state.reason)
                    continuation_handoff = build_handoff(connection, source)
                    source_goal = GoalStore.for_run(connection, continuation_source_run_id)
                    if goal_to_bind is None and source_goal and source_goal.status == "paused":
                        goal_to_bind = GoalAdmission(action="resume", goal_id=source_goal.goal_id,
                                                     expected_revision=source_goal.revision)
                    from zhimeng_runtime.task_review import review_record
                    review_context = review_record(connection, source['id'])
                    if review_context is not None:
                        writer_mode = 'repair_only'

                session_executor_json = str(session["executor_json"])
                session_capabilities_json = str(session["capabilities_json"])
                if current_executor is not None and current_capabilities is not None:
                    stored_executor = ExecutorIdentity.model_validate_json(
                        session_executor_json
                    )
                    if stored_executor != current_executor:
                        legacy_upgrade = (
                            stored_executor.instance_id is None
                            and current_executor.instance_id is not None
                            and stored_executor.model_copy(
                                update={"instance_id": current_executor.instance_id}
                            )
                            == current_executor
                        )
                        managed_o2e_adoption = False
                        if not legacy_upgrade:
                            managed_o2e_adoption = (
                                self._can_adopt_managed_o2e_session_executor(
                                    connection,
                                    session_id=session_id,
                                    context_pack_id=context_pack_id,
                                )
                            )
                        if not legacy_upgrade and not managed_o2e_adoption:
                            raise AgentExecutorConflict(
                                "Agent session executor configuration changed"
                            )
                        session_executor_json = _json(
                            current_executor.model_dump(mode="json")
                        )
                    session_capabilities_json = _json(
                        current_capabilities.model_dump(mode="json")
                    )

                active_run = connection.execute(
                    """
                    SELECT id FROM run
                    WHERE session_id = ?
                      AND status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if active_run is not None:
                    raise AgentStoreConflict(
                        "Agent session already has a non-terminal Run"
                    )

                session_project_id = session["project_binding_id"]
                if session_project_id is None:
                    # 「随便聊」Run：会话没有项目绑定，跳过项目身份校验，
                    # Run 行的三列项目身份落 NULL。
                    project_id = None
                else:
                    project_id = str(session_project_id)
                    project = connection.execute(
                        "SELECT * FROM project_binding WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                    if project is None:
                        raise AgentStoreConflict(
                            "Session project binding no longer exists"
                        )
                    if (
                        expected_project_revision is None
                        or expected_project_fingerprint is None
                    ):
                        raise AgentStoreInvalidState(
                            "Project-bound Run requires expected project revision and fingerprint"
                        )
                    if (
                        int(project["revision"]) != expected_project_revision
                        or str(project["fingerprint"]) != expected_project_fingerprint
                    ):
                        raise AgentStoreConflict(
                            "Project revision changed; refresh the project before sending this turn"
                        )

                if context_pack_id is not None:
                    context_pack = connection.execute(
                        "SELECT * FROM context_pack WHERE id = ?",
                        (context_pack_id,),
                    ).fetchone()
                    if context_pack is None:
                        raise AgentContextPackNotFound("Context Pack was not found")
                    if str(context_pack["pack_hash"]) != expected_context_pack_hash:
                        raise AgentContextPackConflict(
                            "Context Pack hash does not match the reviewed Pack",
                            code="context_pack_hash_mismatch",
                        )
                    # 资料包可以不绑知识库快照（普通对话自 2026-09-25 起都不绑）。
                    if (
                        str(context_pack["session_id"]) != session_id
                        or str(context_pack["project_binding_id"]) != project_id
                    ):
                        raise AgentContextPackConflict(
                            "Context Pack belongs to another session or project",
                            code="context_pack_scope_mismatch",
                        )
                    consumed = connection.execute(
                        "SELECT id FROM run WHERE context_pack_id = ? LIMIT 1",
                        (context_pack_id,),
                    ).fetchone()
                    if consumed is not None:
                        raise AgentContextPackConflict(
                            "Context Pack was already consumed by another Run",
                            code="context_pack_already_consumed",
                        )
                    if (
                        int(context_pack["project_revision"])
                        != expected_project_revision
                        or str(context_pack["project_fingerprint"])
                        != expected_project_fingerprint
                        or int(context_pack["history_through_ordinal"])
                        != int(session["last_message_ordinal"])
                    ):
                        raise AgentContextPackConflict(
                            "Context Pack is stale for the current project or session history",
                            code="context_pack_stale",
                        )
                    pack_chapter_id = (
                        str(context_pack["active_chapter_id"])
                        if context_pack["active_chapter_id"] is not None
                        else None
                    )
                    if (
                        str(context_pack["objective"]) != message
                        or pack_chapter_id != active_chapter_id
                    ):
                        raise AgentContextPackConflict(
                            "Context Pack objective or active chapter does not match the Run",
                            code="context_pack_request_mismatch",
                        )
                    if str(context_pack["readiness"]) == "blocked":
                        raise AgentContextPackConflict(
                            "Blocked Context Pack cannot start a Run",
                            code="context_pack_blocked",
                        )
                    prewrite_package = connection.execute(
                        """
                        SELECT payload_json FROM prewrite_operation_package
                        WHERE context_pack_id = ?
                        """,
                        (context_pack_id,),
                    ).fetchone()
                    if prewrite_package is None:
                        raise AgentContextPackConflict(
                            "Context Pack has no prewrite operation package",
                            code="prewrite_operation_package_missing",
                        )
                    try:
                        prewrite_payload = json.loads(
                            str(prewrite_package["payload_json"])
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise AgentContextPackConflict(
                            "Context Pack has an invalid prewrite operation package",
                            code="prewrite_operation_package_invalid",
                        ) from exc
                    missing_requirements = (
                        prewrite_payload.get("missing_requirements")
                        if isinstance(prewrite_payload, dict)
                        else None
                    )
                    if not isinstance(missing_requirements, list) or any(
                        not isinstance(item, str) for item in missing_requirements
                    ):
                        raise AgentContextPackConflict(
                            "Context Pack has an invalid prewrite operation package",
                            code="prewrite_operation_package_invalid",
                        )
                    if (
                        "migrated_prewrite_package_requires_recompile"
                        in missing_requirements
                    ):
                        raise AgentContextPackConflict(
                            "Migrated prewrite operation package must be recompiled",
                            code="prewrite_operation_package_recompile_required",
                        )
                    self._require_current_knowledge(
                        context_pack,
                        knowledge_observation,
                    )
                created_at = _now()
                run_id = _new_id("run")
                run_correlation_id = _new_id("correlation")
                ordinal = int(session["last_run_ordinal"]) + 1
                connection.execute(
                    """
                    INSERT INTO run(
                        id, session_id, project_binding_id, context_pack_id, ordinal,
                        run_type, status, objective, idempotency_key, request_hash, project_revision,
                        project_fingerprint, executor_json, capabilities_json,
                        correlation_id, causation_id, last_activity_sequence, error_json,
                        created_at, started_at, updated_at, completed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'agent_turn', 'queued', ?, ?, ?, ?, ?, ?, ?, ?,
                        NULL, 0, NULL, ?, NULL, ?, NULL
                    )
                    """,
                    (
                        run_id,
                        session_id,
                        project_id,
                        context_pack_id,
                        ordinal,
                        message,
                        idempotency_key,
                        request_hash,
                        expected_project_revision,
                        expected_project_fingerprint,
                        session_executor_json,
                        session_capabilities_json,
                        run_correlation_id,
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
                run = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
                if goal_to_bind:
                    GoalStore.bind(connection, session_id, run_id, message, goal_to_bind)
                connection.execute("UPDATE run SET origin_kind=?,stop_generation=? WHERE id=?",
                    ("command_completion" if completion_event_ids else "user",
                     completion_store.current_generation(session_id, connection=connection), run_id))
                run = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
                if run is None:  # pragma: no cover - insert invariant.
                    raise RuntimeError("run transaction produced no row")
                # 第一轮对话顺手把占位标题换成「按内容起的标题」：
                # 作者回看历史时靠的是标题，不是时间戳。作者手动改过的
                # 名字（或已经像样的标题）绝不覆盖。
                if not completion_event_ids and ordinal == 1 and _title_is_placeholder(str(session["title"])):
                    derived = _derive_session_title(message)
                    if derived:
                        connection.execute(
                            "UPDATE execution_session SET title = ? WHERE id = ?",
                            (derived, session_id),
                        )
                if workflow_plan_builder is not None:
                    plan = workflow_plan_builder(self._run_from_row(run))
                    self._create_workflow_plan_in_connection(
                        connection,
                        run,
                        self._validated_workflow_plan(plan),
                    )
                sequence = 1
                causation_id: str | None = None
                if context_pack is not None:
                    artifact_id = _new_id("artifact")
                    pack_hash = str(context_pack["pack_hash"])
                    metadata = {
                        "context_pack_id": context_pack_id,
                        "pack_hash": pack_hash,
                        "injection_hash": str(context_pack["injection_hash"]),
                        "provider_messages_hash": str(
                            context_pack["provider_messages_hash"]
                        ),
                        "knowledge_fingerprint": (
                            str(context_pack["knowledge_fingerprint"])
                            if context_pack["knowledge_fingerprint"] is not None
                            else None
                        ),
                        # 普通对话的资料包不绑知识库快照：记 null，不是字符串 "None"。
                        "project_kb_state_id": (
                            str(context_pack["project_kb_state_id"])
                            if context_pack["project_kb_state_id"] is not None
                            else None
                        ),
                        "project_kb_manifest_hash": (
                            str(context_pack["project_kb_manifest_hash"])
                            if context_pack["project_kb_manifest_hash"] is not None
                            else None
                        ),
                    }
                    connection.execute(
                        """
                        INSERT INTO artifact(
                            id, run_id, artifact_type, relative_path,
                            content_hash, metadata_json, created_at
                        ) VALUES (?, ?, 'context_pack', NULL, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            run_id,
                            pack_hash,
                            _json(metadata),
                            created_at.isoformat(),
                        ),
                    )
                    context_activity = self._insert_activity(
                        connection,
                        run,
                        sequence=sequence,
                        kind=ActivityKind.ARTIFACT,
                        payload=ArtifactPayload(
                            artifact_id=artifact_id,
                            artifact_type="context_pack",
                            name="Reviewed Context Pack",
                            uri=f"runtime://context-packs/{context_pack_id}",
                            media_type="application/json",
                            content_hash=pack_hash,
                            size_bytes=len(
                                str(context_pack["payload_json"]).encode("utf-8")
                            ),
                        ).model_dump(mode="json"),
                        causation_id=None,
                        created_at=created_at,
                    )
                    causation_id = context_activity.root.activity_id
                    sequence += 1
                # 挡位每一轮都记（不绑书的也记）：工具目录和执行命令前的检查都按它来。
                writer_intent = {
                    "schema": "zhimeng.writer-intent.v1",
                    "mode": writer_mode,
                    "active_chapter_id": active_chapter_id,
                    **({"manual_file_ids": sorted(set(manual_file_ids or []))} if context_pack is None else {}),
                    **({"open_file_path": open_file_path} if open_file_path else {}),
                }
                writer_intent_hash = canonical_json_hash(writer_intent)
                writer_intent_id = _new_id("artifact")
                connection.execute(
                    """
                    INSERT INTO artifact(
                        id, run_id, artifact_type, relative_path,
                        content_hash, metadata_json, created_at
                    ) VALUES (?, ?, 'writer_intent', NULL, ?, ?, ?)
                    """,
                    (
                        writer_intent_id,
                        run_id,
                        writer_intent_hash,
                        _json(writer_intent),
                        created_at.isoformat(),
                    ),
                )
                intent_activity = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.ARTIFACT,
                    payload=ArtifactPayload(
                        artifact_id=writer_intent_id,
                        artifact_type="writer_intent",
                        name="Writer intent",
                        uri=f"runtime://agent/runs/{run_id}/writer-intent",
                        media_type="application/json",
                        content_hash=writer_intent_hash,
                        size_bytes=len(_json(writer_intent).encode("utf-8")),
                    ).model_dump(mode="json"),
                    causation_id=causation_id,
                    created_at=created_at,
                )
                causation_id = intent_activity.root.activity_id
                sequence += 1
                message_id = _new_id("message")
                user_activity = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.MESSAGE if not completion_event_ids else ActivityKind.RUN_STATE,
                    payload=MessagePayload(
                        role="user",
                        content=message,
                        name=None,
                        message_id=message_id,
                        phase="completed",
                        delta=None,
                        format="plain",
                        finish_reason=None,
                        usage=None,
                    ).model_dump(mode="json") if not completion_event_ids else RunStatePayload(
                        status=RunStatus.QUEUED, previous_status=None, detail="后台命令结果触发接续",
                    ).model_dump(mode="json"),
                    causation_id=causation_id,
                    created_at=created_at,
                )
                message_ordinal = int(session["last_message_ordinal"]) + (0 if completion_event_ids else 1)
                if not completion_event_ids:
                    connection.execute(
                    """
                    INSERT INTO session_message(
                        activity_id, session_id, run_id, message_id, ordinal,
                        role, format, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'user', 'plain', ?)
                    """,
                    (
                        user_activity.root.activity_id,
                        session_id,
                        run_id,
                        message_id,
                        message_ordinal,
                        created_at.isoformat(),
                    ),
                )
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence + 1,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=RunStatus.QUEUED,
                        previous_status=None,
                        detail="Run accepted and durably queued",
                    ).model_dump(mode="json"),
                    causation_id=user_activity.root.activity_id,
                    created_at=created_at,
                )
                if images:
                    connection.execute("UPDATE session_message SET images_json=? WHERE message_id=?", (_json(images), message_id))
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ? WHERE id = ?",
                    (sequence + 1, run_id),
                )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET status = 'active', revision = revision + 1,
                        latest_project_revision = ?, latest_project_fingerprint = ?,
                        executor_json = ?, capabilities_json = ?,
                        last_run_ordinal = ?, last_message_ordinal = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        expected_project_revision,
                        expected_project_fingerprint,
                        session_executor_json,
                        session_capabilities_json,
                        ordinal,
                        message_ordinal,
                        created_at.isoformat(),
                        session_id,
                    ),
                )
                run = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
                message_row = None if completion_event_ids else self._message_row_for_run(connection, run_id, "user")
                if completion_event_ids:
                    messages = self.command_completion_messages(completion_events)
                    self._save_run_transcript_in_connection(connection, run_id, messages)
                    completion_store.mark_delivered(completion_event_ids, run_id, connection=connection)
                if continuation_handoff is not None:
                    self._insert_provider_recovery_event(
                        connection, run_id, event_type="run_continuation",
                        payload=continuation_handoff, created_at=created_at,
                    )
                if review_context is not None:
                    self._insert_provider_recovery_event(
                        connection, run_id, event_type='task_review', payload=review_context, created_at=created_at,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(run), self._message_from_row(message_row) if message_row is not None else None, False

    def get_run_continuation(self, run_id: str) -> AgentRunContinuationResponse:
        from zhimeng_runtime.agent_continuation import inspect_continuation
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            return inspect_continuation(connection, run)

    def continuation_provider_message(self, run_id: str) -> dict[str, str] | None:
        from zhimeng_runtime.agent_continuation import continuation_record, current_changes, provider_message
        with self.database.connection() as connection:
            self._require_run_row(connection, run_id)
            record = continuation_record(connection, run_id)
            if record is None:
                return None
            # The persisted handoff is retained. Refresh only mutable change status:
            # an author may have rolled back a file after the child was queued.
            record["file_changes"] = current_changes(connection, record.get("source_run_ids", []))
            return provider_message(record)

    def get_writer_mode(
        self,
        run_id: str,
    ) -> Literal["repair_only", "author_review_change_proposal"]:
        with self.database.connection() as connection:
            self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT content_hash, metadata_json FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_intent'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        if not rows:
            return "repair_only"
        if len(rows) != 1:
            raise AgentStoreInvalidState("Run must have exactly one Writer intent")
        try:
            payload = json.loads(str(rows[0]["metadata_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentStoreInvalidState("Stored Writer intent is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "zhimeng.writer-intent.v1"
            or payload.get("mode")
            not in {"repair_only", "author_review_change_proposal"}
            or canonical_json_hash(payload) != str(rows[0]["content_hash"])
        ):
            raise AgentStoreInvalidState("Stored Writer intent failed integrity validation")
        return payload["mode"]

    def _writer_intent_payload(self, run_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT metadata_json FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_intent'
                ORDER BY id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def get_writer_focus(self, run_id: str) -> tuple[str | None, tuple[str, ...]]:
        """这一轮用户在编辑器里开着的章（chapter_id）和亲手附上的文件（file_id）；没记就是空的。"""

        payload = self._writer_intent_payload(run_id)
        if payload is None:
            return None, ()
        chapter = payload.get("active_chapter_id")
        files = payload.get("manual_file_ids")
        return (
            chapter if isinstance(chapter, str) and chapter else None,
            tuple(item for item in files if isinstance(item, str)) if isinstance(files, list) else (),
        )

    def get_open_file_path(self, run_id: str) -> str | None:
        """这一轮用户在右栏开着的文件（打开的文件夹里的相对路径）；没开就是 None。"""

        payload = self._writer_intent_payload(run_id)
        path = payload.get("open_file_path") if payload is not None else None
        return path if isinstance(path, str) and path else None

    def session_first_run(self, session_id: str) -> tuple[str, datetime] | None:
        """这场对话的第一轮（编号和开始时间）：对话开头那段环境信息的日期、挡位取它的，一场对话里不变。"""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, created_at FROM run WHERE session_id = ? ORDER BY ordinal LIMIT 1",
                (session_id,),
            ).fetchone()
        return None if row is None else (str(row["id"]), _parse_time(row["created_at"]))

    def session_opening_context(self, session_id: str) -> dict[str, Any] | None:
        """这场对话开头那份用户记忆和说明文件（第一次记下的那份），还没记过返回 None。

        记在 artifact 表里（类型 opening_context，挂在当时那一轮上）：别处读 artifact 都按类型过滤，不会被误读。
        """

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT a.metadata_json FROM artifact AS a
                JOIN run AS r ON r.id = a.run_id
                WHERE r.session_id = ? AND a.artifact_type = 'opening_context'
                ORDER BY r.ordinal, a.created_at, a.id
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["metadata_json"]))
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def record_opening_context(self, run_id: str, payload: dict[str, Any]) -> None:
        body = _json(payload)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO artifact(
                        id, run_id, artifact_type, relative_path,
                        content_hash, metadata_json, created_at
                    ) VALUES (?, ?, 'opening_context', NULL, ?, ?, ?)
                    """,
                    (
                        f"artifact_opening_{uuid4().hex}",
                        run_id,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        body,
                        _now().isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def find_run_by_idempotency_key(self, session_id: str, key: str):
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM run WHERE session_id = ? AND idempotency_key = ?",
                (session_id, key),
            ).fetchone()
            if row is None:
                if connection.execute(
                    "SELECT 1 FROM session_admission_withdrawal WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, key),
                ).fetchone() is not None:
                    raise AgentAdmissionWithdrawn("这条尚未受理的发送已取回，可以编辑后重新发送")
                return None
            message = None if row["origin_kind"] == "command_completion" else self._message_row_for_run(connection, str(row["id"]), "user", first=True)
            return self._run_from_row(row), self._message_from_row(message) if message is not None else None

    def withdraw_run_admission(
        self, session_id: str, key: str,
    ) -> tuple[Run, AgentMessageResponse] | None:
        """Either preserve the admitted run or retire this key before creation."""
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT id FROM execution_session WHERE id = ?", (session_id,),
                ).fetchone() is None:
                    raise AgentStoreNotFound("Agent session was not found")
                row = connection.execute(
                    "SELECT * FROM run WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, key),
                ).fetchone()
                if row is not None:
                    message = self._message_row_for_run(connection, str(row["id"]), "user", first=True)
                    result = self._run_from_row(row), self._message_from_row(message)
                    connection.commit()
                    return result
                connection.execute(
                    "INSERT INTO session_admission_withdrawal(session_id, idempotency_key, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                    (session_id, key, _now().isoformat()),
                )
                connection.commit()
                return None
            except Exception:
                connection.rollback()
                raise

    def get_run(self, run_id: str) -> Run:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("Agent run was not found")
        return self._run_from_row(row)

    def list_runs(self, session_id: str, *, limit: int = 100) -> list[Run]:
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            rows = connection.execute(
                """
                SELECT * FROM run
                WHERE session_id = ? AND run_type != 'conversation_fork'
                ORDER BY ordinal DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._run_from_row(row) for row in reversed(rows)]

    def begin_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, Any],
        request_hash: str,
        idempotency_key: str | None = None,
    ) -> tuple[StoredToolCall, bool]:
        normalized_version = tool_version.strip()
        normalized_key = idempotency_key.strip() if idempotency_key is not None else None
        if not normalized_version or len(normalized_version) > 128:
            raise AgentStoreInvalidState("Tool version must be 1 to 128 characters")
        if normalized_key is not None and not 8 <= len(normalized_key) <= 128:
            raise AgentStoreInvalidState(
                "ToolCall idempotency key must be 8 to 128 characters"
            )
        if len(request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_hash
        ):
            raise AgentStoreInvalidState("ToolCall request_hash must be lowercase SHA-256")
        activity_payload = ToolCallPayload(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        rendered_arguments = activity_payload.model_dump(mode="json")["arguments"]
        encoded_arguments = _json(rendered_arguments)

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentStoreInvalidState("Tool calls require a running Run")
                existing = connection.execute(
                    "SELECT * FROM tool_call WHERE id = ?",
                    (tool_call_id,),
                ).fetchone()
                if existing is None and normalized_key is not None:
                    existing = connection.execute(
                        """
                        SELECT * FROM tool_call
                        WHERE run_id = ? AND idempotency_key = ?
                        """,
                        (run_id, normalized_key),
                    ).fetchone()
                if existing is not None:
                    if (
                        str(existing["id"]) != tool_call_id
                        or str(existing["run_id"]) != run_id
                        or str(existing["tool_name"]) != tool_name
                        or str(existing["tool_version"]) != normalized_version
                        or str(existing["request_hash"]) != request_hash
                        or str(existing["input_json"]) != encoded_arguments
                        or (
                            (
                                str(existing["idempotency_key"])
                                if existing["idempotency_key"] is not None
                                else None
                            )
                            != normalized_key
                        )
                    ):
                        raise AgentStoreConflict(
                            "ToolCall idempotency key was used with a different request"
                        )
                    connection.commit()
                    return self._tool_call_from_row(existing), True

                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                activity = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.TOOL_CALL,
                    payload=activity_payload.model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO tool_call(
                        id, run_id, tool_name, tool_version, status,
                        idempotency_key, request_hash, input_json, output_json,
                        error_json, call_activity_id, result_activity_id,
                        result_hash, duration_ms, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL, ?, NULL,
                              NULL, NULL, ?, NULL)
                    """,
                    (
                        tool_call_id,
                        run_id,
                        tool_name,
                        normalized_version,
                        normalized_key,
                        request_hash,
                        encoded_arguments,
                        activity.root.activity_id,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence, now.isoformat(), run_id),
                )
                stored = connection.execute(
                    "SELECT * FROM tool_call WHERE id = ?",
                    (tool_call_id,),
                ).fetchone()
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentStoreConflict("ToolCall identity already exists") from exc
            except Exception:
                connection.rollback()
                raise
        if stored is None:  # pragma: no cover - insert invariant.
            raise RuntimeError("ToolCall transaction produced no record")
        return self._tool_call_from_row(stored), False

    def finish_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        status: Literal["succeeded", "failed", "cancelled"],
        output: Any,
        error: ErrorEnvelope | None,
        duration_ms: int | None,
        change_proposal: ChangeProposalPayload | None = None,
        skill_call: SkillCallPayload | None = None,
        artifacts: tuple[ArtifactPayload, ...] = (),
    ) -> tuple[StoredToolCall, bool]:
        result_payload = ToolResultPayload(
            tool_call_id=tool_call_id,
            status=status,
            output=output,
            error=error,
            duration_ms=duration_ms,
        )
        result_hash = self._tool_result_hash(
            result_payload,
            change_proposal,
            skill_call,
            artifacts,
        )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                tool_call = self._require_tool_call_row(
                    connection,
                    tool_call_id,
                )
                if str(tool_call["run_id"]) != run_id:
                    raise AgentStoreConflict("ToolCall belongs to another Run")
                current_status = str(tool_call["status"])
                if current_status in {"succeeded", "failed", "cancelled"}:
                    if str(tool_call["result_hash"] or "") != result_hash:
                        raise AgentStoreConflict(
                            "ToolCall was already finished with a different result"
                        )
                    connection.commit()
                    return self._tool_call_from_row(tool_call), True
                if current_status not in {"pending", "running"}:
                    raise AgentStoreInvalidState(
                        f"ToolCall cannot finish from state {current_status}"
                    )
                if RunStatus(str(run["status"])) in TERMINAL_RUN_STATUSES:
                    raise AgentStoreInvalidState("A terminal Run cannot accept a ToolResult")

                self._finish_tool_call_in_transaction(
                    connection,
                    run,
                    tool_call,
                    result_payload=result_payload,
                    result_hash=result_hash,
                    change_proposal=change_proposal,
                    skill_call=skill_call,
                    artifacts=artifacts,
                    created_at=_now(),
                )
                stored = self._require_tool_call_row(connection, tool_call_id)
                if isinstance(output, dict) and output.get("schema") == "zhimeng.tool-result.command-session.v1" and output.get("status") in {"exited", "cancelled", "interrupted"}:
                    from zhimeng_runtime.command_completion import CommandCompletionStore
                    CommandCompletionStore(self.database).mark_observed(output, run_id, connection=connection)
                elif isinstance(output, dict) and output.get("schema") == "zhimeng.tool-result.command-session-list.v1":
                    from zhimeng_runtime.command_completion import CommandCompletionStore
                    for command in output.get("sessions", []):
                        if isinstance(command, dict) and command.get("status") in {"exited", "cancelled", "interrupted"}:
                            CommandCompletionStore(self.database).mark_observed(command, run_id, connection=connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._tool_call_from_row(stored), False

    def observe_cancelled_mcp_call(
        self, run_id: str, *, tool_call_id: str, output: dict[str, Any], duration_ms: int,
    ) -> StoredToolCall:
        """Attach a local observation after cancellation already closed the call.

        The outcome stays cancelled: neither a scan nor a lost MCP response
        establishes remote success. A new result event lets open clients refresh.
        """
        if output.get("source_mcp") is not True or output.get("cancelled") is not True:
            raise AgentStoreInvalidState("MCP cancellation observation needs its source metadata")
        payload = ToolResultPayload(tool_call_id=tool_call_id, status="cancelled", output=output,
                                    error=None, duration_ms=duration_ms)
        result_hash = self._tool_result_hash(payload, None)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                call = self._require_tool_call_row(connection, tool_call_id)
                if str(call["run_id"]) != run_id or not str(call["tool_name"]).startswith("mcp__"):
                    raise AgentStoreConflict("Cancellation observation belongs to another tool or Run")
                if str(call["status"]) != "cancelled":
                    raise AgentStoreConflict("Only cancelled MCP calls can receive cancellation observations")
                if str(call["result_hash"] or "") != result_hash:
                    self._finish_tool_call_in_transaction(
                        connection, run, call, result_payload=payload, result_hash=result_hash,
                        change_proposal=None, created_at=_now(),
                    )
                stored = self._require_tool_call_row(connection, tool_call_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._tool_call_from_row(stored)

    def get_tool_call(self, tool_call_id: str) -> StoredToolCall:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tool_call WHERE id = ?",
                (tool_call_id,),
            ).fetchone()
        if row is None:
            raise AgentStoreNotFound("ToolCall was not found")
        return self._tool_call_from_row(row)

    def list_tool_calls(self, run_id: str) -> list[StoredToolCall]:
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT id FROM run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise AgentStoreNotFound("Agent run was not found")
            rows = connection.execute(
                """
                SELECT * FROM tool_call
                WHERE run_id = ?
                ORDER BY started_at, id
                """,
                (run_id,),
            ).fetchall()
        return [self._tool_call_from_row(row) for row in rows]

    def create_workflow_plan(self, plan: WorkflowPlan) -> WorkflowPlan:
        """Persist the immutable sequence-zero plan or replay the same plan."""

        plan = self._validated_workflow_plan(plan)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, plan.run_id)
                result = self._create_workflow_plan_in_connection(
                    connection,
                    run,
                    plan,
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Plan identity or sequence already exists",
                    code="workflow_plan_conflict",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return result

    def _create_workflow_plan_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        plan: WorkflowPlan,
    ) -> WorkflowPlan:
        """Insert a Plan inside the caller's Run transaction."""

        self._validate_workflow_plan_binding(connection, run, plan)
        rows = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
            (plan.run_id,),
        ).fetchall()
        if rows:
            first = rows[0]
            if (
                int(first["sequence"]) != 0
                or str(first["checkpoint_type"]) != WORKFLOW_PLAN_CHECKPOINT_TYPE
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow checkpoint chain has no sequence-zero plan",
                    code="workflow_checkpoint_integrity_failed",
                )
            existing = self._workflow_plan_from_row(first)
            if existing.run_id != plan.run_id or existing.plan_hash != plan.plan_hash:
                raise AgentWorkflowCheckpointConflict(
                    "Run already has a different Workflow Plan",
                    code="workflow_plan_conflict",
                )
            self._workflow_chain_from_rows(connection, run, rows)
            self._ensure_creation_instruction_activation_in_connection(
                connection,
                run,
                existing,
            )
            return existing

        conflicting_id = connection.execute(
            "SELECT run_id FROM checkpoint WHERE id = ?",
            (plan.workflow_plan_id,),
        ).fetchone()
        if conflicting_id is not None:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan identity belongs to another Run",
                code="workflow_checkpoint_scope_mismatch",
            )
        connection.execute(
            """
            INSERT INTO checkpoint(
                id, run_id, sequence, checkpoint_type, state_json, created_at
            ) VALUES (?, ?, 0, ?, ?, ?)
            """,
            (
                plan.workflow_plan_id,
                plan.run_id,
                WORKFLOW_PLAN_CHECKPOINT_TYPE,
                _json(plan.model_dump(mode="json", by_alias=True)),
                plan.created_at.isoformat(),
            ),
        )
        stored = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? AND sequence = 0",
            (plan.run_id,),
        ).fetchone()
        if stored is None:  # pragma: no cover - insert invariant.
            raise RuntimeError("Workflow Plan transaction produced no record")
        result = self._workflow_plan_from_row(stored)
        self._ensure_creation_instruction_activation_in_connection(
            connection,
            run,
            result,
        )
        return result

    def _ensure_creation_instruction_activation_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        plan: WorkflowPlan,
    ) -> CreationInstructionActivationBinding | None:
        if not plan.steps or plan.steps[-1].skill_id != CREATION_HANDOFF_SKILL_ID:
            return None
        rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_activation_binding'
            ORDER BY id
            """,
            (plan.run_id,),
        ).fetchall()
        if len(rows) > 1:
            raise CreationInstructionConflict(
                "Run has multiple Creation instruction activation bindings",
                code="creation_instruction_activation_duplicate",
            )
        if rows:
            binding = self._creation_instruction_activation_from_artifact(rows[0])
            if (
                binding.run_id != plan.run_id
                or binding.project_id != plan.project_id
                or binding.workflow_plan_id != plan.workflow_plan_id
                or binding.workflow_plan_hash != plan.plan_hash
            ):
                raise CreationInstructionConflict(
                    "Creation instruction activation does not bind the Workflow Plan",
                    code="creation_instruction_activation_tampered",
                )
            return binding

        binding = self._build_creation_instruction_activation_in_connection(
            connection,
            run,
            plan,
            created_at=_parse_time(run["created_at"]),
        )
        payload_json = _json(binding.model_dump(mode="json", by_alias=True))
        connection.execute(
            """
            INSERT INTO artifact(
                id, run_id, artifact_type, relative_path,
                content_hash, metadata_json, created_at
            ) VALUES (
                ?, ?, 'creation_instruction_activation_binding', NULL, ?, ?, ?
            )
            """,
            (
                binding.activation_binding_id,
                binding.run_id,
                binding.binding_hash,
                payload_json,
                binding.created_at.isoformat(),
            ),
        )
        return binding

    def _build_creation_instruction_activation_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        plan: WorkflowPlan,
        *,
        created_at: datetime,
    ) -> CreationInstructionActivationBinding:
        if (
            str(run["id"]) != plan.run_id
            or str(run["project_binding_id"]) != plan.project_id
        ):
            raise CreationInstructionConflict(
                "Creation instruction activation cannot cross Run or project scope",
                code="creation_instruction_activation_scope_mismatch",
            )
        project_scope_key = f"project:{plan.project_id}"
        project_row = connection.execute(
            """
            SELECT * FROM skill_activation
            WHERE scope_key = ? AND skill_id = ?
            """,
            (project_scope_key, CREATION_INSTRUCTION_SKILL_ID),
        ).fetchone()
        if project_row is None:
            project_scope = CreationInstructionActivationScope(
                scope_type="project",
                scope_key=project_scope_key,
                configured_state="unconfigured",
                state_revision=0,
                activation_receipt_id=None,
                activation_receipt_hash=None,
                configured_version_id=None,
            )
        else:
            project_scope = self._creation_instruction_scope_from_row(
                connection,
                project_row,
                expected_scope_type="project",
                expected_scope_key=project_scope_key,
                expected_project_id=plan.project_id,
            )

        if project_scope.configured_state == "disabled":
            raise CreationInstructionConflict(
                "Creation instruction source is disabled for this project",
                code="creation_instruction_activation_disabled",
            )
        if project_scope.configured_state == "enabled":
            scope_chain = (project_scope,)
            effective_scope: Literal["application", "project"] = "project"
            version_id = project_scope.configured_version_id
        else:
            application_row = connection.execute(
                """
                SELECT * FROM skill_activation
                WHERE scope_key = 'application' AND skill_id = ?
                """,
                (CREATION_INSTRUCTION_SKILL_ID,),
            ).fetchone()
            if application_row is None:
                raise CreationInstructionConflict(
                    "Creation instruction source is not enabled",
                    code="creation_instruction_activation_missing",
                )
            application_scope = self._creation_instruction_scope_from_row(
                connection,
                application_row,
                expected_scope_type="application",
                expected_scope_key="application",
                expected_project_id=None,
            )
            if application_scope.configured_state != "enabled":
                raise CreationInstructionConflict(
                    "Creation instruction source is not enabled",
                    code="creation_instruction_activation_disabled",
                )
            scope_chain = (project_scope, application_scope)
            effective_scope = "application"
            version_id = application_scope.configured_version_id
        if version_id is None:  # pragma: no cover - scope model enforces this.
            raise CreationInstructionConflict(
                "Creation instruction activation has no managed version",
                code="creation_instruction_activation_tampered",
            )

        version_row = connection.execute(
            """
            SELECT * FROM skill_version
            WHERE id = ? AND skill_id = ?
            """,
            (version_id, CREATION_INSTRUCTION_SKILL_ID),
        ).fetchone()
        if (
            version_row is None
            or str(version_row["snapshot_status"]) != "managed"
            or str(version_row["status"]) != "active"
        ):
            raise CreationInstructionConflict(
                "Creation instruction activation does not resolve to an active snapshot",
                code="creation_instruction_version_invalid",
            )
        manifest_path = Path(str(version_row["source_path"]))
        if not manifest_path.is_absolute() or manifest_path.name != MANIFEST_NAME:
            raise CreationInstructionConflict(
                "Creation instruction manifest path is invalid",
                code="creation_instruction_package_tampered",
            )
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = SkillManifest.model_validate_json(manifest_bytes, strict=True)
            package_root = manifest_path.parent.resolve(strict=True)
            package_fingerprint = self._managed_package_fingerprint(package_root)
            instruction_map_path = package_root / CREATION_INSTRUCTION_MAP_PATH
            instruction_map_bytes = instruction_map_path.read_bytes()
            instruction_map = parse_creation_instruction_map(instruction_map_bytes)
            source_path = package_root.joinpath(
                *Path(instruction_map.source_path).parts
            ).resolve(strict=True)
            source_path.relative_to(package_root)
            source_bytes = source_path.read_bytes()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CreationInstructionConflict(
                "Creation instruction managed package failed verification",
                code="creation_instruction_package_tampered",
            ) from exc
        manifest_fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
        instruction_map_hash = hashlib.sha256(instruction_map_bytes).hexdigest()
        if (
            manifest.id != CREATION_INSTRUCTION_SKILL_ID
            or manifest.kind != "instruction_source"
            or manifest.version != str(version_row["version"])
            or CREATION_INSTRUCTION_MAP_PATH not in manifest.resources
            or manifest_fingerprint != str(version_row["manifest_fingerprint"])
            or package_fingerprint != str(version_row["fingerprint"])
            or hashlib.sha256(source_bytes).hexdigest() != instruction_map.source_hash
            or instruction_map.skill_id != manifest.id
        ):
            raise CreationInstructionConflict(
                "Creation instruction snapshot identity or content changed",
                code="creation_instruction_package_tampered",
            )
        return build_creation_instruction_activation_binding(
            run_id=plan.run_id,
            project_id=plan.project_id,
            workflow_plan_id=plan.workflow_plan_id,
            workflow_plan_hash=plan.plan_hash,
            scope_chain=scope_chain,
            effective_scope=effective_scope,
            skill_version_id=version_id,
            skill_version=manifest.version,
            manifest_fingerprint=manifest_fingerprint,
            package_fingerprint=package_fingerprint,
            instruction_map_hash=instruction_map_hash,
            created_at=created_at,
        )

    def _creation_instruction_scope_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_scope_type: Literal["application", "project"],
        expected_scope_key: str,
        expected_project_id: str | None,
    ) -> CreationInstructionActivationScope:
        state = str(row["state"])
        revision = int(row["state_revision"])
        project_id = (
            None
            if row["project_binding_id"] is None
            else str(row["project_binding_id"])
        )
        if (
            str(row["scope_type"]) != expected_scope_type
            or str(row["scope_key"]) != expected_scope_key
            or project_id != expected_project_id
            or state not in {"enabled", "disabled", "inherited"}
            or revision < 1
        ):
            raise CreationInstructionConflict(
                "Creation instruction activation row is invalid",
                code="creation_instruction_activation_tampered",
            )
        receipt_row = connection.execute(
            """
            SELECT * FROM skill_activation_receipt
            WHERE scope_key = ? AND skill_id = ? AND state_revision = ?
            """,
            (expected_scope_key, CREATION_INSTRUCTION_SKILL_ID, revision),
        ).fetchone()
        if receipt_row is None:
            raise CreationInstructionConflict(
                "Creation instruction activation decision receipt is missing",
                code="creation_instruction_activation_receipt_missing",
            )
        try:
            payload = json.loads(str(receipt_row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("activation receipt payload must be an object")
            unhashed = dict(payload)
            persisted_hash = unhashed.pop("receipt_hash")
            receipt = ManagedSkillActivationReceipt.model_validate(payload, strict=False)
        except (KeyError, TypeError, ValueError) as exc:
            raise CreationInstructionConflict(
                "Creation instruction activation decision receipt is invalid",
                code="creation_instruction_activation_receipt_tampered",
            ) from exc
        active_version_id = (
            None if row["active_version_id"] is None else str(row["active_version_id"])
        )
        if (
            persisted_hash != _sha256_json(unhashed)
            or receipt.receipt_id != str(receipt_row["id"])
            or receipt.receipt_hash != str(receipt_row["receipt_hash"])
            or receipt.receipt_hash != persisted_hash
            or receipt.skill_id != CREATION_INSTRUCTION_SKILL_ID
            or receipt.scope.scope_type != expected_scope_type
            or receipt.scope.project_id != expected_project_id
            or receipt.next_state != state
            or receipt.next_version_id != active_version_id
            or receipt.state_revision != revision
            or receipt.created_at != _parse_time(row["updated_at"])
        ):
            raise CreationInstructionConflict(
                "Creation instruction activation decision receipt was tampered",
                code="creation_instruction_activation_receipt_tampered",
            )
        return CreationInstructionActivationScope(
            scope_type=expected_scope_type,
            scope_key=expected_scope_key,
            configured_state=state,
            state_revision=revision,
            activation_receipt_id=receipt.receipt_id,
            activation_receipt_hash=receipt.receipt_hash,
            configured_version_id=active_version_id,
        )

    @staticmethod
    def _managed_package_fingerprint(package_root: Path) -> str:
        hasher = hashlib.sha256(b"zhimeng-managed-skill-package-v1\0")
        files = sorted(
            (path for path in package_root.rglob("*") if path.is_file()),
            key=lambda path: (
                path.relative_to(package_root).as_posix().casefold(),
                path.relative_to(package_root).as_posix(),
            ),
        )
        if not files:
            raise ValueError("managed instruction package is empty")
        for path in files:
            if path.is_symlink():
                raise ValueError("managed instruction package contains a link")
            relative_path = path.relative_to(package_root).as_posix()
            content = path.read_bytes()
            hasher.update(
                _json(
                    {
                        "path": relative_path,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ).encode("utf-8")
            )
            hasher.update(b"\0")
        return hasher.hexdigest()

    @staticmethod
    def _creation_instruction_activation_from_artifact(
        row: sqlite3.Row,
    ) -> CreationInstructionActivationBinding:
        try:
            binding = CreationInstructionActivationBinding.model_validate_json(
                str(row["metadata_json"]),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise CreationInstructionConflict(
                "Stored Creation instruction activation binding is invalid",
                code="creation_instruction_activation_tampered",
            ) from exc
        if (
            str(row["id"]) != binding.activation_binding_id
            or str(row["run_id"]) != binding.run_id
            or str(row["artifact_type"])
            != "creation_instruction_activation_binding"
            or str(row["content_hash"]) != binding.binding_hash
            or binding.binding_hash
            != creation_instruction_activation_binding_hash(binding)
            or _parse_time(row["created_at"]) != binding.created_at
        ):
            raise CreationInstructionConflict(
                "Stored Creation instruction activation binding was tampered",
                code="creation_instruction_activation_tampered",
            )
        return binding

    def get_creation_instruction_activation_binding(
        self,
        run_id: str,
        *,
        require_current: bool = False,
    ) -> CreationInstructionActivationBinding:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_activation_binding'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentStoreNotFound(
                    "Creation instruction activation binding was not found"
                )
            if len(rows) != 1:
                raise CreationInstructionConflict(
                    "Run has multiple Creation instruction activation bindings",
                    code="creation_instruction_activation_duplicate",
                )
            binding = self._creation_instruction_activation_from_artifact(rows[0])
            if require_current:
                checkpoint_rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (run_id,),
                ).fetchall()
                if not checkpoint_rows:
                    raise CreationInstructionConflict(
                        "Creation instruction activation has no Workflow Plan",
                        code="creation_instruction_activation_tampered",
                    )
                plan, _ = self._workflow_chain_from_rows(
                    connection,
                    run,
                    checkpoint_rows,
                )
                current = self._build_creation_instruction_activation_in_connection(
                    connection,
                    run,
                    plan,
                    created_at=binding.created_at,
                )
                if current != binding:
                    raise CreationInstructionConflict(
                        "Creation instruction activation changed after Run creation",
                        code="creation_instruction_activation_stale",
                    )
        return binding

    def ensure_creation_instruction_materialization(
        self,
        run_id: str,
        *,
        created_at: datetime | None = None,
    ) -> CreationInstructionMaterialization:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise CreationInstructionConflict(
                        "New Creation instruction materializations require a running Run",
                        code="creation_instruction_run_not_running",
                    )
                checkpoint_rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (run_id,),
                ).fetchall()
                if not checkpoint_rows:
                    raise CreationInstructionConflict(
                        "Creation instruction materialization requires a Workflow Plan",
                        code="creation_instruction_workflow_missing",
                    )
                plan, chain = self._workflow_chain_from_rows(
                    connection,
                    run,
                    checkpoint_rows,
                )
                checkpoints = tuple(
                    item for item in chain if isinstance(item, WorkflowStepCheckpoint)
                )
                if (
                    len(checkpoints) != len(plan.steps)
                    or not checkpoints
                    or checkpoints[-1].execution_status != "succeeded"
                    or any(isinstance(item, ProviderIoBoundary) for item in chain)
                ):
                    raise CreationInstructionConflict(
                        "Creation instruction requires a completed pre-Provider workflow",
                        code="creation_instruction_workflow_incomplete",
                    )
                activation_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ?
                      AND artifact_type = 'creation_instruction_activation_binding'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if len(activation_rows) != 1:
                    raise CreationInstructionConflict(
                        "Creation instruction requires one frozen activation binding",
                        code="creation_instruction_activation_missing",
                    )
                binding = self._creation_instruction_activation_from_artifact(
                    activation_rows[0]
                )
                current_binding = self._build_creation_instruction_activation_in_connection(
                    connection,
                    run,
                    plan,
                    created_at=binding.created_at,
                )
                if current_binding != binding:
                    raise CreationInstructionConflict(
                        "Creation instruction activation changed before materialization",
                        code="creation_instruction_activation_stale",
                    )
                task_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_task'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if len(task_rows) != 1:
                    raise CreationInstructionConflict(
                        "Creation instruction requires exactly one Writer task",
                        code="creation_instruction_writer_task_missing",
                    )
                writer_task = self._writer_task_from_artifact(task_rows[0])
                version_row = connection.execute(
                    "SELECT source_path FROM skill_version WHERE id = ?",
                    (binding.skill_version_id,),
                ).fetchone()
                if version_row is None:
                    raise CreationInstructionConflict(
                        "Creation instruction managed version disappeared",
                        code="creation_instruction_version_invalid",
                    )
                try:
                    package_root = Path(str(version_row["source_path"])).parent.resolve(
                        strict=True
                    )
                    map_path = (package_root / binding.instruction_map_path).resolve(
                        strict=True
                    )
                    map_path.relative_to(package_root)
                    instruction_map_bytes = map_path.read_bytes()
                    instruction_map = parse_creation_instruction_map(
                        instruction_map_bytes
                    )
                    source_path = package_root.joinpath(
                        *Path(instruction_map.source_path).parts
                    ).resolve(strict=True)
                    source_path.relative_to(package_root)
                    source_bytes = source_path.read_bytes()
                except (OSError, RuntimeError, ValueError) as exc:
                    raise CreationInstructionConflict(
                        "Creation instruction managed resources cannot be read",
                        code="creation_instruction_package_tampered",
                    ) from exc
                existing_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ?
                      AND artifact_type = 'creation_instruction_materialization'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if len(existing_rows) > 1:
                    raise CreationInstructionConflict(
                        "Run has multiple Creation instruction materializations",
                        code="creation_instruction_materialization_duplicate",
                    )
                materialized_at = (
                    self._creation_instruction_materialization_from_artifact(
                        existing_rows[0]
                    ).created_at
                    if existing_rows
                    else created_at or _now()
                )
                try:
                    expected = build_creation_instruction_materialization(
                        binding,
                        writer_task,
                        instruction_map_bytes=instruction_map_bytes,
                        source_bytes=source_bytes,
                        created_at=materialized_at,
                    )
                except ValueError as exc:
                    raise CreationInstructionConflict(
                        "Creation instruction materialization failed immutable validation",
                        code="creation_instruction_materialization_invalid",
                    ) from exc
                if existing_rows:
                    stored = self._creation_instruction_materialization_from_artifact(
                        existing_rows[0]
                    )
                    if stored != expected:
                        raise CreationInstructionConflict(
                            "Stored Creation instruction no longer matches its inputs",
                            code="creation_instruction_materialization_stale",
                        )
                    connection.commit()
                    return stored

                payload_json = _json(expected.model_dump(mode="json", by_alias=True))
                connection.execute(
                    """
                    INSERT INTO artifact(
                        id, run_id, artifact_type, relative_path,
                        content_hash, metadata_json, created_at
                    ) VALUES (
                        ?, ?, 'creation_instruction_materialization', NULL, ?, ?, ?
                    )
                    """,
                    (
                        expected.materialization_id,
                        run_id,
                        expected.materialization_hash,
                        payload_json,
                        expected.created_at.isoformat(),
                    ),
                )
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.ARTIFACT,
                    payload=ArtifactPayload(
                        artifact_id=expected.materialization_id,
                        artifact_type="creation_instruction_materialization",
                        name="Creation instruction materialization",
                        uri=(
                            f"runtime://agent/runs/{run_id}/"
                            "creation-instruction-materialization"
                        ),
                        media_type="application/json",
                        content_hash=expected.materialization_hash,
                        size_bytes=len(payload_json.encode("utf-8")),
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=expected.created_at,
                )
                connection.execute(
                    """
                    UPDATE run SET last_activity_sequence = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (sequence, expected.created_at.isoformat(), run_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise CreationInstructionConflict(
                    "Run already owns a conflicting Creation instruction",
                    code="creation_instruction_materialization_duplicate",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return expected

    @staticmethod
    def _creation_instruction_materialization_from_artifact(
        row: sqlite3.Row,
    ) -> CreationInstructionMaterialization:
        try:
            materialization = CreationInstructionMaterialization.model_validate_json(
                str(row["metadata_json"]),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise CreationInstructionConflict(
                "Stored Creation instruction materialization is invalid",
                code="creation_instruction_materialization_tampered",
            ) from exc
        if (
            str(row["id"]) != materialization.materialization_id
            or str(row["run_id"]) != materialization.run_id
            or str(row["artifact_type"]) != "creation_instruction_materialization"
            or str(row["content_hash"]) != materialization.materialization_hash
            or materialization.materialization_hash
            != creation_instruction_materialization_hash(materialization)
            or _parse_time(row["created_at"]) != materialization.created_at
        ):
            raise CreationInstructionConflict(
                "Stored Creation instruction materialization was tampered",
                code="creation_instruction_materialization_tampered",
            )
        return materialization

    def get_creation_instruction_materialization(
        self,
        run_id: str,
    ) -> CreationInstructionMaterialization:
        with self.database.connection() as connection:
            self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_materialization'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        if not rows:
            raise AgentStoreNotFound(
                "Creation instruction materialization was not found"
            )
        if len(rows) != 1:
            raise CreationInstructionConflict(
                "Run has multiple Creation instruction materializations",
                code="creation_instruction_materialization_duplicate",
            )
        return self._creation_instruction_materialization_from_artifact(rows[0])

    def get_workflow_plan(self, run_id: str) -> WorkflowPlan:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentStoreNotFound("Workflow Plan was not found")
            plan, _ = self._workflow_chain_from_rows(connection, run, rows)
        return plan

    def list_workflow_checkpoints(
        self,
        run_id: str,
    ) -> list[WorkflowStepCheckpoint | ProviderIoBoundary]:
        """Return the validated post-plan chain in durable sequence order."""

        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentStoreNotFound("Workflow Plan was not found")
            _, checkpoints = self._workflow_chain_from_rows(connection, run, rows)
        return list(checkpoints)

    def build_workflow_step_checkpoint_from_execution(
        self,
        plan: WorkflowPlan,
        *,
        step_ordinal: int,
        provider_tool_call_id: str | None,
        previous_checkpoint: WorkflowStepCheckpoint | None,
        pre_dispatch_error: ErrorDetail | None = None,
    ) -> WorkflowStepCheckpoint:
        """Project one immutable checkpoint from already persisted execution evidence."""

        plan = self._validated_workflow_plan(plan)
        if isinstance(step_ordinal, bool) or not 1 <= step_ordinal <= len(plan.steps):
            raise AgentWorkflowCheckpointConflict(
                "Workflow step ordinal is outside the immutable Plan",
                code="workflow_checkpoint_sequence_conflict",
            )
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                run = self._require_run_row(connection, plan.run_id)
                rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (plan.run_id,),
                ).fetchall()
                if not rows:
                    raise AgentStoreNotFound("Workflow Plan was not found")
                stored_plan, chain = self._workflow_chain_from_rows(
                    connection,
                    run,
                    rows,
                )
                if (
                    stored_plan.workflow_plan_id != plan.workflow_plan_id
                    or stored_plan.plan_hash != plan.plan_hash
                ):
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow checkpoint factory received another Plan",
                        code="workflow_plan_conflict",
                    )
                if any(isinstance(item, ProviderIoBoundary) for item in chain):
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow is already closed for Provider I/O",
                        code="workflow_checkpoint_closed",
                    )
                prior = tuple(
                    item
                    for item in chain
                    if isinstance(item, WorkflowStepCheckpoint)
                )
                expected_previous = prior[-1] if prior else None
                if (
                    step_ordinal != len(prior) + 1
                    or previous_checkpoint != expected_previous
                ):
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow checkpoint factory predecessor is stale",
                        code="workflow_checkpoint_cas_failed",
                    )
                result = self._build_workflow_checkpoint_from_execution(
                    connection,
                    stored_plan,
                    step_ordinal=step_ordinal,
                    provider_tool_call_id=provider_tool_call_id,
                    previous_checkpoint=previous_checkpoint,
                    pre_dispatch_error=pre_dispatch_error,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result

    def append_workflow_step_checkpoint(
        self,
        checkpoint: WorkflowStepCheckpoint,
        *,
        expected_previous_checkpoint_hash: str | None,
    ) -> WorkflowStepCheckpoint:
        """CAS-append one immutable, terminal Skill-step checkpoint."""

        checkpoint = self._validated_workflow_step_checkpoint(checkpoint)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, checkpoint.run_id)
                rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (checkpoint.run_id,),
                ).fetchall()
                if not rows:
                    raise AgentStoreNotFound("Workflow Plan was not found")
                plan, chain = self._workflow_chain_from_rows(connection, run, rows)
                existing_row = connection.execute(
                    "SELECT * FROM checkpoint WHERE id = ?",
                    (checkpoint.checkpoint_id,),
                ).fetchone()
                if existing_row is not None:
                    if str(existing_row["run_id"]) != checkpoint.run_id:
                        raise AgentWorkflowCheckpointConflict(
                            "Workflow checkpoint identity belongs to another Run",
                            code="workflow_checkpoint_scope_mismatch",
                        )
                    if (
                        str(existing_row["checkpoint_type"])
                        != WORKFLOW_STEP_CHECKPOINT_TYPE
                    ):
                        raise AgentWorkflowCheckpointConflict(
                            "Workflow checkpoint identity has another type",
                            code="workflow_checkpoint_identity_conflict",
                        )
                    existing = self._workflow_step_checkpoint_from_row(existing_row)
                    if existing.checkpoint_hash != checkpoint.checkpoint_hash:
                        raise AgentWorkflowCheckpointConflict(
                            "Workflow checkpoint identity was reused with different material",
                            code="workflow_checkpoint_identity_conflict",
                        )
                    connection.commit()
                    return existing

                current_status = RunStatus(str(run["status"]))
                if current_status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                    raise AgentWorkflowCheckpointConflict(
                        "New workflow checkpoints require a running or recovery-paused Run",
                        code="workflow_checkpoint_run_not_running",
                    )
                self._validate_workflow_step_append(
                    connection,
                    plan,
                    chain,
                    checkpoint,
                    expected_previous_checkpoint_hash=expected_previous_checkpoint_hash,
                )
                sequence = int(rows[-1]["sequence"]) + 1
                connection.execute(
                    """
                    INSERT INTO checkpoint(
                        id, run_id, sequence, checkpoint_type, state_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.checkpoint_id,
                        checkpoint.run_id,
                        sequence,
                        WORKFLOW_STEP_CHECKPOINT_TYPE,
                        _json(checkpoint.model_dump(mode="json", by_alias=True)),
                        checkpoint.created_at.isoformat(),
                    ),
                )
                activity_created_at = _now()
                activity_sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=activity_sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=current_status,
                        previous_status=current_status,
                        detail=(
                            f"Workflow step {checkpoint.step_ordinal}/"
                            f"{len(plan.steps)} checkpoint persisted"
                        ),
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, checkpoint.run_id),
                    created_at=activity_created_at,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET last_activity_sequence = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        activity_sequence,
                        activity_created_at.isoformat(),
                        checkpoint.run_id,
                    ),
                )
                run = self._require_run_row(connection, checkpoint.run_id)
                stored_rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (checkpoint.run_id,),
                ).fetchall()
                _, stored_chain = self._workflow_chain_from_rows(
                    connection,
                    run,
                    stored_rows,
                )
                result = stored_chain[-1]
                if not isinstance(result, WorkflowStepCheckpoint):
                    raise RuntimeError("Workflow step transaction produced another record type")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentWorkflowCheckpointConflict(
                    "Workflow checkpoint identity or sequence already exists",
                    code="workflow_checkpoint_identity_conflict",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return result

    def create_provider_io_boundary(
        self,
        boundary: ProviderIoBoundary,
        *,
        expected_previous_checkpoint_hash: str,
        provider_messages_hash: str | None = None,
        tool_definitions_hash: str | None = None,
    ) -> ProviderIoBoundary:
        """Close a completed workflow chain immediately before Provider I/O."""

        if (provider_messages_hash is None) != (tool_definitions_hash is None):
            raise CreationInstructionConflict(
                "Provider dispatch hashes must be supplied together",
                code="creation_instruction_dispatch_invalid",
            )
        boundary = self._validated_provider_io_boundary(boundary)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, boundary.run_id)
                rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (boundary.run_id,),
                ).fetchall()
                if not rows:
                    raise AgentStoreNotFound("Workflow Plan was not found")
                plan, chain = self._workflow_chain_from_rows(connection, run, rows)
                if (
                    plan.steps
                    and plan.steps[-1].skill_id == CREATION_HANDOFF_SKILL_ID
                    and provider_messages_hash is None
                ):
                    raise CreationInstructionConflict(
                        "Creation Writer dispatch requires atomic instruction evidence",
                        code="creation_instruction_dispatch_receipt_required",
                    )

                existing_row = connection.execute(
                    "SELECT * FROM checkpoint WHERE id = ?",
                    (boundary.boundary_id,),
                ).fetchone()
                if existing_row is not None:
                    if str(existing_row["run_id"]) != boundary.run_id:
                        raise AgentWorkflowCheckpointConflict(
                            "Provider boundary identity belongs to another Run",
                            code="workflow_checkpoint_scope_mismatch",
                        )
                    if (
                        str(existing_row["checkpoint_type"])
                        != PROVIDER_IO_BOUNDARY_CHECKPOINT_TYPE
                    ):
                        raise AgentWorkflowCheckpointConflict(
                            "Provider boundary identity has another type",
                            code="workflow_checkpoint_identity_conflict",
                        )
                    existing = self._provider_io_boundary_from_row(existing_row)
                    if existing.boundary_hash != boundary.boundary_hash:
                        raise AgentWorkflowCheckpointConflict(
                            "Provider boundary identity was reused with different material",
                            code="workflow_checkpoint_identity_conflict",
                        )
                    if provider_messages_hash is not None:
                        receipt_rows = connection.execute(
                            """
                            SELECT * FROM artifact
                            WHERE run_id = ?
                              AND artifact_type = 'creation_instruction_dispatch_receipt'
                            ORDER BY id
                            """,
                            (boundary.run_id,),
                        ).fetchall()
                        if len(receipt_rows) != 1:
                            raise CreationInstructionConflict(
                                "Provider boundary has no atomic instruction dispatch receipt",
                                code="creation_instruction_dispatch_receipt_missing",
                            )
                        self._creation_instruction_dispatch_from_artifact(
                            receipt_rows[0]
                        )
                        raise CreationInstructionConflict(
                            "Provider dispatch was already committed and cannot be replayed",
                            code="creation_instruction_dispatch_already_committed",
                        )
                    connection.commit()
                    return existing

                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentWorkflowCheckpointConflict(
                        "New Provider I/O boundaries require a running Run",
                        code="workflow_checkpoint_run_not_running",
                    )
                self._validate_provider_boundary_append(
                    run,
                    plan,
                    chain,
                    boundary,
                    expected_previous_checkpoint_hash=expected_previous_checkpoint_hash,
                )
                dispatch_receipt = (
                    self._build_creation_instruction_dispatch_in_connection(
                        connection,
                        run,
                        plan,
                        boundary,
                        provider_messages_hash=provider_messages_hash,
                        tool_definitions_hash=tool_definitions_hash,
                    )
                    if provider_messages_hash is not None
                    and tool_definitions_hash is not None
                    else None
                )
                sequence = int(rows[-1]["sequence"]) + 1
                connection.execute(
                    """
                    INSERT INTO checkpoint(
                        id, run_id, sequence, checkpoint_type, state_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        boundary.boundary_id,
                        boundary.run_id,
                        sequence,
                        PROVIDER_IO_BOUNDARY_CHECKPOINT_TYPE,
                        _json(boundary.model_dump(mode="json", by_alias=True)),
                        boundary.created_at.isoformat(),
                    ),
                )
                if dispatch_receipt is not None:
                    dispatch_payload = _json(
                        dispatch_receipt.model_dump(mode="json", by_alias=True)
                    )
                    connection.execute(
                        """
                        INSERT INTO artifact(
                            id, run_id, artifact_type, relative_path,
                            content_hash, metadata_json, created_at
                        ) VALUES (
                            ?, ?, 'creation_instruction_dispatch_receipt',
                            NULL, ?, ?, ?
                        )
                        """,
                        (
                            dispatch_receipt.dispatch_receipt_id,
                            boundary.run_id,
                            dispatch_receipt.receipt_hash,
                            dispatch_payload,
                            dispatch_receipt.created_at.isoformat(),
                        ),
                    )
                activity_created_at = _now()
                activity_sequence = int(run["last_activity_sequence"]) + 1
                if dispatch_receipt is not None:
                    self._insert_activity(
                        connection,
                        run,
                        sequence=activity_sequence,
                        kind=ActivityKind.ARTIFACT,
                        payload=ArtifactPayload(
                            artifact_id=dispatch_receipt.dispatch_receipt_id,
                            artifact_type="creation_instruction_dispatch_receipt",
                            name="Creation instruction dispatch receipt",
                            uri=(
                                f"runtime://agent/runs/{boundary.run_id}/"
                                "creation-instruction-dispatch"
                            ),
                            media_type="application/json",
                            content_hash=dispatch_receipt.receipt_hash,
                            size_bytes=len(dispatch_payload.encode("utf-8")),
                        ).model_dump(mode="json"),
                        causation_id=self._last_activity_id(connection, boundary.run_id),
                        created_at=activity_created_at,
                    )
                    activity_sequence += 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=activity_sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=RunStatus.RUNNING,
                        previous_status=RunStatus.RUNNING,
                        detail=(
                            "Provider boundary and instruction dispatch receipt persisted"
                            if dispatch_receipt is not None
                            else "Provider dispatch boundary persisted"
                        ),
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, boundary.run_id),
                    created_at=activity_created_at,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET last_activity_sequence = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        activity_sequence,
                        activity_created_at.isoformat(),
                        boundary.run_id,
                    ),
                )
                run = self._require_run_row(connection, boundary.run_id)
                stored_rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (boundary.run_id,),
                ).fetchall()
                _, stored_chain = self._workflow_chain_from_rows(
                    connection,
                    run,
                    stored_rows,
                )
                result = stored_chain[-1]
                if not isinstance(result, ProviderIoBoundary):
                    raise RuntimeError("Provider boundary transaction produced another record type")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentWorkflowCheckpointConflict(
                    "Provider boundary identity or sequence already exists",
                    code="workflow_checkpoint_identity_conflict",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return result

    def get_provider_io_boundary(self, run_id: str) -> ProviderIoBoundary:
        checkpoints = self.list_workflow_checkpoints(run_id)
        boundaries = [
            checkpoint
            for checkpoint in checkpoints
            if isinstance(checkpoint, ProviderIoBoundary)
        ]
        if not boundaries:
            raise AgentStoreNotFound("Provider I/O boundary was not found")
        if len(boundaries) != 1:  # pragma: no cover - chain validator rejects this first.
            raise AgentWorkflowCheckpointConflict(
                "Run has multiple Provider I/O boundaries",
                code="workflow_checkpoint_integrity_failed",
            )
        return boundaries[0]

    def _build_creation_instruction_dispatch_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        plan: WorkflowPlan,
        boundary: ProviderIoBoundary,
        *,
        provider_messages_hash: str,
        tool_definitions_hash: str,
    ) -> CreationInstructionDispatchReceipt:
        materialization_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_materialization'
            ORDER BY id
            """,
            (boundary.run_id,),
        ).fetchall()
        if len(materialization_rows) != 1:
            raise CreationInstructionConflict(
                "Provider dispatch requires one Creation instruction materialization",
                code="creation_instruction_materialization_missing",
            )
        materialization = self._creation_instruction_materialization_from_artifact(
            materialization_rows[0]
        )
        activation_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_activation_binding'
            ORDER BY id
            """,
            (boundary.run_id,),
        ).fetchall()
        if len(activation_rows) != 1:
            raise CreationInstructionConflict(
                "Provider dispatch requires one frozen instruction activation",
                code="creation_instruction_activation_missing",
            )
        activation = self._creation_instruction_activation_from_artifact(
            activation_rows[0]
        )
        current_activation = self._build_creation_instruction_activation_in_connection(
            connection,
            run,
            plan,
            created_at=activation.created_at,
        )
        if current_activation != activation:
            raise CreationInstructionConflict(
                "Creation instruction activation changed before Provider dispatch",
                code="creation_instruction_activation_stale",
            )
        if (
            materialization.activation_binding_id
            != activation.activation_binding_id
            or materialization.activation_binding_hash != activation.binding_hash
            or materialization.workflow_plan_id != plan.workflow_plan_id
            or materialization.workflow_plan_hash != plan.plan_hash
        ):
            raise CreationInstructionConflict(
                "Creation instruction materialization does not bind current Run evidence",
                code="creation_instruction_materialization_stale",
            )
        existing_receipts = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_dispatch_receipt'
            ORDER BY id
            """,
            (boundary.run_id,),
        ).fetchall()
        if existing_receipts:
            raise CreationInstructionConflict(
                "Run already owns Creation instruction dispatch evidence",
                code="creation_instruction_dispatch_already_committed",
            )
        try:
            return build_creation_instruction_dispatch_receipt(
                boundary,
                materialization,
                provider_messages_hash=provider_messages_hash,
                tool_definitions_hash=tool_definitions_hash,
                created_at=boundary.created_at,
            )
        except ValueError as exc:
            raise CreationInstructionConflict(
                "Creation instruction dispatch receipt failed immutable validation",
                code="creation_instruction_dispatch_invalid",
            ) from exc

    @staticmethod
    def _creation_instruction_dispatch_from_artifact(
        row: sqlite3.Row,
    ) -> CreationInstructionDispatchReceipt:
        try:
            receipt = CreationInstructionDispatchReceipt.model_validate_json(
                str(row["metadata_json"]),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise CreationInstructionConflict(
                "Stored Creation instruction dispatch receipt is invalid",
                code="creation_instruction_dispatch_receipt_tampered",
            ) from exc
        if (
            str(row["id"]) != receipt.dispatch_receipt_id
            or str(row["run_id"]) != receipt.run_id
            or str(row["artifact_type"])
            != "creation_instruction_dispatch_receipt"
            or str(row["content_hash"]) != receipt.receipt_hash
            or receipt.receipt_hash
            != creation_instruction_dispatch_receipt_hash(receipt)
            or _parse_time(row["created_at"]) != receipt.created_at
        ):
            raise CreationInstructionConflict(
                "Stored Creation instruction dispatch receipt was tampered",
                code="creation_instruction_dispatch_receipt_tampered",
            )
        return receipt

    def get_creation_instruction_dispatch_receipt(
        self,
        run_id: str,
    ) -> CreationInstructionDispatchReceipt:
        with self.database.connection() as connection:
            self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_dispatch_receipt'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        if not rows:
            raise AgentStoreNotFound(
                "Creation instruction dispatch receipt was not found"
            )
        if len(rows) != 1:
            raise CreationInstructionConflict(
                "Run has multiple Creation instruction dispatch receipts",
                code="creation_instruction_dispatch_receipt_duplicate",
            )
        return self._creation_instruction_dispatch_from_artifact(rows[0])

    def create_provider_turn_receipt(
        self,
        receipt: ProviderTurnReceipt,
    ) -> ProviderTurnReceipt:
        """Append one observed Provider turn before any response ToolCall executes."""

        receipt = self._validated_provider_turn_receipt(receipt)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, receipt.run_id)
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise ProviderTurnReceiptConflict(
                        "New Provider turn receipts require a running Run",
                        code="provider_turn_receipt_run_not_running",
                    )
                boundary = self._provider_boundary_for_run(connection, run)
                existing = self._provider_turn_receipts_from_connection(
                    connection,
                    run,
                    boundary,
                )
                if receipt.turn_ordinal <= len(existing):
                    current = existing[receipt.turn_ordinal - 1]
                    if (
                        current.receipt_id == receipt.receipt_id
                        and current.receipt_hash == receipt.receipt_hash
                    ):
                        connection.commit()
                        return current
                    raise ProviderTurnReceiptConflict(
                        "Provider turn ordinal already owns different immutable evidence",
                        code="provider_turn_receipt_duplicate",
                    )
                if receipt.turn_ordinal != len(existing) + 1:
                    raise ProviderTurnReceiptConflict(
                        "Provider turn receipts must be appended in contiguous order",
                        code="provider_turn_receipt_order_invalid",
                    )
                self._validate_provider_turn_receipt_binding(
                    receipt,
                    boundary=boundary,
                    previous=existing,
                )
                sequence_row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_event WHERE run_id = ?",
                    (receipt.run_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO run_event(
                        run_id, sequence, event_type, activity_id, payload_json, created_at
                    ) VALUES (?, ?, 'provider_turn_receipt', NULL, ?, ?)
                    """,
                    (
                        receipt.run_id,
                        int(sequence_row[0]),
                        _json(receipt.model_dump(mode="json", by_alias=True)),
                        receipt.created_at.isoformat(),
                    ),
                )
                stored = self._provider_turn_receipts_from_connection(
                    connection,
                    run,
                    boundary,
                )
                result = stored[-1]
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ProviderTurnReceiptConflict(
                    "Provider turn receipt identity or sequence already exists",
                    code="provider_turn_receipt_duplicate",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return result

    def list_provider_turn_receipts(
        self,
        run_id: str,
    ) -> tuple[ProviderTurnReceipt, ...]:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            boundary = self._provider_boundary_for_run(connection, run)
            return self._provider_turn_receipts_from_connection(
                connection,
                run,
                boundary,
            )

    def get_provider_turn_receipt_list(
        self,
        run_id: str,
    ) -> ProviderTurnReceiptListResponse:
        """Return one atomically verified view of all Provider turn evidence."""

        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            try:
                boundary = self._provider_boundary_for_run(connection, run)
            except ProviderTurnReceiptConflict as exc:
                if exc.code != "provider_turn_receipt_boundary_missing":
                    raise
                raise AgentStoreNotFound(
                    "Provider turn receipt evidence was not found"
                ) from exc
            receipts = self._provider_turn_receipts_from_connection(
                connection,
                run,
                boundary,
            )
        return build_provider_turn_receipt_list_response(boundary, receipts)

    def ensure_writer_task(
        self,
        run_id: str,
        *,
        target_files: tuple[WriterTargetFile, ...],
        created_at: datetime | None = None,
    ) -> WriterTask:
        """Persist one immutable Writer task before Provider dispatch."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentStoreInvalidState(
                        "New Writer tasks require a running Run"
                    )
                plan, checkpoints, handoff = self._writer_workflow_sources(
                    connection,
                    run,
                    require_provider_boundary=False,
                )
                pack, route, operation_package = self._writer_context_sources(
                    connection,
                    plan,
                )
                mode, intent_chapter_id = self._writer_intent_from_connection(
                    connection,
                    run_id,
                )
                if (
                    intent_chapter_id is not None
                    and intent_chapter_id != operation_package.active_chapter_id
                ):
                    raise AgentStoreInvalidState(
                        "Writer intent active chapter does not match the operation package"
                    )
                existing_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_task'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if len(existing_rows) > 1:
                    raise AgentStoreInvalidState(
                        "Run must have exactly one Writer task"
                    )
                task_created_at = (
                    self._writer_task_from_artifact(existing_rows[0]).created_at
                    if existing_rows
                    else created_at or _now()
                )
                expected = build_writer_task(
                    plan,
                    checkpoints,
                    handoff=handoff,
                    context_pack=pack,
                    route=route,
                    operation_package=operation_package,
                    mode=mode,
                    target_files=target_files,
                    created_at=task_created_at,
                )
                if existing_rows:
                    stored = self._writer_task_from_artifact(existing_rows[0])
                    if stored != expected:
                        raise AgentStoreInvalidState(
                            "Stored Writer task does not match its Runtime inputs"
                        )
                    connection.commit()
                    return stored

                payload_json = _json(
                    expected.model_dump(mode="json", by_alias=True)
                )
                now = expected.created_at
                connection.execute(
                    """
                    INSERT INTO artifact(
                        id, run_id, artifact_type, relative_path,
                        content_hash, metadata_json, created_at
                    ) VALUES (?, ?, 'writer_task', NULL, ?, ?, ?)
                    """,
                    (
                        expected.writer_task_id,
                        run_id,
                        expected.task_hash,
                        payload_json,
                        now.isoformat(),
                    ),
                )
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.ARTIFACT,
                    payload=ArtifactPayload(
                        artifact_id=expected.writer_task_id,
                        artifact_type="writer_task",
                        name="Runtime Writer task",
                        uri=f"runtime://agent/runs/{run_id}/writer-task",
                        media_type="application/json",
                        content_hash=expected.task_hash,
                        size_bytes=len(payload_json.encode("utf-8")),
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence, now.isoformat(), run_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentStoreConflict(
                    "Run already owns a conflicting Writer task"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return expected

    def get_writer_task(self, run_id: str) -> WriterTask:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_task'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentStoreNotFound("Writer task was not found")
            if len(rows) != 1:
                raise AgentStoreInvalidState("Run must have exactly one Writer task")
            task = self._writer_task_from_artifact(rows[0])
            plan, checkpoints, handoff = self._writer_workflow_sources(
                connection,
                run,
                require_provider_boundary=None,
            )
            pack, route, operation_package = self._writer_context_sources(
                connection,
                plan,
            )
            expected = build_writer_task(
                plan,
                checkpoints,
                handoff=handoff,
                context_pack=pack,
                route=route,
                operation_package=operation_package,
                mode=task.mode,
                target_files=task.target_files,
                created_at=task.created_at,
            )
            if expected != task:
                raise AgentStoreInvalidState(
                    "Stored Writer task does not match its Runtime inputs"
                )
        return task

    def build_writer_candidate_result(
        self,
        candidate: AgentCandidateOutput,
        *,
        created_at: datetime | None = None,
    ) -> WriterCandidateResult:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, candidate.run_id)
            return self._build_writer_candidate_result_in_connection(
                connection,
                run,
                candidate,
                created_at=created_at or _now(),
            )

    def _build_writer_candidate_result_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        candidate: AgentCandidateOutput,
        *,
        created_at: datetime,
    ) -> WriterCandidateResult:
        task_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ? AND artifact_type = 'writer_task'
            ORDER BY id
            """,
            (candidate.run_id,),
        ).fetchall()
        if len(task_rows) != 1:
            raise AgentStoreInvalidState(
                "Writer candidate requires exactly one Runtime Writer task"
            )
        task = self._writer_task_from_artifact(task_rows[0])
        change_sets = self._writer_change_sets_from_connection(
            connection,
            candidate.run_id,
        )
        if task.mode == "author_review_change_proposal":
            if len(change_sets) != 1:
                raise AgentStoreInvalidState(
                    "Writer candidate requires exactly one same-Run ChangeSet"
                )
            change_set = change_sets[0]
        else:
            if change_sets:
                raise AgentStoreInvalidState(
                    "Repair-only Writer candidate cannot own a ChangeSet"
                )
            change_set = None
        self._validate_agent_candidate_output_binding(
            connection,
            run,
            candidate,
        )
        try:
            return build_writer_candidate_result(
                task,
                candidate,
                change_set=change_set,
                created_at=created_at,
            )
        except ValueError as exc:
            raise AgentStoreInvalidState(
                "Writer candidate result failed immutable validation"
            ) from exc

    def get_writer_candidate_result(self, run_id: str) -> WriterCandidateResult:
        with self.database.connection() as connection:
            self._require_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        if not rows:
            raise AgentStoreNotFound("Writer candidate result was not found")
        if len(rows) != 1:
            raise AgentStoreInvalidState(
                "Run must have exactly one Writer candidate result"
            )
        return self._writer_candidate_result_from_artifact(rows[0])

    def build_agent_candidate_output(
        self,
        run_id: str,
        submission: AgentCandidateSubmission,
        *,
        created_at: datetime | None = None,
    ) -> AgentCandidateOutput:
        """Bind Provider-owned content to the persisted Runtime workflow."""

        try:
            submission = AgentCandidateSubmission.model_validate_json(
                _json(submission.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentCandidateOutputConflict(
                "Agent candidate submission is invalid",
                code="agent_candidate_output_invalid",
            ) from exc
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentCandidateOutputConflict(
                    "Agent candidate output requires a managed Workflow Plan",
                    code="agent_candidate_output_workflow_missing",
                )
            plan, chain = self._workflow_chain_from_rows(connection, run, rows)
            step_checkpoints = tuple(
                item for item in chain if isinstance(item, WorkflowStepCheckpoint)
            )
            if (
                len(step_checkpoints) != len(plan.steps)
                or not step_checkpoints
                or not isinstance(chain[-1], ProviderIoBoundary)
            ):
                raise AgentCandidateOutputConflict(
                    "Agent candidate output requires one completed Provider boundary",
                    code="agent_candidate_output_boundary_missing",
                )
            final_checkpoint = step_checkpoints[-1]
            boundary = chain[-1]
            handoff = self._creation_handoff_from_checkpoint(
                connection,
                final_checkpoint,
            )
            task_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_task'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            if len(task_rows) != 1:
                raise AgentCandidateOutputConflict(
                    "Agent candidate output requires exactly one Runtime Writer task",
                    code="agent_candidate_output_writer_task_missing",
                )
            writer_task = self._writer_task_from_artifact(task_rows[0])
            materialization_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_materialization'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            if len(materialization_rows) != 1:
                raise AgentCandidateOutputConflict(
                    "Agent candidate output requires one instruction materialization",
                    code="agent_candidate_output_materialization_missing",
                )
            instruction_materialization = (
                self._creation_instruction_materialization_from_artifact(
                    materialization_rows[0]
                )
            )
            dispatch_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_dispatch_receipt'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            if len(dispatch_rows) != 1:
                raise AgentCandidateOutputConflict(
                    "Agent candidate output requires one instruction dispatch receipt",
                    code="agent_candidate_output_dispatch_receipt_missing",
                )
            instruction_dispatch_receipt = (
                self._creation_instruction_dispatch_from_artifact(dispatch_rows[0])
            )
            change_sets = self._writer_change_sets_from_connection(connection, run_id)
            if writer_task.mode == "author_review_change_proposal":
                if len(change_sets) != 1:
                    raise AgentCandidateOutputConflict(
                        "Writer candidate requires exactly one same-Run ChangeSet",
                        code="agent_candidate_output_change_set_missing",
                    )
                change_set = change_sets[0]
            else:
                if change_sets:
                    raise AgentCandidateOutputConflict(
                        "Repair-only Writer candidate cannot own a ChangeSet",
                        code="agent_candidate_output_change_set_forbidden",
                    )
                change_set = None
            provider_turn_receipts = self._provider_turn_receipts_from_connection(
                connection,
                run,
                boundary,
            )
        return build_candidate_output_envelope(
            plan,
            step_checkpoints,
            boundary,
            handoff=handoff,
            writer_task=writer_task,
            instruction_materialization=instruction_materialization,
            instruction_dispatch_receipt=instruction_dispatch_receipt,
            provider_turn_receipts=provider_turn_receipts,
            change_set=change_set,
            submission=submission,
            created_at=created_at or _now(),
        )

    def create_agent_candidate_output(
        self,
        candidate: AgentCandidateOutput,
    ) -> AgentCandidateOutput:
        """Persist one immutable candidate or replay equivalent content."""

        candidate = self._validated_agent_candidate_output(candidate)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, candidate.run_id)
                result = self._create_agent_candidate_output_in_connection(
                    connection,
                    run,
                    candidate,
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentCandidateOutputConflict(
                    "Run already owns a different Agent candidate output",
                    code="agent_candidate_output_conflict",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return result

    def persist_candidate_recovery_cutpoint(
        self,
        candidate: AgentCandidateOutput,
        *,
        candidate_tool_call_id: str,
        candidate_tool_output: Any,
        candidate_tool_duration_ms: int | None,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
    ) -> AgentCandidateOutput:
        """Commit Provider submission evidence without marking the Run successful."""

        candidate = self._validated_agent_candidate_output(candidate)
        result_payload = ToolResultPayload(
            tool_call_id=candidate_tool_call_id,
            status="succeeded",
            output=candidate_tool_output,
            error=None,
            duration_ms=candidate_tool_duration_ms,
        )
        result_hash = self._tool_result_hash(result_payload, None)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, candidate.run_id)
                if current_project_kb_verifier is not None:
                    context_pack = self._require_run_project_kb_binding(connection, run)
                    current_project_kb_verifier(
                        self._knowledge_target_from_row(context_pack),
                        connection,
                    )
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentCandidateOutputConflict(
                        "Candidate recovery cutpoints require a running Run",
                        code="agent_candidate_output_run_not_running",
                    )
                existing_row = connection.execute(
                    "SELECT * FROM agent_candidate_output WHERE run_id = ?",
                    (candidate.run_id,),
                ).fetchone()
                tool_call = self._candidate_submission_tool_call_in_connection(
                    connection,
                    candidate,
                    candidate_tool_call_id=candidate_tool_call_id,
                )
                current_status = str(tool_call["status"])
                if current_status == "succeeded":
                    if existing_row is None or str(tool_call["result_hash"] or "") != result_hash:
                        raise AgentCandidateOutputConflict(
                            "Candidate submission evidence is not an atomic recovery cutpoint",
                            code="agent_candidate_output_submit_mismatch",
                        )
                    stored = self._create_agent_candidate_output_in_connection(
                        connection,
                        run,
                        candidate,
                    )
                    connection.commit()
                    return stored
                if current_status != "running":
                    raise AgentCandidateOutputConflict(
                        "Candidate submission ToolCall is not pending completion",
                        code="agent_candidate_output_submit_invalid",
                    )
                writer_results = connection.execute(
                    """
                    SELECT id FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
                    """,
                    (candidate.run_id,),
                ).fetchall()
                if writer_results:
                    raise AgentCandidateOutputConflict(
                        "Candidate recovery cutpoint cannot follow a Writer result",
                        code="agent_candidate_output_result_conflict",
                    )
                stored = self._create_agent_candidate_output_in_connection(
                    connection,
                    run,
                    candidate,
                )
                self._finish_tool_call_in_transaction(
                    connection,
                    run,
                    tool_call,
                    result_payload=result_payload,
                    result_hash=result_hash,
                    change_proposal=None,
                    created_at=_now(),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise AgentCandidateOutputConflict(
                    "Candidate recovery cutpoint conflicts with persisted evidence",
                    code="agent_candidate_output_conflict",
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return stored

    def _candidate_submission_tool_call_in_connection(
        self,
        connection: sqlite3.Connection,
        candidate: AgentCandidateOutput,
        *,
        candidate_tool_call_id: str | None = None,
        require_succeeded: bool = False,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT * FROM tool_call
            WHERE run_id = ? AND tool_name = ?
            ORDER BY started_at, id
            """,
            (candidate.run_id, CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME),
        ).fetchall()
        if len(rows) != 1:
            raise AgentCandidateOutputConflict(
                "Recoverable candidates require exactly one submission ToolCall",
                code="agent_candidate_output_submit_missing",
            )
        tool_call = rows[0]
        if candidate_tool_call_id is not None and str(tool_call["id"]) != candidate_tool_call_id:
            raise AgentCandidateOutputConflict(
                "Candidate submission ToolCall identity does not match",
                code="agent_candidate_output_submit_mismatch",
            )
        if str(tool_call["tool_version"]) != "2":
            raise AgentCandidateOutputConflict(
                "Recoverable candidates require candidate_output_submit version 2",
                code="agent_candidate_output_submit_version_invalid",
            )
        try:
            submission = AgentCandidateSubmission.model_validate_json(
                str(tool_call["input_json"]),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise AgentCandidateOutputConflict(
                "Candidate submission arguments failed immutable validation",
                code="agent_candidate_output_submit_invalid",
            ) from exc
        if submission.kind != candidate.kind or submission.content != candidate.content:
            raise AgentCandidateOutputConflict(
                "Candidate output does not match its submitted content",
                code="agent_candidate_output_submit_mismatch",
            )
        if require_succeeded:
            try:
                stored_call = self._tool_call_from_row(tool_call)
                result_payload = ToolResultPayload(
                    tool_call_id=stored_call.tool_call_id,
                    status="succeeded",
                    output=stored_call.output,
                    error=None,
                    duration_ms=stored_call.duration_ms,
                )
            except (TypeError, ValueError) as exc:
                raise AgentCandidateOutputConflict(
                    "Candidate submission result failed immutable validation",
                    code="agent_candidate_output_submit_invalid",
                ) from exc
            if (
                stored_call.status != "succeeded"
                or stored_call.error is not None
                or stored_call.result_hash
                != self._tool_result_hash(result_payload, None)
            ):
                raise AgentCandidateOutputConflict(
                    "Candidate submission has no valid succeeded result",
                    code="agent_candidate_output_submit_invalid",
                )
        return tool_call

    def get_agent_candidate_output(self, run_id: str) -> AgentCandidateOutputRecord:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            row = connection.execute(
                "SELECT * FROM agent_candidate_output WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgentStoreNotFound("Agent candidate output was not found")
            candidate = self._agent_candidate_output_from_row(row)
            self._validate_agent_candidate_output_binding(
                connection,
                run,
                candidate,
            )
        return candidate

    # These aliases keep the persistence primitive discoverable to callers that
    # describe the envelope as a generic candidate rather than an Agent output.
    def create_candidate_output(
        self,
        candidate: AgentCandidateOutput,
    ) -> AgentCandidateOutput:
        return self.create_agent_candidate_output(candidate)

    def get_candidate_output(self, run_id: str) -> AgentCandidateOutputRecord:
        return self.get_agent_candidate_output(run_id)

    def _recoverable_candidate_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> tuple[Literal["persisted", "absent", "invalid"], AgentCandidateOutput | None]:
        run_id = str(run["id"])
        candidate_row = connection.execute(
            "SELECT * FROM agent_candidate_output WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if candidate_row is None:
            return "absent", None
        try:
            candidate = self._agent_candidate_output_from_row(candidate_row)
            self._validate_agent_candidate_output_binding(connection, run, candidate)
            if not isinstance(candidate, AgentCandidateOutput):
                raise AgentCandidateOutputConflict(
                    "Legacy candidate records are read-only recovery evidence",
                    code="agent_candidate_output_legacy_read_only",
                )
            self._candidate_submission_tool_call_in_connection(
                connection,
                candidate,
                require_succeeded=True,
            )
        except (AgentStoreError, TypeError, ValueError):
            return "invalid", None
        return "persisted", candidate

    def _create_agent_candidate_output_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        candidate: AgentCandidateOutput,
    ) -> AgentCandidateOutput:
        """Insert a candidate inside a caller-owned Run transaction."""

        existing_row = connection.execute(
            "SELECT * FROM agent_candidate_output WHERE run_id = ?",
            (candidate.run_id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._agent_candidate_output_from_row(existing_row)
            self._validate_agent_candidate_output_binding(
                connection,
                run,
                existing,
            )
            if agent_candidate_output_idempotency_hash(
                existing
            ) != agent_candidate_output_idempotency_hash(candidate):
                raise AgentCandidateOutputConflict(
                    "Run already owns a different Agent candidate output",
                    code="agent_candidate_output_conflict",
                )
            return existing

        if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
            raise AgentCandidateOutputConflict(
                "New Agent candidate outputs require a running Run",
                code="agent_candidate_output_run_not_running",
            )
        self._validate_agent_candidate_output_binding(
            connection,
            run,
            candidate,
        )
        conflicting_id = connection.execute(
            "SELECT run_id FROM agent_candidate_output WHERE id = ?",
            (candidate.candidate_output_id,),
        ).fetchone()
        if conflicting_id is not None:
            raise AgentCandidateOutputConflict(
                "Agent candidate identity belongs to another Run",
                code="agent_candidate_output_scope_mismatch",
            )
        connection.execute(
            """
            INSERT INTO agent_candidate_output(
                id, run_id, workflow_plan_id, workflow_plan_hash,
                final_checkpoint_id, final_checkpoint_hash,
                provider_boundary_id, provider_boundary_hash,
                provider_request_hash, context_pack_id, context_pack_hash,
                operation_package_id, operation_package_hash,
                project_binding_id, project_revision, project_fingerprint,
                handoff_status, allowed_output, failure_signatures_json,
                formal_draft_allowed, requires_author_review,
                kind, content_hash, references_hash, envelope_hash,
                payload_json, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                candidate.candidate_output_id,
                candidate.run_id,
                candidate.workflow_plan_id,
                candidate.workflow_plan_hash,
                candidate.final_checkpoint_id,
                candidate.final_checkpoint_hash,
                candidate.provider_boundary_id,
                candidate.provider_boundary_hash,
                candidate.provider_request_hash,
                candidate.context_pack_id,
                candidate.context_pack_hash,
                candidate.operation_package_id,
                candidate.operation_package_hash,
                candidate.project_id,
                candidate.project_revision,
                candidate.project_fingerprint,
                candidate.handoff_status,
                candidate.allowed_output,
                _json(list(candidate.failure_signatures)),
                0,
                1,
                candidate.kind,
                candidate.content_hash,
                candidate.references_hash,
                candidate.envelope_hash,
                _json(candidate.model_dump(mode="json", by_alias=True)),
                candidate.created_at.isoformat(),
            ),
        )
        stored = connection.execute(
            "SELECT * FROM agent_candidate_output WHERE run_id = ?",
            (candidate.run_id,),
        ).fetchone()
        if stored is None:  # pragma: no cover - insert invariant.
            raise RuntimeError("Agent candidate transaction produced no record")
        result = self._agent_candidate_output_from_row(stored)
        self._validate_agent_candidate_output_binding(connection, run, result)
        return result

    def ensure_provider_recovery_dossier(
        self,
        run_id: str,
    ) -> ProviderRecoveryDossier:
        """Materialize one immutable dossier for a Run after Provider dispatch."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                dossier = self._ensure_provider_recovery_dossier_in_connection(
                    connection,
                    run,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return dossier

    def get_provider_recovery(self, run_id: str) -> ProviderRecoveryResponse:
        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            dossier = self._read_provider_recovery_dossier(connection, run_id)
            if dossier is None:
                raise AgentStoreNotFound("Provider recovery dossier was not found")
            decision = self._read_provider_recovery_decision(connection, run_id)
        try:
            return ProviderRecoveryResponse(dossier=dossier, decision=decision)
        except ValueError as exc:
            raise ProviderRecoveryConflict(
                "Provider recovery decision does not match its dossier",
                code="provider_recovery_integrity_failed",
            ) from exc

    def decide_provider_recovery(
        self,
        run_id: str,
        request: ProviderRecoveryDecisionRequest,
        *,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
    ) -> ProviderRecoveryResponse:
        try:
            request = ProviderRecoveryDecisionRequest.model_validate_json(
                _json(request.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise ProviderRecoveryConflict(
                "Provider recovery decision request is invalid",
                code="provider_recovery_invalid",
            ) from exc

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                dossier = self._ensure_provider_recovery_dossier_in_connection(
                    connection,
                    run,
                )
                existing_decision = self._read_provider_recovery_decision(
                    connection,
                    run_id,
                )
                if existing_decision is not None:
                    raise ProviderRecoveryConflict(
                        "Provider recovery dossier already has a terminal decision",
                        code="provider_recovery_already_decided",
                    )
                if (
                    request.expected_dossier_revision != dossier.dossier_revision
                    or request.expected_dossier_hash != dossier.dossier_hash
                ):
                    raise ProviderRecoveryConflict(
                        "Provider recovery dossier changed before the author decision",
                        code="provider_recovery_stale",
                    )

                if request.action == "adopt_persisted_candidate":
                    context_pack = self._require_run_project_kb_binding(connection, run)
                    if dossier.candidate_state != "persisted":
                        raise ProviderRecoveryConflict(
                            "No valid persisted candidate is available to adopt",
                            code="provider_recovery_candidate_missing",
                        )
                    candidate_state, candidate = self._recoverable_candidate_in_connection(
                        connection,
                        run,
                    )
                    if candidate_state != "persisted" or candidate is None:
                        raise ProviderRecoveryConflict(
                            "Persisted candidate evidence changed before recovery",
                            code="provider_recovery_candidate_missing",
                        )
                    if (
                        candidate.candidate_output_id != dossier.candidate_output_id
                        or candidate.envelope_hash != dossier.candidate_output_hash
                    ):
                        raise ProviderRecoveryConflict(
                            "Persisted candidate identity changed after dossier capture",
                            code="provider_recovery_candidate_changed",
                        )
                    result_rows = connection.execute(
                        """
                        SELECT * FROM artifact
                        WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
                        ORDER BY id
                        """,
                        (run_id,),
                    ).fetchall()
                    if len(result_rows) > 1:
                        raise ProviderRecoveryConflict(
                            "Persisted candidate has duplicate Writer results",
                            code="provider_recovery_writer_result_duplicate",
                        )
                    result_created_at = (
                        self._writer_candidate_result_from_artifact(
                            result_rows[0]
                        ).created_at
                        if result_rows
                        else _now()
                    )
                    try:
                        writer_result = self._build_writer_candidate_result_in_connection(
                            connection,
                            run,
                            candidate,
                            created_at=result_created_at,
                        )
                    except AgentStoreError as exc:
                        raise ProviderRecoveryConflict(
                            "Persisted candidate cannot form a Writer result",
                            code="provider_recovery_writer_result_invalid",
                        ) from exc
                    if current_project_kb_verifier is None:
                        raise AgentContextPackConflict(
                            "Project KB verification is required for candidate adoption",
                            code="project_kb_unavailable",
                        )
                    current_project_kb_verifier(
                        self._knowledge_target_from_row(context_pack),
                        connection,
                    )
                    self._create_writer_candidate_result_in_connection(
                        connection,
                        run,
                        candidate,
                        writer_result,
                    )
                    run = self._require_run_row(connection, run_id)
                    assistant_content = self._candidate_assistant_content(candidate)
                    result, _ = self._complete_run_in_connection(
                        connection,
                        run_id,
                        message_id=f"recovery-message-{uuid4().hex}",
                        content=assistant_content,
                        finish_reason="stop",
                        usage=None,
                        allow_recovery=True,
                    )
                    resulting_status = RunStatus.SUCCEEDED
                else:
                    result = self._abandon_provider_recovery_in_connection(
                        connection,
                        run,
                    )
                    resulting_status = RunStatus(str(result["status"]))

                now = _now()
                decision_material = {
                    "schema_id": "zhimeng.provider-recovery-decision.v1",
                    "decision_id": provider_recovery_decision_id(
                        dossier_id=dossier.dossier_id,
                        previous_dossier_hash=dossier.dossier_hash,
                        instruction_dispatch_receipt_id=(
                            dossier.instruction_dispatch_receipt_id
                        ),
                        instruction_dispatch_receipt_hash=(
                            dossier.instruction_dispatch_receipt_hash
                        ),
                        action=request.action,
                        previous_dossier_revision=dossier.dossier_revision,
                    ),
                    "dossier_id": dossier.dossier_id,
                    "run_id": run_id,
                    "previous_dossier_hash": dossier.dossier_hash,
                    "instruction_dispatch_receipt_id": (
                        dossier.instruction_dispatch_receipt_id
                    ),
                    "instruction_dispatch_receipt_hash": (
                        dossier.instruction_dispatch_receipt_hash
                    ),
                    "action": request.action,
                    "previous_status": "pending",
                    "resulting_run_status": resulting_status,
                    "previous_dossier_revision": dossier.dossier_revision,
                    "new_dossier_revision": dossier.dossier_revision + 1,
                    "created_at": now,
                }
                draft = ProviderRecoveryDecision.model_construct(
                    **decision_material,
                    decision_hash="0" * 64,
                )
                decision = ProviderRecoveryDecision(
                    **decision_material,
                    decision_hash=provider_recovery_decision_hash(draft),
                )
                self._insert_provider_recovery_event(
                    connection,
                    run_id,
                    event_type="provider_recovery_decision",
                    payload=decision.model_dump(mode="json", by_alias=True),
                    created_at=now,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return ProviderRecoveryResponse(dossier=dossier, decision=decision)

    def _ensure_provider_recovery_dossier_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> ProviderRecoveryDossier:
        run_id = str(run["id"])
        existing = self._read_provider_recovery_dossier(connection, run_id)
        if existing is not None:
            return existing
        rows = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        ).fetchall()
        if not rows:
            raise AgentStoreNotFound("Provider recovery requires a Workflow Plan")
        plan, chain = self._workflow_chain_from_rows(connection, run, rows)
        boundaries = tuple(item for item in chain if isinstance(item, ProviderIoBoundary))
        checkpoints = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )
        if len(boundaries) != 1 or not checkpoints or not isinstance(chain[-1], ProviderIoBoundary):
            raise ProviderRecoveryConflict(
                "Provider recovery requires exactly one persisted boundary",
                code="provider_recovery_boundary_missing",
            )
        final_checkpoint = checkpoints[-1]
        boundary = boundaries[0]
        dispatch_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_dispatch_receipt'
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        if len(dispatch_rows) != 1:
            raise ProviderRecoveryConflict(
                "Provider recovery requires one immutable instruction dispatch receipt",
                code="provider_recovery_dispatch_receipt_missing",
            )
        dispatch_receipt = self._creation_instruction_dispatch_from_artifact(
            dispatch_rows[0]
        )
        if (
            dispatch_receipt.provider_boundary_id != boundary.boundary_id
            or dispatch_receipt.provider_boundary_hash != boundary.boundary_hash
            or dispatch_receipt.provider_request_hash != boundary.request_hash
            or dispatch_receipt.automatic_provider_replay_count != 0
        ):
            raise ProviderRecoveryConflict(
                "Provider recovery dispatch receipt does not bind its boundary",
                code="provider_recovery_dispatch_receipt_mismatch",
            )
        candidate_state, candidate = self._recoverable_candidate_in_connection(
            connection,
            run,
        )
        candidate_output_id = (
            candidate.candidate_output_id if candidate is not None else None
        )
        candidate_output_hash = candidate.envelope_hash if candidate is not None else None
        created_at = _now()
        material = {
            "schema_id": "zhimeng.provider-recovery-dossier.v1",
            "dossier_id": provider_recovery_dossier_id(
                run_id=run_id,
                provider_boundary_id=boundary.boundary_id,
            ),
            "run_id": run_id,
            "workflow_plan_id": plan.workflow_plan_id,
            "workflow_plan_hash": plan.plan_hash,
            "final_checkpoint_id": final_checkpoint.checkpoint_id,
            "final_checkpoint_hash": final_checkpoint.checkpoint_hash,
            "provider_boundary_id": boundary.boundary_id,
            "provider_boundary_hash": boundary.boundary_hash,
            "provider_request_hash": boundary.request_hash,
            "instruction_dispatch_receipt_id": dispatch_receipt.dispatch_receipt_id,
            "instruction_dispatch_receipt_hash": dispatch_receipt.receipt_hash,
            "context_pack_id": plan.context_pack_id,
            "context_pack_hash": plan.context_pack_hash,
            "operation_package_id": plan.operation_package_id,
            "operation_package_hash": plan.operation_package_hash,
            "project_id": plan.project_id,
            "project_revision": plan.project_revision,
            "project_fingerprint": plan.project_fingerprint,
            "run_status_at_capture": RunStatus(str(run["status"])),
            "candidate_state": candidate_state,
            "candidate_output_id": candidate_output_id,
            "candidate_output_hash": candidate_output_hash,
            "remote_result": "unknown",
            "provider_replay_count": 0,
            "dossier_revision": 1,
            "created_at": created_at,
        }
        draft = ProviderRecoveryDossier.model_construct(
            **material,
            dossier_hash="0" * 64,
        )
        dossier = ProviderRecoveryDossier(
            **material,
            dossier_hash=provider_recovery_dossier_hash(draft),
        )
        self._insert_provider_recovery_event(
            connection,
            run_id,
            event_type="provider_recovery_dossier",
            payload=dossier.model_dump(mode="json", by_alias=True),
            created_at=created_at,
        )
        return dossier

    @staticmethod
    def _insert_provider_recovery_event(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        event_type: str,
        payload: dict[str, object],
        created_at: datetime,
    ) -> None:
        sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_event WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO run_event(
                run_id, sequence, event_type, activity_id, payload_json, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                run_id,
                int(sequence_row[0]),
                event_type,
                _json(payload),
                created_at.isoformat(),
            ),
        )

    def _read_provider_recovery_dossier(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> ProviderRecoveryDossier | None:
        rows = connection.execute(
            """
            SELECT payload_json FROM run_event
            WHERE run_id = ? AND event_type = 'provider_recovery_dossier'
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ProviderRecoveryConflict(
                "Provider recovery dossier is duplicated",
                code="provider_recovery_integrity_failed",
            )
        try:
            dossier = ProviderRecoveryDossier.model_validate_json(
                str(rows[0]["payload_json"])
            )
        except ValueError as exc:
            raise ProviderRecoveryConflict(
                "Provider recovery dossier is invalid",
                code="provider_recovery_integrity_failed",
            ) from exc
        if dossier.run_id != run_id:
            raise ProviderRecoveryConflict(
                "Provider recovery dossier belongs to another Run",
                code="provider_recovery_integrity_failed",
            )
        dispatch_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ?
              AND artifact_type = 'creation_instruction_dispatch_receipt'
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        if len(dispatch_rows) != 1:
            raise ProviderRecoveryConflict(
                "Provider recovery dispatch receipt is missing or duplicated",
                code="provider_recovery_integrity_failed",
            )
        try:
            dispatch_receipt = self._creation_instruction_dispatch_from_artifact(
                dispatch_rows[0]
            )
        except AgentStoreError as exc:
            raise ProviderRecoveryConflict(
                "Provider recovery dispatch receipt is invalid",
                code="provider_recovery_integrity_failed",
            ) from exc
        if (
            dossier.instruction_dispatch_receipt_id
            != dispatch_receipt.dispatch_receipt_id
            or dossier.instruction_dispatch_receipt_hash != dispatch_receipt.receipt_hash
        ):
            raise ProviderRecoveryConflict(
                "Provider recovery dossier does not bind the persisted dispatch receipt",
                code="provider_recovery_integrity_failed",
            )
        return dossier

    @staticmethod
    def _read_provider_recovery_decision(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> ProviderRecoveryDecision | None:
        rows = connection.execute(
            """
            SELECT payload_json FROM run_event
            WHERE run_id = ? AND event_type = 'provider_recovery_decision'
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ProviderRecoveryConflict(
                "Provider recovery has more than one decision",
                code="provider_recovery_integrity_failed",
            )
        try:
            decision = ProviderRecoveryDecision.model_validate_json(
                str(rows[0]["payload_json"])
            )
        except ValueError as exc:
            raise ProviderRecoveryConflict(
                "Provider recovery decision is invalid",
                code="provider_recovery_integrity_failed",
            ) from exc
        if decision.run_id != run_id:
            raise ProviderRecoveryConflict(
                "Provider recovery decision belongs to another Run",
                code="provider_recovery_integrity_failed",
            )
        return decision

    def _abandon_provider_recovery_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> sqlite3.Row:
        current = RunStatus(str(run["status"]))
        if current in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}:
            raise ProviderRecoveryConflict(
                "A terminal Run cannot be abandoned through Provider recovery",
                code="provider_recovery_run_terminal",
            )
        now = _now()
        run = self._close_open_tool_calls(
            connection,
            run,
            target_status=RunStatus.FAILED,
            detail="Author abandoned the unknown Provider result; compile a new Run",
            run_error=None,
            created_at=now,
        )
        error = ErrorEnvelope(
            error=ErrorDetail(
                code="provider_result_abandoned",
                message="Unknown remote Provider result was abandoned by the author",
                request_id=str(run["id"]),
                retryable=False,
            )
        )
        sequence = int(run["last_activity_sequence"]) + 1
        error_item = self._insert_activity(
            connection,
            run,
            sequence=sequence,
            kind=ActivityKind.ERROR,
            payload=ErrorPayload(
                error=error,
                fatal=True,
                scope="run",
            ).model_dump(mode="json", by_alias=True),
            causation_id=self._last_activity_id(connection, str(run["id"])),
            created_at=now,
        )
        sequence += 1
        self._insert_activity(
            connection,
            run,
            sequence=sequence,
            kind=ActivityKind.RUN_STATE,
            payload=RunStatePayload(
                status=RunStatus.FAILED,
                previous_status=current,
                detail="Provider result abandoned; no old request was replayed",
            ).model_dump(mode="json"),
            causation_id=error_item.root.activity_id,
            created_at=now,
        )
        connection.execute(
            """
            UPDATE run
            SET status = 'failed', last_activity_sequence = ?, error_json = ?,
                started_at = COALESCE(started_at, created_at),
                updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                sequence,
                _json(error.model_dump(mode="json", by_alias=True)),
                now.isoformat(),
                now.isoformat(),
                str(run["id"]),
            ),
        )
        connection.execute(
            """
            UPDATE execution_session
            SET status = 'idle', revision = revision + 1, updated_at = ?
            WHERE id = ?
            """,
            (now.isoformat(), str(run["session_id"])),
        )
        return self._require_run_row(connection, str(run["id"]))

    @staticmethod
    def _candidate_assistant_content(candidate: AgentCandidateOutput) -> str:
        return "\n\n".join(
            (candidate.content.title, candidate.content.summary, candidate.content.body)
        )

    def list_nonterminal_workflow_run_ids(self) -> list[str]:
        """Return active Runs that own a durable Workflow Plan."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT run.id
                FROM run
                WHERE run.status IN (
                    'queued', 'starting', 'running', 'paused'
                )
                  AND EXISTS (
                    SELECT 1 FROM checkpoint
                    WHERE checkpoint.run_id = run.id
                      AND checkpoint.sequence = 0
                      AND checkpoint.checkpoint_type = ?
                  )
                ORDER BY run.created_at, run.id
                """,
                (WORKFLOW_PLAN_CHECKPOINT_TYPE,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def workflow_resume_blocker(self, run_id: str) -> str | None:
        """Return the persisted reason a pre-Provider Workflow cannot advance."""

        with self.database.connection() as connection:
            run = self._require_run_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                (run_id,),
            ).fetchall()
            if not rows:
                raise AgentStoreNotFound("Workflow Plan was not found")
            plan, chain = self._workflow_chain_from_rows(connection, run, rows)
            if any(isinstance(item, ProviderIoBoundary) for item in chain):
                return "provider_dispatch_already_started"
            checkpoints = tuple(
                item for item in chain if isinstance(item, WorkflowStepCheckpoint)
            )
            return self._workflow_resume_blocker_from_connection(
                connection,
                plan,
                checkpoints,
            )

    def pause_workflow_for_restart(self, run_id: str) -> Run:
        """Close uncertain ToolCalls and pause a pre-Provider workflow atomically."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (run_id,),
                ).fetchall()
                if not rows:
                    raise AgentStoreNotFound("Workflow Plan was not found")
                _, chain = self._workflow_chain_from_rows(connection, run, rows)
                if any(isinstance(item, ProviderIoBoundary) for item in chain):
                    raise AgentWorkflowCheckpointConflict(
                        "Provider dispatch may already have started",
                        code="provider_dispatch_already_started",
                    )
                current = RunStatus(str(run["status"]))
                if current is RunStatus.PAUSED:
                    connection.commit()
                    return self._run_from_row(run)
                if RunStatus.PAUSED not in RUN_TRANSITIONS[current]:
                    raise AgentWorkflowCheckpointConflict(
                        f"Run cannot pause workflow recovery from {current.value}",
                        code="workflow_not_resumable",
                    )

                now = _now()
                run = self._close_open_tool_calls(
                    connection,
                    run,
                    target_status=RunStatus.INTERRUPTED,
                    detail="Runtime restarted during a workflow step",
                    run_error=None,
                    created_at=now,
                )
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=RunStatus.PAUSED,
                        previous_status=current,
                        detail="Runtime restarted before Provider dispatch",
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET status = 'paused',
                        started_at = COALESCE(started_at, created_at),
                        last_activity_sequence = ?,
                        error_json = NULL, updated_at = ?, completed_at = NULL
                    WHERE id = ?
                    """,
                    (sequence, now.isoformat(), run_id),
                )
                result = self._require_run_row(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(result)

    def claim_workflow_resume(
        self,
        run_id: str,
        *,
        expected_workflow_plan_id: str,
        expected_plan_hash: str,
        expected_checkpoint_id: str | None,
        expected_checkpoint_hash: str | None,
    ) -> WorkflowResumeClaim:
        """CAS-claim one paused pre-Provider workflow for explicit execution."""

        if (expected_checkpoint_id is None) != (expected_checkpoint_hash is None):
            raise AgentWorkflowCheckpointConflict(
                "Expected workflow checkpoint identity and hash must be paired",
                code="workflow_resume_stale",
            )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                self._require_run_project_kb_binding(connection, run)
                if RunStatus(str(run["status"])) is not RunStatus.PAUSED:
                    raise AgentWorkflowCheckpointConflict(
                        "Only a paused workflow Run can be resumed",
                        code="agent_run_not_paused",
                    )
                rows = connection.execute(
                    "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
                    (run_id,),
                ).fetchall()
                if not rows:
                    raise AgentStoreNotFound("Workflow Plan was not found")
                plan, chain = self._workflow_chain_from_rows(connection, run, rows)
                if (
                    plan.workflow_plan_id != expected_workflow_plan_id
                    or plan.plan_hash != expected_plan_hash
                ):
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow Plan changed before Resume",
                        code="workflow_resume_stale",
                    )
                if any(isinstance(item, ProviderIoBoundary) for item in chain):
                    raise AgentWorkflowCheckpointConflict(
                        "Provider dispatch already started and cannot be replayed",
                        code="provider_dispatch_already_started",
                    )
                checkpoints = tuple(
                    item for item in chain if isinstance(item, WorkflowStepCheckpoint)
                )
                last = checkpoints[-1] if checkpoints else None
                actual_checkpoint_id = None if last is None else last.checkpoint_id
                actual_checkpoint_hash = None if last is None else last.checkpoint_hash
                if (
                    expected_checkpoint_id != actual_checkpoint_id
                    or expected_checkpoint_hash != actual_checkpoint_hash
                ):
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow checkpoint changed before Resume",
                        code="workflow_resume_stale",
                    )
                if last is not None:
                    previous_step = plan.steps[last.step_ordinal - 1]
                    if (
                        last.execution_status != "succeeded"
                        or last.domain_status
                        not in previous_step.continue_on_domain_status
                    ):
                        raise AgentWorkflowCheckpointConflict(
                            "Workflow stopped on a non-resumable step outcome",
                            code="workflow_not_resumable",
                        )
                blocker = self._workflow_resume_blocker_from_connection(
                    connection,
                    plan,
                    checkpoints,
                )
                if blocker is not None:
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow step has terminal execution evidence without a resumable checkpoint",
                        code=blocker,
                    )
                next_step_ordinal = (
                    len(checkpoints) + 1
                    if len(checkpoints) < len(plan.steps)
                    else None
                )
                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=RunStatus.RUNNING,
                        previous_status=RunStatus.PAUSED,
                        detail="Author explicitly resumed the durable workflow",
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET status = 'running', last_activity_sequence = ?,
                        error_json = NULL, updated_at = ?, completed_at = NULL
                    WHERE id = ?
                    """,
                    (sequence, now.isoformat(), run_id),
                )
                claimed = self._require_run_row(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return WorkflowResumeClaim(
            run=self._run_from_row(claimed),
            plan=plan,
            checkpoints=checkpoints,
            next_step_ordinal=next_step_ordinal,
        )

    def list_activities(self, run_id: str, *, after_sequence: int = 0) -> list[ActivityItem]:
        return self.read_activity_page(run_id, after_sequence=after_sequence).activities

    def read_activity_page(
        self, run_id: str, *, after_sequence: int = 0,
        limit: int | None = None, through_sequence: int | None = None,
    ) -> AgentActivityListResponse:
        """Bound HTTP reads to one activity prefix; keep unpaged callers compatible.

        The first page chooses a durable upper bound. Later pages reuse it even
        while the run grows. Its SSE cursor points to the same activity prefix,
        never to the current (possibly newer) event tail.
        """
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            run = connection.execute("SELECT last_activity_sequence FROM run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise AgentStoreNotFound("Agent run was not found")
            upper = int(run['last_activity_sequence'])
            if through_sequence is not None:
                upper = min(upper, through_sequence)
            rows = connection.execute(
                """
                SELECT sequence, payload_json FROM activity
                WHERE run_id = ? AND sequence > ? AND sequence <= ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (run_id, after_sequence, upper, limit + 1 if limit is not None else -1),
            ).fetchall()
            event_cursor = connection.execute(
                """SELECT COALESCE(MAX(re.sequence), 0) FROM run_event AS re
                   JOIN activity AS a ON a.id = re.activity_id
                   WHERE re.run_id = ? AND a.sequence <= ?""", (run_id, upper),
            ).fetchone()[0]
        more = limit is not None and len(rows) > limit
        if more:
            rows = rows[:limit]
        return AgentActivityListResponse(
            run_id=run_id,
            activities=[ActivityItem.model_validate_json(str(row['payload_json'])) for row in rows],
            last_sequence=upper,
            next_after_sequence=int(rows[-1]['sequence']) if more else None,
            last_event_sequence=int(event_cursor),
        )

    def list_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[StoredRunEvent]:
        with self.database.connection() as connection:
            run = connection.execute("SELECT id FROM run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise AgentStoreNotFound("Agent run was not found")
            rows = connection.execute(
                """
                SELECT re.sequence, a.payload_json
                FROM run_event AS re
                JOIN activity AS a ON a.id = re.activity_id
                WHERE re.run_id = ? AND re.sequence > ? AND re.event_type = 'activity'
                ORDER BY re.sequence ASC
                LIMIT ?
                """,
                (run_id, after_sequence, limit if limit is not None else -1),
            ).fetchall()
        return [
            StoredRunEvent(
                sequence=int(row["sequence"]),
                activity=ActivityItem.model_validate_json(str(row["payload_json"])),
            )
            for row in rows
        ]

    def append_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """Append a non-activity event (model turn, fold, stop) to the Run log.

        与 activity 事件共用 run_event 的 ``UNIQUE(run_id, sequence)``，
        sequence 分配必须在 BEGIN IMMEDIATE 事务内，避免并发写撞号。
        ``list_run_events`` 只捞 ``event_type = 'activity'``，这些生命周期
        事件不会混进 SSE 活动流。
        """
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run_row(connection, run_id)
                self._insert_provider_recovery_event(
                    connection,
                    run_id,
                    event_type=event_type,
                    payload=payload,
                    created_at=_now(),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_messages(
        self,
        session_id: str,
        *,
        after_ordinal: int = 0,
        limit: int = 200,
        before_ordinal: int | None = None,
        latest: bool = False,
    ) -> list[AgentMessageResponse]:
        """按顺序返回消息。``latest`` 为真时取这段范围里最新的 ``limit`` 条（仍按先后排）：
        界面打开长对话要看的是最后那些，不是最早的那些。"""
        upper = before_ordinal if before_ordinal is not None else 2**62
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            rows = connection.execute(
                f"""
                SELECT sm.*, a.payload_json
                FROM session_message AS sm
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.session_id = ? AND sm.ordinal > ? AND sm.ordinal < ?
                ORDER BY sm.ordinal {"DESC" if latest else "ASC"}
                LIMIT ?
                """,
                (session_id, after_ordinal, upper, limit),
            ).fetchall()
        if latest:
            rows = list(reversed(rows))
        return [self._message_from_row(row) for row in rows]

    def has_messages_between(self, session_id: str, *, after_ordinal: int, before_ordinal: int) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM session_message WHERE session_id = ? AND ordinal > ? AND ordinal < ? LIMIT 1",
                (session_id, after_ordinal, before_ordinal),
            ).fetchone()
        return row is not None

    def run_process_summaries(
        self, session_id: str, run_ids: list[str],
    ) -> dict[str, AgentRunProcessSummary]:
        """Aggregate one message page without loading tool arguments/results.

        One query covers all distinct runs. JSON metadata is inspected only for
        reasoning and artifacts; large tool input/output bodies stay in SQLite.
        """
        selected = list(dict.fromkeys(run_ids))
        if not selected:
            return {}
        placeholders = ",".join("?" for _ in selected)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.id AS run_id,
                    SUM(CASE WHEN a.kind = 'tool_call' THEN 1 ELSE 0 END) AS tool_count,
                    MAX(CASE
                        WHEN a.kind IN ('tool_call', 'command', 'skill_call', 'change_proposal', 'error') THEN 1
                        WHEN a.kind = 'artifact' THEN
                            COALESCE(json_extract(a.payload_json, '$.payload.artifact_type'), '')
                                NOT IN ('context_pack', 'writer_intent', 'context_injection_receipt')
                        WHEN a.kind = 'reasoning' THEN
                            COALESCE(json_extract(a.payload_json, '$.payload.source'), 'model') != 'runtime'
                            AND NOT (
                                COALESCE(json_extract(a.payload_json, '$.payload.summary'), '') LIKE '本次%'
                                AND instr(COALESCE(json_extract(a.payload_json, '$.payload.content'), ''), '这描述的是发送参数') > 0
                            )
                            AND length(trim(CASE
                                WHEN json_extract(a.payload_json, '$.payload.phase') = 'delta'
                                    THEN COALESCE(json_extract(a.payload_json, '$.payload.delta'), json_extract(a.payload_json, '$.payload.content'), '')
                                ELSE COALESCE(json_extract(a.payload_json, '$.payload.content'), json_extract(a.payload_json, '$.payload.summary'), '')
                            END)) > 0
                        ELSE 0
                    END) AS has_process
                FROM run AS r
                LEFT JOIN activity AS a ON a.run_id = r.id
                    AND a.kind IN ('tool_call', 'command', 'skill_call', 'change_proposal', 'error', 'artifact', 'reasoning')
                WHERE r.session_id = ? AND r.id IN ({placeholders})
                GROUP BY r.id
                """,
                (session_id, *selected),
            ).fetchall()
        return {
            str(row["run_id"]): AgentRunProcessSummary(
                has_process=bool(row["has_process"]), tool_count=int(row["tool_count"]),
            )
            for row in rows
        }

    def pending_steering_inputs(self, run_id: str):
        """读取当前运行待吸收的用户输入；局部导入避免存储模块相互依赖。"""
        from zhimeng_runtime.agent_steering import AgentSteeringStore
        return AgentSteeringStore(self).pending(run_id)

    @staticmethod
    def command_completion_messages(events: list[dict]) -> list[dict[str, object]]:
        """Runtime facts have a source; they never become session user messages."""
        messages = []
        for event in events:
            payload = dict(event["payload"])
            for name in ("stdout", "stderr"):
                path = payload.get(name + "_file")
                if path:
                    try:
                        with Path(path).open("rb") as stream:
                            size = stream.seek(0, 2)
                            stream.seek(max(0, size - 8000))
                            payload[name + "_tail"] = stream.read().decode("utf-8", errors="replace")
                    except OSError:
                        payload[name + "_tail"] = "日志目前不可读，可稍后按完整日志路径重试。"
            messages.append({"role": "system", "content": "后台命令的真实完成结果，继续结合此前用户的任务处理：\n" + _json({
                "source": "command_completion", "event_id": event["event_id"],
                "command_session_id": event["command_session_id"], "owner_run_id": event["owner_run_id"],
                "status": event["status"], "exit_code": event["exit_code"], "result": payload,
            })})
        return messages

    def completion_start_messages(self, run_id: str) -> list[dict[str, object]]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT messages_json FROM run_transcript WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["messages_json"]) if row else []

    def pending_command_completions(self, run_id: str) -> list[dict]:
        """Optional event-source seam, parallel to pending_steering_inputs."""
        from zhimeng_runtime.command_completion import CommandCompletionStore
        run = self.get_run(run_id)
        return CommandCompletionStore(self.database).pending(run.session_id)

    def consume_command_completions(self, run_id: str, transcript: list[dict[str, object]], pin: tuple[int, str] | None = None,
                                    event_ids: list[str] | None = None) -> list[dict[str, object]]:
        from zhimeng_runtime.command_completion import CommandCompletionStore
        completions = CommandCompletionStore(self.database)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if run["status"] not in {"starting", "running"}:
                    connection.commit()
                    return []
                events = completions.pending(str(run["session_id"]), connection=connection)
                if event_ids is not None:
                    selected = set(event_ids)
                    events = [item for item in events if item["event_id"] in selected]
                if not events:
                    connection.commit()
                    return []
                messages = self.command_completion_messages(events)
                GoalStore.invalidate_outcome(connection, run_id)
                self._save_run_transcript_in_connection(connection, run_id, [*transcript, *messages])
                completions.mark_delivered([item["event_id"] for item in events], run_id, connection=connection)
                if pin is not None:
                    metadata = {"event_ids": [item["event_id"] for item in events], "result_revision": pin[0], "result_fingerprint": pin[1]}
                    connection.execute("INSERT INTO artifact(id,run_id,artifact_type,relative_path,content_hash,metadata_json,created_at) VALUES(?,?,'command_completion_observation',NULL,?,?,?)",
                        (_new_id("artifact"), run_id, hashlib.sha256(_json(metadata).encode()).hexdigest(), _json(metadata), _now().isoformat()))
                connection.commit()
                return messages
            except Exception:
                connection.rollback()
                raise

    def message_run_origins(self, run_ids: list[str]) -> dict[str, str]:
        if not run_ids:
            return {}
        with self.database.connection() as connection:
            rows = connection.execute("SELECT id,origin_kind FROM run WHERE id IN (" + ",".join("?" for _ in run_ids) + ")", run_ids).fetchall()
        return {str(row["id"]): str(row["origin_kind"]) for row in rows}

    def message_review_sources(self, run_ids: list[str]):
        from zhimeng_runtime.task_review import review_sources
        with self.database.connection() as connection:
            return review_sources(connection, run_ids)

    def review_provider_messages(self, run_id: str, *, max_characters: int):
        from zhimeng_runtime.task_review import review_messages
        with self.database.connection() as connection:
            return review_messages(connection, self._require_run_row(connection, run_id), max_characters=max_characters)

    def review_source_file_ids(self, run_id: str) -> tuple[str, ...]:
        from zhimeng_runtime.task_review import review_record
        with self.database.connection() as connection:
            record = review_record(connection, run_id)
        return tuple(dict.fromkeys(
            file_id for source_id in (record or {}).get('source_run_ids', [])
            for file_id in self.get_writer_focus(source_id)[1]
        ))

    def save_run_transcript(self, run_id: str, messages: list[dict[str, object]]) -> None:
        """保存本轮模型往来，包括执行工具前的请求检查点。

        下一轮拼历史时原样放回（``provider_history``）。同一轮再存一次就换掉（续跑同一轮时）。
        """
        with self.database.connection() as connection:
            self._save_run_transcript_in_connection(connection, run_id, messages)

    def get_run_goal(self, run_id: str):
        with self.database.connection() as connection:
            return GoalStore.for_run(connection, run_id)

    def commit_run_compaction(self, run_id: str, messages: list[dict[str, object]], stats: dict) -> None:
        """The compacted transcript and its success receipt become visible together."""
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_run_row(connection, run_id)
                if row["status"] != RunStatus.RUNNING.value:
                    raise AgentStoreConflict("任务已停止，不再替换上下文。")
                self._save_run_transcript_in_connection(connection, run_id, messages)
                self._insert_provider_recovery_event(connection, run_id, event_type="run_context_compaction",
                                                     payload=stats, created_at=_now())
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _recover_run_transcript_in_connection(self, connection: sqlite3.Connection, run_id: str) -> None:
        from zhimeng_runtime.agent_transcript import recovered_tool_exchange, repair_tool_pairs
        from zhimeng_runtime.workflow_runner import workflow_runtime_tool_call_id

        saved = connection.execute(
            "SELECT messages_json, through_message_ordinal FROM run_transcript WHERE run_id = ?", (run_id,),
        ).fetchone()
        call_rows = connection.execute(
            "SELECT tc.*, a.sequence FROM tool_call tc JOIN activity a ON a.id = tc.call_activity_id "
            "WHERE tc.run_id = ? ORDER BY a.sequence", (run_id,),
        ).fetchall()
        if not call_rows and saved is None:
            return
        calls = [self._tool_call_from_row(row) for row in call_rows]
        messages = json.loads(saved["messages_json"]) if saved is not None else []
        covered = saved["through_message_ordinal"] if saved is not None else 0
        repaired = repair_tool_pairs(run_id, messages, calls)
        represented = {
            workflow_runtime_tool_call_id(run_id, call["id"])
            for message in messages for call in message.get("tool_calls", [])
        } | {call["id"] for message in messages for call in message.get("tool_calls", [])}
        # Older checkpoints may precede the last batch. Merge only missing durable
        # receipts and later visible messages, ordered by their recorded activities.
        tail = [(int(row["sequence"]), recovered_tool_exchange(call))
                for row, call in zip(call_rows, calls) if call.tool_call_id not in represented]
        visible = connection.execute(
            "SELECT sm.*, a.payload_json, a.sequence FROM session_message sm "
            "JOIN activity a ON a.id = sm.activity_id WHERE sm.run_id = ? ORDER BY sm.ordinal", (run_id,),
        ).fetchall()
        for index, row in enumerate(visible):
            if index == 0 and row["role"] == "user":
                continue  # provider_history prepends the original request.
            if covered is None or int(row["ordinal"]) <= covered:
                continue
            message = self._message_from_row(row)
            tail.append((int(row["sequence"]), [provider_image_message(message.role, message.content, message.images)]))
        for _, exchange in sorted(tail, key=lambda item: item[0]):
            repaired.extend(exchange)
        if repaired:
            self._save_run_transcript_in_connection(connection, run_id, repaired)

    def _save_run_transcript_in_connection(self, connection, run_id: str, messages: list[dict[str, object]]) -> None:
        payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        estimated = sum(_provider_message_tokens(message) for message in messages)
        row = self._require_run_row(connection, run_id)
        through_ordinal = connection.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM session_message WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        connection.execute(
                """
                INSERT INTO run_transcript (run_id, session_id, messages_json, estimated_tokens, through_message_ordinal, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    estimated_tokens = excluded.estimated_tokens,
                    through_message_ordinal = excluded.through_message_ordinal,
                    created_at = excluded.created_at
                """,
                (run_id, str(row["session_id"]), payload, estimated, through_ordinal, datetime.now(UTC).isoformat()),
        )

    def transcript_token_estimates(self, session_id: str) -> dict[str, int]:
        """这场对话里存过往来的各轮：run_id → 那一段估算的 token（整理对话时按它算历史有多长）。"""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT run_id, estimated_tokens FROM run_transcript WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return {str(row["run_id"]): int(row["estimated_tokens"]) for row in rows}

    @staticmethod
    def _run_transcripts(connection: sqlite3.Connection, session_id: str) -> dict[str, list[dict[str, object]]]:
        rows = connection.execute(
            "SELECT run_id, messages_json FROM run_transcript WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        transcripts: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            try:
                messages = json.loads(str(row["messages_json"]))
            except ValueError:
                continue
            if isinstance(messages, list) and messages and all(
                isinstance(message, dict) and message.get("role") in {"system", "user", "assistant", "tool"}
                for message in messages
            ):
                transcripts[str(row["run_id"])] = messages
        return transcripts

    def provider_history(
        self,
        session_id: str,
        *,
        max_messages: int | None = None,
        max_characters: int = 120_000,
        max_tokens: int | None = None,
    ) -> list[dict[str, object]]:
        """拼这一轮要发给模型的对话历史。

        已经折叠进摘要的那一段不再逐条重发，改为在最前面放一段摘要；摘要之后
        的消息原样保留。没有摘要时（绝大多数会话）行为与从前逐字节一致。

        9-27 起默认不按条数截：以前只取最近 64 条，聊满 32 个来回还没到压缩线时窗口每轮往后滑一格，
        整段历史每轮都换开头、提示缓存全不命中（Reasonix 档案 7.1）。压缩线以下只追加；
        ``max_characters`` / ``max_tokens`` 由调用方按窗口档位给，只防一次贴进来的超长内容。

        9-28 起存过往来的那几轮（``save_run_transcript``）照原样放回：作者的话 + 那一轮发给模型的整段
        （工具调用、结果、补话、回答），一轮当一整块取舍，调用和结果不拆开（ZCode 档案 12.2）。没存过的照旧只带文字。
        """
        return self.provider_history_parts(
            session_id, max_messages=max_messages, max_characters=max_characters, max_tokens=max_tokens
        )[0]

    def provider_history_parts(
        self,
        session_id: str,
        *,
        max_messages: int | None = None,
        max_characters: int = 120_000,
        max_tokens: int | None = None,
    ) -> tuple[list[dict[str, object]], str | None]:
        """同 ``provider_history``，另外给出最后一条消息是哪一轮的（新的一轮存往来时，从这一轮作者的话后面开始切）。"""
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            checkpoint_row = connection.execute(
                """
                SELECT *
                FROM session_checkpoint
                WHERE session_id = ?
                ORDER BY through_ordinal DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            folded_through = (
                0 if checkpoint_row is None else int(checkpoint_row["through_ordinal"])
            )
            has_folded_images = connection.execute(
                "SELECT 1 FROM session_message WHERE session_id=? AND ordinal<=? AND images_json!='[]' LIMIT 1",
                (session_id, folded_through),
            ).fetchone() is not None if folded_through else False
            rows = connection.execute(
                """
                SELECT sm.*, a.payload_json
                FROM session_message AS sm
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.session_id = ? AND sm.ordinal > ?
                ORDER BY sm.ordinal DESC
                LIMIT ?
                """,
                (session_id, folded_through, PROVIDER_HISTORY_ROW_LIMIT),
            ).fetchall()
            transcripts = self._run_transcripts(connection, session_id)
            history_runs = connection.execute("SELECT id,ordinal,origin_kind,status FROM run WHERE session_id=?", (session_id,)).fetchall()
            run_ordinals = {str(row["id"]): int(row["ordinal"]) for row in history_runs}
            orphan_completion_ids = {str(row["id"]) for row in history_runs if row["origin_kind"] == "command_completion" and row["status"] in {"failed", "interrupted", "cancelled"}} - {str(row["run_id"]) for row in rows}
            transcript_coverage = {str(row['run_id']): row['through_message_ordinal'] for row in connection.execute(
                "SELECT run_id, through_message_ordinal FROM run_transcript WHERE session_id = ?", (session_id,),
            ).fetchall()}
        messages = [self._message_from_row(row) for row in reversed(rows)]
        last_run_id = messages[-1].run_id if messages else None
        # 取舍的单位：存过往来的一轮是一整块（这一轮作者的话 + 那一段），别的照旧一条一块。
        blocks: list[list[dict[str, object]]] = []
        block_run_ids: list[str] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            transcript = transcripts.get(message.run_id)
            if transcript is None:
                if message.role in {"user", "assistant"}:
                    blocks.append([provider_image_message(message.role, message.content, message.images)])
                    block_run_ids.append(message.run_id)
                index += 1
                continue
            block: list[dict[str, object]] = []
            later: list[dict[str, object]] = []
            covered_through = transcript_coverage.get(message.run_id)
            while index < len(messages) and messages[index].run_id == message.run_id:
                # 中途插入的用户消息已经在 transcript 的实际位置；这里只补初始输入。
                if messages[index].role == "user" and not block:
                    block.append(provider_image_message("user", messages[index].content, messages[index].images))
                elif covered_through is not None and messages[index].ordinal > covered_through and messages[index].role in {'user', 'assistant'}:
                    later.append(provider_image_message(messages[index].role, messages[index].content, messages[index].images))
                index += 1
            blocks.append([*block, *transcript, *later])
            block_run_ids.append(message.run_id)
        # An internal run may fail/interruption before its first answer. Its
        # already-claimed source facts still belong in the next turn's history.
        for orphan in orphan_completion_ids:
            if orphan in transcripts and transcript_coverage.get(orphan, 0) == 0:
                blocks.append(transcripts[orphan])
                block_run_ids.append(orphan)
        blocks = [block for _, block in sorted(zip(block_run_ids, blocks), key=lambda pair: run_ordinals[pair[0]])]
        selected: list[list[dict[str, object]]] = []
        size = 0
        message_count = 0
        for block in reversed(blocks):
            block_size = sum(
                _provider_message_tokens(item) if max_tokens is not None else _provider_message_characters(item)
                for item in block
            )
            limit = max_tokens if max_tokens is not None else max_characters
            if selected and (
                (max_messages is not None and message_count + len(block) > max_messages)
                or size + block_size > limit
            ):
                break
            selected.append(block)
            size += block_size
            message_count += len(block)
        history = [item for block in reversed(selected) for item in block]
        if checkpoint_row is None:
            return history, last_run_id
        # 摘要放在最前面：它替代的正是这段历史之前的内容，顺序上也对得上，
        # 而且稳定的前缀对 provider 的 prompt cache 更友好。
        summary = {
            "role": "system",
            "content": render_checkpoint_block(
                through_ordinal=folded_through,
                content=str(checkpoint_row["content"]),
            ),
        }
        if has_folded_images:
            summary["content"] += "\n较早的附图仍保存在历史消息中，本次上下文使用压缩后的文字摘要。"
        return [summary, *history], last_run_id

    # ------------------------------------------------------------------
    # 会话摘要（Session Checkpoint）的存取。
    #
    # 触发与注入分别在 agent_service / context 那边；这里只负责「写一份」
    # 与「读最新的一份」。一份摘要落库之后内容不再变化，同一 session 的
    # through_ordinal 唯一，重复折叠同一段历史会被数据库直接挡住。
    # ------------------------------------------------------------------

    def latest_session_checkpoint(
        self,
        session_id: str,
    ) -> SessionCheckpointRecord | None:
        """取这个会话最新的一份摘要；从没折叠过则返回 None。"""
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            row = connection.execute(
                """
                SELECT *
                FROM session_checkpoint
                WHERE session_id = ?
                ORDER BY through_ordinal DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else self._session_checkpoint_from_row(row)

    def compaction_source_records(
        self,
        session_id: str,
        *,
        through_ordinal: int,
    ) -> tuple[list[StoredToolCall], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """整理到第 ``through_ordinal`` 条消息为止时，程序要附在摘要后面的原始记录（``compaction_facts`` 拼）。

        返回 (这些消息所属各轮的工具调用, 修改记录（带现在的状态和 changes）, 打开的文件夹以外的写入, 作者说过的每一句)，
        都按时间排。修改记录以表里现在的状态为准：作者在界面上点的保存 / 撤回不经过工具，只改这里。
        """
        runs = "SELECT DISTINCT run_id FROM session_message WHERE session_id = ? AND ordinal <= ?"
        params = (session_id, through_ordinal)
        with self.database.connection() as connection:
            call_rows = connection.execute(
                f"SELECT * FROM tool_call WHERE run_id IN ({runs}) ORDER BY started_at, id",
                params,
            ).fetchall()
            change_set_rows = connection.execute(
                f"""
                SELECT status, payload_json FROM change_set
                WHERE json_extract(payload_json, '$.source_run_id') IN ({runs})
                ORDER BY created_at, id
                """,
                params,
            ).fetchall()
            file_change_rows = connection.execute(
                f"""
                SELECT operation, path, target_path, undone_at FROM file_change
                WHERE run_id IN ({runs})
                ORDER BY created_at, seq
                """,
                params,
            ).fetchall()
            author_rows = connection.execute(
                """
                SELECT sm.*, a.payload_json
                FROM session_message AS sm
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.session_id = ? AND sm.ordinal <= ? AND sm.role = 'user'
                ORDER BY sm.ordinal
                """,
                params,
            ).fetchall()
        change_sets: list[dict[str, Any]] = []
        for row in change_set_rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            change_sets.append(
                {
                    "status": row["status"],
                    "title": payload.get("title"),
                    "changes": payload.get("changes") or [],
                }
            )
        return (
            [self._tool_call_from_row(row) for row in call_rows],
            change_sets,
            [dict(row) for row in file_change_rows],
            [self._message_from_row(row).content for row in author_rows],
        )

    def append_session_checkpoint(
        self,
        session_id: str,
        *,
        run_id: str | None,
        through_ordinal: int,
        source_message_count: int,
        content: str,
        reported_model: str | None = None,
        expected_previous_through_ordinal: int | None = None,
    ) -> SessionCheckpointRecord:
        """落一份摘要。

        ``through_ordinal`` 必须比已有的最新一份更靠后——摘要只会往前推进，
        不会回头覆盖已经折叠过的区间。
        """
        normalized = content.strip()
        if not normalized:
            raise ValueError("session checkpoint content must not be blank")
        if through_ordinal < 2 or source_message_count < 2:
            raise ValueError("a checkpoint must fold at least two messages")
        created_at = _now()
        record_id = _new_id("checkpoint")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            previous = connection.execute(
                """
                SELECT through_ordinal
                FROM session_checkpoint
                WHERE session_id = ?
                ORDER BY through_ordinal DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if expected_previous_through_ordinal is not None and expected_previous_through_ordinal != (0 if previous is None else int(previous["through_ordinal"])):
                raise AgentStoreConflict("对话摘要已更新，请重新整理")
            if previous is not None and through_ordinal <= int(
                previous["through_ordinal"]
            ):
                raise AgentStoreConflict(
                    "A session checkpoint already covers this history range"
                )
            connection.execute(
                """
                INSERT INTO session_checkpoint(
                    id, session_id, run_id, through_ordinal,
                    source_message_count, content, content_hash,
                    reported_model, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    session_id,
                    run_id,
                    through_ordinal,
                    source_message_count,
                    normalized,
                    checkpoint_content_hash(normalized),
                    reported_model,
                    created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_checkpoint WHERE id = ?",
                (record_id,),
            ).fetchone()
            connection.commit()
        return self._session_checkpoint_from_row(row)

    @staticmethod
    def _session_checkpoint_from_row(row: sqlite3.Row) -> SessionCheckpointRecord:
        run_id = row["run_id"]
        return SessionCheckpointRecord(
            checkpoint_id=str(row["id"]),
            session_id=str(row["session_id"]),
            run_id=None if run_id is None else str(run_id),
            through_ordinal=int(row["through_ordinal"]),
            source_message_count=int(row["source_message_count"]),
            content=str(row["content"]),
            content_hash=str(row["content_hash"]),
            reported_model=(
                None if row["reported_model"] is None else str(row["reported_model"])
            ),
            created_at=_parse_time(row["created_at"]),
        )

    def completed_history(
        self,
        session_id: str,
        *,
        max_runs: int = 20,
        max_characters: int = 120_000,
        max_tokens: int = 120_000,
        after_ordinal: int | None = None,
    ) -> tuple[int, list[CompletedHistoryMessage]]:
        """Return only complete successful turns for a future Context Pack."""

        if max_runs < 1 or max_characters < 1 or max_tokens < 1:
            raise ValueError("history limits must be positive")
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT last_message_ordinal FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            cp_row = connection.execute(
                """
                SELECT through_ordinal
                FROM session_checkpoint
                WHERE session_id = ?
                ORDER BY through_ordinal DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            folded_through = (0 if cp_row is None else int(cp_row["through_ordinal"])) if after_ordinal is None else after_ordinal
            rows = connection.execute(
                """
                WITH completed_runs AS (
                    SELECT id, ordinal, origin_kind
                    FROM run
                    WHERE session_id = ? AND (status = 'succeeded' OR (run_type = 'conversation_fork'
                        AND (SELECT COUNT(*) FROM session_message sm WHERE sm.run_id = run.id
                             AND sm.role IN ('user', 'assistant')) = 2)
                        OR (run_type = 'conversation_fork' AND origin_kind='command_completion'))
                    ORDER BY ordinal DESC
                    LIMIT ?
                )
                SELECT sm.*, a.payload_json, cr.ordinal AS run_ordinal, cr.origin_kind AS run_origin_kind
                FROM completed_runs AS cr
                JOIN session_message AS sm ON sm.run_id = cr.id
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.role IN ('user', 'assistant') AND sm.ordinal > ?
                ORDER BY cr.ordinal DESC, sm.ordinal ASC
                """,
                (session_id, max_runs, folded_through),
            ).fetchall()

        completion_message_ordinals = {int(row["ordinal"]) for row in rows if row["run_origin_kind"] == "command_completion"}
        turns: list[list[CompletedHistoryMessage]] = []
        current_run_ordinal: int | None = None
        current_turn: list[CompletedHistoryMessage] = []
        for row in rows:
            run_ordinal = int(row["run_ordinal"])
            if current_run_ordinal is not None and run_ordinal != current_run_ordinal:
                turns.append(current_turn)
                current_turn = []
            current_run_ordinal = run_ordinal
            message = self._message_from_row(row)
            current_turn.append(
                CompletedHistoryMessage(
                    ordinal=message.ordinal,
                    role=message.role,
                    content=message.content,
                )
            )
        if current_turn:
            turns.append(current_turn)

        selected_newest_first: list[list[CompletedHistoryMessage]] = []
        character_count = 0
        token_count = 0
        for index, turn in enumerate(turns):
            roles = [message.role for message in turn]
            if (
                folded_through > 0
                and index == len(turns) - 1
                and roles
                and all(role == "assistant" for role in roles)
                and turn[0].ordinal not in completion_message_ordinals
            ):
                # 以前整理记忆会从一轮中间切开（截断续写的一轮有好几条回答）：这一轮的提问
                # 已经进了摘要，剩下的续写段接不上前文，跳过它，比整条对话再也建不了资料包强。
                continue
            # 同一轮可能有运行中补充的用户输入，保持真实顺序。
            if (len(roles) < 2 or roles[0] != "user" or roles[-1] != "assistant") and not (
                roles and roles[-1] == "assistant" and turn[0].ordinal in completion_message_ordinals
            ):
                raise AgentStoreInvalidState(
                    "A succeeded Run must start with user input and end with an assistant message"
                )
            next_count = character_count + sum(len(message.content) for message in turn)
            next_token_count = token_count + sum(
                4
                + conservative_token_estimate(message.role)
                + conservative_token_estimate(message.content)
                for message in turn
            )
            if next_count > max_characters or next_token_count > max_tokens:
                break
            selected_newest_first.append(turn)
            character_count = next_count
            token_count = next_token_count

        selected = [
            message
            for turn in reversed(selected_newest_first)
            for message in turn
        ]
        return int(session["last_message_ordinal"]), selected

    def prepare_context_injection(
        self,
        run_id: str,
        *,
        knowledge_observation: KnowledgeFingerprintObservation | None = None,
    ) -> PreparedContextInjection | None:
        """Validate the bound Pack and persist a receipt before Provider I/O."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                context_pack_id = (
                    str(run["context_pack_id"])
                    if run["context_pack_id"] is not None
                    else None
                )
                if context_pack_id is None:
                    connection.commit()
                    return None
                run_status = RunStatus(str(run["status"]))
                if run_status not in {RunStatus.STARTING, RunStatus.RUNNING}:
                    raise AgentStoreInvalidState(
                        "Context injection requires a starting or resumed running Run"
                    )
                row = connection.execute(
                    "SELECT * FROM context_pack WHERE id = ?",
                    (context_pack_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - protected by the foreign key.
                    raise AgentContextPackNotFound("Context Pack was not found")
                try:
                    pack = ContextPack.model_validate_json(str(row["payload_json"]))
                except Exception as exc:
                    raise AgentContextPackConflict(
                        "Stored Context Pack failed integrity validation",
                        code="context_injection_integrity_failed",
                    ) from exc

                duplicated_values = {
                    "context_pack_id": str(row["id"]),
                    "session_id": str(row["session_id"]),
                    "project_id": str(row["project_binding_id"]),
                    "history_through_ordinal": int(row["history_through_ordinal"]),
                    "objective": str(row["objective"]),
                    "active_chapter_id": (
                        str(row["active_chapter_id"])
                        if row["active_chapter_id"] is not None
                        else None
                    ),
                    "profile": str(row["profile"]),
                    "project_revision": int(row["project_revision"]),
                    "project_fingerprint": str(row["project_fingerprint"]),
                    "project_kb_state_id": (
                        str(row["project_kb_state_id"])
                        if row["project_kb_state_id"] is not None
                        else None
                    ),
                    "project_kb_manifest_hash": (
                        str(row["project_kb_manifest_hash"])
                        if row["project_kb_manifest_hash"] is not None
                        else None
                    ),
                    "knowledge_fingerprint": (
                        str(row["knowledge_fingerprint"])
                        if row["knowledge_fingerprint"] is not None
                        else None
                    ),
                    "readiness": str(row["readiness"]),
                    "pack_hash": str(row["pack_hash"]),
                    "injection_hash": str(row["injection_hash"]),
                    "provider_messages_hash": str(row["provider_messages_hash"]),
                }
                pack_values = {
                    "context_pack_id": pack.context_pack_id,
                    "session_id": pack.session_id,
                    "project_id": pack.project_id,
                    "history_through_ordinal": pack.history_through_ordinal,
                    "objective": pack.objective,
                    "active_chapter_id": pack.active_chapter_id,
                    "profile": pack.profile,
                    "project_revision": pack.project_revision,
                    "project_fingerprint": pack.project_fingerprint,
                    "project_kb_state_id": pack.project_kb_state_id,
                    "project_kb_manifest_hash": pack.project_kb_manifest_hash,
                    "knowledge_fingerprint": pack.knowledge_fingerprint,
                    "readiness": pack.readiness.value,
                    "pack_hash": pack.pack_hash,
                    "injection_hash": pack.injection_hash,
                    "provider_messages_hash": pack.provider_messages_hash,
                }
                if duplicated_values != pack_values:
                    raise AgentContextPackConflict(
                        "Context Pack columns do not match the immutable payload",
                        code="context_injection_integrity_failed",
                    )
                if (
                    pack.session_id != str(run["session_id"])
                    or pack.project_id != str(run["project_binding_id"])
                    or pack.project_revision != int(run["project_revision"])
                    or pack.project_fingerprint != str(run["project_fingerprint"])
                    or pack.objective != str(run["objective"])
                    or pack.readiness.value == "blocked"
                ):
                    raise AgentContextPackConflict(
                        "Context Pack no longer matches its bound Run",
                        code="context_injection_integrity_failed",
                    )
                project = connection.execute(
                    "SELECT revision, fingerprint FROM project_binding WHERE id = ?",
                    (pack.project_id,),
                ).fetchone()
                if project is None:
                    raise AgentContextPackConflict(
                        "Context Pack project binding no longer exists",
                        code="context_injection_integrity_failed",
                    )
                if (
                    int(project["revision"]) != pack.project_revision
                    or str(project["fingerprint"]) != pack.project_fingerprint
                ):
                    raise AgentContextPackConflict(
                        "Project changed after the Context Pack was bound",
                        code="context_pack_stale",
                    )
                session = connection.execute(
                    "SELECT last_message_ordinal FROM execution_session WHERE id = ?",
                    (pack.session_id,),
                ).fetchone()
                if (
                    session is None
                    or int(session["last_message_ordinal"])
                    != pack.history_through_ordinal + 1
                ):
                    raise AgentContextPackConflict(
                        "Session history changed after the Context Pack was bound",
                        code="context_pack_stale",
                    )

                messages = tuple(
                    {"role": item.role, "content": item.content}
                    for item in pack.exact_provider_messages
                )
                expected_metadata = {
                    "context_pack_id": pack.context_pack_id,
                    "pack_hash": pack.pack_hash,
                    "injection_hash": pack.injection_hash,
                    "provider_messages_hash": pack.provider_messages_hash,
                    "knowledge_fingerprint": pack.knowledge_fingerprint,
                    "project_kb_state_id": pack.project_kb_state_id,
                    "project_kb_manifest_hash": pack.project_kb_manifest_hash,
                }
                binding_receipts = connection.execute(
                    """
                    SELECT content_hash, metadata_json FROM artifact
                    WHERE run_id = ? AND artifact_type = 'context_pack'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if len(binding_receipts) != 1:
                    raise AgentContextPackConflict(
                        "Run must have exactly one Context Pack binding receipt",
                        code="context_injection_integrity_failed",
                    )
                try:
                    binding_metadata = json.loads(
                        str(binding_receipts[0]["metadata_json"])
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AgentContextPackConflict(
                        "Context Pack binding receipt is invalid",
                        code="context_injection_integrity_failed",
                    ) from exc
                if (
                    str(binding_receipts[0]["content_hash"]) != pack.pack_hash
                    or binding_metadata != expected_metadata
                ):
                    raise AgentContextPackConflict(
                        "Context Pack changed after the Run was bound",
                        code="context_injection_integrity_failed",
                    )
                existing_receipts = connection.execute(
                    """
                    SELECT content_hash, metadata_json FROM artifact
                    WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
                if existing_receipts:
                    if len(existing_receipts) != 1:
                        raise AgentContextPackConflict(
                            "Run must have exactly one Context injection receipt",
                            code="context_injection_integrity_failed",
                        )
                    existing_receipt = existing_receipts[0]
                    try:
                        receipt_metadata = json.loads(
                            str(existing_receipt["metadata_json"])
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise AgentContextPackConflict(
                            "Context injection receipt is invalid",
                            code="context_injection_integrity_failed",
                        ) from exc
                    if (
                        str(existing_receipt["content_hash"])
                        != pack.provider_messages_hash
                        or receipt_metadata != expected_metadata
                    ):
                        raise AgentContextPackConflict(
                            "Context injection receipt does not match the bound Pack",
                            code="context_injection_integrity_failed",
                        )
                    self._require_current_knowledge(row, knowledge_observation)
                    connection.commit()
                    return PreparedContextInjection(
                        context_pack_id=pack.context_pack_id,
                        pack_hash=pack.pack_hash,
                        injection_hash=pack.injection_hash,
                        provider_messages_hash=pack.provider_messages_hash,
                        messages=messages,
                    )

                if run_status is RunStatus.RUNNING:
                    raise AgentContextPackConflict(
                        "A resumed running Run requires an existing Context injection receipt",
                        code="context_injection_receipt_missing",
                    )
                self._require_current_knowledge(row, knowledge_observation)
                now = _now()
                artifact_id = _new_id("artifact")
                encoded_messages = _json(list(messages))
                connection.execute(
                    """
                    INSERT INTO artifact(
                        id, run_id, artifact_type, relative_path,
                        content_hash, metadata_json, created_at
                    ) VALUES (?, ?, 'context_injection_receipt', NULL, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        run_id,
                        pack.provider_messages_hash,
                        _json(expected_metadata),
                        now.isoformat(),
                    ),
                )
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.ARTIFACT,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        artifact_type="context_injection_receipt",
                        name="Provider Context injection receipt",
                        uri=f"runtime://context-packs/{pack.context_pack_id}/injection",
                        media_type="application/json",
                        content_hash=pack.provider_messages_hash,
                        size_bytes=len(encoded_messages.encode("utf-8")),
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence, now.isoformat(), run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PreparedContextInjection(
            context_pack_id=pack.context_pack_id,
            pack_hash=pack.pack_hash,
            injection_hash=pack.injection_hash,
            provider_messages_hash=pack.provider_messages_hash,
            messages=messages,
        )

    @staticmethod
    def _knowledge_target_from_row(row: sqlite3.Row) -> ContextPackKnowledgeTarget:
        knowledge_fingerprint = (
            str(row["knowledge_fingerprint"])
            if row["knowledge_fingerprint"] is not None
            else None
        )
        route_source = (
            str(row["nlcl_route_source"])
            if "nlcl_route_source" in row.keys()
            and row["nlcl_route_source"] is not None
            else None
        )
        return ContextPackKnowledgeTarget(
            context_pack_id=str(row["id"]),
            pack_hash=str(row["pack_hash"]),
            knowledge_fingerprint=knowledge_fingerprint,
            project_kb_knowledge_fingerprint=(
                knowledge_fingerprint
                if route_source == "native_knowledge"
                else None
            ),
            project_id=(
                str(row["project_binding_id"])
                if row["project_binding_id"] is not None
                else None
            ),
            project_kb_state_id=(
                str(row["project_kb_state_id"])
                if row["project_kb_state_id"] is not None
                else None
            ),
            project_kb_manifest_hash=(
                str(row["project_kb_manifest_hash"])
                if row["project_kb_manifest_hash"] is not None
                else None
            ),
            nlcl_route_source=route_source,
        )


    @staticmethod
    def _can_adopt_managed_o2e_session_executor(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        context_pack_id: str | None,
    ) -> bool:
        """Allow author confirm to rebind synthetic O2-E sessions to the live provider."""
        if (
            not session_id.startswith("session-o2e-")
            or context_pack_id is None
            or not context_pack_id.startswith("context-pack-o2e-")
        ):
            return False
        row = connection.execute(
            """
            SELECT context_pack.session_id,
                   nlcl_route_receipt.source AS nlcl_route_source
            FROM context_pack
            LEFT JOIN nlcl_route_receipt
              ON nlcl_route_receipt.context_pack_id = context_pack.id
            WHERE context_pack.id = ?
            """,
            (context_pack_id,),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["session_id"]) == session_id
            and row["nlcl_route_source"] == "external_distillation"
        )

    @staticmethod
    def _require_project_kb_binding(context_pack: sqlite3.Row) -> None:
        state_id = context_pack["project_kb_state_id"]
        manifest_hash = context_pack["project_kb_manifest_hash"]
        if state_id is None or manifest_hash is None:
            raise AgentContextPackConflict(
                "Legacy Context Pack has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )

    @classmethod
    def _require_run_project_kb_binding(
        cls,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> sqlite3.Row:
        context_pack_id = run["context_pack_id"]
        if context_pack_id is None:
            raise AgentContextPackConflict(
                "Managed workflow Run has no Context Pack",
                code="context_pack_project_kb_unbound",
            )
        context_pack = connection.execute(
            """
            SELECT context_pack.*,
                   nlcl_route_receipt.source AS nlcl_route_source
            FROM context_pack
            LEFT JOIN nlcl_route_receipt
              ON nlcl_route_receipt.context_pack_id = context_pack.id
            WHERE context_pack.id = ?
            """,
            (str(context_pack_id),),
        ).fetchone()
        if context_pack is None:
            raise AgentContextPackNotFound("Context Pack was not found")
        cls._require_project_kb_binding(context_pack)
        return context_pack

    @staticmethod
    def _require_current_knowledge(
        context_pack: sqlite3.Row,
        observation: KnowledgeFingerprintObservation | None,
    ) -> None:
        expected = (
            str(context_pack["knowledge_fingerprint"])
            if context_pack["knowledge_fingerprint"] is not None
            else None
        )
        if expected is None:
            return
        if observation is None:
            raise AgentContextPackSourceUnavailable(
                "Context Pack knowledge source could not be verified"
            )
        if (
            observation.context_pack_id != str(context_pack["id"])
            or observation.pack_hash != str(context_pack["pack_hash"])
        ):
            raise AgentContextPackConflict(
                "Knowledge observation does not match the bound Context Pack",
                code="context_pack_source_changed",
            )
        if observation.state is KnowledgeObservationState.UNAVAILABLE:
            raise AgentContextPackSourceUnavailable(
                "Context Pack knowledge source is temporarily unavailable"
            )
        if (
            observation.state is KnowledgeObservationState.CHANGED
            or observation.expected_fingerprint != expected
            or observation.observed_fingerprint != expected
        ):
            raise AgentContextPackConflict(
                "Context Pack knowledge source changed after review",
                code="context_pack_source_changed",
            )

    def transition_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        detail: str | None,
    ) -> Run:
        if status in TERMINAL_RUN_STATUSES:
            raise AgentStoreInvalidState(
                "Terminal Run states require the dedicated completion method"
            )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                current = RunStatus(str(run["status"]))
                if status == current:
                    connection.commit()
                    return self._run_from_row(run)
                if status not in RUN_TRANSITIONS[current]:
                    raise AgentStoreInvalidState(
                        f"Run cannot transition from {current.value} to {status.value}"
                    )
                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                causation_id = self._last_activity_id(connection, run_id)
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=status,
                        previous_status=current,
                        detail=detail,
                    ).model_dump(mode="json"),
                    causation_id=causation_id,
                    created_at=now,
                )
                started_at = run["started_at"]
                if status is not RunStatus.QUEUED and started_at is None:
                    started_at = now.isoformat()
                completed_at = now.isoformat() if status in TERMINAL_RUN_STATUSES else None
                connection.execute(
                    """
                    UPDATE run
                    SET status = ?, last_activity_sequence = ?, started_at = ?,
                        updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        sequence,
                        started_at,
                        now.isoformat(),
                        completed_at,
                        run_id,
                    ),
                )
                result = self._require_run_row(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(result)

    def append_assistant_delta(
        self,
        run_id: str,
        *,
        message_id: str,
        delta: str,
    ) -> ActivityItem:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) not in {RunStatus.RUNNING, RunStatus.CANCELLING}:
                    raise AgentStoreInvalidState("Assistant deltas require a running or cancelling Run")
                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                item = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.MESSAGE,
                    payload=MessagePayload(
                        role="assistant",
                        content=None,
                        name=None,
                        message_id=message_id,
                        phase="delta",
                        delta=delta,
                        format="markdown",
                        finish_reason=None,
                        usage=None,
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence, now.isoformat(), run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return item

    def append_reasoning(
        self,
        run_id: str,
        *,
        summary: str,
        content: str | None,
        block_id: str | None = None,
        phase: str = "completed",
        delta: str | None = None,
        source: str = "model",
        visibility: str = "full",
    ) -> ActivityItem:
        """Persist public reasoning deltas and terminal snapshots in the event log."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) not in {RunStatus.RUNNING, RunStatus.CANCELLING}:
                    raise AgentStoreInvalidState("Reasoning requires a running Run")
                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                item = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.REASONING,
                    payload=ReasoningPayload(
                        summary=summary,
                        content=content,
                        visibility=visibility,
                        block_id=block_id,
                        phase=phase,
                        delta=delta,
                        source=source,
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence, now.isoformat(), run_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return item

    def finalize_assistant_message(
        self,
        run_id: str,
        *,
        message_id: str,
        content: str,
        finish_reason: str = "length",
        goal_transcript: list[dict[str, object]] | None = None,
    ) -> AgentMessageResponse:
        """把一条已流式输出到一半的助手消息就地落成完整消息（Run 继续跑）。

        用于「输出被 token 上限截断后接着续写」：截断的前半段先成为一条
        真实消息（断线重连、历史回放都看得到），续写段是新的一条消息。
        """
        if not content.strip():
            raise AgentStoreInvalidState("A finalized message requires non-blank content")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentStoreInvalidState(
                        "Assistant message finalization requires a running Run"
                    )
                now = _now()
                sequence = int(run["last_activity_sequence"]) + 1
                completed = self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.MESSAGE,
                    payload=MessagePayload(
                        role="assistant",
                        content=content,
                        name=None,
                        message_id=message_id,
                        phase="completed",
                        delta=None,
                        format="markdown",
                        finish_reason=finish_reason,
                        usage=None,
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                session = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (str(run["session_id"]),),
                ).fetchone()
                if session is None:  # pragma: no cover - foreign key invariant.
                    raise RuntimeError("Run session disappeared")
                message_ordinal = int(session["last_message_ordinal"]) + 1
                connection.execute(
                    """
                    INSERT INTO session_message(
                        activity_id, session_id, run_id, message_id, ordinal,
                        role, format, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'assistant', 'markdown', ?)
                    """,
                    (
                        completed.root.activity_id,
                        str(run["session_id"]),
                        run_id,
                        message_id,
                        message_ordinal,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE run
                    SET last_activity_sequence = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (sequence, now.isoformat(), run_id),
                )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET last_message_ordinal = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (message_ordinal, now.isoformat(), str(run["session_id"])),
                )
                message_row = connection.execute(
                    """
                    SELECT sm.*, a.payload_json
                    FROM session_message AS sm
                    JOIN activity AS a ON a.id = sm.activity_id
                    WHERE sm.activity_id = ?
                    """,
                    (completed.root.activity_id,),
                ).fetchone()
                if goal_transcript is not None:
                    GoalStore.stage(connection, run_id)
                    self._save_run_transcript_in_connection(connection, run_id, goal_transcript)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._message_from_row(message_row)

    def complete_run(
        self,
        run_id: str,
        *,
        message_id: str,
        content: str,
        finish_reason: str,
        usage: MessageUsage | None,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
        allow_truncated_finish: bool = False,
        transcript: list[dict[str, object]] | None = None,
    ) -> tuple[Run, AgentMessageResponse]:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if current_project_kb_verifier is not None:
                    run = self._require_run_row(connection, run_id)
                    context_pack = self._require_run_project_kb_binding(connection, run)
                    current_project_kb_verifier(
                        self._knowledge_target_from_row(context_pack),
                        connection,
                    )
                result, message_row = self._complete_run_in_connection(
                    connection,
                    run_id,
                    message_id=message_id,
                    content=content,
                    finish_reason=finish_reason,
                    usage=usage,
                    allow_truncated_finish=allow_truncated_finish,
                )
                if transcript is not None:
                    self._save_run_transcript_in_connection(connection, run_id, transcript)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(result), self._message_from_row(message_row)

    def complete_run_with_candidate_output(
        self,
        candidate: AgentCandidateOutput,
        *,
        candidate_tool_call_id: str,
        candidate_tool_output: Any = None,
        candidate_tool_duration_ms: int | None = None,
        message_id: str,
        usage: MessageUsage | None,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
    ) -> tuple[Run, AgentMessageResponse, AgentCandidateOutput]:
        """Persist a recovery cutpoint, then finalize it in a separate transaction."""

        stored_candidate = self.persist_candidate_recovery_cutpoint(
            candidate,
            candidate_tool_call_id=candidate_tool_call_id,
            candidate_tool_output=candidate_tool_output,
            candidate_tool_duration_ms=candidate_tool_duration_ms,
            current_project_kb_verifier=current_project_kb_verifier,
        )
        return self.finalize_persisted_candidate(
            candidate.run_id,
            expected_candidate_output_id=stored_candidate.candidate_output_id,
            expected_candidate_output_hash=stored_candidate.envelope_hash,
            message_id=message_id,
            usage=usage,
            current_project_kb_verifier=current_project_kb_verifier,
        )

    def finalize_persisted_candidate(
        self,
        run_id: str,
        *,
        expected_candidate_output_id: str,
        expected_candidate_output_hash: str,
        message_id: str,
        usage: MessageUsage | None,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
    ) -> tuple[Run, AgentMessageResponse, AgentCandidateOutput]:
        """Complete a live Run from its cutpoint without any Provider replay."""

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                if current_project_kb_verifier is not None:
                    context_pack = self._require_run_project_kb_binding(connection, run)
                    current_project_kb_verifier(
                        self._knowledge_target_from_row(context_pack),
                        connection,
                    )
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentStoreInvalidState(
                        "Only a running Run can finalize a persisted candidate"
                    )
                if self._read_provider_recovery_dossier(connection, run_id) is not None:
                    raise AgentStoreInvalidState(
                        "A Run with a recovery dossier requires an author decision"
                    )
                candidate_row = connection.execute(
                    "SELECT * FROM agent_candidate_output WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if candidate_row is None:
                    raise AgentStoreInvalidState(
                        "Candidate finalization requires a persisted candidate"
                    )
                stored_candidate = self._agent_candidate_output_from_row(candidate_row)
                if not isinstance(stored_candidate, AgentCandidateOutput):
                    raise AgentStoreInvalidState(
                        "Candidate finalization requires a current v3 candidate"
                    )
                self._validate_agent_candidate_output_binding(
                    connection,
                    run,
                    stored_candidate,
                )
                if (
                    stored_candidate.candidate_output_id
                    != expected_candidate_output_id
                    or stored_candidate.envelope_hash
                    != expected_candidate_output_hash
                ):
                    raise AgentStoreInvalidState(
                        "Persisted candidate changed before finalization"
                    )
                self._candidate_submission_tool_call_in_connection(
                    connection,
                    stored_candidate,
                    require_succeeded=True,
                )
                result_rows = connection.execute(
                    """
                    SELECT id FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
                    """,
                    (run_id,),
                ).fetchall()
                if result_rows:
                    raise AgentStoreInvalidState(
                        "A running candidate cutpoint cannot already own a Writer result"
                    )
                writer_result = self._build_writer_candidate_result_in_connection(
                    connection,
                    run,
                    stored_candidate,
                    created_at=_now(),
                )
                self._create_writer_candidate_result_in_connection(
                    connection,
                    run,
                    stored_candidate,
                    writer_result,
                )
                result, message_row = self._complete_run_in_connection(
                    connection,
                    run_id,
                    message_id=message_id,
                    content=self._candidate_assistant_content(stored_candidate),
                    finish_reason="stop",
                    usage=usage,
                )
                self._recover_run_transcript_in_connection(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return (
            self._run_from_row(result),
            self._message_from_row(message_row),
            stored_candidate,
        )

    def _create_writer_candidate_result_in_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        candidate: AgentCandidateOutput,
        result: WriterCandidateResult,
    ) -> WriterCandidateResult:
        try:
            result = WriterCandidateResult.model_validate_json(
                _json(result.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentStoreInvalidState(
                "Writer candidate result failed immutable validation"
            ) from exc
        if result.run_id != str(run["id"]):
            raise AgentStoreInvalidState(
                "Writer candidate result belongs to another Run"
            )
        task_rows = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ? AND artifact_type = 'writer_task'
            ORDER BY id
            """,
            (result.run_id,),
        ).fetchall()
        if len(task_rows) != 1:
            raise AgentStoreInvalidState(
                "Writer candidate result requires exactly one Writer task"
            )
        task = self._writer_task_from_artifact(task_rows[0])
        change_sets = self._writer_change_sets_from_connection(
            connection,
            result.run_id,
        )
        if task.mode == "author_review_change_proposal":
            if len(change_sets) != 1:
                raise AgentStoreInvalidState(
                    "Writer candidate result requires exactly one same-Run ChangeSet"
                )
            change_set = change_sets[0]
        else:
            if change_sets:
                raise AgentStoreInvalidState(
                    "Repair-only Writer result cannot own a ChangeSet"
                )
            change_set = None
        try:
            expected = build_writer_candidate_result(
                task,
                candidate,
                change_set=change_set,
                created_at=result.created_at,
            )
        except ValueError as exc:
            raise AgentStoreInvalidState(
                "Writer candidate result does not match Runtime evidence"
            ) from exc
        if expected != result:
            raise AgentStoreInvalidState(
                "Writer candidate result does not match Runtime evidence"
            )
        existing = connection.execute(
            """
            SELECT * FROM artifact
            WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
            """,
            (result.run_id,),
        ).fetchall()
        if existing:
            if len(existing) != 1:
                raise AgentStoreInvalidState(
                    "Run must have exactly one Writer candidate result"
                )
            stored = self._writer_candidate_result_from_artifact(existing[0])
            if stored != result:
                raise AgentStoreInvalidState(
                    "Run already owns a different Writer candidate result"
                )
            return stored

        payload_json = _json(result.model_dump(mode="json", by_alias=True))
        connection.execute(
            """
            INSERT INTO artifact(
                id, run_id, artifact_type, relative_path,
                content_hash, metadata_json, created_at
            ) VALUES (?, ?, 'writer_candidate_result', NULL, ?, ?, ?)
            """,
            (
                result.writer_candidate_result_id,
                result.run_id,
                result.result_hash,
                payload_json,
                result.created_at.isoformat(),
            ),
        )
        current_run = self._require_run_row(connection, result.run_id)
        sequence = int(current_run["last_activity_sequence"]) + 1
        self._insert_activity(
            connection,
            current_run,
            sequence=sequence,
            kind=ActivityKind.ARTIFACT,
            payload=ArtifactPayload(
                artifact_id=result.writer_candidate_result_id,
                artifact_type="writer_candidate_result",
                name="Writer candidate result",
                uri=f"runtime://agent/runs/{result.run_id}/writer-candidate-result",
                media_type="application/json",
                content_hash=result.result_hash,
                size_bytes=len(payload_json.encode("utf-8")),
            ).model_dump(mode="json"),
            causation_id=self._last_activity_id(connection, result.run_id),
            created_at=result.created_at,
        )
        connection.execute(
            "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
            (sequence, result.created_at.isoformat(), result.run_id),
        )
        return result

    def _complete_run_in_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        message_id: str,
        content: str,
        finish_reason: str,
        usage: MessageUsage | None,
        allow_recovery: bool = False,
        allow_truncated_finish: bool = False,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not content.strip():
            raise AgentStoreInvalidState("A successful Run requires non-blank output")
        if finish_reason in {"length", "tool_calls", "function_call"} and not (
            allow_truncated_finish and finish_reason == "length"
        ):
            # length 只在一种情况下放行：收尾轮的输出也被 token 上限掐断，
            # 与其把已生成的正文盖进失败卡，不如如实按截断收场（事件里有记录）。
            raise AgentStoreInvalidState(
                f"Run cannot succeed with finish reason {finish_reason!r}"
            )
        run = self._require_run_row(connection, run_id)
        current = RunStatus(str(run["status"]))
        if current is not RunStatus.RUNNING and not (
            allow_recovery
            and current in {RunStatus.INTERRUPTED, RunStatus.FAILED, RunStatus.PAUSED}
        ):
            raise AgentStoreInvalidState("Only a running Run can complete")
        if connection.execute(
            "SELECT 1 FROM session_steering WHERE run_id = ? AND status = 'pending' LIMIT 1", (run_id,),
        ).fetchone():
            raise AgentSteeringPending("这轮刚收到补充消息，接着处理后再收尾。")
        from zhimeng_runtime.command_completion import CommandCompletionStore
        if CommandCompletionStore(self.database).pending(str(run["session_id"]), connection=connection):
            raise AgentSteeringPending("后台命令已完成，接着处理结果后再收尾。")
        GoalStore.finish(connection, run_id)
        managed_workflow = connection.execute(
            """
            SELECT * FROM checkpoint
            WHERE run_id = ? AND sequence = 0 AND checkpoint_type = ?
            LIMIT 1
            """,
            (run_id, WORKFLOW_PLAN_CHECKPOINT_TYPE),
        ).fetchone()
        if managed_workflow is not None:
            # 交了候选就按原合同全量校验绑定；没交候选视为纯对话收尾，
            # 同样合法——不再强制每次创作工作流都以信封结束。
            candidate_row = connection.execute(
                "SELECT * FROM agent_candidate_output WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if candidate_row is not None:
                candidate = self._agent_candidate_output_from_row(candidate_row)
                self._validate_agent_candidate_output_binding(
                    connection,
                    run,
                    candidate,
                )
                if not isinstance(candidate, AgentCandidateOutput):
                    raise AgentStoreInvalidState(
                        "A current Creation Writer Run requires a v3 candidate output"
                    )
                writer_task_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_task'
                    """,
                    (run_id,),
                ).fetchall()
                if len(writer_task_rows) != 1:
                    raise AgentStoreInvalidState(
                        "A Writer Run requires exactly one Writer task"
                    )
                self._writer_task_from_artifact(writer_task_rows[0])
                writer_result_rows = connection.execute(
                    """
                    SELECT * FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_candidate_result'
                    """,
                    (run_id,),
                ).fetchall()
                if len(writer_result_rows) != 1:
                    raise AgentStoreInvalidState(
                        "A Writer Run requires exactly one Writer candidate result"
                    )
                self._writer_candidate_result_from_artifact(writer_result_rows[0])
        open_tool_call = connection.execute(
            """
            SELECT id FROM tool_call
            WHERE run_id = ? AND status IN ('pending', 'running')
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if open_tool_call is not None:
            raise AgentStoreInvalidState(
                "A successful Run cannot contain an unfinished ToolCall"
            )
        now = _now()
        sequence = int(run["last_activity_sequence"]) + 1
        completed = self._insert_activity(
            connection,
            run,
            sequence=sequence,
            kind=ActivityKind.MESSAGE,
            payload=MessagePayload(
                role="assistant",
                content=content,
                name=None,
                message_id=message_id,
                phase="completed",
                delta=None,
                format="markdown",
                finish_reason=finish_reason,
                usage=usage,
            ).model_dump(mode="json"),
            causation_id=self._last_activity_id(connection, run_id),
            created_at=now,
        )
        session = connection.execute(
            "SELECT * FROM execution_session WHERE id = ?",
            (str(run["session_id"]),),
        ).fetchone()
        if session is None:  # pragma: no cover - foreign key invariant.
            raise RuntimeError("Run session disappeared")
        message_ordinal = int(session["last_message_ordinal"]) + 1
        connection.execute(
            """
            INSERT INTO session_message(
                activity_id, session_id, run_id, message_id, ordinal,
                role, format, created_at
            ) VALUES (?, ?, ?, ?, ?, 'assistant', 'markdown', ?)
            """,
            (
                completed.root.activity_id,
                str(run["session_id"]),
                run_id,
                message_id,
                message_ordinal,
                now.isoformat(),
            ),
        )
        terminal_sequence = sequence + 1
        self._insert_activity(
            connection,
            run,
            sequence=terminal_sequence,
            kind=ActivityKind.RUN_STATE,
            payload=RunStatePayload(
                status=RunStatus.SUCCEEDED,
                previous_status=current,
                detail="Assistant message completed and was persisted",
            ).model_dump(mode="json"),
            causation_id=completed.root.activity_id,
            created_at=now,
        )
        connection.execute(
            """
            UPDATE run
            SET status = 'succeeded', last_activity_sequence = ?,
                error_json = NULL, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (terminal_sequence, now.isoformat(), now.isoformat(), run_id),
        )
        connection.execute(
            """
            UPDATE execution_session
            SET status = 'idle', revision = revision + 1,
                last_message_ordinal = ?, updated_at = ?
            WHERE id = ?
            """,
            (message_ordinal, now.isoformat(), str(run["session_id"])),
        )
        result = self._require_run_row(connection, run_id)
        message_row = self._message_row_for_run(connection, run_id, "assistant")
        return result, message_row

    def fail_run(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: dict | None = None,
    ) -> Run:
        return self._finish_without_message(
            run_id,
            status=RunStatus.FAILED,
            detail=message,
            error=ErrorEnvelope(
                error=ErrorDetail(
                    code=code,
                    message=message,
                    request_id=run_id,
                    retryable=retryable,
                    details=details,
                )
            ),
        )

    def cancel_run(self, run_id: str) -> Run:
        detail = "Cancellation requested by the author"
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                current = RunStatus(str(run["status"]))
                if current in TERMINAL_RUN_STATUSES or current is RunStatus.CANCELLING:
                    connection.commit()
                    return self._run_from_row(run)
                if RunStatus.CANCELLING not in RUN_TRANSITIONS[current]:
                    raise AgentStoreInvalidState(
                        f"Run cannot transition from {current.value} to cancelling"
                    )
                now = _now()
                from zhimeng_runtime.command_completion import CommandCompletionStore
                CommandCompletionStore(self.database).bump_stop_generation(str(run["session_id"]), connection=connection)
                GoalStore.interrupt(connection, run_id, "用户暂停了执行。", source="user")
                run = self._close_open_tool_calls(
                    connection,
                    run,
                    target_status=RunStatus.CANCELLING,
                    detail=detail,
                    run_error=None,
                    created_at=now,
                )
                sequence = int(run["last_activity_sequence"]) + 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=RunStatus.CANCELLING,
                        previous_status=current,
                        detail=detail,
                    ).model_dump(mode="json"),
                    causation_id=self._last_activity_id(connection, run_id),
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET status = 'cancelling', last_activity_sequence = ?,
                        started_at = COALESCE(started_at, created_at), updated_at = ?
                    WHERE id = ?
                    """,
                    (sequence, now.isoformat(), run_id),
                )
                result = self._require_run_row(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(result)

    def mark_cancelled(self, run_id: str) -> Run:
        return self._finish_without_message(
            run_id,
            status=RunStatus.CANCELLED,
            detail="Agent run cancelled",
            error=None,
        )

    def mark_interrupted(self, run_id: str, *, detail: str) -> Run:
        return self._finish_without_message(
            run_id,
            status=RunStatus.INTERRUPTED,
            detail=detail,
            error=None,
        )

    def recover_interrupted(self) -> int:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM run
                WHERE status IN (
                    'queued', 'starting', 'running', 'awaiting_approval',
                    'cancelling'
                )
                  AND (
                    run.status IN ('awaiting_approval', 'cancelling')
                    OR NOT EXISTS (
                      SELECT 1 FROM checkpoint
                      WHERE checkpoint.run_id = run.id
                        AND checkpoint.sequence = 0
                        AND checkpoint.checkpoint_type = ?
                    )
                  )
                ORDER BY created_at, id
                """,
                (WORKFLOW_PLAN_CHECKPOINT_TYPE,),
            ).fetchall()
        count = 0
        for row in rows:
            try:
                self._finish_without_message(
                    str(row["id"]),
                    status=RunStatus.INTERRUPTED,
                    detail="Runtime restarted before the Run reached a terminal state",
                    error=None,
                )
            except AgentStoreInvalidState:
                continue
            count += 1
        return count

    @staticmethod
    def _tool_result_hash(
        result_payload: ToolResultPayload,
        change_proposal: ChangeProposalPayload | None,
        skill_call: SkillCallPayload | None = None,
        artifacts: tuple[ArtifactPayload, ...] = (),
    ) -> str:
        return _sha256_json(
            {
                "schema": "zhimeng.tool-result-identity.v1",
                "tool_result": result_payload.model_dump(mode="json", by_alias=True),
                "change_proposal": (
                    change_proposal.model_dump(mode="json", by_alias=True)
                    if change_proposal is not None
                    else None
                ),
                "skill_call": (
                    skill_call.model_dump(mode="json", by_alias=True)
                    if skill_call is not None
                    else None
                ),
                "artifacts": [
                    artifact.model_dump(mode="json", by_alias=True)
                    for artifact in artifacts
                ],
            }
        )

    def _finish_tool_call_in_transaction(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        tool_call: sqlite3.Row,
        *,
        result_payload: ToolResultPayload,
        result_hash: str,
        change_proposal: ChangeProposalPayload | None,
        skill_call: SkillCallPayload | None = None,
        artifacts: tuple[ArtifactPayload, ...] = (),
        created_at: datetime,
    ) -> None:
        sequence = int(run["last_activity_sequence"]) + 1
        causation_id = (
            str(tool_call["call_activity_id"])
            if tool_call["call_activity_id"] is not None
            else self._last_activity_id(connection, str(run["id"]))
        )
        if skill_call is not None:
            skill_activity = self._insert_activity(
                connection,
                run,
                sequence=sequence,
                kind=ActivityKind.SKILL_CALL,
                payload=skill_call.model_dump(mode="json", by_alias=True),
                causation_id=causation_id,
                created_at=created_at,
            )
            causation_id = skill_activity.root.activity_id
            sequence += 1

        for artifact in artifacts:
            connection.execute(
                """
                INSERT INTO artifact(
                    id, run_id, artifact_type, relative_path,
                    content_hash, metadata_json, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    str(run["id"]),
                    artifact.artifact_type,
                    artifact.content_hash,
                    _json(artifact.model_dump(mode="json", by_alias=True)),
                    created_at.isoformat(),
                ),
            )
            artifact_activity = self._insert_activity(
                connection,
                run,
                sequence=sequence,
                kind=ActivityKind.ARTIFACT,
                payload=artifact.model_dump(mode="json", by_alias=True),
                causation_id=causation_id,
                created_at=created_at,
            )
            causation_id = artifact_activity.root.activity_id
            sequence += 1

        result_activity = self._insert_activity(
            connection,
            run,
            sequence=sequence,
            kind=ActivityKind.TOOL_RESULT,
            payload=result_payload.model_dump(mode="json", by_alias=True),
            causation_id=causation_id,
            created_at=created_at,
        )
        last_activity_id = result_activity.root.activity_id
        if change_proposal is not None:
            sequence += 1
            proposal_activity = self._insert_activity(
                connection,
                run,
                sequence=sequence,
                kind=ActivityKind.CHANGE_PROPOSAL,
                payload=change_proposal.model_dump(mode="json", by_alias=True),
                causation_id=last_activity_id,
                created_at=created_at,
                revision_override=(
                    (change_proposal.base_revision, change_proposal.base_fingerprint)
                    if run["project_revision"] is not None
                    and change_proposal.base_revision != int(run["project_revision"])
                    else None
                ),
            )
            last_activity_id = proposal_activity.root.activity_id

        connection.execute(
            """
            UPDATE tool_call
            SET status = ?, output_json = ?, error_json = ?,
                result_activity_id = ?, result_hash = ?, duration_ms = ?,
                finished_at = ?
            WHERE id = ? AND run_id = ?
            """,
            (
                result_payload.status,
                _json(result_payload.output),
                (
                    _json(result_payload.error.model_dump(mode="json", by_alias=True))
                    if result_payload.error is not None
                    else None
                ),
                result_activity.root.activity_id,
                result_hash,
                result_payload.duration_ms,
                created_at.isoformat(),
                str(tool_call["id"]),
                str(run["id"]),
            ),
        )
        connection.execute(
            "UPDATE run SET last_activity_sequence = ?, updated_at = ? WHERE id = ?",
            (sequence, created_at.isoformat(), str(run["id"])),
        )

    def _close_open_tool_calls(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        *,
        target_status: RunStatus,
        detail: str,
        run_error: ErrorEnvelope | None,
        created_at: datetime,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT * FROM tool_call
            WHERE run_id = ? AND (
                status IN ('pending', 'running')
                OR (status IN ('failed', 'cancelled')
                    AND tool_name IN ('write_file', 'edit_file', 'project_write_file', 'project_edit_file'))
            )
            ORDER BY started_at, id
            """,
            (str(run["id"]),),
        ).fetchall()
        for tool_call in rows:
            tool_call_id = str(tool_call["id"])
            known_write = self._known_direct_write(connection, tool_call)
            if known_write is not None:
                self._finish_tool_call_in_transaction(
                    connection, run, tool_call, result_payload=known_write,
                    result_hash=self._tool_result_hash(known_write, None),
                    change_proposal=None, created_at=created_at,
                )
                run = self._require_run_row(connection, str(run["id"]))
                continue
            if str(tool_call["status"]) not in {"pending", "running"}:
                continue
            known_proposal = self._known_change_proposal(connection, tool_call)
            if known_proposal is not None:
                result_payload, change_proposal = known_proposal
                self._finish_tool_call_in_transaction(
                    connection,
                    run,
                    tool_call,
                    result_payload=result_payload,
                    result_hash=self._tool_result_hash(
                        result_payload,
                        change_proposal,
                    ),
                    change_proposal=change_proposal,
                    created_at=created_at,
                )
                run = self._require_run_row(connection, str(run["id"]))
                continue
            if target_status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                result_payload = ToolResultPayload(
                    tool_call_id=tool_call_id,
                    status="cancelled",
                    output=(
                        {"reason": detail, "outcome": "unknown", "may_have_side_effects": True,
                         "retryable": False}
                        if str(tool_call["tool_name"]) == "computer_run_command"
                        else {"reason": detail}
                    ),
                    error=None,
                    duration_ms=None,
                )
            else:
                retryable = target_status is RunStatus.INTERRUPTED or bool(
                    run_error and run_error.error.retryable
                )
                if str(tool_call["tool_name"]) == "computer_run_command":
                    # A process may have changed external state before its result
                    # was saved. Never advertise an unobserved command as retryable.
                    retryable = False
                code = (
                    "tool_execution_interrupted"
                    if target_status is RunStatus.INTERRUPTED
                    else "tool_execution_aborted"
                )
                tool_error = ErrorEnvelope(
                    error=ErrorDetail(
                        code=code,
                        message=detail,
                        request_id=tool_call_id,
                        retryable=retryable,
                        details={"run_status": target_status.value},
                    )
                )
                result_payload = ToolResultPayload(
                    tool_call_id=tool_call_id,
                    status="failed",
                    output=None,
                    error=tool_error,
                    duration_ms=None,
                )
            self._finish_tool_call_in_transaction(
                connection,
                run,
                tool_call,
                result_payload=result_payload,
                result_hash=self._tool_result_hash(result_payload, None),
                change_proposal=None,
                created_at=created_at,
            )
            run = self._require_run_row(connection, str(run["id"]))
        return run

    @staticmethod
    def _known_direct_write(
        connection: sqlite3.Connection,
        tool_call: sqlite3.Row,
    ) -> ToolResultPayload | None:
        """Recover a committed file effect when only its ToolResult was lost."""
        # 9-26 起叫 write_file / edit_file；旧对话里是 project_write_file / project_edit_file。
        if str(tool_call["tool_name"]) not in {"write_file", "edit_file", "project_write_file", "project_edit_file"}:
            return None
        try:
            previous = json.loads(tool_call["output_json"] or "null")
            arguments = json.loads(tool_call["input_json"])
        except (TypeError, ValueError):
            return None
        if isinstance(previous, dict) and previous.get("incomplete") is True:
            return None
        from zhimeng_runtime.change_set_service import ChangeSetService

        change_set_id = ChangeSetService._agent_change_set_id(
            str(tool_call["run_id"]), str(tool_call["id"])
        )
        row = connection.execute(
            "SELECT payload_json FROM change_set WHERE id = ?", (change_set_id,)
        ).fetchone()
        if row is None:
            return AgentStore._known_journal_write(connection, tool_call, arguments)
        try:
            record = ChangeSetResponse.model_validate_json(str(row["payload_json"]))
        except ValueError:
            return None
        if (record.source_run_id != str(tool_call["run_id"])
                or record.source_tool_call_id != str(tool_call["id"])
                or record.source_request_hash != str(tool_call["request_hash"])
                or record.writeback is None
                or record.status.value not in {"applied", "rolled_back"}):
            return None
        manifest = record.rollback if record.status.value == "rolled_back" else record.writeback
        if manifest is None:
            return None
        requested = arguments.get("files") if isinstance(arguments, dict) else None
        if isinstance(requested, list):
            return AgentStore._known_batch_write(connection, tool_call, record, requested)
        if len(record.changes) != 1:
            return None
        change = record.changes[0]
        output = {
            "schema": "zhimeng.tool-result.project-write-file.v1",
            "change_set_id": record.change_set_id,
            "status": record.status.value,
            "operation": "created" if change.operation.value == "create" else "updated",
            "relative_path": change.relative_path,
            "applied": record.status.value == "applied",
            "result_revision": manifest.result_revision,
            "result_fingerprint": manifest.result_fingerprint,
            "recovered": True,
            "message": ("文件已保存，已从修改记录恢复执行结果。" if record.status.value == "applied"
                        else "这次文件修改曾保存，随后已撤回。"),
        }
        return ToolResultPayload(
            tool_call_id=str(tool_call["id"]), status="succeeded", output=output,
            error=None, duration_ms=None,
        )

    @staticmethod
    def _known_journal_write(connection, tool_call, arguments) -> ToolResultPayload | None:
        """A no-project single-file write has a journal receipt, not a ChangeSet."""
        if not isinstance(arguments, dict) or isinstance(arguments.get("files"), list):
            return None
        path = arguments.get("path")
        if not isinstance(path, str):
            return None
        rows = connection.execute(
            "SELECT * FROM file_change WHERE run_id = ? AND tool_call_id = ? ORDER BY seq",
            (str(tool_call["run_id"]), str(tool_call["id"])),
        ).fetchall()
        if len(rows) != 1 or rows[0]["apply_phase"] != "applied":
            return None
        row = rows[0]
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(row["path"])):
            return None
        applied = row["undone_at"] is None
        output = {
            "schema": "zhimeng.tool-result.file-change.v1", "change_id": row["change_id"],
            "path": row["path"], "operation": "created" if row["operation"] == "create" else "updated",
            "files": [{"path": row["path"], "operation": row["operation"]}],
            "applied": applied, "status": "applied" if applied else "rolled_back", "recovered": True,
            "message": "已从修改记录恢复此前保存的结果；文件当前内容可重新读取核对。",
        }
        return ToolResultPayload(tool_call_id=str(tool_call["id"]), status="succeeded",
                                 output=output, error=None, duration_ms=None)

    @staticmethod
    def _known_batch_write(
        connection: sqlite3.Connection,
        tool_call: sqlite3.Row,
        record: ChangeSetResponse,
        requested: list[Any],
    ) -> ToolResultPayload | None:
        """Recover each committed destination; an absent receipt is not success."""
        manifest = record.rollback if record.status.value == "rolled_back" else record.writeback
        if manifest is None:
            return None
        binding = connection.execute(
            "SELECT root_path FROM project_binding WHERE id = ?", (record.project_id,),
        ).fetchone()
        if binding is None:
            return None
        root = Path(binding["root_path"])
        def path_key(path: Path) -> str:
            return os.path.normpath(str(path)).replace("\\", "/").casefold()

        files = [{"relative_path": item.relative_path,
                  "operation": "created" if item.operation.value == "create" else "updated"}
                 for item in record.changes]
        confirmed = {path_key(root / item.relative_path) for item in record.changes}
        output: dict[str, Any] = {
            "schema": "zhimeng.tool-result.project-write-file.v1",
            "change_set_id": record.change_set_id,
            "status": record.status.value, "operation": "written", "files": files,
            "applied": record.status.value == "applied",
            "result_revision": manifest.result_revision,
            "result_fingerprint": manifest.result_fingerprint, "recovered": True,
        }
        from zhimeng_runtime.file_journal import FileChangeJournal
        journal_rows = connection.execute(
            "SELECT * FROM file_change WHERE run_id = ? AND tool_call_id = ? ORDER BY seq",
            (str(tool_call["run_id"]), str(tool_call["id"])),
        ).fetchall()
        if journal_rows:
            change = FileChangeJournal._change_from_rows(journal_rows)
            complete = change.apply_phase == "applied"
            output["outside_open_folder"] = {
                "change_id": change.change_id,
                "applied": complete and change.undone_at is None,
                "status": "rolled_back" if change.undone_at is not None else "applied" if complete else "unconfirmed",
                "files": [{"path": entry.path, "operation": entry.operation,
                           **({"status": "rolled_back"} if entry.undone_at is not None else {})}
                          for entry in change.entries],
            }
            if complete:
                confirmed.update(path_key(Path(entry.path)) for entry in change.entries)
        unconfirmed = []
        for item in requested:
            raw = item.get("path", item.get("relative_path")) if isinstance(item, dict) else None
            if not isinstance(raw, str):
                return None
            path = Path(raw)
            absolute = path if path.is_absolute() else root / path
            if path_key(absolute) not in confirmed:
                unconfirmed.append(raw)
        output["incomplete"] = bool(unconfirmed)
        output["unconfirmed_paths"] = unconfirmed
        output["message"] = (
            f"已恢复 {len(confirmed)} 个文件的保存记录；另外 {len(unconfirmed)} 个文件未确认，请重新读取核对。"
            if unconfirmed else "本批文件的保存记录已恢复；后续撤回状态以各条修改记录为准。"
        )
        status = "succeeded"
        error = None
        if unconfirmed:
            status = "cancelled" if tool_call["status"] == "cancelled" else "failed"
            if status == "failed":
                error = ErrorEnvelope(error=ErrorDetail(
                    code="agent_tool_batch_incomplete", message=output["message"],
                    request_id=str(tool_call["id"]), retryable=False,
                ))
        return ToolResultPayload(
            tool_call_id=str(tool_call["id"]), status=status, output=output,
            error=error, duration_ms=None,
        )

    @staticmethod
    def _known_change_proposal(
        connection: sqlite3.Connection,
        tool_call: sqlite3.Row,
    ) -> tuple[ToolResultPayload, ChangeProposalPayload] | None:
        if str(tool_call["tool_name"]) != "change_set_propose":
            return None
        row = connection.execute(
            """
            SELECT cs.payload_json, provenance.request_hash
            FROM change_set_provenance AS provenance
            JOIN change_set AS cs ON cs.id = provenance.change_set_id
            WHERE provenance.tool_call_id = ?
              AND provenance.run_id = ?
            """,
            (str(tool_call["id"]), str(tool_call["run_id"])),
        ).fetchone()
        if row is None or str(row["request_hash"]) != str(tool_call["request_hash"]):
            return None
        try:
            record = ChangeSetResponse.model_validate_json(str(row["payload_json"]))
        except ValueError:
            return None
        output = {
            "schema": "zhimeng.tool-result.change-set-propose.v1",
            "change_set_id": record.change_set_id,
            "change_set_hash": change_set_payload_hash(record),
            "status": record.status.value,
            "title": record.title,
            "file_count": len(record.changes),
            "hunk_count": sum(len(change.hunks) for change in record.changes),
            "base_revision": record.base_revision,
            "base_fingerprint": record.base_fingerprint,
            "requires_author_review": True,
            "applied": record.writeback is not None,
            "recovered": True,
        }
        result_payload = ToolResultPayload(
            tool_call_id=str(tool_call["id"]),
            status="succeeded",
            output=output,
            error=None,
            duration_ms=(
                int(tool_call["duration_ms"])
                if tool_call["duration_ms"] is not None
                else None
            ),
        )
        proposal = ChangeProposalPayload(
            proposal_id=record.change_set_id,
            title=record.title,
            summary=record.summary,
            base_revision=record.base_revision,
            base_fingerprint=record.base_fingerprint,
            files=[
                FileChange(
                    file_id=change.file_id,
                    path=change.relative_path,
                    operation=change.operation.value,
                    content_hash_before=change.before_sha256,
                    content_hash_after=change.proposed_after_sha256,
                )
                for change in record.changes
            ],
        )
        return result_payload, proposal

    def _close_open_reasoning_blocks(
        self, connection: sqlite3.Connection, run: sqlite3.Row, *, created_at: datetime,
    ) -> sqlite3.Row:
        """Close durable partial blocks when recovering a process that stopped mid-stream."""
        pending: dict[str, tuple[dict, list[str]]] = {}
        for row in connection.execute(
            "SELECT payload_json FROM activity WHERE run_id = ? AND kind = 'reasoning' ORDER BY sequence",
            (str(run["id"]),),
        ).fetchall():
            payload = json.loads(str(row["payload_json"]))["payload"]
            block_id = payload.get("block_id")
            if not block_id:
                continue
            if payload.get("phase") == "delta":
                pending.setdefault(block_id, (payload, []))[1].append(payload.get("delta") or "")
            else:
                pending.pop(block_id, None)
        sequence = int(run["last_activity_sequence"])
        for block_id, (payload, parts) in pending.items():
            sequence += 1
            self._insert_activity(
                connection, run, sequence=sequence, kind=ActivityKind.REASONING,
                payload=ReasoningPayload(
                    summary=payload["summary"], content="".join(parts),
                    visibility=payload["visibility"], block_id=block_id,
                    phase="interrupted", source=payload.get("source", "model"),
                ).model_dump(mode="json"),
                causation_id=self._last_activity_id(connection, str(run["id"])),
                created_at=created_at,
            )
        if pending:
            connection.execute(
                "UPDATE run SET last_activity_sequence = ? WHERE id = ?", (sequence, str(run["id"])),
            )
            return self._require_run_row(connection, str(run["id"]))
        return run

    def _finish_without_message(
        self,
        run_id: str,
        *,
        status: RunStatus,
        detail: str,
        error: ErrorEnvelope | None,
    ) -> Run:
        if status not in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
            raise ValueError("unsupported terminal status")
        if (status is RunStatus.FAILED) != (error is not None):
            raise ValueError("failed Runs require an error and other terminal states forbid it")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run_row(connection, run_id)
                current = RunStatus(str(run["status"]))
                if current is status:
                    connection.commit()
                    return self._run_from_row(run)
                if status not in RUN_TRANSITIONS[current]:
                    raise AgentStoreInvalidState(
                        f"Run cannot transition from {current.value} to {status.value}"
                    )
                now = _now()
                run = self._close_open_reasoning_blocks(connection, run, created_at=now)
                run = self._close_open_tool_calls(
                    connection,
                    run,
                    target_status=status,
                    detail=detail,
                    run_error=error,
                    created_at=now,
                )
                self._recover_run_transcript_in_connection(connection, run_id)
                GoalStore.interrupt(connection, run_id, detail)
                provider_boundary_exists = connection.execute(
                    """
                    SELECT 1 FROM checkpoint
                    WHERE run_id = ? AND checkpoint_type = 'provider_io_boundary'
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if provider_boundary_exists is not None:
                    self._ensure_provider_recovery_dossier_in_connection(
                        connection,
                        run,
                    )
                sequence = int(run["last_activity_sequence"]) + 1
                causation_id = self._last_activity_id(connection, run_id)
                if error is not None:
                    error_item = self._insert_activity(
                        connection,
                        run,
                        sequence=sequence,
                        kind=ActivityKind.ERROR,
                        payload=ErrorPayload(
                            error=error,
                            fatal=True,
                            scope="run",
                        ).model_dump(mode="json", by_alias=True),
                        causation_id=causation_id,
                        created_at=now,
                    )
                    causation_id = error_item.root.activity_id
                    sequence += 1
                self._insert_activity(
                    connection,
                    run,
                    sequence=sequence,
                    kind=ActivityKind.RUN_STATE,
                    payload=RunStatePayload(
                        status=status,
                        previous_status=current,
                        detail=detail,
                    ).model_dump(mode="json"),
                    causation_id=causation_id,
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE run
                    SET status = ?, last_activity_sequence = ?, error_json = ?,
                        started_at = COALESCE(started_at, created_at),
                        updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        sequence,
                        _json(error.model_dump(mode="json", by_alias=True)) if error else None,
                        now.isoformat(),
                        now.isoformat(),
                        run_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE execution_session
                    SET status = 'idle', revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), str(run["session_id"])),
                )
                result = self._require_run_row(connection, run_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(result)

    @staticmethod
    def _validated_workflow_plan(plan: WorkflowPlan) -> WorkflowPlan:
        try:
            return WorkflowPlan.model_validate_json(
                _json(plan.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan payload failed immutable validation",
                code="workflow_plan_invalid",
            ) from exc

    @staticmethod
    def _validated_workflow_step_checkpoint(
        checkpoint: WorkflowStepCheckpoint,
    ) -> WorkflowStepCheckpoint:
        try:
            return WorkflowStepCheckpoint.model_validate_json(
                _json(checkpoint.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint payload failed immutable validation",
                code="workflow_checkpoint_invalid",
            ) from exc

    @staticmethod
    def _validated_provider_io_boundary(
        boundary: ProviderIoBoundary,
    ) -> ProviderIoBoundary:
        try:
            return ProviderIoBoundary.model_validate_json(
                _json(boundary.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Provider I/O boundary payload failed immutable validation",
                code="workflow_checkpoint_invalid",
            ) from exc

    @staticmethod
    def _validated_provider_turn_receipt(
        receipt: ProviderTurnReceipt,
    ) -> ProviderTurnReceipt:
        try:
            return ProviderTurnReceipt.model_validate_json(
                _json(receipt.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise ProviderTurnReceiptConflict(
                "Provider turn receipt payload failed immutable validation",
                code="provider_turn_receipt_invalid",
            ) from exc

    def _provider_boundary_for_run(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> ProviderIoBoundary:
        rows = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
            (str(run["id"]),),
        ).fetchall()
        if not rows:
            raise ProviderTurnReceiptConflict(
                "Provider turn receipt requires a persisted Workflow boundary",
                code="provider_turn_receipt_boundary_missing",
            )
        _, chain = self._workflow_chain_from_rows(connection, run, rows)
        boundaries = tuple(item for item in chain if isinstance(item, ProviderIoBoundary))
        if len(boundaries) != 1 or not isinstance(chain[-1], ProviderIoBoundary):
            raise ProviderTurnReceiptConflict(
                "Provider turn receipt requires exactly one final Workflow boundary",
                code="provider_turn_receipt_boundary_missing",
            )
        return boundaries[0]

    def _provider_turn_receipts_from_connection(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        boundary: ProviderIoBoundary,
    ) -> tuple[ProviderTurnReceipt, ...]:
        rows = connection.execute(
            """
            SELECT sequence, payload_json, created_at
            FROM run_event
            WHERE run_id = ? AND event_type = 'provider_turn_receipt'
            ORDER BY sequence ASC
            """,
            (str(run["id"]),),
        ).fetchall()
        receipts: list[ProviderTurnReceipt] = []
        for expected_ordinal, row in enumerate(rows, start=1):
            try:
                receipt = ProviderTurnReceipt.model_validate_json(
                    str(row["payload_json"])
                )
                stored_time = _parse_time(row["created_at"])
            except (TypeError, ValueError) as exc:
                raise ProviderTurnReceiptConflict(
                    "Stored Provider turn receipt failed immutable validation",
                    code="provider_turn_receipt_integrity_failed",
                ) from exc
            if (
                receipt.run_id != str(run["id"])
                or receipt.turn_ordinal != expected_ordinal
                or receipt.created_at.astimezone(UTC) != stored_time
            ):
                raise ProviderTurnReceiptConflict(
                    "Stored Provider turn receipt row does not match its payload",
                    code="provider_turn_receipt_integrity_failed",
                )
            self._validate_provider_turn_receipt_binding(
                receipt,
                boundary=boundary,
                previous=tuple(receipts),
            )
            receipts.append(receipt)
        return tuple(receipts)

    @staticmethod
    def _validate_provider_turn_receipt_binding(
        receipt: ProviderTurnReceipt,
        *,
        boundary: ProviderIoBoundary,
        previous: tuple[ProviderTurnReceipt, ...],
    ) -> None:
        if (
            receipt.run_id != boundary.run_id
            or receipt.provider_boundary_id != boundary.boundary_id
            or receipt.provider_boundary_hash != boundary.boundary_hash
            or receipt.requested_model != boundary.provider_model
        ):
            raise ProviderTurnReceiptConflict(
                "Provider turn receipt does not bind the persisted dispatch boundary",
                code="provider_turn_receipt_boundary_mismatch",
            )
        if receipt.turn_ordinal == 1 and receipt.request_hash != boundary.request_hash:
            raise ProviderTurnReceiptConflict(
                "First Provider turn receipt does not bind the boundary request",
                code="provider_turn_receipt_request_mismatch",
            )
        if previous:
            prior = previous[-1]
            if receipt.turn_ordinal != prior.turn_ordinal + 1:
                raise ProviderTurnReceiptConflict(
                    "Provider turn receipt order is not contiguous",
                    code="provider_turn_receipt_order_invalid",
                )
            if receipt.accepted_reported_models != prior.accepted_reported_models:
                raise ProviderTurnReceiptConflict(
                    "Provider alias allowlist changed within one Run",
                    code="provider_turn_receipt_alias_changed",
                )

    @staticmethod
    def _validated_agent_candidate_output(
        candidate: AgentCandidateOutput,
    ) -> AgentCandidateOutput:
        try:
            return AgentCandidateOutput.model_validate_json(
                _json(candidate.model_dump(mode="json", by_alias=True))
            )
        except (TypeError, ValueError) as exc:
            raise AgentCandidateOutputConflict(
                "Agent candidate output failed immutable validation",
                code="agent_candidate_output_invalid",
            ) from exc

    @staticmethod
    def _agent_candidate_output_from_row(
        row: sqlite3.Row,
    ) -> AgentCandidateOutputRecord:
        try:
            candidate = parse_agent_candidate_output_json(str(row["payload_json"]))
            stored_created_at = _parse_time(row["created_at"])
            stored_failure_signatures = json.loads(
                str(row["failure_signatures_json"])
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentCandidateOutputConflict(
                "Stored Agent candidate output failed immutable validation",
                code="agent_candidate_output_integrity_failed",
            ) from exc
        scalar_pairs = (
            (row["id"], candidate.candidate_output_id),
            (row["run_id"], candidate.run_id),
            (row["workflow_plan_id"], candidate.workflow_plan_id),
            (row["workflow_plan_hash"], candidate.workflow_plan_hash),
            (row["final_checkpoint_id"], candidate.final_checkpoint_id),
            (row["final_checkpoint_hash"], candidate.final_checkpoint_hash),
            (row["provider_boundary_id"], candidate.provider_boundary_id),
            (row["provider_boundary_hash"], candidate.provider_boundary_hash),
            (row["provider_request_hash"], candidate.provider_request_hash),
            (row["context_pack_id"], candidate.context_pack_id),
            (row["context_pack_hash"], candidate.context_pack_hash),
            (row["operation_package_id"], candidate.operation_package_id),
            (row["operation_package_hash"], candidate.operation_package_hash),
            (row["project_binding_id"], candidate.project_id),
            (row["project_fingerprint"], candidate.project_fingerprint),
            (row["handoff_status"], candidate.handoff_status),
            (row["allowed_output"], candidate.allowed_output),
            (row["kind"], candidate.kind),
            (row["content_hash"], candidate.content_hash),
            (row["references_hash"], candidate.references_hash),
            (row["envelope_hash"], candidate.envelope_hash),
        )
        if (
            any(str(stored) != str(expected) for stored, expected in scalar_pairs)
            or int(row["project_revision"]) != candidate.project_revision
            or int(row["formal_draft_allowed"]) != 0
            or int(row["requires_author_review"]) != 1
            or stored_failure_signatures != list(candidate.failure_signatures)
            or stored_created_at != candidate.created_at.astimezone(UTC)
        ):
            raise AgentCandidateOutputConflict(
                "Stored Agent candidate columns do not match the immutable envelope",
                code="agent_candidate_output_integrity_failed",
            )
        return candidate

    def _validate_agent_candidate_output_binding(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        candidate: AgentCandidateOutputRecord,
    ) -> None:
        if candidate.run_id != str(run["id"]):
            raise AgentCandidateOutputConflict(
                "Agent candidate output belongs to another Run",
                code="agent_candidate_output_scope_mismatch",
            )
        rows = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
            (candidate.run_id,),
        ).fetchall()
        if not rows:
            raise AgentCandidateOutputConflict(
                "Agent candidate output requires a managed Workflow Plan",
                code="agent_candidate_output_workflow_missing",
            )
        plan, chain = self._workflow_chain_from_rows(connection, run, rows)
        step_checkpoints = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )
        boundaries = tuple(
            item for item in chain if isinstance(item, ProviderIoBoundary)
        )
        if (
            len(step_checkpoints) != len(plan.steps)
            or not step_checkpoints
            or len(boundaries) != 1
            or not isinstance(chain[-1], ProviderIoBoundary)
        ):
            raise AgentCandidateOutputConflict(
                "Agent candidate output requires one completed Provider boundary",
                code="agent_candidate_output_boundary_missing",
            )
        final_checkpoint = step_checkpoints[-1]
        boundary = boundaries[0]
        handoff = self._creation_handoff_from_checkpoint(
            connection,
            final_checkpoint,
        )
        expected = (
            candidate.workflow_plan_id == plan.workflow_plan_id
            and candidate.workflow_plan_hash == plan.plan_hash
            and candidate.final_checkpoint_id == final_checkpoint.checkpoint_id
            and candidate.final_checkpoint_hash == final_checkpoint.checkpoint_hash
            and candidate.provider_boundary_id == boundary.boundary_id
            and candidate.provider_boundary_hash == boundary.boundary_hash
            and candidate.provider_request_hash == boundary.request_hash
            and candidate.context_pack_id == plan.context_pack_id
            and candidate.context_pack_hash == plan.context_pack_hash
            and candidate.operation_package_id == plan.operation_package_id
            and candidate.operation_package_hash == plan.operation_package_hash
            and candidate.project_id == plan.project_id
            and candidate.project_revision == plan.project_revision
            and candidate.project_fingerprint == plan.project_fingerprint
            and candidate.handoff_status == handoff.handoff_status
            and candidate.allowed_output == handoff.allowed_output
            and candidate.failure_signatures == tuple(handoff.failure_signatures)
        )
        if not expected:
            raise AgentCandidateOutputConflict(
                "Agent candidate output does not match its Runtime-owned identities",
                code="agent_candidate_output_binding_mismatch",
            )
        provider_turn_receipts: tuple[ProviderTurnReceipt, ...] = ()
        if isinstance(candidate, AgentCandidateOutputV2):
            provider_turn_receipts = self._provider_turn_receipts_from_connection(
                connection, run, boundary
            )
            bindings = tuple(item.binding() for item in provider_turn_receipts)
            expected_identity_status = (
                "unreported"
                if any(
                    item.model_identity_status == "unreported"
                    for item in provider_turn_receipts
                )
                else "verified"
            )
            if (
                not provider_turn_receipts
                or candidate.provider_turn_receipts != bindings
                or candidate.provider_identity_status != expected_identity_status
                or any(
                    item.model_identity_status == "rejected_mismatch"
                    for item in provider_turn_receipts
                )
            ):
                raise AgentCandidateOutputConflict(
                    "Agent candidate Provider receipt chain does not match Runtime evidence",
                    code="agent_candidate_output_provider_receipt_mismatch",
                )
        if isinstance(candidate, AgentCandidateOutput):
            task_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_task'
                ORDER BY id
                """,
                (candidate.run_id,),
            ).fetchall()
            materialization_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_materialization'
                ORDER BY id
                """,
                (candidate.run_id,),
            ).fetchall()
            dispatch_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_dispatch_receipt'
                ORDER BY id
                """,
                (candidate.run_id,),
            ).fetchall()
            if (
                len(task_rows) != 1
                or len(materialization_rows) != 1
                or len(dispatch_rows) != 1
            ):
                raise AgentCandidateOutputConflict(
                    "Current candidate requires one Writer task and instruction dispatch chain",
                    code="agent_candidate_output_writer_evidence_missing",
                )
            writer_task = self._writer_task_from_artifact(task_rows[0])
            instruction_materialization = (
                self._creation_instruction_materialization_from_artifact(
                    materialization_rows[0]
                )
            )
            instruction_dispatch_receipt = (
                self._creation_instruction_dispatch_from_artifact(dispatch_rows[0])
            )
            change_sets = self._writer_change_sets_from_connection(
                connection,
                candidate.run_id,
            )
            if writer_task.mode == "author_review_change_proposal":
                if len(change_sets) != 1:
                    raise AgentCandidateOutputConflict(
                        "Current Writer candidate requires exactly one same-Run ChangeSet",
                        code="agent_candidate_output_change_set_missing",
                    )
                change_set = change_sets[0]
            else:
                if change_sets:
                    raise AgentCandidateOutputConflict(
                        "Repair-only Writer candidate cannot own a ChangeSet",
                        code="agent_candidate_output_change_set_forbidden",
                    )
                change_set = None
            try:
                expected_candidate = build_candidate_output_envelope(
                    plan,
                    step_checkpoints,
                    boundary,
                    handoff=handoff,
                    writer_task=writer_task,
                    instruction_materialization=instruction_materialization,
                    instruction_dispatch_receipt=instruction_dispatch_receipt,
                    provider_turn_receipts=provider_turn_receipts,
                    change_set=change_set,
                    submission=AgentCandidateSubmission(
                        kind=candidate.kind,
                        content=candidate.content,
                    ),
                    created_at=candidate.created_at,
                )
            except ValueError as exc:
                raise AgentCandidateOutputConflict(
                    "Current candidate does not bind the Runtime Writer evidence",
                    code="agent_candidate_output_writer_binding_mismatch",
                ) from exc
            if expected_candidate != candidate:
                raise AgentCandidateOutputConflict(
                    "Current candidate does not match Runtime-owned Writer evidence",
                    code="agent_candidate_output_writer_binding_mismatch",
                )
        self._validate_agent_candidate_references(
            connection,
            run_id=candidate.run_id,
            workflow_plan=plan,
            workflow_chain=chain,
            references=candidate.references,
        )

    @staticmethod
    def _creation_handoff_from_checkpoint(
        connection: sqlite3.Connection,
        checkpoint: WorkflowStepCheckpoint,
    ) -> CreationHandoffResult:
        if (
            checkpoint.skill_id != CREATION_HANDOFF_SKILL_ID
            or checkpoint.tool_call_id is None
            or checkpoint.execution_status != "succeeded"
        ):
            raise AgentCandidateOutputConflict(
                "Final workflow checkpoint is not a succeeded Creation handoff",
                code="agent_candidate_output_handoff_missing",
            )
        row = connection.execute(
            "SELECT run_id, status, output_json FROM tool_call WHERE id = ?",
            (checkpoint.tool_call_id,),
        ).fetchone()
        try:
            output = json.loads(str(row["output_json"])) if row is not None else None
            handoff_payload = (
                output.get("creation_handoff")
                if isinstance(output, dict)
                else None
            )
            handoff = CreationHandoffResult.model_validate_json(
                _json(handoff_payload)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentCandidateOutputConflict(
                "Creation handoff ToolResult is not a valid immutable result",
                code="agent_candidate_output_handoff_invalid",
            ) from exc
        if (
            row is None
            or str(row["run_id"]) != checkpoint.run_id
            or str(row["status"]) != "succeeded"
        ):
            raise AgentCandidateOutputConflict(
                "Creation handoff ToolResult does not belong to this Run",
                code="agent_candidate_output_handoff_invalid",
            )
        return handoff

    @staticmethod
    def _validate_agent_candidate_references(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        workflow_plan: WorkflowPlan,
        workflow_chain: tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
        references: tuple[AgentCandidateReference, ...],
    ) -> None:
        workflow_evidence = {workflow_plan.workflow_plan_id: workflow_plan.plan_hash}
        workflow_evidence.update(
            {
                (
                    item.checkpoint_id
                    if isinstance(item, WorkflowStepCheckpoint)
                    else item.boundary_id
                ): (
                    item.checkpoint_hash
                    if isinstance(item, WorkflowStepCheckpoint)
                    else item.boundary_hash
                )
                for item in workflow_chain
            }
        )
        for reference in references:
            valid = False
            if reference.reference_kind == "workflow_checkpoint":
                valid = (
                    workflow_evidence.get(reference.reference_id)
                    == reference.reference_hash
                )
            elif reference.reference_kind == "runtime_identity":
                runtime_evidence = {
                    workflow_plan.context_pack_id: workflow_plan.context_pack_hash,
                    workflow_plan.operation_package_id: workflow_plan.operation_package_hash,
                }
                writer_tasks = connection.execute(
                    """
                    SELECT id, content_hash FROM artifact
                    WHERE run_id = ? AND artifact_type = 'writer_task'
                    """,
                    (run_id,),
                ).fetchall()
                if len(writer_tasks) == 1:
                    runtime_evidence[str(writer_tasks[0]["id"])] = str(
                        writer_tasks[0]["content_hash"]
                    )
                valid = (
                    runtime_evidence.get(reference.reference_id)
                    == reference.reference_hash
                )
            elif reference.reference_kind == "artifact":
                row = connection.execute(
                    "SELECT content_hash FROM artifact WHERE id = ? AND run_id = ?",
                    (reference.reference_id, run_id),
                ).fetchone()
                valid = (
                    row is not None
                    and str(row["content_hash"]) == reference.reference_hash
                )
            elif reference.reference_kind == "change_set":
                row = connection.execute(
                    """
                    SELECT change_set.payload_json
                    FROM change_set
                    JOIN change_set_provenance
                      ON change_set_provenance.change_set_id = change_set.id
                    WHERE change_set.id = ?
                      AND change_set_provenance.run_id = ?
                    """,
                    (reference.reference_id, run_id),
                ).fetchone()
                if row is not None:
                    try:
                        payload = json.loads(str(row["payload_json"]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    # 存储 payload 带 ai_smell（作者审阅要看），信封哈希剔除它；
                    # 比对必须走信封口径，否则带写法提示的候选必然失配。
                    valid = (
                        payload is not None
                        and change_set_reference_hash_from_payload(payload)
                        == reference.reference_hash
                    )
            if not valid:
                raise AgentCandidateOutputConflict(
                    "Agent candidate reference is not same-Run persisted evidence: "
                    f"kind={reference.reference_kind} "
                    f"reference_id={reference.reference_id} "
                    f"expected_hash={reference.reference_hash}",
                    code="agent_candidate_output_reference_invalid",
                )

    def _writer_workflow_sources(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        *,
        require_provider_boundary: bool | None,
    ) -> tuple[
        WorkflowPlan,
        tuple[WorkflowStepCheckpoint, ...],
        CreationHandoffResult,
    ]:
        rows = connection.execute(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence ASC",
            (str(run["id"]),),
        ).fetchall()
        if not rows:
            raise AgentStoreInvalidState(
                "Writer task requires a managed Workflow Plan"
            )
        plan, chain = self._workflow_chain_from_rows(connection, run, rows)
        checkpoints = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )
        boundaries = tuple(
            item for item in chain if isinstance(item, ProviderIoBoundary)
        )
        if len(checkpoints) != len(plan.steps) or not checkpoints:
            raise AgentStoreInvalidState(
                "Writer task requires the completed managed Workflow"
            )
        if require_provider_boundary is False and boundaries:
            raise AgentStoreInvalidState(
                "Writer task must be persisted before Provider dispatch"
            )
        if require_provider_boundary is True and len(boundaries) != 1:
            raise AgentStoreInvalidState(
                "Writer result requires one Provider boundary"
            )
        handoff = self._creation_handoff_from_checkpoint(
            connection,
            checkpoints[-1],
        )
        return plan, checkpoints, handoff

    def _writer_context_sources(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
    ) -> tuple[ContextPack, NlclRouteReceipt, PrewriteOperationPackage]:
        row = connection.execute(
            "SELECT * FROM context_pack WHERE id = ?",
            (plan.context_pack_id,),
        ).fetchone()
        if row is None:
            raise AgentStoreInvalidState("Writer Context Pack was not found")
        context_store = ContextStore(self.database)
        try:
            stored = context_store._stored_from_row(connection, row)
            route = context_store._route_from_connection(
                connection,
                plan.context_pack_id,
            )
            operation_package = context_store._prewrite_from_connection(
                connection,
                plan.context_pack_id,
            )
            context_store._validate_prewrite_binding(
                operation_package,
                stored.pack,
                route,
            )
        except ContextStoreError as exc:
            raise AgentStoreInvalidState(
                "Writer Context/NLCL/operation package chain is invalid"
            ) from exc
        return stored.pack, route, operation_package

    @staticmethod
    def _writer_intent_from_connection(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> tuple[
        Literal["repair_only", "author_review_change_proposal"],
        str | None,
    ]:
        rows = connection.execute(
            """
            SELECT content_hash, metadata_json FROM artifact
            WHERE run_id = ? AND artifact_type = 'writer_intent'
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            return "repair_only", None
        if len(rows) != 1:
            raise AgentStoreInvalidState("Run must have exactly one Writer intent")
        try:
            payload = json.loads(str(rows[0]["metadata_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentStoreInvalidState("Stored Writer intent is invalid") from exc
        mode = payload.get("mode") if isinstance(payload, dict) else None
        chapter_id = (
            payload.get("active_chapter_id")
            if isinstance(payload, dict)
            else None
        )
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "zhimeng.writer-intent.v1"
            or mode not in {"repair_only", "author_review_change_proposal"}
            or not (chapter_id is None or isinstance(chapter_id, str))
            or canonical_json_hash(payload) != str(rows[0]["content_hash"])
        ):
            raise AgentStoreInvalidState(
                "Stored Writer intent failed integrity validation"
            )
        return mode, chapter_id

    @staticmethod
    def _writer_task_from_artifact(row: sqlite3.Row) -> WriterTask:
        try:
            task = WriterTask.model_validate_json(str(row["metadata_json"]))
            created_at = _parse_time(row["created_at"])
        except (TypeError, ValueError) as exc:
            raise AgentStoreInvalidState(
                "Stored Writer task failed immutable validation"
            ) from exc
        if (
            str(row["id"]) != task.writer_task_id
            or str(row["content_hash"]) != task.task_hash
            or writer_task_hash(task) != task.task_hash
            or created_at != task.created_at.astimezone(UTC)
        ):
            raise AgentStoreInvalidState(
                "Stored Writer task columns do not match its payload"
            )
        return task

    @staticmethod
    def _writer_candidate_result_from_artifact(
        row: sqlite3.Row,
    ) -> WriterCandidateResult:
        try:
            result = WriterCandidateResult.model_validate_json(
                str(row["metadata_json"])
            )
            created_at = _parse_time(row["created_at"])
        except (TypeError, ValueError) as exc:
            raise AgentStoreInvalidState(
                "Stored Writer candidate result failed immutable validation"
            ) from exc
        if (
            str(row["id"]) != result.writer_candidate_result_id
            or str(row["content_hash"]) != result.result_hash
            or writer_candidate_result_hash(result) != result.result_hash
            or created_at != result.created_at.astimezone(UTC)
        ):
            raise AgentStoreInvalidState(
                "Stored Writer candidate result columns do not match its payload"
            )
        return result

    @staticmethod
    def _writer_change_sets_from_connection(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> tuple[ChangeSetResponse, ...]:
        rows = connection.execute(
            """
            SELECT
                change_set.payload_json,
                change_set_provenance.tool_call_id,
                tool_call.run_id AS tool_run_id,
                tool_call.tool_name,
                tool_call.status AS tool_status
            FROM change_set
            JOIN change_set_provenance
              ON change_set_provenance.change_set_id = change_set.id
            JOIN tool_call
              ON tool_call.id = change_set_provenance.tool_call_id
            WHERE change_set_provenance.run_id = ?
            ORDER BY change_set.created_at, change_set.id
            """,
            (run_id,),
        ).fetchall()
        try:
            change_sets = tuple(
                ChangeSetResponse.model_validate_json(str(row["payload_json"]))
                for row in rows
            )
        except (TypeError, ValueError) as exc:
            raise AgentStoreInvalidState(
                "Stored Writer ChangeSet failed immutable validation"
            ) from exc
        for row, change_set in zip(rows, change_sets, strict=True):
            if (
                str(row["tool_run_id"]) != run_id
                or str(row["tool_name"]) != "change_set_propose"
                or str(row["tool_status"]) != "succeeded"
                or change_set.source_run_id != run_id
                or change_set.source_tool_call_id != str(row["tool_call_id"])
            ):
                raise AgentStoreInvalidState(
                    "Writer ChangeSet requires one succeeded same-Run proposal ToolCall"
                )
        return change_sets

    def _validate_workflow_plan_binding(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        plan: WorkflowPlan,
    ) -> None:
        if (
            plan.run_id != str(run["id"])
            or plan.project_id != str(run["project_binding_id"])
            or plan.project_revision != int(run["project_revision"])
            or plan.project_fingerprint != str(run["project_fingerprint"])
            or plan.context_pack_id
            != (str(run["context_pack_id"]) if run["context_pack_id"] else None)
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan does not bind the Run's immutable inputs",
                code="workflow_checkpoint_scope_mismatch",
            )

        context_row = connection.execute(
            "SELECT * FROM context_pack WHERE id = ?",
            (plan.context_pack_id,),
        ).fetchone()
        if context_row is None:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan Context Pack was not found",
                code="workflow_plan_source_missing",
            )
        context_store = ContextStore(self.database)
        try:
            stored_pack = context_store._stored_from_row(connection, context_row)
            route = context_store._route_from_connection(
                connection,
                plan.context_pack_id,
            )
            operation_package = context_store._prewrite_from_connection(
                connection,
                plan.context_pack_id,
            )
            context_store._validate_prewrite_binding(
                operation_package,
                stored_pack.pack,
                route,
            )
        except ContextStoreError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan source chain failed immutable validation",
                code="workflow_plan_source_integrity_failed",
            ) from exc

        pack = stored_pack.pack
        if (
            stored_pack.consumed_by_run_id != plan.run_id
            or pack.pack_hash != plan.context_pack_hash
            or getattr(pack, "knowledge_fingerprint", None)
            != plan.knowledge_fingerprint
            or pack.project_id != plan.project_id
            or pack.project_revision != plan.project_revision
            or pack.project_fingerprint != plan.project_fingerprint
            or route.route_receipt_id != plan.route_receipt_id
            or route.receipt_hash != plan.route_receipt_hash
            or operation_package.operation_package_id != plan.operation_package_id
            or operation_package.package_hash != plan.operation_package_hash
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan does not match its persisted Context/Route/Package chain",
                code="workflow_plan_source_mismatch",
            )
        if (
            "migrated_prewrite_package_requires_recompile"
            in operation_package.missing_requirements
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan cannot use a migrated prewrite package",
                code="workflow_plan_source_mismatch",
            )

        route_calls = tuple(route.chapter_call_list)
        if len(route_calls) != len(plan.steps):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan steps do not match the NLCL route call sequence",
                code="workflow_plan_route_mismatch",
            )
        for step, route_call in zip(plan.steps, route_calls, strict=True):
            if (
                step.ordinal != route_call.ordinal
                or step.skill_id != route_call.skill_id
                or route_call.context_pack_id != plan.context_pack_id
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Plan step does not match its NLCL route call",
                    code="workflow_plan_route_mismatch",
                )
            self._validate_workflow_skill_version(connection, plan.project_id, step)

    def _validate_workflow_skill_version(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        step: WorkflowStep,
    ) -> None:
        project_activation = connection.execute(
            """
            SELECT state, active_version_id
            FROM skill_activation
            WHERE scope_key = ? AND skill_id = ?
            """,
            (f"project:{project_id}", step.skill_id),
        ).fetchone()
        effective_activation = project_activation
        if project_activation is None or str(project_activation["state"]) == "inherited":
            effective_activation = connection.execute(
                """
                SELECT state, active_version_id
                FROM skill_activation
                WHERE scope_key = 'application' AND skill_id = ?
                """,
                (step.skill_id,),
            ).fetchone()
        if (
            effective_activation is None
            or str(effective_activation["state"]) != "enabled"
            or str(effective_activation["active_version_id"])
            != step.expected_skill_version_id
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan Skill version is no longer enabled for the project",
                code="workflow_plan_skill_changed",
            )
        row = connection.execute(
            """
            SELECT *
            FROM skill_version
            WHERE id = ? AND skill_id = ? AND snapshot_status = 'managed'
              AND status = 'active'
            """,
            (step.expected_skill_version_id, step.skill_id),
        ).fetchone()
        if row is None:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan Skill version is not an executable managed snapshot",
                code="workflow_plan_skill_changed",
            )
        source_path = Path(str(row["source_path"]))
        if source_path.name != MANIFEST_NAME or not source_path.is_absolute():
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan Skill source path is invalid",
                code="workflow_plan_skill_integrity_failed",
            )
        try:
            package_root = source_path.parent
            managed_root = package_root.parent.parent
            managed_service = ManagedSkillService(self.database, managed_root)
            active = managed_service.get_active(project_id, step.skill_id)
            managed_service.verify_active(active)
        except (ManagedSkillError, OSError, RuntimeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan managed Skill package failed verification",
                code="workflow_plan_skill_integrity_failed",
            ) from exc
        manifest = active.manifest
        if (
            active.skill_version_id != step.expected_skill_version_id
            or manifest.id != step.skill_id
            or manifest.version != step.expected_skill_version
            or active.manifest_fingerprint != step.expected_manifest_fingerprint
            or active.package_fingerprint != step.expected_package_fingerprint
            or manifest.input_schema != step.input_contract
            or manifest.output_schema != step.output_contract
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Plan step does not match its active managed Skill manifest",
                code="workflow_plan_skill_changed",
            )

    @staticmethod
    def _validate_workflow_row_time(
        row: sqlite3.Row,
        created_at: datetime,
    ) -> None:
        try:
            stored = _parse_time(row["created_at"])
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint timestamp is invalid",
                code="workflow_checkpoint_integrity_failed",
            ) from exc
        if stored != created_at.astimezone(UTC):
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint timestamp does not match its payload",
                code="workflow_checkpoint_integrity_failed",
            )

    @classmethod
    def _workflow_plan_from_row(cls, row: sqlite3.Row) -> WorkflowPlan:
        try:
            plan = WorkflowPlan.model_validate_json(str(row["state_json"]))
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Stored Workflow Plan failed immutable validation",
                code="workflow_checkpoint_integrity_failed",
            ) from exc
        if (
            str(row["id"]) != plan.workflow_plan_id
            or str(row["run_id"]) != plan.run_id
            or int(row["sequence"]) != 0
            or str(row["checkpoint_type"]) != WORKFLOW_PLAN_CHECKPOINT_TYPE
        ):
            raise AgentWorkflowCheckpointConflict(
                "Stored Workflow Plan identity does not match its database row",
                code="workflow_checkpoint_integrity_failed",
            )
        cls._validate_workflow_row_time(row, plan.created_at)
        return plan

    @classmethod
    def _workflow_step_checkpoint_from_row(
        cls,
        row: sqlite3.Row,
    ) -> WorkflowStepCheckpoint:
        try:
            checkpoint = WorkflowStepCheckpoint.model_validate_json(
                str(row["state_json"])
            )
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Stored workflow checkpoint failed immutable validation",
                code="workflow_checkpoint_integrity_failed",
            ) from exc
        if (
            str(row["id"]) != checkpoint.checkpoint_id
            or str(row["run_id"]) != checkpoint.run_id
            or str(row["checkpoint_type"]) != WORKFLOW_STEP_CHECKPOINT_TYPE
        ):
            raise AgentWorkflowCheckpointConflict(
                "Stored workflow checkpoint identity does not match its database row",
                code="workflow_checkpoint_integrity_failed",
            )
        cls._validate_workflow_row_time(row, checkpoint.created_at)
        return checkpoint

    @classmethod
    def _provider_io_boundary_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ProviderIoBoundary:
        try:
            boundary = ProviderIoBoundary.model_validate_json(str(row["state_json"]))
        except (TypeError, ValueError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Stored Provider I/O boundary failed immutable validation",
                code="workflow_checkpoint_integrity_failed",
            ) from exc
        if (
            str(row["id"]) != boundary.boundary_id
            or str(row["run_id"]) != boundary.run_id
            or str(row["checkpoint_type"])
            != PROVIDER_IO_BOUNDARY_CHECKPOINT_TYPE
        ):
            raise AgentWorkflowCheckpointConflict(
                "Stored Provider I/O boundary identity does not match its database row",
                code="workflow_checkpoint_integrity_failed",
            )
        cls._validate_workflow_row_time(row, boundary.created_at)
        return boundary

    def _workflow_chain_from_rows(
        self,
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        rows: list[sqlite3.Row],
    ) -> tuple[
        WorkflowPlan,
        tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
    ]:
        if (
            not rows
            or int(rows[0]["sequence"]) != 0
            or str(rows[0]["checkpoint_type"])
            != WORKFLOW_PLAN_CHECKPOINT_TYPE
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint chain has no sequence-zero plan",
                code="workflow_checkpoint_integrity_failed",
            )
        plan = self._workflow_plan_from_row(rows[0])
        self._validate_workflow_plan_binding(connection, run, plan)

        chain: list[WorkflowStepCheckpoint | ProviderIoBoundary] = []
        step_checkpoints: list[WorkflowStepCheckpoint] = []
        provider_boundary: ProviderIoBoundary | None = None
        for expected_sequence, row in enumerate(rows[1:], start=1):
            if int(row["sequence"]) != expected_sequence:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow checkpoint sequence is not contiguous",
                    code="workflow_checkpoint_integrity_failed",
                )
            checkpoint_type = str(row["checkpoint_type"])
            if checkpoint_type == WORKFLOW_STEP_CHECKPOINT_TYPE:
                if provider_boundary is not None:
                    raise AgentWorkflowCheckpointConflict(
                        "Workflow step appears after the Provider I/O boundary",
                        code="workflow_checkpoint_integrity_failed",
                    )
                checkpoint = self._workflow_step_checkpoint_from_row(row)
                self._validate_workflow_execution_binding(
                    connection,
                    plan,
                    checkpoint,
                )
                step_checkpoints.append(checkpoint)
                chain.append(checkpoint)
            elif checkpoint_type == PROVIDER_IO_BOUNDARY_CHECKPOINT_TYPE:
                if provider_boundary is not None:
                    raise AgentWorkflowCheckpointConflict(
                        "Run has multiple Provider I/O boundaries",
                        code="workflow_checkpoint_integrity_failed",
                    )
                provider_boundary = self._provider_io_boundary_from_row(row)
                chain.append(provider_boundary)
            else:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow checkpoint chain contains an unknown record type",
                    code="workflow_checkpoint_integrity_failed",
                )

        try:
            validate_workflow_checkpoint_chain(plan, tuple(step_checkpoints))
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Stored workflow checkpoint chain failed immutable validation",
                code="workflow_checkpoint_integrity_failed",
            ) from exc
        if provider_boundary is not None:
            self._validate_existing_provider_boundary(
                run,
                plan,
                tuple(step_checkpoints),
                provider_boundary,
            )
        return plan, tuple(chain)

    @staticmethod
    def _workflow_provider_tool_call_id(plan: WorkflowPlan, step_ordinal: int) -> str:
        digest = hashlib.sha256(
            f"{plan.route_receipt_id}\0{step_ordinal}".encode("utf-8")
        ).hexdigest()
        return f"nlcl_call_{digest[:24]}"

    @staticmethod
    def _workflow_runtime_tool_call_id(run_id: str, provider_tool_call_id: str) -> str:
        digest = hashlib.sha256(
            f"{run_id}\0{provider_tool_call_id}".encode("utf-8")
        ).hexdigest()
        return f"tool_{digest[:32]}"

    def _workflow_resume_blocker_from_connection(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        checkpoints: tuple[WorkflowStepCheckpoint, ...],
    ) -> str | None:
        if len(checkpoints) >= len(plan.steps):
            return None
        next_step_ordinal = len(checkpoints) + 1
        provider_tool_call_id = self._workflow_provider_tool_call_id(
            plan,
            next_step_ordinal,
        )
        runtime_tool_call_id = self._workflow_runtime_tool_call_id(
            plan.run_id,
            provider_tool_call_id,
        )
        existing = connection.execute(
            "SELECT 1 FROM tool_call WHERE id = ? AND run_id = ?",
            (runtime_tool_call_id, plan.run_id),
        ).fetchone()
        return "workflow_step_interrupted" if existing is not None else None

    def _build_workflow_checkpoint_from_execution(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        *,
        step_ordinal: int,
        provider_tool_call_id: str | None,
        previous_checkpoint: WorkflowStepCheckpoint | None,
        pre_dispatch_error: ErrorDetail | None,
    ) -> WorkflowStepCheckpoint:
        step = plan.steps[step_ordinal - 1]
        expected_provider_id = self._workflow_provider_tool_call_id(plan, step_ordinal)
        runtime_tool_call_id = self._workflow_runtime_tool_call_id(
            plan.run_id,
            expected_provider_id,
        )
        if provider_tool_call_id is None:
            if pre_dispatch_error is None:
                raise AgentWorkflowCheckpointConflict(
                    "Pre-dispatch workflow failure requires a persisted error projection",
                    code="workflow_checkpoint_evidence_missing",
                )
            self._validate_no_workflow_execution_evidence(
                connection,
                plan,
                step_ordinal=step_ordinal,
                runtime_tool_call_id=runtime_tool_call_id,
            )
            return build_workflow_step_checkpoint(
                plan,
                step,
                attempt_ordinal=1,
                previous_checkpoint=previous_checkpoint,
                tool_call_id=None,
                provider_tool_call_id=None,
                skill_call_id=None,
                skill_receipt_id=None,
                skill_receipt_hash=None,
                route_execution_receipt_id=None,
                route_execution_receipt_hash=None,
                input_hash=None,
                output_hash=None,
                tool_result_hash=None,
                execution_status="failed",
                domain_status=None,
                error_code=pre_dispatch_error.code,
                error_message=pre_dispatch_error.message,
                created_at=_now(),
            )
        if pre_dispatch_error is not None or provider_tool_call_id != expected_provider_id:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Provider ToolCall identity does not match the immutable route step",
                code="workflow_checkpoint_evidence_mismatch",
            )

        material = self._workflow_execution_material(
            connection,
            plan,
            step,
            provider_tool_call_id=provider_tool_call_id,
            runtime_tool_call_id=runtime_tool_call_id,
        )
        route_execution = material["route_execution"]
        skill_call = material["skill_call"]
        skill_receipt = material["skill_receipt"]
        tool_error = material["tool_error"]
        execution_status = str(route_execution.execution_status)
        if execution_status == "succeeded":
            error_code = None
            error_message = None
        else:
            if tool_error is None:
                raise AgentWorkflowCheckpointConflict(
                    "Failed workflow ToolCall has no persisted error",
                    code="workflow_checkpoint_evidence_missing",
                )
            error_code = tool_error.error.code
            error_message = tool_error.error.message
        return build_workflow_step_checkpoint(
            plan,
            step,
            attempt_ordinal=1,
            previous_checkpoint=previous_checkpoint,
            tool_call_id=runtime_tool_call_id,
            provider_tool_call_id=provider_tool_call_id,
            skill_call_id=skill_call.skill_call_id,
            skill_receipt_id=skill_receipt.receipt_id,
            skill_receipt_hash=str(material["skill_receipt_hash"]),
            route_execution_receipt_id=route_execution.execution_receipt_id,
            route_execution_receipt_hash=route_execution.receipt_hash,
            input_hash=skill_receipt.input_hash,
            output_hash=skill_receipt.output_hash,
            tool_result_hash=route_execution.tool_result_hash,
            execution_status=execution_status,
            domain_status=route_execution.domain_status,
            error_code=error_code,
            error_message=error_message,
            created_at=route_execution.created_at,
        )

    @staticmethod
    def _validate_no_workflow_execution_evidence(
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        *,
        step_ordinal: int,
        runtime_tool_call_id: str,
    ) -> None:
        tool_call = connection.execute(
            "SELECT id FROM tool_call WHERE id = ?",
            (runtime_tool_call_id,),
        ).fetchone()
        route_execution = connection.execute(
            """
            SELECT id FROM nlcl_route_execution_receipt
            WHERE context_pack_id = ? AND call_ordinal = ?
            """,
            (plan.context_pack_id, step_ordinal),
        ).fetchone()
        if tool_call is not None or route_execution is not None:
            raise AgentWorkflowCheckpointConflict(
                "Pre-dispatch workflow failure has orphan execution evidence",
                code="workflow_checkpoint_evidence_conflict",
            )

    def _workflow_execution_material(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        step: WorkflowStep,
        *,
        provider_tool_call_id: str,
        runtime_tool_call_id: str,
    ) -> dict[str, Any]:
        tool_call = connection.execute(
            "SELECT * FROM tool_call WHERE id = ?",
            (runtime_tool_call_id,),
        ).fetchone()
        if tool_call is None:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall was not found",
                code="workflow_checkpoint_evidence_missing",
            )
        if (
            str(tool_call["run_id"]) != plan.run_id
            or str(tool_call["tool_name"]) != "skill_execute"
            or str(tool_call["tool_version"]) != "2"
            or str(tool_call["idempotency_key"] or "") != runtime_tool_call_id
            or str(tool_call["status"])
            not in {"succeeded", "failed", "cancelled"}
            or tool_call["result_hash"] is None
            or tool_call["call_activity_id"] is None
            or tool_call["result_activity_id"] is None
            or tool_call["finished_at"] is None
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall is not the required terminal skill_execute v2 call",
                code="workflow_checkpoint_evidence_mismatch",
            )
        try:
            arguments = json.loads(str(tool_call["input_json"]))
            output = json.loads(str(tool_call["output_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall input/output is not valid JSON",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        if (
            not isinstance(arguments, dict)
            or arguments.get("skill_id") != step.skill_id
            or not isinstance(arguments.get("input"), dict)
            or str(tool_call["request_hash"])
            != _sha256_json(
                {
                    "tool_name": "skill_execute",
                    "tool_version": "2",
                    "arguments": arguments,
                }
            )
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall request does not match the immutable step",
                code="workflow_checkpoint_evidence_mismatch",
            )

        activity_rows = connection.execute(
            """
            SELECT * FROM activity
            WHERE run_id = ? AND id IN (?, ?)
            ORDER BY sequence
            """,
            (
                plan.run_id,
                str(tool_call["call_activity_id"]),
                str(tool_call["result_activity_id"]),
            ),
        ).fetchall()
        if len(activity_rows) != 2:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall boundary activities are missing",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        call_row, result_row = activity_rows
        try:
            call_activity = ActivityItem.model_validate_json(
                str(call_row["payload_json"])
            ).root
            result_activity = ActivityItem.model_validate_json(
                str(result_row["payload_json"])
            ).root
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall boundary activity is invalid",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        if (
            call_activity.activity_id != str(tool_call["call_activity_id"])
            or call_activity.kind is not ActivityKind.TOOL_CALL
            or not isinstance(call_activity.payload, ToolCallPayload)
            or call_activity.payload.tool_call_id != runtime_tool_call_id
            or call_activity.payload.tool_name != "skill_execute"
            or call_activity.payload.arguments != arguments
            or result_activity.activity_id != str(tool_call["result_activity_id"])
            or result_activity.kind is not ActivityKind.TOOL_RESULT
            or not isinstance(result_activity.payload, ToolResultPayload)
            or result_activity.payload.tool_call_id != runtime_tool_call_id
            or result_activity.payload.status != str(tool_call["status"])
            or result_activity.payload.output != output
            or result_activity.payload.duration_ms != tool_call["duration_ms"]
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall activity chain does not match its row",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        try:
            tool_error = (
                ErrorEnvelope.model_validate_json(str(tool_call["error_json"]))
                if tool_call["error_json"] is not None
                else None
            )
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall error payload is invalid",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        if result_activity.payload.error != tool_error:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolResult error does not match its ToolCall row",
                code="workflow_checkpoint_evidence_integrity_failed",
            )

        intermediate_rows = connection.execute(
            """
            SELECT * FROM activity
            WHERE run_id = ? AND sequence > ? AND sequence < ?
            ORDER BY sequence
            """,
            (plan.run_id, int(call_row["sequence"]), int(result_row["sequence"])),
        ).fetchall()
        if not intermediate_rows:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall has no direct SkillCall evidence",
                code="workflow_checkpoint_evidence_missing",
            )
        try:
            intermediate = [
                ActivityItem.model_validate_json(str(row["payload_json"])).root
                for row in intermediate_rows
            ]
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall intermediate activity is invalid",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        skill_activities = [
            item for item in intermediate if item.kind is ActivityKind.SKILL_CALL
        ]
        if len(skill_activities) != 1 or not isinstance(
            skill_activities[0].payload,
            SkillCallPayload,
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall must have exactly one direct SkillCall",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        skill_activity = skill_activities[0]
        skill_call = skill_activity.payload
        if (
            skill_activity.causation_id != call_activity.activity_id
            or skill_call.skill_id != step.skill_id
            or skill_call.input != arguments["input"]
            or not isinstance(skill_call.output, dict)
            or not isinstance(skill_call.output.get("receipt"), dict)
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow SkillCall does not match the ToolCall input",
                code="workflow_checkpoint_evidence_mismatch",
            )
        try:
            skill_receipt = SkillExecutionReceipt.model_validate_json(
                _json(skill_call.output["receipt"])
            )
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Skill execution receipt is invalid",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        skill_receipt_hash = canonical_json_hash(skill_receipt)
        if (
            skill_receipt.run_id != plan.run_id
            or skill_receipt.tool_call_id != runtime_tool_call_id
            or skill_receipt.project_id != plan.project_id
            or skill_receipt.project_revision != plan.project_revision
            or skill_receipt.project_fingerprint != plan.project_fingerprint
            or skill_receipt.skill_version_id != step.expected_skill_version_id
            or skill_receipt.skill_id != step.skill_id
            or skill_receipt.skill_version != step.expected_skill_version
            or skill_receipt.manifest_fingerprint
            != step.expected_manifest_fingerprint
            or skill_receipt.package_fingerprint != step.expected_package_fingerprint
            or skill_receipt.input_hash != canonical_json_hash(arguments["input"])
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow Skill receipt does not bind the immutable Plan and ToolCall",
                code="workflow_checkpoint_evidence_mismatch",
            )

        artifact_activities = [
            item for item in intermediate if item.kind is ActivityKind.ARTIFACT
        ]
        if len(intermediate) != len(artifact_activities) + 1:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolCall contains unexpected intermediate activities",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        expected_causation = skill_activity.activity_id
        artifacts: list[ArtifactPayload] = []
        for artifact_activity in artifact_activities:
            if (
                artifact_activity.causation_id != expected_causation
                or not isinstance(artifact_activity.payload, ArtifactPayload)
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow artifact causation chain is broken",
                    code="workflow_checkpoint_evidence_integrity_failed",
                )
            artifact = artifact_activity.payload
            artifact_row = connection.execute(
                "SELECT * FROM artifact WHERE id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if artifact_row is None:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow artifact row is missing",
                    code="workflow_checkpoint_evidence_missing",
                )
            try:
                artifact_metadata = ArtifactPayload.model_validate_json(
                    str(artifact_row["metadata_json"])
                )
            except ValueError as exc:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow artifact metadata is invalid",
                    code="workflow_checkpoint_evidence_integrity_failed",
                ) from exc
            if (
                str(artifact_row["run_id"]) != plan.run_id
                or str(artifact_row["artifact_type"]) != artifact.artifact_type
                or artifact_metadata != artifact
                or artifact.content_hash != artifact_row["content_hash"]
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow artifact row does not match its Activity",
                    code="workflow_checkpoint_evidence_integrity_failed",
                )
            artifacts.append(artifact)
            expected_causation = artifact_activity.activity_id
        if result_activity.causation_id != expected_causation:
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolResult causation chain is broken",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        receipt_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.artifact_type == "skill_execution_receipt"
            and artifact.content_hash == skill_receipt_hash
        ]
        if len(receipt_artifacts) != 1:
            raise AgentWorkflowCheckpointConflict(
                "Workflow Skill receipt artifact is missing or ambiguous",
                code="workflow_checkpoint_evidence_integrity_failed",
            )
        recomputed_result_hash = self._tool_result_hash(
            result_activity.payload,
            None,
            skill_call,
            tuple(artifacts),
        )
        if recomputed_result_hash != str(tool_call["result_hash"]):
            raise AgentWorkflowCheckpointConflict(
                "Workflow ToolResult hash does not match its persisted activity chain",
                code="workflow_checkpoint_evidence_integrity_failed",
            )

        route_row = connection.execute(
            """
            SELECT * FROM nlcl_route_execution_receipt
            WHERE context_pack_id = ? AND call_ordinal = ?
            """,
            (plan.context_pack_id, step.ordinal),
        ).fetchone()
        if route_row is None:
            raise AgentWorkflowCheckpointConflict(
                "Workflow NLCL route execution receipt was not found",
                code="workflow_checkpoint_evidence_missing",
            )
        try:
            route_execution = ContextStore(self.database)._route_execution_from_row(
                connection,
                route_row,
            )
        except ContextStoreError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow NLCL route execution receipt failed validation",
                code="workflow_checkpoint_evidence_integrity_failed",
            ) from exc
        if (
            route_execution.route_receipt_id != plan.route_receipt_id
            or route_execution.route_receipt_hash != plan.route_receipt_hash
            or route_execution.context_pack_id != plan.context_pack_id
            or route_execution.context_pack_hash != plan.context_pack_hash
            or route_execution.call_ordinal != step.ordinal
            or route_execution.skill_id != step.skill_id
            or route_execution.run_id != plan.run_id
            or route_execution.tool_call_id != runtime_tool_call_id
            or route_execution.skill_call_id != skill_call.skill_call_id
            or route_execution.skill_receipt_id != skill_receipt.receipt_id
            or route_execution.skill_receipt_hash != skill_receipt_hash
            or route_execution.execution_status != str(tool_call["status"])
            or route_execution.tool_result_hash != str(tool_call["result_hash"])
        ):
            raise AgentWorkflowCheckpointConflict(
                "Workflow route execution does not match the Plan/Tool/Skill evidence",
                code="workflow_checkpoint_evidence_mismatch",
            )
        return {
            "route_execution": route_execution,
            "skill_call": skill_call,
            "skill_receipt": skill_receipt,
            "skill_receipt_hash": skill_receipt_hash,
            "tool_error": tool_error,
            "provider_tool_call_id": provider_tool_call_id,
        }

    def _validate_workflow_execution_binding(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        checkpoint: WorkflowStepCheckpoint,
    ) -> None:
        if checkpoint.tool_call_id is None:
            runtime_tool_call_id = self._workflow_runtime_tool_call_id(
                plan.run_id,
                self._workflow_provider_tool_call_id(plan, checkpoint.step_ordinal),
            )
            self._validate_no_workflow_execution_evidence(
                connection,
                plan,
                step_ordinal=checkpoint.step_ordinal,
                runtime_tool_call_id=runtime_tool_call_id,
            )
            return
        previous = None
        if checkpoint.previous_checkpoint_id is not None:
            previous_row = connection.execute(
                "SELECT * FROM checkpoint WHERE id = ? AND run_id = ?",
                (checkpoint.previous_checkpoint_id, checkpoint.run_id),
            ).fetchone()
            if previous_row is None:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow checkpoint predecessor row was not found",
                    code="workflow_checkpoint_integrity_failed",
                )
            previous = self._workflow_step_checkpoint_from_row(previous_row)
        expected = self._build_workflow_checkpoint_from_execution(
            connection,
            plan,
            step_ordinal=checkpoint.step_ordinal,
            provider_tool_call_id=checkpoint.provider_tool_call_id,
            previous_checkpoint=previous,
            pre_dispatch_error=None,
        )
        if expected != checkpoint:
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint does not match persisted execution evidence",
                code="workflow_checkpoint_evidence_mismatch",
            )

    def _validate_workflow_step_append(
        self,
        connection: sqlite3.Connection,
        plan: WorkflowPlan,
        chain: tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
        checkpoint: WorkflowStepCheckpoint,
        *,
        expected_previous_checkpoint_hash: str | None,
    ) -> None:
        if any(isinstance(item, ProviderIoBoundary) for item in chain):
            raise AgentWorkflowCheckpointConflict(
                "Workflow cannot append a step after Provider I/O begins",
                code="workflow_checkpoint_closed",
            )
        previous = chain[-1] if chain else None
        if previous is not None and not isinstance(previous, WorkflowStepCheckpoint):
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint predecessor has an invalid type",
                code="workflow_checkpoint_integrity_failed",
            )
        expected_hash = None if previous is None else previous.checkpoint_hash
        if expected_previous_checkpoint_hash != expected_hash:
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint compare-and-swap predecessor changed",
                code="workflow_checkpoint_cas_failed",
            )
        candidate_chain = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        ) + (checkpoint,)
        try:
            validate_workflow_checkpoint_chain(plan, candidate_chain)
        except ValueError as exc:
            raise AgentWorkflowCheckpointConflict(
                "Workflow checkpoint cannot extend the immutable plan chain",
                code="workflow_checkpoint_sequence_conflict",
            ) from exc
        self._validate_workflow_execution_binding(
            connection,
            plan,
            checkpoint,
        )

    @staticmethod
    def _validate_existing_provider_boundary(
        run: sqlite3.Row,
        plan: WorkflowPlan,
        step_checkpoints: tuple[WorkflowStepCheckpoint, ...],
        boundary: ProviderIoBoundary,
    ) -> None:
        if not step_checkpoints:
            raise AgentWorkflowCheckpointConflict(
                "Provider I/O boundary has no completed workflow step",
                code="workflow_checkpoint_integrity_failed",
            )
        last = step_checkpoints[-1]
        final_step = plan.steps[-1]
        expected_executor = ExecutorIdentity.model_validate_json(str(run["executor_json"]))
        if (
            boundary.workflow_plan_id != plan.workflow_plan_id
            or boundary.plan_hash != plan.plan_hash
            or boundary.run_id != plan.run_id
            or boundary.last_step_checkpoint_id != last.checkpoint_id
            or boundary.last_step_checkpoint_hash != last.checkpoint_hash
            or last.step_ordinal != len(plan.steps)
            or last.execution_status != "succeeded"
            or last.domain_status not in final_step.continue_on_domain_status
            or boundary.executor != expected_executor
        ):
            raise AgentWorkflowCheckpointConflict(
                "Provider I/O boundary does not close the completed workflow chain",
                code="workflow_checkpoint_integrity_failed",
            )

    @classmethod
    def _validate_provider_boundary_append(
        cls,
        run: sqlite3.Row,
        plan: WorkflowPlan,
        chain: tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
        boundary: ProviderIoBoundary,
        *,
        expected_previous_checkpoint_hash: str,
    ) -> None:
        if not chain or not isinstance(chain[-1], WorkflowStepCheckpoint):
            raise AgentWorkflowCheckpointConflict(
                "Provider I/O requires a completed workflow step chain",
                code="workflow_checkpoint_sequence_conflict",
            )
        if any(isinstance(item, ProviderIoBoundary) for item in chain):
            raise AgentWorkflowCheckpointConflict(
                "Run already has a Provider I/O boundary",
                code="workflow_checkpoint_closed",
            )
        last = chain[-1]
        if expected_previous_checkpoint_hash != last.checkpoint_hash:
            raise AgentWorkflowCheckpointConflict(
                "Provider I/O boundary compare-and-swap predecessor changed",
                code="workflow_checkpoint_cas_failed",
            )
        cls._validate_existing_provider_boundary(
            run,
            plan,
            tuple(
                item for item in chain if isinstance(item, WorkflowStepCheckpoint)
            ),
            boundary,
        )

    @staticmethod
    def _require_run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("Agent run was not found")
        return row

    @staticmethod
    def _require_tool_call_row(
        connection: sqlite3.Connection,
        tool_call_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_call WHERE id = ?",
            (tool_call_id,),
        ).fetchone()
        if row is None:
            raise AgentStoreNotFound("ToolCall was not found")
        return row

    @staticmethod
    def _last_activity_id(connection: sqlite3.Connection, run_id: str) -> str | None:
        row = connection.execute(
            "SELECT id FROM activity WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else str(row["id"])

    @staticmethod
    def _message_row_for_run(
        connection: sqlite3.Connection,
        run_id: str,
        role: str,
        *,
        first: bool = False,
    ) -> sqlite3.Row:
        row = connection.execute(
            f"""
            SELECT sm.*, a.payload_json
            FROM session_message AS sm
            JOIN activity AS a ON a.id = sm.activity_id
            WHERE sm.run_id = ? AND sm.role = ?
            ORDER BY sm.ordinal {'ASC' if first else 'DESC'} LIMIT 1
            """,
            (run_id, role),
        ).fetchone()
        if row is None:  # pragma: no cover - run/message transaction invariant.
            raise RuntimeError(f"Run has no persisted {role} message")
        return row

    @staticmethod
    def _insert_activity(
        connection: sqlite3.Connection,
        run: sqlite3.Row,
        *,
        sequence: int,
        kind: ActivityKind,
        payload: dict[str, Any],
        causation_id: str | None,
        created_at: datetime,
        revision_override: tuple[int, str] | None = None,
    ) -> ActivityItem:
        activity_id = _new_id("activity")
        # revision_override：这场对话自己直接写过文件、版本往前走了以后提的「先看修改」，按提案的基准记
        # （9-27 真机：先 edit_file 再提案，校验「提案基准 = 对话开头的版本」对不上，整轮报「程序出错了」）。
        revision, fingerprint = revision_override or (run["project_revision"], run["project_fingerprint"])
        # 无项目 Run（随便聊）三列均为空，活动不带项目身份。
        project_revision = (
            ProjectRevision(
                project_id=str(run["project_binding_id"]),
                revision=int(revision),
                fingerprint=str(fingerprint),
                observed_at=_parse_time(run["created_at"]),
            )
            if run["project_binding_id"] is not None
            and run["project_revision"] is not None
            and run["project_fingerprint"] is not None
            else None
        )
        item = ActivityItem.model_validate(
            {
                "schema": "zhimeng.activity-item.v1",
                "activity_id": activity_id,
                "session_id": str(run["session_id"]),
                "run_id": str(run["id"]),
                "sequence": sequence,
                "created_at": created_at,
                "project_revision": project_revision,
                "correlation_id": str(run["correlation_id"]),
                "causation_id": causation_id,
                "kind": kind,
                "payload": payload,
            }
        )
        rendered = item.model_dump(mode="json", by_alias=True)
        connection.execute(
            """
            INSERT INTO activity(
                id, session_id, run_id, sequence, kind, payload_json,
                project_revision, project_fingerprint, correlation_id,
                causation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity_id,
                str(run["session_id"]),
                str(run["id"]),
                sequence,
                kind.value,
                _json(rendered),
                revision,
                fingerprint,
                str(run["correlation_id"]),
                causation_id,
                created_at.isoformat(),
            ),
        )
        event_sequence_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_event WHERE run_id = ?",
            (str(run["id"]),),
        ).fetchone()
        event_sequence = int(event_sequence_row[0])
        connection.execute(
            """
            INSERT INTO run_event(
                run_id, sequence, event_type, activity_id, payload_json, created_at
            ) VALUES (?, ?, 'activity', ?, ?, ?)
            """,
            (
                str(run["id"]),
                event_sequence,
                activity_id,
                _json({"activity_id": activity_id}),
                created_at.isoformat(),
            ),
        )
        return item

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> AgentSessionResponse:
        binding_id = row["project_binding_id"]
        revision_value = row["latest_project_revision"]
        fingerprint_value = row["latest_project_fingerprint"]
        project_revision = (
            ProjectRevision(
                project_id=str(binding_id),
                revision=int(revision_value),
                fingerprint=str(fingerprint_value),
                observed_at=_parse_time(row["updated_at"]),
            )
            if binding_id is not None
            and revision_value is not None
            and fingerprint_value is not None
            else None
        )
        return AgentSessionResponse(
            session_id=str(row["id"]),
            project_id=str(binding_id) if binding_id is not None else None,
            project_revision=project_revision,
            title=str(row["title"]),
            pinned=bool(row["pinned"]),
            status=SessionStatus(str(row["status"])),
            revision=int(row["revision"]),
            executor=ExecutorIdentity.model_validate_json(str(row["executor_json"])),
            capabilities=CapabilityProfile.model_validate_json(
                str(row["capabilities_json"])
            ),
            correlation_id=str(row["correlation_id"]),
            last_run_ordinal=int(row["last_run_ordinal"]),
            last_message_ordinal=int(row["last_message_ordinal"]),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Run:
        status = RunStatus(str(row["status"]))
        error = (
            ErrorEnvelope.model_validate_json(str(row["error_json"]))
            if row["error_json"] is not None
            else None
        )
        return Run(
            schema="zhimeng.execution-run.v1",
            run_id=str(row["id"]),
            session_id=str(row["session_id"]),
            ordinal=int(row["ordinal"]),
            status=status,
            objective=str(row["objective"]),
            origin_kind=str(row["origin_kind"]),
            stop_generation=int(row["stop_generation"]),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
            started_at=_parse_time(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                _parse_time(row["completed_at"]) if row["completed_at"] else None
            ),
            executor=ExecutorIdentity.model_validate_json(str(row["executor_json"])),
            capabilities=CapabilityProfile.model_validate_json(
                str(row["capabilities_json"])
            ),
            # 无项目 Run（随便聊）三列均为空；绑定项目的 Run 照旧携带快照。
            project_revision=(
                ProjectRevision(
                    project_id=str(row["project_binding_id"]),
                    revision=int(row["project_revision"]),
                    fingerprint=str(row["project_fingerprint"]),
                    observed_at=_parse_time(row["created_at"]),
                )
                if row["project_binding_id"] is not None
                and row["project_revision"] is not None
                and row["project_fingerprint"] is not None
                else None
            ),
            correlation_id=str(row["correlation_id"]),
            causation_id=str(row["causation_id"]) if row["causation_id"] else None,
            last_activity_sequence=int(row["last_activity_sequence"]),
            checkpoint_id=None,
            error=error,
        )

    @staticmethod
    def _tool_call_from_row(row: sqlite3.Row) -> StoredToolCall:
        def decode_json(value: object) -> Any:
            if value is None:
                return None
            try:
                return json.loads(str(value))
            except json.JSONDecodeError:
                return str(value)

        error = (
            ErrorEnvelope.model_validate_json(str(row["error_json"]))
            if row["error_json"] is not None
            else None
        )
        return StoredToolCall(
            tool_call_id=str(row["id"]),
            run_id=str(row["run_id"]),
            tool_name=str(row["tool_name"]),
            tool_version=str(row["tool_version"]),
            status=str(row["status"]),
            idempotency_key=(
                str(row["idempotency_key"])
                if row["idempotency_key"] is not None
                else None
            ),
            request_hash=str(row["request_hash"]),
            arguments=decode_json(row["input_json"]),
            output=decode_json(row["output_json"]),
            error=error,
            call_activity_id=(
                str(row["call_activity_id"])
                if row["call_activity_id"] is not None
                else None
            ),
            result_activity_id=(
                str(row["result_activity_id"])
                if row["result_activity_id"] is not None
                else None
            ),
            result_hash=(
                str(row["result_hash"]) if row["result_hash"] is not None else None
            ),
            duration_ms=(
                int(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            started_at=(
                _parse_time(row["started_at"])
                if row["started_at"] is not None
                else None
            ),
            finished_at=(
                _parse_time(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> AgentMessageResponse:
        activity = ActivityItem.model_validate_json(str(row["payload_json"])).root
        if activity.kind is not ActivityKind.MESSAGE:
            raise RuntimeError("session_message points to a non-message activity")
        payload = activity.payload
        if payload.phase != "completed" or payload.content is None:
            raise RuntimeError("session_message points to an incomplete message")
        return AgentMessageResponse(
            images=json.loads(row["images_json"]) if "images_json" in row.keys() else [],
            message_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            ordinal=int(row["ordinal"]),
            role=str(row["role"]),
            format=str(row["format"]),
            content=payload.content,
            created_at=_parse_time(row["created_at"]),
            usage=(
                payload.usage.model_dump(mode="json")
                if payload.usage is not None
                else None
            ),
        )
