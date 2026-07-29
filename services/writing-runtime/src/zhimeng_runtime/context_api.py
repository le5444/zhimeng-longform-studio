from __future__ import annotations

from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Header, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.context_models import ContextCreateRequest, ContextPack
from zhimeng_runtime.context_service import (
    ContextConflictError,
    ContextNotFoundError,
    ContextService,
    ContextServiceError,
    ContextUnavailableError,
)
from zhimeng_runtime.context_store import StoredContextPack
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.nlcl_models import NlclRouteReceipt
from zhimeng_runtime.nlcl_route_execution_models import NlclRouteExecutionState
from zhimeng_runtime.prewrite_models import PrewriteOperationPackage


class ContextApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContextPackResponse(ContextApiModel):
    schema_id: Literal["zhimeng.context-pack-record.v1"] = Field(
        default="zhimeng.context-pack-record.v1",
        alias="schema",
    )
    pack: ContextPack
    consumed_by_run_id: str | None = None


class ContextPackListResponse(ContextApiModel):
    schema_id: Literal["zhimeng.context-pack-list.v1"] = Field(
        default="zhimeng.context-pack-list.v1",
        alias="schema",
    )
    session_id: str
    context_packs: list[ContextPackResponse]


def _response(stored: StoredContextPack) -> ContextPackResponse:
    return ContextPackResponse(
        pack=stored.pack,
        consumed_by_run_id=stored.consumed_by_run_id,
    )


def create_context_router(
    service: ContextService | None,
    *,
    service_error: str | None = None,
) -> APIRouter:
    router = APIRouter(tags=["context-packs"])

    def require_service() -> ContextService:
        if service is None:
            raise ApiError(
                service_error or "Context service is not configured",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="context_unavailable",
                retryable=True,
            )
        return service

    def raise_context_error(exc: ContextServiceError) -> NoReturn:
        if isinstance(exc, ContextNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "context_not_found"
        elif isinstance(exc, ContextConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "context_conflict"
        elif isinstance(exc, ContextUnavailableError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = "context_unavailable"
        else:  # pragma: no cover - future ContextServiceError subclasses.
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = "context_unavailable"
        if exc.code.startswith("project_kb_"):
            code = exc.code
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=exc.retryable,
        ) from exc

    @router.post(
        "/api/v1/agent/sessions/{session_id}/context-packs",
        response_model=ContextPackResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_context_pack(
        session_id: str,
        request: ContextCreateRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
            ),
        ],
    ) -> ContextPackResponse:
        active = require_service()
        try:
            stored = await active.create_pack(
                session_id,
                request,
                idempotency_key=idempotency_key,
            )
        except ContextServiceError as exc:
            raise_context_error(exc)
        return _response(stored)

    @router.get(
        "/api/v1/agent/context-packs/{context_pack_id}",
        response_model=ContextPackResponse,
    )
    async def get_context_pack(context_pack_id: str) -> ContextPackResponse:
        active = require_service()
        try:
            stored = await run_in_threadpool(active.get_pack, context_pack_id)
        except ContextServiceError as exc:
            raise_context_error(exc)
        return _response(stored)

    @router.get(
        "/api/v1/agent/context-packs/{context_pack_id}/nlcl-route",
        response_model=NlclRouteReceipt,
    )
    async def get_context_pack_nlcl_route(
        context_pack_id: str,
    ) -> NlclRouteReceipt:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_nlcl_route, context_pack_id)
        except ContextServiceError as exc:
            raise_context_error(exc)

    @router.get(
        "/api/v1/agent/context-packs/{context_pack_id}/prewrite-operation-package",
        response_model=PrewriteOperationPackage,
    )
    async def get_context_pack_prewrite_operation_package(
        context_pack_id: str,
    ) -> PrewriteOperationPackage:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_prewrite_operation_package,
                context_pack_id,
            )
        except ContextServiceError as exc:
            raise_context_error(exc)

    @router.get(
        "/api/v1/agent/context-packs/{context_pack_id}/nlcl-route/executions/{call_ordinal}",
        response_model=NlclRouteExecutionState,
    )
    async def get_context_pack_nlcl_route_execution(
        context_pack_id: str,
        call_ordinal: Annotated[int, Path(ge=1, le=64)],
    ) -> NlclRouteExecutionState:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_route_execution_state,
                context_pack_id,
                call_ordinal=call_ordinal,
            )
        except ContextServiceError as exc:
            raise_context_error(exc)

    @router.get(
        "/api/v1/agent/sessions/{session_id}/context-packs",
        response_model=ContextPackListResponse,
    )
    async def list_session_context_packs(
        session_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> ContextPackListResponse:
        active = require_service()
        try:
            stored = await run_in_threadpool(
                active.list_session_packs,
                session_id,
                limit=limit,
            )
        except ContextServiceError as exc:
            raise_context_error(exc)
        return ContextPackListResponse(
            session_id=session_id,
            context_packs=[_response(item) for item in stored],
        )

    @router.get(
        "/api/v1/agent/runs/{run_id}/context-pack",
        response_model=ContextPackResponse,
    )
    async def get_run_context_pack(run_id: str) -> ContextPackResponse:
        active = require_service()
        try:
            stored = await run_in_threadpool(active.get_run_pack, run_id)
        except ContextServiceError as exc:
            raise_context_error(exc)
        return _response(stored)

    return router


__all__ = [
    "ContextPackListResponse",
    "ContextPackResponse",
    "create_context_router",
]
