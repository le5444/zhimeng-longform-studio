from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.formal_draft_continuous_o2_models import (  # noqa: E402
    FormalDraftContinuousO2EvaluateRequest,
    FormalDraftContinuousO2Response,
)
from zhimeng_runtime.formal_draft_models import (  # noqa: E402
    FormalDraftAuthorDecisionRequest,
    FormalDraftAuthorDecisionResponse,
    FormalDraftAuthorizationResponse,
    FormalDraftEvaluateRequest,
)
from zhimeng_runtime.formal_draft_provenance_models import (  # noqa: E402
    FormalDraftProvenanceEvaluateRequest,
    FormalDraftProvenanceResponse,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "formal-draft-authorization-request.schema.json": FormalDraftEvaluateRequest,
    "formal-draft-authorization-response.schema.json": FormalDraftAuthorizationResponse,
    "formal-draft-author-decision-request.schema.json": FormalDraftAuthorDecisionRequest,
    "formal-draft-author-decision-response.schema.json": FormalDraftAuthorDecisionResponse,
    "formal-draft-provenance-request.schema.json": FormalDraftProvenanceEvaluateRequest,
    "formal-draft-provenance-response.schema.json": FormalDraftProvenanceResponse,
    "formal-draft-continuous-o2-request.schema.json": FormalDraftContinuousO2EvaluateRequest,
    "formal-draft-continuous-o2-response.schema.json": FormalDraftContinuousO2Response,
}


def render_schema(file_name: str, model: type) -> str:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)
        print(f"wrote {target.name}")


if __name__ == "__main__":
    main()
