from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "writing-runtime.sqlite3"
    client = TestClient(
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
    )
    return client, db_path


def _upsert_marker(db_path: Path, marker: str) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_backup_marker (id TEXT PRIMARY KEY, note TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO runtime_backup_marker (id, note) VALUES (?, ?)",
            ("marker", marker),
        )
        connection.commit()
    finally:
        connection.close()


def _read_marker(db_path: Path) -> str | None:
    connection = sqlite3.connect(str(db_path))
    try:
        try:
            row = connection.execute(
                "SELECT note FROM runtime_backup_marker WHERE id = ?",
                ("marker",),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else str(row[0])
    finally:
        connection.close()


def test_runtime_backup_create_list_restore_and_invariants(tmp_path: Path) -> None:
    client, db_path = _client(tmp_path)
    with client:
        empty_list = client.get("/api/v1/runtime/backups")
        assert empty_list.status_code == 200, empty_list.text
        assert empty_list.json()["backups"] == []
        assert "project_file_backup" in empty_list.json()["not_claiming"]

        _upsert_marker(db_path, "before")
        assert _read_marker(db_path) == "before"

        created = client.post("/api/v1/runtime/backups")
        assert created.status_code == 200, created.text
        created_body = created.json()
        receipt = created_body["receipt"]
        manifest = receipt["manifest"]
        backup_id = receipt["backup_id"]
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False
        assert manifest["includes_secrets"] is False
        assert manifest["includes_novel_body"] is False
        assert manifest["includes_project_files"] is False
        assert manifest["formal_draft_allowed"] is False
        assert manifest["integrity_check"] == "ok"
        assert "super-secret-key" not in created.text
        assert "local_v1_exe" in created_body["not_claiming"]

        package_dir = db_path.parent / "backups" / backup_id
        assert package_dir.is_dir()
        package_names = {path.name for path in package_dir.iterdir()}
        assert package_names == {
            "writing-runtime.sqlite3",
            "manifest.json",
            "backup-receipt.json",
        }
        package_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in package_dir.iterdir()
            if path.suffix == ".json"
        )
        assert "super-secret-key" not in package_text
        assert "vault://test/provider" not in package_text

        listed = client.get("/api/v1/runtime/backups")
        assert listed.status_code == 200, listed.text
        backups = listed.json()["backups"]
        assert len(backups) == 1
        assert backups[0]["backup_id"] == backup_id
        assert backups[0]["database_sha256"] == manifest["database_sha256"]
        assert backups[0]["manifest_hash"] == manifest["manifest_hash"]

        _upsert_marker(db_path, "after")
        assert _read_marker(db_path) == "after"

        restored = client.post(
            "/api/v1/runtime/backups/restore",
            json={
                "schema": "zhimeng.runtime-restore-request.v1",
                "backup_id": backup_id,
                "expected_manifest_hash": manifest["manifest_hash"],
            },
        )
        assert restored.status_code == 200, restored.text
        restore_body = restored.json()
        restore_receipt = restore_body["receipt"]
        assert restore_receipt["outcome"] == "restored"
        assert restore_receipt["formal_draft_allowed"] is False
        assert restore_receipt["writes_project_truth"] is False
        assert restore_receipt["includes_project_files"] is False
        assert restore_receipt["integrity_check"] == "ok"
        assert restore_receipt["pre_restore_backup_id"] is not None
        assert "project_file_restore" in restore_body["not_claiming"]
        assert "secret_vault_import" in restore_body["not_claiming"]
        assert "super-secret-key" not in restored.text

        assert _read_marker(db_path) == "before"

        after_list = client.get("/api/v1/runtime/backups")
        assert after_list.status_code == 200, after_list.text
        listed_ids = {item["backup_id"] for item in after_list.json()["backups"]}
        assert backup_id in listed_ids
        assert restore_receipt["pre_restore_backup_id"] in listed_ids


def test_runtime_restore_rejects_hash_mismatch_and_missing(tmp_path: Path) -> None:
    client, _db_path = _client(tmp_path)
    with client:
        created = client.post("/api/v1/runtime/backups")
        assert created.status_code == 200, created.text
        backup_id = created.json()["receipt"]["backup_id"]

        conflict = client.post(
            "/api/v1/runtime/backups/restore",
            json={
                "schema": "zhimeng.runtime-restore-request.v1",
                "backup_id": backup_id,
                "expected_manifest_hash": "0" * 64,
            },
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "runtime_backup_conflict"

        missing = client.post(
            "/api/v1/runtime/backups/restore",
            json={
                "schema": "zhimeng.runtime-restore-request.v1",
                "backup_id": "runtime-backup-" + ("a" * 32),
                "expected_manifest_hash": "b" * 64,
            },
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "runtime_backup_not_found"

