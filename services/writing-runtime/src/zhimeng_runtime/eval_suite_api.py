from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.eval_suite_models import (
    EvalSuiteActionRequest,
    EvalSuiteListResponse,
    EvalSuitePrepareRequest,
    EvalSuiteProviderFingerprintResponse,
    EvalSuiteResponse,
)
from zhimeng_runtime.eval_suite_service import (
    EvalSuiteBlockedError,
    EvalSuiteConflictError,
    EvalSuiteError,
    EvalSuiteInvalidError,
    EvalSuiteNotFoundError,
    EvalSuiteService,
)


def create_eval_suite_router(service: EvalSuiteService | None) -> APIRouter:
    router = APIRouter(tags=["eval-suite"])

    def require_service() -> EvalSuiteService:
        if service is None:
            raise ApiError(
                "Eval suite service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="eval_suite_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: EvalSuiteError) -> None:
        if isinstance(exc, EvalSuiteNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, EvalSuiteConflictError | EvalSuiteBlockedError):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, EvalSuiteInvalidError):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc


    @router.get(
        "/api/v1/eval-suites",
        response_model=EvalSuiteListResponse,
    )
    async def list_eval_suites() -> EvalSuiteListResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.list_suites)
        except EvalSuiteError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/eval-suites/prepare",
        response_model=EvalSuiteResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def prepare_eval_suite(
        request: EvalSuitePrepareRequest,
    ) -> EvalSuiteResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.prepare, request)
        except EvalSuiteError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/eval-suites/{suite_id}",
        response_model=EvalSuiteResponse,
    )
    async def get_eval_suite(suite_id: str) -> EvalSuiteResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, suite_id)
        except EvalSuiteError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/eval-suites/{suite_id}/actions",
        response_model=EvalSuiteResponse,
    )
    async def act_eval_suite(
        suite_id: str,
        request: EvalSuiteActionRequest,
    ) -> EvalSuiteResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.act, suite_id, request)
        except EvalSuiteError as exc:
            raise_error(exc)


    @router.post(
        "/api/v1/eval-suites/{suite_id}/provider-fingerprint-report",
        response_model=EvalSuiteProviderFingerprintResponse,
    )
    async def build_provider_fingerprint_report(
        suite_id: str,
    ) -> EvalSuiteProviderFingerprintResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.build_provider_fingerprint_report,
                suite_id,
            )
        except EvalSuiteError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/eval-suites/{suite_id}/provider-fingerprint-report",
        response_model=EvalSuiteProviderFingerprintResponse,
    )
    async def get_provider_fingerprint_report(
        suite_id: str,
    ) -> EvalSuiteProviderFingerprintResponse:
        active = require_service()
        try:
            return await run_in_threadpool(
                active.get_provider_fingerprint_report,
                suite_id,
            )
        except EvalSuiteError as exc:
            raise_error(exc)


    return router


__all__ = ["create_eval_suite_router"]
