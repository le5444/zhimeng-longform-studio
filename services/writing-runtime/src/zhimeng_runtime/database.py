from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from zhimeng_runtime.knowledge_scope_models import (
    APPLICATION_KNOWLEDGE_SCOPE_ID,
    knowledge_scope_id,
)


SCHEMA_VERSION = 33

LEGACY_EXECUTOR = {
    "executor_id": "executor.migrated-legacy",
    "kind": "embedded",
    "implementation": "legacy-runtime-record",
    "version": "0",
    "protocol_version": "1",
    "instance_id": None,
}
LEGACY_CAPABILITIES = {
    "protocol_version": "1",
    "names": [],
    "extensions": {},
}

REQUIRED_TABLES = frozenset(
    {
        "project_binding",
        "execution_session",
        "run",
        "run_event",
        "activity",
        "session_message",
        "artifact",
        "approval",
        "tool_call",
        "checkpoint",
        "skill_version",
        "skill_import_receipt",
        "skill_activation",
        "skill_activation_receipt",
        "change_set",
        "post_draft_gate_receipt",
        "change_set_provenance",
        "source_import_job",
        "source_import_execution_receipt",
        "source_import_attempt_receipt",
        "distillation_invocation",
        "distillation_invocation_receipt",
        "context_pack",
        "nlcl_route_receipt",
        "prewrite_operation_package",
        "agent_candidate_output",
        "nlcl_route_execution_receipt",
        "knowledge_candidate",
        "knowledge_card",
        "knowledge_card_version",
        "knowledge_version_lineage_receipt",
        "knowledge_formalization_receipt",
        "knowledge_scope",
        "knowledge_source_scope",
        "knowledge_scope_card",
        "skill_handoff_run",
        "context_handoff_run",
        "file_change",
        "run_transcript",
        "session_queue",
        "session_goal",
        "goal_event",
        "session_steering",
        "session_admission_withdrawal",
        "agent_image",
        "command_completion",
        "command_completion_scope",
    }
)

REQUIRED_COLUMNS = {
    "session_goal": frozenset({"goal_id", "session_id", "objective", "status", "revision", "run_id", "stage_count", "progress", "evidence", "pause_reason", "requested_status", "created_at", "updated_at", "status_source"}),
    "goal_event": frozenset({"goal_id", "revision", "kind", "state_json", "created_at"}),
    "file_change": frozenset({"apply_phase"}),
    "command_completion": frozenset({"sequence", "event_id", "command_session_id", "owner_session_id", "owner_run_id", "status", "exit_code", "ended_at", "payload_json", "delivery_state", "delivered_run_id", "stop_generation", "created_at"}),
    "command_completion_scope": frozenset({"session_id", "stop_generation"}),
    "agent_image": frozenset({"id", "session_id", "name", "mime_type", "width", "height", "data", "created_at"}),
    "session_admission_withdrawal": frozenset({"session_id", "idempotency_key", "created_at"}),
    "session_steering": frozenset({"session_id", "item_id", "run_id", "input_json", "status", "message_id"}),
    "run_transcript": frozenset({"run_id", "session_id", "messages_json", "through_message_ordinal"}),
    "session_queue": frozenset({"session_id", "revision", "state_json"}),
    "project_binding": frozenset(
        {
            "id",
            "root_path",
            "display_name",
            "fingerprint",
            "revision",
            "created_at",
            "updated_at",
        }
    ),
    "run": frozenset(
        {
            "id",
            "session_id",
            "project_binding_id",
            "ordinal",
            "objective",
            "project_revision",
            "project_fingerprint",
            "status",
            "executor_json",
            "capabilities_json",
            "correlation_id",
            "last_activity_sequence",
            "request_hash",
            "context_pack_id",
            "origin_kind",
            "stop_generation",
        }
    ),
    "execution_session": frozenset(
        {
            "id",
            "project_binding_id",
            "title",
            "pinned",
            "status",
            "revision",
            "latest_project_revision",
            "latest_project_fingerprint",
            "executor_json",
            "capabilities_json",
            "correlation_id",
            "last_run_ordinal",
            "last_message_ordinal",
            "created_at",
            "updated_at",
        }
    ),
    "context_pack": frozenset(
        {
            "id",
            "session_id",
            "project_binding_id",
            "history_through_ordinal",
            "objective",
            "active_chapter_id",
            "profile",
            "project_revision",
            "project_fingerprint",
            "project_kb_state_id",
            "project_kb_manifest_hash",
            "knowledge_fingerprint",
            "readiness",
            "pack_hash",
            "injection_hash",
            "provider_messages_hash",
            "payload_json",
            "created_at",
        }
    ),
    "nlcl_route_receipt": frozenset(
        {
            "id",
            "context_pack_id",
            "project_binding_id",
            "context_pack_hash",
            "project_revision",
            "project_fingerprint",
            "project_kb_state_id",
            "project_kb_manifest_hash",
            "active_chapter_id",
            "profile",
            "objective_hash",
            "knowledge_fingerprint",
            "source",
            "route_status",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "nlcl_route_execution_receipt": frozenset(
        {
            "id",
            "route_receipt_id",
            "context_pack_id",
            "route_receipt_hash",
            "context_pack_hash",
            "project_kb_state_id",
            "project_kb_manifest_hash",
            "call_ordinal",
            "run_id",
            "tool_call_id",
            "skill_call_id",
            "skill_receipt_id",
            "skill_receipt_hash",
            "execution_status",
            "domain_status",
            "output_hash",
            "tool_result_hash",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "prewrite_operation_package": frozenset(
        {
            "id",
            "context_pack_id",
            "route_receipt_id",
            "project_binding_id",
            "context_pack_hash",
            "route_receipt_hash",
            "project_revision",
            "project_fingerprint",
            "active_chapter_id",
            "readiness",
            "package_hash",
            "payload_json",
            "created_at",
        }
    ),
    "run_event": frozenset(
        {
            "id",
            "run_id",
            "sequence",
            "event_type",
            "activity_id",
            "payload_json",
            "created_at",
        }
    ),
    "agent_candidate_output": frozenset(
        {
            "id",
            "run_id",
            "workflow_plan_id",
            "workflow_plan_hash",
            "final_checkpoint_id",
            "final_checkpoint_hash",
            "provider_boundary_id",
            "provider_boundary_hash",
            "provider_request_hash",
            "context_pack_id",
            "context_pack_hash",
            "operation_package_id",
            "operation_package_hash",
            "project_binding_id",
            "project_revision",
            "project_fingerprint",
            "handoff_status",
            "allowed_output",
            "failure_signatures_json",
            "formal_draft_allowed",
            "requires_author_review",
            "kind",
            "content_hash",
            "references_hash",
            "envelope_hash",
            "payload_json",
            "created_at",
        }
    ),
    "skill_handoff_run": frozenset(
        {
            "id",
            "handoff_key",
            "project_binding_id",
            "status",
            "project_revision",
            "project_fingerprint",
            "source_import_id",
            "formalization_receipt_id",
            "formalization_receipt_hash",
            "knowledge_fingerprint",
            "upstream_cards_hash",
            "workflow_plan_id",
            "workflow_plan_hash",
            "checkpoint_id",
            "checkpoint_hash",
            "skill_version_id",
            "skill_receipt_id",
            "skill_receipt_hash",
            "result_hash",
            "artifact_hash",
            "payload_json",
            "created_at",
            "updated_at",
        }
    ),
    "context_handoff_run": frozenset(
        {
            "id",
            "handoff_key",
            "project_binding_id",
            "status",
            "project_revision",
            "project_fingerprint",
            "source_handoff_id",
            "distillation_index_hash",
            "workflow_plan_id",
            "workflow_plan_hash",
            "checkpoint_id",
            "checkpoint_hash",
            "skill_version_id",
            "skill_receipt_id",
            "skill_receipt_hash",
            "result_hash",
            "artifact_hash",
            "context_pack_id",
            "context_pack_hash",
            "route_receipt_id",
            "route_receipt_hash",
            "payload_json",
            "created_at",
            "updated_at",
        }
    ),
    "skill_version": frozenset(
        {
            "id",
            "skill_id",
            "version",
            "source_path",
            "fingerprint",
            "manifest_fingerprint",
            "adapter_id",
            "adapter_version",
            "adapter_fingerprint",
            "input_contract_fingerprint",
            "output_contract_fingerprint",
            "contract_bundle_fingerprint",
            "file_count",
            "total_bytes",
            "snapshot_status",
            "status",
            "registered_at",
        }
    ),
    "skill_import_receipt": frozenset(
        {
            "id",
            "scope_key",
            "scope_type",
            "project_binding_id",
            "skill_version_id",
            "source_kind",
            "source_reference",
            "source_locator_hash",
            "source_package_fingerprint",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "skill_activation": frozenset(
        {
            "scope_key",
            "scope_type",
            "project_binding_id",
            "skill_id",
            "state",
            "active_version_id",
            "state_revision",
            "updated_at",
        }
    ),
    "skill_activation_receipt": frozenset(
        {
            "id",
            "scope_key",
            "scope_type",
            "project_binding_id",
            "skill_id",
            "action",
            "previous_state",
            "previous_version_id",
            "next_state",
            "next_version_id",
            "expected_state_revision",
            "expected_version_id",
            "state_revision",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "activity": frozenset(
        {
            "id",
            "session_id",
            "run_id",
            "sequence",
            "kind",
            "payload_json",
            "project_revision",
            "project_fingerprint",
            "correlation_id",
            "causation_id",
            "created_at",
        }
    ),
    "session_message": frozenset(
        {
            "activity_id",
            "session_id",
            "run_id",
            "message_id",
            "ordinal",
            "role",
            "format",
            "images_json",
            "created_at",
        }
    ),
    "change_set": frozenset(
        {
            "id",
            "project_binding_id",
            "status",
            "base_revision",
            "base_fingerprint",
            "review_revision",
            "payload_json",
            "created_at",
            "updated_at",
        }
    ),
    "post_draft_gate_receipt": frozenset(
        {
            "id",
            "change_set_id",
            "project_binding_id",
            "attempt_ordinal",
            "review_revision",
            "candidate_hash",
            "base_revision",
            "base_fingerprint",
            "context_pack_id",
            "context_pack_hash",
            "knowledge_fingerprint",
            "validator_id",
            "validator_version",
            "validator_manifest_fingerprint",
            "validator_package_fingerprint",
            "candidate_file_count",
            "skill_receipt_id",
            "skill_receipt_hash",
            "status",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "tool_call": frozenset(
        {
            "id",
            "run_id",
            "tool_name",
            "tool_version",
            "status",
            "idempotency_key",
            "request_hash",
            "input_json",
            "output_json",
            "error_json",
            "call_activity_id",
            "result_activity_id",
            "result_hash",
            "duration_ms",
            "started_at",
            "finished_at",
        }
    ),
    "change_set_provenance": frozenset(
        {
            "change_set_id",
            "run_id",
            "tool_call_id",
            "context_pack_id",
            "project_id",
            "project_revision",
            "project_fingerprint",
            "request_hash",
            "created_at",
        }
    ),
    "source_import_job": frozenset(
        {
            "id",
            "provider",
            "query",
            "book_id",
            "source_key",
            "status",
            "payload_json",
            "created_at",
            "updated_at",
        }
    ),
    "source_import_execution_receipt": frozenset(
        {
            "id",
            "source_import_id",
            "run_id",
            "revision",
            "idempotency_key",
            "request_hash",
            "prepare_invocation_id",
            "prepare_tool_fingerprint",
            "source_packet_fingerprint",
            "distillation_invocation_id",
            "distillation_tool_fingerprint",
            "distillation_input_hash",
            "distillation_output_hash",
            "draft_fingerprint",
            "cards_draft_hash",
            "envelope_hash",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "source_import_attempt_receipt": frozenset(
        {
            "id",
            "source_import_id",
            "attempt_ordinal",
            "status",
            "stage",
            "provider",
            "request_hash",
            "execution_receipt_id",
            "execution_receipt_hash",
            "failure_signature",
            "cleanup_status",
            "cleanup_hash",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "distillation_invocation": frozenset(
        {
            "id",
            "source_import_id",
            "source_receipt_id",
            "source_receipt_hash",
            "source_packet_fingerprint",
            "packet_text_hash",
            "project_binding_id",
            "project_revision",
            "project_fingerprint",
            "knowledge_scope_id",
            "knowledge_scope_state_revision",
            "knowledge_fingerprint",
            "project_kb_state_id",
            "project_kb_manifest_hash",
            "skill_id",
            "skill_version_id",
            "skill_version",
            "skill_manifest_fingerprint",
            "skill_package_fingerprint",
            "activation_receipt_id",
            "activation_receipt_hash",
            "activation_state_revision",
            "effective_scope",
            "instruction_materialization_id",
            "instruction_materialization_hash",
            "managed_instruction_hash",
            "provider_executor_json",
            "provider_capabilities_json",
            "requested_model",
            "accepted_reported_models_json",
            "dispatch_config_fingerprint",
            "credential_reference",
            "tool_definitions_hash",
            "anchor_catalog_id",
            "anchor_catalog_hash",
            "retry_of_invocation_id",
            "idempotency_key",
            "request_hash",
            "status",
            "state_revision",
            "provider_replay_count",
            "last_receipt_ordinal",
            "last_receipt_id",
            "last_receipt_hash",
            "last_dispatch_receipt_id",
            "last_dispatch_receipt_hash",
            "last_dispatch_turn_ordinal",
            "last_turn_result_receipt_id",
            "last_turn_result_receipt_hash",
            "provider_turn_count",
            "provider_turn_chain_hash",
            "candidate_output_receipt_id",
            "candidate_output_receipt_hash",
            "recovery_dossier_receipt_id",
            "recovery_dossier_revision",
            "recovery_dossier_receipt_hash",
            "recovery_decision_receipt_id",
            "recovery_decision_receipt_hash",
            "final_result_receipt_id",
            "final_result_receipt_hash",
            "candidate_set_hash",
            "work_count",
            "voice_count",
            "validator_count",
            "payload_json",
            "head_hash",
            "created_at",
            "updated_at",
        }
    ),
    "distillation_invocation_receipt": frozenset(
        {
            "id",
            "invocation_id",
            "ordinal",
            "kind",
            "turn_ordinal",
            "parent_receipt_id",
            "expected_invocation_revision",
            "resulting_invocation_revision",
            "resulting_status",
            "idempotency_key",
            "payload_hash",
            "receipt_hash",
            "payload_json",
            "created_at",
        }
    ),
    "knowledge_candidate": frozenset(
        {
            "id",
            "source_import_id",
            "source_card_index",
            "source_draft_hash",
            "lineage_status",
            "origin_json",
            "origin_hash",
            "runtime_distillation_invocation_id",
            "card_id",
            "card_type",
            "status",
            "revision",
            "content_hash",
            "payload_json",
            "issues_json",
            "promoted_version_id",
            "created_at",
            "updated_at",
        }
    ),
    "knowledge_card": frozenset(
        {
            "card_id",
            "status",
            "state_revision",
            "active_version_id",
            "created_at",
            "updated_at",
        }
    ),
    "knowledge_card_version": frozenset(
        {
            "id",
            "card_id",
            "version_number",
            "content_hash",
            "payload_json",
            "source_candidate_id",
            "created_at",
        }
    ),
    "knowledge_version_lineage_receipt": frozenset(
        {
            "id",
            "version_id",
            "candidate_id",
            "source_import_id",
            "source_receipt_id",
            "source_receipt_hash",
            "candidate_origin_hash",
            "approved_candidate_revision",
            "approved_candidate_content_hash",
            "activation_mode",
            "replacement_authorized",
            "activation_expected_version_id",
            "activation_expected_state_revision",
            "receipt_hash",
            "payload_json",
            "created_at",
            "scope_id",
        }
    ),
    "knowledge_formalization_receipt": frozenset(
        {
            "id",
            "source_import_id",
            "source_receipt_id",
            "source_receipt_hash",
            "cards_draft_hash",
            "candidate_set_hash",
            "knowledge_state_hash",
            "receipt_hash",
            "payload_json",
            "created_at",
            "scope_id",
        }
    ),
    "knowledge_scope": frozenset(
        {
            "id",
            "scope_type",
            "project_binding_id",
            "state_revision",
            "created_at",
            "updated_at",
        }
    ),
    "knowledge_source_scope": frozenset(
        {
            "source_import_id",
            "scope_id",
            "bound_at",
        }
    ),
    "knowledge_scope_card": frozenset(
        {
            "scope_id",
            "card_id",
            "status",
            "state_revision",
            "active_version_id",
            "created_at",
            "updated_at",
        }
    ),
}


POST_DRAFT_GATE_RECEIPT_PAYLOAD_COLUMNS = (
    "id",
    "change_set_id",
    "project_binding_id",
    "review_revision",
    "candidate_hash",
    "base_revision",
    "base_fingerprint",
    "context_pack_id",
    "context_pack_hash",
    "knowledge_fingerprint",
    "validator_id",
    "validator_version",
    "validator_manifest_fingerprint",
    "validator_package_fingerprint",
    "candidate_file_count",
    "skill_receipt_id",
    "skill_receipt_hash",
    "status",
    "receipt_hash",
    "payload_json",
    "created_at",
)
POST_DRAFT_GATE_RECEIPT_COLUMNS = (
    "id",
    "change_set_id",
    "project_binding_id",
    "attempt_ordinal",
    *POST_DRAFT_GATE_RECEIPT_PAYLOAD_COLUMNS[3:],
)
POST_DRAFT_GATE_IDENTITY_COLUMNS = (
    "change_set_id",
    "review_revision",
    "candidate_hash",
    "validator_package_fingerprint",
)

POST_DRAFT_GATE_MANAGED_INDEX_SQL = {
    "idx_post_draft_gate_change_set_created": """
CREATE INDEX idx_post_draft_gate_change_set_created
    ON post_draft_gate_receipt(change_set_id, attempt_ordinal DESC)
""",
    "idx_post_draft_gate_identity": """
CREATE INDEX idx_post_draft_gate_identity
    ON post_draft_gate_receipt(
        change_set_id, review_revision, candidate_hash,
        validator_package_fingerprint, attempt_ordinal DESC
    )
""",
    "idx_post_draft_gate_terminal_identity": """
CREATE UNIQUE INDEX idx_post_draft_gate_terminal_identity
    ON post_draft_gate_receipt(
        change_set_id, review_revision, candidate_hash,
        validator_package_fingerprint
    )
    WHERE status IN ('passed', 'blocked')
""",
}


def _compact_schema_sql(value: str) -> str:
    """Normalize managed SQLite DDL without changing quoted values."""

    compact: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            compact.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    compact.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
            compact.append(character.casefold())
        elif not character.isspace() and character != ";":
            compact.append(character.casefold())
        index += 1
    return "".join(compact).replace("ifnotexists", "")


def _check_expressions(table_sql: str) -> Counter[str]:
    """Extract normalized CHECK expressions from controlled SQLite table DDL."""

    expressions: Counter[str] = Counter()
    for match in re.finditer(r"\bCHECK\s*\(", table_sql, flags=re.IGNORECASE):
        start = match.end() - 1
        depth = 0
        quote: str | None = None
        index = start
        while index < len(table_sql):
            character = table_sql[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(table_sql) and table_sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"', "`"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    expression = table_sql[start + 1 : index]
                    expressions[_compact_schema_sql(expression)] += 1
                    break
            index += 1
        else:
            raise RuntimeError("post-draft Gate table contains an invalid CHECK clause")
    return expressions


def _post_draft_gate_receipt_table_sql(
    table_name: str,
    *,
    if_not_exists: bool = False,
) -> str:
    allowed_names = {
        "post_draft_gate_receipt",
        "post_draft_gate_receipt_v16_rebuild",
    }
    if table_name not in allowed_names:
        raise ValueError("unsupported post-draft Gate receipt table name")
    create_guard = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE TABLE {create_guard}{table_name} (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_set(id) ON DELETE CASCADE,
    project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
    review_revision INTEGER NOT NULL CHECK (review_revision >= 0),
    candidate_hash TEXT NOT NULL CHECK (length(candidate_hash) = 64),
    base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
    base_fingerprint TEXT NOT NULL CHECK (length(base_fingerprint) = 64),
    context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
    context_pack_hash TEXT CHECK (
        context_pack_hash IS NULL OR length(context_pack_hash) = 64
    ),
    knowledge_fingerprint TEXT CHECK (
        knowledge_fingerprint IS NULL OR length(knowledge_fingerprint) = 64
    ),
    validator_id TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    validator_manifest_fingerprint TEXT NOT NULL CHECK (
        length(validator_manifest_fingerprint) = 64
    ),
    validator_package_fingerprint TEXT NOT NULL CHECK (
        length(validator_package_fingerprint) = 64
    ),
    candidate_file_count INTEGER NOT NULL CHECK (candidate_file_count >= 1),
    skill_receipt_id TEXT,
    skill_receipt_hash TEXT CHECK (
        skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64
    ),
    status TEXT NOT NULL CHECK (status IN ('passed', 'blocked', 'failed')),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((context_pack_id IS NULL) = (context_pack_hash IS NULL)),
    CHECK ((skill_receipt_id IS NULL) = (skill_receipt_hash IS NULL)),
    UNIQUE (change_set_id, attempt_ordinal)
) STRICT;
"""


SCHEMA_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS project_binding (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS execution_session (
    id TEXT PRIMARY KEY,
    project_binding_id TEXT REFERENCES project_binding(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'idle', 'closing', 'closed', 'interrupted')
    ),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    latest_project_revision INTEGER CHECK (
        latest_project_revision IS NULL OR latest_project_revision >= 1
    ),
    latest_project_fingerprint TEXT CHECK (
        latest_project_fingerprint IS NULL OR length(latest_project_fingerprint) = 64
    ),
    executor_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    last_run_ordinal INTEGER NOT NULL DEFAULT 0 CHECK (last_run_ordinal >= 0),
    last_message_ordinal INTEGER NOT NULL DEFAULT 0 CHECK (last_message_ordinal >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS context_pack (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE RESTRICT,
    project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    history_through_ordinal INTEGER NOT NULL CHECK (history_through_ordinal >= 0),
    objective TEXT NOT NULL,
    active_chapter_id TEXT,
    profile TEXT NOT NULL,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    project_kb_state_id TEXT CHECK (
        project_kb_state_id IS NULL
        OR (
            length(project_kb_state_id) = 49
            AND substr(project_kb_state_id, 1, 17) = 'project-kb-state-'
            AND substr(project_kb_state_id, 18) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    project_kb_manifest_hash TEXT CHECK (
        project_kb_manifest_hash IS NULL
        OR (
            length(project_kb_manifest_hash) = 64
            AND project_kb_manifest_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    knowledge_fingerprint TEXT CHECK (
        knowledge_fingerprint IS NULL OR length(knowledge_fingerprint) = 64
    ),
    readiness TEXT NOT NULL CHECK (readiness IN ('ready', 'degraded', 'blocked')),
    pack_hash TEXT NOT NULL CHECK (length(pack_hash) = 64),
    injection_hash TEXT NOT NULL CHECK (length(injection_hash) = 64),
    provider_messages_hash TEXT NOT NULL CHECK (length(provider_messages_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((project_kb_state_id IS NULL) = (project_kb_manifest_hash IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS nlcl_route_receipt (
    id TEXT PRIMARY KEY,
    context_pack_id TEXT NOT NULL UNIQUE
        REFERENCES context_pack(id) ON DELETE CASCADE,
    project_binding_id TEXT NOT NULL
        REFERENCES project_binding(id) ON DELETE RESTRICT,
    context_pack_hash TEXT NOT NULL CHECK (length(context_pack_hash) = 64),
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    project_kb_state_id TEXT CHECK (
        project_kb_state_id IS NULL
        OR (
            length(project_kb_state_id) = 49
            AND substr(project_kb_state_id, 1, 17) = 'project-kb-state-'
            AND substr(project_kb_state_id, 18) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    project_kb_manifest_hash TEXT CHECK (
        project_kb_manifest_hash IS NULL
        OR (
            length(project_kb_manifest_hash) = 64
            AND project_kb_manifest_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    active_chapter_id TEXT,
    profile TEXT NOT NULL,
    objective_hash TEXT NOT NULL CHECK (length(objective_hash) = 64),
    knowledge_fingerprint TEXT CHECK (
        knowledge_fingerprint IS NULL OR length(knowledge_fingerprint) = 64
    ),
    source TEXT NOT NULL CHECK (
        source IN ('native_knowledge', 'external_distillation', 'unavailable')
    ),
    route_status TEXT NOT NULL CHECK (
        route_status IN ('ready', 'risk_ready', 'unavailable')
    ),
    receipt_hash TEXT NOT NULL CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((project_kb_state_id IS NULL) = (project_kb_manifest_hash IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS prewrite_operation_package (
    id TEXT PRIMARY KEY,
    context_pack_id TEXT NOT NULL UNIQUE
        REFERENCES context_pack(id) ON DELETE CASCADE,
    route_receipt_id TEXT NOT NULL UNIQUE
        REFERENCES nlcl_route_receipt(id) ON DELETE CASCADE,
    project_binding_id TEXT NOT NULL
        REFERENCES project_binding(id) ON DELETE RESTRICT,
    context_pack_hash TEXT NOT NULL CHECK (length(context_pack_hash) = 64),
    route_receipt_hash TEXT NOT NULL CHECK (length(route_receipt_hash) = 64),
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    active_chapter_id TEXT,
    readiness TEXT NOT NULL CHECK (
        readiness IN ('blocked', 'risk_draft_only')
    ),
    package_hash TEXT NOT NULL CHECK (length(package_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS run (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    project_binding_id TEXT REFERENCES project_binding(id) ON DELETE RESTRICT,
    context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    run_type TEXT NOT NULL,
    origin_kind TEXT NOT NULL DEFAULT 'user',
    stop_generation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'starting', 'running', 'awaiting_approval', 'paused',
            'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted'
        )
    ),
    objective TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    project_revision INTEGER CHECK (project_revision IS NULL OR project_revision >= 1),
    project_fingerprint TEXT CHECK (
        project_fingerprint IS NULL OR length(project_fingerprint) = 64
    ),
    executor_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    last_activity_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_activity_sequence >= 0),
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (session_id, ordinal),
    UNIQUE (session_id, idempotency_key)
) STRICT;

CREATE TABLE IF NOT EXISTS run_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    event_type TEXT NOT NULL,
    activity_id TEXT REFERENCES activity(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
) STRICT;

CREATE TABLE IF NOT EXISTS activity (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    project_revision INTEGER CHECK (project_revision IS NULL OR project_revision >= 1),
    project_fingerprint TEXT CHECK (
        project_fingerprint IS NULL OR length(project_fingerprint) = 64
    ),
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
) STRICT;

CREATE TABLE IF NOT EXISTS session_message (
    activity_id TEXT PRIMARY KEY REFERENCES activity(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL UNIQUE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    format TEXT NOT NULL CHECK (format IN ('plain', 'markdown')),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS session_checkpoint (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES run(id) ON DELETE SET NULL,
    through_ordinal INTEGER NOT NULL CHECK (through_ordinal >= 2),
    source_message_count INTEGER NOT NULL CHECK (source_message_count >= 2),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    reported_model TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, through_ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS artifact (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    relative_path TEXT,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS approval (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    artifact_id TEXT REFERENCES artifact(id) ON DELETE SET NULL,
    approval_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS tool_call (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    idempotency_key TEXT,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_json TEXT,
    call_activity_id TEXT UNIQUE REFERENCES activity(id) ON DELETE RESTRICT,
    result_activity_id TEXT UNIQUE REFERENCES activity(id) ON DELETE RESTRICT,
    result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (run_id, idempotency_key)
) STRICT;

CREATE TABLE IF NOT EXISTS nlcl_route_execution_receipt (
    id TEXT PRIMARY KEY,
    route_receipt_id TEXT NOT NULL
        REFERENCES nlcl_route_receipt(id) ON DELETE RESTRICT,
    context_pack_id TEXT NOT NULL
        REFERENCES context_pack(id) ON DELETE RESTRICT,
    route_receipt_hash TEXT NOT NULL CHECK (length(route_receipt_hash) = 64),
    context_pack_hash TEXT NOT NULL CHECK (length(context_pack_hash) = 64),
    project_kb_state_id TEXT CHECK (
        project_kb_state_id IS NULL
        OR (
            length(project_kb_state_id) = 49
            AND substr(project_kb_state_id, 1, 17) = 'project-kb-state-'
            AND substr(project_kb_state_id, 18) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    project_kb_manifest_hash TEXT CHECK (
        project_kb_manifest_hash IS NULL
        OR (
            length(project_kb_manifest_hash) = 64
            AND project_kb_manifest_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    call_ordinal INTEGER NOT NULL CHECK (call_ordinal >= 1 AND call_ordinal <= 64),
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE RESTRICT,
    tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_call(id) ON DELETE RESTRICT,
    skill_call_id TEXT,
    skill_receipt_id TEXT,
    skill_receipt_hash TEXT CHECK (
        skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64
    ),
    execution_status TEXT NOT NULL CHECK (
        execution_status IN ('succeeded', 'failed', 'cancelled')
    ),
    domain_status TEXT CHECK (
        domain_status IS NULL OR domain_status IN ('healthy', 'degraded', 'blocked')
    ),
    output_hash TEXT NOT NULL CHECK (length(output_hash) = 64),
    tool_result_hash TEXT NOT NULL CHECK (length(tool_result_hash) = 64),
    receipt_hash TEXT NOT NULL CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (route_receipt_id, call_ordinal),
    CHECK ((skill_receipt_id IS NULL) = (skill_receipt_hash IS NULL)),
    CHECK ((project_kb_state_id IS NULL) = (project_kb_manifest_hash IS NULL)),
    CHECK (
        execution_status != 'succeeded'
        OR (
            skill_call_id IS NOT NULL
            AND skill_receipt_id IS NOT NULL
            AND domain_status IS NOT NULL
        )
    ),
    CHECK (execution_status = 'succeeded' OR domain_status IS NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS checkpoint (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    checkpoint_type TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
) STRICT;

CREATE TABLE IF NOT EXISTS agent_candidate_output (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES run(id) ON DELETE CASCADE,
    workflow_plan_id TEXT NOT NULL REFERENCES checkpoint(id) ON DELETE RESTRICT,
    workflow_plan_hash TEXT NOT NULL CHECK (length(workflow_plan_hash) = 64),
    final_checkpoint_id TEXT NOT NULL REFERENCES checkpoint(id) ON DELETE RESTRICT,
    final_checkpoint_hash TEXT NOT NULL CHECK (length(final_checkpoint_hash) = 64),
    provider_boundary_id TEXT NOT NULL REFERENCES checkpoint(id) ON DELETE RESTRICT,
    provider_boundary_hash TEXT NOT NULL CHECK (length(provider_boundary_hash) = 64),
    provider_request_hash TEXT NOT NULL CHECK (length(provider_request_hash) = 64),
    context_pack_id TEXT NOT NULL REFERENCES context_pack(id) ON DELETE RESTRICT,
    context_pack_hash TEXT NOT NULL CHECK (length(context_pack_hash) = 64),
    operation_package_id TEXT NOT NULL
        REFERENCES prewrite_operation_package(id) ON DELETE RESTRICT,
    operation_package_hash TEXT NOT NULL CHECK (length(operation_package_hash) = 64),
    project_binding_id TEXT NOT NULL
        REFERENCES project_binding(id) ON DELETE RESTRICT,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    handoff_status TEXT NOT NULL CHECK (
        handoff_status IN ('blocked', 'risk_draft_only')
    ),
    allowed_output TEXT NOT NULL CHECK (
        allowed_output = 'repair_plan_or_risk_draft_only'
    ),
    failure_signatures_json TEXT NOT NULL,
    formal_draft_allowed INTEGER NOT NULL CHECK (formal_draft_allowed = 0),
    requires_author_review INTEGER NOT NULL CHECK (requires_author_review = 1),
    kind TEXT NOT NULL CHECK (
        kind IN ('repair_plan', 'risk_draft', 'change_proposal', 'next_step_advice')
    ),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    references_hash TEXT NOT NULL CHECK (length(references_hash) = 64),
    envelope_hash TEXT NOT NULL UNIQUE CHECK (length(envelope_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        workflow_plan_id != final_checkpoint_id
        AND workflow_plan_id != provider_boundary_id
        AND final_checkpoint_id != provider_boundary_id
    )
) STRICT;

CREATE TABLE IF NOT EXISTS skill_version (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version TEXT NOT NULL,
    source_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    manifest_fingerprint TEXT CHECK (
        manifest_fingerprint IS NULL OR length(manifest_fingerprint) = 64
    ),
    adapter_id TEXT,
    adapter_version TEXT,
    adapter_fingerprint TEXT CHECK (
        adapter_fingerprint IS NULL OR length(adapter_fingerprint) = 64
    ),
    input_contract_fingerprint TEXT CHECK (
        input_contract_fingerprint IS NULL OR length(input_contract_fingerprint) = 64
    ),
    output_contract_fingerprint TEXT CHECK (
        output_contract_fingerprint IS NULL OR length(output_contract_fingerprint) = 64
    ),
    contract_bundle_fingerprint TEXT CHECK (
        contract_bundle_fingerprint IS NULL OR length(contract_bundle_fingerprint) = 64
    ),
    file_count INTEGER CHECK (file_count IS NULL OR file_count >= 1),
    total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 1),
    snapshot_status TEXT NOT NULL DEFAULT 'legacy_unmanaged' CHECK (
        snapshot_status IN ('managed', 'legacy_unmanaged', 'quarantined')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'version_changed', 'disabled')),
    registered_at TEXT NOT NULL,
    UNIQUE (skill_id, fingerprint)
) STRICT;

CREATE TABLE IF NOT EXISTS skill_import_receipt (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('application', 'project')),
    project_binding_id TEXT REFERENCES project_binding(id) ON DELETE CASCADE,
    skill_version_id TEXT NOT NULL REFERENCES skill_version(id) ON DELETE RESTRICT,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('builtin_bundle', 'trusted_import')
    ),
    source_reference TEXT NOT NULL,
    source_locator_hash TEXT NOT NULL CHECK (length(source_locator_hash) = 64),
    source_package_fingerprint TEXT NOT NULL CHECK (
        length(source_package_fingerprint) = 64
    ),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (scope_key, skill_version_id),
    CHECK (
        (scope_type = 'application' AND project_binding_id IS NULL)
        OR (scope_type = 'project' AND project_binding_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS skill_activation (
    scope_key TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('application', 'project')),
    project_binding_id TEXT REFERENCES project_binding(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('enabled', 'disabled', 'inherited')),
    active_version_id TEXT REFERENCES skill_version(id) ON DELETE RESTRICT,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, skill_id),
    CHECK ((state = 'enabled') = (active_version_id IS NOT NULL)),
    CHECK (
        (scope_type = 'application' AND project_binding_id IS NULL AND state != 'inherited')
        OR (scope_type = 'project' AND project_binding_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS skill_activation_receipt (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('application', 'project')),
    project_binding_id TEXT REFERENCES project_binding(id) ON DELETE CASCADE,
    skill_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN (
            'builtin_bootstrap', 'enable', 'disable', 'upgrade', 'rollback', 'inherit'
        )
    ),
    previous_state TEXT NOT NULL CHECK (
        previous_state IN ('unconfigured', 'enabled', 'disabled', 'inherited')
    ),
    previous_version_id TEXT REFERENCES skill_version(id) ON DELETE RESTRICT,
    next_state TEXT NOT NULL CHECK (next_state IN ('enabled', 'disabled', 'inherited')),
    next_version_id TEXT REFERENCES skill_version(id) ON DELETE RESTRICT,
    expected_state_revision INTEGER NOT NULL CHECK (expected_state_revision >= 0),
    expected_version_id TEXT REFERENCES skill_version(id) ON DELETE RESTRICT,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (scope_key, skill_id, state_revision),
    CHECK ((previous_state = 'enabled') = (previous_version_id IS NOT NULL)),
    CHECK ((next_state = 'enabled') = (next_version_id IS NOT NULL)),
    CHECK (
        (scope_type = 'application' AND project_binding_id IS NULL AND next_state != 'inherited')
        OR (scope_type = 'project' AND project_binding_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS change_set (
    id TEXT PRIMARY KEY,
    project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN (
            'reviewing', 'ready', 'applying', 'rolling_back', 'applied', 'rolled_back',
            'discarded', 'conflicted', 'failed'
        )
    ),
    base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
    base_fingerprint TEXT NOT NULL CHECK (length(base_fingerprint) = 64),
    review_revision INTEGER NOT NULL CHECK (review_revision >= 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

{_post_draft_gate_receipt_table_sql("post_draft_gate_receipt", if_not_exists=True)}

CREATE TABLE IF NOT EXISTS change_set_provenance (
    change_set_id TEXT PRIMARY KEY REFERENCES change_set(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE RESTRICT,
    tool_call_id TEXT NOT NULL UNIQUE REFERENCES tool_call(id) ON DELETE RESTRICT,
    context_pack_id TEXT NOT NULL REFERENCES context_pack(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_import_job (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('tomato', 'local_file', 'feedback_experience')),
    query TEXT NOT NULL,
    book_id TEXT,
    source_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'downloading', 'source_ready', 'drafting', 'awaiting_review',
            'formalizing', 'completed', 'cancel_requested', 'cancelled', 'failed',
            'interrupted'
        )
    ),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_import_execution_receipt (
    id TEXT PRIMARY KEY,
    source_import_id TEXT NOT NULL UNIQUE
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    prepare_invocation_id TEXT NOT NULL UNIQUE,
    prepare_tool_fingerprint TEXT NOT NULL CHECK (
        length(prepare_tool_fingerprint) = 64
    ),
    source_packet_fingerprint TEXT NOT NULL CHECK (
        length(source_packet_fingerprint) = 64
    ),
    distillation_invocation_id TEXT NOT NULL UNIQUE,
    distillation_tool_fingerprint TEXT NOT NULL CHECK (
        length(distillation_tool_fingerprint) = 64
    ),
    distillation_input_hash TEXT NOT NULL CHECK (
        length(distillation_input_hash) = 64
    ),
    distillation_output_hash TEXT NOT NULL CHECK (
        length(distillation_output_hash) = 64
    ),
    draft_fingerprint TEXT NOT NULL CHECK (length(draft_fingerprint) = 64),
    cards_draft_hash TEXT NOT NULL CHECK (length(cards_draft_hash) = 64),
    envelope_hash TEXT NOT NULL CHECK (length(envelope_hash) = 64),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_import_attempt_receipt (
    id TEXT PRIMARY KEY,
    source_import_id TEXT NOT NULL
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('success', 'failed', 'cancelled', 'interrupted')
    ),
    stage TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('tomato', 'local_file', 'feedback_experience')),
    request_hash TEXT CHECK (request_hash IS NULL OR length(request_hash) = 64),
    execution_receipt_id TEXT
        REFERENCES source_import_execution_receipt(id) ON DELETE RESTRICT,
    execution_receipt_hash TEXT CHECK (
        execution_receipt_hash IS NULL OR length(execution_receipt_hash) = 64
    ),
    failure_signature TEXT CHECK (
        failure_signature IS NULL OR length(failure_signature) = 64
    ),
    cleanup_status TEXT NOT NULL CHECK (
        cleanup_status IN ('clean', 'partial', 'not_required')
    ),
    cleanup_hash TEXT NOT NULL CHECK (length(cleanup_hash) = 64),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_import_id, attempt_ordinal),
    CHECK (
        (status = 'success'
            AND execution_receipt_id IS NOT NULL
            AND execution_receipt_hash IS NOT NULL
            AND failure_signature IS NULL)
        OR
        (status != 'success'
            AND execution_receipt_id IS NULL
            AND execution_receipt_hash IS NULL
            AND failure_signature IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_candidate (
    id TEXT PRIMARY KEY,
    source_import_id TEXT NOT NULL REFERENCES source_import_job(id) ON DELETE RESTRICT,
    source_card_index INTEGER NOT NULL CHECK (source_card_index >= 0),
    source_draft_hash TEXT NOT NULL CHECK (length(source_draft_hash) = 64),
    lineage_status TEXT NOT NULL DEFAULT 'legacy_unverified' CHECK (
        lineage_status IN ('verified', 'legacy_unverified')
    ),
    origin_json TEXT,
    origin_hash TEXT CHECK (origin_hash IS NULL OR length(origin_hash) = 64),
    runtime_distillation_invocation_id TEXT
        REFERENCES distillation_invocation(id) ON DELETE RESTRICT CHECK (
            runtime_distillation_invocation_id IS NULL
            OR (
                length(runtime_distillation_invocation_id)
                    = length('distillation-invocation-') + 32
                AND substr(
                    runtime_distillation_invocation_id,
                    1,
                    length('distillation-invocation-')
                ) = 'distillation-invocation-'
                AND substr(
                    runtime_distillation_invocation_id,
                    length('distillation-invocation-') + 1
                ) NOT GLOB '*[^0-9a-f]*'
            )
        ),
    card_id TEXT NOT NULL,
    card_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'promoted', 'deferred', 'rejected')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    payload_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    promoted_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_import_id, source_card_index, source_draft_hash),
    CHECK ((lineage_status = 'verified') = (origin_json IS NOT NULL)),
    CHECK ((origin_json IS NULL) = (origin_hash IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_card (
    card_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    active_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_card_version (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES knowledge_card(card_id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    payload_json TEXT NOT NULL,
    source_candidate_id TEXT NOT NULL REFERENCES knowledge_candidate(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (card_id, version_number)
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_version_lineage_receipt (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE
        REFERENCES knowledge_card_version(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL
        REFERENCES knowledge_candidate(id) ON DELETE RESTRICT,
    source_import_id TEXT NOT NULL
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    source_receipt_id TEXT NOT NULL
        REFERENCES source_import_execution_receipt(id) ON DELETE RESTRICT,
    source_receipt_hash TEXT NOT NULL CHECK (length(source_receipt_hash) = 64),
    candidate_origin_hash TEXT NOT NULL CHECK (length(candidate_origin_hash) = 64),
    approved_candidate_revision INTEGER NOT NULL CHECK (
        approved_candidate_revision >= 1
    ),
    approved_candidate_content_hash TEXT NOT NULL CHECK (
        length(approved_candidate_content_hash) = 64
    ),
    activation_mode TEXT NOT NULL CHECK (activation_mode IN ('initial', 'replace')),
    replacement_authorized INTEGER NOT NULL CHECK (replacement_authorized IN (0, 1)),
    activation_expected_version_id TEXT NOT NULL,
    activation_expected_state_revision INTEGER NOT NULL CHECK (
        activation_expected_state_revision >= 1
    ),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scope_id TEXT REFERENCES knowledge_scope(id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_formalization_receipt (
    id TEXT PRIMARY KEY,
    source_import_id TEXT NOT NULL UNIQUE
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    source_receipt_id TEXT NOT NULL UNIQUE
        REFERENCES source_import_execution_receipt(id) ON DELETE RESTRICT,
    source_receipt_hash TEXT NOT NULL CHECK (length(source_receipt_hash) = 64),
    cards_draft_hash TEXT NOT NULL CHECK (length(cards_draft_hash) = 64),
    candidate_set_hash TEXT NOT NULL CHECK (length(candidate_set_hash) = 64),
    knowledge_state_hash TEXT NOT NULL CHECK (length(knowledge_state_hash) = 64),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scope_id TEXT REFERENCES knowledge_scope(id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_scope (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('application', 'project')),
    project_binding_id TEXT UNIQUE
        REFERENCES project_binding(id) ON DELETE RESTRICT,
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope_type = 'application' AND project_binding_id IS NULL)
        OR (scope_type = 'project' AND project_binding_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_source_scope (
    source_import_id TEXT PRIMARY KEY
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    scope_id TEXT NOT NULL REFERENCES knowledge_scope(id) ON DELETE RESTRICT,
    bound_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_scope_card (
    scope_id TEXT NOT NULL REFERENCES knowledge_scope(id) ON DELETE RESTRICT,
    card_id TEXT NOT NULL REFERENCES knowledge_card(card_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    active_version_id TEXT NOT NULL
        REFERENCES knowledge_card_version(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope_id, card_id)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_scope_application_singleton
    ON knowledge_scope(scope_type) WHERE scope_type = 'application';
CREATE INDEX IF NOT EXISTS idx_knowledge_source_scope_scope
    ON knowledge_source_scope(scope_id, source_import_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_scope_card_status
    ON knowledge_scope_card(scope_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS skill_handoff_run (
    id TEXT PRIMARY KEY CHECK (
        length(id) = length('skill-handoff-') + 32
        AND substr(id, 1, length('skill-handoff-')) = 'skill-handoff-'
        AND substr(id, length('skill-handoff-') + 1) NOT GLOB '*[^0-9a-f]*'
    ),
    handoff_key TEXT NOT NULL CHECK (handoff_key = 'novel-distillation-to-kb-manager'),
    project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'approved', 'rejected', 'disabled', 'running', 'succeeded', 'failed')
    ),
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    source_import_id TEXT NOT NULL REFERENCES source_import_job(id) ON DELETE RESTRICT,
    formalization_receipt_id TEXT NOT NULL REFERENCES knowledge_formalization_receipt(id) ON DELETE RESTRICT,
    formalization_receipt_hash TEXT NOT NULL CHECK (length(formalization_receipt_hash) = 64),
    knowledge_fingerprint TEXT NOT NULL CHECK (length(knowledge_fingerprint) = 64),
    upstream_cards_hash TEXT NOT NULL CHECK (length(upstream_cards_hash) = 64),
    workflow_plan_id TEXT NOT NULL CHECK (length(workflow_plan_id) = length('workflow-plan-') + 32),
    workflow_plan_hash TEXT NOT NULL CHECK (length(workflow_plan_hash) = 64),
    checkpoint_id TEXT NOT NULL CHECK (length(checkpoint_id) = length('workflow-checkpoint-') + 32),
    checkpoint_hash TEXT NOT NULL CHECK (length(checkpoint_hash) = 64),
    skill_version_id TEXT NOT NULL REFERENCES skill_version(id) ON DELETE RESTRICT,
    skill_receipt_id TEXT,
    skill_receipt_hash TEXT CHECK (skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64),
    result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
    artifact_hash TEXT CHECK (artifact_hash IS NULL OR length(artifact_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_binding_id, handoff_key, source_import_id, formalization_receipt_hash)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_skill_handoff_project_status
    ON skill_handoff_run(project_binding_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_handoff_source
    ON skill_handoff_run(source_import_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS context_handoff_run (
    id TEXT PRIMARY KEY CHECK (
        length(id) = length('context-handoff-') + 32
        AND substr(id, 1, length('context-handoff-')) = 'context-handoff-'
        AND substr(id, length('context-handoff-') + 1) NOT GLOB '*[^0-9a-f]*'
    ),
    handoff_key TEXT NOT NULL CHECK (handoff_key = 'novel-kb-import-index-to-assemble-context'),
    project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'approved', 'rejected', 'disabled', 'running', 'succeeded', 'failed')
    ),
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
    source_handoff_id TEXT NOT NULL REFERENCES skill_handoff_run(id) ON DELETE RESTRICT,
    distillation_index_hash TEXT NOT NULL CHECK (length(distillation_index_hash) = 64),
    workflow_plan_id TEXT NOT NULL CHECK (length(workflow_plan_id) = length('workflow-plan-') + 32),
    workflow_plan_hash TEXT NOT NULL CHECK (length(workflow_plan_hash) = 64),
    checkpoint_id TEXT NOT NULL CHECK (length(checkpoint_id) = length('workflow-checkpoint-') + 32),
    checkpoint_hash TEXT NOT NULL CHECK (length(checkpoint_hash) = 64),
    skill_version_id TEXT NOT NULL REFERENCES skill_version(id) ON DELETE RESTRICT,
    skill_receipt_id TEXT,
    skill_receipt_hash TEXT CHECK (skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64),
    result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
    artifact_hash TEXT CHECK (artifact_hash IS NULL OR length(artifact_hash) = 64),
    context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
    context_pack_hash TEXT CHECK (context_pack_hash IS NULL OR length(context_pack_hash) = 64),
    route_receipt_id TEXT REFERENCES nlcl_route_receipt(id) ON DELETE RESTRICT,
    route_receipt_hash TEXT CHECK (route_receipt_hash IS NULL OR length(route_receipt_hash) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_binding_id, handoff_key, source_handoff_id, distillation_index_hash),
    CHECK ((context_pack_id IS NULL) = (context_pack_hash IS NULL)),
    CHECK ((route_receipt_id IS NULL) = (route_receipt_hash IS NULL))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_context_handoff_project_status
    ON context_handoff_run(project_binding_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_handoff_source
    ON context_handoff_run(source_handoff_id, updated_at DESC);


CREATE TRIGGER IF NOT EXISTS trg_knowledge_source_scope_immutable
BEFORE UPDATE ON knowledge_source_scope
BEGIN
    SELECT RAISE(ABORT, 'knowledge source scope binding is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_knowledge_source_scope_delete_immutable
BEFORE DELETE ON knowledge_source_scope
BEGIN
    SELECT RAISE(ABORT, 'knowledge source scope binding is immutable');
END;

CREATE INDEX IF NOT EXISTS idx_run_project_status
    ON run(project_binding_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_session_project_updated
    ON execution_session(project_binding_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_event_run_sequence
    ON run_event(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_activity_run_sequence
    ON activity(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_session_message_session_ordinal
    ON session_message(session_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_artifact_run_type
    ON artifact(run_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_approval_run_status
    ON approval(run_id, status);
CREATE INDEX IF NOT EXISTS idx_tool_call_run_status
    ON tool_call(run_id, status);
CREATE INDEX IF NOT EXISTS idx_checkpoint_run_sequence
    ON checkpoint(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_agent_candidate_project_created
    ON agent_candidate_output(project_binding_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_version_skill_status
    ON skill_version(skill_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_import_scope_skill
    ON skill_import_receipt(scope_key, skill_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_activation_project
    ON skill_activation(project_binding_id, skill_id);
CREATE INDEX IF NOT EXISTS idx_skill_activation_receipt_scope
    ON skill_activation_receipt(scope_key, skill_id, state_revision DESC);
CREATE INDEX IF NOT EXISTS idx_change_set_project_status
    ON change_set(project_binding_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_set_provenance_run
    ON change_set_provenance(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_source_import_status
    ON source_import_job(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_import_receipt_created
    ON source_import_execution_receipt(created_at DESC, source_import_id);
CREATE INDEX IF NOT EXISTS idx_source_import_attempt_created
    ON source_import_attempt_receipt(source_import_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_candidate_source_status
    ON knowledge_candidate(source_import_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_card_status
    ON knowledge_card(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_version_card_number
    ON knowledge_card_version(card_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_version_card_content
    ON knowledge_card_version(card_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_knowledge_lineage_source
    ON knowledge_version_lineage_receipt(source_import_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_formalization_created
    ON knowledge_formalization_receipt(created_at DESC, source_import_id);
CREATE INDEX IF NOT EXISTS idx_nlcl_route_project_status
    ON nlcl_route_receipt(project_binding_id, route_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prewrite_package_project_readiness
    ON prewrite_operation_package(project_binding_id, readiness, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_nlcl_route_execution_run
    ON nlcl_route_execution_receipt(run_id, created_at DESC);

-- 打开的文件夹以外的写入（没打开文件夹时的写入也算）：写文件工具在这里一文件一行留痕，
-- 写之前的原内容备份在状态目录 file-history/，可以撤回（file_journal.py）。
CREATE TABLE IF NOT EXISTS file_change (
    change_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    title TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update', 'delete', 'move', 'mkdir')),
    path TEXT NOT NULL,
    target_path TEXT,
    before_sha256 TEXT,
    after_sha256 TEXT,
    backup_path TEXT,
    created_dirs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    undone_at TEXT,
    apply_phase TEXT NOT NULL DEFAULT 'legacy' CHECK (apply_phase IN ('legacy', 'pending', 'applied', 'failed')),
    PRIMARY KEY (change_id, seq)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_file_change_session
    ON file_change(session_id, created_at DESC);

-- 一轮答完以后，这一轮发给模型的那一段（作者的话后面：附言、工具调用和结果、补话，最后是回答），照模型当时看到的样子存一份。
-- 下一轮拼历史时原样放回（ZCode 档案 12.2），模型看得见上一轮自己读过什么、改过什么，下一轮开头也和上一轮最后一步一字不差。
CREATE TABLE IF NOT EXISTS run_transcript (
    run_id TEXT PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    messages_json TEXT NOT NULL,
    estimated_tokens INTEGER NOT NULL CHECK (estimated_tokens >= 0),
    through_message_ordinal INTEGER,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_run_transcript_session
    ON run_transcript(session_id);

CREATE TABLE IF NOT EXISTS session_goal (
    goal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','blocked','completed','ended')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    run_id TEXT NOT NULL UNIQUE REFERENCES run(id),
    stage_count INTEGER NOT NULL DEFAULT 0,
    progress TEXT NOT NULL,
    evidence TEXT NOT NULL,
    pause_reason TEXT,
    requested_status TEXT CHECK(requested_status IN ('completed','blocked')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status_source TEXT NOT NULL CHECK(status_source IN ('agent','user','runtime'))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_session_goal_session ON session_goal(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_goal_unfinished ON session_goal(session_id)
    WHERE status IN ('active','paused','blocked');
CREATE TABLE IF NOT EXISTS goal_event (
    goal_id TEXT NOT NULL REFERENCES session_goal(goal_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id,revision)
) STRICT;

CREATE TABLE IF NOT EXISTS session_queue (
    session_id TEXT PRIMARY KEY REFERENCES execution_session(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL
);

-- 用户把排队消息插入当前运行：接收和消费分别留痕，重启及重复请求不会丢失或重发。
CREATE TABLE IF NOT EXISTS session_steering (
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'consumed', 'released')),
    message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(session_id, item_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_session_steering_pending ON session_steering(run_id, status, created_at);

-- An unsent identity can be withdrawn atomically before a delayed POST arrives.
CREATE TABLE IF NOT EXISTS session_admission_withdrawal (
    session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, idempotency_key)
) STRICT;

CREATE TABLE IF NOT EXISTS command_completion_scope (
    session_id TEXT PRIMARY KEY REFERENCES execution_session(id) ON DELETE CASCADE,
    stop_generation INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE IF NOT EXISTS command_completion (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    command_session_id TEXT NOT NULL UNIQUE,
    owner_session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
    owner_run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('exited','cancelled','interrupted')),
    exit_code INTEGER,
    ended_at REAL,
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('pending','delivered','suppressed')),
    delivered_run_id TEXT REFERENCES run(id) ON DELETE SET NULL,
    stop_generation INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_command_completion_pending ON command_completion(owner_session_id, delivery_state, sequence);

CREATE TABLE IF NOT EXISTS agent_image (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES execution_session(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_agent_image_session ON agent_image(session_id);

COMMIT;
"""


DISTILLATION_INVOCATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS distillation_invocation (
    id TEXT PRIMARY KEY CHECK (
        length(id) = length('distillation-invocation-') + 32
        AND substr(id, 1, length('distillation-invocation-'))
            = 'distillation-invocation-'
        AND substr(id, length('distillation-invocation-') + 1)
            NOT GLOB '*[^0-9a-f]*'
    ),
    source_import_id TEXT NOT NULL
        REFERENCES source_import_job(id) ON DELETE RESTRICT,
    source_receipt_id TEXT NOT NULL
        REFERENCES source_import_execution_receipt(id) ON DELETE RESTRICT,
    source_receipt_hash TEXT NOT NULL CHECK (
        length(source_receipt_hash) = 64
        AND source_receipt_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_packet_fingerprint TEXT NOT NULL CHECK (
        length(source_packet_fingerprint) = 64
        AND source_packet_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    packet_text_hash TEXT NOT NULL CHECK (
        length(packet_text_hash) = 64
        AND packet_text_hash NOT GLOB '*[^0-9a-f]*'
    ),
    project_binding_id TEXT NOT NULL
        REFERENCES project_binding(id) ON DELETE RESTRICT,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    project_fingerprint TEXT NOT NULL CHECK (
        length(project_fingerprint) = 64
        AND project_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    knowledge_scope_id TEXT NOT NULL
        REFERENCES knowledge_scope(id) ON DELETE RESTRICT,
    knowledge_scope_state_revision INTEGER NOT NULL CHECK (
        knowledge_scope_state_revision >= 1
    ),
    knowledge_fingerprint TEXT NOT NULL CHECK (
        length(knowledge_fingerprint) = 64
        AND knowledge_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    project_kb_state_id TEXT CHECK (
        project_kb_state_id IS NULL
        OR (
            length(project_kb_state_id) = length('project-kb-state-') + 32
            AND substr(project_kb_state_id, 1, length('project-kb-state-'))
                = 'project-kb-state-'
            AND substr(project_kb_state_id, length('project-kb-state-') + 1)
                NOT GLOB '*[^0-9a-f]*'
        )
    ),
    project_kb_manifest_hash TEXT CHECK (
        project_kb_manifest_hash IS NULL
        OR (
            length(project_kb_manifest_hash) = 64
            AND project_kb_manifest_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    skill_id TEXT NOT NULL CHECK (
        skill_id = 'novel-distillation.deep-instruction'
    ),
    skill_version_id TEXT NOT NULL
        REFERENCES skill_version(id) ON DELETE RESTRICT,
    skill_version TEXT NOT NULL CHECK (length(skill_version) BETWEEN 1 AND 128),
    skill_manifest_fingerprint TEXT NOT NULL CHECK (
        length(skill_manifest_fingerprint) = 64
        AND skill_manifest_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    skill_package_fingerprint TEXT NOT NULL CHECK (
        length(skill_package_fingerprint) = 64
        AND skill_package_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    activation_receipt_id TEXT NOT NULL
        REFERENCES skill_activation_receipt(id) ON DELETE RESTRICT,
    activation_receipt_hash TEXT NOT NULL CHECK (
        length(activation_receipt_hash) = 64
        AND activation_receipt_hash NOT GLOB '*[^0-9a-f]*'
    ),
    activation_state_revision INTEGER NOT NULL CHECK (
        activation_state_revision >= 1
    ),
    effective_scope TEXT NOT NULL CHECK (
        effective_scope IN ('application', 'project')
    ),
    instruction_materialization_id TEXT NOT NULL CHECK (
        length(instruction_materialization_id)
            = length('distillation-instruction-materialization-') + 32
        AND substr(
            instruction_materialization_id,
            1,
            length('distillation-instruction-materialization-')
        ) = 'distillation-instruction-materialization-'
        AND substr(
            instruction_materialization_id,
            length('distillation-instruction-materialization-') + 1
        ) NOT GLOB '*[^0-9a-f]*'
    ),
    instruction_materialization_hash TEXT NOT NULL CHECK (
        length(instruction_materialization_hash) = 64
        AND instruction_materialization_hash NOT GLOB '*[^0-9a-f]*'
    ),
    managed_instruction_hash TEXT NOT NULL CHECK (
        length(managed_instruction_hash) = 64
        AND managed_instruction_hash NOT GLOB '*[^0-9a-f]*'
    ),
    provider_executor_json TEXT NOT NULL CHECK (json_valid(provider_executor_json)),
    provider_capabilities_json TEXT NOT NULL CHECK (
        json_valid(provider_capabilities_json)
    ),
    requested_model TEXT CHECK (
        requested_model IS NULL OR length(requested_model) BETWEEN 1 AND 512
    ),
    accepted_reported_models_json TEXT NOT NULL CHECK (
        json_valid(accepted_reported_models_json)
    ),
    dispatch_config_fingerprint TEXT NOT NULL CHECK (
        length(dispatch_config_fingerprint) = 64
        AND dispatch_config_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    credential_reference TEXT CHECK (
        credential_reference IS NULL
        OR length(credential_reference) BETWEEN 1 AND 256
    ),
    tool_definitions_hash TEXT NOT NULL CHECK (
        length(tool_definitions_hash) = 64
        AND tool_definitions_hash NOT GLOB '*[^0-9a-f]*'
    ),
    anchor_catalog_id TEXT NOT NULL CHECK (
        length(anchor_catalog_id) = length('distillation-anchor-catalog-') + 32
        AND substr(anchor_catalog_id, 1, length('distillation-anchor-catalog-'))
            = 'distillation-anchor-catalog-'
        AND substr(anchor_catalog_id, length('distillation-anchor-catalog-') + 1)
            NOT GLOB '*[^0-9a-f]*'
    ),
    anchor_catalog_hash TEXT NOT NULL CHECK (
        length(anchor_catalog_hash) = 64
        AND anchor_catalog_hash NOT GLOB '*[^0-9a-f]*'
    ),
    retry_of_invocation_id TEXT
        REFERENCES distillation_invocation(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        length(idempotency_key) = length('distillation-idempotency-') + 32
        AND substr(idempotency_key, 1, length('distillation-idempotency-'))
            = 'distillation-idempotency-'
        AND substr(idempotency_key, length('distillation-idempotency-') + 1)
            NOT GLOB '*[^0-9a-f]*'
    ),
    request_hash TEXT NOT NULL CHECK (
        length(request_hash) = 64
        AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'prepared', 'running', 'output_persisted', 'interrupted',
            'succeeded', 'failed', 'cancelled', 'abandoned'
        )
    ),
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    provider_replay_count INTEGER NOT NULL DEFAULT 0 CHECK (
        provider_replay_count = 0
    ),
    last_receipt_ordinal INTEGER NOT NULL CHECK (last_receipt_ordinal >= 1),
    last_receipt_id TEXT NOT NULL
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    last_receipt_hash TEXT NOT NULL CHECK (
        length(last_receipt_hash) = 64
        AND last_receipt_hash NOT GLOB '*[^0-9a-f]*'
    ),
    last_dispatch_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    last_dispatch_receipt_hash TEXT CHECK (
        last_dispatch_receipt_hash IS NULL
        OR (
            length(last_dispatch_receipt_hash) = 64
            AND last_dispatch_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    last_dispatch_turn_ordinal INTEGER CHECK (
        last_dispatch_turn_ordinal IS NULL
        OR last_dispatch_turn_ordinal BETWEEN 1 AND 32
    ),
    last_turn_result_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    last_turn_result_receipt_hash TEXT CHECK (
        last_turn_result_receipt_hash IS NULL
        OR (
            length(last_turn_result_receipt_hash) = 64
            AND last_turn_result_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    provider_turn_count INTEGER NOT NULL DEFAULT 0 CHECK (
        provider_turn_count BETWEEN 0 AND 32
    ),
    provider_turn_chain_hash TEXT CHECK (
        provider_turn_chain_hash IS NULL
        OR (
            length(provider_turn_chain_hash) = 64
            AND provider_turn_chain_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    candidate_output_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    candidate_output_receipt_hash TEXT CHECK (
        candidate_output_receipt_hash IS NULL
        OR (
            length(candidate_output_receipt_hash) = 64
            AND candidate_output_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    recovery_dossier_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    recovery_dossier_revision INTEGER CHECK (
        recovery_dossier_revision IS NULL OR recovery_dossier_revision >= 1
    ),
    recovery_dossier_receipt_hash TEXT CHECK (
        recovery_dossier_receipt_hash IS NULL
        OR (
            length(recovery_dossier_receipt_hash) = 64
            AND recovery_dossier_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    recovery_decision_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    recovery_decision_receipt_hash TEXT CHECK (
        recovery_decision_receipt_hash IS NULL
        OR (
            length(recovery_decision_receipt_hash) = 64
            AND recovery_decision_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    final_result_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    final_result_receipt_hash TEXT CHECK (
        final_result_receipt_hash IS NULL
        OR (
            length(final_result_receipt_hash) = 64
            AND final_result_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    candidate_set_hash TEXT CHECK (
        candidate_set_hash IS NULL
        OR (
            length(candidate_set_hash) = 64
            AND candidate_set_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    work_count INTEGER CHECK (work_count IS NULL OR work_count BETWEEN 0 AND 1),
    voice_count INTEGER CHECK (voice_count IS NULL OR voice_count BETWEEN 0 AND 1),
    validator_count INTEGER CHECK (
        validator_count IS NULL OR validator_count BETWEEN 0 AND 1
    ),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    head_hash TEXT NOT NULL CHECK (
        length(head_hash) = 64 AND head_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (retry_of_invocation_id IS NULL OR retry_of_invocation_id != id),
    CHECK ((project_kb_state_id IS NULL) = (project_kb_manifest_hash IS NULL)),
    CHECK (state_revision = last_receipt_ordinal),
    CHECK (
        (last_dispatch_receipt_id IS NULL)
            = (last_dispatch_receipt_hash IS NULL)
        AND (last_dispatch_receipt_id IS NULL)
            = (last_dispatch_turn_ordinal IS NULL)
    ),
    CHECK (
        (provider_turn_count = 0
            AND last_turn_result_receipt_id IS NULL
            AND last_turn_result_receipt_hash IS NULL
            AND provider_turn_chain_hash IS NULL)
        OR
        (provider_turn_count BETWEEN 1 AND 32
            AND last_turn_result_receipt_id IS NOT NULL
            AND last_turn_result_receipt_hash IS NOT NULL
            AND provider_turn_chain_hash IS NOT NULL)
    ),
    CHECK (
        last_dispatch_turn_ordinal IS NULL
        OR last_dispatch_turn_ordinal IN (
            provider_turn_count, provider_turn_count + 1
        )
    ),
    CHECK (
        (candidate_output_receipt_id IS NULL)
            = (candidate_output_receipt_hash IS NULL)
    ),
    CHECK (
        (recovery_dossier_receipt_id IS NULL)
            = (recovery_dossier_revision IS NULL)
        AND (recovery_dossier_receipt_id IS NULL)
            = (recovery_dossier_receipt_hash IS NULL)
    ),
    CHECK (
        (recovery_decision_receipt_id IS NULL)
            = (recovery_decision_receipt_hash IS NULL)
    ),
    CHECK (
        (final_result_receipt_id IS NULL)
            = (final_result_receipt_hash IS NULL)
    ),
    CHECK (
        (candidate_set_hash IS NULL) = (work_count IS NULL)
        AND (candidate_set_hash IS NULL) = (voice_count IS NULL)
        AND (candidate_set_hash IS NULL) = (validator_count IS NULL)
    ),
    CHECK (
        (status = 'prepared'
            AND last_dispatch_receipt_id IS NULL
            AND last_turn_result_receipt_id IS NULL
            AND candidate_output_receipt_id IS NULL
            AND recovery_dossier_receipt_id IS NULL
            AND recovery_decision_receipt_id IS NULL
            AND final_result_receipt_id IS NULL)
        OR
        (status = 'running'
            AND last_dispatch_receipt_id IS NOT NULL
            AND candidate_output_receipt_id IS NULL
            AND recovery_dossier_receipt_id IS NULL
            AND recovery_decision_receipt_id IS NULL
            AND final_result_receipt_id IS NULL)
        OR
        (status = 'output_persisted'
            AND last_dispatch_receipt_id IS NOT NULL
            AND last_turn_result_receipt_id IS NOT NULL
            AND candidate_output_receipt_id IS NOT NULL
            AND recovery_dossier_receipt_id IS NULL
            AND recovery_decision_receipt_id IS NULL
            AND final_result_receipt_id IS NULL)
        OR
        (status = 'interrupted'
            AND last_dispatch_receipt_id IS NOT NULL
            AND recovery_dossier_receipt_id IS NOT NULL
            AND final_result_receipt_id IS NULL)
        OR
        (status = 'succeeded'
            AND last_dispatch_receipt_id IS NOT NULL
            AND last_turn_result_receipt_id IS NOT NULL
            AND candidate_output_receipt_id IS NOT NULL
            AND final_result_receipt_id IS NOT NULL
            AND candidate_set_hash IS NOT NULL
            AND work_count = 1
            AND voice_count = 1
            AND validator_count = 1)
        OR
        (status IN ('failed', 'cancelled')
            AND final_result_receipt_id IS NOT NULL
            AND candidate_output_receipt_id IS NULL
            AND candidate_set_hash IS NULL)
        OR
        (status = 'abandoned'
            AND last_dispatch_receipt_id IS NOT NULL
            AND recovery_dossier_receipt_id IS NOT NULL
            AND recovery_decision_receipt_id IS NOT NULL
            AND final_result_receipt_id IS NOT NULL
            AND candidate_set_hash IS NULL)
    )
) STRICT
"""


DISTILLATION_INVOCATION_RECEIPT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS distillation_invocation_receipt (
    id TEXT PRIMARY KEY CHECK (
        length(id) = length('distillation-receipt-') + 32
        AND substr(id, 1, length('distillation-receipt-'))
            = 'distillation-receipt-'
        AND substr(id, length('distillation-receipt-') + 1)
            NOT GLOB '*[^0-9a-f]*'
    ),
    invocation_id TEXT NOT NULL
        REFERENCES distillation_invocation(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    kind TEXT NOT NULL CHECK (
        kind IN (
            'prepared', 'provider_dispatch', 'provider_turn_result',
            'candidate_output', 'recovery_dossier', 'recovery_decision',
            'final_result'
        )
    ),
    turn_ordinal INTEGER CHECK (
        turn_ordinal IS NULL OR turn_ordinal BETWEEN 1 AND 32
    ),
    parent_receipt_id TEXT
        REFERENCES distillation_invocation_receipt(id) ON DELETE RESTRICT,
    expected_invocation_revision INTEGER NOT NULL CHECK (
        expected_invocation_revision >= 0
    ),
    resulting_invocation_revision INTEGER NOT NULL CHECK (
        resulting_invocation_revision >= 1
    ),
    resulting_status TEXT NOT NULL CHECK (
        resulting_status IN (
            'prepared', 'running', 'output_persisted', 'interrupted',
            'succeeded', 'failed', 'cancelled', 'abandoned'
        )
    ),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        length(idempotency_key)
            = length('distillation-receipt-idempotency-') + 32
        AND substr(
            idempotency_key,
            1,
            length('distillation-receipt-idempotency-')
        ) = 'distillation-receipt-idempotency-'
        AND substr(
            idempotency_key,
            length('distillation-receipt-idempotency-') + 1
        ) NOT GLOB '*[^0-9a-f]*'
    ),
    payload_hash TEXT NOT NULL CHECK (
        length(payload_hash) = 64
        AND payload_hash NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_hash TEXT NOT NULL UNIQUE CHECK (
        length(receipt_hash) = 64
        AND receipt_hash NOT GLOB '*[^0-9a-f]*'
    ),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    UNIQUE (invocation_id, ordinal),
    CHECK (resulting_invocation_revision = expected_invocation_revision + 1),
    CHECK (ordinal = resulting_invocation_revision),
    CHECK (
        (kind IN ('provider_dispatch', 'provider_turn_result')
            AND turn_ordinal IS NOT NULL)
        OR
        (kind NOT IN ('provider_dispatch', 'provider_turn_result')
            AND turn_ordinal IS NULL)
    ),
    CHECK (
        (kind = 'prepared'
            AND ordinal = 1
            AND expected_invocation_revision = 0
            AND parent_receipt_id IS NULL
            AND resulting_status = 'prepared')
        OR
        (kind != 'prepared'
            AND ordinal >= 2
            AND parent_receipt_id IS NOT NULL)
    ),
    CHECK (
        (kind IN ('provider_dispatch', 'provider_turn_result')
            AND resulting_status = 'running')
        OR (kind = 'candidate_output' AND resulting_status = 'output_persisted')
        OR (kind IN ('recovery_dossier', 'recovery_decision')
            AND resulting_status = 'interrupted')
        OR (kind = 'final_result'
            AND resulting_status IN ('succeeded', 'failed', 'cancelled', 'abandoned'))
        OR (kind = 'prepared' AND resulting_status = 'prepared')
    )
) STRICT
"""


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    ready: bool
    journal_mode: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class ProjectBindingRecord:
    project_id: str
    root_path: str
    display_name: str
    fingerprint: str
    revision: int
    created_at: str
    updated_at: str


def latest_own_write_pin(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[int, str] | None:
    """这场对话最近一次自己落盘（或执行命令）后的版本钉子。

    落盘必然让 run.project_revision（本回合资料包的基准）落后。Agent 写完要
    读回来核对、要在同一场对话里接着改或接着提案，基准只能取这里，不能拿
    资料包钉子卡它。放在 database 层是因为 agent_tools 与 change_set_service
    两边都要用，而前者 import 后者，反向会成环。
    """
    # 9-26 起写文件工具改了中性名字（write_file…），旧对话里记的是旧名字（project_write_file…），两套都认。
    watched = (
        "computer_run_command",
        "command_session",
        "write_file",
        "edit_file",
        "delete_file",
        "move_file",
        "make_folder",
        "undo_change",
        "project_edit_file",
        "project_write_file",
        "project_delete_file",
        "project_move_file",
        "project_mkdir",
        "project_undo_change",
    )
    placeholders = ", ".join("?" for _ in watched)
    rows = connection.execute(
        f"""
        SELECT tool_name, output_json FROM tool_call
        WHERE run_id = ? AND (
          (status = 'succeeded' AND tool_name IN ({placeholders}))
          OR (status IN ('succeeded', 'failed', 'cancelled') AND tool_name GLOB 'mcp__*')
        )
        ORDER BY rowid DESC
        LIMIT 200
        """,
        (run_id, *watched),
    ).fetchall()
    # 往前找最近一次带版本号的：写到打开的文件夹以外、「内容本来就这样」这类结果没有版本号，
    # 以前只看最后一条，最后一条恰好是它们时钉子就丢了，后面再改书里的文件会被当成「书被别人改过」拒掉。
    pin: tuple[int, str] | None = None
    for row in rows:
        if not row["output_json"]:
            continue
        try:
            output = json.loads(str(row["output_json"]))
        except ValueError:
            continue
        if str(row["tool_name"]).startswith("mcp__") and (
            not isinstance(output, dict) or output.get("source_mcp") is not True or output.get("read_only") is not False
        ):
            continue
        revision = output.get("result_revision") if isinstance(output, dict) else None
        fingerprint = output.get("result_fingerprint") if isinstance(output, dict) else None
        if isinstance(revision, int) and not isinstance(revision, bool) and isinstance(fingerprint, str):
            if pin is None or revision > pin[0]:
                pin = revision, fingerprint
    # A background command can finish between tools without a new tool_call.
    # Its consumer persists a fresh project observation in the same transaction
    # as delivery/transcript. Project binding revisions are monotonic per
    # project; use that fact instead of racing timestamps from different paths.
    observations = connection.execute(
        "SELECT metadata_json FROM artifact WHERE run_id=? AND artifact_type='command_completion_observation' ORDER BY rowid DESC",
        (run_id,),
    ).fetchall()
    for row in observations:
        try:
            observation = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError):
            continue
        revision = observation.get("result_revision") if isinstance(observation, dict) else None
        fingerprint = observation.get("result_fingerprint") if isinstance(observation, dict) else None
        if isinstance(revision, int) and not isinstance(revision, bool) and isinstance(fingerprint, str):
            if pin is None or revision > pin[0]:
                pin = revision, fingerprint
    return pin


class SqliteStore:
    """Small connection factory and schema bootstrap for runtime metadata."""

    @staticmethod
    def _proposal_base_matches(
        expected_revision: int,
        expected_fingerprint: str,
        base_revision: int,
        base_fingerprint: str,
        own_pin: tuple[int, str] | None,
    ) -> bool:
        """提案基准 = 资料包钉子，或本场 Run 自己落盘推进过的钉子。

        直接写落盘后资料包钉子必然落后一格；同一场对话「先存一份、再提一份」
        是合法工作流，第三道建档校验不能拿旧钉子把它拦死。own_pin 之外的
        任何基准一律照旧拒绝。
        """
        if base_revision == expected_revision and base_fingerprint == expected_fingerprint:
            return True
        return own_pin is not None and (base_revision, base_fingerprint) == own_pin

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize_lock = threading.Lock()

    def initialize(self) -> DatabaseHealth:
        with self._initialize_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as connection:
                current_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database schema {current_version} is newer than runtime schema {SCHEMA_VERSION}"
                    )
                journal_mode = str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                ).lower()
                connection.execute("PRAGMA synchronous = NORMAL")
                try:
                    connection.executescript(SCHEMA_SQL)
                    self._migrate_schema(connection)
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                health = self._health_from_connection(connection)

            if journal_mode != "wal":
                raise RuntimeError(
                    f"SQLite refused WAL mode for database {self.path}; got {journal_mode!r}"
                )
            if not health.ready:
                raise RuntimeError("SQLite schema initialization did not pass readiness checks")
            return health

    def sync_project_binding(
        self,
        *,
        project_id: str,
        root_path: str,
        display_name: str,
        fingerprint: str,
        observed_at: datetime | None = None,
    ) -> ProjectBindingRecord:
        """Persist a read-only snapshot identity and advance revision on change."""

        observed = observed_at or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed = observed.astimezone(UTC)
        now = datetime.now(UTC).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM project_binding WHERE id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO project_binding(
                            id, root_path, display_name, fingerprint, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            project_id,
                            root_path,
                            display_name,
                            fingerprint,
                            now,
                            now,
                        ),
                    )
                else:
                    if str(row["root_path"]) != root_path:
                        raise RuntimeError(
                            "project binding identity resolved to a different root path"
                        )
                    current_updated_at = datetime.fromisoformat(
                        str(row["updated_at"]).replace("Z", "+00:00")
                    ).astimezone(UTC)
                    if current_updated_at <= observed:
                        revision = int(row["revision"])
                        if str(row["fingerprint"]) != fingerprint:
                            revision += 1
                        connection.execute(
                            """
                            UPDATE project_binding
                            SET display_name = ?, fingerprint = ?, revision = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (display_name, fingerprint, revision, now, project_id),
                        )
                result = connection.execute(
                    "SELECT * FROM project_binding WHERE id = ?",
                    (project_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if result is None:  # pragma: no cover - guarded by the transaction above.
            raise RuntimeError("project binding transaction produced no record")
        return self._project_binding_from_row(result)

    def get_project_binding(self, project_id: str) -> ProjectBindingRecord | None:
        with self.connection() as connection:
            return self.get_project_binding_in_connection(connection, project_id)

    def get_project_binding_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> ProjectBindingRecord | None:
        """Read a binding without opening or completing the caller's transaction."""

        row = connection.execute(
            "SELECT * FROM project_binding WHERE id = ?",
            (project_id,),
        ).fetchone()
        return None if row is None else self._project_binding_from_row(row)

    def list_project_bindings(self) -> list[ProjectBindingRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM project_binding ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        return [self._project_binding_from_row(row) for row in rows]

    def update_project_binding_display_name(
        self,
        project_id: str,
        display_name: str,
    ) -> ProjectBindingRecord | None:
        now = datetime.now(UTC).isoformat()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE project_binding
                SET display_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (display_name.strip(), now, project_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_binding WHERE id = ?",
                (project_id,),
            ).fetchone()
        return None if row is None else self._project_binding_from_row(row)

    def delete_project_binding(self, project_id: str) -> bool:
        with self.connection() as connection:
            # 动态发现所有引用 project_binding 的表并先清子行：
            # 这些外键大多是 ON DELETE RESTRICT，直接删绑定会让作者"删除作品"
            # 变成 500 报错 + 一堆孤儿数据。子表之间还有嵌套外键，按多轮迭代
            # 删除直到清干净（每轮删掉能删的，被更深子行挡住的下一轮再删）。
            # 递归清理：先找到所有直接引用 project_binding 的表，再顺着外键图
            # 一层层把引用这些行的孙子行（run → session、artifact → run …）也删掉。
            # 以前只删一层，会话下面还挂着 run / 消息时删绑定就 500，作者连一条
            # 指向已归档文件夹的死绑定都摘不掉（2026-09-24 真机 13 条）。
            graph: dict[str, list[tuple[str, str, str]]] = {}
            for (table_name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                for fk in connection.execute(
                    f'PRAGMA foreign_key_list("{table_name}")'
                ).fetchall():
                    # (id, seq, referenced_table, from_column, to_column, ...)
                    parent, from_column, to_column = str(fk[2]), fk[3], fk[4]
                    if not from_column:
                        continue
                    graph.setdefault(parent, []).append(
                        (str(table_name), str(from_column), str(to_column or "id"))
                    )

            def purge(table: str, column: str, values: list[object], depth: int) -> None:
                if not values or depth > 16:
                    return
                for offset in range(0, len(values), 400):
                    chunk = values[offset : offset + 400]
                    marks = ",".join("?" for _ in chunk)
                    for child_table, child_column, parent_column in graph.get(table, []):
                        if child_table == table:
                            # 自引用（父子 run）在同一条 DELETE 里一起删。
                            continue
                        parent_values = [
                            row[0]
                            for row in connection.execute(
                                f'SELECT "{parent_column}" FROM "{table}" WHERE "{column}" IN ({marks})',
                                chunk,
                            ).fetchall()
                            if row[0] is not None
                        ]
                        purge(child_table, child_column, parent_values, depth + 1)
                    connection.execute(
                        f'DELETE FROM "{table}" WHERE "{column}" IN ({marks})',
                        chunk,
                    )

            for child_table, child_column, _parent_column in graph.get("project_binding", []):
                purge(child_table, child_column, [project_id], 1)
            cursor = connection.execute(
                "DELETE FROM project_binding WHERE id = ?",
                (project_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def create_change_set(self, payload: dict[str, object]) -> None:
        encoded = self._encode_payload(payload)
        with self.connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO change_set(
                        id, project_binding_id, status, base_revision,
                        base_fingerprint, review_revision, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["change_set_id"],
                        payload["project_id"],
                        payload["status"],
                        payload["base_revision"],
                        payload["base_fingerprint"],
                        payload["review_revision"],
                        encoded,
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("change set identity already exists") from exc

    def create_agent_change_set(
        self,
        payload: dict[str, object],
        provenance: dict[str, object],
    ) -> None:
        """Atomically persist an Agent proposal and its verified Runtime provenance."""

        required = {
            "change_set_id",
            "run_id",
            "tool_call_id",
            "context_pack_id",
            "project_id",
            "project_revision",
            "project_fingerprint",
            "request_hash",
            "created_at",
        }
        missing = sorted(required - provenance.keys())
        if missing:
            raise ValueError(
                "change set provenance is missing fields: " + ", ".join(missing)
            )

        change_set_id = str(provenance["change_set_id"])
        run_id = str(provenance["run_id"])
        tool_call_id = str(provenance["tool_call_id"])
        context_pack_id = str(provenance["context_pack_id"])
        project_id = str(provenance["project_id"])
        project_revision = provenance["project_revision"]
        project_fingerprint = str(provenance["project_fingerprint"])
        request_hash = str(provenance["request_hash"])
        created_at = str(provenance["created_at"])
        if not isinstance(project_revision, int) or isinstance(project_revision, bool):
            raise ValueError("change set provenance project_revision must be an integer")
        if project_revision < 1:
            raise ValueError("change set provenance project_revision must be positive")
        if len(project_fingerprint) != 64 or len(request_hash) != 64:
            raise ValueError("change set provenance hashes must be SHA-256 values")
        if payload.get("change_set_id") != change_set_id:
            raise ValueError("change set provenance identity does not match its payload")
        if payload.get("project_id") != project_id:
            raise ValueError("change set provenance project does not match its payload")
        if payload.get("base_revision") != project_revision:
            raise ValueError("change set provenance revision does not match its payload")
        if payload.get("base_fingerprint") != project_fingerprint:
            raise ValueError("change set provenance fingerprint does not match its payload")
        if payload.get("origin") != "agent" or payload.get("source_run_id") != run_id:
            raise ValueError("verified Agent change sets must identify their source Run")
        if payload.get("source_tool_call_id") != tool_call_id:
            raise ValueError(
                "verified Agent change sets must identify their source ToolCall"
            )
        if payload.get("source_request_hash") != request_hash:
            raise ValueError(
                "verified Agent change sets must identify their source request hash"
            )

        encoded = self._encode_payload(payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute(
                    "SELECT * FROM run WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise RuntimeError("change set provenance Run was not found")
                tool_call = connection.execute(
                    "SELECT * FROM tool_call WHERE id = ?",
                    (tool_call_id,),
                ).fetchone()
                if tool_call is None:
                    raise RuntimeError("change set provenance ToolCall was not found")
                context_pack = connection.execute(
                    "SELECT * FROM context_pack WHERE id = ?",
                    (context_pack_id,),
                ).fetchone()
                if context_pack is None:
                    raise RuntimeError("change set provenance Context Pack was not found")
                if (
                    str(tool_call["run_id"]) != run_id
                    or str(tool_call["request_hash"]) != request_hash
                    or str(tool_call["status"]) not in {"pending", "running", "succeeded"}
                ):
                    raise RuntimeError("change set provenance does not match its ToolCall")
                own_pin = latest_own_write_pin(connection, run_id)
                if (
                    str(run["project_binding_id"]) != project_id
                    or str(run["context_pack_id"] or "") != context_pack_id
                    or not self._proposal_base_matches(
                        int(run["project_revision"]),
                        str(run["project_fingerprint"]),
                        project_revision,
                        project_fingerprint,
                        own_pin,
                    )
                ):
                    raise RuntimeError("change set provenance does not match its Run")
                if (
                    str(context_pack["session_id"]) != str(run["session_id"])
                    or str(context_pack["project_binding_id"]) != project_id
                    or not self._proposal_base_matches(
                        int(context_pack["project_revision"]),
                        str(context_pack["project_fingerprint"]),
                        project_revision,
                        project_fingerprint,
                        own_pin,
                    )
                ):
                    raise RuntimeError("change set provenance does not match its Context Pack")

                connection.execute(
                    """
                    INSERT INTO change_set(
                        id, project_binding_id, status, base_revision,
                        base_fingerprint, review_revision, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["change_set_id"],
                        payload["project_id"],
                        payload["status"],
                        payload["base_revision"],
                        payload["base_fingerprint"],
                        payload["review_revision"],
                        encoded,
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO change_set_provenance(
                        change_set_id, run_id, tool_call_id, context_pack_id,
                        project_id, project_revision, project_fingerprint,
                        request_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        change_set_id,
                        run_id,
                        tool_call_id,
                        context_pack_id,
                        project_id,
                        project_revision,
                        project_fingerprint,
                        request_hash,
                        created_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RuntimeError(
                    "Agent change set identity or provenance already exists"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def save_change_set(self, payload: dict[str, object]) -> None:
        encoded = self._encode_payload(payload)
        with self.connection() as connection:
            result = connection.execute(
                """
                UPDATE change_set
                SET status = ?, review_revision = ?, payload_json = ?, updated_at = ?
                WHERE id = ? AND project_binding_id = ?
                """,
                (
                    payload["status"],
                    payload["review_revision"],
                    encoded,
                    payload["updated_at"],
                    payload["change_set_id"],
                    payload["project_id"],
                ),
            )
        if result.rowcount != 1:
            raise RuntimeError("change set was not found while saving")

    def get_change_set(self, change_set_id: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM change_set WHERE id = ?",
                (change_set_id,),
            ).fetchone()
        if row is None:
            return None
        return self._json_payload_row(row, label="change set")

    def get_change_set_provenance(
        self,
        change_set_id: str,
    ) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM change_set_provenance WHERE change_set_id = ?",
                (change_set_id,),
            ).fetchone()
        return None if row is None else self._change_set_provenance_from_row(row)

    def get_change_set_by_tool_call(
        self,
        tool_call_id: str,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT cs.payload_json, provenance.*
                FROM change_set_provenance AS provenance
                JOIN change_set AS cs ON cs.id = provenance.change_set_id
                WHERE provenance.tool_call_id = ?
                """,
                (tool_call_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            self._json_payload_row(row, label="change set"),
            self._change_set_provenance_from_row(row),
        )

    def list_change_sets(self, project_id: str) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM change_set
                WHERE project_binding_id = ?
                ORDER BY updated_at DESC, id ASC
                """,
                (project_id,),
            ).fetchall()
        return self._json_payload_rows(rows, label="change set")

    def list_change_sets_with_status(
        self,
        statuses: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM change_set
                WHERE status IN ({placeholders})
                ORDER BY updated_at ASC, id ASC
                """,
                statuses,
            ).fetchall()
        return self._json_payload_rows(rows, label="change set")

    def save_post_draft_gate_receipt(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Persist an immutable post-draft Gate receipt idempotently.

        The receipt is deliberately kept outside ``change_set.payload_json`` so
        every Gate attempt remains independently auditable. Successful and
        blocked identities are idempotent; failed attempts remain immutable but
        allow a later attempt for the same candidate/review/package identity.
        """

        values = self._validate_post_draft_gate_payload(payload)
        encoded = self._encode_payload(payload)
        identity = (
            values["change_set_id"],
            values["review_revision"],
            values["candidate_hash"],
            values["validator_package_fingerprint"],
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                change_set = connection.execute(
                    """
                    SELECT project_binding_id, base_revision, base_fingerprint
                    FROM change_set
                    WHERE id = ?
                    """,
                    (values["change_set_id"],),
                ).fetchone()
                if change_set is None:
                    raise RuntimeError("post-draft Gate ChangeSet was not found")
                if (
                    str(change_set["project_binding_id"])
                    != values["project_id"]
                    or int(change_set["base_revision"]) != values["base_revision"]
                    or str(change_set["base_fingerprint"])
                    != values["base_fingerprint"]
                ):
                    raise RuntimeError(
                        "post-draft Gate receipt does not match its ChangeSet base"
                    )

                if values["context_pack_id"] is not None:
                    context_pack = connection.execute(
                        """
                        SELECT project_binding_id, pack_hash, project_revision,
                               project_fingerprint
                        FROM context_pack
                        WHERE id = ?
                        """,
                        (values["context_pack_id"],),
                    ).fetchone()
                    if context_pack is None:
                        raise RuntimeError(
                            "post-draft Gate Context Pack was not found"
                        )
                    if (
                        str(context_pack["project_binding_id"])
                        != values["project_id"]
                        or str(context_pack["pack_hash"])
                        != values["context_pack_hash"]
                        or int(context_pack["project_revision"])
                        != values["base_revision"]
                        or str(context_pack["project_fingerprint"])
                        != values["base_fingerprint"]
                    ):
                        raise RuntimeError(
                            "post-draft Gate Context Pack does not match its ChangeSet"
                        )

                existing_id = connection.execute(
                    """
                    SELECT * FROM post_draft_gate_receipt WHERE id = ?
                    """,
                    (values["gate_receipt_id"],),
                ).fetchone()
                if existing_id is not None:
                    stored = self._post_draft_gate_receipt_payload_from_row(existing_id)
                    if stored != payload:
                        raise RuntimeError(
                            "post-draft Gate receipt ID was already used with "
                            "different immutable content"
                        )
                    connection.commit()
                    return stored

                terminal = connection.execute(
                    """
                    SELECT *
                    FROM post_draft_gate_receipt
                    WHERE change_set_id = ?
                      AND review_revision = ?
                      AND candidate_hash = ?
                      AND validator_package_fingerprint = ?
                      AND status IN ('passed', 'blocked')
                    ORDER BY attempt_ordinal DESC
                    LIMIT 1
                    """,
                    identity,
                ).fetchone()
                if terminal is not None:
                    self._post_draft_gate_receipt_payload_from_row(terminal)
                    raise RuntimeError(
                        "post-draft Gate receipt identity was already used with "
                        "different immutable content"
                    )

                attempt_ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(attempt_ordinal), 0) + 1
                        FROM post_draft_gate_receipt
                        WHERE change_set_id = ?
                        """,
                        (values["change_set_id"],),
                    ).fetchone()[0]
                )

                connection.execute(
                    """
                    INSERT INTO post_draft_gate_receipt(
                        id, change_set_id, project_binding_id, attempt_ordinal,
                        review_revision, candidate_hash, base_revision, base_fingerprint,
                        context_pack_id, context_pack_hash, knowledge_fingerprint,
                        validator_id, validator_version,
                        validator_manifest_fingerprint,
                        validator_package_fingerprint, candidate_file_count,
                        skill_receipt_id, skill_receipt_hash, status, receipt_hash,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["gate_receipt_id"],
                        values["change_set_id"],
                        values["project_id"],
                        attempt_ordinal,
                        values["review_revision"],
                        values["candidate_hash"],
                        values["base_revision"],
                        values["base_fingerprint"],
                        values["context_pack_id"],
                        values["context_pack_hash"],
                        values["knowledge_fingerprint"],
                        values["validator_id"],
                        values["validator_version"],
                        values["validator_manifest_fingerprint"],
                        values["validator_package_fingerprint"],
                        values["candidate_file_count"],
                        values["skill_receipt_id"],
                        values["skill_receipt_hash"],
                        values["status"],
                        values["receipt_hash"],
                        encoded,
                        values["created_at"],
                    ),
                )
                connection.commit()
                return dict(payload)
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RuntimeError(
                    "post-draft Gate receipt identity already exists"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def get_post_draft_gate_receipt(
        self,
        gate_receipt_id: str,
    ) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM post_draft_gate_receipt WHERE id = ?",
                (gate_receipt_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._post_draft_gate_receipt_payload_from_row(row)
        )

    def get_latest_post_draft_gate_receipt(
        self,
        change_set_id: str,
    ) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM post_draft_gate_receipt
                WHERE change_set_id = ?
                ORDER BY attempt_ordinal DESC
                LIMIT 1
                """,
                (change_set_id,),
            ).fetchone()
        return (
            None
            if row is None
            else self._post_draft_gate_receipt_payload_from_row(row)
        )

    def find_post_draft_gate_receipt(
        self,
        change_set_id: str,
        review_revision: int,
        candidate_hash: str,
        base_revision: int,
        base_fingerprint: str,
        context_pack_id: str | None,
        context_pack_hash: str | None,
        knowledge_fingerprint: str | None,
        validator_package_fingerprint: str,
    ) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM post_draft_gate_receipt
                WHERE change_set_id = ?
                  AND review_revision = ?
                  AND candidate_hash = ?
                  AND base_revision = ?
                  AND base_fingerprint = ?
                  AND context_pack_id IS ?
                  AND context_pack_hash IS ?
                  AND knowledge_fingerprint IS ?
                  AND validator_package_fingerprint = ?
                ORDER BY attempt_ordinal DESC
                LIMIT 1
                """,
                (
                    change_set_id,
                    review_revision,
                    candidate_hash,
                    base_revision,
                    base_fingerprint,
                    context_pack_id,
                    context_pack_hash,
                    knowledge_fingerprint,
                    validator_package_fingerprint,
                ),
            ).fetchone()
        return (
            None
            if row is None
            else self._post_draft_gate_receipt_payload_from_row(row)
        )

    def list_post_draft_gate_receipts(
        self,
        change_set_id: str,
    ) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM post_draft_gate_receipt
                WHERE change_set_id = ?
                ORDER BY attempt_ordinal DESC
                """,
                (change_set_id,),
            ).fetchall()
        return [
            self._post_draft_gate_receipt_payload_from_row(row)
            for row in rows
        ]

    def create_source_import_job(self, payload: dict[str, object]) -> None:
        encoded = self._encode_payload(payload)
        book_id, source_key = self._source_import_identity_from_payload(payload)
        with self.connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO source_import_job(
                        id, provider, query, book_id, source_key, status, payload_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["import_id"],
                        payload["provider"],
                        payload["query"],
                        book_id,
                        source_key,
                        payload["status"],
                        encoded,
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("source import identity already exists") from exc

    def save_source_import_job(
        self, payload: dict[str, object], *, expected_payload: dict[str, object] | None = None,
    ) -> None:
        from zhimeng_runtime.source_import_receipt_models import source_import_request_from_payload

        encoded = self._encode_payload(payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM source_import_job WHERE id = ?", (payload["import_id"],),
            ).fetchone()
            if current is None:
                raise RuntimeError("source import was not found while saving")
            current_payload = self._source_import_job_payload_from_row(current)
            if expected_payload is not None and current_payload != expected_payload:
                # A progress update must not restore the earlier request or state
                # over a concurrent edit. Compare and write under one DB lock;
                # the service's in-process lock cannot cover another connection.
                if source_import_request_from_payload(current_payload) != source_import_request_from_payload(expected_payload):
                    raise RuntimeError("source import job request changed while saving progress")
                raise RuntimeError("source import changed before its state was saved")
            result = connection.execute(
                """
                UPDATE source_import_job
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["status"],
                    encoded,
                    payload["updated_at"],
                    payload["import_id"],
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("source import was not found while saving")
            connection.commit()

    def save_source_import_job_with_execution_receipt(
        self,
        payload: dict[str, object],
        receipt: dict[str, object],
        attempt_receipt: dict[str, object] | None = None,
    ) -> None:
        """Atomically publish an awaiting-review job and immutable receipts."""

        from zhimeng_runtime.source_import_receipt_models import (
            LocalSourceImportRequestEvidence,
            SourceImportAttemptReceipt,
            SourceImportRequestEvidence,
            source_import_idempotency_key,
            source_import_request_from_payload,
            source_import_request_hash,
            source_import_run_id,
        )

        source_import_id = str(payload.get("import_id") or "")
        if payload.get("status") != "awaiting_review":
            raise ValueError(
                "source import execution receipt requires an awaiting_review job"
            )
        try:
            job_request = source_import_request_from_payload(payload)
            request_payload = receipt.get("request")
            request_model = (
                LocalSourceImportRequestEvidence
                if receipt.get("provider") == "local_file"
                else SourceImportRequestEvidence
            )
            receipt_request = request_model.model_validate(request_payload)
        except Exception as exc:
            raise ValueError("source import request evidence is invalid") from exc
        run_id = source_import_run_id(source_import_id)
        revision = 1
        if (
            receipt.get("source_import_id") != source_import_id
            or receipt.get("provider") != payload.get("provider")
            or receipt.get("retry_of") != payload.get("retry_of")
            or receipt.get("lineage_status") != "verified"
            or receipt_request != job_request
            or receipt.get("request_hash") != source_import_request_hash(receipt_request)
            or receipt.get("run_id") != run_id
            or receipt.get("revision") != revision
            or receipt.get("idempotency_key")
            != source_import_idempotency_key(
                run_id,
                revision,
                str(receipt.get("request_hash") or ""),
            )
        ):
            raise ValueError(
                "source import receipt does not match its awaiting-review job"
            )
        prepare_tool = receipt.get("prepare_tool")
        distillation_tool = receipt.get("distillation_tool")
        if not isinstance(prepare_tool, dict) or not isinstance(distillation_tool, dict):
            raise ValueError("source import receipt tool evidence is missing")
        required_hashes = (
            "request_hash",
            "source_packet_fingerprint",
            "distillation_input_hash",
            "distillation_output_hash",
            "draft_fingerprint",
            "cards_draft_hash",
            "envelope_hash",
            "receipt_hash",
        )
        if any(len(str(receipt.get(field) or "")) != 64 for field in required_hashes):
            raise ValueError("source import receipt hashes must be SHA-256 values")
        if any(
            len(str(tool.get("tool_fingerprint") or "")) != 64
            for tool in (prepare_tool, distillation_tool)
        ):
            raise ValueError("source import receipt tool fingerprints must be SHA-256 values")
        if not receipt.get("receipt_id") or not receipt.get("created_at"):
            raise ValueError("source import receipt identity and timestamp are required")
        normalized_attempt: dict[str, object] | None = None
        if attempt_receipt is not None:
            try:
                attempt_model = SourceImportAttemptReceipt.model_validate(
                    attempt_receipt
                )
            except Exception as exc:
                raise ValueError("source import attempt receipt is invalid") from exc
            if (
                attempt_model.source_import_id != source_import_id
                or attempt_model.provider != payload.get("provider")
                or attempt_model.status != "success"
                or attempt_model.request_hash != receipt.get("request_hash")
                or attempt_model.execution_receipt_id != receipt.get("receipt_id")
                or attempt_model.execution_receipt_hash != receipt.get("receipt_hash")
                or attempt_model.prepare_tool_fingerprint
                != prepare_tool.get("tool_fingerprint")
                or attempt_model.distillation_tool_fingerprint
                != distillation_tool.get("tool_fingerprint")
            ):
                raise ValueError(
                    "source import attempt receipt does not match its execution receipt"
                )
            normalized_attempt = attempt_model.model_dump(
                mode="json",
                by_alias=True,
            )
        job_json = self._encode_payload(payload)
        receipt_json = self._encode_payload(receipt)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM source_import_job WHERE id = ?",
                    (source_import_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("source import was not found while publishing receipt")
                if str(current["status"]) != "drafting":
                    raise RuntimeError(
                        "source import changed before its execution receipt was committed"
                    )
                try:
                    current_payload = self._source_import_job_payload_from_row(current)
                    current_request = source_import_request_from_payload(current_payload)
                    if (
                        current_request != receipt_request
                        or current_payload.get("provider") != receipt.get("provider")
                        or current_payload.get("retry_of") != receipt.get("retry_of")
                    ):
                        raise ValueError("source import request changed")
                except Exception as exc:
                    raise RuntimeError(
                        "source import job request changed before receipt commit"
                    ) from exc
                connection.execute(
                    """
                    INSERT INTO source_import_execution_receipt(
                        id, source_import_id, run_id, revision, idempotency_key,
                        request_hash,
                        prepare_invocation_id, prepare_tool_fingerprint,
                        source_packet_fingerprint, distillation_invocation_id,
                        distillation_tool_fingerprint, distillation_input_hash,
                        distillation_output_hash, draft_fingerprint,
                        cards_draft_hash, envelope_hash, receipt_hash,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt["receipt_id"],
                        source_import_id,
                        receipt["run_id"],
                        receipt["revision"],
                        receipt["idempotency_key"],
                        receipt["request_hash"],
                        receipt["prepare_invocation_id"],
                        prepare_tool["tool_fingerprint"],
                        receipt["source_packet_fingerprint"],
                        receipt["distillation_invocation_id"],
                        distillation_tool["tool_fingerprint"],
                        receipt["distillation_input_hash"],
                        receipt["distillation_output_hash"],
                        receipt["draft_fingerprint"],
                        receipt["cards_draft_hash"],
                        receipt["envelope_hash"],
                        receipt["receipt_hash"],
                        receipt_json,
                        receipt["created_at"],
                    ),
                )
                if normalized_attempt is not None:
                    self._insert_source_import_attempt_receipt(
                        connection,
                        normalized_attempt,
                    )
                result = connection.execute(
                    """
                    UPDATE source_import_job
                    SET status = 'awaiting_review', payload_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'drafting'
                    """,
                    (job_json, payload["updated_at"], source_import_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        "source import changed before its execution receipt was committed"
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RuntimeError(
                    "source import execution receipt identity already exists"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def save_source_import_job_with_attempt_receipt(
        self,
        payload: dict[str, object],
        attempt_receipt: dict[str, object],
    ) -> None:
        """Atomically close an unsuccessful job and append its attempt receipt."""

        from zhimeng_runtime.source_import_receipt_models import (
            SourceImportAttemptReceipt,
        )

        try:
            attempt_model = SourceImportAttemptReceipt.model_validate(attempt_receipt)
        except Exception as exc:
            raise ValueError("source import attempt receipt is invalid") from exc
        source_import_id = str(payload.get("import_id") or "")
        terminal_status = str(payload.get("status") or "")
        if terminal_status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError(
                "source import attempt receipt requires an unsuccessful terminal job"
            )
        if (
            attempt_model.source_import_id != source_import_id
            or attempt_model.provider != payload.get("provider")
            or attempt_model.status != terminal_status
        ):
            raise ValueError("source import attempt receipt does not match its job")
        normalized_attempt = attempt_model.model_dump(mode="json", by_alias=True)
        job_json = self._encode_payload(payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT status FROM source_import_job WHERE id = ?",
                    (source_import_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(
                        "source import was not found while publishing attempt receipt"
                    )
                current_status = str(current["status"])
                if current_status in {
                    "completed",
                    "cancelled",
                    "failed",
                    "interrupted",
                }:
                    raise RuntimeError(
                        "source import was already terminal before attempt receipt commit"
                    )
                self._insert_source_import_attempt_receipt(
                    connection,
                    normalized_attempt,
                )
                result = connection.execute(
                    """
                    UPDATE source_import_job
                    SET status = ?, payload_json = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        terminal_status,
                        job_json,
                        payload["updated_at"],
                        source_import_id,
                        current_status,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        "source import changed before its attempt receipt was committed"
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise RuntimeError(
                    "source import attempt receipt identity already exists"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _insert_source_import_attempt_receipt(
        connection: sqlite3.Connection,
        receipt: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_import_attempt_receipt(
                id, source_import_id, attempt_ordinal, status, stage, provider,
                request_hash, execution_receipt_id, execution_receipt_hash,
                failure_signature, cleanup_status, cleanup_hash, receipt_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt["receipt_id"],
                receipt["source_import_id"],
                receipt["attempt_ordinal"],
                receipt["status"],
                receipt["stage"],
                receipt["provider"],
                receipt.get("request_hash"),
                receipt.get("execution_receipt_id"),
                receipt.get("execution_receipt_hash"),
                receipt.get("failure_signature"),
                receipt["cleanup_status"],
                receipt["cleanup_hash"],
                receipt["receipt_hash"],
                SqliteStore._encode_payload(receipt),
                receipt["created_at"],
            ),
        )

    def get_source_import_execution_receipt(
        self,
        source_import_id: str,
    ) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM source_import_execution_receipt
                WHERE source_import_id = ?
                """,
                (source_import_id,),
            ).fetchone()
        if row is None:
            return None
        identity = (row["run_id"], row["revision"], row["idempotency_key"])
        if all(value is None for value in identity):
            return None
        if any(value is None for value in identity):
            raise RuntimeError("stored source import receipt has partial execution identity")
        payload = self._json_payload_row(row, label="source import execution receipt")
        prepare_tool = payload.get("prepare_tool")
        distillation_tool = payload.get("distillation_tool")
        if not isinstance(prepare_tool, dict) or not isinstance(distillation_tool, dict):
            raise RuntimeError("stored source import receipt has invalid tool evidence")
        expected = {
            "receipt_id": row["id"],
            "source_import_id": row["source_import_id"],
            "run_id": row["run_id"],
            "revision": row["revision"],
            "idempotency_key": row["idempotency_key"],
            "request_hash": row["request_hash"],
            "prepare_invocation_id": row["prepare_invocation_id"],
            "source_packet_fingerprint": row["source_packet_fingerprint"],
            "distillation_invocation_id": row["distillation_invocation_id"],
            "distillation_input_hash": row["distillation_input_hash"],
            "distillation_output_hash": row["distillation_output_hash"],
            "draft_fingerprint": row["draft_fingerprint"],
            "cards_draft_hash": row["cards_draft_hash"],
            "envelope_hash": row["envelope_hash"],
            "receipt_hash": row["receipt_hash"],
            "created_at": row["created_at"],
        }
        if any(str(payload.get(field)) != str(value) for field, value in expected.items()):
            raise RuntimeError(
                "stored source import receipt columns do not match its payload"
            )
        if (
            str(prepare_tool.get("tool_fingerprint"))
            != str(row["prepare_tool_fingerprint"])
            or str(distillation_tool.get("tool_fingerprint"))
            != str(row["distillation_tool_fingerprint"])
        ):
            raise RuntimeError(
                "stored source import receipt tool fingerprints do not match"
            )
        return payload

    def list_source_import_attempt_receipts(
        self,
        source_import_id: str,
    ) -> list[dict[str, object]]:
        from zhimeng_runtime.source_import_receipt_models import (
            SourceImportAttemptReceipt,
        )

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM source_import_attempt_receipt
                WHERE source_import_id = ?
                ORDER BY attempt_ordinal ASC, created_at ASC
                """,
                (source_import_id,),
            ).fetchall()
        receipts: list[dict[str, object]] = []
        for row in rows:
            payload = self._json_payload_row(
                row,
                label="source import attempt receipt",
            )
            try:
                model = SourceImportAttemptReceipt.model_validate(payload)
            except Exception as exc:
                raise RuntimeError(
                    "stored source import attempt receipt is invalid"
                ) from exc
            expected = {
                "receipt_id": row["id"],
                "source_import_id": row["source_import_id"],
                "attempt_ordinal": row["attempt_ordinal"],
                "status": row["status"],
                "stage": row["stage"],
                "provider": row["provider"],
                "request_hash": row["request_hash"],
                "execution_receipt_id": row["execution_receipt_id"],
                "execution_receipt_hash": row["execution_receipt_hash"],
                "failure_signature": row["failure_signature"],
                "cleanup_status": row["cleanup_status"],
                "cleanup_hash": row["cleanup_hash"],
                "receipt_hash": row["receipt_hash"],
            }
            observed = model.model_dump(mode="json", by_alias=True)
            if any(
                str(observed.get(field)) != str(value)
                for field, value in expected.items()
            ) or not self._stored_datetime_matches(
                observed.get("created_at"),
                row["created_at"],
            ):
                raise RuntimeError(
                    "stored source import attempt columns do not match its payload"
                )
            receipts.append(observed)
        return receipts

    def get_source_import_job(self, import_id: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_import_job WHERE id = ?",
                (import_id,),
            ).fetchone()
        if row is None:
            return None
        return self._source_import_job_payload_from_row(row)

    def list_source_import_jobs(self) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM source_import_job
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._source_import_job_payload_from_row(row) for row in rows]

    def list_source_import_jobs_with_status(
        self,
        statuses: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM source_import_job
                WHERE status IN ({placeholders})
                ORDER BY updated_at ASC, id ASC
                """,
                statuses,
            ).fetchall()
        return [self._source_import_job_payload_from_row(row) for row in rows]

    def recover_interrupted_source_imports(self) -> int:
        """Close source-import work that cannot survive a Runtime restart."""

        from zhimeng_runtime.source_import_models import (
            ACTIVE_SOURCE_IMPORT_STATUSES,
            SourceImportProgress,
            SourceImportStatus,
            parse_source_import_job,
        )

        statuses = tuple(
            sorted(status.value for status in ACTIVE_SOURCE_IMPORT_STATUSES)
        )
        placeholders = ", ".join("?" for _ in statuses)
        recovered_at = datetime.now(UTC)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM source_import_job
                    WHERE status IN ({placeholders})
                    ORDER BY updated_at ASC, id ASC
                    """,
                    statuses,
                ).fetchall()
                for row in rows:
                    record = parse_source_import_job(
                        self._source_import_job_payload_from_row(row)
                    )
                    recovered = parse_source_import_job(
                        record.model_copy(
                            update={
                                "status": SourceImportStatus.INTERRUPTED,
                                "progress": SourceImportProgress(
                                    stage="interrupted",
                                    message=(
                                        "Runtime stopped before this import reached "
                                        "a durable review point"
                                    ),
                                    saved_chapters=record.progress.saved_chapters,
                                    total_chapters=record.progress.total_chapters,
                                    percent=record.progress.percent,
                                ),
                                "error": (
                                    "The previous Runtime stopped during this import; "
                                    "retry creates a new traceable job."
                                ),
                                "updated_at": recovered_at,
                            }
                        ).model_dump(mode="json", by_alias=True)
                    )
                    payload = recovered.model_dump(mode="json", by_alias=True)
                    result = connection.execute(
                        """
                        UPDATE source_import_job
                        SET status = 'interrupted', payload_json = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            self._encode_payload(payload),
                            recovered.updated_at.isoformat(),
                            recovered.import_id,
                            record.status.value,
                        ),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(
                            "source import changed during startup recovery"
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return len(rows)

    @staticmethod
    def _encode_payload(payload: dict[str, object]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _json_payload_row(
        row: sqlite3.Row,
        *,
        label: str,
    ) -> dict[str, object]:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise RuntimeError(f"stored {label} payload is not an object")
        return payload

    @staticmethod
    def _validate_post_draft_gate_payload(
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Validate and extract the scalar identity persisted beside a Gate payload."""

        if not isinstance(payload, dict):
            raise ValueError("post-draft Gate receipt payload must be an object")

        # The scalar columns are an index and integrity mirror, not a second
        # receipt definition. Validate the complete immutable payload here so a
        # direct store caller cannot persist a hash-valid-looking but malformed
        # Gate result that later bypasses Runtime validation.
        from zhimeng_runtime.post_draft_gate_models import PostDraftGateReceipt

        try:
            PostDraftGateReceipt.model_validate_json(
                json.dumps(payload, ensure_ascii=False),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("post-draft Gate receipt payload failed its contract") from exc

        required = (
            "gate_receipt_id",
            "change_set_id",
            "project_id",
            "review_revision",
            "candidate_hash",
            "base_revision",
            "base_fingerprint",
            "validator_id",
            "validator_version",
            "validator_manifest_fingerprint",
            "validator_package_fingerprint",
            "candidate_file_count",
            "status",
            "receipt_hash",
            "created_at",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise ValueError(
                "post-draft Gate receipt is missing fields: " + ", ".join(missing)
            )

        def text(name: str) -> str:
            value = payload[name]
            if not isinstance(value, str) or not value:
                raise ValueError(f"post-draft Gate receipt {name} must be non-empty text")
            return value

        def sha(name: str) -> str:
            value = text(name)
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(
                    f"post-draft Gate receipt {name} must be lowercase SHA-256"
                )
            return value

        gate_receipt_id = text("gate_receipt_id")
        prefix = "post-draft-gate-"
        if (
            not gate_receipt_id.startswith(prefix)
            or len(gate_receipt_id) != len(prefix) + 32
            or any(character not in "0123456789abcdef" for character in gate_receipt_id[len(prefix) :])
        ):
            raise ValueError("post-draft Gate receipt gate_receipt_id is invalid")

        project_id = text("project_id")
        review_revision = payload["review_revision"]
        if isinstance(review_revision, bool) or not isinstance(review_revision, int) or review_revision < 0:
            raise ValueError("post-draft Gate receipt review_revision is invalid")
        base_revision = payload["base_revision"]
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 1:
            raise ValueError("post-draft Gate receipt base_revision is invalid")
        candidate_file_count = payload["candidate_file_count"]
        if (
            isinstance(candidate_file_count, bool)
            or not isinstance(candidate_file_count, int)
            or candidate_file_count < 1
        ):
            raise ValueError("post-draft Gate receipt candidate_file_count is invalid")

        status = payload["status"]
        if status not in {"passed", "blocked", "failed"}:
            raise ValueError("post-draft Gate receipt status is invalid")

        created_at = text("created_at")
        try:
            parsed_created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("post-draft Gate receipt created_at is invalid") from exc
        if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
            raise ValueError("post-draft Gate receipt created_at must be timezone-aware")

        context_pack_id = payload.get("context_pack_id")
        context_pack_hash = payload.get("context_pack_hash")
        if context_pack_id is not None and (
            not isinstance(context_pack_id, str) or not context_pack_id
        ):
            raise ValueError("post-draft Gate receipt context_pack_id is invalid")
        if context_pack_hash is not None:
            if not isinstance(context_pack_hash, str):
                raise ValueError("post-draft Gate receipt context_pack_hash is invalid")
            context_pack_hash = sha("context_pack_hash")
        if (context_pack_id is None) != (context_pack_hash is None):
            raise ValueError(
                "post-draft Gate receipt Context Pack identity and hash must pair"
            )

        knowledge_fingerprint = payload.get("knowledge_fingerprint")
        if knowledge_fingerprint is not None:
            knowledge_fingerprint = sha("knowledge_fingerprint")

        skill_receipt_id = payload.get("skill_receipt_id")
        skill_receipt_hash = payload.get("skill_receipt_hash")
        if skill_receipt_id is not None and (
            not isinstance(skill_receipt_id, str) or not skill_receipt_id
        ):
            raise ValueError("post-draft Gate receipt skill_receipt_id is invalid")
        if skill_receipt_hash is not None:
            if not isinstance(skill_receipt_hash, str):
                raise ValueError("post-draft Gate receipt skill_receipt_hash is invalid")
            skill_receipt_hash = sha("skill_receipt_hash")
        if (skill_receipt_id is None) != (skill_receipt_hash is None):
            raise ValueError(
                "post-draft Gate receipt Skill identity and hash must pair"
            )

        return {
            "gate_receipt_id": gate_receipt_id,
            "change_set_id": text("change_set_id"),
            "project_id": project_id,
            "review_revision": review_revision,
            "candidate_hash": sha("candidate_hash"),
            "base_revision": base_revision,
            "base_fingerprint": sha("base_fingerprint"),
            "context_pack_id": context_pack_id,
            "context_pack_hash": context_pack_hash,
            "knowledge_fingerprint": knowledge_fingerprint,
            "validator_id": text("validator_id"),
            "validator_version": text("validator_version"),
            "validator_manifest_fingerprint": sha("validator_manifest_fingerprint"),
            "validator_package_fingerprint": sha("validator_package_fingerprint"),
            "candidate_file_count": candidate_file_count,
            "skill_receipt_id": skill_receipt_id,
            "skill_receipt_hash": skill_receipt_hash,
            "status": status,
            "receipt_hash": sha("receipt_hash"),
            "created_at": created_at,
        }

    @classmethod
    def _post_draft_gate_receipt_payload_from_row(
        cls,
        row: sqlite3.Row,
    ) -> dict[str, object]:
        payload = cls._json_payload_row(row, label="post-draft Gate receipt")
        values = cls._validate_post_draft_gate_payload(payload)
        expected = {
            "gate_receipt_id": row["id"],
            "change_set_id": row["change_set_id"],
            "project_id": row["project_binding_id"],
            "review_revision": row["review_revision"],
            "candidate_hash": row["candidate_hash"],
            "base_revision": row["base_revision"],
            "base_fingerprint": row["base_fingerprint"],
            "context_pack_id": row["context_pack_id"],
            "context_pack_hash": row["context_pack_hash"],
            "knowledge_fingerprint": row["knowledge_fingerprint"],
            "validator_id": row["validator_id"],
            "validator_version": row["validator_version"],
            "validator_manifest_fingerprint": row["validator_manifest_fingerprint"],
            "validator_package_fingerprint": row["validator_package_fingerprint"],
            "candidate_file_count": row["candidate_file_count"],
            "skill_receipt_id": row["skill_receipt_id"],
            "skill_receipt_hash": row["skill_receipt_hash"],
            "status": row["status"],
            "receipt_hash": row["receipt_hash"],
        }
        for field, expected_value in expected.items():
            if values[field] != expected_value:
                raise RuntimeError(
                    "stored post-draft Gate receipt column does not match its payload: "
                    + field
                )
        if not cls._stored_datetime_matches(values["created_at"], row["created_at"]):
            raise RuntimeError(
                "stored post-draft Gate receipt column does not match its payload: created_at"
            )
        return payload

    @staticmethod
    def _json_payload_rows(
        rows: list[sqlite3.Row],
        *,
        label: str,
    ) -> list[dict[str, object]]:
        return [
            SqliteStore._json_payload_row(row, label=label)
            for row in rows
        ]

    @staticmethod
    def _source_import_job_payload_from_row(
        row: sqlite3.Row,
    ) -> dict[str, object]:
        payload = SqliteStore._json_payload_row(row, label="source import")
        try:
            observed_book_id, observed_source_key = (
                SqliteStore._source_import_identity_from_payload(payload)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored source import candidate is invalid") from exc
        expected = {
            "import_id": row["id"],
            "provider": row["provider"],
            "query": row["query"],
            "book_id": row["book_id"],
            "source_key": row["source_key"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        observed = {
            "import_id": payload.get("import_id"),
            "provider": payload.get("provider"),
            "query": payload.get("query"),
            "book_id": observed_book_id,
            "source_key": observed_source_key,
            "status": payload.get("status"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }
        scalar_fields = (
            "import_id",
            "provider",
            "query",
            "book_id",
            "source_key",
            "status",
        )
        if any(
            str(observed[field]) != str(expected[field]) for field in scalar_fields
        ) or any(
            not SqliteStore._stored_datetime_matches(
                observed[field],
                expected[field],
            )
            for field in ("created_at", "updated_at")
        ):
            raise RuntimeError(
                "stored source import columns do not match its payload"
            )
        return payload

    @staticmethod
    def _source_import_identity_from_payload(
        payload: dict[str, object],
    ) -> tuple[str | None, str]:
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("source import candidate must be an object")
        provider = str(payload.get("provider") or "")
        if provider == "tomato":
            book_id = str(candidate.get("book_id") or "").strip()
            if not book_id:
                raise ValueError("Tomato source import requires a book ID")
            return book_id, book_id
        if provider == "local_file":
            source_key = str(
                candidate.get("source_key")
                or candidate.get("source_locator_hash")
                or candidate.get("source_sha256")
                or ""
            ).strip()
            if not source_key:
                raise ValueError("local-file source import requires a source key")
            return None, source_key
        if provider == "feedback_experience":
            source_key = str(
                candidate.get("package_hash")
                or candidate.get("package_id")
                or ""
            ).strip()
            if not source_key:
                raise ValueError("feedback_experience source import requires package identity")
            return None, source_key
        raise ValueError("unsupported source import provider")

    @staticmethod
    def _stored_datetime_matches(left: object, right: object) -> bool:
        try:
            left_value = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
            right_value = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
        except ValueError:
            return False
        return left_value == right_value

    @staticmethod
    def _change_set_provenance_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "change_set_id": str(row["change_set_id"]),
            "run_id": str(row["run_id"]),
            "tool_call_id": str(row["tool_call_id"]),
            "context_pack_id": str(row["context_pack_id"]),
            "project_id": str(row["project_id"]),
            "project_revision": int(row["project_revision"]),
            "project_fingerprint": str(row["project_fingerprint"]),
            "request_hash": str(row["request_hash"]),
            "created_at": str(row["created_at"]),
        }

    def probe(self) -> DatabaseHealth:
        try:
            with self.connection() as connection:
                connection.execute("SELECT 1").fetchone()
                return self._health_from_connection(connection)
        except sqlite3.Error:
            return DatabaseHealth(
                ready=False,
                journal_mode="unknown",
                schema_version=0,
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # 等写锁最多 30 秒：2026-09-13 真机上作者点「撤回」时 Agent 正在整理这本书的资料索引
        # （一个长写事务），5 秒就放弃直接 500 "database is locked"，撤回失败。宁可多等几秒。
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current_version} is newer than runtime schema {SCHEMA_VERSION}"
            )

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            project_columns = SqliteStore._table_columns(connection, "project_binding")
            if current_version < 2 and "fingerprint" not in project_columns:
                connection.execute(
                    "ALTER TABLE project_binding ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"
                )
            elif current_version >= 2 and "fingerprint" not in project_columns:
                raise RuntimeError(
                    "project_binding is missing fingerprint at schema version 2"
                )
            session_columns = SqliteStore._table_columns(
                connection,
                "execution_session",
            )
            if "pinned" not in session_columns:
                if current_version >= 24:
                    raise RuntimeError(
                        "execution_session is missing pinned at schema version 24"
                    )
                connection.execute(
                    "ALTER TABLE execution_session "
                    "ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (pinned IN (0, 1))"
                )
            run_columns = SqliteStore._table_columns(connection, "run")
            pre_context_run_columns = REQUIRED_COLUMNS["run"] - {"context_pack_id", "origin_kind", "stop_generation"}
            if not pre_context_run_columns.issubset(run_columns):
                SqliteStore._rebuild_run_schema_v6(connection, run_columns)
            elif "context_pack_id" not in run_columns:
                connection.execute(
                    """
                    ALTER TABLE run
                    ADD COLUMN context_pack_id TEXT
                    REFERENCES context_pack(id) ON DELETE RESTRICT
                    """
                )
            run_event_columns = SqliteStore._table_columns(connection, "run_event")
            if "activity_id" not in run_event_columns:
                connection.execute(
                    "ALTER TABLE run_event ADD COLUMN activity_id TEXT REFERENCES activity(id) ON DELETE CASCADE"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_event_activity
                ON run_event(activity_id) WHERE activity_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_context_pack_unique
                ON run(context_pack_id) WHERE context_pack_id IS NOT NULL
                """
            )
            tool_call_columns = SqliteStore._table_columns(connection, "tool_call")
            if not REQUIRED_COLUMNS["tool_call"].issubset(tool_call_columns):
                if current_version >= 7:
                    raise RuntimeError(
                        "tool_call is missing required columns at schema version 7"
                    )
                SqliteStore._rebuild_tool_call_schema_v7(
                    connection,
                    tool_call_columns,
                )
            skill_version_columns = SqliteStore._table_columns(
                connection,
                "skill_version",
            )
            lifecycle_skill_columns = {
                "manifest_fingerprint": (
                    "TEXT CHECK (manifest_fingerprint IS NULL "
                    "OR length(manifest_fingerprint) = 64)"
                ),
                "adapter_id": "TEXT",
                "adapter_version": "TEXT",
                "adapter_fingerprint": (
                    "TEXT CHECK (adapter_fingerprint IS NULL "
                    "OR length(adapter_fingerprint) = 64)"
                ),
                "input_contract_fingerprint": (
                    "TEXT CHECK (input_contract_fingerprint IS NULL "
                    "OR length(input_contract_fingerprint) = 64)"
                ),
                "output_contract_fingerprint": (
                    "TEXT CHECK (output_contract_fingerprint IS NULL "
                    "OR length(output_contract_fingerprint) = 64)"
                ),
                "contract_bundle_fingerprint": (
                    "TEXT CHECK (contract_bundle_fingerprint IS NULL "
                    "OR length(contract_bundle_fingerprint) = 64)"
                ),
                "file_count": "INTEGER CHECK (file_count IS NULL OR file_count >= 1)",
                "total_bytes": "INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 1)",
                "snapshot_status": (
                    "TEXT NOT NULL DEFAULT 'legacy_unmanaged' CHECK "
                    "(snapshot_status IN "
                    "('managed', 'legacy_unmanaged', 'quarantined'))"
                ),
            }
            missing_skill_columns = (
                lifecycle_skill_columns.keys() - skill_version_columns
            )
            if missing_skill_columns and current_version >= 18:
                raise RuntimeError(
                    "skill_version is missing lifecycle columns at schema version 18"
                )
            for column_name in lifecycle_skill_columns:
                if column_name not in missing_skill_columns:
                    continue
                connection.execute(
                    "ALTER TABLE skill_version ADD COLUMN "
                    f"{column_name} {lifecycle_skill_columns[column_name]}"
                )
            candidate_columns = SqliteStore._table_columns(
                connection,
                "agent_candidate_output",
            )
            if not REQUIRED_COLUMNS["agent_candidate_output"].issubset(
                candidate_columns
            ):
                raise RuntimeError(
                    "agent_candidate_output is missing required columns at "
                    "schema version 17"
                )
            source_import_table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("source_import_job",),
            ).fetchone()
            source_import_columns = SqliteStore._table_columns(
                connection,
                "source_import_job",
            )
            source_import_v19_ready = (
                source_import_table is not None
                and source_import_table["sql"] is not None
                and "source_key" in source_import_columns
                and "local_file" in str(source_import_table["sql"])
            )
            if current_version >= 19 and not source_import_v19_ready:
                raise RuntimeError(
                    "source_import_job is missing the local-file contract at schema version 19"
                )
            project_kb_binding_columns = {
                "project_kb_state_id",
                "project_kb_manifest_hash",
            }
            project_kb_binding_tables = (
                "context_pack",
                "nlcl_route_receipt",
                "nlcl_route_execution_receipt",
            )
            missing_project_kb_bindings = {
                table_name: project_kb_binding_columns
                - SqliteStore._table_columns(connection, table_name)
                for table_name in project_kb_binding_tables
            }
            if current_version >= 20 and any(
                missing_project_kb_bindings.values()
            ):
                raise RuntimeError(
                    "Context/NLCL tables are missing project-KB binding columns "
                    "at schema version 20"
                )
            if current_version >= 21:
                missing_distillation_tables = {
                    table_name
                    for table_name in (
                        "distillation_invocation",
                        "distillation_invocation_receipt",
                    )
                    if not SqliteStore._table_columns(connection, table_name)
                }
                missing_distillation_columns = {
                    table_name: REQUIRED_COLUMNS[table_name]
                    - SqliteStore._table_columns(connection, table_name)
                    for table_name in (
                        "distillation_invocation",
                        "distillation_invocation_receipt",
                    )
                }
                candidate_binding_missing = (
                    "runtime_distillation_invocation_id"
                    not in SqliteStore._table_columns(
                        connection,
                        "knowledge_candidate",
                    )
                )
                if (
                    missing_distillation_tables
                    or any(missing_distillation_columns.values())
                    or candidate_binding_missing
                ):
                    raise RuntimeError(
                        "distillation invocation schema is incomplete at schema version 21"
                    )
            if current_version < 9:
                SqliteStore._backfill_nlcl_route_receipts_v9(connection)
            if current_version < 11:
                SqliteStore._backfill_prewrite_operation_packages_v11(connection)
            if current_version < 12:
                SqliteStore._migrate_knowledge_lineage_v12(connection)
            if current_version < 13:
                SqliteStore._migrate_source_import_receipts_v13(connection)
            if current_version < 14:
                SqliteStore._migrate_knowledge_versions_v14(connection)
            if current_version < 15:
                SqliteStore._migrate_knowledge_scopes_v15(connection)
            # v16 briefly shipped in development with a UNIQUE candidate
            # identity. Run the v16 structural audit at version 16 as well so
            # those databases can retain failed-attempt history after restart.
            if current_version <= 16:
                SqliteStore._migrate_post_draft_gate_receipts_v16(connection)
            if current_version < 19:
                SqliteStore._migrate_source_import_jobs_v19(connection)
            # Also self-heal v20 databases created before the binding triggers
            # were introduced. The helper is idempotent once both columns exist.
            SqliteStore._migrate_project_kb_bindings_v20(connection)
            # Trigger and index definitions are managed on every restart so a
            # v21 database cannot keep a locally relaxed immutable ledger.
            SqliteStore._migrate_distillation_invocations_v21(connection)
            if current_version < 23:
                SqliteStore._migrate_source_import_feedback_provider_v23(connection)
            if current_version < 25:
                SqliteStore._migrate_agent_casual_run_v25(connection)
            if "through_message_ordinal" not in SqliteStore._table_columns(connection, "run_transcript"):
                connection.execute("ALTER TABLE run_transcript ADD COLUMN through_message_ordinal INTEGER")
            if "images_json" not in SqliteStore._table_columns(connection, "session_message"):
                connection.execute("ALTER TABLE session_message ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]'")
            for column, declaration in (("origin_kind", "TEXT NOT NULL DEFAULT 'user'"), ("stop_generation", "INTEGER NOT NULL DEFAULT 0")):
                if column not in SqliteStore._table_columns(connection, "run"):
                    if current_version >= 31:
                        raise RuntimeError(f"run is missing {column} at schema version 31")
                    connection.execute(f"ALTER TABLE run ADD COLUMN {column} {declaration}")
            if "apply_phase" not in SqliteStore._table_columns(connection, "file_change"):
                if current_version >= 32:
                    raise RuntimeError("file_change is missing apply_phase at schema version 32")
                connection.execute("ALTER TABLE file_change ADD COLUMN apply_phase TEXT NOT NULL DEFAULT 'legacy' CHECK (apply_phase IN ('legacy', 'pending', 'applied', 'failed'))")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("database migration produced foreign key violations")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }

    @staticmethod
    def _migrate_agent_casual_run_v25(connection: sqlite3.Connection) -> None:
        """放行无项目 Run（「随便聊」会话）。

        闲聊会话不绑定小说项目，其 Run 没有项目身份；旧约束要求 run 表的
        project_binding_id / project_revision / project_fingerprint 全部非空。
        SQLite 无法就地放宽 NOT NULL，按库内既有做法整表重建：既有行三列
        均有值，逐行原样搬运，形状之外零改动。
        """
        info = connection.execute("PRAGMA table_info(run)").fetchall()
        if not info:
            # 极旧库还没有 run 表：SCHEMA_SQL 已按新形状建好，无需迁移。
            return
        notnull_by_name = {str(row[1]): int(row[3]) for row in info}
        if notnull_by_name.get("project_binding_id") == 0 and notnull_by_name.get(
            "project_revision"
        ) == 0:
            # 全新库由 SCHEMA_SQL 直接建成新形状，无需重建。
            return
        connection.execute(
            """
            CREATE TABLE run_v25 (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
                project_binding_id TEXT REFERENCES project_binding(id) ON DELETE RESTRICT,
                context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                run_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'starting', 'running', 'awaiting_approval', 'paused',
                        'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted'
                    )
                ),
                objective TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
                project_revision INTEGER CHECK (
                    project_revision IS NULL OR project_revision >= 1
                ),
                project_fingerprint TEXT CHECK (
                    project_fingerprint IS NULL OR length(project_fingerprint) = 64
                ),
                executor_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                last_activity_sequence INTEGER NOT NULL DEFAULT 0
                    CHECK (last_activity_sequence >= 0),
                error_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE (session_id, ordinal),
                UNIQUE (session_id, idempotency_key)
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO run_v25 (
                id, session_id, project_binding_id, context_pack_id, ordinal,
                run_type, status, objective, idempotency_key, request_hash,
                project_revision, project_fingerprint, executor_json,
                capabilities_json, correlation_id, causation_id,
                last_activity_sequence, error_json, created_at, started_at,
                updated_at, completed_at
            )
            SELECT
                id, session_id, project_binding_id, context_pack_id, ordinal,
                run_type, status, objective, idempotency_key, request_hash,
                project_revision, project_fingerprint, executor_json,
                capabilities_json, correlation_id, causation_id,
                last_activity_sequence, error_json, created_at, started_at,
                updated_at, completed_at
            FROM run
            """
        )
        connection.execute("DROP TABLE run")
        connection.execute("ALTER TABLE run_v25 RENAME TO run")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_project_status "
            "ON run(project_binding_id, status, created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_context_pack_unique "
            "ON run(context_pack_id) WHERE context_pack_id IS NOT NULL"
        )
        # activity 表同样放行无项目身份（「随便聊」Run 也会产生活动流）。
        connection.execute(
            """
            CREATE TABLE activity_v25 (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                project_revision INTEGER CHECK (
                    project_revision IS NULL OR project_revision >= 1
                ),
                project_fingerprint TEXT CHECK (
                    project_fingerprint IS NULL OR length(project_fingerprint) = 64
                ),
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, sequence)
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO activity_v25 (
                id, session_id, run_id, sequence, kind, payload_json,
                project_revision, project_fingerprint, correlation_id,
                causation_id, created_at
            )
            SELECT
                id, session_id, run_id, sequence, kind, payload_json,
                project_revision, project_fingerprint, correlation_id,
                causation_id, created_at
            FROM activity
            """
        )
        connection.execute("DROP TABLE activity")
        connection.execute("ALTER TABLE activity_v25 RENAME TO activity")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_run_sequence "
            "ON activity(run_id, sequence)"
        )
        # context_pack 的绑定触发器（v20 自愈逻辑）随 DROP run 之外独立存在，
        # 但重建期间保持幂等：这里主动跑一遍，别等下一次重启。
        SqliteStore._migrate_project_kb_bindings_v20(connection)

    @staticmethod
    def _migrate_project_kb_bindings_v20(connection: sqlite3.Connection) -> None:
        """Add nullable binding columns without fabricating identities for v19 rows."""

        tables = (
            "context_pack",
            "nlcl_route_receipt",
            "nlcl_route_execution_receipt",
        )
        column_types = {
            "project_kb_state_id": (
                "TEXT CHECK (project_kb_state_id IS NULL "
                "OR (length(project_kb_state_id) = 49 "
                "AND substr(project_kb_state_id, 1, 17) = 'project-kb-state-' "
                "AND substr(project_kb_state_id, 18) NOT GLOB '*[^0-9a-f]*'))"
            ),
            "project_kb_manifest_hash": (
                "TEXT CHECK (project_kb_manifest_hash IS NULL "
                "OR (length(project_kb_manifest_hash) = 64 "
                "AND project_kb_manifest_hash NOT GLOB '*[^0-9a-f]*'))"
            ),
        }
        for table_name in tables:
            columns = SqliteStore._table_columns(connection, table_name)
            for column_name, column_type in column_types.items():
                if column_name in columns:
                    continue
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )
            invalid_binding = """
                (NEW.project_kb_state_id IS NULL)
                    != (NEW.project_kb_manifest_hash IS NULL)
                OR (
                    NEW.project_kb_state_id IS NOT NULL
                    AND (
                        length(NEW.project_kb_state_id) != 49
                        OR substr(NEW.project_kb_state_id, 1, 17)
                            != 'project-kb-state-'
                        OR substr(NEW.project_kb_state_id, 18)
                            GLOB '*[^0-9a-f]*'
                        OR length(NEW.project_kb_manifest_hash) != 64
                        OR NEW.project_kb_manifest_hash GLOB '*[^0-9a-f]*'
                    )
                )
            """
            for operation in ("INSERT", "UPDATE"):
                trigger_name = (
                    f"trg_{table_name}_project_kb_binding_{operation.lower()}"
                )
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                connection.execute(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE {operation} ON {table_name}
                    FOR EACH ROW WHEN {invalid_binding}
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid project KB binding');
                    END
                    """
                )

    @staticmethod
    def _migrate_distillation_invocations_v21(
        connection: sqlite3.Connection,
    ) -> None:
        """Install the deep-distillation head and its append-only CAS ledger."""

        connection.execute(DISTILLATION_INVOCATION_TABLE_SQL)
        connection.execute(DISTILLATION_INVOCATION_RECEIPT_TABLE_SQL)
        connection.execute("""
        CREATE TABLE IF NOT EXISTS skill_handoff_run (
            id TEXT PRIMARY KEY CHECK (
                length(id) = length('skill-handoff-') + 32
                AND substr(id, 1, length('skill-handoff-')) = 'skill-handoff-'
                AND substr(id, length('skill-handoff-') + 1) NOT GLOB '*[^0-9a-f]*'
            ),
            handoff_key TEXT NOT NULL CHECK (handoff_key = 'novel-distillation-to-kb-manager'),
            project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (
                status IN ('prepared', 'approved', 'rejected', 'disabled', 'running', 'succeeded', 'failed')
            ),
            project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
            project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
            source_import_id TEXT NOT NULL REFERENCES source_import_job(id) ON DELETE RESTRICT,
            formalization_receipt_id TEXT NOT NULL REFERENCES knowledge_formalization_receipt(id) ON DELETE RESTRICT,
            formalization_receipt_hash TEXT NOT NULL CHECK (length(formalization_receipt_hash) = 64),
            knowledge_fingerprint TEXT NOT NULL CHECK (length(knowledge_fingerprint) = 64),
            upstream_cards_hash TEXT NOT NULL CHECK (length(upstream_cards_hash) = 64),
            workflow_plan_id TEXT NOT NULL CHECK (length(workflow_plan_id) = length('workflow-plan-') + 32),
            workflow_plan_hash TEXT NOT NULL CHECK (length(workflow_plan_hash) = 64),
            checkpoint_id TEXT NOT NULL CHECK (length(checkpoint_id) = length('workflow-checkpoint-') + 32),
            checkpoint_hash TEXT NOT NULL CHECK (length(checkpoint_hash) = 64),
            skill_version_id TEXT NOT NULL REFERENCES skill_version(id) ON DELETE RESTRICT,
            skill_receipt_id TEXT,
            skill_receipt_hash TEXT CHECK (skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64),
            result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
            artifact_hash TEXT CHECK (artifact_hash IS NULL OR length(artifact_hash) = 64),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (project_binding_id, handoff_key, source_import_id, formalization_receipt_hash)
        ) STRICT;
        """)
        connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_skill_handoff_project_status
        ON skill_handoff_run(project_binding_id, status, updated_at DESC)
        """)
        connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_skill_handoff_source
        ON skill_handoff_run(source_import_id, updated_at DESC)
        """)
        connection.execute("""
        CREATE TABLE IF NOT EXISTS context_handoff_run (
            id TEXT PRIMARY KEY CHECK (
                length(id) = length('context-handoff-') + 32
                AND substr(id, 1, length('context-handoff-')) = 'context-handoff-'
                AND substr(id, length('context-handoff-') + 1) NOT GLOB '*[^0-9a-f]*'
            ),
            handoff_key TEXT NOT NULL CHECK (handoff_key = 'novel-kb-import-index-to-assemble-context'),
            project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (
                status IN ('prepared', 'approved', 'rejected', 'disabled', 'running', 'succeeded', 'failed')
            ),
            project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
            project_fingerprint TEXT NOT NULL CHECK (length(project_fingerprint) = 64),
            source_handoff_id TEXT NOT NULL REFERENCES skill_handoff_run(id) ON DELETE RESTRICT,
            distillation_index_hash TEXT NOT NULL CHECK (length(distillation_index_hash) = 64),
            workflow_plan_id TEXT NOT NULL CHECK (length(workflow_plan_id) = length('workflow-plan-') + 32),
            workflow_plan_hash TEXT NOT NULL CHECK (length(workflow_plan_hash) = 64),
            checkpoint_id TEXT NOT NULL CHECK (length(checkpoint_id) = length('workflow-checkpoint-') + 32),
            checkpoint_hash TEXT NOT NULL CHECK (length(checkpoint_hash) = 64),
            skill_version_id TEXT NOT NULL REFERENCES skill_version(id) ON DELETE RESTRICT,
            skill_receipt_id TEXT,
            skill_receipt_hash TEXT CHECK (skill_receipt_hash IS NULL OR length(skill_receipt_hash) = 64),
            result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
            artifact_hash TEXT CHECK (artifact_hash IS NULL OR length(artifact_hash) = 64),
            context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
            context_pack_hash TEXT CHECK (context_pack_hash IS NULL OR length(context_pack_hash) = 64),
            route_receipt_id TEXT REFERENCES nlcl_route_receipt(id) ON DELETE RESTRICT,
            route_receipt_hash TEXT CHECK (route_receipt_hash IS NULL OR length(route_receipt_hash) = 64),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (project_binding_id, handoff_key, source_handoff_id, distillation_index_hash),
            CHECK ((context_pack_id IS NULL) = (context_pack_hash IS NULL)),
            CHECK ((route_receipt_id IS NULL) = (route_receipt_hash IS NULL))
        ) STRICT;
        """)
        connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_context_handoff_project_status
        ON context_handoff_run(project_binding_id, status, updated_at DESC)
        """)
        connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_context_handoff_source
        ON context_handoff_run(source_handoff_id, updated_at DESC)
        """)

        candidate_columns = SqliteStore._table_columns(
            connection,
            "knowledge_candidate",
        )
        if "runtime_distillation_invocation_id" not in candidate_columns:
            connection.execute(
                """
                ALTER TABLE knowledge_candidate
                ADD COLUMN runtime_distillation_invocation_id TEXT
                    REFERENCES distillation_invocation(id) ON DELETE RESTRICT
                    CHECK (
                        runtime_distillation_invocation_id IS NULL
                        OR (
                            length(runtime_distillation_invocation_id)
                                = length('distillation-invocation-') + 32
                            AND substr(
                                runtime_distillation_invocation_id,
                                1,
                                length('distillation-invocation-')
                            ) = 'distillation-invocation-'
                            AND substr(
                                runtime_distillation_invocation_id,
                                length('distillation-invocation-') + 1
                            ) NOT GLOB '*[^0-9a-f]*'
                        )
                    )
                """
            )

        managed_indexes = {
            "idx_distillation_invocation_source_created": """
                CREATE INDEX idx_distillation_invocation_source_created
                ON distillation_invocation(
                    source_import_id, project_binding_id, created_at DESC
                )
            """,
            "idx_distillation_invocation_active_source_project": """
                CREATE UNIQUE INDEX
                    idx_distillation_invocation_active_source_project
                ON distillation_invocation(source_import_id, project_binding_id)
                WHERE status IN (
                    'prepared', 'running', 'output_persisted', 'interrupted'
                )
            """,
            "idx_distillation_invocation_succeeded_source_project": """
                CREATE UNIQUE INDEX
                    idx_distillation_invocation_succeeded_source_project
                ON distillation_invocation(source_import_id, project_binding_id)
                WHERE status = 'succeeded'
            """,
            "idx_distillation_receipt_prepared_singleton": """
                CREATE UNIQUE INDEX idx_distillation_receipt_prepared_singleton
                ON distillation_invocation_receipt(invocation_id)
                WHERE kind = 'prepared'
            """,
            "idx_distillation_receipt_dispatch_turn": """
                CREATE UNIQUE INDEX idx_distillation_receipt_dispatch_turn
                ON distillation_invocation_receipt(invocation_id, turn_ordinal)
                WHERE kind = 'provider_dispatch'
            """,
            "idx_distillation_receipt_result_turn": """
                CREATE UNIQUE INDEX idx_distillation_receipt_result_turn
                ON distillation_invocation_receipt(invocation_id, turn_ordinal)
                WHERE kind = 'provider_turn_result'
            """,
            "idx_distillation_receipt_candidate_singleton": """
                CREATE UNIQUE INDEX idx_distillation_receipt_candidate_singleton
                ON distillation_invocation_receipt(invocation_id)
                WHERE kind = 'candidate_output'
            """,
            "idx_distillation_receipt_dossier_singleton": """
                CREATE UNIQUE INDEX idx_distillation_receipt_dossier_singleton
                ON distillation_invocation_receipt(invocation_id)
                WHERE kind = 'recovery_dossier'
            """,
            "idx_distillation_receipt_decision_singleton": """
                CREATE UNIQUE INDEX idx_distillation_receipt_decision_singleton
                ON distillation_invocation_receipt(invocation_id)
                WHERE kind = 'recovery_decision'
            """,
            "idx_distillation_receipt_final_singleton": """
                CREATE UNIQUE INDEX idx_distillation_receipt_final_singleton
                ON distillation_invocation_receipt(invocation_id)
                WHERE kind = 'final_result'
            """,
            "idx_knowledge_candidate_distillation_card_type": """
                CREATE UNIQUE INDEX
                    idx_knowledge_candidate_distillation_card_type
                ON knowledge_candidate(
                    runtime_distillation_invocation_id, card_type
                )
                WHERE runtime_distillation_invocation_id IS NOT NULL
            """,
            "idx_knowledge_candidate_distillation_output_index": """
                CREATE UNIQUE INDEX
                    idx_knowledge_candidate_distillation_output_index
                ON knowledge_candidate(
                    runtime_distillation_invocation_id, source_card_index
                )
                WHERE runtime_distillation_invocation_id IS NOT NULL
            """,
        }
        for index_name, index_sql in managed_indexes.items():
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
            connection.execute(index_sql)

        trigger_names = (
            "trg_distillation_receipt_update_immutable",
            "trg_distillation_receipt_delete_immutable",
            "trg_distillation_receipt_insert_chain",
            "trg_distillation_receipt_kind_chain",
            "trg_distillation_invocation_insert_root",
            "trg_distillation_invocation_delete_immutable",
            "trg_distillation_invocation_identity_immutable",
            "trg_distillation_invocation_state_transition",
            "trg_distillation_invocation_receipt_binding",
            "trg_distillation_invocation_success_contract",
            "trg_knowledge_candidate_distillation_binding_immutable",
            "trg_knowledge_candidate_distillation_insert",
            "trg_knowledge_candidate_distillation_update",
        )
        for trigger_name in trigger_names:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

        triggers = (
            """
            CREATE TRIGGER trg_distillation_receipt_update_immutable
            BEFORE UPDATE ON distillation_invocation_receipt
            BEGIN
                SELECT RAISE(ABORT, 'distillation receipt is append-only');
            END
            """,
            """
            CREATE TRIGGER trg_distillation_receipt_delete_immutable
            BEFORE DELETE ON distillation_invocation_receipt
            BEGIN
                SELECT RAISE(ABORT, 'distillation receipt is append-only');
            END
            """,
            """
            CREATE TRIGGER trg_distillation_receipt_insert_chain
            BEFORE INSERT ON distillation_invocation_receipt
            BEGIN
                SELECT CASE
                    WHEN NEW.kind = 'prepared' AND NOT EXISTS (
                        SELECT 1
                        FROM distillation_invocation AS invocation
                        WHERE invocation.id = NEW.invocation_id
                          AND invocation.status = 'prepared'
                          AND invocation.state_revision = 1
                          AND invocation.last_receipt_ordinal = 1
                          AND invocation.last_receipt_id = NEW.id
                          AND invocation.last_receipt_hash = NEW.receipt_hash
                    )
                    THEN RAISE(
                        ABORT,
                        'prepared receipt does not bind the invocation root'
                    )
                END;
                SELECT CASE
                    WHEN NEW.kind != 'prepared' AND NOT EXISTS (
                        SELECT 1
                        FROM distillation_invocation AS invocation
                        JOIN distillation_invocation_receipt AS parent
                          ON parent.id = invocation.last_receipt_id
                        WHERE invocation.id = NEW.invocation_id
                          AND invocation.state_revision
                              = NEW.expected_invocation_revision
                          AND invocation.last_receipt_ordinal = NEW.ordinal - 1
                          AND invocation.last_receipt_id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.ordinal = NEW.ordinal - 1
                    )
                    THEN RAISE(
                        ABORT,
                        'distillation receipt CAS parent does not match head'
                    )
                END;
            END
            """,
            """
            CREATE TRIGGER trg_distillation_receipt_kind_chain
            BEFORE INSERT ON distillation_invocation_receipt
            BEGIN
                SELECT CASE
                    WHEN NEW.kind = 'provider_dispatch'
                     AND NEW.turn_ordinal = 1
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS parent
                        WHERE parent.id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.kind = 'prepared'
                     )
                    THEN RAISE(ABORT, 'first Provider dispatch must follow prepared')
                END;
                SELECT CASE
                    WHEN NEW.kind = 'provider_dispatch'
                     AND NEW.turn_ordinal > 1
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS parent
                        WHERE parent.id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.kind = 'provider_turn_result'
                          AND parent.turn_ordinal = NEW.turn_ordinal - 1
                     )
                    THEN RAISE(
                        ABORT,
                        'later Provider dispatch must follow the prior result'
                    )
                END;
                SELECT CASE
                    WHEN NEW.kind = 'provider_turn_result'
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS parent
                        WHERE parent.id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.kind = 'provider_dispatch'
                          AND parent.turn_ordinal = NEW.turn_ordinal
                     )
                    THEN RAISE(
                        ABORT,
                        'Provider result must follow its persisted dispatch'
                    )
                END;
                SELECT CASE
                    WHEN NEW.kind = 'candidate_output'
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS parent
                        WHERE parent.id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.kind = 'provider_turn_result'
                     )
                    THEN RAISE(
                        ABORT,
                        'candidate output must follow the final Provider result'
                    )
                END;
                SELECT CASE
                    WHEN NEW.kind = 'recovery_dossier'
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation AS invocation
                        WHERE invocation.id = NEW.invocation_id
                          AND invocation.last_dispatch_receipt_id IS NOT NULL
                     )
                    THEN RAISE(
                        ABORT,
                        'recovery dossier requires a persisted Provider dispatch'
                    )
                END;
                SELECT CASE
                    WHEN NEW.kind = 'recovery_decision'
                     AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS parent
                        WHERE parent.id = NEW.parent_receipt_id
                          AND parent.invocation_id = NEW.invocation_id
                          AND parent.kind = 'recovery_dossier'
                     )
                    THEN RAISE(
                        ABORT,
                        'recovery decision must follow its immutable dossier'
                    )
                END;
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_insert_root
            BEFORE INSERT ON distillation_invocation
            WHEN NEW.status != 'prepared'
              OR NEW.state_revision != 1
              OR NEW.last_receipt_ordinal != 1
              OR NEW.provider_replay_count != 0
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'distillation invocation must start at the prepared root'
                );
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_delete_immutable
            BEFORE DELETE ON distillation_invocation
            BEGIN
                SELECT RAISE(ABORT, 'distillation invocation cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_identity_immutable
            BEFORE UPDATE ON distillation_invocation
            WHEN NEW.id IS NOT OLD.id
              OR NEW.source_import_id IS NOT OLD.source_import_id
              OR NEW.source_receipt_id IS NOT OLD.source_receipt_id
              OR NEW.source_receipt_hash IS NOT OLD.source_receipt_hash
              OR NEW.source_packet_fingerprint IS NOT OLD.source_packet_fingerprint
              OR NEW.packet_text_hash IS NOT OLD.packet_text_hash
              OR NEW.project_binding_id IS NOT OLD.project_binding_id
              OR NEW.project_revision IS NOT OLD.project_revision
              OR NEW.project_fingerprint IS NOT OLD.project_fingerprint
              OR NEW.knowledge_scope_id IS NOT OLD.knowledge_scope_id
              OR NEW.knowledge_scope_state_revision
                    IS NOT OLD.knowledge_scope_state_revision
              OR NEW.knowledge_fingerprint IS NOT OLD.knowledge_fingerprint
              OR NEW.project_kb_state_id IS NOT OLD.project_kb_state_id
              OR NEW.project_kb_manifest_hash IS NOT OLD.project_kb_manifest_hash
              OR NEW.skill_id IS NOT OLD.skill_id
              OR NEW.skill_version_id IS NOT OLD.skill_version_id
              OR NEW.skill_version IS NOT OLD.skill_version
              OR NEW.skill_manifest_fingerprint IS NOT OLD.skill_manifest_fingerprint
              OR NEW.skill_package_fingerprint IS NOT OLD.skill_package_fingerprint
              OR NEW.activation_receipt_id IS NOT OLD.activation_receipt_id
              OR NEW.activation_receipt_hash IS NOT OLD.activation_receipt_hash
              OR NEW.activation_state_revision IS NOT OLD.activation_state_revision
              OR NEW.effective_scope IS NOT OLD.effective_scope
              OR NEW.instruction_materialization_id
                    IS NOT OLD.instruction_materialization_id
              OR NEW.instruction_materialization_hash
                    IS NOT OLD.instruction_materialization_hash
              OR NEW.managed_instruction_hash IS NOT OLD.managed_instruction_hash
              OR NEW.provider_executor_json IS NOT OLD.provider_executor_json
              OR NEW.provider_capabilities_json IS NOT OLD.provider_capabilities_json
              OR NEW.requested_model IS NOT OLD.requested_model
              OR NEW.accepted_reported_models_json
                    IS NOT OLD.accepted_reported_models_json
              OR NEW.dispatch_config_fingerprint
                    IS NOT OLD.dispatch_config_fingerprint
              OR NEW.credential_reference IS NOT OLD.credential_reference
              OR NEW.tool_definitions_hash IS NOT OLD.tool_definitions_hash
              OR NEW.anchor_catalog_id IS NOT OLD.anchor_catalog_id
              OR NEW.anchor_catalog_hash IS NOT OLD.anchor_catalog_hash
              OR NEW.retry_of_invocation_id IS NOT OLD.retry_of_invocation_id
              OR NEW.idempotency_key IS NOT OLD.idempotency_key
              OR NEW.request_hash IS NOT OLD.request_hash
              OR NEW.created_at IS NOT OLD.created_at
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'distillation invocation bindings are immutable'
                );
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_state_transition
            BEFORE UPDATE ON distillation_invocation
            WHEN NEW.state_revision != OLD.state_revision + 1
              OR NEW.last_receipt_ordinal != OLD.last_receipt_ordinal + 1
              OR OLD.status IN ('succeeded', 'failed', 'cancelled', 'abandoned')
              OR NOT (
                    (OLD.status = 'prepared'
                        AND NEW.status IN ('running', 'failed', 'cancelled'))
                 OR (OLD.status = 'running'
                        AND NEW.status IN (
                            'running', 'output_persisted', 'interrupted',
                            'failed', 'cancelled'
                        ))
                 OR (OLD.status = 'output_persisted'
                        AND NEW.status IN ('interrupted', 'succeeded'))
                 OR (OLD.status = 'interrupted'
                        AND NEW.status IN ('interrupted', 'succeeded', 'abandoned'))
              )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'invalid distillation invocation CAS transition'
                );
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_receipt_binding
            BEFORE UPDATE ON distillation_invocation
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1
                    FROM distillation_invocation_receipt AS receipt
                    WHERE receipt.id = NEW.last_receipt_id
                      AND receipt.invocation_id = NEW.id
                      AND receipt.ordinal = NEW.last_receipt_ordinal
                      AND receipt.receipt_hash = NEW.last_receipt_hash
                      AND receipt.expected_invocation_revision = OLD.state_revision
                      AND receipt.resulting_invocation_revision = NEW.state_revision
                      AND receipt.resulting_status = NEW.status
                      AND receipt.parent_receipt_id = OLD.last_receipt_id
                ) THEN RAISE(
                    ABORT,
                    'distillation head does not match its latest receipt'
                ) END;
                SELECT CASE
                    WHEN NEW.last_dispatch_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.last_dispatch_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'provider_dispatch'
                          AND receipt.turn_ordinal = NEW.last_dispatch_turn_ordinal
                          AND receipt.receipt_hash = NEW.last_dispatch_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid latest Provider dispatch binding')
                END;
                SELECT CASE
                    WHEN NEW.last_turn_result_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.last_turn_result_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'provider_turn_result'
                          AND receipt.turn_ordinal = NEW.provider_turn_count
                          AND receipt.receipt_hash = NEW.last_turn_result_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid latest Provider result binding')
                END;
                SELECT CASE
                    WHEN NEW.candidate_output_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.candidate_output_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'candidate_output'
                          AND receipt.receipt_hash = NEW.candidate_output_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid candidate output receipt binding')
                END;
                SELECT CASE
                    WHEN NEW.recovery_dossier_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.recovery_dossier_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'recovery_dossier'
                          AND receipt.receipt_hash = NEW.recovery_dossier_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid recovery dossier receipt binding')
                END;
                SELECT CASE
                    WHEN NEW.recovery_decision_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.recovery_decision_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'recovery_decision'
                          AND receipt.receipt_hash = NEW.recovery_decision_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid recovery decision receipt binding')
                END;
                SELECT CASE
                    WHEN NEW.final_result_receipt_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM distillation_invocation_receipt AS receipt
                        WHERE receipt.id = NEW.final_result_receipt_id
                          AND receipt.invocation_id = NEW.id
                          AND receipt.kind = 'final_result'
                          AND receipt.receipt_hash = NEW.final_result_receipt_hash
                    )
                    THEN RAISE(ABORT, 'invalid final result receipt binding')
                END;
            END
            """,
            """
            CREATE TRIGGER trg_distillation_invocation_success_contract
            BEFORE UPDATE ON distillation_invocation
            WHEN NEW.status = 'succeeded'
            BEGIN
                SELECT CASE WHEN (
                    SELECT COUNT(*)
                    FROM distillation_invocation_receipt
                    WHERE invocation_id = NEW.id
                      AND kind = 'provider_turn_result'
                ) < 1 THEN RAISE(
                    ABORT,
                    'successful distillation requires Provider result evidence'
                ) END;
                SELECT CASE WHEN (
                    SELECT COUNT(*)
                    FROM knowledge_candidate
                    WHERE runtime_distillation_invocation_id = NEW.id
                      AND status = 'pending'
                ) != 3 THEN RAISE(
                    ABORT,
                    'successful distillation requires exactly three pending candidates'
                ) END;
                SELECT CASE WHEN EXISTS (
                    SELECT 1
                    FROM (
                        SELECT 0 AS output_index, 'work' AS card_type
                        UNION ALL SELECT 1, 'voice'
                        UNION ALL SELECT 2, 'validator'
                    ) AS expected
                    LEFT JOIN knowledge_candidate AS candidate
                      ON candidate.runtime_distillation_invocation_id = NEW.id
                     AND candidate.source_card_index = expected.output_index
                     AND candidate.card_type = expected.card_type
                    WHERE candidate.id IS NULL
                ) THEN RAISE(
                    ABORT,
                    'distillation candidates violate canonical work/voice/validator order'
                ) END;
            END
            """,
            """
            CREATE TRIGGER trg_knowledge_candidate_distillation_binding_immutable
            BEFORE UPDATE OF runtime_distillation_invocation_id ON knowledge_candidate
            WHEN OLD.runtime_distillation_invocation_id IS NOT NULL
             AND NEW.runtime_distillation_invocation_id
                    IS NOT OLD.runtime_distillation_invocation_id
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'knowledge candidate distillation binding is immutable'
                );
            END
            """,
            """
            CREATE TRIGGER trg_knowledge_candidate_distillation_insert
            BEFORE INSERT ON knowledge_candidate
            WHEN NEW.runtime_distillation_invocation_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1
                FROM distillation_invocation AS invocation
                WHERE invocation.id = NEW.runtime_distillation_invocation_id
                  AND invocation.source_import_id = NEW.source_import_id
                  AND NEW.source_card_index BETWEEN 0 AND 2
                  AND NEW.card_type = CASE NEW.source_card_index
                        WHEN 0 THEN 'work'
                        WHEN 1 THEN 'voice'
                        WHEN 2 THEN 'validator'
                      END
             )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'invalid knowledge candidate distillation binding'
                );
            END
            """,
            """
            CREATE TRIGGER trg_knowledge_candidate_distillation_update
            BEFORE UPDATE OF source_import_id, source_card_index, card_type,
                runtime_distillation_invocation_id
            ON knowledge_candidate
            WHEN NEW.runtime_distillation_invocation_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1
                FROM distillation_invocation AS invocation
                WHERE invocation.id = NEW.runtime_distillation_invocation_id
                  AND invocation.source_import_id = NEW.source_import_id
                  AND NEW.source_card_index BETWEEN 0 AND 2
                  AND NEW.card_type = CASE NEW.source_card_index
                        WHEN 0 THEN 'work'
                        WHEN 1 THEN 'voice'
                        WHEN 2 THEN 'validator'
                      END
             )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'invalid knowledge candidate distillation binding'
                );
            END
            """,
        )
        for trigger_sql in triggers:
            connection.execute(trigger_sql)

    @staticmethod
    def _migrate_source_import_jobs_v19(connection: sqlite3.Connection) -> None:
        """Add an explicit local-file provider without rewriting job payloads."""

        table_record = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("source_import_job",),
        ).fetchone()
        if table_record is None or table_record["sql"] is None:
            raise RuntimeError("source import job table definition is unavailable")
        columns = SqliteStore._table_columns(connection, "source_import_job")
        required_legacy = {
            "id",
            "provider",
            "query",
            "book_id",
            "status",
            "payload_json",
            "created_at",
            "updated_at",
        }
        if not required_legacy.issubset(columns):
            raise RuntimeError("source import job table cannot be migrated to schema 19")

        table_sql = str(table_record["sql"])
        if "source_key" in columns and "local_file" in table_sql:
            connection.execute(
                """
                UPDATE source_import_job
                SET source_key = COALESCE(NULLIF(book_id, ''), id)
                WHERE source_key = ''
                """
            )
            return

        connection.execute("DROP INDEX IF EXISTS idx_source_import_status")
        connection.execute(
            """
            CREATE TABLE source_import_job_v19 (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK (provider IN ('tomato', 'local_file', 'feedback_experience')),
                query TEXT NOT NULL,
                book_id TEXT,
                source_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'downloading', 'source_ready', 'drafting',
                        'awaiting_review', 'formalizing', 'completed',
                        'cancel_requested', 'cancelled', 'failed', 'interrupted'
                    )
                ),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO source_import_job_v19(
                id, provider, query, book_id, source_key, status,
                payload_json, created_at, updated_at
            )
            SELECT id, provider, query, book_id,
                   COALESCE(NULLIF(book_id, ''), id),
                   status, payload_json, created_at, updated_at
            FROM source_import_job
            """
        )
        connection.execute("DROP TABLE source_import_job")
        connection.execute("ALTER TABLE source_import_job_v19 RENAME TO source_import_job")
        connection.execute(
            "CREATE INDEX idx_source_import_status "
            "ON source_import_job(status, updated_at DESC)"
        )

    @staticmethod

    @staticmethod
    def _migrate_source_import_feedback_provider_v23(
        connection: sqlite3.Connection,
    ) -> None:
        """Allow feedback_experience synthetic source imports for formal-knowledge materialize."""

        def rebuild(table_name: str, create_sql: str, copy_sql: str) -> None:
            temp_name = f"{table_name}_v23"
            connection.execute(f"DROP TABLE IF EXISTS {temp_name}")
            connection.execute(create_sql)
            connection.execute(copy_sql)
            connection.execute(f"DROP TABLE {table_name}")
            connection.execute(f"ALTER TABLE {temp_name} RENAME TO {table_name}")

        job_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("source_import_job",),
        ).fetchone()
        if job_sql is None or job_sql["sql"] is None:
            raise RuntimeError("source_import_job table definition is unavailable")
        if "feedback_experience" not in str(job_sql["sql"]):
            rebuild(
                "source_import_job",
                """
                CREATE TABLE source_import_job_v23 (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK (
                        provider IN ('tomato', 'local_file', 'feedback_experience')
                    ),
                    query TEXT NOT NULL,
                    book_id TEXT,
                    source_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'queued', 'downloading', 'source_ready', 'drafting',
                            'awaiting_review', 'formalizing', 'completed',
                            'cancel_requested', 'cancelled', 'failed', 'interrupted'
                        )
                    ),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                ) STRICT
                """,
                """
                INSERT INTO source_import_job_v23(
                    id, provider, query, book_id, source_key, status,
                    payload_json, created_at, updated_at
                )
                SELECT id, provider, query, book_id, source_key, status,
                       payload_json, created_at, updated_at
                FROM source_import_job
                """,
            )
            connection.execute("DROP INDEX IF EXISTS idx_source_import_status")
            connection.execute(
                "CREATE INDEX idx_source_import_status "
                "ON source_import_job(status, updated_at DESC)"
            )

        attempt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("source_import_attempt_receipt",),
        ).fetchone()
        if attempt_sql is not None and attempt_sql["sql"] is not None:
            if "feedback_experience" not in str(attempt_sql["sql"]):
                # Preserve attempt receipts while expanding provider check.
                columns = [
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM pragma_table_info('source_import_attempt_receipt') ORDER BY cid"
                    ).fetchall()
                ]
                col_list = ", ".join(columns)
                rebuild(
                    "source_import_attempt_receipt",
                    """
                    CREATE TABLE source_import_attempt_receipt_v23 (
                        id TEXT PRIMARY KEY,
                        source_import_id TEXT NOT NULL
                            REFERENCES source_import_job(id) ON DELETE RESTRICT,
                        attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
                        status TEXT NOT NULL CHECK (
                            status IN ('success', 'failed', 'cancelled', 'interrupted')
                        ),
                        stage TEXT NOT NULL,
                        provider TEXT NOT NULL CHECK (
                            provider IN ('tomato', 'local_file', 'feedback_experience')
                        ),
                        request_hash TEXT CHECK (
                            request_hash IS NULL OR length(request_hash) = 64
                        ),
                        execution_receipt_id TEXT
                            REFERENCES source_import_execution_receipt(id) ON DELETE RESTRICT,
                        execution_receipt_hash TEXT CHECK (
                            execution_receipt_hash IS NULL
                            OR length(execution_receipt_hash) = 64
                        ),
                        failure_signature TEXT CHECK (
                            failure_signature IS NULL OR length(failure_signature) = 64
                        ),
                        cleanup_status TEXT NOT NULL CHECK (
                            cleanup_status IN ('clean', 'partial', 'not_required')
                        ),
                        cleanup_hash TEXT NOT NULL CHECK (length(cleanup_hash) = 64),
                        receipt_hash TEXT NOT NULL UNIQUE CHECK (length(receipt_hash) = 64),
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (source_import_id, attempt_ordinal)
                    ) STRICT
                    """,
                    f"""
                    INSERT INTO source_import_attempt_receipt_v23({col_list})
                    SELECT {col_list}
                    FROM source_import_attempt_receipt
                    """,
                )

    def _migrate_post_draft_gate_receipts_v16(

        connection: sqlite3.Connection,
    ) -> None:
        """Repair known v16 drift while refusing to erase unknown constraints."""

        table_name = "post_draft_gate_receipt"
        table_record = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if table_record is None or table_record["sql"] is None:
            raise RuntimeError("post-draft Gate receipt table definition is unavailable")
        table_sql = str(table_record["sql"])

        column_rows = connection.execute(
            "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
            (table_name,),
        ).fetchall()
        column_names = tuple(str(row["name"]) for row in column_rows)
        canonical_columns = POST_DRAFT_GATE_RECEIPT_COLUMNS
        legacy_columns = POST_DRAFT_GATE_RECEIPT_PAYLOAD_COLUMNS
        if column_names not in {canonical_columns, legacy_columns}:
            missing = sorted(set(legacy_columns) - set(column_names))
            extra = sorted(set(column_names) - set(canonical_columns))
            details: list[str] = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            if not details:
                details.append("column order is not a known v16 layout")
            raise RuntimeError(
                "post-draft Gate receipt table has an unknown column layout: "
                + "; ".join(details)
            )

        requires_rebuild = column_names == legacy_columns
        integer_columns = {
            "attempt_ordinal",
            "review_revision",
            "base_revision",
            "candidate_file_count",
        }
        nullable_columns = {
            "context_pack_id",
            "context_pack_hash",
            "knowledge_fingerprint",
            "skill_receipt_id",
            "skill_receipt_hash",
        }
        for row in column_rows:
            name = str(row["name"])
            expected_type = "INTEGER" if name in integer_columns else "TEXT"
            expected_not_null = name not in nullable_columns
            expected_primary_key = name == "id"
            if int(row["hidden"]) != 0 or row["dflt_value"] is not None:
                raise RuntimeError(
                    "post-draft Gate receipt table has an unknown generated/default column: "
                    + name
                )
            if bool(row["notnull"]) and not expected_not_null:
                raise RuntimeError(
                    "post-draft Gate receipt table has an unknown NOT NULL constraint: "
                    + name
                )
            if bool(row["pk"]) and not expected_primary_key:
                raise RuntimeError(
                    "post-draft Gate receipt table has an unknown primary-key constraint: "
                    + name
                )
            if (
                str(row["type"]).upper() != expected_type
                or bool(row["notnull"]) != expected_not_null
                or bool(row["pk"]) != expected_primary_key
            ):
                requires_rebuild = True

        compact_table_sql = _compact_schema_sql(table_sql)
        if "withoutrowid" in compact_table_sql:
            raise RuntimeError(
                "post-draft Gate receipt table has an unsupported WITHOUT ROWID constraint"
            )
        if re.search(
            r"\b(COLLATE|AUTOINCREMENT|DEFERRABLE)\b|\bON\s+CONFLICT\b",
            table_sql,
            flags=re.IGNORECASE,
        ):
            raise RuntimeError(
                "post-draft Gate receipt table has an unknown DDL constraint"
            )
        if not compact_table_sql.endswith(")strict"):
            requires_rebuild = True

        expected_checks = _check_expressions(
            _post_draft_gate_receipt_table_sql(table_name)
        )
        observed_checks = _check_expressions(table_sql)
        unknown_checks = observed_checks - expected_checks
        if unknown_checks:
            raise RuntimeError(
                "post-draft Gate receipt table has unknown CHECK constraints"
            )
        if observed_checks != expected_checks:
            requires_rebuild = True

        expected_foreign_keys = {
            "change_set_id": ("change_set", "id", "NO ACTION", "CASCADE", "NONE"),
            "project_binding_id": (
                "project_binding",
                "id",
                "NO ACTION",
                "RESTRICT",
                "NONE",
            ),
            "context_pack_id": (
                "context_pack",
                "id",
                "NO ACTION",
                "RESTRICT",
                "NONE",
            ),
        }
        observed_foreign_keys: dict[str, tuple[str, str, str, str, str]] = {}
        for row in connection.execute(
            "SELECT * FROM pragma_foreign_key_list(?)",
            (table_name,),
        ).fetchall():
            source = str(row["from"])
            if source in observed_foreign_keys or source not in expected_foreign_keys:
                raise RuntimeError(
                    "post-draft Gate receipt table has an unknown foreign-key constraint"
                )
            observed_foreign_keys[source] = (
                str(row["table"]),
                str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
                str(row["match"]),
            )
        if observed_foreign_keys != expected_foreign_keys:
            requires_rebuild = True

        index_rows = connection.execute(
            "SELECT * FROM pragma_index_list(?) ORDER BY seq",
            (table_name,),
        ).fetchall()
        managed_indexes = set(POST_DRAFT_GATE_MANAGED_INDEX_SQL)
        preserved_index_sql: list[str] = []
        autoindex_counts: Counter[tuple[str, tuple[tuple[str, int, str], ...]]] = (
            Counter()
        )
        legacy_identity_signature = (
            "u",
            tuple((name, 0, "BINARY") for name in POST_DRAFT_GATE_IDENTITY_COLUMNS),
        )
        primary_key_signature = ("pk", (("id", 0, "BINARY"),))
        receipt_hash_signature = ("u", (("receipt_hash", 0, "BINARY"),))
        attempt_signature = (
            "u",
            (("change_set_id", 0, "BINARY"), ("attempt_ordinal", 0, "BINARY")),
        )
        allowed_autoindexes = Counter(
            {
                primary_key_signature: 1,
                receipt_hash_signature: 1,
                attempt_signature: 1,
                legacy_identity_signature: 1,
            }
        )
        for index_row in index_rows:
            index_name = str(index_row["name"])
            origin = str(index_row["origin"])
            partial = bool(index_row["partial"])
            key_columns = tuple(
                (
                    str(row["name"]),
                    int(row["desc"]),
                    str(row["coll"]),
                )
                for row in connection.execute(
                    "SELECT * FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno",
                    (index_name,),
                ).fetchall()
            )
            if origin != "c":
                if partial:
                    raise RuntimeError(
                        "post-draft Gate receipt table has an unknown partial autoindex"
                    )
                signature = (origin, key_columns)
                autoindex_counts[signature] += 1
                if autoindex_counts[signature] > allowed_autoindexes[signature]:
                    raise RuntimeError(
                        "post-draft Gate receipt table has an unknown automatic constraint"
                    )
                continue

            if index_name in managed_indexes:
                continue
            if (
                bool(index_row["unique"])
                and not partial
                and tuple(name for name, _, _ in key_columns)
                == POST_DRAFT_GATE_IDENTITY_COLUMNS
            ):
                raise RuntimeError(
                    "post-draft Gate receipt table has an unknown explicit terminal constraint"
                )
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            if definition is None or definition["sql"] is None:
                raise RuntimeError(
                    "post-draft Gate receipt index definition is unavailable: "
                    + index_name
                )
            preserved_index_sql.append(str(definition["sql"]))

        unknown_autoindexes = autoindex_counts - allowed_autoindexes
        if unknown_autoindexes:
            raise RuntimeError(
                "post-draft Gate receipt table has unknown automatic constraints"
            )
        if (
            autoindex_counts[primary_key_signature] != 1
            or autoindex_counts[receipt_hash_signature] != 1
            or autoindex_counts[attempt_signature] != 1
            or autoindex_counts[legacy_identity_signature] == 1
        ):
            requires_rebuild = True

        trigger_rows = connection.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'trigger' AND tbl_name = ?
            ORDER BY name
            """,
            (table_name,),
        ).fetchall()
        preserved_trigger_sql: list[str] = []
        for row in trigger_rows:
            if row["sql"] is None:
                raise RuntimeError(
                    "post-draft Gate receipt trigger definition is unavailable: "
                    + str(row["name"])
                )
            preserved_trigger_sql.append(str(row["sql"]))

        if requires_rebuild:
            try:
                rows = connection.execute(
                    "SELECT rowid AS migration_rowid, * "
                    "FROM post_draft_gate_receipt ORDER BY rowid"
                ).fetchall()
            except sqlite3.Error as exc:
                raise RuntimeError(
                    "post-draft Gate receipt rows have no recoverable insertion order"
                ) from exc
            for row in rows:
                SqliteStore._post_draft_gate_receipt_payload_from_row(row)

            next_ordinal: dict[str, int] = {}
            used_ordinals: set[tuple[str, int]] = set()
            migrated_rows: list[tuple[object, ...]] = []
            has_attempt_ordinal = "attempt_ordinal" in column_names
            for row in rows:
                change_set_id = str(row["change_set_id"])
                if has_attempt_ordinal:
                    raw_ordinal = row["attempt_ordinal"]
                    if (
                        isinstance(raw_ordinal, bool)
                        or not isinstance(raw_ordinal, int)
                        or raw_ordinal < 1
                    ):
                        raise RuntimeError(
                            "post-draft Gate receipt has an invalid attempt ordinal"
                        )
                    attempt_ordinal = raw_ordinal
                else:
                    attempt_ordinal = next_ordinal.get(change_set_id, 0) + 1
                    next_ordinal[change_set_id] = attempt_ordinal
                ordinal_identity = (change_set_id, attempt_ordinal)
                if ordinal_identity in used_ordinals:
                    raise RuntimeError(
                        "post-draft Gate receipt attempt ordinal is duplicated"
                    )
                used_ordinals.add(ordinal_identity)
                migrated_rows.append(
                    tuple(
                        attempt_ordinal if name == "attempt_ordinal" else row[name]
                        for name in POST_DRAFT_GATE_RECEIPT_COLUMNS
                    )
                )

            rebuild_table = "post_draft_gate_receipt_v16_rebuild"
            if SqliteStore._table_columns(connection, rebuild_table):
                raise RuntimeError(
                    "post-draft Gate v16 rebuild table already exists"
                )
            connection.execute(_post_draft_gate_receipt_table_sql(rebuild_table))
            column_list = ", ".join(POST_DRAFT_GATE_RECEIPT_COLUMNS)
            placeholders = ", ".join("?" for _ in POST_DRAFT_GATE_RECEIPT_COLUMNS)
            connection.executemany(
                f"INSERT INTO {rebuild_table} ({column_list}) VALUES ({placeholders})",
                migrated_rows,
            )
            copied_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {rebuild_table}"
                ).fetchone()[0]
            )
            if copied_count != len(rows):
                raise RuntimeError(
                    "post-draft Gate v16 rebuild did not preserve every receipt"
                )
            for row in connection.execute(
                f"SELECT * FROM {rebuild_table} ORDER BY change_set_id, attempt_ordinal"
            ).fetchall():
                SqliteStore._post_draft_gate_receipt_payload_from_row(row)

            connection.execute("DROP TABLE post_draft_gate_receipt")
            connection.execute(
                f"ALTER TABLE {rebuild_table} RENAME TO post_draft_gate_receipt"
            )
            for definition in preserved_index_sql:
                connection.execute(definition)
            for definition in preserved_trigger_sql:
                connection.execute(definition)

        for index_name, definition in POST_DRAFT_GATE_MANAGED_INDEX_SQL.items():
            current = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            if current is not None and (
                current["sql"] is None
                or _compact_schema_sql(str(current["sql"]))
                != _compact_schema_sql(definition)
            ):
                connection.execute(f"DROP INDEX {index_name}")
                current = None
            if current is None:
                connection.execute(definition)

    @staticmethod
    def _validate_source_import_receipts_v12(
        connection: sqlite3.Connection,
    ) -> None:
        """Validate experimental rows but never invent receipts for legacy jobs."""

        from pydantic import ValidationError

        from zhimeng_runtime.source_import_receipt_models import (
            SourceImportExecutionReceipt,
        )

        rows = connection.execute(
            """
            SELECT receipt.*
            FROM source_import_execution_receipt AS receipt
            ORDER BY receipt.created_at, receipt.id
            """
        ).fetchall()
        for row in rows:
            try:
                receipt = SourceImportExecutionReceipt.model_validate_json(
                    str(row["payload_json"])
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise RuntimeError(
                    f"cannot validate source import receipt {row['id']} during v12 migration"
                ) from exc
            if (
                receipt.receipt_id != str(row["id"])
                or receipt.source_import_id != str(row["source_import_id"])
                or receipt.receipt_hash != str(row["receipt_hash"])
            ):
                raise RuntimeError(
                    f"source import receipt {row['id']} columns do not match its payload"
                )

    @staticmethod
    def _migrate_source_import_receipts_v13(
        connection: sqlite3.Connection,
    ) -> None:
        """Add execution identity without upgrading older receipts to verified."""

        columns = SqliteStore._table_columns(
            connection,
            "source_import_execution_receipt",
        )
        additions = {
            "run_id": "TEXT",
            "revision": "INTEGER CHECK (revision IS NULL OR revision >= 1)",
            "idempotency_key": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE source_import_execution_receipt "
                    f"ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_source_import_receipt_run "
            "ON source_import_execution_receipt(run_id) WHERE run_id IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_source_import_receipt_idempotency "
            "ON source_import_execution_receipt(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )

        from pydantic import ValidationError

        from zhimeng_runtime.source_import_receipt_models import (
            SourceImportExecutionReceipt,
        )

        rows = connection.execute(
            "SELECT * FROM source_import_execution_receipt ORDER BY created_at, id"
        ).fetchall()
        for row in rows:
            identity = (row["run_id"], row["revision"], row["idempotency_key"])
            if all(value is None for value in identity):
                continue
            if any(value is None for value in identity):
                raise RuntimeError(
                    f"source import receipt {row['id']} has partial execution identity"
                )
            try:
                receipt = SourceImportExecutionReceipt.model_validate_json(
                    str(row["payload_json"])
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise RuntimeError(
                    f"cannot validate source import receipt {row['id']} during v13 migration"
                ) from exc
            expected = {
                "receipt_id": row["id"],
                "source_import_id": row["source_import_id"],
                "run_id": row["run_id"],
                "revision": row["revision"],
                "idempotency_key": row["idempotency_key"],
                "request_hash": row["request_hash"],
                "receipt_hash": row["receipt_hash"],
            }
            if any(
                str(receipt.model_dump(mode="json").get(field)) != str(value)
                for field, value in expected.items()
            ):
                raise RuntimeError(
                    f"source import receipt {row['id']} columns do not match its payload"
                )

    @staticmethod
    def _migrate_knowledge_versions_v14(connection: sqlite3.Connection) -> None:
        """Allow a new lineage to reuse content from an abandoned staged version."""

        connection.execute("DROP INDEX IF EXISTS idx_knowledge_version_card_number")
        connection.execute("DROP INDEX IF EXISTS idx_knowledge_version_card_content")
        connection.execute(
            """
            CREATE TABLE knowledge_card_version_v14 (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL
                    REFERENCES knowledge_card(card_id) ON DELETE RESTRICT,
                version_number INTEGER NOT NULL CHECK (version_number >= 1),
                content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
                payload_json TEXT NOT NULL,
                source_candidate_id TEXT NOT NULL
                    REFERENCES knowledge_candidate(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE (card_id, version_number)
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_card_version_v14(
                id, card_id, version_number, content_hash,
                payload_json, source_candidate_id, created_at
            )
            SELECT id, card_id, version_number, content_hash,
                   payload_json, source_candidate_id, created_at
            FROM knowledge_card_version
            """
        )
        connection.execute("DROP TABLE knowledge_card_version")
        connection.execute(
            "ALTER TABLE knowledge_card_version_v14 RENAME TO knowledge_card_version"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_version_card_number "
            "ON knowledge_card_version(card_id, version_number DESC)"
        )
        connection.execute(
            "CREATE INDEX idx_knowledge_version_card_content "
            "ON knowledge_card_version(card_id, content_hash)"
        )

    @staticmethod
    def _migrate_knowledge_scopes_v15(connection: sqlite3.Connection) -> None:
        """Create the application mapping without rewriting immutable v1 receipts."""

        for table_name in (
            "knowledge_version_lineage_receipt",
            "knowledge_formalization_receipt",
        ):
            columns = SqliteStore._table_columns(connection, table_name)
            if "scope_id" not in columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN scope_id TEXT "
                    "REFERENCES knowledge_scope(id) ON DELETE RESTRICT"
                )

        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_scope(
                id, scope_type, project_binding_id, state_revision,
                created_at, updated_at
            ) VALUES (?, 'application', NULL, 1, ?, ?)
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID, now, now),
        )
        application = connection.execute(
            "SELECT * FROM knowledge_scope WHERE scope_type = 'application'"
        ).fetchone()
        if (
            application is None
            or str(application["id"]) != APPLICATION_KNOWLEDGE_SCOPE_ID
            or application["project_binding_id"] is not None
        ):
            raise RuntimeError("knowledge application scope singleton is invalid")

        project_rows = connection.execute(
            "SELECT id FROM project_binding ORDER BY id"
        ).fetchall()
        for project in project_rows:
            project_id = str(project["id"])
            deterministic_id = knowledge_scope_id(project_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_scope(
                    id, scope_type, project_binding_id, state_revision,
                    created_at, updated_at
                ) VALUES (?, 'project', ?, 1, ?, ?)
                """,
                (deterministic_id, project_id, now, now),
            )
            project_scope = connection.execute(
                "SELECT * FROM knowledge_scope WHERE id = ?",
                (deterministic_id,),
            ).fetchone()
            if (
                project_scope is None
                or str(project_scope["scope_type"]) != "project"
                or str(project_scope["project_binding_id"]) != project_id
            ):
                raise RuntimeError(
                    f"project knowledge scope {deterministic_id} is not deterministic"
                )

        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_source_scope(
                source_import_id, scope_id, bound_at
            )
            SELECT id, ?, created_at FROM source_import_job
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        )
        conflicting_source = connection.execute(
            """
            SELECT source_import_id
            FROM knowledge_source_scope
            WHERE scope_id != ?
            LIMIT 1
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        ).fetchone()
        if conflicting_source is not None:
            raise RuntimeError(
                "legacy knowledge source is bound outside the application scope"
            )

        missing_active = connection.execute(
            "SELECT card_id FROM knowledge_card WHERE active_version_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_active is not None:
            raise RuntimeError(
                "legacy knowledge card cannot be scoped without an active version"
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_scope_card(
                scope_id, card_id, status, state_revision, active_version_id,
                created_at, updated_at
            )
            SELECT ?, card_id, status, state_revision, active_version_id,
                   created_at, updated_at
            FROM knowledge_card
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        )
        mismatched_card = connection.execute(
            """
            SELECT card.card_id
            FROM knowledge_card AS card
            LEFT JOIN knowledge_scope_card AS scoped
              ON scoped.scope_id = ? AND scoped.card_id = card.card_id
            WHERE scoped.card_id IS NULL
               OR scoped.status != card.status
               OR scoped.state_revision != card.state_revision
               OR scoped.active_version_id != card.active_version_id
            LIMIT 1
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        ).fetchone()
        if mismatched_card is not None:
            raise RuntimeError("legacy knowledge card application mapping is invalid")

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_lineage_scope "
            "ON knowledge_version_lineage_receipt(scope_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_formalization_scope "
            "ON knowledge_formalization_receipt(scope_id, created_at DESC)"
        )

    @staticmethod
    def _migrate_knowledge_lineage_v12(
        connection: sqlite3.Connection,
    ) -> None:
        """Mark existing knowledge as unverified without inventing provenance."""

        columns = SqliteStore._table_columns(connection, "knowledge_candidate")
        additions = {
            "lineage_status": (
                "TEXT NOT NULL DEFAULT 'legacy_unverified' "
                "CHECK (lineage_status IN ('verified', 'legacy_unverified'))"
            ),
            "origin_json": "TEXT",
            "origin_hash": "TEXT CHECK (origin_hash IS NULL OR length(origin_hash) = 64)",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE knowledge_candidate ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """
            UPDATE knowledge_candidate
            SET lineage_status = 'legacy_unverified',
                origin_json = NULL,
                origin_hash = NULL
            WHERE lineage_status != 'verified'
               OR origin_json IS NULL
               OR origin_hash IS NULL
            """
        )

    @staticmethod
    def _backfill_nlcl_route_receipts_v9(connection: sqlite3.Connection) -> None:
        from pydantic import ValidationError

        from zhimeng_runtime.context_models import ContextPack
        from zhimeng_runtime.nlcl_models import (
            NlclRouteSource,
            build_nlcl_route_receipt,
        )

        rows = connection.execute(
            """
            SELECT cp.* FROM context_pack AS cp
            LEFT JOIN nlcl_route_receipt AS route
                ON route.context_pack_id = cp.id
            WHERE route.context_pack_id IS NULL
            ORDER BY cp.created_at, cp.id
            """
        ).fetchall()
        for row in rows:
            try:
                pack = ContextPack.model_validate_json(str(row["payload_json"]))
                receipt = build_nlcl_route_receipt(
                    pack,
                    source=NlclRouteSource.UNAVAILABLE,
                    extra_warnings=("migrated_context_pack_route_unavailable",),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise RuntimeError(
                    f"cannot backfill NLCL route for Context Pack {row['id']}"
                ) from exc
            payload_json = json.dumps(
                receipt.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            columns = SqliteStore._table_columns(connection, "nlcl_route_receipt")
            binding_columns = {
                "project_kb_state_id",
                "project_kb_manifest_hash",
            }
            if binding_columns.issubset(columns):
                connection.execute(
                    """
                    INSERT INTO nlcl_route_receipt(
                        id, context_pack_id, project_binding_id, context_pack_hash,
                        project_revision, project_fingerprint, project_kb_state_id,
                        project_kb_manifest_hash, active_chapter_id, profile,
                        objective_hash, knowledge_fingerprint, source, route_status,
                        receipt_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.route_receipt_id,
                        receipt.context_pack_id,
                        receipt.project_id,
                        receipt.context_pack_hash,
                        receipt.project_revision,
                        receipt.project_fingerprint,
                        receipt.project_kb_state_id,
                        receipt.project_kb_manifest_hash,
                        receipt.active_chapter_id,
                        receipt.profile,
                        receipt.objective_hash,
                        receipt.knowledge_fingerprint,
                        receipt.source.value,
                        receipt.status.value,
                        receipt.receipt_hash,
                        payload_json,
                        receipt.created_at.isoformat(),
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO nlcl_route_receipt(
                        id, context_pack_id, project_binding_id, context_pack_hash,
                        project_revision, project_fingerprint, active_chapter_id,
                        profile, objective_hash, knowledge_fingerprint, source,
                        route_status, receipt_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.route_receipt_id,
                        receipt.context_pack_id,
                        receipt.project_id,
                        receipt.context_pack_hash,
                        receipt.project_revision,
                        receipt.project_fingerprint,
                        receipt.active_chapter_id,
                        receipt.profile,
                        receipt.objective_hash,
                        receipt.knowledge_fingerprint,
                        receipt.source.value,
                        receipt.status.value,
                        receipt.receipt_hash,
                        payload_json,
                        receipt.created_at.isoformat(),
                    ),
                )

    @staticmethod
    def _backfill_prewrite_operation_packages_v11(
        connection: sqlite3.Connection,
    ) -> None:
        from pydantic import ValidationError

        from zhimeng_runtime.context_models import ContextPack
        from zhimeng_runtime.nlcl_models import NlclRouteReceipt
        from zhimeng_runtime.prewrite_models import build_prewrite_operation_package

        rows = connection.execute(
            """
            SELECT
                cp.id AS context_pack_id,
                cp.payload_json AS context_payload_json,
                route.payload_json AS route_payload_json
            FROM context_pack AS cp
            JOIN nlcl_route_receipt AS route ON route.context_pack_id = cp.id
            LEFT JOIN prewrite_operation_package AS package
                ON package.context_pack_id = cp.id
            WHERE package.context_pack_id IS NULL
            ORDER BY cp.created_at, cp.id
            """
        ).fetchall()
        for row in rows:
            try:
                pack = ContextPack.model_validate_json(
                    str(row["context_payload_json"])
                )
                route = NlclRouteReceipt.model_validate_json(
                    str(row["route_payload_json"])
                )
                package = build_prewrite_operation_package(
                    pack,
                    route,
                    extra_missing_requirements=(
                        "migrated_prewrite_package_requires_recompile",
                    ),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise RuntimeError(
                    "cannot backfill prewrite operation package for Context Pack "
                    f"{row['context_pack_id']}"
                ) from exc
            payload_json = json.dumps(
                package.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            connection.execute(
                """
                INSERT INTO prewrite_operation_package(
                    id, context_pack_id, route_receipt_id, project_binding_id,
                    context_pack_hash, route_receipt_hash, project_revision,
                    project_fingerprint, active_chapter_id, readiness,
                    package_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package.operation_package_id,
                    package.context_pack_id,
                    package.route_receipt_id,
                    package.project_id,
                    package.context_pack_hash,
                    package.route_receipt_hash,
                    package.project_revision,
                    package.project_fingerprint,
                    package.active_chapter_id,
                    package.readiness,
                    package.package_hash,
                    payload_json,
                    package.created_at.isoformat(),
                ),
            )

    @staticmethod
    def _rebuild_tool_call_schema_v7(
        connection: sqlite3.Connection,
        old_columns: set[str],
    ) -> None:
        connection.execute("DROP TABLE IF EXISTS tool_call_migrated_v7")
        connection.execute(
            """
            CREATE TABLE tool_call_migrated_v7 (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
                ),
                idempotency_key TEXT,
                request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
                input_json TEXT NOT NULL,
                output_json TEXT,
                error_json TEXT,
                call_activity_id TEXT UNIQUE REFERENCES activity(id) ON DELETE RESTRICT,
                result_activity_id TEXT UNIQUE REFERENCES activity(id) ON DELETE RESTRICT,
                result_hash TEXT CHECK (
                    result_hash IS NULL OR length(result_hash) = 64
                ),
                duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
                started_at TEXT,
                finished_at TEXT,
                UNIQUE (run_id, idempotency_key)
            ) STRICT
            """
        )

        rows = connection.execute("SELECT * FROM tool_call ORDER BY rowid").fetchall()
        for row in rows:
            tool_call_id = str(row["id"])
            run_id = str(row["run_id"])
            tool_name = str(row["tool_name"])
            tool_version = (
                str(row["tool_version"])
                if "tool_version" in old_columns and row["tool_version"]
                else "0"
            )
            input_json = str(row["input_json"])
            request_hash = (
                str(row["request_hash"])
                if "request_hash" in old_columns
                and row["request_hash"]
                and len(str(row["request_hash"])) == 64
                else hashlib.sha256(
                    json.dumps(
                        {
                            "schema": "zhimeng.legacy-tool-call-request.v1",
                            "tool_call_id": tool_call_id,
                            "run_id": run_id,
                            "tool_name": tool_name,
                            "tool_version": tool_version,
                            "input_json": input_json,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            output_json = row["output_json"] if "output_json" in old_columns else None
            error_json = row["error_json"] if "error_json" in old_columns else None
            status = str(row["status"])
            result_hash = (
                str(row["result_hash"])
                if "result_hash" in old_columns
                and row["result_hash"]
                and len(str(row["result_hash"])) == 64
                else hashlib.sha256(
                    json.dumps(
                        {
                            "schema": "zhimeng.legacy-tool-call-result.v1",
                            "status": status,
                            "output_json": output_json,
                            "error_json": error_json,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if status in {"succeeded", "failed", "cancelled"}
                else None
            )
            connection.execute(
                """
                INSERT INTO tool_call_migrated_v7(
                    id, run_id, tool_name, tool_version, status,
                    idempotency_key, request_hash, input_json, output_json,
                    error_json, call_activity_id, result_activity_id,
                    result_hash, duration_ms, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_call_id,
                    run_id,
                    tool_name,
                    tool_version,
                    status,
                    row["idempotency_key"] if "idempotency_key" in old_columns else None,
                    request_hash,
                    input_json,
                    output_json,
                    error_json,
                    row["call_activity_id"]
                    if "call_activity_id" in old_columns
                    else None,
                    row["result_activity_id"]
                    if "result_activity_id" in old_columns
                    else None,
                    result_hash,
                    row["duration_ms"] if "duration_ms" in old_columns else None,
                    row["started_at"] if "started_at" in old_columns else None,
                    row["finished_at"] if "finished_at" in old_columns else None,
                ),
            )

        connection.execute("DROP TABLE tool_call")
        connection.execute("ALTER TABLE tool_call_migrated_v7 RENAME TO tool_call")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_call_run_status "
            "ON tool_call(run_id, status)"
        )

    @staticmethod
    def _rebuild_run_schema_v6(
        connection: sqlite3.Connection,
        old_columns: set[str],
    ) -> None:
        connection.execute("DROP TABLE IF EXISTS run_migrated_v6")
        connection.execute(
            """
            CREATE TABLE run_migrated_v6 (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES execution_session(id) ON DELETE CASCADE,
                project_binding_id TEXT NOT NULL REFERENCES project_binding(id) ON DELETE RESTRICT,
                context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                run_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'starting', 'running', 'awaiting_approval', 'paused',
                        'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted'
                    )
                ),
                objective TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
                project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
                project_fingerprint TEXT NOT NULL CHECK (
                    length(project_fingerprint) = 64
                ),
                executor_json TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                causation_id TEXT,
                last_activity_sequence INTEGER NOT NULL DEFAULT 0 CHECK (
                    last_activity_sequence >= 0
                ),
                error_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE (session_id, ordinal),
                UNIQUE (session_id, idempotency_key)
            ) STRICT
            """
        )

        rows = connection.execute("SELECT * FROM run ORDER BY created_at, id").fetchall()
        session_ordinals: dict[str, int] = {}
        now = datetime.now(UTC).isoformat()
        for row in rows:
            run_id = str(row["id"])
            context_pack_id = (
                str(row["context_pack_id"])
                if "context_pack_id" in old_columns and row["context_pack_id"]
                else None
            )
            project_binding_id = (
                str(row["project_binding_id"])
                if "project_binding_id" in old_columns and row["project_binding_id"]
                else ""
            )
            project_row = (
                connection.execute(
                    "SELECT revision, fingerprint FROM project_binding WHERE id = ?",
                    (project_binding_id,),
                ).fetchone()
                if project_binding_id
                else None
            )
            if project_row is None:
                project_binding_id = "project-legacy-" + hashlib.sha256(
                    run_id.encode("utf-8")
                ).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO project_binding(
                        id, root_path, display_name, fingerprint, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, 'Legacy unbound project', '', 1, ?, ?)
                    """,
                    (project_binding_id, f"legacy://unbound/{project_binding_id}", now, now),
                )
                project_row = connection.execute(
                    "SELECT revision, fingerprint FROM project_binding WHERE id = ?",
                    (project_binding_id,),
                ).fetchone()
            if project_row is None:  # pragma: no cover - insert invariant.
                raise RuntimeError("legacy project migration produced no binding")
            project_revision = (
                int(row["project_revision"])
                if "project_revision" in old_columns
                and row["project_revision"] is not None
                and int(row["project_revision"]) >= 1
                else max(1, int(project_row["revision"]))
            )
            fingerprint_candidates = [
                row["project_fingerprint"]
                if "project_fingerprint" in old_columns
                else None,
                project_row["fingerprint"],
            ]
            project_fingerprint = next(
                (
                    str(value)
                    for value in fingerprint_candidates
                    if value is not None and len(str(value)) == 64
                ),
                hashlib.sha256(
                    f"legacy-project-revision:{project_binding_id}:{project_revision}".encode(
                        "utf-8"
                    )
                ).hexdigest(),
            )
            raw_session_id = (
                str(row["session_id"]).strip()
                if "session_id" in old_columns and row["session_id"]
                else ""
            )
            session_source = raw_session_id or f"run:{run_id}"
            session_id = "session-legacy-" + hashlib.sha256(
                f"{project_binding_id}:{session_source}".encode("utf-8")
            ).hexdigest()[:24]
            session_correlation_id = "correlation-legacy-" + hashlib.sha256(
                session_id.encode("utf-8")
            ).hexdigest()[:24]
            run_correlation_id = "correlation-legacy-run-" + hashlib.sha256(
                run_id.encode("utf-8")
            ).hexdigest()[:24]
            legacy_executor_json = json.dumps(
                LEGACY_EXECUTOR,
                sort_keys=True,
                separators=(",", ":"),
            )
            legacy_capabilities_json = json.dumps(
                LEGACY_CAPABILITIES,
                sort_keys=True,
                separators=(",", ":"),
            )
            created_at = str(row["created_at"])
            updated_at = str(row["updated_at"])
            connection.execute(
                """
                INSERT OR IGNORE INTO execution_session(
                    id, project_binding_id, title, status, revision,
                    latest_project_revision, latest_project_fingerprint,
                    executor_json, capabilities_json, correlation_id,
                    last_run_ordinal, last_message_ordinal, created_at, updated_at
                ) VALUES (?, ?, ?, 'interrupted', 1, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    session_id,
                    project_binding_id,
                    f"Migrated legacy run {run_id}",
                    project_revision,
                    project_fingerprint,
                    legacy_executor_json,
                    legacy_capabilities_json,
                    session_correlation_id,
                    created_at,
                    updated_at,
                ),
            )
            ordinal = session_ordinals.get(session_id, 0) + 1
            session_ordinals[session_id] = ordinal
            raw_status = str(row["status"])
            status = {
                "waiting_approval": "awaiting_approval",
                "completed": "succeeded",
            }.get(raw_status, raw_status)
            run_type = str(row["run_type"]) if "run_type" in old_columns else "legacy"
            idempotency_key = (
                str(row["idempotency_key"])
                if "idempotency_key" in old_columns and row["idempotency_key"]
                else f"legacy:{run_id}"
            )
            request_hash = hashlib.sha256(
                json.dumps(
                    {
                        "run_id": run_id,
                        "run_type": run_type,
                        "idempotency_key": idempotency_key,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            completed_at = (
                str(row["completed_at"])
                if "completed_at" in old_columns and row["completed_at"]
                else updated_at if status in {"succeeded", "failed", "cancelled", "interrupted"} else None
            )
            started_at = (
                None
                if status == "queued"
                else str(row["started_at"])
                if "started_at" in old_columns and row["started_at"]
                else created_at
            )
            error_json = (
                json.dumps(
                    {
                        "schema": "zhimeng.error.v1",
                        "error": {
                            "code": "legacy_run_failed",
                            "message": "Migrated legacy Run was already failed",
                            "request_id": run_id,
                            "retryable": False,
                            "details": {"migrated": True},
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if status == "failed"
                else None
            )
            connection.execute(
                """
                INSERT INTO run_migrated_v6(
                    id, session_id, project_binding_id, context_pack_id,
                    ordinal, run_type, status,
                    objective, idempotency_key, request_hash, project_revision,
                    project_fingerprint, executor_json, capabilities_json,
                    correlation_id, causation_id, last_activity_sequence, error_json,
                    created_at, started_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    project_binding_id,
                    context_pack_id,
                    ordinal,
                    run_type,
                    status,
                    f"Migrated {run_type} run",
                    idempotency_key,
                    request_hash,
                    project_revision,
                    project_fingerprint,
                    legacy_executor_json,
                    legacy_capabilities_json,
                    run_correlation_id,
                    0,
                    error_json,
                    created_at,
                    started_at,
                    updated_at,
                    completed_at,
                ),
            )
            connection.execute(
                """
                UPDATE execution_session
                SET last_run_ordinal = MAX(last_run_ordinal, ?),
                    latest_project_revision = ?, latest_project_fingerprint = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    ordinal,
                    project_revision,
                    project_fingerprint,
                    updated_at,
                    session_id,
                ),
            )

        connection.execute("DROP TABLE run")
        connection.execute("ALTER TABLE run_migrated_v6 RENAME TO run")
        connection.execute(
            """
            UPDATE run
            SET last_activity_sequence = COALESCE(
                (SELECT MAX(activity.sequence) FROM activity WHERE activity.run_id = run.id),
                0
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_project_status ON run(project_binding_id, status, created_at)"
        )

    @staticmethod
    def _project_binding_from_row(row: sqlite3.Row) -> ProjectBindingRecord:
        return ProjectBindingRecord(
            project_id=str(row["id"]),
            root_path=str(row["root_path"]),
            display_name=str(row["display_name"]),
            fingerprint=str(row["fingerprint"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _health_from_connection(connection: sqlite3.Connection) -> DatabaseHealth:
        journal_mode = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required_columns_present = all(
            columns.issubset(
                {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                }
            )
            for table_name, columns in REQUIRED_COLUMNS.items()
        )
        return DatabaseHealth(
            ready=(
                journal_mode == "wal"
                and schema_version == SCHEMA_VERSION
                and REQUIRED_TABLES.issubset(table_names)
                and required_columns_present
            ),
            journal_mode=journal_mode,
            schema_version=schema_version,
        )
