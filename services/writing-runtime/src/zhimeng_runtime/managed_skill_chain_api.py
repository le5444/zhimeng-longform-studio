from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.managed_skill_chain_models import (
    ManagedSkillChainDecisionRequest,
    ManagedSkillChainPrepareRequest,
    ManagedSkillChainResponse,
)
from zhimeng_runtime.managed_skill_chain_service import (
    ManagedSkillChainError,
    ManagedSkillChainService,
    raise_managed_skill_chain_error,
)


def create_managed_skill_chain_router(service: ManagedSkillChainService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/managed-skill-chains",
        tags=["managed-skill-chains"],
    )

    @router.post("/prepare", response_model=ManagedSkillChainResponse)
    async def prepare_chain(
        project_id: str,
        request: ManagedSkillChainPrepareRequest,
    ) -> ManagedSkillChainResponse:
        try:
            return await run_in_threadpool(service.prepare, project_id, request)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    @router.get("/{chain_id}", response_model=ManagedSkillChainResponse)
    async def get_chain(project_id: str, chain_id: str) -> ManagedSkillChainResponse:
        try:
            return await run_in_threadpool(service.get, project_id, chain_id)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    @router.post("/{chain_id}/approve", response_model=ManagedSkillChainResponse)
    async def approve_chain(
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        try:
            return await run_in_threadpool(service.approve, project_id, request)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    @router.post("/{chain_id}/reject", response_model=ManagedSkillChainResponse)
    async def reject_chain(
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        try:
            return await run_in_threadpool(service.reject, project_id, request)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    @router.post("/{chain_id}/disable", response_model=ManagedSkillChainResponse)
    async def disable_chain(
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        try:
            return await run_in_threadpool(service.disable, project_id, request)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    @router.post("/{chain_id}/run", response_model=ManagedSkillChainResponse)
    async def run_chain(
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        try:
            return await service.run(project_id, request)
        except ManagedSkillChainError as exc:
            raise_managed_skill_chain_error(exc)

    return router


__all__ = ["create_managed_skill_chain_router"]
