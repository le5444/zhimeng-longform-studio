from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.session_recall import SessionReadArguments, SessionSearchArguments
from zhimeng_runtime.session_source_api import SessionHistorySearchResponse, SessionSourceResponse
from zhimeng_runtime.session_plan import PlanReadArguments, SessionPlanResponse
from zhimeng_runtime.run_artifacts import RunArtifactsResponse


if __name__ == "__main__":
    for name, model in (
        ("session-search-arguments.schema.json", SessionSearchArguments),
        ("session-read-arguments.schema.json", SessionReadArguments),
        ("session-source-response.schema.json", SessionSourceResponse),
        ("session-history-search-response.schema.json", SessionHistorySearchResponse),
        ("plan-read-arguments.schema.json", PlanReadArguments),
        ("session-plan-response.schema.json", SessionPlanResponse),
        ("run-artifacts-response.schema.json", RunArtifactsResponse),
    ):
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        target = ROOT / "packages/contracts" / name
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes((json.dumps(schema, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        temporary.replace(target)
