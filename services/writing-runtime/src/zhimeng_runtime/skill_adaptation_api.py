from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.skill_adaptation_models import (
    SkillAdaptationMaterializeRequest,
    SkillAdaptationMaterializationResponse,
    SkillAdaptationPlanRequest,
    SkillAdaptationPlanResponse,
)
from zhimeng_runtime.skill_adaptation_service import (
    SkillAdaptationError,
    SkillAdaptationService,
    raise_skill_adaptation_error,
)


def create_skill_adaptation_router(service: SkillAdaptationService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/managed-skills/adaptations",
        tags=["managed-skill-adaptations"],
    )

    @router.post("/plan", response_model=SkillAdaptationPlanResponse)
    async def plan_adaptation(
        request: SkillAdaptationPlanRequest,
    ) -> SkillAdaptationPlanResponse:
        try:
            return await run_in_threadpool(service.plan, request)
        except SkillAdaptationError as exc:
            raise_skill_adaptation_error(exc)

    @router.get("/{plan_id}", response_model=SkillAdaptationPlanResponse)
    async def get_adaptation_plan(plan_id: str) -> SkillAdaptationPlanResponse:
        try:
            return await run_in_threadpool(service.get_plan, plan_id)
        except SkillAdaptationError as exc:
            raise_skill_adaptation_error(exc)

    @router.post("/materialize", response_model=SkillAdaptationMaterializationResponse)
    async def materialize_adaptation(
        request: SkillAdaptationMaterializeRequest,
    ) -> SkillAdaptationMaterializationResponse:
        try:
            return await run_in_threadpool(service.materialize, request)
        except SkillAdaptationError as exc:
            raise_skill_adaptation_error(exc)

    return router


__all__ = ["create_skill_adaptation_router"]
