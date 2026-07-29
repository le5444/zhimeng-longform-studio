from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.skill_handoff_models import (
    SkillHandoffDecisionRequest,
    SkillHandoffPrepareRequest,
    SkillHandoffResponse,
)
from zhimeng_runtime.skill_handoff_service import (
    SkillHandoffError,
    SkillHandoffService,
    raise_skill_handoff_error,
)


def create_skill_handoff_router(service: SkillHandoffService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/skill-handoffs/novel-distillation-kb",
        tags=["skill-handoffs"],
    )

    @router.get("", response_model=SkillHandoffResponse)
    async def get_handoff(project_id: str) -> SkillHandoffResponse:
        try:
            return await run_in_threadpool(service.get, project_id)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    @router.post("/prepare", response_model=SkillHandoffResponse)
    async def prepare_handoff(project_id: str, request: SkillHandoffPrepareRequest) -> SkillHandoffResponse:
        try:
            return await run_in_threadpool(service.prepare, project_id, request)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    @router.post("/approve", response_model=SkillHandoffResponse)
    async def approve_handoff(project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        try:
            return await run_in_threadpool(service.approve, project_id, request)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    @router.post("/reject", response_model=SkillHandoffResponse)
    async def reject_handoff(project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        try:
            return await run_in_threadpool(service.reject, project_id, request)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    @router.post("/disable", response_model=SkillHandoffResponse)
    async def disable_handoff(project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        try:
            return await run_in_threadpool(service.disable, project_id, request)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    @router.post("/run", response_model=SkillHandoffResponse)
    async def run_handoff(project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        try:
            return await service.run(project_id, request)
        except SkillHandoffError as exc:
            raise_skill_handoff_error(exc)

    return router


__all__ = ["create_skill_handoff_router"]
