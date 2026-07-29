from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.four_skills_raw_adaptation_models import (  # noqa: E402
    FourSkillsInstructionChainHandoffRequest,
    FourSkillsInstructionChainHandoffResponse,
    FourSkillsRawAdaptationMaterializeRequest,
    FourSkillsRawAdaptationPlanRequest,
    FourSkillsRawAdaptationResponse,
    FourSkillsWorkflowBindingRequest,
    FourSkillsWorkflowBindingResponse,
    FourSkillsWorkflowOrchestrationRequest,
    FourSkillsWorkflowOrchestrationResponse,
    ProjectInstructionChainHandoffBindRequest,
    ProjectInstructionChainHandoffBindingResponse,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "four-skills-raw-adaptation-plan-request.schema.json": FourSkillsRawAdaptationPlanRequest,
    "four-skills-raw-adaptation-materialize-request.schema.json": FourSkillsRawAdaptationMaterializeRequest,
    "four-skills-raw-adaptation-response.schema.json": FourSkillsRawAdaptationResponse,
    "four-skills-workflow-binding-request.schema.json": FourSkillsWorkflowBindingRequest,
    "four-skills-workflow-binding-response.schema.json": FourSkillsWorkflowBindingResponse,
    "four-skills-workflow-orchestration-request.schema.json": FourSkillsWorkflowOrchestrationRequest,
    "four-skills-workflow-orchestration-response.schema.json": FourSkillsWorkflowOrchestrationResponse,
    "four-skills-instruction-chain-handoff-request.schema.json": FourSkillsInstructionChainHandoffRequest,
    "four-skills-instruction-chain-handoff-response.schema.json": FourSkillsInstructionChainHandoffResponse,
    "project-instruction-chain-handoff-bind-request.schema.json": ProjectInstructionChainHandoffBindRequest,
    "project-instruction-chain-handoff-binding-response.schema.json": ProjectInstructionChainHandoffBindingResponse,
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
