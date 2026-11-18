from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))

from zhimeng_runtime.search_models import SearchHit, SearchQueryResponse


if __name__ == "__main__":
    for name, model in {
        "project-search-hit.schema.json": SearchHit,
        "project-search-query-response.schema.json": SearchQueryResponse,
    }.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        payload = (json.dumps(schema, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        target = ROOT / "packages/contracts" / name
        pending = target.with_suffix(".tmp")
        pending.write_bytes(payload)
        pending.replace(target)
