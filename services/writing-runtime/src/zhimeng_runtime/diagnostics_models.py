from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DiagnosticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DiagnosticsPackageReceipt(DiagnosticsModel):
    schema_id: Literal["zhimeng.diagnostics-package-receipt.v1"] = Field(
        default="zhimeng.diagnostics-package-receipt.v1",
        alias="schema",
    )
    receipt_id: str = Field(pattern=r"^diagnostics-package-[0-9a-f]{32}$")
    generated_at: datetime
    product_version: str = Field(min_length=1, max_length=64)
    environment: str = Field(min_length=1, max_length=64)
    runtime_state: Literal["starting", "ready", "degraded"]
    database_ready: bool
    database_schema_version: int = Field(ge=0)
    journal_mode: str = Field(min_length=1, max_length=32)
    integrity_check: Literal["ok", "failed", "unavailable"]
    foreign_key_violations: int = Field(ge=0)
    table_counts: dict[str, int] = Field(default_factory=dict)
    trusted_project_root_count: int = Field(ge=0)
    trusted_source_root_count: int = Field(ge=0)
    trusted_skill_root_count: int = Field(ge=0)
    provider_configured: bool
    provider_api_key_present: bool
    provider_api_key_redacted: Literal[True] = True
    export_relative_path: str = Field(min_length=1, max_length=512)
    export_file_sha256: Sha256
    included: list[str] = Field(min_length=1, max_length=64)
    excluded: list[str] = Field(min_length=1, max_length=64)
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    contains_secrets: Literal[False] = False
    contains_novel_body: Literal[False] = False
    policy_version: Literal["local-v1-diagnostics-1"] = "local-v1-diagnostics-1"
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "DiagnosticsPackageReceipt":
        if self.formal_draft_allowed is not False:
            raise ValueError("diagnostics package cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("diagnostics package cannot write project truth")
        if self.contains_secrets is not False:
            raise ValueError("diagnostics package cannot contain secrets")
        if self.contains_novel_body is not False:
            raise ValueError("diagnostics package cannot contain novel body")
        if self.provider_api_key_redacted is not True:
            raise ValueError("provider API key must be redacted")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        expected = diagnostics_package_receipt_hash(self)
        if self.receipt_hash != expected:
            raise ValueError("diagnostics package receipt hash mismatch")
        return self


class DiagnosticsPackageResponse(DiagnosticsModel):
    schema_id: Literal["zhimeng.diagnostics-package-response.v1"] = Field(
        default="zhimeng.diagnostics-package-response.v1",
        alias="schema",
    )
    receipt: DiagnosticsPackageReceipt
    package: dict[str, object]
    not_claiming: tuple[str, ...] = (
        "local_v1_exe",
        "formal_prose_writeback",
        "formal_draft_authorization_unlock",
        "training_dataset_ready",
    )


def diagnostics_package_receipt_hash(receipt: DiagnosticsPackageReceipt) -> str:
    material = {
        "schema": "zhimeng.diagnostics-package-receipt-hash.v1",
        "receipt_id": receipt.receipt_id,
        "generated_at": receipt.generated_at.isoformat(),
        "product_version": receipt.product_version,
        "environment": receipt.environment,
        "runtime_state": receipt.runtime_state,
        "database_ready": receipt.database_ready,
        "database_schema_version": receipt.database_schema_version,
        "journal_mode": receipt.journal_mode,
        "integrity_check": receipt.integrity_check,
        "foreign_key_violations": receipt.foreign_key_violations,
        "table_counts": receipt.table_counts,
        "trusted_project_root_count": receipt.trusted_project_root_count,
        "trusted_source_root_count": receipt.trusted_source_root_count,
        "trusted_skill_root_count": receipt.trusted_skill_root_count,
        "provider_configured": receipt.provider_configured,
        "provider_api_key_present": receipt.provider_api_key_present,
        "provider_api_key_redacted": True,
        "export_relative_path": receipt.export_relative_path,
        "export_file_sha256": receipt.export_file_sha256,
        "included": list(receipt.included),
        "excluded": list(receipt.excluded),
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "contains_secrets": False,
        "contains_novel_body": False,
        "policy_version": receipt.policy_version,
    }
    return canonical_json_hash(material)


__all__ = [
    "DiagnosticsPackageReceipt",
    "DiagnosticsPackageResponse",
    "diagnostics_package_receipt_hash",
]
