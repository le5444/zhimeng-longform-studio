from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def test_diagnostics_package_is_redacted_and_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=db_path,
                trusted_project_roots=(tmp_path,),
                allowed_origins=(),
                agent_provider_base_url="https://example.invalid/v1",
                agent_provider_api_key="super-secret-key",
                agent_provider_credential_reference="vault://test/provider",
                agent_provider_model="demo-model",
            )
        )
    ) as client:
        response = client.post("/api/v1/runtime/diagnostics-package")
        assert response.status_code == 200, response.text
        body = response.json()
        receipt = body["receipt"]
        package = body["package"]
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False
        assert receipt["contains_secrets"] is False
        assert receipt["contains_novel_body"] is False
        assert receipt["provider_api_key_present"] is True
        assert receipt["provider_api_key_redacted"] is True
        assert receipt["integrity_check"] in {"ok", "failed", "unavailable"}
        assert "super-secret-key" not in response.text
        assert package["config_redacted"]["provider_api_key"] == "[redacted]"
        assert "local_v1_exe" in body["not_claiming"]
        export_name = Path(receipt["export_relative_path"]).name
        export_path = db_path.parent / "diagnostics" / export_name
        assert export_path.is_file()
        exported = export_path.read_text(encoding="utf-8")
        assert "super-secret-key" not in exported
        assert '"contains_secrets": false' in exported.replace("False", "false")


def test_diagnostics_package_uses_configured_diagnostics_root(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "writing-runtime.sqlite3"
    diagnostics_root = tmp_path / "portable-diagnostics"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="portable",
                database_path=db_path,
                diagnostics_root=diagnostics_root,
                trusted_project_roots=(tmp_path,),
                allowed_origins=(),
                agent_provider_base_url="https://example.invalid/v1",
                agent_provider_api_key="super-secret-key",
                agent_provider_credential_reference="vault://test/provider",
                agent_provider_model="demo-model",
            )
        )
    ) as client:
        response = client.post("/api/v1/runtime/diagnostics-package")
        assert response.status_code == 200, response.text
        receipt = response.json()["receipt"]
        export_name = Path(receipt["export_relative_path"]).name
        export_path = diagnostics_root / export_name
        assert export_path.is_file()
        assert "super-secret-key" not in export_path.read_text(encoding="utf-8")
        # Must not leak into default database-adjacent diagnostics path.
        default_path = db_path.parent / "diagnostics" / export_name
        assert not default_path.exists()
