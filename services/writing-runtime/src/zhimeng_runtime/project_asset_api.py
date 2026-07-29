from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.project_asset_service import (
    ProjectAssetError,
    ProjectAssetNotFound,
    ProjectAssetService,
)


def create_project_asset_router(
    service: ProjectAssetService | None,
    *,
    project_root_resolver=None,
) -> APIRouter:
    router = APIRouter(tags=["project-assets"])

    def require_service() -> ProjectAssetService:
        if service is None:
            raise ApiError(
                "Project asset service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="project_asset_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: ProjectAssetError) -> None:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if isinstance(exc, ProjectAssetNotFound)
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=False,
        ) from exc

    @router.post("/api/v1/projects/{project_id}/assets/rebuild")
    async def rebuild_assets(project_id: str) -> dict:
        active = require_service()
        if project_root_resolver is None:
            raise ApiError(
                "Project root resolver is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="project_asset_unavailable",
                retryable=False,
            )
        try:
            project_root = await run_in_threadpool(project_root_resolver, project_id)
            return await run_in_threadpool(
                active.rebuild,
                project_id=project_id,
                project_root=project_root,
            )
        except ProjectAssetError as exc:
            raise_error(exc)
        except FileNotFoundError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_404_NOT_FOUND,
                code="project_asset_not_found",
                retryable=False,
            ) from exc

    @router.get("/api/v1/projects/{project_id}/assets")
    async def get_assets(project_id: str) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, project_id)
        except ProjectAssetError as exc:
            raise_error(exc)

    return router


__all__ = ["create_project_asset_router"]
