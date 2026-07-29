from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from zhimeng_runtime.backup_models import (
    RuntimeBackupListResponse,
    RuntimeBackupManifest,
    RuntimeBackupReceipt,
    RuntimeBackupResponse,
    RuntimeBackupSummary,
    RuntimeRestoreReceipt,
    RuntimeRestoreRequest,
    RuntimeRestoreResponse,
    runtime_backup_manifest_hash,
    runtime_backup_receipt_hash,
    runtime_restore_receipt_hash,
)
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore


class BackupError(RuntimeError):
    pass


class BackupNotFoundError(BackupError):
    pass


class BackupConflictError(BackupError):
    pass


class BackupStateError(BackupError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _probe_sqlite(path: Path) -> tuple[str, int, int]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_value = str(integrity_row[0] if integrity_row is not None else "failed")
        integrity = "ok" if integrity_value.lower() == "ok" else "failed"
        fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return integrity, len(fk_rows), schema_version
    finally:
        connection.close()


class BackupService:
    """Consistent Runtime SQLite backup/restore without project files or secrets."""

    def __init__(
        self,
        store: SqliteStore,
        settings: RuntimeSettings,
        *,
        product_version: str,
        backup_root: Path | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.product_version = product_version
        self.backup_root = (
            backup_root
            if backup_root is not None
            else (
                settings.backup_root
                if settings.backup_root is not None
                else settings.database_path.parent / "backups"
            )
        )

    def list_backups(self) -> RuntimeBackupListResponse:
        summaries: list[RuntimeBackupSummary] = []
        if self.backup_root.is_dir():
            for path in sorted(self.backup_root.glob("runtime-backup-*/manifest.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    manifest = RuntimeBackupManifest.model_validate(payload)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                summaries.append(
                    RuntimeBackupSummary(
                        backup_id=manifest.backup_id,
                        created_at=manifest.created_at,
                        database_schema_version=manifest.database_schema_version,
                        database_sha256=manifest.database_sha256,
                        database_size_bytes=manifest.database_size_bytes,
                        integrity_check=manifest.integrity_check,
                        manifest_hash=manifest.manifest_hash,
                        backup_relative_path=f"backups/{manifest.backup_id}",
                    )
                )
        summaries.sort(key=lambda item: item.created_at, reverse=True)
        return RuntimeBackupListResponse(backups=summaries)

    def create_backup(self) -> RuntimeBackupResponse:
        created_at = _now()
        source = self.settings.database_path.resolve(strict=False)
        if not source.is_file():
            raise BackupStateError("runtime database is missing")

        # Encourage a consistent on-disk view before online backup.
        with self.store.connection() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            live_schema = int(connection.execute("PRAGMA user_version").fetchone()[0])

        seed = f"{source}\0{created_at.isoformat()}\0{live_schema}".encode("utf-8")
        backup_id = "runtime-backup-" + hashlib.sha256(seed).hexdigest()[:32]
        package_dir = self.backup_root / backup_id
        if package_dir.exists():
            raise BackupConflictError("backup identity already exists")
        package_dir.mkdir(parents=True, exist_ok=False)
        database_name = "writing-runtime.sqlite3"
        target_db = package_dir / database_name

        source_conn = sqlite3.connect(str(source))
        try:
            dest_conn = sqlite3.connect(str(target_db))
            try:
                source_conn.backup(dest_conn)
                dest_conn.commit()
                # Package only the main DB file; drop WAL companions from the copy.
                dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dest_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest_conn.close()
        finally:
            source_conn.close()
        for suffix in ("-wal", "-shm"):
            companion = Path(str(target_db) + suffix)
            if companion.exists():
                companion.unlink()

        integrity, fk_violations, schema_version = _probe_sqlite(target_db)
        if integrity != "ok":
            shutil.rmtree(package_dir, ignore_errors=True)
            raise BackupStateError("backup integrity_check failed")
        if schema_version != live_schema:
            shutil.rmtree(package_dir, ignore_errors=True)
            raise BackupStateError("backup schema version mismatch")

        database_sha256 = _sha256_file(target_db)
        size_bytes = target_db.stat().st_size
        provisional_manifest = RuntimeBackupManifest.model_construct(
            schema_id="zhimeng.runtime-backup-manifest.v1",
            backup_id=backup_id,
            created_at=created_at,
            product_version=self.product_version,
            environment=self.settings.environment,
            database_schema_version=schema_version,
            journal_mode=journal_mode,
            database_relative_path=database_name,
            database_sha256=database_sha256,
            database_size_bytes=size_bytes,
            integrity_check=integrity,  # type: ignore[arg-type]
            foreign_key_violations=fk_violations,
            includes_secrets=False,
            includes_novel_body=False,
            includes_project_files=False,
            formal_draft_allowed=False,
            policy_version="local-v1-runtime-backup-1",
            manifest_hash="0" * 64,
        )
        manifest = RuntimeBackupManifest(
            backup_id=backup_id,
            created_at=created_at,
            product_version=self.product_version,
            environment=self.settings.environment,
            database_schema_version=schema_version,
            journal_mode=journal_mode,
            database_relative_path=database_name,
            database_sha256=database_sha256,
            database_size_bytes=size_bytes,
            integrity_check=integrity,  # type: ignore[arg-type]
            foreign_key_violations=fk_violations,
            includes_secrets=False,
            includes_novel_body=False,
            includes_project_files=False,
            formal_draft_allowed=False,
            policy_version="local-v1-runtime-backup-1",
            manifest_hash=runtime_backup_manifest_hash(provisional_manifest),
        )
        _atomic_write_json(
            package_dir / "manifest.json",
            manifest.model_dump(mode="json", by_alias=True),
        )

        receipt_id = "runtime-backup-receipt-" + hashlib.sha256(
            f"{backup_id}\0{manifest.manifest_hash}\0{created_at.isoformat()}".encode("utf-8")
        ).hexdigest()[:32]
        provisional_receipt = RuntimeBackupReceipt.model_construct(
            schema_id="zhimeng.runtime-backup-receipt.v1",
            receipt_id=receipt_id,
            backup_id=backup_id,
            outcome="created",
            backup_relative_path=f"backups/{backup_id}",
            manifest=manifest,
            formal_draft_allowed=False,
            writes_project_truth=False,
            created_at=created_at,
            receipt_hash="0" * 64,
        )
        receipt = RuntimeBackupReceipt(
            receipt_id=receipt_id,
            backup_id=backup_id,
            outcome="created",
            backup_relative_path=f"backups/{backup_id}",
            manifest=manifest,
            formal_draft_allowed=False,
            writes_project_truth=False,
            created_at=created_at,
            receipt_hash=runtime_backup_receipt_hash(provisional_receipt),
        )
        _atomic_write_json(
            package_dir / "backup-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        return RuntimeBackupResponse(receipt=receipt)

    def restore_backup(self, request: RuntimeRestoreRequest) -> RuntimeRestoreResponse:
        package_dir = self.backup_root / request.backup_id
        manifest_path = package_dir / "manifest.json"
        if not manifest_path.is_file():
            raise BackupNotFoundError("backup package was not found")
        try:
            manifest = RuntimeBackupManifest.model_validate(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise BackupStateError("backup manifest is unreadable") from exc
        if manifest.manifest_hash != request.expected_manifest_hash:
            raise BackupConflictError("backup manifest hash does not match expected value")

        source_db = package_dir / manifest.database_relative_path
        if not source_db.is_file():
            raise BackupStateError("backup database file is missing")
        actual_sha = _sha256_file(source_db)
        if actual_sha != manifest.database_sha256:
            raise BackupStateError("backup database hash mismatch")
        integrity, fk_violations, schema_version = _probe_sqlite(source_db)
        if integrity != "ok":
            raise BackupStateError("backup integrity_check failed before restore")
        if schema_version > SCHEMA_VERSION:
            raise BackupStateError(
                f"backup schema {schema_version} is newer than runtime schema {SCHEMA_VERSION}"
            )

        # Safety snapshot of current DB before replace.
        pre_restore: RuntimeBackupResponse | None = None
        if self.settings.database_path.is_file():
            pre_restore = self.create_backup()

        live = self.settings.database_path.resolve(strict=False)
        live.parent.mkdir(parents=True, exist_ok=True)
        temporary = live.with_name(live.name + f".restore-{request.backup_id[-8:]}.tmp")
        if temporary.exists():
            temporary.unlink()
        shutil.copy2(source_db, temporary)
        # Drop WAL/SHM companions so restored main DB is authoritative.
        for suffix in ("-wal", "-shm"):
            companion = Path(str(live) + suffix)
            if companion.exists():
                companion.unlink()
        os.replace(temporary, live)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(live) + suffix)
            if companion.exists():
                companion.unlink()

        restored_sha = _sha256_file(live)
        restored_integrity, restored_fk, restored_schema = _probe_sqlite(live)
        if restored_integrity != "ok":
            raise BackupStateError("restored database failed integrity_check")
        # Re-open/probe through store after replacement.
        health = self.store.initialize()
        if not health.ready:
            raise BackupStateError("runtime database is not ready after restore")

        created_at = _now()
        receipt_id = "runtime-restore-receipt-" + hashlib.sha256(
            f"{request.backup_id}\0{restored_sha}\0{created_at.isoformat()}".encode("utf-8")
        ).hexdigest()[:32]
        provisional = RuntimeRestoreReceipt.model_construct(
            schema_id="zhimeng.runtime-restore-receipt.v1",
            receipt_id=receipt_id,
            backup_id=request.backup_id,
            outcome="restored",
            restored_database_schema_version=restored_schema,
            restored_database_sha256=restored_sha,
            integrity_check=restored_integrity,  # type: ignore[arg-type]
            foreign_key_violations=restored_fk,
            pre_restore_backup_id=(
                None if pre_restore is None else pre_restore.receipt.backup_id
            ),
            formal_draft_allowed=False,
            writes_project_truth=False,
            includes_project_files=False,
            created_at=created_at,
            receipt_hash="0" * 64,
        )
        receipt = RuntimeRestoreReceipt(
            receipt_id=receipt_id,
            backup_id=request.backup_id,
            outcome="restored",
            restored_database_schema_version=restored_schema,
            restored_database_sha256=restored_sha,
            integrity_check=restored_integrity,  # type: ignore[arg-type]
            foreign_key_violations=restored_fk,
            pre_restore_backup_id=(
                None if pre_restore is None else pre_restore.receipt.backup_id
            ),
            formal_draft_allowed=False,
            writes_project_truth=False,
            includes_project_files=False,
            created_at=created_at,
            receipt_hash=runtime_restore_receipt_hash(provisional),
        )
        _atomic_write_json(
            package_dir / "restore-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        return RuntimeRestoreResponse(receipt=receipt)


__all__ = [
    "BackupConflictError",
    "BackupError",
    "BackupNotFoundError",
    "BackupService",
    "BackupStateError",
]


