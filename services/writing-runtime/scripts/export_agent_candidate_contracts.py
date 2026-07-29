from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.agent_candidate_models import (  # noqa: E402
    AgentCandidateOutput,
    AgentCandidateOutputV1,
    AgentCandidateOutputV2,
)
from zhimeng_runtime.provider_turn_models import (  # noqa: E402
    ProviderTurnReceipt,
    ProviderTurnReceiptListResponse,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
CONTRACTS_BY_FILE = {
    "agent-candidate-output.schema.json": AgentCandidateOutput,
    "agent-candidate-output-v1.schema.json": AgentCandidateOutputV1,
    "agent-candidate-output-v2.schema.json": AgentCandidateOutputV2,
    "provider-turn-receipt.schema.json": ProviderTurnReceipt,
    "provider-turn-receipt-list.schema.json": ProviderTurnReceiptListResponse,
}


def build_schema(model: type) -> dict[str, object]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def render_schema(file_name: str, model: type) -> str:
    schema = build_schema(model)
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return json.dumps(
        schema,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in CONTRACTS_BY_FILE.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)


if __name__ == "__main__":
    main()
