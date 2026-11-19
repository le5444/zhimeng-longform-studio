"""Export model-facing file-write and command-session argument contracts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.agent_tools import CommandSessionArguments, ComputerRunCommandArguments, ProjectWriteFileArguments, ProjectEditFileArguments, SkillReadFileArguments

MODELS = {
    "tool-edit-file-arguments.schema.json": ProjectEditFileArguments,
    "tool-skill-read-file-arguments.schema.json": SkillReadFileArguments,
    "tool-write-file-arguments.schema.json": ProjectWriteFileArguments,
    "tool-command-session-arguments.schema.json": CommandSessionArguments,
    "tool-run-command-arguments.schema.json": ComputerRunCommandArguments,
}

if __name__ == "__main__":
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        target = ROOT / "packages/contracts" / name
        if target.exists() and json.loads(target.read_bytes()) == schema:
            continue
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes((json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        temporary.replace(target)
