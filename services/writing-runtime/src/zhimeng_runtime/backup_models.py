from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BackupId = Annotated[str, StringConstraints(pattern=r"^runtime-backup-[0-9a-f]{32}$")]
BackupReceiptId = Annotated[
    str, StringConstraints(pattern=r"^runtime-backup-receipt-[0-9a-f]{32}$")
]
RestoreReceiptId = Annotated[
    str, StringConstraints(pattern=r"^runtime-restore-receipt-[0-9a-f]{32}$")
]


class BackupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RuntimeBackupManifest(BackupModel):
    schema_id: Literal["zhimeng.runtime-backup-manifest.v1"] = Field(
        default="zhimeng.runtime-backup-manifest.v1",
        alias="schema",
    )
    backup_id: BackupId
    created_at: datetime
    product_version: str = Field(min_length=1, max_length=64)
    environment: str = Field(min_length=1, max_length=64)
    database_schema_version: int = Field(ge=0)
    journal_mode: str = Field(min_length=1, max_length=32)
    database_relative_path: str = Field(min_length=1, max_length=512)
    database_sha256: Sha256
    database_size_bytes: int = Field(ge=0)
    integrity_check: Literal["ok", "failed", "unavailable"]
    foreign_key_violations: int = Field(ge=0)
    includes_secrets: Literal[False] = False
    includes_novel_body: Literal[False] = False
    includes_project_files: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    policy_version: Literal["local-v1-runtime-backup-1"] = "local-v1-runtime-backup-1"
    manifest_hash: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> "RuntimeBackupManifest":
        if self.includes_secrets is not False:
            raise ValueError("runtime backup cannot include secrets")
        if self.includes_novel_body is not False:
            raise ValueError("runtime backup cannot include novel body")
        if self.includes_project_files is not False:
            raise ValueError("runtime backup cannot include project files")
        if self.formal_draft_allowed is not False:
            raise ValueError("runtime backup cannot allow formal draft")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        expected = runtime_backup_manifest_hash(self)
        if self.manifest_hash != expected:
            raise ValueError("runtime backup manifest hash mismatch")
        return self


class RuntimeBackupReceipt(BackupModel):
    schema_id: Literal["zhimeng.runtime-backup-receipt.v1"] = Field(
        default="zhimeng.runtime-backup-receipt.v1",
        alias="schema",
    )
    receipt_id: BackupReceiptId
    backup_id: BackupId
    outcome: Literal["created"]
    backup_relative_path: str = Field(min_length=1, max_length=512)
    manifest: RuntimeBackupManifest
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "RuntimeBackupReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("runtime backup receipt cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("runtime backup receipt cannot write project truth")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        expected = runtime_backup_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("runtime backup receipt hash mismatch")
        return self


class RuntimeBackupResponse(BackupModel):
    schema_id: Literal["zhimeng.runtime-backup-response.v1"] = Field(
        default="zhimeng.runtime-backup-response.v1",
        alias="schema",
    )
    receipt: RuntimeBackupReceipt
    not_claiming: tuple[str, ...] = (
        "local_v1_exe",
        "project_file_backup",
        "formal_prose_writeback",
        "secret_vault_export",
    )


class RuntimeBackupSummary(BackupModel):
    backup_id: BackupId
    created_at: datetime
    database_schema_version: int = Field(ge=0)
    database_sha256: Sha256
    database_size_bytes: int = Field(ge=0)
    integrity_check: Literal["ok", "failed", "unavailable"]
    manifest_hash: Sha256
    backup_relative_path: str = Field(min_length=1, max_length=512)


class RuntimeBackupListResponse(BackupModel):
    schema_id: Literal["zhimeng.runtime-backup-list.v1"] = Field(
        default="zhimeng.runtime-backup-list.v1",
        alias="schema",
    )
    backups: list[RuntimeBackupSummary] = Field(default_factory=list, max_length=256)
    not_claiming: tuple[str, ...] = (
        "local_v1_exe",
        "project_file_backup",
    )


class RuntimeRestoreRequest(BackupModel):
    schema_id: Literal["zhimeng.runtime-restore-request.v1"] = Field(
        default="zhimeng.runtime-restore-request.v1",
        alias="schema",
    )
    backup_id: BackupId
    expected_manifest_hash: Sha256


class RuntimeRestoreReceipt(BackupModel):
    schema_id: Literal["zhimeng.runtime-restore-receipt.v1"] = Field(
        default="zhimeng.runtime-restore-receipt.v1",
        alias="schema",
    )
    receipt_id: RestoreReceiptId
    backup_id: BackupId
    outcome: Literal["restored"]
    restored_database_schema_version: int = Field(ge=0)
    restored_database_sha256: Sha256
    integrity_check: Literal["ok", "failed", "unavailable"]
    foreign_key_violations: int = Field(ge=0)
    pre_restore_backup_id: BackupId | None = None
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    includes_project_files: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "RuntimeRestoreReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("runtime restore cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("runtime restore cannot write project truth")
        if self.includes_project_files is not False:
            raise ValueError("runtime restore cannot include project files")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        expected = runtime_restore_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("runtime restore receipt hash mismatch")
        return self


class RuntimeRestoreResponse(BackupModel):
    schema_id: Literal["zhimeng.runtime-restore-response.v1"] = Field(
        default="zhimeng.runtime-restore-response.v1",
        alias="schema",
    )
    receipt: RuntimeRestoreReceipt
    not_claiming: tuple[str, ...] = (
        "local_v1_exe",
        "project_file_restore",
        "formal_prose_writeback",
        "secret_vault_import",
    )


def runtime_backup_manifest_hash(manifest: RuntimeBackupManifest) -> str:
    material = {
        "schema": "zhimeng.runtime-backup-manifest-hash.v1",
        "backup_id": manifest.backup_id,
        "created_at": manifest.created_at.isoformat(),
        "product_version": manifest.product_version,
        "environment": manifest.environment,
        "database_schema_version": manifest.database_schema_version,
        "journal_mode": manifest.journal_mode,
        "database_relative_path": manifest.database_relative_path,
        "database_sha256": manifest.database_sha256,
        "database_size_bytes": manifest.database_size_bytes,
        "integrity_check": manifest.integrity_check,
        "foreign_key_violations": manifest.foreign_key_violations,
        "includes_secrets": False,
        "includes_novel_body": False,
        "includes_project_files": False,
        "formal_draft_allowed": False,
        "policy_version": manifest.policy_version,
    }
    return canonical_json_hash(material)


def runtime_backup_receipt_hash(receipt: RuntimeBackupReceipt) -> str:
    material = {
        "schema": "zhimeng.runtime-backup-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "backup_id": receipt.backup_id,
        "outcome": receipt.outcome,
        "backup_relative_path": receipt.backup_relative_path,
        "manifest_hash": receipt.manifest.manifest_hash,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "created_at": receipt.created_at.isoformat(),
    }
    return canonical_json_hash(material)


def runtime_restore_receipt_hash(receipt: RuntimeRestoreReceipt) -> str:
    material = {
        "schema": "zhimeng.runtime-restore-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "backup_id": receipt.backup_id,
        "outcome": receipt.outcome,
        "restored_database_schema_version": receipt.restored_database_schema_version,
        "restored_database_sha256": receipt.restored_database_sha256,
        "integrity_check": receipt.integrity_check,
        "foreign_key_violations": receipt.foreign_key_violations,
        "pre_restore_backup_id": receipt.pre_restore_backup_id,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "includes_project_files": False,
        "created_at": receipt.created_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "RuntimeBackupListResponse",
    "RuntimeBackupManifest",
    "RuntimeBackupReceipt",
    "RuntimeBackupResponse",
    "RuntimeBackupSummary",
    "RuntimeRestoreReceipt",
    "RuntimeRestoreRequest",
    "RuntimeRestoreResponse",
    "runtime_backup_manifest_hash",
    "runtime_backup_receipt_hash",
    "runtime_restore_receipt_hash",
]

