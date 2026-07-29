from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.distillation_invocation_prepare import (
    SourceImportDistillationPrepareError,
    SourceImportDistillationPrepareRequest,
    SourceImportDistillationPrepareService,
)
from zhimeng_runtime.source_import_models import (
    AnySourceImportJob,
    LocalFileSourceCandidate,
    LocalSourceImportCreateRequest,
    LocalSourceImportJob,
    LocalSourceInspectRequest,
    SourceImportCreateRequest,
    SourceImportJob,
    SourceImportListResponse,
    TomatoResolveRequest,
    TomatoResolveResponse,
    TomatoSearchRequest,
    TomatoSearchResponse,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_service import (
    SourceImportError,
    SourceImportExecutionError,
    SourceImportIntegrityError,
    SourceImportNotFoundError,
    SourceImportService,
    SourceImportStateError,
)
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportAttemptReceipt,
    SourceImportExecutionReceiptRecord,
    SourceImportLineageStatus,
    parse_source_import_execution_receipt,
    validate_source_import_receipt_job,
)


def create_source_import_router(
    service: SourceImportService | None,
    *,
    store: SqliteStore | None = None,
    service_error: str | None = None,
    distillation_prepare_service: SourceImportDistillationPrepareService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/source-imports", tags=["source-imports"])

    def require_service() -> SourceImportService:
        if service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service_error or "Source import adapters are not configured",
            )
        return service

    def raise_source_import_error(exc: SourceImportError) -> NoReturn:
        if isinstance(exc, SourceImportNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "source_import_not_found"
            retryable = False
        elif isinstance(exc, SourceImportIntegrityError):
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "source_import_receipt_invalid"
            retryable = False
        elif isinstance(exc, SourceImportStateError):
            status_code = status.HTTP_409_CONFLICT
            code = "source_import_state_invalid"
            retryable = False
        elif isinstance(exc, SourceImportExecutionError):
            status_code = status.HTTP_502_BAD_GATEWAY
            code = "source_import_adapter_failed"
            retryable = True
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "source_import_failed"
            retryable = True
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    def require_distillation_prepare_service() -> SourceImportDistillationPrepareService:
        if distillation_prepare_service is None:
            raise ApiError(
                "Runtime deep distillation prepare is not configured",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="distillation_prepare_unavailable",
                retryable=False,
            )
        return distillation_prepare_service

    def raise_distillation_prepare_error(
        exc: SourceImportDistillationPrepareError,
    ) -> NoReturn:
        raise ApiError(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    def distillation_prepare_payload(result) -> dict[str, object]:
        return {
            "schema": "zhimeng.source-import-distillation-prepare-result.v1",
            "snapshot": {
                "schema": "zhimeng.distillation-invocation-snapshot.v1",
                "invocation": result.snapshot.invocation.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                "last_receipt_id": result.snapshot.last_receipt_id,
                "receipts": [
                    receipt.model_dump(mode="json", by_alias=True)
                    for receipt in result.snapshot.receipts
                ],
            },
            "anchor_catalog": result.anchor_catalog.model_dump(
                mode="json",
                by_alias=True,
            ),
            "text_by_anchor_id": dict(result.text_by_anchor_id),
        }

    def read_job(import_id: str) -> AnySourceImportJob:
        if service is not None:
            return service.get(import_id)
        if store is None:
            require_service()
        try:
            payload = store.get_source_import_job(import_id)
            if payload is None:
                raise SourceImportNotFoundError("Source import was not found")
            return parse_source_import_job(payload)
        except SourceImportNotFoundError:
            raise
        except Exception as exc:
            raise SourceImportIntegrityError(
                "Stored source import receipt is invalid"
            ) from exc

    def read_list() -> SourceImportListResponse:
        if service is not None:
            return service.list()
        if store is None:
            require_service()
        try:
            return SourceImportListResponse(
                imports=[
                    parse_source_import_job(payload)
                    for payload in store.list_source_import_jobs()
                ]
            )
        except Exception as exc:
            raise SourceImportIntegrityError(
                "Stored source import list is invalid"
            ) from exc

    def read_execution_receipt(
        import_id: str,
    ) -> SourceImportExecutionReceiptRecord:
        job = read_job(import_id)
        if service is not None:
            return service.get_execution_receipt(import_id)
        if store is None:
            require_service()
        try:
            payload = store.get_source_import_execution_receipt(import_id)
            if payload is None:
                return SourceImportExecutionReceiptRecord(
                    source_import_id=import_id,
                    lineage_status=SourceImportLineageStatus.LEGACY_UNVERIFIED,
                    receipt=None,
                )
            receipt = parse_source_import_execution_receipt(payload)
            validate_source_import_receipt_job(receipt, job)
            return SourceImportExecutionReceiptRecord(
                source_import_id=import_id,
                lineage_status=SourceImportLineageStatus.VERIFIED,
                receipt=receipt,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "Stored source import execution receipt is invalid"
            ) from exc

    def read_attempt_receipts(
        import_id: str,
    ) -> list[SourceImportAttemptReceipt]:
        read_job(import_id)
        if service is not None:
            return service.list_attempt_receipts(import_id)
        if store is None:
            require_service()
        try:
            return [
                SourceImportAttemptReceipt.model_validate(payload)
                for payload in store.list_source_import_attempt_receipts(import_id)
            ]
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "Stored source import attempt receipts are invalid"
            ) from exc

    @router.post("/tomato/search", response_model=TomatoSearchResponse)
    async def search_tomato(request: TomatoSearchRequest) -> TomatoSearchResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.search, request.query)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post("/tomato/resolve", response_model=TomatoResolveResponse)
    async def resolve_tomato(request: TomatoResolveRequest) -> TomatoResolveResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.resolve_tomato, request)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post("/local/inspect", response_model=LocalFileSourceCandidate)
    async def inspect_local_source(
        request: LocalSourceInspectRequest,
    ) -> LocalFileSourceCandidate:
        active = require_service()
        try:
            return await run_in_threadpool(active.inspect_local, request.source_path)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post(
        "",
        response_model=SourceImportJob | LocalSourceImportJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_source_import(
        # Keep the provider field optional for the historical Tomato v1 body;
        # Pydantic's literal/default model selects Tomato when it is omitted.
        request: SourceImportCreateRequest | LocalSourceImportCreateRequest,
    ) -> AnySourceImportJob:
        active = require_service()
        try:
            return await run_in_threadpool(active.create, request)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.get("", response_model=SourceImportListResponse)
    async def list_source_imports() -> SourceImportListResponse:
        try:
            return await run_in_threadpool(read_list)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.get(
        "/{import_id}",
        response_model=SourceImportJob | LocalSourceImportJob,
    )
    async def get_source_import(import_id: str) -> AnySourceImportJob:
        try:
            return await run_in_threadpool(read_job, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.get(
        "/{import_id}/execution-receipt",
        response_model=SourceImportExecutionReceiptRecord,
    )
    async def get_source_import_execution_receipt(
        import_id: str,
    ) -> SourceImportExecutionReceiptRecord:
        try:
            return await run_in_threadpool(read_execution_receipt, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.get(
        "/{import_id}/attempt-receipts",
        response_model=list[SourceImportAttemptReceipt],
    )
    async def list_source_import_attempt_receipts(
        import_id: str,
    ) -> list[SourceImportAttemptReceipt]:
        try:
            return await run_in_threadpool(read_attempt_receipts, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post("/{import_id}/distillation-invocation/prepare")
    async def prepare_source_import_distillation_invocation(
        import_id: str,
        request: SourceImportDistillationPrepareRequest,
    ) -> dict[str, object]:
        active = require_distillation_prepare_service()
        try:
            result = await run_in_threadpool(active.prepare, import_id, request)
        except SourceImportDistillationPrepareError as exc:
            raise_distillation_prepare_error(exc)
        return distillation_prepare_payload(result)

    @router.post(
        "/{import_id}/cancel",
        response_model=SourceImportJob | LocalSourceImportJob,
    )
    async def cancel_source_import(import_id: str) -> AnySourceImportJob:
        active = require_service()
        try:
            return await run_in_threadpool(active.cancel, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post(
        "/{import_id}/retry",
        response_model=SourceImportJob | LocalSourceImportJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_source_import(import_id: str) -> AnySourceImportJob:
        active = require_service()
        try:
            return await run_in_threadpool(active.retry, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)

    @router.post(
        "/{import_id}/network-recovery-evidence",
        status_code=status.HTTP_201_CREATED,
    )
    async def build_network_recovery_evidence(import_id: str) -> dict:
        active = require_service()
        try:
            return await run_in_threadpool(active.build_network_recovery_evidence, import_id)
        except SourceImportError as exc:
            raise_source_import_error(exc)


    return router
