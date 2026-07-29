from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from test_agent_workflow_models import _run
from test_workflow_contracts import _completed_chain
from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_models import (
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentWorkflowStateResponse,
)
from zhimeng_runtime.agent_store import (
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
)
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.execution_protocol import RunStatus


class WorkflowApiService:
    def __init__(self) -> None:
        self.workflow_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.workflow_calls: list[str] = []
        self.resume_calls: list[tuple[str, AgentRunResumeRequest]] = []

    async def get_workflow_state(self, run_id: str) -> AgentWorkflowStateResponse:
        self.workflow_calls.append(run_id)
        if self.workflow_error is not None:
            raise self.workflow_error
        plan, first, _ = _completed_chain()
        return AgentWorkflowStateResponse(
            run_id=run_id,
            run_status=RunStatus.PAUSED,
            plan=plan,
            checkpoints=(first,),
            provider_boundary=None,
            last_checkpoint_id=first.checkpoint_id,
            last_checkpoint_hash=first.checkpoint_hash,
            next_step_ordinal=2,
            resume_state="resumable",
            resume_allowed=True,
            reason_code="workflow_paused_before_provider",
            formal_draft_allowed=False,
        )

    async def resume_run(
        self,
        run_id: str,
        request: AgentRunResumeRequest,
    ) -> AgentRunResumeResponse:
        self.resume_calls.append((run_id, request))
        if self.resume_error is not None:
            raise self.resume_error
        plan, first, _ = _completed_chain()
        return AgentRunResumeResponse(
            run=_run().model_copy(update={"run_id": run_id}),
            accepted=True,
            workflow_plan_id=plan.workflow_plan_id,
            resumed_from_checkpoint_id=first.checkpoint_id,
            resumed_from_checkpoint_hash=first.checkpoint_hash,
            next_action="workflow_step",
        )


def _client(service: WorkflowApiService) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(
        create_agent_router(  # type: ignore[arg-type]
            service,
            heartbeat_seconds=0.01,
        )
    )
    return TestClient(app)


def _resume_payload() -> tuple[str, dict[str, object]]:
    plan, first, _ = _completed_chain()
    return plan.run_id, {
        "schema": "zhimeng.agent-run-resume-request.v1",
        "expected_workflow_plan_id": plan.workflow_plan_id,
        "expected_plan_hash": plan.plan_hash,
        "expected_checkpoint_id": first.checkpoint_id,
        "expected_checkpoint_hash": first.checkpoint_hash,
    }


def test_get_workflow_state_uses_service_and_returns_reviewable_cursor() -> None:
    service = WorkflowApiService()
    run_id, _ = _resume_payload()
    with _client(service) as client:
        response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema"] == "zhimeng.agent-workflow-state.v1"
    assert payload["run_id"] == run_id
    assert payload["resume_state"] == "resumable"
    assert payload["resume_allowed"] is True
    assert payload["last_checkpoint_id"] == payload["checkpoints"][-1]["checkpoint_id"]
    assert service.workflow_calls == [run_id]


def test_resume_passes_the_strict_request_object_and_returns_202() -> None:
    service = WorkflowApiService()
    run_id, request = _resume_payload()
    with _client(service) as client:
        response = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            json=request,
        )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["schema"] == "zhimeng.agent-run-resume.v1"
    assert payload["accepted"] is True
    assert payload["next_action"] == "workflow_step"
    assert len(service.resume_calls) == 1
    called_run_id, called_request = service.resume_calls[0]
    assert called_run_id == run_id
    assert called_request == AgentRunResumeRequest.model_validate(request)


def test_resume_accepts_first_step_null_checkpoint_cas() -> None:
    service = WorkflowApiService()
    run_id, request = _resume_payload()
    request["expected_checkpoint_id"] = None
    request["expected_checkpoint_hash"] = None
    with _client(service) as client:
        response = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            json=request,
        )

    assert response.status_code == 202, response.text
    assert service.resume_calls[0][1].expected_checkpoint_id is None
    assert service.resume_calls[0][1].expected_checkpoint_hash is None


def test_resume_rejects_half_checkpoint_before_calling_service() -> None:
    service = WorkflowApiService()
    run_id, request = _resume_payload()
    request["expected_checkpoint_hash"] = None
    with _client(service) as client:
        response = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            json=request,
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert service.resume_calls == []


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("workflow_resume_stale", True),
        ("workflow_checkpoint_cas_failed", True),
        ("agent_run_not_paused", False),
        ("workflow_not_resumable", False),
        ("provider_dispatch_already_started", False),
        ("workflow_checkpoint_integrity_failed", False),
    ],
)
def test_workflow_conflict_codes_are_not_collapsed_to_agent_conflict(
    code: str,
    retryable: bool,
) -> None:
    service = WorkflowApiService()
    service.resume_error = AgentWorkflowCheckpointConflict(
        "Fixture workflow conflict",
        code=code,
    )
    run_id, request = _resume_payload()
    with _client(service) as client:
        response = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            json=request,
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable


def test_workflow_state_not_found_uses_existing_agent_404_mapping() -> None:
    service = WorkflowApiService()
    service.workflow_error = AgentStoreNotFound("Workflow Plan was not found")
    run_id, _ = _resume_payload()
    with _client(service) as client:
        response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "agent_resource_not_found"
