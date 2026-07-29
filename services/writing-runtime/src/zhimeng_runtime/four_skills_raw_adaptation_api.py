from __future__ import annotations

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.four_skills_raw_adaptation_models import (
    FourSkillsInstructionChainHandoffRequest,
    FourSkillsInstructionChainHandoffResponse,
    FourSkillsGenericExecutionProbeRequest,
    FourSkillsGenericExecutionProbeResponse,
    FourSkillsGenericManagedExecutionRequest,
    FourSkillsGenericManagedExecutionResponse,
    FourSkillsRawAdaptationMaterializeRequest,
    FourSkillsRawAdaptationPlanRequest,
    FourSkillsRawAdaptationResponse,
    FourSkillsWorkflowBindingRequest,
    FourSkillsWorkflowBindingResponse,
    FourSkillsWorkflowOrchestrationRequest,
    FourSkillsWorkflowOrchestrationResponse,
)
from zhimeng_runtime.four_skills_raw_adaptation_service import (
    FourSkillsRawAdaptationConflict,
    FourSkillsRawAdaptationError,
    FourSkillsRawAdaptationInvalid,
    FourSkillsRawAdaptationNotFound,
    FourSkillsRawAdaptationService,
    FourSkillsRawAdaptationUnavailable,
)


def create_four_skills_raw_adaptation_router(
    service: FourSkillsRawAdaptationService | None,
) -> APIRouter:
    router = APIRouter(tags=["four-skills-raw-adaptation"])

    def require_service() -> FourSkillsRawAdaptationService:
        if service is None:
            raise ApiError(
                "Four skills raw adaptation service is unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="four_skills_raw_adaptation_unavailable",
                retryable=False,
            )
        return service

    def raise_error(exc: FourSkillsRawAdaptationError) -> None:
        if isinstance(exc, FourSkillsRawAdaptationNotFound):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, FourSkillsRawAdaptationConflict):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, FourSkillsRawAdaptationInvalid):
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif isinstance(exc, FourSkillsRawAdaptationUnavailable):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc

    @router.post(
        "/api/v1/four-skills-raw-adaptations/plan",
        response_model=FourSkillsRawAdaptationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def plan_four_skills_raw_adaptation(
        request: FourSkillsRawAdaptationPlanRequest,
    ) -> FourSkillsRawAdaptationResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.plan, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.post(
        "/api/v1/four-skills-raw-adaptations/materialize",
        response_model=FourSkillsRawAdaptationResponse,
    )
    async def materialize_four_skills_raw_adaptation(
        request: FourSkillsRawAdaptationMaterializeRequest,
    ) -> FourSkillsRawAdaptationResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.materialize, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}",
        response_model=FourSkillsRawAdaptationResponse,
    )
    async def get_four_skills_raw_adaptation(
        batch_id: str,
    ) -> FourSkillsRawAdaptationResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)


    @router.post(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
        response_model=FourSkillsWorkflowBindingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def bind_four_skills_workflow(
        batch_id: str,
        request: FourSkillsWorkflowBindingRequest,
    ) -> FourSkillsWorkflowBindingResponse:
        active = require_service()
        if request.expected_batch_id != batch_id:
            raise ApiError(
                "path batch_id does not match request expected_batch_id",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="four_skills_raw_adaptation_invalid",
                retryable=False,
            )
        try:
            return await run_in_threadpool(active.bind_workflow, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
        response_model=FourSkillsWorkflowBindingResponse,
    )
    async def get_four_skills_workflow_binding(
        batch_id: str,
    ) -> FourSkillsWorkflowBindingResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_workflow_binding, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)



    @router.post(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-orchestrations",
        response_model=FourSkillsWorkflowOrchestrationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def orchestrate_four_skills_workflow(
        batch_id: str,
        request: FourSkillsWorkflowOrchestrationRequest,
    ) -> FourSkillsWorkflowOrchestrationResponse:
        active = require_service()
        if request.expected_batch_id != batch_id:
            raise ApiError(
                "path batch_id does not match request expected_batch_id",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="four_skills_raw_adaptation_invalid",
                retryable=False,
            )
        try:
            return await run_in_threadpool(active.orchestrate_workflow, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-orchestrations",
        response_model=FourSkillsWorkflowOrchestrationResponse,
    )
    async def get_four_skills_workflow_orchestration(
        batch_id: str,
    ) -> FourSkillsWorkflowOrchestrationResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_workflow_orchestration, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)



    @router.post(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs",
        response_model=FourSkillsInstructionChainHandoffResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def handoff_four_skills_instruction_chain(
        batch_id: str,
        request: FourSkillsInstructionChainHandoffRequest,
    ) -> FourSkillsInstructionChainHandoffResponse:
        active = require_service()
        if request.expected_batch_id != batch_id:
            raise ApiError(
                "path batch_id does not match request expected_batch_id",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="four_skills_raw_adaptation_invalid",
                retryable=False,
            )
        try:
            return await run_in_threadpool(active.handoff_instruction_chain, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs",
        response_model=FourSkillsInstructionChainHandoffResponse,
    )
    async def get_four_skills_instruction_chain_handoff(
        batch_id: str,
    ) -> FourSkillsInstructionChainHandoffResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_instruction_chain_handoff, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)



    @router.post(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/generic-execution-probes",
        response_model=FourSkillsGenericExecutionProbeResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def probe_four_skills_generic_execution(
        batch_id: str,
        request: FourSkillsGenericExecutionProbeRequest,
    ) -> FourSkillsGenericExecutionProbeResponse:
        active = require_service()
        if request.expected_batch_id != batch_id:
            raise ApiError(
                "path batch_id does not match request expected_batch_id",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="four_skills_raw_adaptation_invalid",
                retryable=False,
            )
        try:
            return await run_in_threadpool(active.probe_generic_execution, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/generic-execution-probes",
        response_model=FourSkillsGenericExecutionProbeResponse,
    )
    async def get_four_skills_generic_execution_probe(
        batch_id: str,
    ) -> FourSkillsGenericExecutionProbeResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_generic_execution_probe, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)



    @router.post(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/generic-managed-executions",
        response_model=FourSkillsGenericManagedExecutionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def execute_four_skills_generic_managed(
        batch_id: str,
        request: FourSkillsGenericManagedExecutionRequest,
    ) -> FourSkillsGenericManagedExecutionResponse:
        active = require_service()
        if request.expected_batch_id != batch_id:
            raise ApiError(
                "path batch_id does not match request expected_batch_id",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="four_skills_raw_adaptation_invalid",
                retryable=False,
            )
        try:
            return await run_in_threadpool(active.execute_generic_managed, request)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)

    @router.get(
        "/api/v1/four-skills-raw-adaptations/{batch_id}/generic-managed-executions",
        response_model=FourSkillsGenericManagedExecutionResponse,
    )
    async def get_four_skills_generic_managed_execution(
        batch_id: str,
    ) -> FourSkillsGenericManagedExecutionResponse:
        active = require_service()
        try:
            return await run_in_threadpool(active.get_generic_managed_execution, batch_id)
        except FourSkillsRawAdaptationError as exc:
            raise_error(exc)


    return router


__all__ = ["create_four_skills_raw_adaptation_router"]
