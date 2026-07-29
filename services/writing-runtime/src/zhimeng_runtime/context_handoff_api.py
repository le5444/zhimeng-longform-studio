from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.context_handoff_models import (
    ContextHandoffDecisionRequest,
    ContextHandoffPrepareRequest,
    ContextHandoffResponse,
)
from zhimeng_runtime.context_handoff_service import (
    ContextHandoffError,
    ContextHandoffService,
    raise_context_handoff_error,
)


def create_context_handoff_router(service: ContextHandoffService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/context-handoffs/novel-kb-assemble",
        tags=["context-handoffs"],
    )

    @router.get("", response_model=ContextHandoffResponse)
    async def get_handoff(project_id: str) -> ContextHandoffResponse:
        try:
            return await run_in_threadpool(service.get, project_id)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    @router.post("/prepare", response_model=ContextHandoffResponse)
    async def prepare_handoff(
        project_id: str,
        request: ContextHandoffPrepareRequest,
    ) -> ContextHandoffResponse:
        try:
            return await run_in_threadpool(service.prepare, project_id, request)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    @router.post("/approve", response_model=ContextHandoffResponse)
    async def approve_handoff(
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        try:
            return await run_in_threadpool(service.approve, project_id, request)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    @router.post("/reject", response_model=ContextHandoffResponse)
    async def reject_handoff(
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        try:
            return await run_in_threadpool(service.reject, project_id, request)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    @router.post("/disable", response_model=ContextHandoffResponse)
    async def disable_handoff(
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        try:
            return await run_in_threadpool(service.disable, project_id, request)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    @router.post("/run", response_model=ContextHandoffResponse)
    async def run_handoff(
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        try:
            return await service.run(project_id, request)
        except ContextHandoffError as exc:
            raise_context_handoff_error(exc)

    return router


__all__ = ["create_context_handoff_router"]
