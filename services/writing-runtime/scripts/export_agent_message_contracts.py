from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.agent_models import AgentMessageListResponse, AgentActivityListResponse


if __name__ == "__main__":
    for name, model in (("agent-message-list.schema.json", AgentMessageListResponse),
                        ("agent-activity-list.schema.json", AgentActivityListResponse)):
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        data = (json.dumps(schema, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        target = ROOT / "packages/contracts" / name
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
