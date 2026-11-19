from __future__ import annotations

import sqlite3
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.backup_service import AutoBackupWorker, BackupService
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.backup_models import RuntimeBackupManifest, runtime_backup_manifest_hash
from zhimeng_runtime.context_models import canonical_json_hash


def test_backup_keeps_health_requests_responsive_while_copy_is_waiting(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    entered, release = threading.Event(), threading.Event()
    create_backup = BackupService.create_backup

    def waiting_backup(service):
        entered.set()
        assert release.wait(15), 'test did not release the controlled backup'
        return create_backup(service)

    monkeypatch.setattr(BackupService, 'create_backup', waiting_backup)
    with client, ThreadPoolExecutor(max_workers=2) as pool:
        backup = pool.submit(client.post, '/api/v1/runtime/backups')
        try:
            assert entered.wait(10)
            health = pool.submit(client.get, '/health/ready')
            response = health.result(timeout=5)
            assert response.status_code == 200, response.text
            assert not backup.done()
        finally:
            release.set()
        saved = backup.result(timeout=15)
        assert saved.status_code == 200, saved.text
        assert saved.json()['receipt']['manifest']['integrity_check'] == 'ok'


def test_cancelled_backup_is_drained_before_database_maintenance(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path)
    entered, release = threading.Event(), threading.Event()
    create_backup = BackupService.create_backup

    def waiting_backup(service):
        entered.set()
        assert release.wait(15)
        return create_backup(service)

    monkeypatch.setattr(BackupService, 'create_backup', waiting_backup)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as requests:
            request = asyncio.create_task(requests.post('/api/v1/runtime/backups'))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
                maintenance = client.app.state.runtime_maintenance
                drain = asyncio.create_task(maintenance.drain_background())
                await asyncio.sleep(0.05)
                assert not drain.done(), 'database maintenance abandoned a running backup'
            finally:
                release.set()
            await asyncio.wait_for(drain, 10)
            packages = list((db_path.parent / 'backups').glob('runtime-backup-*/manifest.json'))
            assert len(packages) == 1
            assert (await requests.get('/health/ready')).status_code == 200

    with client:
        client.portal.call(scenario)


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
        assert manifest["includes_secrets"] is None
        assert manifest["includes_novel_body"] is None
        assert manifest["content_scope"] == "runtime_database_unredacted"
        assert manifest["schema"] == "zhimeng.runtime-backup-manifest.v2"
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


def _service(tmp_path: Path) -> BackupService:
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "writing-runtime.sqlite3",
        trusted_project_roots=(tmp_path,),
        allowed_origins=(),
    )
    store = SqliteStore(settings.database_path)
    store.initialize()
    return BackupService(store, settings, product_version="test-1.0.0")


def test_full_database_backup_discloses_and_preserves_private_conversation_text(tmp_path):
    client, db_path = _client(tmp_path)
    with client:
        session = client.post('/api/v1/agent/sessions', json={'title': '备份样本'}).json()
        marker = 'SYNTHETIC_PRIVATE_DOCUMENT_20261028'
        # Persist through the real conversation store without dispatching a model.
        store = AgentStore(SqliteStore(db_path))
        run, message, _ = store.create_run(session_id=session['session_id'], message=marker,
            expected_project_revision=None, expected_project_fingerprint=None,
            active_chapter_id=None, idempotency_key='backup-content')
        response = client.post('/api/v1/runtime/backups')
        assert response.status_code == 200, response.text
        manifest = response.json()['receipt']['manifest']
        assert manifest['content_scope'] == 'runtime_database_unredacted'
        assert manifest['includes_novel_body'] is None and manifest['includes_secrets'] is None
        package = db_path.parent / 'backups' / manifest['backup_id']
        with sqlite3.connect(package / manifest['database_relative_path']) as db:
            payloads = db.execute("SELECT payload_json FROM activity WHERE run_id=?", (run.run_id,)).fetchall()
        assert any(marker in row[0] for row in payloads)
        assert RuntimeBackupManifest.model_validate_json((package / 'manifest.json').read_text()).manifest_hash == manifest['manifest_hash']
        assert marker not in response.text


def test_legacy_manifest_hash_and_restore_remain_compatible(tmp_path):
    client, db_path = _client(tmp_path)
    with client:
        _upsert_marker(db_path, 'legacy content')
        manifest = client.post('/api/v1/runtime/backups').json()['receipt']['manifest']
        manifest.update(schema='zhimeng.runtime-backup-manifest.v1',
                        policy_version='local-v1-runtime-backup-1', includes_secrets=False,
                        includes_novel_body=False)
        manifest.pop('content_scope')
        # Reproduce the old v1 hash material independently of the new helper.
        material = {key: value for key, value in manifest.items() if key not in {'schema', 'manifest_hash'}}
        material['schema'] = 'zhimeng.runtime-backup-manifest-hash.v1'
        material['created_at'] = datetime.fromisoformat(manifest['created_at']).isoformat()
        manifest['manifest_hash'] = canonical_json_hash(material)
        package = db_path.parent / 'backups' / manifest['backup_id']
        (package / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        parsed = RuntimeBackupManifest.model_validate(manifest)
        assert runtime_backup_manifest_hash(parsed) == manifest['manifest_hash']
        listed = client.get('/api/v1/runtime/backups').json()['backups']
        assert listed[0]['content_scope'] == 'runtime_database_unredacted'
        _upsert_marker(db_path, 'later content')
        restored = client.post('/api/v1/runtime/backups/restore', json={
            'backup_id': manifest['backup_id'], 'expected_manifest_hash': manifest['manifest_hash'],
        })
        assert restored.status_code == 200, restored.text
        assert _read_marker(db_path) == 'legacy content'


@pytest.mark.parametrize('field,value', [('includes_novel_body', False), ('includes_secrets', False),
                                       ('content_scope', None), ('policy_version', 'local-v1-runtime-backup-1')])
def test_new_backup_cannot_claim_unverified_content_absent(tmp_path, field, value):
    manifest = _service(tmp_path).create_backup().receipt.manifest.model_dump(mode='json', by_alias=True)
    manifest[field] = value
    with pytest.raises(ValueError):
        RuntimeBackupManifest.model_validate(manifest)


class _Clock:
    """测试用的表针：备份的真时间戳动不了（manifest 有哈希封口），就挪「现在」。"""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


def test_auto_backup_backs_up_once_then_waits_for_the_interval(tmp_path: Path) -> None:
    service = _service(tmp_path)
    clock = _Clock()
    worker = AutoBackupWorker(service, interval_hours=24, keep=3, clock=clock)

    assert worker.run_once() is not None
    assert service.list_backups().backups[0].integrity_check == "ok"
    assert worker.run_once() is None
    clock.offset = timedelta(hours=23)
    assert worker.run_once() is None
    assert len(service.list_backups().backups) == 1

    clock.offset = timedelta(hours=25)
    assert worker.run_once() is not None
    assert len(service.list_backups().backups) == 2


def test_auto_backup_keeps_only_the_newest_copies_and_never_touches_the_authors_own(
    tmp_path: Path,
) -> None:
    """库近百 MB：自动备份必须自己收旧的，但作者手动丢进这个文件夹的东西不能动。"""

    service = _service(tmp_path)
    service.backup_root.mkdir(parents=True, exist_ok=True)
    (service.backup_root / "workbench-v24-20260829.sqlite3").write_text(
        "作者自己的旧副本\n", encoding="utf-8"
    )
    (service.backup_root / "pre-v9-20260720-191143").mkdir()

    clock = _Clock()
    worker = AutoBackupWorker(service, interval_hours=24, keep=2, clock=clock)
    made: list[str] = []
    for day in range(3):
        clock.offset = timedelta(days=day)
        backup_id = worker.run_once()
        assert backup_id is not None, f"第 {day} 天该备一份"
        made.append(backup_id)

    remaining = {item.backup_id for item in service.list_backups().backups}
    assert remaining == set(made[1:])
    assert not (service.backup_root / made[0]).exists()
    assert (service.backup_root / "workbench-v24-20260829.sqlite3").is_file()
    assert (service.backup_root / "pre-v9-20260720-191143").is_dir()

