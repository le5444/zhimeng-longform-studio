from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Query, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCandidateDecisionRequest,
    KnowledgeCandidateList,
    KnowledgeCandidateUpdateRequest,
    KnowledgeCard,
    KnowledgeCardList,
    KnowledgeScopedCardList,
    KnowledgeCardStateRequest,
    KnowledgeCardStatus,
    KnowledgeFormalizationReceipt,
    KnowledgeFormalizationReceiptV2,
    KnowledgeFormalizationRecord,
    KnowledgeFormalizationRequest,
    KnowledgePromotionRequest,
    KnowledgeRollbackRequest,
)
from zhimeng_runtime.knowledge_service import (
    KnowledgeConflictError,
    KnowledgeInvalidSourceError,
    KnowledgeNotFoundError,
    KnowledgeReviewRequiredError,
    KnowledgeService,
    KnowledgeServiceError,
)
from zhimeng_runtime.knowledge_scope_models import KnowledgeScopeRecord
from zhimeng_runtime.project_kb_models import (
    ProjectKbRebuildRequest,
    ProjectKbRebuildResult,
    ProjectKbStateRecord,
)
from zhimeng_runtime.project_kb_service import (
    ProjectKbConflictError,
    ProjectKbNotFoundError,
    ProjectKbService,
    ProjectKbServiceError,
    ProjectKbTamperedError,
)


def create_knowledge_router(
    service: KnowledgeService,
    project_kb_service: ProjectKbService | None = None,
    *,
    project_kb_service_error: str | None = None,
) -> APIRouter:
    # Keep the original application-wide API mounted unchanged.  Project
    # routes live in a sibling router so a project id can never be silently
    # dropped while calling the legacy handlers.
    router = APIRouter(tags=["knowledge"])
    legacy_router = APIRouter(prefix="/api/v1/knowledge")
    project_router = APIRouter(prefix="/api/v1/projects/{project_id}/knowledge")

    def raise_knowledge_error(exc: KnowledgeServiceError) -> NoReturn:
        if isinstance(exc, KnowledgeNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "knowledge_not_found"
        elif isinstance(exc, KnowledgeReviewRequiredError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "knowledge_review_required"
        elif isinstance(exc, KnowledgeInvalidSourceError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "knowledge_source_invalid"
        elif isinstance(exc, KnowledgeConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "knowledge_conflict"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "knowledge_failed"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    def require_project_kb_service() -> ProjectKbService:
        if project_kb_service is None:
            raise ApiError(
                project_kb_service_error or "project KB service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="project_kb_unavailable",
                retryable=True,
            )
        return project_kb_service

    def raise_project_kb_error(exc: ProjectKbServiceError) -> NoReturn:
        if isinstance(exc, ProjectKbNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "project_kb_not_found"
        elif isinstance(exc, ProjectKbTamperedError):
            status_code = status.HTTP_409_CONFLICT
            code = "project_kb_tampered"
        elif isinstance(exc, ProjectKbConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "project_kb_conflict"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "project_kb_failed"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    @legacy_router.post(
        "/source-imports/{source_import_id}/candidates",
        response_model=KnowledgeCandidateList,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_source_candidates(source_import_id: str) -> KnowledgeCandidateList:
        try:
            return await run_in_threadpool(service.import_source_candidates, source_import_id)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.get("/candidates", response_model=KnowledgeCandidateList)
    async def list_candidates(
        source_import_id: str | None = Query(default=None),
    ) -> KnowledgeCandidateList:
        try:
            return await run_in_threadpool(service.list_candidates, source_import_id)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.get(
        "/source-imports/{source_import_id}/formalization",
        response_model=KnowledgeFormalizationRecord,
    )
    async def get_source_formalization(
        source_import_id: str,
    ) -> KnowledgeFormalizationRecord:
        try:
            return await run_in_threadpool(service.get_formalization, source_import_id)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post(
        "/source-imports/{source_import_id}/finalize",
        response_model=KnowledgeFormalizationReceipt,
    )
    async def finalize_source_import(
        source_import_id: str,
        request: KnowledgeFormalizationRequest,
    ) -> KnowledgeFormalizationReceipt:
        try:
            return await run_in_threadpool(
                service.finalize_source_import,
                source_import_id,
                request,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.put("/candidates/{candidate_id}", response_model=KnowledgeCandidate)
    async def update_candidate(
        candidate_id: str,
        request: KnowledgeCandidateUpdateRequest,
    ) -> KnowledgeCandidate:
        try:
            return await run_in_threadpool(service.update_candidate, candidate_id, request)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post("/candidates/{candidate_id}/promote", response_model=KnowledgeCard)
    async def promote_candidate(
        candidate_id: str,
        request: KnowledgePromotionRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(service.promote_candidate, candidate_id, request)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post("/candidates/{candidate_id}/reject", response_model=KnowledgeCandidate)
    async def reject_candidate(
        candidate_id: str,
        request: KnowledgeCandidateDecisionRequest,
    ) -> KnowledgeCandidate:
        try:
            return await run_in_threadpool(service.reject_candidate, candidate_id, request)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.get("/cards", response_model=KnowledgeCardList)
    async def list_cards() -> KnowledgeCardList:
        try:
            return await run_in_threadpool(service.list_cards)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post("/cards/{card_id}/disable", response_model=KnowledgeCard)
    async def disable_card(
        card_id: str,
        request: KnowledgeCardStateRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.set_card_status,
                card_id,
                request,
                status=KnowledgeCardStatus.DISABLED,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post("/cards/{card_id}/enable", response_model=KnowledgeCard)
    async def enable_card(
        card_id: str,
        request: KnowledgeCardStateRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.set_card_status,
                card_id,
                request,
                status=KnowledgeCardStatus.ACTIVE,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @legacy_router.post("/cards/{card_id}/rollback", response_model=KnowledgeCard)
    async def rollback_card(
        card_id: str,
        request: KnowledgeRollbackRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(service.rollback_card, card_id, request)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.get("/scope", response_model=KnowledgeScopeRecord)
    async def get_project_knowledge_scope(project_id: str) -> KnowledgeScopeRecord:
        try:
            return await run_in_threadpool(service.get_scope, project_id=project_id)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.get("/kb-state", response_model=ProjectKbStateRecord)
    async def get_project_kb_state(project_id: str) -> ProjectKbStateRecord:
        kb_service = require_project_kb_service()
        try:
            return await run_in_threadpool(kb_service.get_state, project_id)
        except ProjectKbServiceError as exc:
            raise_project_kb_error(exc)

    @project_router.post(
        "/kb-state/rebuild",
        response_model=ProjectKbRebuildResult,
    )
    async def rebuild_project_kb_state(
        project_id: str,
        request: ProjectKbRebuildRequest,
    ) -> ProjectKbRebuildResult:
        kb_service = require_project_kb_service()
        try:
            return await run_in_threadpool(kb_service.rebuild, project_id, request)
        except ProjectKbServiceError as exc:
            raise_project_kb_error(exc)

    @project_router.post(
        "/source-imports/{source_import_id}/candidates",
        response_model=KnowledgeCandidateList,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_project_source_candidates(
        project_id: str,
        source_import_id: str,
    ) -> KnowledgeCandidateList:
        try:
            return await run_in_threadpool(
                service.import_source_candidates,
                source_import_id,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.get("/candidates", response_model=KnowledgeCandidateList)
    async def list_project_candidates(
        project_id: str,
        source_import_id: str | None = Query(default=None),
    ) -> KnowledgeCandidateList:
        try:
            return await run_in_threadpool(
                service.list_candidates,
                source_import_id,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.get(
        "/source-imports/{source_import_id}/formalization",
        response_model=KnowledgeFormalizationRecord,
    )
    async def get_project_source_formalization(
        project_id: str,
        source_import_id: str,
    ) -> KnowledgeFormalizationRecord:
        try:
            return await run_in_threadpool(
                service.get_formalization,
                source_import_id,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    async def finalize_project_source_import(
        project_id: str,
        source_import_id: str,
        request: KnowledgeFormalizationRequest,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        try:
            return await run_in_threadpool(
                service.finalize_source_import,
                source_import_id,
                request,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post(
        "/source-imports/{source_import_id}/formalization/finalize",
        response_model=KnowledgeFormalizationReceiptV2 | KnowledgeFormalizationReceipt,
    )
    async def finalize_project_source_import_formalization(
        project_id: str,
        source_import_id: str,
        request: KnowledgeFormalizationRequest,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        return await finalize_project_source_import(project_id, source_import_id, request)

    # Transitional alias matching the legacy suffix.  Both project paths are
    # scoped identically; the nested form is the canonical new contract.
    @project_router.post(
        "/source-imports/{source_import_id}/finalize",
        response_model=KnowledgeFormalizationReceiptV2 | KnowledgeFormalizationReceipt,
        include_in_schema=False,
    )
    async def finalize_project_source_import_legacy_suffix(
        project_id: str,
        source_import_id: str,
        request: KnowledgeFormalizationRequest,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        return await finalize_project_source_import(project_id, source_import_id, request)

    @project_router.put("/candidates/{candidate_id}", response_model=KnowledgeCandidate)
    async def update_project_candidate(
        project_id: str,
        candidate_id: str,
        request: KnowledgeCandidateUpdateRequest,
    ) -> KnowledgeCandidate:
        try:
            return await run_in_threadpool(
                service.update_candidate,
                candidate_id,
                request,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post("/candidates/{candidate_id}/promote", response_model=KnowledgeCard)
    async def promote_project_candidate(
        project_id: str,
        candidate_id: str,
        request: KnowledgePromotionRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.promote_candidate,
                candidate_id,
                request,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post("/candidates/{candidate_id}/reject", response_model=KnowledgeCandidate)
    async def reject_project_candidate(
        project_id: str,
        candidate_id: str,
        request: KnowledgeCandidateDecisionRequest,
    ) -> KnowledgeCandidate:
        try:
            return await run_in_threadpool(
                service.reject_candidate,
                candidate_id,
                request,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.get("/cards", response_model=KnowledgeScopedCardList)
    async def list_project_cards(project_id: str) -> KnowledgeScopedCardList:
        try:
            return await run_in_threadpool(service.list_cards, project_id=project_id)
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post("/cards/{card_id}/disable", response_model=KnowledgeCard)
    async def disable_project_card(
        project_id: str,
        card_id: str,
        request: KnowledgeCardStateRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.set_card_status,
                card_id,
                request,
                status=KnowledgeCardStatus.DISABLED,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post("/cards/{card_id}/enable", response_model=KnowledgeCard)
    async def enable_project_card(
        project_id: str,
        card_id: str,
        request: KnowledgeCardStateRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.set_card_status,
                card_id,
                request,
                status=KnowledgeCardStatus.ACTIVE,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    @project_router.post("/cards/{card_id}/rollback", response_model=KnowledgeCard)
    async def rollback_project_card(
        project_id: str,
        card_id: str,
        request: KnowledgeRollbackRequest,
    ) -> KnowledgeCard:
        try:
            return await run_in_threadpool(
                service.rollback_card,
                card_id,
                request,
                project_id=project_id,
            )
        except KnowledgeServiceError as exc:
            raise_knowledge_error(exc)

    router.include_router(legacy_router)
    router.include_router(project_router)
    return router


__all__ = ["create_knowledge_router"]
