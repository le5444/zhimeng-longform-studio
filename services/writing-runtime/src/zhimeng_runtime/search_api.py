from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.search_models import (
    SearchQueryRequest,
    SearchQueryResponse,
    SearchRebuildRequest,
    SearchRebuildResult,
)
from zhimeng_runtime.search_service import (
    SearchConflictError,
    SearchInvalidError,
    SearchNotFoundError,
    SearchService,
    SearchServiceError,
    SearchUnavailableError,
)


def create_search_router(
    service: SearchService | None,
    *,
    service_error: str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/search", tags=["search"])

    def require_service() -> SearchService:
        if service is None:
            raise ApiError(
                service_error or "project search service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="search_unavailable",
                retryable=True,
            )
        return service

    def raise_search_error(exc: SearchServiceError) -> NoReturn:
        if isinstance(exc, SearchNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "search_not_found"
        elif isinstance(exc, SearchInvalidError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "search_invalid"
        elif isinstance(exc, SearchConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "search_conflict"
        elif isinstance(exc, SearchUnavailableError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = "search_unavailable"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "search_failed"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    @router.post("/rebuild", response_model=SearchRebuildResult)
    async def rebuild_search_index(
        project_id: str,
        request: SearchRebuildRequest | None = None,
    ) -> SearchRebuildResult:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.rebuild,
                project_id,
                request or SearchRebuildRequest(),
            )
        except SearchServiceError as exc:
            raise_search_error(exc)

    @router.post("", response_model=SearchQueryResponse)
    async def search_project(
        project_id: str,
        request: SearchQueryRequest,
    ) -> SearchQueryResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.search, project_id, request)
        except SearchServiceError as exc:
            raise_search_error(exc)

    return router


__all__ = ["create_search_router"]
