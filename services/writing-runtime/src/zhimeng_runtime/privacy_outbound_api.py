from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.privacy_outbound_service import (
    PrivacyOutboundError,
    PrivacyOutboundService,
)


def create_privacy_outbound_router(
    service: PrivacyOutboundService | None,
) -> APIRouter:
    router = APIRouter(tags=["privacy-outbound"])

    def require_service() -> PrivacyOutboundService:
        if service is None:
            raise ApiError(
                "Privacy outbound service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="privacy_outbound_unavailable",
                retryable=False,
            )
        return service

    @router.get("/api/v1/privacy/outbound-disclosure")
    async def get_outbound_disclosure() -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(active.disclosure)
        except PrivacyOutboundError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code=exc.code,
                retryable=False,
            ) from exc

    return router


__all__ = ["create_privacy_outbound_router"]
