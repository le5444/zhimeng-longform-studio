from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.skill_models import (
    SkillRegistryResponse,
)
from zhimeng_runtime.skill_registry import SkillRegistry


def create_skill_router(
    registry: SkillRegistry | None,
    *,
    registry_error: str | None = None,
) -> APIRouter:
    router = APIRouter(tags=["skills"])

    @router.get("/api/v1/skills", response_model=SkillRegistryResponse)
    async def list_skills() -> SkillRegistryResponse:
        if registry_error is not None:
            raise ApiError(
                "Skill registry is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="skill_registry_unavailable",
                details={"startup_error": registry_error},
                retryable=False,
            )
        if registry is None:
            return SkillRegistry.unconfigured_response()
        return await run_in_threadpool(registry.scan)

    return router


__all__ = ["create_skill_router"]
