from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateContent,
    AgentCandidateOutput,
    AgentCandidateOutputV1,
    AgentCandidateOutputV2,
    AgentCandidateReference,
    agent_candidate_content_hash,
    agent_candidate_output_envelope_hash,
    agent_candidate_output_id,
    agent_candidate_references_hash,
)
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_service import _SESSION_TITLE_PROMPT
from zhimeng_runtime.agent_tools import (
    AI_SMELL_TOOL_NAMES,
    COMMAND_TOOL_NAMES,
    COMPUTER_TOOL_NAMES,
    DIRECT_WRITE_TOOL_NAMES,
    HISTORY_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    PLAN_TOOL_NAMES,
    GOAL_TOOL_NAMES,
    SKILL_FILE_TOOL_NAMES,
    WEB_TOOL_NAMES,
)
from zhimeng_runtime.project_reader import SECTION_LABELS
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import ContextCreateRequest
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.agent_store import (
    AgentContextPackConflict,
    ContextPackKnowledgeTarget,
    ProviderRecoveryConflict,
)
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
    ProviderTurnReceiptBinding,
    ProviderTurnReceiptListResponse,
    build_provider_turn_receipt,
    build_provider_turn_receipt_list_response,
    provider_turn_receipt_chain_hash,
)
from zhimeng_runtime.project_kb_service import (
    ProjectKbConflictError,
    ProjectKbTamperedError,
)


PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "c" * 64
PROJECT_KB_STATE_ID = "project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROJECT_KB_MANIFEST_HASH = "b" * 64


class CurrentProjectKbVerifier:
    def require_current_binding(
        self,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> SimpleNamespace:
        assert project_id == PROJECT_ID
        assert project_kb_state_id == PROJECT_KB_STATE_ID
        assert project_kb_manifest_hash == PROJECT_KB_MANIFEST_HASH
        return SimpleNamespace(
            knowledge_fingerprint=expected_knowledge_fingerprint,
        )

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        yield self.require_current_binding(project_id, **kwargs)


class RejectingProjectKbVerifier:
    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def require_current_binding(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        error_type = (
            ProjectKbTamperedError
            if self.code == "project_kb_state_tampered"
            else ProjectKbConflictError
        )
        raise error_type(
            "project KB recovery gate rejected the candidate",
            details=[{"code": self.code}],
            retryable=self.code != "project_kb_state_tampered",
        )

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        yield self.require_current_binding(project_id, **kwargs)


class ApiProvider:
    @property
    def dispatch_config_fingerprint(self) -> str:
        return "a" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.api-provider",
            kind=ExecutorKind.EMBEDDED,
            implementation="api-provider",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        for event in (
            ProviderTextDelta("第一段"),
            ProviderTextDelta("第二段"),
            ProviderCompleted("stop", None),
        ):
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event


class ChangedApiProvider(ApiProvider):
    @property
    def dispatch_config_fingerprint(self) -> str:
        return "b" * 64


class UnavailableKnowledgeAdapter:
    def observe_knowledge_fingerprint(self) -> str:
        raise RuntimeError("fixture knowledge source unavailable")


def bind_project(client: TestClient) -> None:
    client.app.state.runtime.store.sync_project_binding(
        project_id=PROJECT_ID,
        root_path="C:/Novels/Test",
        display_name="Test Novel",
        fingerprint=FINGERPRINT,
    )


def create_session(client: TestClient) -> str:
    response = client.post(
        f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
        json={"title": "Writing Agent"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def create_run(client: TestClient, session_id: str) -> str:
    response = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": "turn-api-0001"},
        json={
            "message": "收紧开头",
            "expected_project_revision": 1,
            "expected_project_fingerprint": FINGERPRINT,
            "active_chapter_id": "chapter-001",
        },
    )
    assert response.status_code == 202, response.text
    return str(response.json()["run"]["run_id"])


def wait_for_success(client: TestClient, run_id: str) -> dict[str, object]:
    # 1 秒的窗口在这台机器高负载下不够（9 轮工具调用外加一次收尾起名），
    # 放宽到 10 秒；断言的语义不变。
    for _ in range(1000):
        payload = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("API Run did not complete")


def parse_sse(lines: list[str]) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line.startswith("data: ")
    ]


def candidate_output(run_id: str = "run_candidate_api") -> AgentCandidateOutput:
    provider_boundary_id = f"provider-io-{'1' * 32}"
    final_checkpoint_id = f"workflow-checkpoint-{'2' * 32}"
    content = AgentCandidateContent(
        title="Repair the prewrite evidence",
        summary="The candidate remains bounded to author-reviewed repair planning.",
        body="Resolve the persisted failure signature before any formal Writer dispatch.",
    )
    writer_task_id = f"writer-task-{'c' * 32}"
    instruction_materialization_id = (
        f"creation-instruction-materialization-{'d' * 32}"
    )
    instruction_dispatch_receipt_id = f"creation-instruction-dispatch-{'e' * 32}"
    references = (
        AgentCandidateReference(
            reference_kind="runtime_identity",
            reference_id=writer_task_id,
            reference_hash="c" * 64,
        ),
        AgentCandidateReference(
            reference_kind="artifact",
            reference_id=instruction_materialization_id,
            reference_hash="d" * 64,
        ),
    )
    provider_turn_receipts = (
        ProviderTurnReceiptBinding(
            turn_ordinal=1,
            receipt_id=f"provider-turn-receipt-{'a' * 32}",
            receipt_hash="b" * 64,
        ),
    )
    material = {
        "schema_id": "zhimeng.agent-candidate-output.v3",
        "candidate_output_id": agent_candidate_output_id(
            run_id=run_id,
            provider_boundary_id=provider_boundary_id,
        ),
        "run_id": run_id,
        "workflow_plan_id": f"workflow-plan-{'4' * 32}",
        "workflow_plan_hash": "5" * 64,
        "final_checkpoint_id": final_checkpoint_id,
        "final_checkpoint_hash": "3" * 64,
        "provider_boundary_id": provider_boundary_id,
        "provider_boundary_hash": "6" * 64,
        "provider_request_hash": "7" * 64,
        "provider_turn_receipts": provider_turn_receipts,
        "provider_turn_receipts_hash": provider_turn_receipt_chain_hash(
            provider_turn_receipts
        ),
        "provider_identity_status": "verified",
        "writer_task_id": writer_task_id,
        "writer_task_hash": "c" * 64,
        "instruction_materialization_id": instruction_materialization_id,
        "instruction_materialization_hash": "d" * 64,
        "instruction_dispatch_receipt_id": instruction_dispatch_receipt_id,
        "instruction_dispatch_receipt_hash": "e" * 64,
        "context_pack_id": "context-pack-candidate-api",
        "context_pack_hash": "8" * 64,
        "operation_package_id": f"prewrite-package-{'9' * 32}",
        "operation_package_hash": "a" * 64,
        "project_id": PROJECT_ID,
        "project_revision": 1,
        "project_fingerprint": FINGERPRINT,
        "handoff_status": "risk_draft_only",
        "allowed_output": "repair_plan_or_risk_draft_only",
        "failure_signatures": (),
        "kind": "repair_plan",
        "content": content,
        "content_hash": agent_candidate_content_hash(content),
        "references": references,
        "references_hash": agent_candidate_references_hash(references),
        "formal_draft_allowed": False,
        "requires_author_review": True,
        "created_at": datetime(2026, 7, 22, tzinfo=UTC),
    }
    draft = AgentCandidateOutput.model_construct(
        **material,
        envelope_hash="0" * 64,
    )
    return AgentCandidateOutput(
        **material,
        envelope_hash=agent_candidate_output_envelope_hash(draft),
    )


def candidate_output_v2(
    run_id: str = "run_candidate_v2_api",
) -> AgentCandidateOutputV2:
    current = candidate_output(run_id)
    material = {
        field_name: getattr(current, field_name)
        for field_name in AgentCandidateOutputV2.model_fields
        if field_name != "envelope_hash"
    }
    material["schema_id"] = "zhimeng.agent-candidate-output.v2"
    material["candidate_output_id"] = agent_candidate_output_id(
        run_id=run_id,
        provider_boundary_id=current.provider_boundary_id,
        schema_id="zhimeng.agent-candidate-output.v2",
    )
    draft = AgentCandidateOutputV2.model_construct(
        **material,
        envelope_hash="0" * 64,
    )
    return AgentCandidateOutputV2(
        **material,
        envelope_hash=agent_candidate_output_envelope_hash(draft),
    )


def legacy_candidate_output(
    run_id: str = "run_legacy_candidate_api",
) -> AgentCandidateOutputV1:
    current = candidate_output(run_id)
    material = {
        field_name: getattr(current, field_name)
        for field_name in AgentCandidateOutputV1.model_fields
        if field_name != "envelope_hash"
    }
    material["schema_id"] = "zhimeng.agent-candidate-output.v1"
    material["candidate_output_id"] = agent_candidate_output_id(
        run_id=run_id,
        provider_boundary_id=current.provider_boundary_id,
        schema_id="zhimeng.agent-candidate-output.v1",
    )
    draft = AgentCandidateOutputV1.model_construct(
        **material,
        envelope_hash="0" * 64,
    )
    return AgentCandidateOutputV1(
        **material,
        envelope_hash=agent_candidate_output_envelope_hash(draft),
    )


def provider_turn_receipt_list(
    run_id: str = "run_provider_receipt_api",
) -> ProviderTurnReceiptListResponse:
    boundary = SimpleNamespace(
        run_id=run_id,
        boundary_id=f"provider-io-{'c' * 32}",
        boundary_hash="d" * 64,
        request_hash="e" * 64,
        provider_model="requested-model",
    )
    receipt = build_provider_turn_receipt(
        boundary,
        turn_ordinal=1,
        request_hash=boundary.request_hash,
        reported_model="gateway-model",
        accepted_reported_models=("gateway-model",),
        finish_reason="stop",
        usage=None,
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    return build_provider_turn_receipt_list_response(boundary, (receipt,))


def provider_recovery_response(
    *,
    decided: bool = False,
) -> ProviderRecoveryResponse:
    candidate = candidate_output()
    dossier_material = {
        "schema_id": "zhimeng.provider-recovery-dossier.v1",
        "dossier_id": provider_recovery_dossier_id(
            run_id=candidate.run_id,
            provider_boundary_id=candidate.provider_boundary_id,
        ),
        "run_id": candidate.run_id,
        "workflow_plan_id": candidate.workflow_plan_id,
        "workflow_plan_hash": candidate.workflow_plan_hash,
        "final_checkpoint_id": candidate.final_checkpoint_id,
        "final_checkpoint_hash": candidate.final_checkpoint_hash,
        "provider_boundary_id": candidate.provider_boundary_id,
        "provider_boundary_hash": candidate.provider_boundary_hash,
        "provider_request_hash": candidate.provider_request_hash,
        "instruction_dispatch_receipt_id": candidate.instruction_dispatch_receipt_id,
        "instruction_dispatch_receipt_hash": candidate.instruction_dispatch_receipt_hash,
        "context_pack_id": candidate.context_pack_id,
        "context_pack_hash": candidate.context_pack_hash,
        "operation_package_id": candidate.operation_package_id,
        "operation_package_hash": candidate.operation_package_hash,
        "project_id": candidate.project_id,
        "project_revision": candidate.project_revision,
        "project_fingerprint": candidate.project_fingerprint,
        "run_status_at_capture": RunStatus.INTERRUPTED,
        "candidate_state": "persisted",
        "candidate_output_id": candidate.candidate_output_id,
        "candidate_output_hash": candidate.envelope_hash,
        "remote_result": "unknown",
        "provider_replay_count": 0,
        "dossier_revision": 1,
        "created_at": datetime(2026, 7, 22, 1, tzinfo=UTC),
    }
    dossier_draft = ProviderRecoveryDossier.model_construct(
        **dossier_material,
        dossier_hash="0" * 64,
    )
    dossier = ProviderRecoveryDossier(
        **dossier_material,
        dossier_hash=provider_recovery_dossier_hash(dossier_draft),
    )
    decision = None
    if decided:
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
                action="adopt_persisted_candidate",
                previous_dossier_revision=1,
            ),
            "dossier_id": dossier.dossier_id,
            "run_id": dossier.run_id,
            "previous_dossier_hash": dossier.dossier_hash,
            "instruction_dispatch_receipt_id": (
                dossier.instruction_dispatch_receipt_id
            ),
            "instruction_dispatch_receipt_hash": (
                dossier.instruction_dispatch_receipt_hash
            ),
            "action": "adopt_persisted_candidate",
            "previous_status": "pending",
            "resulting_run_status": RunStatus.SUCCEEDED,
            "previous_dossier_revision": 1,
            "new_dossier_revision": 2,
            "created_at": datetime(2026, 7, 22, 2, tzinfo=UTC),
        }
        decision_draft = ProviderRecoveryDecision.model_construct(
            **decision_material,
            decision_hash="0" * 64,
        )
        decision = ProviderRecoveryDecision(
            **decision_material,
            decision_hash=provider_recovery_decision_hash(decision_draft),
        )
    return ProviderRecoveryResponse(dossier=dossier, decision=decision)


def test_agent_api_stream_replays_from_last_event_id(settings: RuntimeSettings) -> None:
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        bind_project(client)
        assert client.get("/api/v1/agent/status").json()["available"] is True
        session_id = create_session(client)
        run_id = create_run(client, session_id)
        run = wait_for_success(client, run_id)
        assert run["status"] == "succeeded", run["error"]

        with client.stream("GET", f"/api/v1/agent/runs/{run_id}/events") as response:
            assert response.status_code == 200
            lines = list(response.iter_lines())
        events = parse_sse(lines)
        assert events[-1]["event_type"] == "done"
        activity_events = [event for event in events if event["event_type"] == "activity"]
        assert len(activity_events) >= 7
        cursor = str(activity_events[3]["event_id"])

        with client.stream(
            "GET",
            f"/api/v1/agent/runs/{run_id}/events",
            headers={"Last-Event-ID": cursor},
        ) as response:
            replayed = parse_sse(list(response.iter_lines()))
        replayed_activities = [
            event for event in replayed if event["event_type"] == "activity"
        ]
        assert replayed_activities
        assert all(int(str(event["event_id"]).rpartition(":")[2]) > int(cursor.rpartition(":")[2]) for event in replayed_activities)
        assert replayed[-1]["event_type"] == "done"

        messages = client.get(
            f"/api/v1/agent/sessions/{session_id}/messages"
        ).json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == "第一段第二段"


def test_agent_api_is_explicitly_unavailable_without_provider(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        bind_project(client)
        status_payload = client.get("/api/v1/agent/status").json()
        assert status_payload["available"] is False
        response = client.post(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
            json={"title": "Writing Agent"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "agent_unavailable"
        assert client.get(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions"
        ).json()["sessions"] == []


def test_agent_api_manages_persisted_conversation_history(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        bind_project(client)
        session_id = create_session(client)

        updated = client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={"title": "  第一章重写  ", "pinned": True},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["title"] == "第一章重写"
        assert updated.json()["pinned"] is True

        listed = client.get(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions"
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["sessions"][0]["session_id"] == session_id

        removed = client.delete(f"/api/v1/agent/sessions/{session_id}")
        assert removed.status_code == 204, removed.text
        assert client.get(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions"
        ).json()["sessions"] == []
        assert client.get(
            f"/api/v1/agent/sessions/{session_id}"
        ).json()["status"] == "closed"

        empty_update = client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={},
        )
        assert empty_update.status_code == 422


def test_agent_api_uses_workbench_provider_settings_without_restart(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        bind_project(client)
        assert client.get("/api/v1/agent/status").json()["available"] is False

        secret = "workbench-only-secret"
        saved = client.put(
            "/api/v1/settings/provider",
            json={
                "base_url": "https://provider.example/v1",
                "model": "author-selected-model",
                "api_key": secret,
                "reasoning_effort": "high",
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
        assert saved.json()["reasoning_effort"] == "high"
        assert secret not in saved.text

        status_response = client.get("/api/v1/agent/status")
        assert status_response.status_code == 200, status_response.text
        status_payload = status_response.json()
        assert status_payload["available"] is True
        assert status_payload["executor"]["executor_id"] == (
            "executor.openai-compatible"
        )
        assert secret not in status_response.text

        session = client.post(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
            json={"title": "Writing Agent"},
        )
        assert session.status_code == 201, session.text
        assert session.json()["executor"] == status_payload["executor"]
        session_id = str(session.json()["session_id"])

        changed = client.put(
            "/api/v1/settings/provider",
            json={"reasoning_effort": "low"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["reasoning_effort"] == "low"
        changed_executor = client.get("/api/v1/agent/status").json()["executor"]
        assert changed_executor != status_payload["executor"]

        # 切换模型配置不是新开聊天：当前会话只更新下一次运行的 Provider 绑定。
        rebound = client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={"rebind_provider": True},
        )
        assert rebound.status_code == 200, rebound.text
        assert rebound.json()["session_id"] == session_id
        assert rebound.json()["executor"] == changed_executor
        sessions = client.get(f"/api/v1/projects/{PROJECT_ID}/agent/sessions")
        assert sessions.status_code == 200, sessions.text
        assert [item["session_id"] for item in sessions.json()["sessions"]] == [session_id]


def test_agent_api_keeps_model_preference_on_each_conversation(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        bind_project(client)
        first_settings = client.put(
            "/api/v1/settings/provider",
            json={
                "base_url": "https://provider.example/v1",
                "model": "model-for-first-chat",
                "api_key": "conversation-scoped-secret",
                "reasoning_effort": "high",
            },
        )
        assert first_settings.status_code == 200, first_settings.text
        first = client.post(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
            json={"title": "第一条对话"},
        )
        assert first.status_code == 201, first.text

        second_settings = client.put(
            "/api/v1/settings/provider",
            json={
                "model": "model-for-second-chat",
                "reasoning_effort": "low",
            },
        )
        assert second_settings.status_code == 200, second_settings.text
        second = client.post(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
            json={"title": "第二条对话"},
        )
        assert second.status_code == 201, second.text

        first_id = str(first.json()["session_id"])
        second_id = str(second.json()["session_id"])
        switched = client.patch(
            f"/api/v1/agent/sessions/{first_id}",
            json={
                "rebind_provider": True,
                "model": "model-for-first-chat-next",
                "reasoning_effort": "xhigh",
            },
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["session_id"] == first_id

        sessions = client.get(f"/api/v1/projects/{PROJECT_ID}/agent/sessions")
        assert sessions.status_code == 200, sessions.text
        by_id = {item["session_id"]: item for item in sessions.json()["sessions"]}
        first_preference = by_id[first_id]["capabilities"]["extensions"][
            "x-zhimeng.provider-preference"
        ]["config"]
        second_preference = by_id[second_id]["capabilities"]["extensions"][
            "x-zhimeng.provider-preference"
        ]["config"]
        assert first_preference == {
            "provider_id": "default",
            "model": "model-for-first-chat-next",
            "reasoning_effort": "xhigh",
        }
        assert second_preference == {
            "provider_id": "default",
            "model": "model-for-second-chat",
            "reasoning_effort": "low",
        }


def test_agent_api_switches_saved_provider_without_opening_a_new_conversation(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        bind_project(client)
        first = client.put(
            "/api/v1/settings/providers",
            json={
                "provider_id": "primary-line",
                "name": "主力线路",
                "base_url": "https://primary.example/v1",
                "model": "primary-writer",
                "selected_models": ["primary-writer", "primary-reader"],
                "api_key": "primary-secret",
                "reasoning_effort": "high",
                "make_active": True,
            },
        )
        assert first.status_code == 200, first.text
        second = client.put(
            "/api/v1/settings/providers",
            json={
                "provider_id": "backup-line",
                "name": "备用线路",
                "base_url": "https://backup.example/v1",
                "model": "backup-writer",
                "selected_models": ["backup-writer"],
                "api_key": "backup-secret",
                "reasoning_effort": "auto",
                "make_active": False,
            },
        )
        assert second.status_code == 200, second.text

        catalog = client.get("/api/v1/settings/providers")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["active_provider_id"] == "primary-line"
        assert [item["provider_id"] for item in catalog.json()["providers"]] == [
            "primary-line",
            "backup-line",
        ]
        assert "primary-secret" not in catalog.text
        assert "backup-secret" not in catalog.text

        created = client.post(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions",
            json={"title": "同一条对话换线路"},
        )
        assert created.status_code == 201, created.text
        session_id = str(created.json()["session_id"])
        assert created.json()["capabilities"]["extensions"][
            "x-zhimeng.provider-preference"
        ]["config"]["provider_id"] == "primary-line"

        switched = client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={
                "provider_id": "backup-line",
                "model": "backup-writer",
                "reasoning_effort": "low",
            },
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["session_id"] == session_id
        preference = switched.json()["capabilities"]["extensions"][
            "x-zhimeng.provider-preference"
        ]["config"]
        assert preference == {
            "provider_id": "backup-line",
            "model": "backup-writer",
            "reasoning_effort": "low",
        }
        assert client.get("/api/v1/settings/provider").json()["provider_id"] == "primary-line"


def test_agent_api_reports_unavailable_context_source_as_retryable_503(
    settings: RuntimeSettings,
) -> None:
    app = create_app(
        settings,
        agent_provider=ApiProvider(),
        distillation_adapter=UnavailableKnowledgeAdapter(),
    )
    with TestClient(app) as client:
        bind_project(client)
        client.app.state.agent_service.project_kb_service = CurrentProjectKbVerifier()
        session_id = create_session(client)
        request = ContextCreateRequest(
            objective="按已审阅知识继续。",
            profile="rewrite",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-api-source-unavailable",
            session_id=session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
            knowledge_fingerprint="d" * 64,
        )
        ContextStore(client.app.state.runtime.store).create_pack(pack)

        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "turn-api-source-unavailable"},
            json={
                "message": request.objective,
                "expected_project_revision": 1,
                "expected_project_fingerprint": FINGERPRINT,
                "active_chapter_id": None,
                "context_pack_id": pack.context_pack_id,
                "expected_context_pack_hash": pack.pack_hash,
            },
        )

        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "context_pack_source_unavailable"
        assert response.json()["error"]["retryable"] is True
        assert client.app.state.agent_service.store.list_runs(session_id) == []


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("project_kb_state_missing", True),
        ("project_kb_state_stale", True),
        ("project_kb_binding_changed", True),
        ("project_kb_knowledge_fingerprint_mismatch", True),
        ("project_kb_state_tampered", False),
        ("context_pack_project_kb_unbound", False),
    ],
)
def test_agent_api_preserves_project_kb_conflict_retryability(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    retryable: bool,
) -> None:
    app = create_app(settings, agent_provider=ApiProvider())

    async def reject_start_run(**_kwargs: object) -> None:
        raise AgentContextPackConflict("project KB gate rejected the run", code=code)

    with TestClient(app) as client:
        monkeypatch.setattr(
            client.app.state.agent_service,
            "start_run",
            reject_start_run,
        )
        response = client.post(
            "/api/v1/agent/sessions/session-project-kb-conflict/runs",
            headers={"Idempotency-Key": "project-kb-conflict-0001"},
            json={
                "message": "continue",
                "expected_project_revision": 1,
                "expected_project_fingerprint": FINGERPRINT,
                "active_chapter_id": None,
            },
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable


def test_agent_api_reports_provider_identity_drift_as_nonretryable_conflict(
    settings: RuntimeSettings,
) -> None:
    first_app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(first_app) as first_client:
        bind_project(first_client)
        session_id = create_session(first_client)

    second_app = create_app(settings, agent_provider=ChangedApiProvider())
    with TestClient(second_app) as second_client:
        response = second_client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "turn-provider-drift"},
            json={
                "message": "收紧开头",
                "expected_project_revision": 1,
                "expected_project_fingerprint": FINGERPRINT,
                "active_chapter_id": "chapter-001",
            },
        )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "workflow_executor_changed"
        assert response.json()["error"]["retryable"] is False
        assert second_client.app.state.agent_service.store.list_runs(session_id) == []


def test_agent_api_rebinds_provider_without_creating_a_new_session(
    settings: RuntimeSettings,
) -> None:
    first_app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(first_app) as first_client:
        bind_project(first_client)
        session_id = create_session(first_client)
        first_run = create_run(first_client, session_id)
        assert wait_for_success(first_client, first_run)["status"] == "succeeded"
        before_rebind_messages = first_client.app.state.agent_service.store.list_messages(
            session_id
        )
        assert [message.content for message in before_rebind_messages] == [
            "收紧开头",
            "第一段第二段",
        ]

    second_app = create_app(settings, agent_provider=ChangedApiProvider())
    with TestClient(second_app) as second_client:
        assert [
            message.content
            for message in second_client.app.state.agent_service.store.list_messages(
                session_id
            )
        ] == ["收紧开头", "第一段第二段"]

        rebound = second_client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={"rebind_provider": True},
        )

        assert rebound.status_code == 200, rebound.text
        assert rebound.json()["session_id"] == session_id
        assert rebound.json()["executor"]["instance_id"] == "provider-config-" + "b" * 64

        started = second_client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "turn-after-rebind"},
            json={
                "message": "继续当前对话",
                "expected_project_revision": 1,
                "expected_project_fingerprint": FINGERPRINT,
                "active_chapter_id": "chapter-001",
            },
        )
        assert started.status_code == 202, started.text
        assert started.json()["run"]["session_id"] == session_id
        assert wait_for_success(second_client, str(started.json()["run"]["run_id"]))[
            "status"
        ] == "succeeded"
        assert [
            message.content
            for message in second_client.app.state.agent_service.store.list_messages(
                session_id
            )
        ] == ["收紧开头", "第一段第二段", "继续当前对话", "第一段第二段"]


def test_agent_api_rejects_cursor_from_another_run(settings: RuntimeSettings) -> None:
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        bind_project(client)
        run_id = create_run(client, create_session(client))
        response = client.get(
            f"/api/v1/agent/runs/{run_id}/events",
            headers={"Last-Event-ID": "run_other:2"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "agent_event_cursor_invalid"


def test_agent_candidate_output_get_returns_the_strict_envelope(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    candidate = candidate_output()
    monkeypatch.setattr(
        app.state.agent_service.store,
        "get_agent_candidate_output",
        lambda run_id: candidate
        if run_id == candidate.run_id
        else pytest.fail("candidate GET forwarded the wrong Run ID"),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent/runs/{candidate.run_id}/candidate-output"
        )

    assert response.status_code == 200, response.text
    assert response.json() == candidate.model_dump(mode="json", by_alias=True)
    assert response.json()["formal_draft_allowed"] is False
    assert response.json()["requires_author_review"] is True


def test_agent_candidate_output_get_preserves_legacy_v1_without_fabricated_identity(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    candidate = legacy_candidate_output()
    monkeypatch.setattr(
        app.state.agent_service.store,
        "get_agent_candidate_output",
        lambda run_id: candidate
        if run_id == candidate.run_id
        else pytest.fail("candidate GET forwarded the wrong Run ID"),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent/runs/{candidate.run_id}/candidate-output"
        )

    assert response.status_code == 200, response.text
    assert response.json() == candidate.model_dump(mode="json", by_alias=True)
    assert "provider_turn_receipts" not in response.json()
    assert "provider_identity_status" not in response.json()


def test_agent_candidate_output_get_maps_missing_candidate_to_agent_404(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        bind_project(client)
        run_id = create_run(client, create_session(client))
        run = wait_for_success(client, run_id)
        assert run["status"] == "succeeded", run["error"]
        response = client.get(
            f"/api/v1/agent/runs/{run_id}/candidate-output"
        )

    assert response.status_code == 404, response.text
    assert response.json()["error"] == {
        "code": "agent_resource_not_found",
        "message": "Agent candidate output was not found",
        "request_id": response.headers["X-Request-ID"],
        "retryable": False,
    }


def test_agent_candidate_output_route_is_read_only(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    path = "/api/v1/agent/runs/{run_id}/candidate-output"
    operation = app.openapi()["paths"][path]

    assert set(operation) == {"get"}
    response_schema = operation["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {
        "anyOf": [
            {"$ref": "#/components/schemas/AgentCandidateOutput"},
            {"$ref": "#/components/schemas/AgentCandidateOutputV2"},
            {"$ref": "#/components/schemas/AgentCandidateOutputV1"},
        ],
        "title": "Response Get Candidate Output Api V1 Agent Runs  Run Id  Candidate Output Get",
    }

    with TestClient(app) as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.request(
                method,
                "/api/v1/agent/runs/run_candidate_api/candidate-output",
                json={},
            )
            assert response.status_code == 405, (method, response.text)


def test_provider_turn_receipts_get_returns_full_runtime_evidence(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    evidence = provider_turn_receipt_list()
    monkeypatch.setattr(
        app.state.agent_service.store,
        "get_provider_turn_receipt_list",
        lambda run_id: evidence
        if run_id == evidence.run_id
        else pytest.fail("receipt GET forwarded the wrong Run ID"),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/agent/runs/{evidence.run_id}/provider-turn-receipts"
        )

    assert response.status_code == 200, response.text
    assert response.json() == evidence.model_dump(mode="json", by_alias=True)
    assert response.json()["provider_identity_status"] == "verified"
    assert response.json()["provider_turn_receipts"][0]["reported_model"] == (
        "gateway-model"
    )


def test_provider_turn_receipts_route_is_read_only(
    settings: RuntimeSettings,
) -> None:
    app = create_app(settings)
    path = "/api/v1/agent/runs/{run_id}/provider-turn-receipts"
    operation = app.openapi()["paths"][path]

    assert set(operation) == {"get"}
    response_schema = operation["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {
        "$ref": "#/components/schemas/ProviderTurnReceiptListResponse"
    }

    with TestClient(app) as client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.request(
                method,
                "/api/v1/agent/runs/run_provider_receipt_api/provider-turn-receipts",
                json={},
            )
            assert response.status_code == 405, (method, response.text)


def test_provider_recovery_api_exposes_dossier_and_forwards_cas_decision(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    pending = provider_recovery_response()
    decided = provider_recovery_response(decided=True)
    captured: list[ProviderRecoveryDecisionRequest] = []
    service = app.state.agent_service
    service.project_kb_service = CurrentProjectKbVerifier()
    service.project_locks = None
    monkeypatch.setattr(
        service.store,
        "run_context_pack_knowledge_target",
        lambda run_id: ContextPackKnowledgeTarget(
            context_pack_id="context-pack-provider-recovery",
            pack_hash="a" * 64,
            knowledge_fingerprint=None,
            project_id=PROJECT_ID,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        if run_id == pending.dossier.run_id
        else pytest.fail("recovery gate forwarded the wrong Run ID"),
    )
    monkeypatch.setattr(
        service.store,
        "get_provider_recovery",
        lambda run_id: pending
        if run_id == pending.dossier.run_id
        else pytest.fail("recovery GET forwarded the wrong Run ID"),
    )

    def decide(
        run_id: str,
        request: ProviderRecoveryDecisionRequest,
        *,
        current_project_kb_verifier,
    ):
        assert run_id == pending.dossier.run_id
        current_project_kb_verifier(
            service.store.run_context_pack_knowledge_target(run_id)
        )
        captured.append(request)
        return decided

    monkeypatch.setattr(service.store, "decide_provider_recovery", decide)
    request = ProviderRecoveryDecisionRequest(
        expected_dossier_revision=pending.dossier.dossier_revision,
        expected_dossier_hash=pending.dossier.dossier_hash,
        action="adopt_persisted_candidate",
    )

    with TestClient(app) as client:
        get_response = client.get(
            f"/api/v1/agent/runs/{pending.dossier.run_id}/provider-recovery"
        )
        post_response = client.post(
            f"/api/v1/agent/runs/{pending.dossier.run_id}/provider-recovery/decision",
            json=request.model_dump(mode="json", by_alias=True),
        )

    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["dossier"]["provider_replay_count"] == 0
    assert post_response.status_code == 200, post_response.text
    assert post_response.json()["decision"]["action"] == "adopt_persisted_candidate"
    assert captured == [request]


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("project_kb_state_stale", True),
        ("project_kb_state_tampered", False),
    ],
)
def test_provider_recovery_adopt_fails_closed_before_decision(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    retryable: bool,
) -> None:
    app = create_app(settings)
    pending = provider_recovery_response()
    service = app.state.agent_service
    verifier = RejectingProjectKbVerifier(code)
    service.project_kb_service = verifier
    monkeypatch.setattr(
        service.store,
        "run_context_pack_knowledge_target",
        lambda run_id: ContextPackKnowledgeTarget(
            context_pack_id="context-pack-provider-recovery",
            pack_hash="a" * 64,
            knowledge_fingerprint=None,
            project_id=PROJECT_ID,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        if run_id == pending.dossier.run_id
        else pytest.fail("recovery gate forwarded the wrong Run ID"),
    )
    monkeypatch.setattr(
        service.store,
        "decide_provider_recovery",
        lambda *_args, **_kwargs: pytest.fail(
            "stale or tampered adoption must not persist a decision"
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/agent/runs/{pending.dossier.run_id}/provider-recovery/decision",
            json={
                "schema": "zhimeng.provider-recovery-decision-request.v1",
                "expected_dossier_revision": pending.dossier.dossier_revision,
                "expected_dossier_hash": pending.dossier.dossier_hash,
                "action": "adopt_persisted_candidate",
            },
        )

    assert verifier.calls == 1
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable


def test_provider_recovery_api_maps_stale_cas_to_nonretryable_409(
    settings: RuntimeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    pending = provider_recovery_response()
    service = app.state.agent_service
    verifier = RejectingProjectKbVerifier("project_kb_state_tampered")
    service.project_kb_service = verifier
    monkeypatch.setattr(
        service.store,
        "run_context_pack_knowledge_target",
        lambda *_args, **_kwargs: pytest.fail(
            "abandon_and_recompile must not require the old project KB binding"
        ),
    )

    def reject(*_args, **_kwargs):
        raise ProviderRecoveryConflict(
            "Provider recovery dossier changed",
            code="provider_recovery_stale",
        )

    monkeypatch.setattr(service.store, "decide_provider_recovery", reject)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/agent/runs/{pending.dossier.run_id}/provider-recovery/decision",
            json={
                "schema": "zhimeng.provider-recovery-decision-request.v1",
                "expected_dossier_revision": 1,
                "expected_dossier_hash": pending.dossier.dossier_hash,
                "action": "abandon_and_recompile",
            },
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "provider_recovery_stale"
    assert response.json()["error"]["retryable"] is False
    assert verifier.calls == 0


def test_unbound_casual_session_runs_pure_chat_without_project(
    settings: RuntimeSettings,
) -> None:
    """「随便聊」：不绑定小说项目的会话走同一个 Agent 核心，纯回答即完成。"""
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        created = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"})
        assert created.status_code == 201, created.text
        session_payload = created.json()
        assert session_payload["project_revision"] is None
        session_id = str(session_payload["session_id"])

        listed = client.get("/api/v1/agent/sessions").json()
        assert [item["session_id"] for item in listed["sessions"]] == [session_id]
        assert listed["sessions"][0]["project_revision"] is None

        # 项目会话清单不该混进闲聊会话。
        bind_project(client)
        project_sessions = client.get(
            f"/api/v1/projects/{PROJECT_ID}/agent/sessions"
        ).json()
        assert project_sessions["sessions"] == []

        started = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "casual-00000001"},
            json={"message": "聊聊怎么把开篇写得不拖沓"},
        )
        assert started.status_code == 202, started.text
        run_id = str(started.json()["run"]["run_id"])

        run = wait_for_success(client, run_id)
        assert run["status"] == "succeeded", run["error"]
        assert run["project_revision"] is None

        messages = client.get(
            f"/api/v1/agent/sessions/{session_id}/messages"
        ).json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == "第一段第二段"

        activities = client.get(f"/api/v1/agent/runs/{run_id}/activities").json()
        activity_items = activities.get("activities", [])
        assert activity_items
        assert all(item["project_revision"] is None for item in activity_items)

        # 会话改名（rebind/更名路由按会话寻址）照常可用。
        renamed = client.patch(
            f"/api/v1/agent/sessions/{session_id}", json={"title": "灵感草稿"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "灵感草稿"


class RecordingApiProvider(ApiProvider):
    """记录每次 provider 请求拿到的消息与工具名，供断言提示词装配。"""

    def __init__(self) -> None:
        self.requests: list[tuple[list[dict[str, object]], list[str]]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append(
            ([dict(message) for message in messages], [tool.name for tool in tools])
        )
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def test_casual_chat_can_consult_the_authors_skill_folders(
    settings: RuntimeSettings,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """随便聊挂着技能翻阅工具，但不再往提示词里塞技能菜单——模型想用自己去发现。"""
    skills_root = tmp_path / "author-skills"
    folder = skills_root / "网文知识库"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: 网文知识库\n"
        "description: 网文写作机制卡库，玄幻开篇钩子、场景写法、书名章名都能查\n---\n\n"
        "# 网文知识库\n\n先 skill_search 再 skill_read_file。\n",
        encoding="utf-8",
    )
    (folder / "类型规律").mkdir()
    (folder / "类型规律" / "玄幻开篇.md").write_text("# 玄幻开篇\n\n钩子前置。\n", encoding="utf-8")
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(skills_root))
    provider = RecordingApiProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        created = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"})
        assert created.status_code == 201, created.text
        session_id = str(created.json()["session_id"])
        started = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "casual-skills-00001"},
            json={"message": "查一下网文知识库：玄幻开篇怎么写钩子", "writer_mode": "author_review_change_proposal"},
        )
        assert started.status_code == 202, started.text
        run = wait_for_success(client, str(started.json()["run"]["run_id"]))
        assert run["status"] == "succeeded", run["error"]

    assert provider.requests
    messages, tool_names = provider.requests[0]
    # 随便聊（完全访问）：技能翻阅 + 电脑上的读写与执行命令 + 记偏好 + 上网抓网页 / 搜网页
    # + 列工作计划；写文件工具 9-26 起不打开文件夹也给（纠偏指令 3.3），
    # 只认打开的项目的列目录 / 读 / 搜不挂。只读挡位另见 test_casual_chat_tier_20260926.py。
    # AI 味打分随技能来（纠偏指令 3.8）：这里唯一的技能没在 allowed-tools 里声明它，就不给。
    assert sorted(tool_names) == sorted(
        SKILL_FILE_TOOL_NAMES | COMPUTER_TOOL_NAMES | COMMAND_TOOL_NAMES | MEMORY_TOOL_NAMES | WEB_TOOL_NAMES | PLAN_TOOL_NAMES | GOAL_TOOL_NAMES | DIRECT_WRITE_TOOL_NAMES | HISTORY_TOOL_NAMES
    )
    assert not AI_SMELL_TOOL_NAMES & set(tool_names)
    system_messages = [str(message.get("content", "")) for message in messages if message.get("role") == "system"]
    # 作者 2026-09-20：随便聊不塞内部提示词——不发场景说明，也不发技能菜单。
    # 弱模型会把塞进去的系统提示原文念出来当答案；技能靠工具按需发现，不靠菜单硬塞。
    # 唯一的系统消息是开头那段环境事实（纠偏指令 3.2），一行一条「键：值」。
    assert len(system_messages) == 1
    assert [line.split("：", 1)[0] for line in system_messages[0].splitlines()] == [
        "系统", "用户主目录", "当前文件夹", "今天", "挡位"
    ]
    assert "已启用的技能" not in system_messages[0]
    assert "网文知识库" not in system_messages[0]


def _run_request_messages(
    provider: RecordingApiProvider,
    user_text: str,
) -> list[dict[str, object]]:
    """从记录里挑出某一个 Run 的首轮请求（自动起名也走 provider，按用户消息原文区分）。"""

    for messages, _tool_names in provider.requests:
        if any(message.get("content") == user_text for message in messages):
            return messages
    raise AssertionError(f"没有找到装着 {user_text!r} 的 provider 请求")


def test_casual_chat_with_manual_skill_names_gets_one_pointer_message(
    settings: RuntimeSettings,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """作者在输入框用 / 点名技能（引用标签）：这一条 Run 多发一条指路消息，
    告诉模型用 skill_read_file 去读点名的技能；没点名的下一条照旧零系统提示。"""
    skills_root = tmp_path / "author-skills"
    folder = skills_root / "网文知识库"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: 网文知识库\n"
        "description: 网文写作机制卡库，玄幻开篇钩子、场景写法、书名章名都能查\n---\n\n"
        "# 网文知识库\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(skills_root))
    provider = RecordingApiProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        created = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"})
        assert created.status_code == 201, created.text
        session_id = str(created.json()["session_id"])
        started = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "manual-skill-00001"},
            json={
                "message": "玄幻开篇怎么写钩子",
                "manual_skill_names": ["网文知识库"],
            },
        )
        assert started.status_code == 202, started.text
        run = wait_for_success(client, str(started.json()["run"]["run_id"]))
        assert run["status"] == "succeeded", run["error"]

        follow_up = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "manual-skill-00002"},
            json={"message": "那第二章怎么接"},
        )
        assert follow_up.status_code == 202, follow_up.text
        second = wait_for_success(client, str(follow_up.json()["run"]["run_id"]))
        assert second["status"] == "succeeded", second["error"]

    tagged = _run_request_messages(provider, "玄幻开篇怎么写钩子")
    system_text = "\n".join(
        str(message.get("content", "")) for message in tagged if message.get("role") == "system"
    )
    assert "网文知识库" in system_text
    assert "skill_read_file" in system_text

    untagged = _run_request_messages(provider, "那第二章怎么接")
    # 开头那段环境事实和第一条的一字不差。
    assert untagged[0] == tagged[0] and str(untagged[0]["content"]).startswith("系统：")
    # 9-28 起上一轮发给模型的那一段原样带进历史（ZCode 档案 12.2）：点名的指路消息跟在第一句话后面、只出现一次，
    # 第二句话后面不再补。
    pointers = [index for index, message in enumerate(untagged) if "skill_read_file" in str(message.get("content", ""))]
    asked = [index for index, message in enumerate(untagged) if message.get("content") == "那第二章怎么接"]
    assert len(pointers) == 1 and len(asked) == 1 and pointers[0] < asked[0]
    assert untagged[pointers[0] - 1].get("content") == "玄幻开篇怎么写钩子"


def test_book_setup_chat_in_read_only_stays_a_conversation(
    settings: RuntimeSettings,
) -> None:
    """开书聊天在「只问不改」挡位不再炸 writer_task_mode_invalid：
    降级为纯对话（摘掉提案/直接写/命令工具，只留只读翻阅），正常给出回答。"""
    provider = RecordingApiProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        bind_project(client)
        client.app.state.agent_service.project_kb_service = CurrentProjectKbVerifier()
        session_id = create_session(client)
        request = ContextCreateRequest(
            objective="我有个脑洞，帮我开一本新书。",
            profile="chat_book_setup",
            active_chapter_id=None,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-api-book-setup-read-only",
            session_id=session_id,
            project_id=PROJECT_ID,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
        )
        ContextStore(client.app.state.runtime.store).create_pack(pack)

        started = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "book-setup-read-only-0001"},
            json={
                "message": request.objective,
                "expected_project_revision": 1,
                "expected_project_fingerprint": FINGERPRINT,
                "active_chapter_id": None,
                "writer_mode": "repair_only",
                "context_pack_id": pack.context_pack_id,
                "expected_context_pack_hash": pack.pack_hash,
            },
        )
        assert started.status_code == 202, started.text
        run = wait_for_success(client, str(started.json()["run"]["run_id"]))
        assert run["status"] == "succeeded", run["error"]

    assert provider.requests
    messages, tool_names = provider.requests[0]
    # 只读挡位：提案、直接写、命令工具全部摘掉，只读翻阅仍在。
    assert "change_set_propose" not in tool_names
    assert "write_file" not in tool_names
    assert "edit_file" not in tool_names
    assert not (set(COMMAND_TOOL_NAMES) & set(tool_names))
    assert "skill_execute" not in tool_names
    assert "read_file" in tool_names
    assert "list_files" in tool_names
    system_text = "\n".join(
        str(message.get("content", "")) for message in messages if message.get("role") == "system"
    )
    assert "只读" in system_text
    assert "writer_task_mode_invalid" not in system_text



class BookSetupProposalProvider(ApiProvider):
    """开书聊天（动手改挡位）：第一轮提一个 5 份新建的提案，第二轮正常回答。

    5 份里带着旧闸门会拒的每一种：超过 3 份、非 .md、层深超过 4、落在正文夹。
    旧闸门已删（2026-09-20 P2 第 5 条），这个提案应当照常成立。
    """

    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.turns = 0

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.requests.append([dict(message) for message in messages])
        if any(
            message.get("role") == "system" and message.get("content") == _SESSION_TITLE_PROMPT
            for message in messages
        ):
            yield ProviderTextDelta("测试会话起名")
            yield ProviderCompleted("stop", None)
            return
        self.turns += 1
        if self.turns == 1:
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_book_setup_propose_0001",
                tool_name="change_set_propose",
                arguments={
                    "title": "开书五件套",
                    "summary": "一次聊出定位、世界观、人物、大纲与第一章",
                    "changes": [
                        {
                            "operation": "create",
                            "relative_path": "01_项目定位/项目卡片.md",
                            "after_content": "# 项目卡片\n\n一个破宗门只剩七个人。\n",
                            "rationale": "作者已确认故事起点",
                        },
                        {
                            "operation": "create",
                            "relative_path": "02_世界观设定/世界.md",
                            "after_content": "# 世界\n\n低武宗门世界。\n",
                            "rationale": "作者已确认世界基调",
                        },
                        {
                            "operation": "create",
                            "relative_path": "03_人物设定/主角.md",
                            "after_content": "# 主角\n\n接手破宗门。\n",
                            "rationale": "作者已确认主角身份",
                        },
                        {
                            "operation": "create",
                            "relative_path": "04_剧情大纲/卷一/细纲/前三十章/节奏表.txt",
                            "after_content": "前三十章节奏\n",
                            "rationale": "作者要一份纯文本节奏表",
                        },
                        {
                            "operation": "create",
                            "relative_path": "05_正文内容/第一卷/CH001.md",
                            "after_content": "# 第一章\n\n正文。\n",
                            "rationale": "作者要求直接开写第一章",
                        },
                    ],
                },
            )
            yield ProviderCompleted("tool_calls", None)
            return
        yield ProviderTextDelta("五份资料已经整理好，等你确认。")
        yield ProviderCompleted("stop", None)


class _AnyProjectKbVerifier:
    """真实 inspect 出来的项目 id 不是常量 PROJECT_ID，KB 校验双倍放宽到任意项目。"""

    def require_current_binding(
        self,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(knowledge_fingerprint=expected_knowledge_fingerprint)

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        yield self.require_current_binding(project_id, **kwargs)


def test_book_setup_chat_proposal_is_no_longer_count_depth_or_folder_gated(
    tmp_path: Path,
) -> None:
    """开书链新建限制整段删除后的端到端证据：一次提 5 份新建（超 3 份、
    含 .txt、层深 5、含正文夹）照常成立，提案等作者点头，不落盘。"""
    trusted_root = tmp_path / "library"
    book = trusted_root / "开书测试"
    book.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (book / f"{index:02d}_{label}").mkdir()
    (book / "05_正文内容" / "第一卷").mkdir(parents=True)

    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(trusted_root,),
        allowed_origins=(),
        sse_heartbeat_seconds=0.05,
    )
    provider = BookSetupProposalProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(book)})
        assert inspected.status_code == 200, inspected.text
        payload = inspected.json()
        project_id = str(payload["project"]["project_id"])
        revision = payload["project"]["revision"]
        fingerprint = str(payload["project"]["fingerprint"])
        client.app.state.agent_service.project_kb_service = _AnyProjectKbVerifier()
        session_response = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "开新书"},
        )
        assert session_response.status_code == 201, session_response.text
        session_id = str(session_response.json()["session_id"])
        request = ContextCreateRequest(
            objective="我有个脑洞，帮我开一本新书。",
            profile="chat_book_setup",
            active_chapter_id=None,
            expected_project_revision=revision,
            expected_project_fingerprint=fingerprint,
            project_kb_state_id=PROJECT_KB_STATE_ID,
            project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
        )
        pack = compile_context_pack(
            context_pack_id="context-pack-api-book-setup-ungated",
            session_id=session_id,
            project_id=project_id,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=(),
            max_characters=20_000,
            max_tokens=20_000,
            created_at=datetime.now(UTC),
        )
        ContextStore(client.app.state.runtime.store).create_pack(pack)

        started = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "book-setup-ungated-0001"},
            json={
                "message": request.objective,
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
                "active_chapter_id": None,
                "writer_mode": "author_review_change_proposal",
                "context_pack_id": pack.context_pack_id,
                "expected_context_pack_hash": pack.pack_hash,
            },
        )
        assert started.status_code == 202, started.text
        run = wait_for_success(client, str(started.json()["run"]["run_id"]))
        assert run["status"] == "succeeded", run["error"]

    # 第二轮的消息里带着 change_set_propose 的成功结果：提案成立，等待作者确认。
    assert len(provider.requests) >= 2
    tool_results = [
        message
        for message in provider.requests[1]
        if message.get("role") == "tool"
    ]
    assert len(tool_results) == 1
    content = str(tool_results[0].get("content"))
    assert '"status":"succeeded"' in content
    assert "change_set" in content
    # 提案不落盘：作者点头前，书里一个文件都不该多。
    created_on_disk = [path for path in book.rglob("*") if path.is_file()]
    assert created_on_disk == []


def test_accept_reported_model_endpoint_adds_alias_to_active_profile(
    settings: RuntimeSettings,
) -> None:
    """失败卡「认可这个名字」的端点：把上报名加进当前在用那套模型服务，
    响应里看不到密钥；没配服务时 400 而不是 500。"""
    app = create_app(settings)
    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/settings/providers",
            json={
                "provider_id": "primary-line",
                "name": "主力线路",
                "base_url": "https://primary.example/v1",
                "model": "primary-writer",
                "selected_models": ["primary-writer"],
                "api_key": "primary-secret",
                "reasoning_effort": "high",
                "make_active": True,
            },
        )
        assert saved.status_code == 200, saved.text

        accepted = client.post(
            "/api/v1/settings/provider/accept-reported-model",
            json={"reported_model": "primary-writer-0815"},
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert payload["already_accepted"] is False
        assert payload["reported_model"] == "primary-writer-0815"
        assert payload["active_settings"]["accepted_reported_models"] == [
            "primary-writer-0815"
        ]
        assert "primary-secret" not in accepted.text

        again = client.post(
            "/api/v1/settings/provider/accept-reported-model",
            json={"reported_model": "primary-writer-0815"},
        )
        assert again.status_code == 200, again.text
        assert again.json()["already_accepted"] is True

    empty_app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=settings.database_path.parent / "elsewhere" / "other.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(empty_app) as client:
        missing = client.post(
            "/api/v1/settings/provider/accept-reported-model",
            json={"reported_model": "anything"},
        )
        assert missing.status_code == 400, missing.text
