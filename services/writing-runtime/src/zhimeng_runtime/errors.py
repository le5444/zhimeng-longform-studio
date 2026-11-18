from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str
    retryable: bool
    details: Any | None = None


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["zhimeng.error.v1"] = Field(
        default="zhimeng.error.v1",
        alias="schema",
    )
    error: ErrorDetail


class ServiceUnavailableError(RuntimeError):
    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message)
        self.details = details


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details
        self.headers = headers
        self.retryable = retryable


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(
        request: Request,
        exc: ApiError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            details=exc.details,
            headers=exc.headers,
            retryable=exc.retryable,
        )

    @app.exception_handler(ServiceUnavailableError)
    async def service_unavailable_handler(
        request: Request,
        exc: ServiceUnavailableError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="runtime_not_ready",
            message=str(exc),
            details=exc.details,
            retryable=True,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        details = [
            {
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
                "type": item["type"],
            }
            for item in exc.errors()
        ]
        return _error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="request_validation_failed",
            message="Request validation failed",
            details=details,
            retryable=False,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            code = "not_found"
        else:
            code = "http_error"
        message = exc.detail if isinstance(exc.detail, str) else "HTTP request failed"
        return _error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            details=None if isinstance(exc.detail, str) else exc.detail,
            headers=exc.headers,
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="The runtime encountered an unexpected error",
            retryable=True,
        )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unavailable")
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json", exclude_none=True, by_alias=True),
        headers=headers,
    )
