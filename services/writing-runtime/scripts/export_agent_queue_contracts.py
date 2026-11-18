from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.agent_queue import QueueInput, QueueUpdate, QueueState, QueueDispatchResponse, QueueSteerRequest, QueueSteerResponse

MODELS = {
    "agent-queue-input.schema.json": QueueInput,
    "agent-queue-update.schema.json": QueueUpdate,
    "agent-queue-state.schema.json": QueueState,
    "agent-queue-dispatch.schema.json": QueueDispatchResponse,
    "agent-queue-steer-request.schema.json": QueueSteerRequest,
    "agent-queue-steer-response.schema.json": QueueSteerResponse,
}

if __name__ == "__main__":
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        (ROOT / "packages/contracts" / name).write_bytes((json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
