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
    ProviderToolDefinition,
)
from zhimeng_runtime.app import create_app
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
    for _ in range(100):
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
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
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
