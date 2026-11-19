"""Export the version-aware provider settings write contracts."""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.provider_settings_api import ProviderModelOverridePatch, ProviderProfileUpdate, ProviderSettingsUpdate

MODELS = {
    "provider-settings-update.schema.json": ProviderSettingsUpdate,
    "provider-profile-update.schema.json": ProviderProfileUpdate,
    "provider-model-override-patch.schema.json": ProviderModelOverridePatch,
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
