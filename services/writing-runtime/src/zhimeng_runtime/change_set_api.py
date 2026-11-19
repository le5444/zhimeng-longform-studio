from __future__ import annotations

from pathlib import Path
from typing import Callable, NoReturn, TypeVar

from fastapi import APIRouter, HTTPException, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.change_set_models import (
    ChangeSetApplyRequest,
    ChangeSetDiscardRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetHunkEditRequest,
    ChangeSetListResponse,
    ChangeSetOrigin,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
)
from zhimeng_runtime.change_set_service import (
    ChangeSetConflictError,
    ChangeSetError,
    ChangeSetGateBlockedError,
    ChangeSetGatePipelineBlockedError,
    ChangeSetGateError,
    ChangeSetGateMissingError,
    ChangeSetGateStaleError,
    ChangeSetGateUnavailableError,
    ChangeSetInputError,
    ChangeSetNotFoundError,
    ChangeSetReviewRequiredError,
    ChangeSetService,
    ChangeSetStateError,
    ChangeSetWritebackError,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.project_locks import ProjectLockRegistry


T = TypeVar("T")


def create_change_set_router(
    service: ChangeSetService | None,
    store: SqliteStore,
    locks: ProjectLockRegistry,
    *,
    service_error: str | None = None,
) -> APIRouter:
    router = APIRouter(tags=["change-sets"])

    def require_service() -> ChangeSetService:
        if service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service_error or "No trusted project roots are configured",
            )
        return service

    def raise_change_set_error(exc: ChangeSetError) -> NoReturn:
        if isinstance(exc, ChangeSetNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "change_set_not_found"
            retryable = False
        elif isinstance(exc, ChangeSetConflictError):
            status_code = status.HTTP_409_CONFLICT
            code = "change_set_conflict"
            retryable = True
        elif isinstance(exc, ChangeSetStateError):
            status_code = status.HTTP_409_CONFLICT
            code = "change_set_state_invalid"
            retryable = False
        elif isinstance(exc, ChangeSetReviewRequiredError):
            status_code = status.HTTP_409_CONFLICT
            code = "change_set_review_required"
            retryable = False
        elif isinstance(exc, ChangeSetInputError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "change_set_input_invalid"
            retryable = False
        elif isinstance(exc, ChangeSetWritebackError):
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "change_set_writeback_failed"
            retryable = True
        elif isinstance(exc, ChangeSetGateUnavailableError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = exc.code
            retryable = True
        elif isinstance(
            exc,
            (
                ChangeSetGateBlockedError,
                ChangeSetGatePipelineBlockedError,
                ChangeSetGateMissingError,
                ChangeSetGateStaleError,
            ),
        ):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = False
        elif isinstance(exc, ChangeSetGateError):
            status_code = status.HTTP_409_CONFLICT
            code = exc.code
            retryable = False
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "change_set_failed"
            retryable = True
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    def binding_root(project_id: str) -> Path:
        binding = store.get_project_binding(project_id)
        if binding is None:
            raise ChangeSetNotFoundError("Project binding was not found")
        return Path(binding.root_path)

    async def under_project_lock(project_id: str, callback: Callable[[], T]) -> T:
        try:
            root = await run_in_threadpool(binding_root, project_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        async with locks.for_path(root):
            try:
                return await run_in_threadpool(callback)
            except ChangeSetError as exc:
                raise_change_set_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/change-sets",
        response_model=ChangeSetResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_change_set(
        project_id: str,
        request: ChangeSetCreateRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        if request.origin is not ChangeSetOrigin.MANUAL:
            raise ApiError(
                "Public change-set creation only accepts manual origin; "
                "Agent and Skill provenance is Runtime-owned",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="change_set_origin_untrusted",
                retryable=False,
            )
        return await under_project_lock(
            project_id,
            lambda: active.create(project_id, request),
        )

    @router.get(
        "/api/v1/projects/{project_id}/change-sets",
        response_model=ChangeSetListResponse,
    )
    async def list_change_sets(project_id: str) -> ChangeSetListResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.list_for_project, project_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.get(
        "/api/v1/change-sets/{change_set_id}",
        response_model=ChangeSetResponse,
    )
    async def get_change_set(change_set_id: str) -> ChangeSetResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)

    @router.patch(
        "/api/v1/change-sets/{change_set_id}/decisions",
        response_model=ChangeSetResponse,
    )
    async def decide_change_set(
        change_set_id: str,
        request: ChangeSetDecisionRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.decide(change_set_id, request),
        )

    @router.patch(
        "/api/v1/change-sets/{change_set_id}/hunks/{hunk_id}",
        response_model=ChangeSetResponse,
    )
    async def edit_change_set_hunk(
        change_set_id: str,
        hunk_id: str,
        request: ChangeSetHunkEditRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.edit_hunk(change_set_id, hunk_id, request),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/apply",
        response_model=ChangeSetResponse,
    )
    async def apply_change_set(
        change_set_id: str,
        request: ChangeSetApplyRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.apply(change_set_id, request),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/discard",
        response_model=ChangeSetResponse,
    )
    async def discard_change_set(
        change_set_id: str,
        request: ChangeSetDiscardRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.discard(change_set_id, request),
        )

    @router.post(
        "/api/v1/change-sets/{change_set_id}/apply-direct",
        response_model=ChangeSetResponse,
    )
    async def apply_change_set_direct(
        change_set_id: str,
        request: ChangeSetApplyRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.apply_direct(change_set_id, request),
        )


    @router.post(
        "/api/v1/change-sets/{change_set_id}/rollback",
        response_model=ChangeSetResponse,
    )
    async def rollback_change_set(
        change_set_id: str,
        request: ChangeSetRollbackRequest,
    ) -> ChangeSetResponse:
        active = require_service()
        try:
            record = await run_in_threadpool(active.get, change_set_id)
        except ChangeSetError as exc:
            raise_change_set_error(exc)
        return await under_project_lock(
            record.project_id,
            lambda: active.rollback(change_set_id, request),
        )


    return router
