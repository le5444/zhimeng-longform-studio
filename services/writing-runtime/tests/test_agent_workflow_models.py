from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_execution_protocol_contracts import _entry_example
from test_workflow_contracts import _boundary, _completed_chain
from zhimeng_runtime.agent_models import (
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentWorkflowStateResponse,
)
from zhimeng_runtime.execution_protocol import Run, RunStatus


def _run() -> Run:
    return Run.model_validate(_entry_example("execution-run.schema.json"))


def test_workflow_state_checkpoint_cursor_is_paired() -> None:
    plan, first, _ = _completed_chain()
    state = AgentWorkflowStateResponse(
        run_id=plan.run_id,
        run_status=RunStatus.INTERRUPTED,
        plan=plan,
        checkpoints=(first,),
        provider_boundary=None,
        last_checkpoint_id=first.checkpoint_id,
        last_checkpoint_hash=first.checkpoint_hash,
        next_step_ordinal=2,
        resume_state="resumable",
        resume_allowed=True,
        reason_code="workflow_step_interrupted",
        formal_draft_allowed=False,
    )
    assert state.last_checkpoint_id == first.checkpoint_id

    payload = state.model_dump(mode="python", by_alias=True)
    payload["last_checkpoint_hash"] = None
    with pytest.raises(ValidationError, match="identity and hash must be paired"):
        AgentWorkflowStateResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("resume_state", "resume_allowed"),
    [
        ("running", False),
        ("resumable", True),
        ("blocked", False),
        ("provider_dispatch_uncertain", False),
        ("terminal", False),
    ],
)
def test_workflow_state_resume_allowed_is_derived(
    resume_state: str,
    resume_allowed: bool,
) -> None:
    plan, _, _ = _completed_chain()
    value = AgentWorkflowStateResponse(
        run_id=plan.run_id,
        run_status=RunStatus.INTERRUPTED,
        plan=plan,
        checkpoints=(),
        provider_boundary=None,
        last_checkpoint_id=None,
        last_checkpoint_hash=None,
        next_step_ordinal=1,
        resume_state=resume_state,
        resume_allowed=resume_allowed,
        reason_code="fixture_state",
        formal_draft_allowed=False,
    )
    assert value.resume_allowed is (resume_state == "resumable")

    payload = value.model_dump(mode="python", by_alias=True)
    payload["resume_allowed"] = not resume_allowed
    with pytest.raises(ValidationError, match="derived workflow state"):
        AgentWorkflowStateResponse.model_validate(payload)


def test_provider_boundary_forbids_resume() -> None:
    plan, first, final = _completed_chain()
    boundary = _boundary()
    closed = AgentWorkflowStateResponse(
        run_id=plan.run_id,
        run_status=RunStatus.INTERRUPTED,
        plan=plan,
        checkpoints=(first, final),
        provider_boundary=boundary,
        last_checkpoint_id=final.checkpoint_id,
        last_checkpoint_hash=final.checkpoint_hash,
        next_step_ordinal=None,
        resume_state="provider_dispatch_uncertain",
        resume_allowed=False,
        reason_code="provider_dispatch_already_recorded",
        formal_draft_allowed=False,
    )
    assert closed.resume_allowed is False

    payload = closed.model_dump(mode="python", by_alias=True)
    payload["resume_state"] = "resumable"
    payload["resume_allowed"] = True
    with pytest.raises(ValidationError, match="Provider boundary cannot be resumed"):
        AgentWorkflowStateResponse.model_validate(payload)


def test_resume_request_allows_first_step_null_cas_and_rejects_half_cursor() -> None:
    plan, first, _ = _completed_chain()
    first_step = AgentRunResumeRequest(
        expected_workflow_plan_id=plan.workflow_plan_id,
        expected_plan_hash=plan.plan_hash,
        expected_checkpoint_id=None,
        expected_checkpoint_hash=None,
    )
    assert first_step.expected_checkpoint_id is None
    assert first_step.expected_checkpoint_hash is None

    after_first = AgentRunResumeRequest(
        expected_workflow_plan_id=plan.workflow_plan_id,
        expected_plan_hash=plan.plan_hash,
        expected_checkpoint_id=first.checkpoint_id,
        expected_checkpoint_hash=first.checkpoint_hash,
    )
    payload = after_first.model_dump(mode="python", by_alias=True)
    payload["expected_checkpoint_hash"] = None
    with pytest.raises(ValidationError, match="identity and hash must be paired"):
        AgentRunResumeRequest.model_validate(payload)


def test_resume_response_checkpoint_cursor_is_paired() -> None:
    plan, first, _ = _completed_chain()
    first_step = AgentRunResumeResponse(
        run=_run(),
        accepted=True,
        workflow_plan_id=plan.workflow_plan_id,
        resumed_from_checkpoint_id=None,
        resumed_from_checkpoint_hash=None,
        next_action="workflow_step",
    )
    assert first_step.resumed_from_checkpoint_id is None

    after_first = AgentRunResumeResponse(
        run=_run(),
        accepted=True,
        workflow_plan_id=plan.workflow_plan_id,
        resumed_from_checkpoint_id=first.checkpoint_id,
        resumed_from_checkpoint_hash=first.checkpoint_hash,
        next_action="provider_io",
    )
    payload = after_first.model_dump(mode="python", by_alias=True)
    payload["resumed_from_checkpoint_id"] = None
    with pytest.raises(ValidationError, match="identity and hash must be paired"):
        AgentRunResumeResponse.model_validate(payload)
