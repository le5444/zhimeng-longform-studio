from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def test_privacy_outbound_disclosure(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
            agent_provider_base_url="https://example.invalid/v1",
            agent_provider_model="demo-model",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/privacy/outbound-disclosure")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema"] == "zhimeng.privacy-outbound-disclosure-response.v1"
        receipt = body["receipt"]
        assert receipt["local_first"] is True
        assert receipt["telemetry_default_off"] is True
        assert receipt["telemetry_enabled"] is False
        assert receipt["offline_capable_core_ops"] is True
        assert receipt["formal_draft_allowed"] is False
        assert receipt["outbound_target_count"] == 1
        assert receipt["outbound_targets"][0]["host"] == "example.invalid"
        assert "prompt_context" in receipt["outbound_targets"][0]["data_categories"]
        assert any(
            item["operation"] == "apply_or_rollback_change_set" and item["offline_capable"]
            for item in receipt["offline_operations"]
        )
        assert "packet_capture_proof" in body["not_claiming"]
        assert "local_v1_exe" in body["not_claiming"]
