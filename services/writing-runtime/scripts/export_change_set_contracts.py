from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.hard_fact_models import (
    HardFactEvaluateRequest,
    HardFactGateResponse,
)
from zhimeng_runtime.gate_pipeline_models import (
    GatePipelineEvaluateRequest,
    GatePipelineResponse,
)
from zhimeng_runtime.eval_corpus_export_models import (
    EvalCorpusExportRequest,
    EvalCorpusExportResponse,
)
from zhimeng_runtime.eval_corpus_regression_models import (
    EvalCorpusBaselineFreezeRequest,
    EvalCorpusBaselineResponse,
    EvalCorpusRegressionEvaluateRequest,
    EvalCorpusRegressionResponse,
)
from zhimeng_runtime.formal_knowledge_promotion_models import (
    FormalKnowledgePromotionActionRequest,
    FormalKnowledgePromotionEvaluateRequest,
    FormalKnowledgePromotionResponse,
)
from zhimeng_runtime.literary_quality_models import (
    LiteraryQualityEvaluateRequest,
    LiteraryQualityGateResponse,
)
from zhimeng_runtime.change_set_models import (
    ChangeSetApplyRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetListResponse,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
    NlclCallUsageContinuityRequest,
    NlclCallUsageContinuityResponse,
    NlclOriginalCallUsageExportRequest,
    NlclOriginalCallUsageExportResponse,
    NlclOriginalChapterOpsUsageExportRequest,
    NlclOriginalChapterOpsUsageExportResponse,
    NlclOriginalNovelStateBridgeRequest,
    NlclOriginalNovelStateBridgeResponse,
    NlclOriginalNovelStateSyncRequest,
    NlclOriginalNovelStateSyncResponse,
    NlclOriginalPrewriteContextReportRequest,
    NlclOriginalPrewriteContextReportResponse,
    NlclOriginalTruthContentExtractRequest,
    NlclOriginalTruthContentExtractResponse,
    NlclOriginalMandatoryUsagePlanRequest,
    NlclOriginalMandatoryUsagePlanResponse,
    NlclOriginalMandatoryUsageClosureRequest,
    NlclOriginalMandatoryUsageClosureResponse,
    NlclOriginalOperationPackageLikeRequest,
    NlclOriginalOperationPackageLikeResponse,
    NlclOriginalLibraryLayoutInventoryRequest,
    NlclOriginalLibraryLayoutInventoryResponse,
    NlclOriginalLibraryLayoutCompletenessRequest,
    NlclOriginalLibraryLayoutCompletenessResponse,
    NlclOriginalBoundedLibraryActivationRequest,
    NlclOriginalBoundedLibraryActivationResponse,
    FeedbackCandidatePromotionRequest,
    FeedbackCandidatePromotionResponse,
    FeedbackCandidateResponse,
    ReaderReviewDecisionRequest,
    ReaderReviewDecisionResponse,
    ReaderReviewRequest,
    ReaderReviewResponse,
    NlclWritebackSyncRequest,
    NlclWritebackSyncResponse,
    WritebackEvidenceResponse,
    WritebackManifest,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "change-set-create.schema.json": ChangeSetCreateRequest,
    "change-set.schema.json": ChangeSetResponse,
    "change-set-list.schema.json": ChangeSetListResponse,
    "change-set-decisions.schema.json": ChangeSetDecisionRequest,
    "change-set-apply.schema.json": ChangeSetApplyRequest,
    "change-set-rollback.schema.json": ChangeSetRollbackRequest,
    "writeback-manifest.schema.json": WritebackManifest,
    "writeback-evidence-response.schema.json": WritebackEvidenceResponse,
    "nlcl-writeback-sync-request.schema.json": NlclWritebackSyncRequest,
    "nlcl-writeback-sync-response.schema.json": NlclWritebackSyncResponse,
    "nlcl-call-usage-continuity-request.schema.json": NlclCallUsageContinuityRequest,
    "nlcl-call-usage-continuity-response.schema.json": NlclCallUsageContinuityResponse,
    "nlcl-original-call-usage-export-request.schema.json": NlclOriginalCallUsageExportRequest,
    "nlcl-original-call-usage-export-response.schema.json": NlclOriginalCallUsageExportResponse,
    "nlcl-original-chapter-ops-usage-export-request.schema.json": NlclOriginalChapterOpsUsageExportRequest,
    "nlcl-original-chapter-ops-usage-export-response.schema.json": NlclOriginalChapterOpsUsageExportResponse,
    "nlcl-original-novel-state-bridge-request.schema.json": NlclOriginalNovelStateBridgeRequest,
    "nlcl-original-novel-state-bridge-response.schema.json": NlclOriginalNovelStateBridgeResponse,
    "nlcl-original-novel-state-sync-request.schema.json": NlclOriginalNovelStateSyncRequest,
    "nlcl-original-novel-state-sync-response.schema.json": NlclOriginalNovelStateSyncResponse,
    "nlcl-original-prewrite-context-report-request.schema.json": NlclOriginalPrewriteContextReportRequest,
    "nlcl-original-prewrite-context-report-response.schema.json": NlclOriginalPrewriteContextReportResponse,
    "nlcl-original-truth-content-extract-request.schema.json": NlclOriginalTruthContentExtractRequest,
    "nlcl-original-truth-content-extract-response.schema.json": NlclOriginalTruthContentExtractResponse,
    "nlcl-original-mandatory-usage-plan-request.schema.json": NlclOriginalMandatoryUsagePlanRequest,
    "nlcl-original-mandatory-usage-plan-response.schema.json": NlclOriginalMandatoryUsagePlanResponse,
    "nlcl-original-mandatory-usage-closure-request.schema.json": NlclOriginalMandatoryUsageClosureRequest,
    "nlcl-original-mandatory-usage-closure-response.schema.json": NlclOriginalMandatoryUsageClosureResponse,
    "nlcl-original-operation-package-like-request.schema.json": NlclOriginalOperationPackageLikeRequest,
    "nlcl-original-operation-package-like-response.schema.json": NlclOriginalOperationPackageLikeResponse,
    "nlcl-original-library-layout-inventory-request.schema.json": NlclOriginalLibraryLayoutInventoryRequest,
    "nlcl-original-library-layout-inventory-response.schema.json": NlclOriginalLibraryLayoutInventoryResponse,
    "nlcl-original-library-layout-completeness-request.schema.json": NlclOriginalLibraryLayoutCompletenessRequest,
    "nlcl-original-library-layout-completeness-response.schema.json": NlclOriginalLibraryLayoutCompletenessResponse,
    "nlcl-original-bounded-library-activation-request.schema.json": NlclOriginalBoundedLibraryActivationRequest,
    "nlcl-original-bounded-library-activation-response.schema.json": NlclOriginalBoundedLibraryActivationResponse,
    "reader-review-request.schema.json": ReaderReviewRequest,
    "reader-review-response.schema.json": ReaderReviewResponse,
    "reader-review-decision-request.schema.json": ReaderReviewDecisionRequest,
    "reader-review-decision-response.schema.json": ReaderReviewDecisionResponse,
    "feedback-candidate-response.schema.json": FeedbackCandidateResponse,
    "feedback-candidate-promotion-request.schema.json": FeedbackCandidatePromotionRequest,
    "feedback-candidate-promotion-response.schema.json": FeedbackCandidatePromotionResponse,
    "literary-quality-gate-request.schema.json": LiteraryQualityEvaluateRequest,
    "literary-quality-gate-response.schema.json": LiteraryQualityGateResponse,
    "hard-fact-gate-request.schema.json": HardFactEvaluateRequest,
    "hard-fact-gate-response.schema.json": HardFactGateResponse,
    "gate-pipeline-request.schema.json": GatePipelineEvaluateRequest,
    "gate-pipeline-response.schema.json": GatePipelineResponse,
    "formal-knowledge-promotion-request.schema.json": FormalKnowledgePromotionEvaluateRequest,
    "formal-knowledge-promotion-action-request.schema.json": FormalKnowledgePromotionActionRequest,
    "formal-knowledge-promotion-response.schema.json": FormalKnowledgePromotionResponse,
    "eval-corpus-export-request.schema.json": EvalCorpusExportRequest,
    "eval-corpus-export-response.schema.json": EvalCorpusExportResponse,
    "eval-corpus-baseline-request.schema.json": EvalCorpusBaselineFreezeRequest,
    "eval-corpus-baseline-response.schema.json": EvalCorpusBaselineResponse,
    "eval-corpus-regression-request.schema.json": EvalCorpusRegressionEvaluateRequest,
    "eval-corpus-regression-response.schema.json": EvalCorpusRegressionResponse,
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
