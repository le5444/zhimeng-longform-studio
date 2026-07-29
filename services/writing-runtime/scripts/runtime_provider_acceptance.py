from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.agent_provider import (
    AgentProvider,
    AgentProviderCancelled,
    AgentProviderError,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.provider_probe import (
    ProviderProbeError,
    SecretLeakError,
    normalize_base_url,
)


_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_FIXTURE_PROJECT = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "minimal-novel"
)


class AcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProviderTargetEvidence(AcceptanceModel):
    base_url: str
    model: str
    api_key_env: str
    credential_reference: str
    dispatch_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_reported_model_count: int = Field(ge=0)
    accepted_reported_model_hashes: tuple[str, ...] = Field(
        default=(),
        max_length=64,
    )


class ProviderExchangeEvidence(AcceptanceModel):
    request_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    text_delta_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    completion_count: int = Field(ge=0)
    response_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProviderTurnEvidence(AcceptanceModel):
    receipt_id: str
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_boundary_id: str
    provider_boundary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_ordinal: int = Field(ge=1, le=8)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_model: str | None = None
    reported_model: str | None = None
    requested_model_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reported_model_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    accepted_reported_model_count: int = Field(ge=0)
    model_identity_status: Literal[
        "exact_match",
        "accepted_alias",
        "unreported",
        "rejected_mismatch",
    ]
    finish_reason: Literal[
        "stop",
        "tool_calls",
        "function_call",
        "length",
        "content_filter",
        "other",
    ]
    finish_reason_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ProviderReceiptChainEvidence(AcceptanceModel):
    provider_boundary_id: str
    provider_boundary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_model: str | None = None
    provider_turn_receipts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_identity_status: Literal[
        "pending",
        "verified",
        "unreported",
        "rejected_mismatch",
    ]
    turn_count: int = Field(ge=0, le=8)


class RuntimeRunEvidence(AcceptanceModel):
    run_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    project_revision: int | None = Field(default=None, ge=1)
    project_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: str
    error_code: str | None = None
    last_activity_sequence: int = Field(default=0, ge=0)


class ProjectKbEvidence(AcceptanceModel):
    status: Literal["current"]
    project_kb_state_id: str = Field(
        pattern=r"^project-kb-state-[0-9a-f]{32}$"
    )
    project_kb_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_state_revision: int = Field(ge=0)
    reused_snapshot: bool
    context_pack_binding_match: Literal[True]


class WorkflowCheckpointEvidence(AcceptanceModel):
    checkpoint_id: str
    checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    step_ordinal: int = Field(ge=1)
    skill_id: str
    execution_status: str


class WorkflowEvidence(AcceptanceModel):
    workflow_plan_id: str
    workflow_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoints: tuple[WorkflowCheckpointEvidence, ...]
    provider_boundary_id: str | None = None
    provider_boundary_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provider_request_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    resume_state: str
    formal_draft_allowed: Literal[False] = False


class GateEvidence(AcceptanceModel):
    call_ordinal: int = Field(ge=1)
    status: str
    execution_receipt_id: str | None = None
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    skill_receipt_id: str | None = None
    skill_receipt_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    tool_result_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    domain_status: str | None = None


class CandidateEvidence(AcceptanceModel):
    schema_id: Literal["zhimeng.agent-candidate-output.v3"] = Field(alias="schema")
    candidate_output_id: str
    envelope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    references_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str
    reference_count: int = Field(ge=1)
    provider_turn_receipt_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    provider_turn_receipt_hashes: tuple[str, ...] = Field(
        min_length=1,
        max_length=8,
    )
    provider_turn_receipts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_identity_status: Literal["verified", "unreported"]
    writer_task_id: str
    writer_task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_materialization_id: str
    instruction_materialization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_dispatch_receipt_id: str
    instruction_dispatch_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    formal_draft_allowed: Literal[False]
    requires_author_review: Literal[True]


class WriterEvidence(AcceptanceModel):
    writer_task_id: str
    writer_task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    writer_candidate_result_id: str
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_output_id: str
    candidate_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: str
    candidate_kind: str
    change_set_id: str | None = None
    change_set_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    formal_draft_allowed: Literal[False]
    writes_project_truth: Literal[False]
    requires_author_review: Literal[True]


class WriterTraceEvidence(AcceptanceModel):
    schema_id: Literal["zhimeng.writer-trace.v1"] = Field(alias="schema")
    run_id: str
    writer_task_id: str
    writer_task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    activation_binding_id: str
    activation_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_materialization_id: str
    instruction_materialization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_dispatch_receipt_id: str
    instruction_dispatch_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    automatic_provider_replay_count: Literal[0]
    dispatch_status: Literal["committed_before_dispatch"]
    candidate_output_id: str
    candidate_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    writer_candidate_result_id: str
    writer_candidate_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_recovery_present: Literal[False]


class ToolCallEvidence(AcceptanceModel):
    tool_call_id: str
    tool_name: str
    status: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArtifactEvidence(AcceptanceModel):
    artifact_id: str
    artifact_type: str
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class StorageEvidence(AcceptanceModel):
    isolated_runtime_created: bool
    isolated_runtime_removed: bool
    api_key_persisted: bool
    authorization_header_persisted: bool
    project_unchanged: bool | None = None


class ProposedProseEvidence(AcceptanceModel):
    """Author-readable excerpt of the prose the Provider proposed.

    Recorded only for author_review_change_proposal runs so the author can
    read the candidate without restoring the isolated Runtime. The project
    itself stays unchanged until the author approves the ChangeSet.
    """

    change_set_id: str
    relative_path: str
    added_character_count: int = Field(ge=0)
    added_excerpt: str = Field(max_length=4000)
    excerpt_truncated: bool


class RuntimeProviderAcceptanceReport(AcceptanceModel):
    schema_id: Literal["zhimeng.runtime-provider-acceptance.v1"] = Field(
        default="zhimeng.runtime-provider-acceptance.v1",
        alias="schema",
    )
    acceptance_status: Literal["passed", "failed"]
    failure_stage: str | None = None
    failure_code: str | None = None
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    target: ProviderTargetEvidence
    provider_exchange: ProviderExchangeEvidence
    provider_receipt_chain: ProviderReceiptChainEvidence | None = None
    provider_turns: tuple[ProviderTurnEvidence, ...] = ()
    runtime_schema_version: int | None = Field(default=None, ge=1)
    run: RuntimeRunEvidence
    project_kb: ProjectKbEvidence | None = None
    workflow: WorkflowEvidence | None = None
    prewrite_gate: GateEvidence | None = None
    candidate: CandidateEvidence | None = None
    writer: WriterEvidence | None = None
    writer_trace: WriterTraceEvidence | None = None
    tool_calls: tuple[ToolCallEvidence, ...] = ()
    artifacts: tuple[ArtifactEvidence, ...] = ()
    storage: StorageEvidence
    proposed_prose: ProposedProseEvidence | None = None


@dataclass(frozen=True, slots=True)
class RuntimeProviderAcceptanceConfig:
    workspace_root: Path
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)
    accepted_reported_models: tuple[str, ...] = ()
    timeout_seconds: float = 180.0
    provider_timeout_seconds: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.0
    allow_insecure_loopback_http: bool = False
    writer_mode: Literal["repair_only", "author_review_change_proposal"] = (
        "repair_only"
    )


class RuntimeProviderAcceptanceError(RuntimeError):
    def __init__(self, stage: str, code: str) -> None:
        super().__init__("Runtime Provider acceptance failed")
        self.stage = _safe_report_token(stage, fallback="acceptance")
        self.code = _safe_report_token(code, fallback="acceptance_failed")


class CredentialGuardedAgentProvider:
    """Delegate to a real Provider while preventing credential reflection."""

    def __init__(self, provider: AgentProvider, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._provider = provider
        self._api_key = api_key
        self._response_hasher = hashlib.sha256()
        self.request_count = 0
        self.event_count = 0
        self.text_delta_count = 0
        self.tool_call_count = 0
        self.completion_count = 0

    @property
    def identity(self):
        return self._provider.identity

    @property
    def capabilities(self):
        return self._provider.capabilities

    @property
    def dispatch_config_fingerprint(self) -> str:
        return self._provider.dispatch_config_fingerprint

    @property
    def model(self) -> str | None:
        value = getattr(self._provider, "model", None)
        return value if isinstance(value, str) else None

    @property
    def accepted_reported_models(self) -> tuple[str, ...]:
        raw = getattr(self._provider, "accepted_reported_models", ())
        return tuple(raw) if isinstance(raw, (tuple, list)) else ()

    @property
    def response_digest(self) -> str | None:
        return self._response_hasher.hexdigest() if self.event_count else None

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.request_count += 1
        try:
            async for event in self._provider.stream_chat(
                messages,
                cancel_event=cancel_event,
                tools=tools,
            ):
                event_payload = _provider_event_payload(event)
                if _contains_secret(event_payload, self._api_key):
                    raise AgentProviderError(
                        "Provider response contained forbidden credential material",
                        code="provider_acceptance_secret_reflection",
                        retryable=False,
                    )
                encoded = json.dumps(
                    event_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._response_hasher.update(len(encoded).to_bytes(8, "big"))
                self._response_hasher.update(encoded)
                self.event_count += 1
                if isinstance(event, ProviderTextDelta):
                    self.text_delta_count += 1
                elif isinstance(event, ProviderToolCall):
                    self.tool_call_count += 1
                elif isinstance(event, ProviderCompleted):
                    self.completion_count += 1
                yield event
        except AgentProviderCancelled:
            raise
        except AgentProviderError as exc:
            code = _safe_report_token(exc.code, fallback="agent_provider_failed")
            message = (
                "Provider response contained forbidden credential material"
                if code == "provider_acceptance_secret_reflection"
                else "Remote Provider request failed; details were redacted"
            )
            raise AgentProviderError(
                message,
                code=code,
                retryable=exc.retryable,
            ) from None
        except Exception:
            raise AgentProviderError(
                "Remote Provider request failed; details were redacted",
                code="provider_acceptance_transport_failed",
                retryable=True,
            ) from None

    def exchange_evidence(self) -> ProviderExchangeEvidence:
        return ProviderExchangeEvidence(
            request_count=self.request_count,
            event_count=self.event_count,
            text_delta_count=self.text_delta_count,
            tool_call_count=self.tool_call_count,
            completion_count=self.completion_count,
            response_digest=self.response_digest,
        )


def run_runtime_provider_acceptance(
    config: RuntimeProviderAcceptanceConfig,
    *,
    provider: AgentProvider | None = None,
    fixture_project: Path | None = None,
) -> RuntimeProviderAcceptanceReport:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    workspace_root = config.workspace_root.expanduser().resolve()
    runtime_parent = workspace_root / ".runtime" / "provider-acceptance"
    runtime_parent.mkdir(parents=True, exist_ok=True)
    normalized_base_url = normalize_base_url(
        config.base_url,
        allow_insecure_loopback_http=config.allow_insecure_loopback_http,
    )
    model = config.model.strip()
    if not model or len(model) > 256 or any(character.isspace() for character in model):
        raise ProviderProbeError("provider model must be a compact non-empty identifier")
    if not _SAFE_CODE.fullmatch(config.api_key_env.casefold()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", config.api_key_env):
            raise ProviderProbeError("api-key environment variable name is invalid")
    if not config.api_key or "\r" in config.api_key or "\n" in config.api_key:
        raise ProviderProbeError("api-key environment variable is missing or invalid")
    if config.timeout_seconds <= 0 or config.provider_timeout_seconds <= 0:
        raise ProviderProbeError("acceptance timeouts must be positive")
    accepted_reported_models = _normalize_model_aliases(
        config.accepted_reported_models,
        requested_model=model,
    )
    if _contains_secret(
        {
            "base_url": normalized_base_url,
            "model": model,
            "accepted_reported_models": accepted_reported_models,
        },
        config.api_key,
    ):
        raise SecretLeakError("Provider configuration contains credential material")

    credential_reference = f"env:{config.api_key_env}"
    remote_provider = provider or OpenAICompatibleAgentProvider(
        base_url=normalized_base_url,
        model=model,
        api_key=config.api_key,
        credential_reference=credential_reference,
        timeout_seconds=config.provider_timeout_seconds,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        accepted_reported_models=accepted_reported_models,
    )
    guarded_provider = CredentialGuardedAgentProvider(
        remote_provider,
        api_key=config.api_key,
    )
    if guarded_provider.model != model:
        raise ProviderProbeError(
            "injected Provider requested model does not match acceptance config"
        )
    if tuple(sorted(set(guarded_provider.accepted_reported_models))) != (
        accepted_reported_models
    ):
        raise ProviderProbeError(
            "injected Provider alias allowlist does not match acceptance config"
        )
    target = ProviderTargetEvidence(
        base_url=normalized_base_url,
        model=model,
        api_key_env=config.api_key_env,
        credential_reference=credential_reference,
        dispatch_config_fingerprint=guarded_provider.dispatch_config_fingerprint,
        accepted_reported_model_count=len(accepted_reported_models),
        accepted_reported_model_hashes=tuple(
            _text_hash(alias) for alias in accepted_reported_models
        ),
    )

    temporary_root = Path(tempfile.mkdtemp(prefix="work-", dir=runtime_parent))
    isolated_created = temporary_root.exists()
    failure_stage: str | None = None
    failure_code: str | None = None
    runtime_schema_version: int | None = None
    run_payload: dict[str, Any] | None = None
    session_id: str | None = None
    initial_project: dict[str, Any] | None = None
    final_project: dict[str, Any] | None = None
    project_kb_evidence: ProjectKbEvidence | None = None
    workflow_evidence: WorkflowEvidence | None = None
    gate_evidence: GateEvidence | None = None
    candidate_evidence: CandidateEvidence | None = None
    writer_evidence: WriterEvidence | None = None
    writer_trace_evidence: WriterTraceEvidence | None = None
    proposed_prose_evidence: ProposedProseEvidence | None = None
    provider_receipt_chain_evidence: ProviderReceiptChainEvidence | None = None
    provider_turn_evidence: tuple[ProviderTurnEvidence, ...] = ()
    tool_evidence: tuple[ToolCallEvidence, ...] = ()
    artifact_evidence: tuple[ArtifactEvidence, ...] = ()
    key_persisted = False
    isolated_removed = False

    try:
        library = temporary_root / "library"
        library.mkdir()
        project = library / "novel"
        source_project = (fixture_project or _FIXTURE_PROJECT).resolve()
        shutil.copytree(source_project, project)
        database_path = temporary_root / "runtime.sqlite3"
        settings = RuntimeSettings(
            environment="provider-acceptance",
            database_path=database_path,
            trusted_project_roots=(library.resolve(),),
            agent_provider_base_url=normalized_base_url,
            agent_provider_api_key=config.api_key,
            agent_provider_credential_reference=credential_reference,
            agent_provider_model=model,
            agent_provider_accepted_reported_models=accepted_reported_models,
            agent_provider_timeout_seconds=config.provider_timeout_seconds,
            agent_provider_max_tokens=config.max_tokens,
            agent_provider_temperature=config.temperature,
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        )
        app = create_app(settings, agent_provider=guarded_provider)

        try:
            with TestClient(app) as client:
                runtime_schema_version = app.state.runtime.database_health.schema_version
                inspected = _require_response(
                    client.post(
                        "/api/v1/projects/inspect",
                        json={"root_path": str(project)},
                    ),
                    stage="project_inspect",
                    expected_status=200,
                )
                initial_project = dict(inspected["project"])
                project_id = str(initial_project["project_id"])
                knowledge = _require_response(
                    client.get(
                        f"/api/v1/projects/{project_id}/knowledge/cards"
                    ),
                    stage="project_kb_knowledge",
                    expected_status=200,
                )
                scope = knowledge["scope"]
                rebuilt = _require_response(
                    client.post(
                        f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
                        json={
                            "expected_project_revision": initial_project["revision"],
                            "expected_project_fingerprint": initial_project["fingerprint"],
                            "expected_scope_state_revision": scope["state_revision"],
                            "expected_knowledge_fingerprint": knowledge["fingerprint"],
                        },
                    ),
                    stage="project_kb_rebuild",
                    expected_status=200,
                )
                project_kb_record = rebuilt["record"]
                project_kb_manifest = project_kb_record["manifest"]
                session = _require_response(
                    client.post(
                        f"/api/v1/projects/{project_id}/agent/sessions",
                        json={"title": "Real Provider Acceptance"},
                    ),
                    stage="session_create",
                    expected_status=201,
                )
                session_id = str(session["session_id"])
                if config.writer_mode == "author_review_change_proposal":
                    objective = (
                        "用 project_list_files 和 project_read_file 读取本章正文，"
                        "再调用 change_set_propose 提出一份可审阅的正文改动，"
                        "最后调用 candidate_output_submit 恰好一次提交 change_proposal 候选。"
                        "正文必须遵守已下发的写作规范：开头进入当前压力或选择，"
                        "本章至少改变局面、关系、信息、风险、目标或资源之一，"
                        "对话带关系压力，心理描写落到可见执行，"
                        "结尾把未解问题递给下一章。"
                        "不要输出 ToolCall 之外的解释、前言或推理文本。"
                        "候选只提交 kind 和 content；证据引用由 Runtime 拥有。"
                        "正式正文写回仍需作者批准。"
                    )
                else:
                    objective = (
                        "Call candidate_output_submit exactly once with the Runtime-required "
                        "repair plan. Emit no assistant text, preamble, explanation, markdown, "
                        "or reasoning before or after the ToolCall. Do not quote project prose. "
                        "Submit only kind and content; Runtime owns all evidence references. "
                        "Make no project changes."
                    )
                context = _require_response(
                    client.post(
                        f"/api/v1/agent/sessions/{session_id}/context-packs",
                        headers={"Idempotency-Key": "provider-acceptance-context-v1"},
                        json={
                            "objective": objective,
                            "profile": "rewrite",
                            "active_chapter_id": "VOL001-CH001",
                            "expected_project_revision": initial_project["revision"],
                            "expected_project_fingerprint": initial_project["fingerprint"],
                            "project_kb_state_id": project_kb_manifest["state_id"],
                            "project_kb_manifest_hash": project_kb_manifest[
                                "manifest_hash"
                            ],
                        },
                    ),
                    stage="context_compile",
                    expected_status=201,
                )
                pack = context["pack"]
                binding_match = (
                    pack.get("project_kb_state_id") == project_kb_manifest["state_id"]
                    and pack.get("project_kb_manifest_hash")
                    == project_kb_manifest["manifest_hash"]
                )
                if not binding_match:
                    raise RuntimeProviderAcceptanceError(
                        "context_compile",
                        "project_kb_context_binding_mismatch",
                    )
                project_kb_evidence = ProjectKbEvidence(
                    status=project_kb_record["status"],
                    project_kb_state_id=project_kb_manifest["state_id"],
                    project_kb_manifest_hash=project_kb_manifest["manifest_hash"],
                    state_hash=project_kb_manifest["state_hash"],
                    knowledge_fingerprint=project_kb_manifest[
                        "knowledge_fingerprint"
                    ],
                    scope_state_revision=project_kb_manifest["knowledge_scope"][
                        "state_revision"
                    ],
                    reused_snapshot=bool(rebuilt["reused_snapshot"]),
                    context_pack_binding_match=True,
                )
                started_run = _require_response(
                    client.post(
                        f"/api/v1/agent/sessions/{session_id}/runs",
                        headers={"Idempotency-Key": "provider-acceptance-run-v1"},
                        json={
                            "message": objective,
                            "expected_project_revision": pack["project_revision"],
                            "expected_project_fingerprint": pack["project_fingerprint"],
                            "active_chapter_id": pack["active_chapter_id"],
                            "writer_mode": config.writer_mode,
                            "context_pack_id": pack["context_pack_id"],
                            "expected_context_pack_hash": pack["pack_hash"],
                        },
                    ),
                    stage="run_start",
                    expected_status=202,
                )
                run_payload = dict(started_run["run"])
                run_id = str(run_payload["run_id"])
                run_payload = _wait_for_terminal(
                    client,
                    run_id,
                    timeout_seconds=config.timeout_seconds,
                )

                workflow_response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")
                if workflow_response.status_code == 200:
                    workflow_evidence = _workflow_evidence(workflow_response.json())
                gate_response = client.get(
                    f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
                    "/nlcl-route/executions/2"
                )
                if gate_response.status_code == 200:
                    gate_evidence = _gate_evidence(gate_response.json())
                candidate_response = client.get(
                    f"/api/v1/agent/runs/{run_id}/candidate-output"
                )
                if candidate_response.status_code == 200:
                    candidate_evidence = _candidate_evidence(candidate_response.json())
                writer_task_response = client.get(
                    f"/api/v1/agent/runs/{run_id}/writer-task"
                )
                writer_result_response = client.get(
                    f"/api/v1/agent/runs/{run_id}/writer-candidate-result"
                )
                if (
                    writer_task_response.status_code == 200
                    and writer_result_response.status_code == 200
                ):
                    writer_evidence = _writer_evidence(
                        writer_task_response.json(),
                        writer_result_response.json(),
                    )
                writer_trace_response = client.get(
                    f"/api/v1/agent/runs/{run_id}/writer-trace"
                )
                if writer_trace_response.status_code == 200:
                    writer_trace_evidence = _writer_trace_evidence(
                        writer_trace_response.json()
                    )
                if (
                    writer_evidence is not None
                    and writer_evidence.change_set_id is not None
                ):
                    change_set_response = client.get(
                        f"/api/v1/change-sets/{writer_evidence.change_set_id}"
                    )
                    if change_set_response.status_code == 200:
                        proposed_prose_evidence = _proposed_prose_evidence(
                            writer_evidence.change_set_id,
                            change_set_response.json(),
                        )
                if (
                    workflow_evidence is not None
                    and workflow_evidence.provider_boundary_id is not None
                ):
                    provider_receipts_response = _require_response(
                        client.get(
                            f"/api/v1/agent/runs/{run_id}/provider-turn-receipts"
                        ),
                        stage="provider_turn_receipts",
                        expected_status=200,
                    )
                    provider_receipt_chain_evidence = (
                        _provider_receipt_chain_evidence(
                            provider_receipts_response
                        )
                    )
                    provider_turn_evidence = tuple(
                        _provider_turn_evidence(receipt)
                        for receipt in provider_receipts_response[
                            "provider_turn_receipts"
                        ]
                    )
                refreshed = _require_response(
                    client.post(
                        "/api/v1/projects/inspect",
                        json={"root_path": str(project)},
                    ),
                    stage="project_reinspect",
                    expected_status=200,
                )
                final_project = dict(refreshed["project"])

                with app.state.runtime.store.connection() as connection:
                    tool_rows = connection.execute(
                        """
                        SELECT id, tool_name, status, request_hash, result_hash
                        FROM tool_call WHERE run_id = ? ORDER BY started_at, id
                        """,
                        (run_id,),
                    ).fetchall()
                    artifact_rows = connection.execute(
                        """
                        SELECT id, artifact_type, content_hash
                        FROM artifact WHERE run_id = ? ORDER BY created_at, id
                        """,
                        (run_id,),
                    ).fetchall()
                tool_evidence = tuple(
                    ToolCallEvidence(
                        tool_call_id=str(row["id"]),
                        tool_name=str(row["tool_name"]),
                        status=str(row["status"]),
                        request_hash=str(row["request_hash"]),
                        result_hash=(
                            None if row["result_hash"] is None else str(row["result_hash"])
                        ),
                    )
                    for row in tool_rows
                )
                artifact_evidence = tuple(
                    ArtifactEvidence(
                        artifact_id=str(row["id"]),
                        artifact_type=str(row["artifact_type"]),
                        content_hash=(
                            None if row["content_hash"] is None else str(row["content_hash"])
                        ),
                    )
                    for row in artifact_rows
                )
        except RuntimeProviderAcceptanceError as exc:
            failure_stage = exc.stage
            failure_code = exc.code
        except Exception:
            failure_stage = "runtime_execution"
            failure_code = "acceptance_internal_error"
    except RuntimeProviderAcceptanceError as exc:
        failure_stage = exc.stage
        failure_code = exc.code
    except Exception:
        failure_stage = "runtime_setup"
        failure_code = "acceptance_internal_error"
    finally:
        key_persisted = tree_contains_secret(temporary_root, config.api_key)
        if key_persisted:
            failure_stage = "secret_scan"
            failure_code = "provider_credential_persisted"
        try:
            shutil.rmtree(temporary_root)
        except OSError:
            isolated_removed = False
            failure_stage = "runtime_cleanup"
            failure_code = "isolated_runtime_cleanup_failed"
        else:
            isolated_removed = not temporary_root.exists()

    project_unchanged = _project_unchanged(initial_project, final_project)
    run_status = str(run_payload.get("status", "not_started")) if run_payload else "not_started"
    run_error_code = _run_error_code(run_payload)
    run_evidence = RuntimeRunEvidence(
        run_id=(None if run_payload is None else str(run_payload.get("run_id"))),
        session_id=session_id,
        project_id=(
            None if initial_project is None else str(initial_project.get("project_id"))
        ),
        project_revision=(
            None if initial_project is None else int(initial_project["revision"])
        ),
        project_fingerprint=(
            None if initial_project is None else str(initial_project["fingerprint"])
        ),
        status=run_status,
        error_code=run_error_code,
        last_activity_sequence=(
            0 if run_payload is None else int(run_payload.get("last_activity_sequence", 0))
        ),
    )

    # repair_only binds the Writer task and instruction artifact only.
    # author_review_change_proposal additionally binds the same-Run ChangeSet.
    if config.writer_mode == "repair_only":
        expected_reference_count = 2
        expected_candidate_kind = "repair_plan"
    else:
        expected_reference_count = 3
        expected_candidate_kind = "change_proposal"

    passed = (
        failure_code is None
        and run_status == "succeeded"
        and runtime_schema_version is not None
        and runtime_schema_version >= 20
        and project_kb_evidence is not None
        and project_kb_evidence.context_pack_binding_match is True
        and workflow_evidence is not None
        and len(workflow_evidence.checkpoints) == 3
        and workflow_evidence.provider_boundary_id is not None
        and gate_evidence is not None
        and gate_evidence.status == "succeeded"
        and candidate_evidence is not None
        and candidate_evidence.provider_identity_status == "verified"
        and candidate_evidence.provider_turn_receipt_ids
        == tuple(turn.receipt_id for turn in provider_turn_evidence)
        and candidate_evidence.provider_turn_receipt_hashes
        == tuple(turn.receipt_hash for turn in provider_turn_evidence)
        and provider_receipt_chain_evidence is not None
        and provider_receipt_chain_evidence.provider_identity_status == "verified"
        and provider_receipt_chain_evidence.requested_model == model
        and candidate_evidence.provider_turn_receipts_hash
        == provider_receipt_chain_evidence.provider_turn_receipts_hash
        and writer_evidence is not None
        and candidate_evidence.writer_task_id == writer_evidence.writer_task_id
        and candidate_evidence.writer_task_hash == writer_evidence.writer_task_hash
        and writer_trace_evidence is not None
        and writer_trace_evidence.run_id == run_evidence.run_id
        and writer_trace_evidence.writer_task_id == writer_evidence.writer_task_id
        and writer_trace_evidence.writer_task_hash == writer_evidence.writer_task_hash
        and writer_trace_evidence.instruction_materialization_id
        == candidate_evidence.instruction_materialization_id
        and writer_trace_evidence.instruction_materialization_hash
        == candidate_evidence.instruction_materialization_hash
        and writer_trace_evidence.instruction_dispatch_receipt_id
        == candidate_evidence.instruction_dispatch_receipt_id
        and writer_trace_evidence.instruction_dispatch_receipt_hash
        == candidate_evidence.instruction_dispatch_receipt_hash
        and writer_trace_evidence.candidate_output_id
        == candidate_evidence.candidate_output_id
        and writer_trace_evidence.candidate_output_hash
        == candidate_evidence.envelope_hash
        and writer_trace_evidence.writer_candidate_result_id
        == writer_evidence.writer_candidate_result_id
        and writer_trace_evidence.writer_candidate_result_hash
        == writer_evidence.result_hash
        and writer_trace_evidence.automatic_provider_replay_count == 0
        and writer_trace_evidence.provider_recovery_present is False
        and candidate_evidence.reference_count == expected_reference_count
        and writer_evidence.mode == config.writer_mode
        and writer_evidence.candidate_kind == expected_candidate_kind
        and (
            (writer_evidence.change_set_id is None)
            if config.writer_mode == "repair_only"
            else (writer_evidence.change_set_id is not None)
        )
        and {
            "writer_task",
            "creation_instruction_activation_binding",
            "creation_instruction_materialization",
            "creation_instruction_dispatch_receipt",
            "writer_candidate_result",
        }.issubset({artifact.artifact_type for artifact in artifact_evidence})
        and provider_turn_evidence
        and len(provider_turn_evidence) == guarded_provider.completion_count
        and all(
            turn.model_identity_status in {"exact_match", "accepted_alias"}
            for turn in provider_turn_evidence
        )
        and project_unchanged is True
        and not key_persisted
        and isolated_removed
    )
    if not passed and failure_code is None:
        failure_stage = "acceptance_assertions"
        failure_code = run_error_code or "managed_terminal_contract_incomplete"

    completed_at = datetime.now(UTC)
    return RuntimeProviderAcceptanceReport(
        acceptance_status="passed" if passed else "failed",
        failure_stage=failure_stage,
        failure_code=failure_code,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        target=target,
        provider_exchange=guarded_provider.exchange_evidence(),
        provider_receipt_chain=provider_receipt_chain_evidence,
        provider_turns=provider_turn_evidence,
        runtime_schema_version=runtime_schema_version,
        run=run_evidence,
        project_kb=project_kb_evidence,
        workflow=workflow_evidence,
        prewrite_gate=gate_evidence,
        candidate=candidate_evidence,
        writer=writer_evidence,
        writer_trace=writer_trace_evidence,
        tool_calls=tool_evidence,
        artifacts=artifact_evidence,
        storage=StorageEvidence(
            isolated_runtime_created=isolated_created,
            isolated_runtime_removed=isolated_removed,
            api_key_persisted=key_persisted,
            authorization_header_persisted=key_persisted,
            project_unchanged=project_unchanged,
        ),
        proposed_prose=proposed_prose_evidence,
    )


def write_runtime_provider_acceptance_report(
    path: Path,
    report: RuntimeProviderAcceptanceReport,
    *,
    api_key: str,
    overwrite: bool = False,
) -> None:
    payload = report.model_dump(mode="json", by_alias=True)
    _assert_report_safe(payload, api_key=api_key)
    target = path.expanduser()
    if api_key in str(target):
        raise SecretLeakError("acceptance report path contains the API key")
    if target.exists() and not overwrite:
        raise ProviderProbeError("acceptance report output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if tree_contains_secret(temporary, api_key):
            raise SecretLeakError("acceptance report would contain the API key")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def tree_contains_secret(root: Path, secret: str) -> bool:
    if not secret:
        return False
    needle = secret.encode("utf-8")
    targets = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in targets:
        try:
            with path.open("rb") as handle:
                overlap = b""
                while chunk := handle.read(1024 * 1024):
                    material = overlap + chunk
                    if needle in material:
                        return True
                    overlap_size = max(0, len(needle) - 1)
                    overlap = material[-overlap_size:] if overlap_size else b""
        except OSError:
            return True
    return False


def _provider_event_payload(event: ProviderEvent) -> dict[str, Any]:
    if isinstance(event, ProviderTextDelta):
        return {"event": "text_delta", "text": event.text}
    if isinstance(event, ProviderToolCall):
        return {
            "event": "tool_call",
            "index": event.index,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "arguments": event.arguments,
        }
    usage = (
        None
        if event.usage is None
        else event.usage.model_dump(mode="json")
    )
    return {
        "event": "completed",
        "finish_reason": event.finish_reason,
        "usage": usage,
        "reported_model": getattr(event, "reported_model", None),
    }


def _provider_receipt_chain_evidence(
    payload: Mapping[str, Any],
) -> ProviderReceiptChainEvidence:
    return ProviderReceiptChainEvidence(
        provider_boundary_id=str(payload["provider_boundary_id"]),
        provider_boundary_hash=str(payload["provider_boundary_hash"]),
        requested_model=payload.get("requested_model"),
        provider_turn_receipts_hash=str(payload["provider_turn_receipts_hash"]),
        provider_identity_status=payload["provider_identity_status"],
        turn_count=len(payload["provider_turn_receipts"]),
    )


def _provider_turn_evidence(receipt: Mapping[str, Any]) -> ProviderTurnEvidence:
    usage = receipt.get("usage")
    requested_model = receipt.get("requested_model")
    reported_model = receipt.get("reported_model")
    raw_finish_reason = str(receipt["finish_reason"])
    safe_finish_reasons = {
        "stop",
        "tool_calls",
        "function_call",
        "length",
        "content_filter",
    }
    finish_reason = (
        raw_finish_reason
        if raw_finish_reason in safe_finish_reasons
        else "other"
    )
    return ProviderTurnEvidence(
        receipt_id=str(receipt["receipt_id"]),
        receipt_hash=str(receipt["receipt_hash"]),
        provider_boundary_id=str(receipt["provider_boundary_id"]),
        provider_boundary_hash=str(receipt["provider_boundary_hash"]),
        turn_ordinal=int(receipt["turn_ordinal"]),
        request_hash=str(receipt["request_hash"]),
        requested_model=requested_model,
        reported_model=reported_model,
        requested_model_hash=(
            None
            if requested_model is None
            else _text_hash(str(requested_model))
        ),
        reported_model_hash=(
            None
            if reported_model is None
            else _text_hash(str(reported_model))
        ),
        accepted_reported_model_count=len(receipt["accepted_reported_models"]),
        model_identity_status=receipt["model_identity_status"],
        finish_reason=finish_reason,
        finish_reason_hash=_text_hash(raw_finish_reason),
        input_tokens=None if usage is None else int(usage["input_tokens"]),
        output_tokens=None if usage is None else int(usage["output_tokens"]),
        total_tokens=None if usage is None else int(usage["total_tokens"]),
    )


def _normalize_model_aliases(
    values: Sequence[str],
    *,
    requested_model: str,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ProviderProbeError("accepted Provider model aliases must be strings")
        candidate = value.strip()
        if not candidate or len(candidate) > 512:
            raise ProviderProbeError(
                "accepted Provider model aliases must be bounded non-empty names"
            )
        if candidate != requested_model and candidate not in normalized:
            normalized.append(candidate)
    return tuple(sorted(normalized))


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contains_secret(value: object, secret: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            secret in str(key) or _contains_secret(child, secret)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(child, secret) for child in value)
    return isinstance(value, str) and secret in value


def _assert_report_safe(value: object, *, api_key: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in {
                "api_key",
                "apikey",
                "authorization",
                "authorization_header",
                "request_headers",
                "response_body",
                "assistant_content",
                "candidate_content",
                "secret",
                "token",
                "password",
            }:
                raise SecretLeakError("acceptance report has a forbidden field")
            _assert_report_safe(child, api_key=api_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_report_safe(child, api_key=api_key)
    elif isinstance(value, str) and api_key in value:
        raise SecretLeakError("acceptance report would contain the API key")


def _require_response(response, *, stage: str, expected_status: int) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise RuntimeProviderAcceptanceError(stage, _response_error_code(response))
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeProviderAcceptanceError(stage, "runtime_response_invalid")
    return payload


def _response_error_code(response) -> str:
    try:
        payload = response.json()
        code = payload.get("error", {}).get("code")
    except (TypeError, ValueError):
        code = None
    return _safe_report_token(
        code if isinstance(code, str) else f"runtime_http_{response.status_code}",
        fallback="runtime_request_failed",
    )


def _wait_for_terminal(
    client: TestClient,
    run_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/agent/runs/{run_id}")
        payload = _require_response(
            response,
            stage="run_poll",
            expected_status=200,
        )["run"]
        if payload["status"] in _TERMINAL_RUN_STATUSES:
            return dict(payload)
        time.sleep(0.05)
    client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    raise RuntimeProviderAcceptanceError("run_wait", "provider_acceptance_timeout")


def _workflow_evidence(payload: Mapping[str, Any]) -> WorkflowEvidence:
    plan = payload["plan"]
    boundary = payload.get("provider_boundary")
    return WorkflowEvidence(
        workflow_plan_id=str(plan["workflow_plan_id"]),
        workflow_plan_hash=str(plan["plan_hash"]),
        checkpoints=tuple(
            WorkflowCheckpointEvidence(
                checkpoint_id=str(item["checkpoint_id"]),
                checkpoint_hash=str(item["checkpoint_hash"]),
                step_ordinal=int(item["step_ordinal"]),
                skill_id=str(item["skill_id"]),
                execution_status=str(item["execution_status"]),
            )
            for item in payload["checkpoints"]
        ),
        provider_boundary_id=(
            None if boundary is None else str(boundary["boundary_id"])
        ),
        provider_boundary_hash=(
            None if boundary is None else str(boundary["boundary_hash"])
        ),
        provider_request_hash=(
            None if boundary is None else str(boundary["request_hash"])
        ),
        resume_state=str(payload["resume_state"]),
        formal_draft_allowed=payload["formal_draft_allowed"],
    )


def _gate_evidence(payload: Mapping[str, Any]) -> GateEvidence:
    receipt = payload.get("receipt")
    return GateEvidence(
        call_ordinal=int(payload["call_ordinal"]),
        status=str(payload["status"]),
        execution_receipt_id=(
            None if receipt is None else str(receipt["execution_receipt_id"])
        ),
        receipt_hash=None if receipt is None else str(receipt["receipt_hash"]),
        skill_receipt_id=(
            None if receipt is None else receipt.get("skill_receipt_id")
        ),
        skill_receipt_hash=(
            None if receipt is None else receipt.get("skill_receipt_hash")
        ),
        tool_result_hash=(
            None if receipt is None else str(receipt["tool_result_hash"])
        ),
        domain_status=None if receipt is None else receipt.get("domain_status"),
    )


def _candidate_evidence(payload: Mapping[str, Any]) -> CandidateEvidence:
    provider_turn_receipts = payload["provider_turn_receipts"]
    return CandidateEvidence(
        schema=payload["schema"],
        candidate_output_id=str(payload["candidate_output_id"]),
        envelope_hash=str(payload["envelope_hash"]),
        content_hash=str(payload["content_hash"]),
        references_hash=str(payload["references_hash"]),
        kind=str(payload["kind"]),
        reference_count=len(payload["references"]),
        provider_turn_receipt_ids=tuple(
            str(item["receipt_id"]) for item in provider_turn_receipts
        ),
        provider_turn_receipt_hashes=tuple(
            str(item["receipt_hash"]) for item in provider_turn_receipts
        ),
        provider_turn_receipts_hash=str(payload["provider_turn_receipts_hash"]),
        provider_identity_status=payload["provider_identity_status"],
        writer_task_id=str(payload["writer_task_id"]),
        writer_task_hash=str(payload["writer_task_hash"]),
        instruction_materialization_id=str(payload["instruction_materialization_id"]),
        instruction_materialization_hash=str(
            payload["instruction_materialization_hash"]
        ),
        instruction_dispatch_receipt_id=str(
            payload["instruction_dispatch_receipt_id"]
        ),
        instruction_dispatch_receipt_hash=str(
            payload["instruction_dispatch_receipt_hash"]
        ),
        formal_draft_allowed=payload["formal_draft_allowed"],
        requires_author_review=payload["requires_author_review"],
    )


_PROSE_EXCERPT_LIMIT = 4000


def _proposed_prose_evidence(
    change_set_id: str,
    payload: Mapping[str, Any],
) -> ProposedProseEvidence | None:
    """Summarise the prose a change_proposal candidate added, for the author.

    Only the added side is recorded: it is what the author has to read and
    decide on. Existing project prose is not duplicated into the report.
    """
    changes = payload.get("change_set", payload).get("changes")
    if not isinstance(changes, list) or not changes:
        return None
    prose = next(
        (
            change
            for change in changes
            if isinstance(change, Mapping)
            and change.get("post_draft_gate_scope") == "prose_body"
        ),
        None,
    ) or next((c for c in changes if isinstance(c, Mapping)), None)
    if prose is None:
        return None
    hunks = prose.get("hunks")
    if not isinstance(hunks, list):
        return None
    added: list[str] = []
    for hunk in hunks:
        if not isinstance(hunk, Mapping):
            continue
        after = str(hunk.get("after_text", ""))
        before = str(hunk.get("before_text", ""))
        # Keep only the lines this hunk introduces, so the excerpt is the
        # candidate prose rather than a copy of the chapter.
        before_lines = set(before.splitlines())
        added.extend(
            line for line in after.splitlines() if line not in before_lines
        )
    added_text = "\n".join(added).strip()
    if not added_text:
        return None
    truncated = len(added_text) > _PROSE_EXCERPT_LIMIT
    return ProposedProseEvidence(
        change_set_id=change_set_id,
        relative_path=str(prose.get("relative_path", "")),
        added_character_count=len(added_text),
        added_excerpt=added_text[:_PROSE_EXCERPT_LIMIT],
        excerpt_truncated=truncated,
    )


def _writer_evidence(
    task: Mapping[str, Any],
    result: Mapping[str, Any],
) -> WriterEvidence:
    return WriterEvidence(
        writer_task_id=str(task["writer_task_id"]),
        writer_task_hash=str(task["task_hash"]),
        writer_candidate_result_id=str(result["writer_candidate_result_id"]),
        result_hash=str(result["result_hash"]),
        candidate_output_id=str(result["candidate_output_id"]),
        candidate_output_hash=str(result["candidate_output_hash"]),
        mode=str(result["mode"]),
        candidate_kind=str(result["candidate_kind"]),
        change_set_id=result.get("change_set_id"),
        change_set_hash=result.get("change_set_hash"),
        formal_draft_allowed=result["formal_draft_allowed"],
        writes_project_truth=result["writes_project_truth"],
        requires_author_review=result["requires_author_review"],
    )


def _writer_trace_evidence(
    payload: Mapping[str, Any],
) -> WriterTraceEvidence | None:
    writer_task = payload["writer_task"]
    activation = payload["activation_binding"]
    materialization = payload["materialization"]
    dispatch = payload["dispatch_receipt"]
    candidate = payload["candidate_output"]
    result = payload["writer_candidate_result"]
    if not all(
        isinstance(item, Mapping)
        for item in (
            writer_task,
            activation,
            materialization,
            dispatch,
            candidate,
            result,
        )
    ):
        return None
    return WriterTraceEvidence(
        schema=payload["schema"],
        run_id=str(payload["run_id"]),
        writer_task_id=str(writer_task["writer_task_id"]),
        writer_task_hash=str(writer_task["task_hash"]),
        activation_binding_id=str(activation["activation_binding_id"]),
        activation_binding_hash=str(activation["binding_hash"]),
        instruction_materialization_id=str(materialization["materialization_id"]),
        instruction_materialization_hash=str(materialization["materialization_hash"]),
        instruction_dispatch_receipt_id=str(dispatch["dispatch_receipt_id"]),
        instruction_dispatch_receipt_hash=str(dispatch["receipt_hash"]),
        automatic_provider_replay_count=dispatch["automatic_provider_replay_count"],
        dispatch_status=dispatch["status"],
        candidate_output_id=str(candidate["candidate_output_id"]),
        candidate_output_hash=str(candidate["envelope_hash"]),
        writer_candidate_result_id=str(result["writer_candidate_result_id"]),
        writer_candidate_result_hash=str(result["result_hash"]),
        provider_recovery_present=payload.get("provider_recovery") is not None,
    )


def _project_unchanged(
    initial: Mapping[str, Any] | None,
    final: Mapping[str, Any] | None,
) -> bool | None:
    if initial is None or final is None:
        return None
    return (
        initial.get("project_id") == final.get("project_id")
        and initial.get("revision") == final.get("revision")
        and initial.get("fingerprint") == final.get("fingerprint")
    )


def _run_error_code(run: Mapping[str, Any] | None) -> str | None:
    if run is None:
        return None
    try:
        code = run["error"]["error"]["code"]
    except (KeyError, TypeError):
        return None
    return _safe_report_token(code, fallback="runtime_run_failed")


def _safe_report_token(value: object, *, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().casefold().replace("-", "_")
        if _SAFE_CODE.fullmatch(normalized):
            return normalized
    return fallback


__all__ = [
    "CredentialGuardedAgentProvider",
    "RuntimeProviderAcceptanceConfig",
    "RuntimeProviderAcceptanceError",
    "RuntimeProviderAcceptanceReport",
    "run_runtime_provider_acceptance",
    "tree_contains_secret",
    "write_runtime_provider_acceptance_report",
]
