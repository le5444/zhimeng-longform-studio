from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.agent_models import AgentMessageListResponse
from zhimeng_runtime.command_background_api import CommandBackgroundResponse
from zhimeng_runtime.execution_protocol import Run


def save(path, value):
    if path.exists() and json.loads(path.read_bytes()) == value:
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    temporary.replace(path)


if __name__ == "__main__":
    for filename, model in (("command-background-events.schema.json", CommandBackgroundResponse),
                             ("agent-message-list.schema.json", AgentMessageListResponse)):
        value = model.model_json_schema(by_alias=True)
        value["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["$id"] = f"https://zhimeng.local/contracts/{filename}"
        save(ROOT / "packages/contracts" / filename, value)
    additions = {name: Run.model_json_schema(by_alias=True)["properties"][name]
                 for name in ("origin_kind", "stop_generation")}
    # Run is embedded by several existing entry schemas. Only the new optional
    # fields change; preserve their existing refs, examples and lifecycle rules.
    for path in (ROOT / "packages/contracts").glob("*.schema.json"):
        if path.name == "execution-protocol.schema.json":
            # This hand-written schema also carries lifecycle conditions. Its
            # two optional Run properties are maintained in place, not expanded.
            continue
        value = json.loads(path.read_bytes())
        changed = False
        for name, definition in value.get("$defs", {}).items():
            if name in {"Run", "run"} and "run_id" in definition.get("properties", {}):
                definition["properties"].update(additions)
                changed = True
        if changed:
            save(path, value)
