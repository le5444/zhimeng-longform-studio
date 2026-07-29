from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette import status

from zhimeng_runtime.distillation_adapter import (
    DistillationAdapterError,
    DistillationPreviewAdapter,
)
from zhimeng_runtime.distillation_invocation_store import (
    DistillationInvocationSnapshot,
    DistillationInvocationStore,
    DistillationInvocationStoreConflict,
    DistillationInvocationStoreError,
    DistillationInvocationStoreIntegrity,
    DistillationInvocationStoreNotFound,
)
from zhimeng_runtime.distillation_anchor_models import DistillationAnchorCatalog
from zhimeng_runtime.distillation_invocation_service import (
    DistillationInvocationService,
    DistillationInvocationServiceConflict,
    DistillationInvocationServiceError,
    DistillationInvocationServiceUnavailable,
)
from zhimeng_runtime.distillation_models import (
    DistillationCoverageRequest,
    DistillationCoverageResponse,
    DistillationHealthResponse,
)
from zhimeng_runtime.distillation_receipt_models import DistillationInvocationReceipt
from zhimeng_runtime.errors import ApiError


class DistillationInvocationPreparedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=r"^distillation-idempotency-[0-9a-f]{32}$")
    receipt: dict[str, Any]


class DistillationInvocationAppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: dict[str, Any]
    persist_candidates: bool = True


class DistillationInvocationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_catalog: dict[str, Any]
    text_by_anchor_id: dict[str, str]


def create_distillation_router(
    adapter: DistillationPreviewAdapter | None,
    *,
    adapter_error: str | None = None,
    invocation_store: DistillationInvocationStore | None = None,
    invocation_service: DistillationInvocationService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/distillation", tags=["distillation"])

    def require_adapter() -> DistillationPreviewAdapter:
        if adapter is None:
            raise ApiError(
                adapter_error or "Distillation preview is not configured",
                status_code=503,
                code="distillation_unavailable",
                retryable=False,
            )
        return adapter

    def require_invocation_store() -> DistillationInvocationStore:
        if invocation_store is None:
            raise ApiError(
                "Runtime-owned deep distillation invocation store is not configured",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="distillation_invocation_unavailable",
                retryable=False,
            )
        return invocation_store

    def require_invocation_service() -> DistillationInvocationService:
        if invocation_service is None:
            raise ApiError(
                "Runtime-owned deep distillation Provider loop is not configured",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="distillation_invocation_service_unavailable",
                retryable=False,
            )
        return invocation_service

    def raise_adapter_error(exc: DistillationAdapterError) -> None:
        raise ApiError(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    def raise_store_error(exc: DistillationInvocationStoreError) -> None:
        if isinstance(exc, DistillationInvocationStoreNotFound):
            status_code = status.HTTP_404_NOT_FOUND
            code = "distillation_invocation_not_found"
        elif isinstance(exc, DistillationInvocationStoreConflict):
            status_code = status.HTTP_409_CONFLICT
            code = "distillation_invocation_conflict"
        elif isinstance(exc, DistillationInvocationStoreIntegrity):
            status_code = status.HTTP_409_CONFLICT
            code = "distillation_invocation_integrity_error"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "distillation_invocation_store_error"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=False,
        ) from exc

    def raise_service_error(exc: DistillationInvocationServiceError) -> None:
        if isinstance(exc, DistillationInvocationServiceUnavailable):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            code = "distillation_provider_unavailable"
        elif isinstance(exc, DistillationInvocationServiceConflict):
            status_code = status.HTTP_409_CONFLICT
            code = "distillation_invocation_conflict"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "distillation_invocation_service_error"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=False,
        ) from exc

    def parse_receipt(payload: dict[str, Any]) -> DistillationInvocationReceipt:
        try:
            return DistillationInvocationReceipt.model_validate_json(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ApiError(
                "Distillation invocation receipt failed its contract",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="distillation_receipt_invalid",
                details=str(exc),
                retryable=False,
            ) from exc

    def parse_anchor_catalog(payload: dict[str, Any]) -> DistillationAnchorCatalog:
        try:
            return DistillationAnchorCatalog.model_validate_json(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ApiError(
                "Distillation anchor catalog failed its contract",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="distillation_anchor_catalog_invalid",
                details=str(exc),
                retryable=False,
            ) from exc

    def snapshot_payload(snapshot: DistillationInvocationSnapshot) -> dict[str, Any]:
        return {
            "schema": "zhimeng.distillation-invocation-snapshot.v1",
            "invocation": snapshot.invocation.model_dump(mode="json", by_alias=True),
            "last_receipt_id": snapshot.last_receipt_id,
            "receipts": [
                receipt.model_dump(mode="json", by_alias=True)
                for receipt in snapshot.receipts
            ],
        }

    @router.get("/health", response_model=DistillationHealthResponse)
    async def get_distillation_health() -> DistillationHealthResponse:
        active = require_adapter()
        try:
            return await run_in_threadpool(active.health)
        except DistillationAdapterError as exc:
            raise_adapter_error(exc)

    @router.post("/coverage", response_model=DistillationCoverageResponse)
    async def get_distillation_coverage(
        request: DistillationCoverageRequest,
    ) -> DistillationCoverageResponse:
        active = require_adapter()
        try:
            return await run_in_threadpool(active.coverage, request)
        except DistillationAdapterError as exc:
            raise_adapter_error(exc)

    @router.get("/invocations/{invocation_id}")
    async def get_distillation_invocation(invocation_id: str) -> dict[str, Any]:
        active = require_invocation_store()
        try:
            snapshot = await run_in_threadpool(active.get, invocation_id)
        except DistillationInvocationStoreError as exc:
            raise_store_error(exc)
        return snapshot_payload(snapshot)

    @router.get("/invocations/{invocation_id}/receipts")
    async def list_distillation_invocation_receipts(
        invocation_id: str,
    ) -> dict[str, Any]:
        active = require_invocation_store()
        try:
            receipts = await run_in_threadpool(active.list_receipts, invocation_id)
        except DistillationInvocationStoreError as exc:
            raise_store_error(exc)
        return {
            "schema": "zhimeng.distillation-invocation-receipt-list.v1",
            "invocation_id": invocation_id,
            "receipts": [
                receipt.model_dump(mode="json", by_alias=True)
                for receipt in receipts
            ],
        }

    @router.post("/invocations/prepared")
    async def create_distillation_invocation_prepared(
        request: DistillationInvocationPreparedRequest,
    ) -> dict[str, Any]:
        active = require_invocation_store()
        receipt = parse_receipt(request.receipt)
        try:
            snapshot = await run_in_threadpool(
                active.create_prepared,
                receipt=receipt,
                idempotency_key=request.idempotency_key,
            )
        except DistillationInvocationStoreError as exc:
            raise_store_error(exc)
        return snapshot_payload(snapshot)

    @router.post("/invocations/{invocation_id}/receipts")
    async def append_distillation_invocation_receipt(
        invocation_id: str,
        request: DistillationInvocationAppendRequest,
    ) -> dict[str, Any]:
        active = require_invocation_store()
        receipt = parse_receipt(request.receipt)
        if receipt.invocation_id != invocation_id:
            raise ApiError(
                "Distillation receipt path does not match its invocation",
                status_code=status.HTTP_409_CONFLICT,
                code="distillation_invocation_path_mismatch",
                retryable=False,
            )
        try:
            snapshot = await run_in_threadpool(
                active.append_receipt,
                receipt,
                persist_candidates=request.persist_candidates,
            )
        except DistillationInvocationStoreError as exc:
            raise_store_error(exc)
        return snapshot_payload(snapshot)

    @router.post("/invocations/{invocation_id}/run")
    async def run_distillation_invocation(
        invocation_id: str,
        request: DistillationInvocationRunRequest,
    ) -> dict[str, Any]:
        active = require_invocation_service()
        anchor_catalog = parse_anchor_catalog(request.anchor_catalog)
        if anchor_catalog.invocation_id != invocation_id:
            raise ApiError(
                "Distillation anchor catalog path does not match its invocation",
                status_code=status.HTTP_409_CONFLICT,
                code="distillation_invocation_path_mismatch",
                retryable=False,
            )
        try:
            result = await active.run_prepared(
                invocation_id,
                anchor_catalog=anchor_catalog,
                text_by_anchor_id=request.text_by_anchor_id,
            )
        except DistillationInvocationStoreError as exc:
            raise_store_error(exc)
        except DistillationInvocationServiceError as exc:
            raise_service_error(exc)
        payload = snapshot_payload(result.snapshot)
        payload["terminal_reason"] = result.terminal_reason
        return payload

    return router
