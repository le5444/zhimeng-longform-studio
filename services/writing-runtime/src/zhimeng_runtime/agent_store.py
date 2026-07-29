from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

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
    AgentMessageResponse,
    AgentSessionResponse,
)
from zhimeng_runtime.agent_tools import (
    CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
    CREATION_HANDOFF_SKILL_ID,
)
from zhimeng_runtime.change_set_models import (
    ChangeSetResponse,
    change_set_payload_hash,
)
from zhimeng_runtime.context_models import (
    CompletedHistoryMessage,
    ContextPack,
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
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


class AgentStoreError(RuntimeError):
    pass


class AgentStoreNotFound(AgentStoreError):
    pass


class AgentStoreConflict(AgentStoreError):
    pass


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
        project_id: str,
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

    def get_session(self, session_id: str) -> AgentSessionResponse:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise AgentStoreNotFound("Agent session was not found")
        return self._session_from_row(row)

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
                WHERE project_binding_id = ?
                ORDER BY updated_at DESC, id ASC
                """,
                (project_id,),
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
        expected_project_revision: int,
        expected_project_fingerprint: str,
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
    ) -> tuple[Run, AgentMessageResponse, bool]:
        if (context_pack_id is None) != (expected_context_pack_hash is None):
            raise AgentStoreInvalidState(
                "context_pack_id and expected_context_pack_hash must be provided together"
            )
        if (current_executor is None) != (current_capabilities is None):
            raise AgentStoreInvalidState(
                "current executor and capabilities must be provided together"
            )
        if writer_mode == "author_review_change_proposal" and (
            context_pack_id is None or active_chapter_id is None
        ):
            raise AgentStoreInvalidState(
                "author-review Writer mode requires a Context Pack and active chapter"
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
                if str(session["status"]) not in {"active", "idle"}:
                    raise AgentStoreInvalidState("Agent session is not active")
                existing = connection.execute(
                    "SELECT * FROM run WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_hash"]) != request_hash:
                        raise AgentStoreConflict(
                            "Idempotency key was already used with a different request"
                        )
                    if existing["context_pack_id"] is not None:
                        self._require_run_project_kb_binding(connection, existing)
                    message_row = self._message_row_for_run(connection, str(existing["id"]), "user")
                    connection.commit()
                    return (
                        self._run_from_row(existing),
                        self._message_from_row(message_row),
                        True,
                    )

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

                project_id = str(session["project_binding_id"])
                project = connection.execute(
                    "SELECT * FROM project_binding WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if project is None:
                    raise AgentStoreConflict("Session project binding no longer exists")
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
                    self._require_project_kb_binding(context_pack)
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
                if run is None:  # pragma: no cover - insert invariant.
                    raise RuntimeError("run transaction produced no row")
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
                        "project_kb_state_id": str(
                            context_pack["project_kb_state_id"]
                        ),
                        "project_kb_manifest_hash": str(
                            context_pack["project_kb_manifest_hash"]
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
                    writer_intent = {
                        "schema": "zhimeng.writer-intent.v1",
                        "mode": writer_mode,
                        "active_chapter_id": active_chapter_id,
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
                    kind=ActivityKind.MESSAGE,
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
                    ).model_dump(mode="json"),
                    causation_id=causation_id,
                    created_at=created_at,
                )
                message_ordinal = int(session["last_message_ordinal"]) + 1
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
                message_row = self._message_row_for_run(connection, run_id, "user")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._run_from_row(run), self._message_from_row(message_row), False

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
                WHERE session_id = ?
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
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._tool_call_from_row(stored), False

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
        with self.database.connection() as connection:
            run = connection.execute("SELECT id FROM run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise AgentStoreNotFound("Agent run was not found")
            rows = connection.execute(
                """
                SELECT payload_json FROM activity
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [ActivityItem.model_validate_json(str(row["payload_json"])) for row in rows]

    def list_run_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
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
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [
            StoredRunEvent(
                sequence=int(row["sequence"]),
                activity=ActivityItem.model_validate_json(str(row["payload_json"])),
            )
            for row in rows
        ]

    def list_messages(
        self,
        session_id: str,
        *,
        after_ordinal: int = 0,
        limit: int = 200,
    ) -> list[AgentMessageResponse]:
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            rows = connection.execute(
                """
                SELECT sm.*, a.payload_json
                FROM session_message AS sm
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.session_id = ? AND sm.ordinal > ?
                ORDER BY sm.ordinal ASC
                LIMIT ?
                """,
                (session_id, after_ordinal, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def provider_history(
        self,
        session_id: str,
        *,
        max_messages: int = 40,
        max_characters: int = 120_000,
    ) -> list[dict[str, str]]:
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            rows = connection.execute(
                """
                SELECT sm.*, a.payload_json
                FROM session_message AS sm
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.session_id = ?
                ORDER BY sm.ordinal DESC
                LIMIT 500
                """,
                (session_id,),
            ).fetchall()
        messages = [self._message_from_row(row) for row in reversed(rows)]
        selected: list[AgentMessageResponse] = []
        character_count = 0
        for message in reversed(messages):
            if message.role not in {"user", "assistant"}:
                continue
            next_count = character_count + len(message.content)
            if selected and (len(selected) >= max_messages or next_count > max_characters):
                break
            selected.append(message)
            character_count = next_count
        selected.reverse()
        return [{"role": item.role, "content": item.content} for item in selected]

    def completed_history(
        self,
        session_id: str,
        *,
        max_runs: int = 20,
        max_characters: int = 120_000,
    ) -> tuple[int, list[CompletedHistoryMessage]]:
        """Return only complete successful turns for a future Context Pack."""

        if max_runs < 1 or max_characters < 1:
            raise ValueError("history limits must be positive")
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT last_message_ordinal FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise AgentStoreNotFound("Agent session was not found")
            rows = connection.execute(
                """
                WITH completed_runs AS (
                    SELECT id, ordinal
                    FROM run
                    WHERE session_id = ? AND status = 'succeeded'
                    ORDER BY ordinal DESC
                    LIMIT ?
                )
                SELECT sm.*, a.payload_json, cr.ordinal AS run_ordinal
                FROM completed_runs AS cr
                JOIN session_message AS sm ON sm.run_id = cr.id
                JOIN activity AS a ON a.id = sm.activity_id
                WHERE sm.role IN ('user', 'assistant')
                ORDER BY cr.ordinal DESC, sm.ordinal ASC
                """,
                (session_id, max_runs),
            ).fetchall()

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
        for turn in turns:
            if [message.role for message in turn] != ["user", "assistant"]:
                raise AgentStoreInvalidState(
                    "A succeeded Run must have one complete user/assistant message pair"
                )
            next_count = character_count + sum(len(message.content) for message in turn)
            if next_count > max_characters:
                break
            selected_newest_first.append(turn)
            character_count = next_count

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
                self._require_project_kb_binding(row)

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
                if RunStatus(str(run["status"])) is not RunStatus.RUNNING:
                    raise AgentStoreInvalidState("Assistant deltas require a running Run")
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

    def complete_run(
        self,
        run_id: str,
        *,
        message_id: str,
        content: str,
        finish_reason: str,
        usage: MessageUsage | None,
        current_project_kb_verifier: CurrentProjectKbVerifier | None = None,
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
                )
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
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not content.strip():
            raise AgentStoreInvalidState("A successful Run requires non-blank output")
        if finish_reason in {"length", "tool_calls", "function_call"}:
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
        managed_workflow = connection.execute(
            """
            SELECT * FROM checkpoint
            WHERE run_id = ? AND sequence = 0 AND checkpoint_type = ?
            LIMIT 1
            """,
            (run_id, WORKFLOW_PLAN_CHECKPOINT_TYPE),
        ).fetchone()
        candidate_required = False
        if managed_workflow is not None:
            plan = self._workflow_plan_from_row(managed_workflow)
            candidate_required = (
                plan.steps[-1].skill_id == CREATION_HANDOFF_SKILL_ID
            )
        if candidate_required:
            candidate_row = connection.execute(
                "SELECT * FROM agent_candidate_output WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if candidate_row is None:
                raise AgentStoreInvalidState(
                    "A Creation handoff Workflow Run requires exactly one candidate output"
                )
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
            WHERE run_id = ? AND status IN ('pending', 'running')
            ORDER BY started_at, id
            """,
            (str(run["id"]),),
        ).fetchall()
        for tool_call in rows:
            tool_call_id = str(tool_call["id"])
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
                    output={"reason": detail},
                    error=None,
                    duration_ms=None,
                )
            else:
                retryable = target_status is RunStatus.INTERRUPTED or bool(
                    run_error and run_error.error.retryable
                )
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
                run = self._close_open_tool_calls(
                    connection,
                    run,
                    target_status=status,
                    detail=detail,
                    run_error=error,
                    created_at=now,
                )
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
                    valid = (
                        payload is not None
                        and canonical_json_hash(payload) == reference.reference_hash
                    )
            if not valid:
                raise AgentCandidateOutputConflict(
                    "Agent candidate reference is not same-Run persisted evidence",
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
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT sm.*, a.payload_json
            FROM session_message AS sm
            JOIN activity AS a ON a.id = sm.activity_id
            WHERE sm.run_id = ? AND sm.role = ?
            ORDER BY sm.ordinal DESC LIMIT 1
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
    ) -> ActivityItem:
        activity_id = _new_id("activity")
        project_revision = ProjectRevision(
            project_id=str(run["project_binding_id"]),
            revision=int(run["project_revision"]),
            fingerprint=str(run["project_fingerprint"]),
            observed_at=_parse_time(run["created_at"]),
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
                int(run["project_revision"]),
                str(run["project_fingerprint"]),
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
        fingerprint = str(row["latest_project_fingerprint"])
        return AgentSessionResponse(
            session_id=str(row["id"]),
            project_revision=ProjectRevision(
                project_id=str(row["project_binding_id"]),
                revision=int(row["latest_project_revision"]),
                fingerprint=fingerprint,
                observed_at=_parse_time(row["updated_at"]),
            ),
            title=str(row["title"]),
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
            project_revision=ProjectRevision(
                project_id=str(row["project_binding_id"]),
                revision=int(row["project_revision"]),
                fingerprint=str(row["project_fingerprint"]),
                observed_at=_parse_time(row["created_at"]),
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
            message_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            ordinal=int(row["ordinal"]),
            role=str(row["role"]),
            format=str(row["format"]),
            content=payload.content,
            created_at=_parse_time(row["created_at"]),
        )
