from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.formal_draft_models import (
    FormalDraftAuthorizationResponse,
    FormalDraftAuthorDecisionRequest,
    FormalDraftAuthorDecisionResponse,
    FormalDraftEvaluateRequest,
)
from zhimeng_runtime.formal_draft_continuous_o2_models import (
    FormalDraftContinuousO2EvaluateRequest,
    FormalDraftContinuousO2Response,
)
from zhimeng_runtime.formal_draft_provenance_models import (
    FormalDraftProvenanceEvaluateRequest,
    FormalDraftProvenanceResponse,
)
from zhimeng_runtime.formal_draft_service import (
    FormalDraftAuthorizationConflict,
    FormalDraftAuthorizationError,
    FormalDraftAuthorizationInvalid,
    FormalDraftAuthorizationNotFound,
    FormalDraftAuthorizationService,
)
from zhimeng_runtime.four_skills_raw_adaptation_models import (
    ProjectInstructionChainHandoffBindRequest,
    ProjectInstructionChainHandoffBindingResponse,
)


def create_formal_draft_router(service: FormalDraftAuthorizationService) -> APIRouter:
    router = APIRouter(tags=["formal-draft-authorization"])

    def raise_error(exc: FormalDraftAuthorizationError) -> NoReturn:
        if isinstance(exc, FormalDraftAuthorizationNotFound):
            status_code = status.HTTP_404_NOT_FOUND
            code = "formal_draft_authorization_not_found"
        elif isinstance(exc, FormalDraftAuthorizationInvalid):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            code = "formal_draft_authorization_invalid"
        elif isinstance(exc, FormalDraftAuthorizationConflict):
            status_code = status.HTTP_409_CONFLICT
            code = "formal_draft_authorization_conflict"
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            code = "formal_draft_authorization_failed"
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            details=exc.details,
            retryable=exc.retryable,
        ) from exc

    @router.post(
        "/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
        response_model=FormalDraftAuthorizationResponse,
    )
    async def evaluate_formal_draft_authorization(
        project_id: str,
        request: FormalDraftEvaluateRequest | None = None,
    ) -> FormalDraftAuthorizationResponse:
        try:
            return await run_in_threadpool(
                service.evaluate,
                project_id,
                request or FormalDraftEvaluateRequest(),
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/projects/{project_id}/formal-draft-authorization/author-decision",
        response_model=FormalDraftAuthorDecisionResponse,
    )
    async def get_formal_draft_author_decision(
        project_id: str,
    ) -> FormalDraftAuthorDecisionResponse:
        try:
            return await run_in_threadpool(service.get_author_decision, project_id)
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/formal-draft-authorization/author-decision",
        response_model=FormalDraftAuthorDecisionResponse,
    )
    async def record_formal_draft_author_decision(
        project_id: str,
        request: FormalDraftAuthorDecisionRequest,
    ) -> FormalDraftAuthorDecisionResponse:
        try:
            return await run_in_threadpool(
                service.record_author_decision,
                project_id,
                request,
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/formal-draft-authorization/provenance/evaluate",
        response_model=FormalDraftProvenanceResponse,
    )
    async def evaluate_formal_draft_provenance(
        project_id: str,
        request: FormalDraftProvenanceEvaluateRequest | None = None,
    ) -> FormalDraftProvenanceResponse:
        try:
            return await run_in_threadpool(
                service.evaluate_provenance,
                project_id,
                request or FormalDraftProvenanceEvaluateRequest(),
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/formal-draft-authorization/continuous-o2/evaluate",
        response_model=FormalDraftContinuousO2Response,
    )
    async def evaluate_formal_draft_continuous_o2(
        project_id: str,
        request: FormalDraftContinuousO2EvaluateRequest | None = None,
    ) -> FormalDraftContinuousO2Response:
        try:
            return await run_in_threadpool(
                service.evaluate_continuous_o2,
                project_id,
                request or FormalDraftContinuousO2EvaluateRequest(),
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/projects/{project_id}/instruction-chain-handoff-bindings",
        response_model=ProjectInstructionChainHandoffBindingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def bind_project_instruction_chain_handoff(
        project_id: str,
        request: ProjectInstructionChainHandoffBindRequest,
    ) -> ProjectInstructionChainHandoffBindingResponse:
        try:
            return await run_in_threadpool(
                service.bind_instruction_chain_handoff,
                project_id,
                request,
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/projects/{project_id}/instruction-chain-handoff-bindings",
        response_model=ProjectInstructionChainHandoffBindingResponse,
    )
    async def get_project_instruction_chain_handoff_binding(
        project_id: str,
    ) -> ProjectInstructionChainHandoffBindingResponse:
        try:
            return await run_in_threadpool(
                service.get_instruction_chain_handoff_binding,
                project_id,
            )
        except FormalDraftAuthorizationError as exc:
            raise_error(exc)

    return router


__all__ = ["create_formal_draft_router"]
