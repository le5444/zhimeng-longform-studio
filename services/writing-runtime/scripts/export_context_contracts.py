from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.context_models import (  # noqa: E402
    ContextCreateRequest,
    ContextPack,
)
from zhimeng_runtime.nlcl_models import NlclRouteReceipt  # noqa: E402
from zhimeng_runtime.nlcl_route_execution_models import (  # noqa: E402
    NlclRouteExecutionReceipt,
    NlclRouteExecutionState,
)
from zhimeng_runtime.prewrite_models import PrewriteOperationPackage  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "context-pack-create.schema.json": ContextCreateRequest,
    "context-pack.schema.json": ContextPack,
    "nlcl-route-receipt.schema.json": NlclRouteReceipt,
    "nlcl-route-execution-receipt.schema.json": NlclRouteExecutionReceipt,
    "nlcl-route-execution-state.schema.json": NlclRouteExecutionState,
    "prewrite-operation-package.schema.json": PrewriteOperationPackage,
}


def render_schema(file_name: str, model: type) -> str:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return json.dumps(
        schema,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)


if __name__ == "__main__":
    main()
