from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.creation_instruction_models import (  # noqa: E402
    CreationInstructionActivationBinding,
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
)
from zhimeng_runtime.skill_models import (
    CreationHandoffRequest,
    CreationHandoffResult,
    CreationProjectAuditRequest,
    CreationProjectAuditResult,
    CreationPrewriteGateRequest,
    CreationPrewriteGateResult,
    DeterministicSkillOutput,
    ManagedSkillActivationRequest,
    ManagedSkillActivationResponse,
    ManagedSkillCatalogResponse,
    ManagedSkillImportRequest,
    ManagedSkillImportResponse,
    ManagedSkillLifecycleCatalogResponse,
    SkillExecutionReceipt,
    SkillManifest,
    SkillRegistryResponse,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "skill-manifest.schema.json": SkillManifest,
    "skill-registry.schema.json": SkillRegistryResponse,
    "managed-skill-catalog.schema.json": ManagedSkillCatalogResponse,
    "managed-skill-lifecycle-catalog.schema.json": ManagedSkillLifecycleCatalogResponse,
    "managed-skill-import-request.schema.json": ManagedSkillImportRequest,
    "managed-skill-import.schema.json": ManagedSkillImportResponse,
    "managed-skill-activation-request.schema.json": ManagedSkillActivationRequest,
    "managed-skill-activation.schema.json": ManagedSkillActivationResponse,
    "skill-execution-receipt.schema.json": SkillExecutionReceipt,
    "deterministic-skill-output.schema.json": DeterministicSkillOutput,
    "creation-project-audit-request.schema.json": CreationProjectAuditRequest,
    "creation-project-audit-result.schema.json": CreationProjectAuditResult,
    "creation-prewrite-gate-request.schema.json": CreationPrewriteGateRequest,
    "creation-prewrite-gate-result.schema.json": CreationPrewriteGateResult,
    "creation-handoff-request.schema.json": CreationHandoffRequest,
    "creation-handoff-result.schema.json": CreationHandoffResult,
    "creation-instruction-activation.schema.json": CreationInstructionActivationBinding,
    "creation-instruction-materialization.schema.json": CreationInstructionMaterialization,
    "creation-instruction-dispatch-receipt.schema.json": CreationInstructionDispatchReceipt,
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


if __name__ == "__main__":
    main()
