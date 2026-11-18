from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.backup_models import (  # noqa: E402
    RuntimeBackupListResponse,
    RuntimeBackupManifest,
    RuntimeBackupReceipt,
    RuntimeBackupResponse,
    RuntimeRestoreRequest,
    RuntimeRestoreReceipt,
    RuntimeRestoreResponse,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "packages" / "contracts"
MODELS = {
    "runtime-backup-manifest.schema.json": RuntimeBackupManifest,
    "runtime-backup-receipt.schema.json": RuntimeBackupReceipt,
    "runtime-backup-response.schema.json": RuntimeBackupResponse,
    "runtime-backup-list.schema.json": RuntimeBackupListResponse,
    "runtime-restore-request.schema.json": RuntimeRestoreRequest,
    "runtime-restore-receipt.schema.json": RuntimeRestoreReceipt,
    "runtime-restore-response.schema.json": RuntimeRestoreResponse,
}


def render_schema(file_name: str, model: type) -> str:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)


if __name__ == "__main__":
    main()
