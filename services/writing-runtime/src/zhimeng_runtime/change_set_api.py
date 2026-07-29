from __future__ import annotations

from pathlib import Path
from typing import Callable, NoReturn, TypeVar

from fastapi import APIRouter, HTTPException, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.hard_fact_models import (
    HardFactEvaluateRequest,
    HardFactGateResponse,
)
from zhimeng_runtime.gate_pipeline_models import (
    GatePipelineEvaluateRequest,
    GatePipelineResponse,
)
from zhimeng_runtime.literary_quality_models import (
    LiteraryQualityEvaluateRequest,
    LiteraryQualityGateResponse,
)
from zhimeng_runtime.llm_literary_score_models import (
    LlmLiteraryScoreEvaluateRequest,
    LlmLiteraryScoreResponse,
)
from zhimeng_runtime.formal_knowledge_promotion_models import (
    FormalKnowledgePromotionActionRequest,
    FormalKnowledgePromotionEvaluateRequest,
    FormalKnowledgePromotionResponse,
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
from zhimeng_runtime.change_set_models import (
    ChangeSetApplyRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetListResponse,
    ChangeSetOrigin,
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
)
from zhimeng_runtime.post_draft_gate_models import (
    PostDraftGateRequest,
    PostDraftGateResponse,
)
from zhimeng_runtime.change_set_service import (
    ChangeSetConflictError,
    ChangeSetError,
    ChangeSetGateBlockedError,
    ChangeSetGatePipelineBlockedError,
    ChangeSetGateError,
    ChangeSetGateMissingError,
    ChangeSetGateStaleError,
    ChangeSetGateUnavailableError,
    ChangeSetInputError,
    ChangeSetNotFoundError,
    ChangeSetService,
    ChangeSetStateError,
    ChangeSetWritebackError,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.project_locks import ProjectLockRegistry


T = TypeVar("T")


def create_change_set_router(
    service: ChangeSetService | None,
    store: SqliteStore,
    locks: ProjectLockRegistry,
    *,
    service_error: str | None = None,
) -> APIRouter:
    router = APIRouter(tags=["change-sets"])

    def require_service() -> ChangeSetService:
        if service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service_error or "No trusted project roots are configured",
            )
        return service

    def raise_change_set_error(exc: ChangeSetError) -> NoReturn:
        if isinstance(exc, ChangeSetNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "change_set_not_found"
            retryable = False
        elif isinstance(exc, ChangeSetConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "change_set_conflict"
            retryable = True
        elif isinstance(exc, ChangeSetStateError):
            status_code = status.HTTP_409_CONFLICT
            code = "change_set_state_invalid"
            retryable = False
        elif isinstance(exc, ChangeSetInputError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "change_set_input_invalid"
            retryable = False
        elif isinstance(exc, ChangeSetWritebackError):
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "change_set_writeback_failed"
            retryable = True
        elif isinstance(exc, ChangeSetGateUnavailableError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = exc.code
            retryable = True
        elif isinstance(
            exc,
            (
                ChangeSetGateBlockedError,
                ChangeSetGatePipelineBlockedError,
                ChangeSetGateMissingError,
                ChangeSetGateStaleError,
            ),
        ):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = False
        elif isinstance(exc, ChangeSetGateError):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = False
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "change_set_failed"
            retryable = True
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    def binding_root(project_id: str) -> Path:
        binding = store.get_project_binding(project_id)
        if binding is None:
            raise ChangeSetNotFoundError("Project binding was not found")
        return Path(binding.root_path)

    async def under_project_lock(project_id: str, callback: Callable[[], T]) -> T:
        try:
            root = await run_in_threadpool(binding_root, project_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        async with locks.for_path(root):
            try:
                return await run_in_threadpool(callback)
            except ChangeSetError as exc:
                raise_change_set_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/change-sets",
        response_model=ChangeSetResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_change_set(
        project_id: str,
        request: ChangeSetCreateRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        if request.origin is not ChangeSetOrigin.MANUAL:
            raise ApiError(
                "Public change-set creation only accepts manual origin; "
                "Agent and Skill provenance is Runtime-owned",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="change_set_origin_untrusted",
                retryable=False,
            )
        return await under_project_lock(
            project_id,
            lambda: active.create(project_id, request),
        )

    @router.get(
        "/api/v1/projects/{project_id}/change-sets",
        response_model=ChangeSetListResponse,
    )
    async def list_change_sets(project_id: str) -> ChangeSetListResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.list_for_project, project_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.get(
        "/api/v1/change-sets/{change_set_id}",
        response_model=ChangeSetResponse,
    )
    async def get_change_set(change_set_id: str) -> ChangeSetResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.patch(
        "/api/v1/change-sets/{change_set_id}/decisions",
        response_model=ChangeSetResponse,
    )
    async def decide_change_set(
        change_set_id: str,
        request: ChangeSetDecisionRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.decide(change_set_id, request),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/apply",
        response_model=ChangeSetResponse,
    )
    async def apply_change_set(
        change_set_id: str,
        request: ChangeSetApplyRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.apply(change_set_id, request),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/post-draft-gate",
        response_model=ChangeSetResponse,
    )
    async def run_post_draft_gate(
        change_set_id: str,
        request: PostDraftGateRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.post_draft_gate(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/post-draft-gate",
        response_model=PostDraftGateResponse,
    )
    async def get_post_draft_gate(change_set_id: str) -> PostDraftGateResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_post_draft_gate, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/rollback",
        response_model=ChangeSetResponse,
    )
    async def rollback_change_set(
        change_set_id: str,
        request: ChangeSetRollbackRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.rollback(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/writeback-evidence",
        response_model=WritebackEvidenceResponse,
    )
    async def get_writeback_evidence(change_set_id: str) -> WritebackEvidenceResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_writeback_evidence, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-writeback-sync",
        response_model=NlclWritebackSyncResponse,
    )
    async def get_nlcl_writeback_sync(
        change_set_id: str,
    ) -> NlclWritebackSyncResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_nlcl_writeback_sync,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-writeback-sync",
        response_model=NlclWritebackSyncResponse,
    )
    async def sync_nlcl_writeback_evidence(
        change_set_id: str,
        request: NlclWritebackSyncRequest,
    ) -> NlclWritebackSyncResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.sync_nlcl_writeback_evidence(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-call-usage-continuity",
        response_model=NlclCallUsageContinuityResponse,
    )
    async def get_nlcl_call_usage_continuity(
        change_set_id: str,
    ) -> NlclCallUsageContinuityResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_nlcl_call_usage_continuity,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-call-usage-continuity",
        response_model=NlclCallUsageContinuityResponse,
    )
    async def record_nlcl_call_usage_continuity(
        change_set_id: str,
        request: NlclCallUsageContinuityRequest,
    ) -> NlclCallUsageContinuityResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.record_nlcl_call_usage_continuity(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-call-usage-export",
        response_model=NlclOriginalCallUsageExportResponse,
    )
    async def get_original_nlcl_call_usage_export(
        change_set_id: str,
    ) -> NlclOriginalCallUsageExportResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_call_usage_export,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-call-usage-export",
        response_model=NlclOriginalCallUsageExportResponse,
    )
    async def export_original_nlcl_call_usage(
        change_set_id: str,
        request: NlclOriginalCallUsageExportRequest,
    ) -> NlclOriginalCallUsageExportResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_call_usage(change_set_id, request),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-chapter-ops-usage-export",
        response_model=NlclOriginalChapterOpsUsageExportResponse,
    )
    async def get_original_nlcl_chapter_ops_usage_export(
        change_set_id: str,
    ) -> NlclOriginalChapterOpsUsageExportResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_chapter_ops_usage_export,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-chapter-ops-usage-export",
        response_model=NlclOriginalChapterOpsUsageExportResponse,
    )
    async def export_original_nlcl_chapter_ops_usage(
        change_set_id: str,
        request: NlclOriginalChapterOpsUsageExportRequest,
    ) -> NlclOriginalChapterOpsUsageExportResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_chapter_ops_usage(
                change_set_id, request
            ),
        )




    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-bridge",
        response_model=NlclOriginalNovelStateBridgeResponse,
    )
    async def get_original_nlcl_novel_state_bridge(
        change_set_id: str,
    ) -> NlclOriginalNovelStateBridgeResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_novel_state_bridge,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-bridge",
        response_model=NlclOriginalNovelStateBridgeResponse,
    )
    async def export_original_nlcl_novel_state_bridge(
        change_set_id: str,
        request: NlclOriginalNovelStateBridgeRequest,
    ) -> NlclOriginalNovelStateBridgeResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_novel_state_bridge(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-sync",
        response_model=NlclOriginalNovelStateSyncResponse,
    )
    async def get_original_nlcl_novel_state_sync(
        change_set_id: str,
    ) -> NlclOriginalNovelStateSyncResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_novel_state_sync,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-sync",
        response_model=NlclOriginalNovelStateSyncResponse,
    )
    async def export_original_nlcl_novel_state_sync(
        change_set_id: str,
        request: NlclOriginalNovelStateSyncRequest,
    ) -> NlclOriginalNovelStateSyncResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_novel_state_sync(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-prewrite-context-report",
        response_model=NlclOriginalPrewriteContextReportResponse,
    )
    async def get_original_nlcl_prewrite_context_report(
        change_set_id: str,
    ) -> NlclOriginalPrewriteContextReportResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_prewrite_context_report,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-prewrite-context-report",
        response_model=NlclOriginalPrewriteContextReportResponse,
    )
    async def export_original_nlcl_prewrite_context_report(
        change_set_id: str,
        request: NlclOriginalPrewriteContextReportRequest,
    ) -> NlclOriginalPrewriteContextReportResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_prewrite_context_report(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-truth-content-extract",
        response_model=NlclOriginalTruthContentExtractResponse,
    )
    async def get_original_nlcl_truth_content_extract(
        change_set_id: str,
    ) -> NlclOriginalTruthContentExtractResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_truth_content_extract,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-truth-content-extract",
        response_model=NlclOriginalTruthContentExtractResponse,
    )
    async def export_original_nlcl_truth_content_extract(
        change_set_id: str,
        request: NlclOriginalTruthContentExtractRequest,
    ) -> NlclOriginalTruthContentExtractResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_truth_content_extract(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-plan",
        response_model=NlclOriginalMandatoryUsagePlanResponse,
    )
    async def get_original_nlcl_mandatory_usage_plan(
        change_set_id: str,
    ) -> NlclOriginalMandatoryUsagePlanResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_mandatory_usage_plan,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-plan",
        response_model=NlclOriginalMandatoryUsagePlanResponse,
    )
    async def export_original_nlcl_mandatory_usage_plan(
        change_set_id: str,
        request: NlclOriginalMandatoryUsagePlanRequest,
    ) -> NlclOriginalMandatoryUsagePlanResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_mandatory_usage_plan(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-closure",
        response_model=NlclOriginalMandatoryUsageClosureResponse,
    )
    async def get_original_nlcl_mandatory_usage_closure(
        change_set_id: str,
    ) -> NlclOriginalMandatoryUsageClosureResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_mandatory_usage_closure,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-closure",
        response_model=NlclOriginalMandatoryUsageClosureResponse,
    )
    async def export_original_nlcl_mandatory_usage_closure(
        change_set_id: str,
        request: NlclOriginalMandatoryUsageClosureRequest,
    ) -> NlclOriginalMandatoryUsageClosureResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_mandatory_usage_closure(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-operation-package-like",
        response_model=NlclOriginalOperationPackageLikeResponse,
    )
    async def get_original_nlcl_operation_package_like(
        change_set_id: str,
    ) -> NlclOriginalOperationPackageLikeResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_operation_package_like,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-operation-package-like",
        response_model=NlclOriginalOperationPackageLikeResponse,
    )
    async def export_original_nlcl_operation_package_like(
        change_set_id: str,
        request: NlclOriginalOperationPackageLikeRequest,
    ) -> NlclOriginalOperationPackageLikeResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_operation_package_like(
                change_set_id, request
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-inventory",
        response_model=NlclOriginalLibraryLayoutInventoryResponse,
    )
    async def get_original_nlcl_library_layout_inventory(
        change_set_id: str,
    ) -> NlclOriginalLibraryLayoutInventoryResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_library_layout_inventory,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-inventory",
        response_model=NlclOriginalLibraryLayoutInventoryResponse,
    )
    async def export_original_nlcl_library_layout_inventory(
        change_set_id: str,
        request: NlclOriginalLibraryLayoutInventoryRequest,
    ) -> NlclOriginalLibraryLayoutInventoryResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_library_layout_inventory(
                change_set_id, request
            ),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-completeness",
        response_model=NlclOriginalLibraryLayoutCompletenessResponse,
    )
    async def get_original_nlcl_library_layout_completeness(
        change_set_id: str,
    ) -> NlclOriginalLibraryLayoutCompletenessResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_library_layout_completeness,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-completeness",
        response_model=NlclOriginalLibraryLayoutCompletenessResponse,
    )
    async def export_original_nlcl_library_layout_completeness(
        change_set_id: str,
        request: NlclOriginalLibraryLayoutCompletenessRequest,
    ) -> NlclOriginalLibraryLayoutCompletenessResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_library_layout_completeness(
                change_set_id, request
            ),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-bounded-library-activation",
        response_model=NlclOriginalBoundedLibraryActivationResponse,
    )
    async def get_original_nlcl_bounded_library_activation(
        change_set_id: str,
    ) -> NlclOriginalBoundedLibraryActivationResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_original_nlcl_bounded_library_activation,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/nlcl-original-bounded-library-activation",
        response_model=NlclOriginalBoundedLibraryActivationResponse,
    )
    async def export_original_nlcl_bounded_library_activation(
        change_set_id: str,
        request: NlclOriginalBoundedLibraryActivationRequest,
    ) -> NlclOriginalBoundedLibraryActivationResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_original_nlcl_bounded_library_activation(
                change_set_id, request
            ),
        )




    @router.get(
        "/api/v1/change-sets/{change_set_id}/gate-pipeline",
        response_model=GatePipelineResponse,
    )
    async def get_gate_pipeline(change_set_id: str) -> GatePipelineResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_gate_pipeline, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/gate-pipeline/evaluate",
        response_model=GatePipelineResponse,
    )
    async def evaluate_gate_pipeline(
        change_set_id: str,
        request: GatePipelineEvaluateRequest | None = None,
    ) -> GatePipelineResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_gate_pipeline(
                change_set_id,
                request or GatePipelineEvaluateRequest(),
            ),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/hard-fact-gate",
        response_model=HardFactGateResponse,
    )
    async def get_hard_fact_gate(change_set_id: str) -> HardFactGateResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_hard_fact_gate, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/hard-fact-gate/evaluate",
        response_model=HardFactGateResponse,
    )
    async def evaluate_hard_fact_gate(
        change_set_id: str,
        request: HardFactEvaluateRequest | None = None,
    ) -> HardFactGateResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_hard_fact_gate(
                change_set_id,
                request or HardFactEvaluateRequest(),
            ),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/literary-quality-gate",
        response_model=LiteraryQualityGateResponse,
    )
    async def get_literary_quality_gate(
        change_set_id: str,
    ) -> LiteraryQualityGateResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_literary_quality_gate,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/literary-quality-gate/evaluate",
        response_model=LiteraryQualityGateResponse,
    )
    async def evaluate_literary_quality_gate(
        change_set_id: str,
        request: LiteraryQualityEvaluateRequest | None = None,
    ) -> LiteraryQualityGateResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_literary_quality_gate(
                change_set_id,
                request or LiteraryQualityEvaluateRequest(),
            ),
        )



    @router.get(
        "/api/v1/change-sets/{change_set_id}/llm-literary-scores",
        response_model=LlmLiteraryScoreResponse,
    )
    async def get_llm_literary_score(
        change_set_id: str,
    ) -> LlmLiteraryScoreResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_llm_literary_score,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/llm-literary-scores/evaluate",
        response_model=LlmLiteraryScoreResponse,
    )
    async def evaluate_llm_literary_score(
        change_set_id: str,
        request: LlmLiteraryScoreEvaluateRequest | None = None,
    ) -> LlmLiteraryScoreResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_llm_literary_score(
                change_set_id,
                request or LlmLiteraryScoreEvaluateRequest(),
            ),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/reader-review",
        response_model=ReaderReviewResponse,
    )
    async def get_reader_review(change_set_id: str) -> ReaderReviewResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_reader_review, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/reader-review",
        response_model=ReaderReviewResponse,
    )
    async def create_reader_review(
        change_set_id: str,
        request: ReaderReviewRequest,
    ) -> ReaderReviewResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.create_reader_review(change_set_id, request),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/reader-review-decision",
        response_model=ReaderReviewDecisionResponse,
    )
    async def get_reader_review_decision(
        change_set_id: str,
    ) -> ReaderReviewDecisionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_reader_review_decision,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/reader-review-decision",
        response_model=ReaderReviewDecisionResponse,
    )
    async def decide_reader_review(
        change_set_id: str,
        request: ReaderReviewDecisionRequest,
    ) -> ReaderReviewDecisionResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.decide_reader_review(change_set_id, request),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/feedback-candidates",
        response_model=FeedbackCandidateResponse,
    )
    async def get_feedback_candidates(
        change_set_id: str,
    ) -> FeedbackCandidateResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_feedback_candidates,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)


    @router.get(
        "/api/v1/change-sets/{change_set_id}/feedback-candidate-promotion",
        response_model=FeedbackCandidatePromotionResponse,
    )
    async def get_feedback_candidate_promotion(
        change_set_id: str,
    ) -> FeedbackCandidatePromotionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_feedback_candidate_promotion,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/feedback-candidate-promotion",
        response_model=FeedbackCandidatePromotionResponse,
    )
    async def promote_feedback_candidates(
        change_set_id: str,
        request: FeedbackCandidatePromotionRequest,
    ) -> FeedbackCandidatePromotionResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.promote_feedback_candidates(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
        response_model=FormalKnowledgePromotionResponse,
    )
    async def get_formal_knowledge_promotion(
        change_set_id: str,
    ) -> FormalKnowledgePromotionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_formal_knowledge_promotion,
                change_set_id,
            )
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion/evaluate",
        response_model=FormalKnowledgePromotionResponse,
    )
    async def evaluate_formal_knowledge_promotion(
        change_set_id: str,
        request: FormalKnowledgePromotionEvaluateRequest | None = None,
    ) -> FormalKnowledgePromotionResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_formal_knowledge_promotion(
                change_set_id,
                request or FormalKnowledgePromotionEvaluateRequest(),
            ),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
        response_model=FormalKnowledgePromotionResponse,
    )
    async def act_formal_knowledge_promotion(
        change_set_id: str,
        request: FormalKnowledgePromotionActionRequest,
    ) -> FormalKnowledgePromotionResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.act_formal_knowledge_promotion(change_set_id, request),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-export",
        response_model=EvalCorpusExportResponse,
    )
    async def get_eval_corpus_export(
        change_set_id: str,
    ) -> EvalCorpusExportResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_eval_corpus_export, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-export",
        response_model=EvalCorpusExportResponse,
    )
    async def export_eval_corpus(
        change_set_id: str,
        request: EvalCorpusExportRequest | None = None,
    ) -> EvalCorpusExportResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.export_eval_corpus(
                change_set_id,
                request or EvalCorpusExportRequest(),
            ),
        )


    @router.get(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-baseline",
        response_model=EvalCorpusBaselineResponse,
    )
    async def get_eval_corpus_baseline(
        change_set_id: str,
    ) -> EvalCorpusBaselineResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_eval_corpus_baseline, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-baseline",
        response_model=EvalCorpusBaselineResponse,
    )
    async def freeze_eval_corpus_baseline(
        change_set_id: str,
        request: EvalCorpusBaselineFreezeRequest | None = None,
    ) -> EvalCorpusBaselineResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.freeze_eval_corpus_baseline(
                change_set_id,
                request or EvalCorpusBaselineFreezeRequest(),
            ),
        )

    @router.get(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-regression",
        response_model=EvalCorpusRegressionResponse,
    )
    async def get_eval_corpus_regression(
        change_set_id: str,
    ) -> EvalCorpusRegressionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_eval_corpus_regression, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.post(
        "/api/v1/change-sets/{change_set_id}/eval-corpus-regression",
        response_model=EvalCorpusRegressionResponse,
    )
    async def evaluate_eval_corpus_regression(
        change_set_id: str,
        request: EvalCorpusRegressionEvaluateRequest | None = None,
    ) -> EvalCorpusRegressionResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.evaluate_eval_corpus_regression(
                change_set_id,
                request or EvalCorpusRegressionEvaluateRequest(),
            ),
        )

    return router
