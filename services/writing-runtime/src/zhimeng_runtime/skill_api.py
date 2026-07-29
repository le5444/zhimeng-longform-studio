from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillActivationConflictError,
    ManagedSkillActivationInvalidError,
    ManagedSkillError,
    ManagedSkillImportNotAllowedError,
    ManagedSkillNotFoundError,
    ManagedSkillPackageDriftError,
    ManagedSkillPackageError,
    ManagedSkillProjectNotFoundError,
    ManagedSkillService,
    ManagedSkillVersionConflictError,
)
from zhimeng_runtime.skill_models import (
    ManagedSkillActivationRequest,
    ManagedSkillActivationResponse,
    ManagedSkillCatalogResponse,
    ManagedSkillImportRequest,
    ManagedSkillImportResponse,
    ManagedSkillLifecycleCatalogResponse,
    SkillRegistryResponse,
)
from zhimeng_runtime.skill_registry import SkillRegistry


def create_skill_router(
    registry: SkillRegistry | None,
    *,
    registry_error: str | None = None,
    managed_service: ManagedSkillService | None = None,
    managed_service_error: str | Callable[[], str | None] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["skills"])

    def require_managed_service() -> ManagedSkillService:
        current_managed_service_error = (
            managed_service_error()
            if callable(managed_service_error)
            else managed_service_error
        )
        if current_managed_service_error is not None or managed_service is None:
            raise ApiError(
                "Managed Skill service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="managed_skill_unavailable",
                details=(
                    {"startup_error": current_managed_service_error}
                    if current_managed_service_error is not None
                    else None
                ),
                retryable=False,
            )
        return managed_service

    def raise_managed_error(exc: ManagedSkillError) -> None:
        if isinstance(exc, ManagedSkillProjectNotFoundError | ManagedSkillNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ManagedSkillImportNotAllowedError):
            status_code = status.HTTP_403_FORBIDDEN
        elif isinstance(
            exc,
            ManagedSkillActivationConflictError
            | ManagedSkillPackageDriftError
            | ManagedSkillVersionConflictError,
        ):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, ManagedSkillActivationInvalidError | ManagedSkillPackageError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc

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

    @router.get(
        "/api/v1/projects/{project_id}/skills",
        response_model=ManagedSkillCatalogResponse,
    )
    async def list_project_skills(project_id: str) -> ManagedSkillCatalogResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(active.list_for_project, project_id)
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.get(
        "/api/v1/managed-skills",
        response_model=ManagedSkillLifecycleCatalogResponse,
    )
    async def list_application_managed_skills() -> ManagedSkillLifecycleCatalogResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(active.list_lifecycle)
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.get(
        "/api/v1/projects/{project_id}/managed-skills",
        response_model=ManagedSkillLifecycleCatalogResponse,
    )
    async def list_project_managed_skills(
        project_id: str,
    ) -> ManagedSkillLifecycleCatalogResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(active.list_lifecycle, project_id=project_id)
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.post(
        "/api/v1/managed-skills/imports",
        response_model=ManagedSkillImportResponse,
    )
    async def import_application_managed_skill(
        request: ManagedSkillImportRequest,
    ) -> ManagedSkillImportResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(
                active.import_package,
                request.source_path,
                expected_package_fingerprint=request.expected_package_fingerprint,
            )
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/managed-skills/imports",
        response_model=ManagedSkillImportResponse,
    )
    async def import_project_managed_skill(
        project_id: str,
        request: ManagedSkillImportRequest,
    ) -> ManagedSkillImportResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(
                active.import_package,
                request.source_path,
                project_id=project_id,
                expected_package_fingerprint=request.expected_package_fingerprint,
            )
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.put(
        "/api/v1/managed-skills/{skill_id}/activation",
        response_model=ManagedSkillActivationResponse,
    )
    async def activate_application_managed_skill(
        skill_id: str,
        request: ManagedSkillActivationRequest,
    ) -> ManagedSkillActivationResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(active.activate, skill_id, request)
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    @router.put(
        "/api/v1/projects/{project_id}/managed-skills/{skill_id}/activation",
        response_model=ManagedSkillActivationResponse,
    )
    async def activate_project_managed_skill(
        project_id: str,
        skill_id: str,
        request: ManagedSkillActivationRequest,
    ) -> ManagedSkillActivationResponse:
        active = require_managed_service()
        try:
            return await run_in_threadpool(
                active.activate,
                skill_id,
                request,
                project_id=project_id,
            )
        except ManagedSkillError as exc:
            raise_managed_error(exc)

    return router


__all__ = ["create_skill_router"]
