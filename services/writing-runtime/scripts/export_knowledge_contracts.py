from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.knowledge_models import (  # noqa: E402
    KnowledgeCandidate,
    KnowledgeCandidateDecisionRequest,
    KnowledgeCandidateList,
    KnowledgeCandidateUpdateRequest,
    KnowledgeCard,
    KnowledgeCardList,
    KnowledgeScopedCardList,
    KnowledgeCardStateRequest,
    KnowledgeFormalizationReceipt,
    KnowledgeFormalizationReceiptV2,
    KnowledgeFormalizationRecord,
    KnowledgeFormalizationRequest,
    KnowledgePromotionRequest,
    KnowledgeRollbackRequest,
    KnowledgeVersionLineageReceipt,
    KnowledgeVersionLineageReceiptV2,
)
from zhimeng_runtime.knowledge_scope_models import KnowledgeScopeRecord  # noqa: E402
from zhimeng_runtime.project_kb_models import (  # noqa: E402
    ProjectKbRebuildRequest,
    ProjectKbRebuildResult,
    ProjectKbStateRecord,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "knowledge-candidate.schema.json": KnowledgeCandidate,
    "knowledge-candidate-list.schema.json": KnowledgeCandidateList,
    "knowledge-candidate-update.schema.json": KnowledgeCandidateUpdateRequest,
    "knowledge-candidate-decision.schema.json": KnowledgeCandidateDecisionRequest,
    "knowledge-promotion.schema.json": KnowledgePromotionRequest,
    "knowledge-card.schema.json": KnowledgeCard,
    "knowledge-card-list.schema.json": KnowledgeCardList,
    "knowledge-scoped-card-list.schema.json": KnowledgeScopedCardList,
    "knowledge-card-state.schema.json": KnowledgeCardStateRequest,
    "knowledge-card-rollback.schema.json": KnowledgeRollbackRequest,
    "knowledge-version-lineage-receipt.schema.json": KnowledgeVersionLineageReceipt,
    "knowledge-version-lineage-receipt-v2.schema.json": KnowledgeVersionLineageReceiptV2,
    "knowledge-formalization-request.schema.json": KnowledgeFormalizationRequest,
    "knowledge-formalization-receipt.schema.json": KnowledgeFormalizationReceipt,
    "knowledge-formalization-receipt-v2.schema.json": KnowledgeFormalizationReceiptV2,
    "knowledge-formalization-record.schema.json": KnowledgeFormalizationRecord,
    "knowledge-provenance-ref.schema.json": KnowledgeProvenanceRef,
    "knowledge-scope-record.schema.json": KnowledgeScopeRecord,
    "project-kb-rebuild-request.schema.json": ProjectKbRebuildRequest,
    "project-kb-rebuild-result.schema.json": ProjectKbRebuildResult,
    "project-kb-state-record.schema.json": ProjectKbStateRecord,
}


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
        (CONTRACTS / file_name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
