from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))

from zhimeng_runtime.agent_models import AgentRunAdmissionWithdrawResponse


if __name__ == "__main__":
    name = "agent-run-admission-withdraw.schema.json"
    schema = AgentRunAdmissionWithdrawResponse.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{name}"
    content = (json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination = ROOT / "packages/contracts" / name
    pending = destination.with_suffix(".tmp")
    pending.write_bytes(content)
    pending.replace(destination)
