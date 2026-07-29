from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.adjacent_chain_models import (
    AdjacentChainDecisionRequest,
    AdjacentChainPrepareRequest,
    AdjacentChainResponse,
)
from zhimeng_runtime.adjacent_chain_service import (
    AdjacentChainError,
    AdjacentChainService,
    raise_adjacent_chain_error,
)


def create_adjacent_chain_router(service: AdjacentChainService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/adjacent-skill-chains/novel-distillation-kb-to-context",
        tags=["adjacent-skill-chains"],
    )

    @router.get("", response_model=AdjacentChainResponse)
    async def get_chain(project_id: str) -> AdjacentChainResponse:
        try:
            return await run_in_threadpool(service.get, project_id)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    @router.post("/prepare", response_model=AdjacentChainResponse)
    async def prepare_chain(
        project_id: str,
        request: AdjacentChainPrepareRequest,
    ) -> AdjacentChainResponse:
        try:
            return await run_in_threadpool(service.prepare, project_id, request)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    @router.post("/approve", response_model=AdjacentChainResponse)
    async def approve_chain(
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        try:
            return await run_in_threadpool(service.approve, project_id, request)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    @router.post("/reject", response_model=AdjacentChainResponse)
    async def reject_chain(
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        try:
            return await run_in_threadpool(service.reject, project_id, request)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    @router.post("/disable", response_model=AdjacentChainResponse)
    async def disable_chain(
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        try:
            return await run_in_threadpool(service.disable, project_id, request)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    @router.post("/run", response_model=AdjacentChainResponse)
    async def run_chain(
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        try:
            return await service.run(project_id, request)
        except AdjacentChainError as exc:
            raise_adjacent_chain_error(exc)

    return router


__all__ = ["create_adjacent_chain_router"]
