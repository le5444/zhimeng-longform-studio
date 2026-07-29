from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_agent_candidate_output import _completed_workflow, _persist_candidate_cutpoint
from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.execution_protocol import RunStatus


def _service_app(service: AgentService) -> FastAPI:
    app = FastAPI()
    app.include_router(create_agent_router(service, heartbeat_seconds=0.05))
    return app


def test_writer_trace_endpoint_returns_one_bound_pre_candidate_chain(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)
    service = AgentService(fixture.store, None)

    with TestClient(_service_app(service)) as client:
        response = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}/writer-trace"
        )

    assert response.status_code == 200, response.text
    trace = response.json()
    assert trace["schema"] == "zhimeng.writer-trace.v1"
    assert trace["run_id"] == fixture.run.run_id
    assert trace["workflow"]["provider_boundary"] is not None
    assert trace["writer_task"] is not None
    assert trace["activation_binding"] is not None
    assert trace["materialization"] is not None
    assert trace["dispatch_receipt"] is not None
    assert trace["candidate_output"] is None
    assert trace["writer_candidate_result"] is None
    assert trace["provider_recovery"] is None


def test_writer_trace_keeps_candidate_writer_result_and_run_completion_bound(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    run, _, _ = fixture.store.finalize_persisted_candidate(
        fixture.run.run_id,
        expected_candidate_output_id=candidate.candidate_output_id,
        expected_candidate_output_hash=candidate.envelope_hash,
        message_id="message_writer_trace_complete",
        usage=None,
    )
    writer_result = fixture.store.get_writer_candidate_result(fixture.run.run_id)

    trace = asyncio.run(AgentService(fixture.store, None).get_writer_trace(run.run_id))

    assert run.status is RunStatus.SUCCEEDED
    assert trace.workflow.run_status is RunStatus.SUCCEEDED
    assert trace.candidate_output == candidate
    assert trace.writer_candidate_result == writer_result
    assert trace.writer_task is not None
    assert writer_result.writer_task_id == trace.writer_task.writer_task_id
    assert trace.dispatch_receipt is not None
    assert candidate.instruction_dispatch_receipt_id == (
        trace.dispatch_receipt.dispatch_receipt_id
    )
