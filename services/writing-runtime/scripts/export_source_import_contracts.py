from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.source_import_models import (  # noqa: E402
    LocalFileSourceCandidate,
    LocalSourceImportCreateRequest,
    LocalSourceImportJob,
    LocalSourceInspectRequest,
    SourceImportCreateRequest,
    SourceImportJob,
    SourceImportListResponse,
    TomatoResolveRequest,
    TomatoResolveResponse,
    TomatoSearchRequest,
    TomatoSearchResponse,
)
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    LocalSourceImportExecutionReceipt,
    SourceImportExecutionReceipt,
    SourceImportExecutionReceiptRecord,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "tomato-search-request.schema.json": TomatoSearchRequest,
    "tomato-search.schema.json": TomatoSearchResponse,
    "tomato-resolve-request.schema.json": TomatoResolveRequest,
    "tomato-resolve.schema.json": TomatoResolveResponse,
    "local-source-inspect-request.schema.json": LocalSourceInspectRequest,
    "local-source-inspect.schema.json": LocalFileSourceCandidate,
    "local-source-import-create.schema.json": LocalSourceImportCreateRequest,
    "local-source-import.schema.json": LocalSourceImportJob,
    "local-source-import-execution-receipt.schema.json": (
        LocalSourceImportExecutionReceipt
    ),
    "source-import-create.schema.json": SourceImportCreateRequest,
    "source-import.schema.json": SourceImportJob,
    "source-import-list.schema.json": SourceImportListResponse,
    "source-import-execution-receipt.schema.json": SourceImportExecutionReceipt,
    "source-import-execution-receipt-record.schema.json": (
        SourceImportExecutionReceiptRecord
    ),
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
