from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))

from zhimeng_runtime.file_changes_api import SessionFileChangeDetail, SessionFileChanges  # noqa: E402

MODELS = {
    "session-file-changes.schema.json": SessionFileChanges,
    "session-file-change-detail.schema.json": SessionFileChangeDetail,
}

if __name__ == "__main__":
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{name}"
        output = ROOT / "packages/contracts" / name
        content = (json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary = output.with_suffix('.tmp')
        temporary.write_bytes(content)
        temporary.replace(output)
