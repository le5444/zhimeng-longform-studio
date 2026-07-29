from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.skill_rule_evolution_models import (
    SkillRuleEvolutionActionRequest,
    SkillRuleEvolutionPrepareRequest,
    SkillRuleEvolutionResponse,
)
from zhimeng_runtime.skill_rule_evolution_service import (
    SkillRuleEvolutionBlockedError,
    SkillRuleEvolutionConflictError,
    SkillRuleEvolutionError,
    SkillRuleEvolutionInvalidError,
    SkillRuleEvolutionNotFoundError,
    SkillRuleEvolutionService,
)


def create_skill_rule_evolution_router(
    service: SkillRuleEvolutionService | None,
) -> APIRouter:
    router = APIRouter(tags=["skill-rule-evolution"])

    def require_service() -> SkillRuleEvolutionService:
        if service is None:
            raise ApiError(
                "Skill rule evolution service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="skill_rule_evolution_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: SkillRuleEvolutionError) -> None:
        if isinstance(exc, SkillRuleEvolutionNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, SkillRuleEvolutionConflictError | SkillRuleEvolutionBlockedError):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, SkillRuleEvolutionInvalidError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc

    @router.post(
        "/api/v1/skill-rule-evolutions/prepare",
        response_model=SkillRuleEvolutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def prepare_skill_rule_evolution(
        request: SkillRuleEvolutionPrepareRequest,
    ) -> SkillRuleEvolutionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.prepare, request)
        except SkillRuleEvolutionError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/skill-rule-evolutions/{evolution_id}",
        response_model=SkillRuleEvolutionResponse,
    )
    async def get_skill_rule_evolution(evolution_id: str) -> SkillRuleEvolutionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, evolution_id)
        except SkillRuleEvolutionError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/skill-rule-evolutions/{evolution_id}/actions",
        response_model=SkillRuleEvolutionResponse,
    )
    async def act_skill_rule_evolution(
        evolution_id: str,
        request: SkillRuleEvolutionActionRequest,
    ) -> SkillRuleEvolutionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.act, evolution_id, request)
        except SkillRuleEvolutionError as exc:
            raise_error(exc)

    return router


__all__ = ["create_skill_rule_evolution_router"]
