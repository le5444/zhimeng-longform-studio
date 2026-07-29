from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings
from zhimeng_runtime.distillation_adapter import DistillationBusyError
from zhimeng_runtime.distillation_invocation_service import (
    build_distillation_tool_definitions,
    distillation_provider_tool_definitions_hash,
)
from zhimeng_runtime.distillation_models import (
    DistillationCoverageRequest,
    DistillationCoverageResponse,
    DistillationHealthResponse,
)
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderToolCall
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id
from test_context_agent_e2e import _write_project
from test_source_import_api import FakeTomatoAdapter, _wait_for_status


store_fixtures = importlib.import_module("test_distillation_invocation_store")
model_fixtures = importlib.import_module("test_distillation_invocation_models")
service_fixtures = importlib.import_module("test_distillation_invocation_service")


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
    )


def _health() -> DistillationHealthResponse:
    return DistillationHealthResponse(
        health="degraded",
        structural_health="healthy",
        validation_maturity="risk_ready",
        card_count=7,
        counts={"work": 4, "scene": 3},
        validation_ladder_debt={"risk_ready_cards": 3, "warn_steps": 12},
        hard_rule_eligible_counts={"work": 4},
        library_fingerprint="a" * 64,
        tool_fingerprint="b" * 64,
        observed_at=datetime.now(UTC),
    )


def _blocked_coverage(
    request: DistillationCoverageRequest,
) -> DistillationCoverageResponse:
    return DistillationCoverageResponse(
        query=request.query,
        profile=request.profile,
        target_health="blocked",
        writing_gate="pause_and_request_sources",
        recommended_next_step="ask_user_for_same_type_sources",
        risk_preview_allowed=False,
        formal_injection_allowed=False,
        source_coverage_ratio=0.0,
        formal_coverage_ratio=0.0,
        matched_cards=0,
        available_matched_cards=0,
        selected_type_counts={},
        selected_hard_rule_type_counts={},
        selected_risk_ready_type_counts={},
        source_quota_gaps={"scene": 2},
        formal_quota_gaps={"scene": 2},
        missing_types=["scene"],
        formal_missing_types=["scene"],
        selected_cards=[],
        feed_request="请补同路线来源。",
        library_fingerprint="a" * 64,
        tool_fingerprint="b" * 64,
        observed_at=datetime.now(UTC),
    )


class FakeDistillationAdapter:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.requests: list[DistillationCoverageRequest] = []

    def health(self) -> DistillationHealthResponse:
        if self.busy:
            raise DistillationBusyError("fixture busy")
        return _health()

    def coverage(
        self,
        request: DistillationCoverageRequest,
    ) -> DistillationCoverageResponse:
        self.requests.append(request)
        return _blocked_coverage(request)


def test_injected_adapter_exposes_health_and_blocked_coverage_as_domain_response(
    tmp_path: Path,
) -> None:
    adapter = FakeDistillationAdapter()
    with TestClient(
        create_app(_settings(tmp_path), distillation_adapter=adapter)
    ) as client:
        health = client.get("/api/v1/distillation/health")
        coverage = client.post(
            "/api/v1/distillation/coverage",
            json={"query": "缺少的路线", "profile": "scene"},
        )

    assert health.status_code == 200, health.text
    assert health.json()["health"] == "degraded"
    assert coverage.status_code == 200, coverage.text
    assert coverage.json()["target_health"] == "blocked"
    assert coverage.json()["risk_preview_allowed"] is False
    assert coverage.json()["formal_injection_allowed"] is False
    assert adapter.requests[0].query == "缺少的路线"


def test_public_coverage_contract_rejects_path_top_and_option_like_query(
    tmp_path: Path,
) -> None:
    adapter = FakeDistillationAdapter()
    with TestClient(
        create_app(_settings(tmp_path), distillation_adapter=adapter)
    ) as client:
        path_response = client.post(
            "/api/v1/distillation/coverage",
            json={
                "query": "目标",
                "profile": "scene",
                "library": "C:/outside",
            },
        )
        top_response = client.post(
            "/api/v1/distillation/coverage",
            json={"query": "目标", "profile": "scene", "top": 9999},
        )
        option_response = client.post(
            "/api/v1/distillation/coverage",
            json={"query": "--json", "profile": "scene"},
        )

    assert path_response.status_code == 422
    assert top_response.status_code == 422
    assert option_response.status_code == 422
    assert adapter.requests == []


def test_unconfigured_adapter_and_busy_adapter_have_stable_errors(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        unavailable = client.get("/api/v1/distillation/health")
    with TestClient(
        create_app(
            _settings(tmp_path / "busy"),
            distillation_adapter=FakeDistillationAdapter(busy=True),
        )
    ) as client:
        busy = client.get("/api/v1/distillation/health")

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "distillation_unavailable"
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "distillation_preview_busy"
    assert busy.json()["error"]["retryable"] is True


def test_runtime_owned_invocation_api_persists_prepared_and_append_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chain = _runtime_chain(monkeypatch)
    app = create_app(_settings(tmp_path), distillation_adapter=FakeDistillationAdapter())
    with TestClient(app) as client:
        store_fixtures._seed_static_bindings(app.state.runtime.store, chain)
        prepared = client.post(
            "/api/v1/distillation/invocations/prepared",
            json={
                "idempotency_key": store_fixtures.IDEMPOTENCY_KEY,
                "receipt": chain["prepared"].model_dump(mode="json", by_alias=True),
            },
        )
        assert prepared.status_code == 200, prepared.text
        payload = prepared.json()
        assert payload["invocation"]["status"] == "prepared"
        assert [item["kind"] for item in payload["receipts"]] == ["prepared"]

        appended = client.post(
            f"/api/v1/distillation/invocations/{chain['catalog'].invocation_id}/receipts",
            json={
                "receipt": chain["dispatch1"].model_dump(mode="json", by_alias=True),
            },
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["invocation"]["status"] == "running"

        ledger = client.get(
            f"/api/v1/distillation/invocations/{chain['catalog'].invocation_id}"
        )
        assert ledger.status_code == 200, ledger.text
        assert [item["kind"] for item in ledger.json()["receipts"]] == [
            "prepared",
            "provider_dispatch",
        ]


def test_runtime_owned_invocation_api_runs_provider_loop_to_three_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chain = _runtime_chain(monkeypatch)
    catalog = chain["catalog"]
    submission = model_fixtures._submission(catalog.anchors[0].anchor_id)
    provider = service_fixtures.ScriptedDistillationProvider(
        [
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="provider_call_read_0001",
                    tool_name="distillation_anchor_read",
                    arguments={
                        "schema": "zhimeng.distillation-anchor-read-request.v1",
                        "anchor_ids": [catalog.anchors[0].anchor_id],
                    },
                ),
                ProviderCompleted("tool_calls", None, "gateway-model"),
            ],
            [
                ProviderToolCall(
                    index=0,
                    tool_call_id="provider_call_submit_0002",
                    tool_name="distillation_output_submit",
                    arguments=submission.model_dump(mode="json", by_alias=True),
                ),
                ProviderCompleted("tool_calls", None, "gateway-model"),
            ],
        ]
    )
    app = create_app(
        _settings(tmp_path),
        distillation_adapter=FakeDistillationAdapter(),
        agent_provider=provider,
    )
    with TestClient(app) as client:
        store_fixtures._seed_static_bindings(app.state.runtime.store, chain)
        prepared = client.post(
            "/api/v1/distillation/invocations/prepared",
            json={
                "idempotency_key": store_fixtures.IDEMPOTENCY_KEY,
                "receipt": chain["prepared"].model_dump(mode="json", by_alias=True),
            },
        )
        assert prepared.status_code == 200, prepared.text

        executed = client.post(
            f"/api/v1/distillation/invocations/{catalog.invocation_id}/run",
            json={
                "anchor_catalog": catalog.model_dump(mode="json", by_alias=True),
                "text_by_anchor_id": {
                    catalog.anchors[0].anchor_id: "alpha",
                    catalog.anchors[1].anchor_id: "beta",
                },
            },
        )

    assert executed.status_code == 200, executed.text
    payload = executed.json()
    assert payload["terminal_reason"] == "succeeded"
    assert payload["invocation"]["status"] == "succeeded"
    assert [item["kind"] for item in payload["receipts"]] == [
        "prepared",
        "provider_dispatch",
        "provider_turn_result",
        "provider_dispatch",
        "provider_turn_result",
        "candidate_output",
        "final_result",
    ]


def test_source_import_prepare_creates_runtime_owned_invocation_and_can_run(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project_root = library / "novel"
    _write_project(project_root)
    provider = service_fixtures.ScriptedDistillationProvider([])
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            managed_skill_root=DEFAULT_MANAGED_SKILL_ROOT,
            allowed_origins=(),
        ),
        source_import_adapter=FakeTomatoAdapter(),
        distillation_adapter=FakeDistillationAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        assert inspection.status_code == 200, inspection.text
        project = inspection.json()["project"]

        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "tomato",
                "query": "Runtime 深蒸馏测试书",
                "candidate": {
                    "index": 1,
                    "book_id": "2001",
                    "title": "Runtime 深蒸馏测试书",
                    "author": "作者甲",
                },
            },
        )
        assert created.status_code == 202, created.text
        source_import_id = created.json()["import_id"]
        finished = _wait_for_status(client, source_import_id, "awaiting_review")
        assert finished["status"] == "awaiting_review"

        legacy_receipt = client.get(
            f"/api/v1/source-imports/{source_import_id}/execution-receipt"
        ).json()["receipt"]
        assert legacy_receipt["distillation_invocation_id"].startswith(
            "source-import-invocation-"
        )

        prepared = client.post(
            f"/api/v1/source-imports/{source_import_id}/distillation-invocation/prepare",
            json={
                "project_id": project["project_id"],
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
            },
        )
        assert prepared.status_code == 200, prepared.text
        prepared_payload = prepared.json()
        snapshot = prepared_payload["snapshot"]
        invocation_id = snapshot["invocation"]["invocation_id"]
        assert invocation_id.startswith("distillation-invocation-")
        assert snapshot["invocation"]["status"] == "prepared"
        assert snapshot["invocation"]["provider_replay_count"] == 0
        assert [receipt["kind"] for receipt in snapshot["receipts"]] == ["prepared"]
        assert (
            snapshot["receipts"][0]["payload"]["source"]["source_receipt_id"]
            == legacy_receipt["receipt_id"]
        )
        assert prepared_payload["anchor_catalog"]["invocation_id"] == invocation_id
        assert prepared_payload["text_by_anchor_id"]

        again = client.post(
            f"/api/v1/source-imports/{source_import_id}/distillation-invocation/prepare",
            json={"project_id": project["project_id"]},
        )
        assert again.status_code == 200, again.text
        assert again.json()["snapshot"]["invocation"]["invocation_id"] == invocation_id

        anchor_id = prepared_payload["anchor_catalog"]["anchors"][0]["anchor_id"]
        submission = model_fixtures._submission(anchor_id)
        provider.turns.extend(
            [
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="provider_call_read_0001",
                        tool_name="distillation_anchor_read",
                        arguments={
                            "schema": "zhimeng.distillation-anchor-read-request.v1",
                            "anchor_ids": [anchor_id],
                        },
                    ),
                    ProviderCompleted("tool_calls", None, None),
                ],
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="provider_call_submit_0002",
                        tool_name="distillation_output_submit",
                        arguments=submission.model_dump(mode="json", by_alias=True),
                    ),
                    ProviderCompleted("tool_calls", None, None),
                ],
            ]
        )
        executed = client.post(
            f"/api/v1/distillation/invocations/{invocation_id}/run",
            json={
                "anchor_catalog": prepared_payload["anchor_catalog"],
                "text_by_anchor_id": prepared_payload["text_by_anchor_id"],
            },
        )
        assert executed.status_code == 200, executed.text
        result = executed.json()
        assert result["terminal_reason"] == "succeeded"
        assert result["invocation"]["status"] == "succeeded"
        assert (
            result["invocation"]["work_count"],
            result["invocation"]["voice_count"],
            result["invocation"]["validator_count"],
        ) == (1, 1, 1)


def _runtime_chain(monkeypatch):
    monkeypatch.setattr(
        model_fixtures,
        "SOURCE_IMPORT_ID",
        store_fixtures.SOURCE_IMPORT_ID,
    )
    monkeypatch.setattr(
        model_fixtures,
        "SOURCE_RECEIPT_ID",
        source_import_receipt_id(store_fixtures.SOURCE_IMPORT_ID),
    )
    monkeypatch.setattr(model_fixtures, "PROJECT_ID", store_fixtures.PROJECT_ID)
    monkeypatch.setattr(
        model_fixtures,
        "IDEMPOTENCY_KEY",
        store_fixtures.IDEMPOTENCY_KEY,
    )
    monkeypatch.setattr(model_fixtures, "NOW", store_fixtures.NOW)
    original_bindings = model_fixtures._bindings
    tool_hash = distillation_provider_tool_definitions_hash(
        build_distillation_tool_definitions()
    )

    def patched_bindings():
        source, project, knowledge, instruction, provider = original_bindings()
        return (
            source,
            project,
            knowledge,
            instruction,
            provider.model_copy(update={"tool_definitions_hash": tool_hash}),
        )

    monkeypatch.setattr(model_fixtures, "_bindings", patched_bindings)
    return model_fixtures._normal_chain()
