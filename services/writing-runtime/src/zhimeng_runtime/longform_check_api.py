from __future__ import annotations

from fastapi import APIRouter, Query, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.longform_check_service import (
    LongformCheckError,
    LongformCheckNotFound,
    check_project_for_author,
)


def create_longform_check_router(*, project_root_resolver=None) -> APIRouter:
    router = APIRouter(tags=["longform-check"])

    def raise_error(exc: LongformCheckError) -> None:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if isinstance(exc, LongformCheckNotFound)
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=False,
        ) from exc

    @router.get("/api/v1/projects/{project_id}/longform-check")
    async def get_longform_check(
        project_id: str,
        chapter_id: str | None = Query(default=None),
    ) -> dict:
        if project_root_resolver is None:
            raise ApiError(
                "Project root resolver is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="longform_check_unavailable",
                retryable=False,
            )
        try:
            project_root = await run_in_threadpool(project_root_resolver, project_id)
            result = await run_in_threadpool(
                check_project_for_author,
                project_root,
                chapter_id=chapter_id,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_404_NOT_FOUND,
                code="longform_check_not_found",
                retryable=False,
            ) from exc
        except LongformCheckError as exc:
            raise_error(exc)
        result["project_id"] = project_id
        return result

    return router
