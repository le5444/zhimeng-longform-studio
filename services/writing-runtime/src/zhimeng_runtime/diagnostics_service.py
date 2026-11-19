from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import DatabaseHealth, SqliteStore
from zhimeng_runtime.diagnostics_models import (
    DiagnosticsPackageReceipt,
    DiagnosticsPackageResponse,
    diagnostics_package_receipt_hash,
)
from zhimeng_runtime.fs_atomic import replace_with_retry


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


class DiagnosticsService:
    """Build read-only, desensitized diagnostics packages for Local ops."""

    def __init__(
        self,
        store: SqliteStore,
        settings: RuntimeSettings,
        *,
        product_version: str,
        export_root: Path | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.product_version = product_version
        if export_root is not None:
            self.export_root = export_root
        elif settings.diagnostics_root is not None:
            self.export_root = settings.diagnostics_root
        else:
            self.export_root = settings.database_path.parent / "diagnostics"

    def export_package(
        self,
        *,
        runtime_state: str,
        health: DatabaseHealth,
        startup_error: str | None = None,
    ) -> DiagnosticsPackageResponse:
        generated_at = _now()
        integrity = "unavailable"
        foreign_key_violations = 0
        table_counts: dict[str, int] = {}
        try:
            with self.store.connection() as connection:
                integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
                integrity_value = str(integrity_row[0] if integrity_row is not None else "failed")
                integrity = "ok" if integrity_value.lower() == "ok" else "failed"
                fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
                foreign_key_violations = len(fk_rows)
                names = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                for row in names:
                    name = str(row["name"] if "name" in row.keys() else row[0])
                    count_row = connection.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()
                    table_counts[name] = int(count_row["c"] if count_row is not None else 0)
        except Exception:
            integrity = "unavailable"
            foreign_key_violations = 0
            table_counts = {}

        included = [
            "product_version",
            "runtime_state",
            "database_health",
            "integrity_check",
            "foreign_key_check",
            "table_counts",
            "trusted_root_counts",
            "provider_config_flags",
            "includes_excludes",
        ]
        excluded = [
            "provider_api_key",
            "cookies",
            "authorization_headers",
            "novel_body",
            "source_book_bytes",
            "full_trusted_root_paths",
            "prompt_bodies",
            "model_completion_bodies",
        ]
        package: dict[str, Any] = {
            "schema": "zhimeng.diagnostics-package.v1",
            "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
            "product_version": self.product_version,
            "environment": self.settings.environment,
            "runtime_state": runtime_state,
            "startup_error_present": startup_error is not None,
            "database": {
                "ready": bool(health.ready),
                "schema_version": int(health.schema_version),
                "journal_mode": str(health.journal_mode),
                "integrity_check": integrity,
                "foreign_key_violations": foreign_key_violations,
                "table_counts": table_counts,
                # 长期写作会稳定增长（context_pack / run_event / activity 只增不减）；
                # 把体积放进诊断包，清理策略才有数据可依。
                "file_size_bytes": (
                    self.store.path.stat().st_size
                    if self.store.path.exists()
                    else 0
                ),
            },
            "config_redacted": {
                "trusted_project_root_count": len(self.settings.trusted_project_roots),
                "trusted_source_root_count": len(self.settings.trusted_source_roots),
                "trusted_skill_root_count": len(self.settings.trusted_skill_roots),
                "provider_base_url_configured": self.settings.agent_provider_base_url is not None,
                "provider_model_configured": self.settings.agent_provider_model is not None,
                "provider_api_key_present": self.settings.agent_provider_api_key is not None,
                "provider_api_key": "[redacted]",
                "provider_credential_reference_present": (
                    self.settings.agent_provider_credential_reference is not None
                ),
            },
            "included": included,
            "excluded": excluded,
            "formal_draft_allowed": False,
            "writes_project_truth": False,
            "contains_secrets": False,
            "contains_novel_body": False,
        }
        encoded = (
            json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
        digest = _sha256_bytes(encoded)[:16]
        relative_path = f"diagnostics/diagnostics-{stamp}-{digest}.json"
        export_path = self.export_root / f"diagnostics-{stamp}-{digest}.json"
        _atomic_write_bytes(export_path, encoded)
        export_sha = _sha256_bytes(export_path.read_bytes())
        receipt_id = "diagnostics-package-" + hashlib.sha256(
            f"{relative_path}\0{export_sha}\0{generated_at.isoformat()}".encode("utf-8")
        ).hexdigest()[:32]
        provisional = DiagnosticsPackageReceipt.model_construct(
            schema_id="zhimeng.diagnostics-package-receipt.v1",
            receipt_id=receipt_id,
            generated_at=generated_at,
            product_version=self.product_version,
            environment=self.settings.environment,
            runtime_state=runtime_state,  # type: ignore[arg-type]
            database_ready=bool(health.ready),
            database_schema_version=int(health.schema_version),
            journal_mode=str(health.journal_mode),
            integrity_check=integrity,  # type: ignore[arg-type]
            foreign_key_violations=foreign_key_violations,
            table_counts=table_counts,
            trusted_project_root_count=len(self.settings.trusted_project_roots),
            trusted_source_root_count=len(self.settings.trusted_source_roots),
            trusted_skill_root_count=len(self.settings.trusted_skill_roots),
            provider_configured=self.settings.agent_provider_base_url is not None,
            provider_api_key_present=self.settings.agent_provider_api_key is not None,
            provider_api_key_redacted=True,
            export_relative_path=relative_path,
            export_file_sha256=export_sha,
            included=included,
            excluded=excluded,
            formal_draft_allowed=False,
            writes_project_truth=False,
            contains_secrets=False,
            contains_novel_body=False,
            policy_version="local-v1-diagnostics-1",
            receipt_hash="0" * 64,
        )
        receipt = DiagnosticsPackageReceipt(
            receipt_id=receipt_id,
            generated_at=generated_at,
            product_version=self.product_version,
            environment=self.settings.environment,
            runtime_state=runtime_state,  # type: ignore[arg-type]
            database_ready=bool(health.ready),
            database_schema_version=int(health.schema_version),
            journal_mode=str(health.journal_mode),
            integrity_check=integrity,  # type: ignore[arg-type]
            foreign_key_violations=foreign_key_violations,
            table_counts=table_counts,
            trusted_project_root_count=len(self.settings.trusted_project_roots),
            trusted_source_root_count=len(self.settings.trusted_source_roots),
            trusted_skill_root_count=len(self.settings.trusted_skill_roots),
            provider_configured=self.settings.agent_provider_base_url is not None,
            provider_api_key_present=self.settings.agent_provider_api_key is not None,
            provider_api_key_redacted=True,
            export_relative_path=relative_path,
            export_file_sha256=export_sha,
            included=included,
            excluded=excluded,
            formal_draft_allowed=False,
            writes_project_truth=False,
            contains_secrets=False,
            contains_novel_body=False,
            policy_version="local-v1-diagnostics-1",
            receipt_hash=diagnostics_package_receipt_hash(provisional),
        )
        return DiagnosticsPackageResponse(receipt=receipt, package=package)


__all__ = ["DiagnosticsService"]
