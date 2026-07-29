from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.evaluation_scenario_service import (
    EvaluationScenarioError,
    EvaluationScenarioInvalid,
    EvaluationScenarioNotFound,
    EvaluationScenarioService,
)


class EvaluationScenarioBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str = Field(min_length=8, max_length=160)
    suite_receipt_hash: str = Field(min_length=64, max_length=64)
    member_count: int = Field(ge=1, le=32)
    has_provider_fingerprint: bool = False
    blind_review_finalized: bool = False


class EvaluationScenarioExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=8, max_length=160)
    project_revision: int = Field(ge=0)
    project_fingerprint: str = Field(min_length=8, max_length=128)
    available_chapters: list[str] = Field(default_factory=list, max_length=256)
    available_artifacts: list[str] = Field(default_factory=list, max_length=128)
    suite_id: str | None = Field(default=None, min_length=8, max_length=160)
    suite_receipt_hash: str | None = Field(default=None, min_length=64, max_length=64)
    member_count: int = Field(default=1, ge=1, le=32)
    has_provider_fingerprint: bool = False
    blind_review_finalized: bool = False
    external_blind_review_present: bool = False


def create_evaluation_scenario_router(
    service: EvaluationScenarioService | None,
) -> APIRouter:
    router = APIRouter(tags=["evaluation-scenarios"])

    def require_service() -> EvaluationScenarioService:
        if service is None:
            raise ApiError(
                "Evaluation scenario service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="evaluation_scenario_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: EvaluationScenarioError) -> None:
        if isinstance(exc, EvaluationScenarioNotFound):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, EvaluationScenarioInvalid):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc

    @router.get("/api/v1/evaluation-scenario-packs")
    async def list_packs() -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(active.list_packs)
        except EvaluationScenarioError as exc:
            raise_error(exc)

    @router.get("/api/v1/evaluation-scenario-packs/{pack_id}")
    async def get_pack(pack_id: str) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_pack, pack_id)
        except EvaluationScenarioError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/evaluation-scenario-packs/{pack_id}/suite-bindings",
        status_code=status.HTTP_201_CREATED,
    )
    async def bind_pack(pack_id: str, request: EvaluationScenarioBindRequest) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.bind_pack_to_suite,
                pack_id=pack_id,
                suite_id=request.suite_id,
                suite_receipt_hash=request.suite_receipt_hash,
                member_count=request.member_count,
                has_provider_fingerprint=request.has_provider_fingerprint,
                blind_review_finalized=request.blind_review_finalized,
            )
        except EvaluationScenarioError as exc:
            raise_error(exc)


    @router.post(
        "/api/v1/evaluation-scenario-packs/{pack_id}/executions",
        status_code=status.HTTP_201_CREATED,
    )
    async def execute_pack(
        pack_id: str, request: EvaluationScenarioExecuteRequest
    ) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.execute_pack_on_project,
                pack_id=pack_id,
                project_id=request.project_id,
                project_revision=request.project_revision,
                project_fingerprint=request.project_fingerprint,
                available_chapters=list(request.available_chapters),
                available_artifacts=list(request.available_artifacts),
                suite_id=request.suite_id,
                suite_receipt_hash=request.suite_receipt_hash,
                member_count=request.member_count,
                has_provider_fingerprint=request.has_provider_fingerprint,
                blind_review_finalized=request.blind_review_finalized,
                external_blind_review_present=request.external_blind_review_present,
            )
        except EvaluationScenarioError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/evaluation-scenario-packs/{pack_id}/executions/latest",
    )
    async def get_latest_execution(pack_id: str, project_id: str) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_latest_execution,
                pack_id=pack_id,
                project_id=project_id,
            )
        except EvaluationScenarioError as exc:
            raise_error(exc)

    return router


__all__ = ["create_evaluation_scenario_router"]
