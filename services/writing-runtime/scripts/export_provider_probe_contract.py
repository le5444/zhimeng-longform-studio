from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.provider_probe import ProviderCapabilityProbeReport  # noqa: E402


FILE_NAME = "provider-capability-probe.schema.json"


def render_schema() -> str:
    schema = ProviderCapabilityProbeReport.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{FILE_NAME}"
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    target = CONTRACTS / FILE_NAME
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(render_schema(), encoding="utf-8")
    temporary.replace(target)


if __name__ == "__main__":
    main()
