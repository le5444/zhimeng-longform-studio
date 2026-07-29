from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.external_blind_review_matrix import (
    evaluate_external_blind_review_matrix,
    write_external_blind_review_matrix_receipt,
)


class ExternalBlindReviewMatrixEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str | None = Field(default=None, min_length=1, max_length=160)
    evidence_path: str | None = Field(default=None, min_length=1, max_length=1024)
    evidence: dict[str, Any] | None = None
    write_receipt_to: str | None = Field(default=None, min_length=1, max_length=1024)


def create_external_blind_review_matrix_router() -> APIRouter:
    router = APIRouter(tags=["external-blind-review-matrix"])

    @router.post("/api/v1/external-blind-review-matrix/evaluate")
    async def evaluate_matrix(
        request: ExternalBlindReviewMatrixEvaluateRequest | None = None,
    ) -> dict[str, Any]:
        request = request or ExternalBlindReviewMatrixEvaluateRequest()
        if request.evidence is not None and request.evidence_path is not None:
            raise ApiError(
                "Provide either evidence or evidence_path, not both",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="external_blind_review_matrix_invalid",
                retryable=False,
            )
        evidence: Path | dict[str, Any] | None
        if request.evidence is not None:
            evidence = request.evidence
        elif request.evidence_path is not None:
            evidence = Path(request.evidence_path)
        else:
            evidence = None

        def _run() -> dict[str, Any]:
            response = evaluate_external_blind_review_matrix(
                evidence,
                suite_id=request.suite_id,
            )
            if request.write_receipt_to:
                write_external_blind_review_matrix_receipt(
                    response,
                    Path(request.write_receipt_to),
                )
            return response

        return await run_in_threadpool(_run)

    return router


__all__ = ["create_external_blind_review_matrix_router"]
