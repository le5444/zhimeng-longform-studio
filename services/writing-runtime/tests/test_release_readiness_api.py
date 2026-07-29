from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.release_readiness_service import ReleaseReadinessService


def test_release_readiness_api_fail_closed(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        evaluated = client.post("/api/v1/runtime/release-readiness/evaluate")
        assert evaluated.status_code == 200, evaluated.text
        body = evaluated.json()
        assert body["schema"] == "zhimeng.local-v1-release-readiness-response.v1"
        receipt = body["receipt"]
        assert receipt["local_v1_ready"] is False
        assert receipt["local_v1_exe"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["status"] == "not_ready"
        assert "signed_package_ready" in receipt["missing_required"]
        assert "all_local_v1_fr_verified" in receipt["missing_required"]
        assert "ace2e_001_to_007_verified" in receipt["missing_required"]
        assert "local_v1_exe" in body["not_claiming"]
        assert "local_v1_complete" in body["not_claiming"]
        assert receipt["runtime_surface"] == "writing-runtime-ops"

        latest = client.get("/api/v1/runtime/release-readiness")
        assert latest.status_code == 200, latest.text
        latest_body = latest.json()
        assert latest_body["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert latest_body["receipt"]["local_v1_ready"] is False


def test_release_readiness_service_persists_receipt(tmp_path: Path) -> None:
    service = ReleaseReadinessService(state_root=tmp_path / "release-readiness")
    response = service.evaluate()
    assert response["receipt"]["local_v1_ready"] is False
    assert service.latest_path.is_file()
    loaded = service.latest()
    assert loaded is not None
    assert loaded["receipt"]["receipt_id"] == response["receipt"]["receipt_id"]
    assert loaded["receipt"]["local_v1_exe"] is False
