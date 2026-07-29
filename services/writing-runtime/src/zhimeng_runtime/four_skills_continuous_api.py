from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.four_skills_continuous_models import (
    FourSkillsContinuousEvaluateRequest,
    FourSkillsContinuousResponse,
)
from zhimeng_runtime.four_skills_continuous_service import (
    FourSkillsContinuousConflict,
    FourSkillsContinuousError,
    FourSkillsContinuousInvalid,
    FourSkillsContinuousNotFound,
    FourSkillsContinuousService,
)


def create_four_skills_continuous_router(
    service: FourSkillsContinuousService | None,
) -> APIRouter:
    router = APIRouter(tags=["four-skills-continuous"])

    def require_service() -> FourSkillsContinuousService:
        if service is None:
            raise ApiError(
                "Four skills continuous service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="four_skills_continuous_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: FourSkillsContinuousError) -> None:
        if isinstance(exc, FourSkillsContinuousNotFound):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, FourSkillsContinuousConflict):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, FourSkillsContinuousInvalid):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise ApiError(
            str(exc),
            status_code=status_code,
            code="four_skills_continuous_failed",
            details=exc.details or None,
            retryable=exc.retryable,
        ) from exc

    @router.post(
        "/api/v1/projects/{project_id}/four-skills-continuous/evaluate",
        response_model=FourSkillsContinuousResponse,
    )
    async def evaluate_four_skills_continuous(
        project_id: str,
        request: FourSkillsContinuousEvaluateRequest | None = None,
    ) -> FourSkillsContinuousResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.evaluate,
                project_id,
                request or FourSkillsContinuousEvaluateRequest(),
            )
        except FourSkillsContinuousError as exc:
            raise_error(exc)

    return router


__all__ = ["create_four_skills_continuous_router"]
