from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.agent_models import AgentRunContinuationResponse, AgentRunCreateRequest  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
MODELS = {
    "agent-run-continuation.schema.json": AgentRunContinuationResponse,
    "agent-run-create-request.schema.json": AgentRunCreateRequest,
}


def main() -> None:
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        (ROOT / "packages/contracts" / name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )


if __name__ == "__main__":
    main()
