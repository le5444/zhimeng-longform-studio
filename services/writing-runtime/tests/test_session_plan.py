from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_session_recall import history
from test_agent_parallel_tools import TurnProvider, tool_batch_turn, final_turn, tool_messages_in_order
from test_agent_service import wait_terminal
from test_memory_permissions import _harness
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.session_plan import PlanReadArguments, SessionPlanResponse, read_session_plan
from zhimeng_runtime.session_plan_api import create_session_plan_router


ITEMS = [{"content": "Check source CSV", "status": "completed"},
         {"content": "Save and verify the complete table", "status": "in_progress"}]


def update(history, items=ITEMS):
    service, _, _, _, _, run, _ = history
    return asyncio.run(service._execute_tool_call(run, provider_tool_call_id=uuid4().hex,
        tool_name="plan_update", arguments={"items": items}, cancel_event=asyncio.Event())).tool_call


def view(history):
    service, _, _, _, current, *_ = history
    return read_session_plan(service.store.database, current.session_id)


@pytest.fixture
def client(history):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_session_plan_router(history[0]))
    with TestClient(app) as client:
        yield client


def test_empty_then_saved_plan_uses_actual_success_result(history, client):
    assert view(history).plan is None
    saved = update(history)
    service, _, _, _, current, run, _ = history
    response = client.get(f"/api/v1/agent/sessions/{current.session_id}/plan")
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["items"] == ITEMS
    assert plan["run_id"] == run.run_id and plan["tool_call_id"] == saved.tool_call_id
    assert plan["status_source"] == "agent" and plan["source"] == "tool_result"
    with service.store.database.connection() as connection:
        connection.execute("UPDATE tool_call SET input_json = ? WHERE id = ?",
            (json.dumps({"items": [{"content": "Wrong input", "status": "completed"}]}), saved.tool_call_id))
    assert view(history).plan.items[0].content == ITEMS[0]["content"]


def test_failed_cancelled_and_pending_updates_do_not_replace_confirmed_plan(history):
    saved = update(history)
    assert update(history, []).status == "failed"
    service, _, _, _, _, run, _ = history
    for status in ("cancelled", None):
        call_id = uuid4().hex
        service.store.begin_tool_call(run.run_id, tool_call_id=call_id, tool_name="plan_update", tool_version="1",
            arguments={"items": [{"content": "Unconfirmed", "status": "completed"}]}, request_hash="a" * 64)
        if status:
            service.store.finish_tool_call(run.run_id, tool_call_id=call_id, status=status, output=None, error=None, duration_ms=1)
    assert view(history).plan.tool_call_id == saved.tool_call_id


def test_plan_survives_checkpoint_reopen_and_other_session(history):
    update(history)
    service, binding, session, turn, current, run, _ = history
    service.store.complete_run(run.run_id, message_id=uuid4().hex, content="Continue later.", finish_reason="stop", usage=None)
    turn(current, "A later ordinary message.")
    service.store.append_session_checkpoint(current.session_id, run_id=None, through_ordinal=2,
        source_message_count=2, content="Short summary without the checklist")
    other = session(binding.project_id)
    assert read_session_plan(service.store.database, other.session_id).plan is None
    from zhimeng_runtime.database import SqliteStore
    reopened = read_session_plan(SqliteStore(service.store.database.path), current.session_id)
    assert reopened.plan.run_id == run.run_id
    assert reopened.plan.items[1].status == "in_progress"


@pytest.mark.parametrize("raw", ["{bad", '{"items":[]}', '{"items":[{"content":"x","status":"unknown"}]}'])
def test_corrupt_confirmed_output_is_reported_not_replaced_by_input_or_older_plan(history, client, raw):
    update(history)
    saved = update(history, [{"content": "New plan", "status": "pending"}])
    with history[0].store.database.connection() as connection:
        connection.execute("UPDATE tool_call SET output_json = ? WHERE id = ?", (raw, saved.tool_call_id))
    response = client.get(f"/api/v1/agent/sessions/{history[4].session_id}/plan")
    assert response.status_code == 409


def test_legacy_success_is_identified_and_secret_is_masked(history, client):
    secret = "sk-" + "A" * 32
    saved = update(history, [{"content": "Check " + secret, "status": "pending"}])
    with history[0].store.database.connection() as connection:
        connection.execute("UPDATE tool_call SET output_json = NULL WHERE id = ?", (saved.tool_call_id,))
    result = view(history)
    assert result.plan.source == "legacy_arguments"
    assert secret not in result.model_dump_json()
    assert client.get("/api/v1/agent/sessions/missing/plan").status_code == 404


def test_plan_tool_update_then_read_in_one_batch_and_new_turn(tmp_path):
    async def scenario():
        service, _, _, folder = _harness(tmp_path)
        provider = TurnProvider([tool_batch_turn([
            ("update", "plan_update", {"items": ITEMS}), ("read", "plan_read", {}),
        ]), final_turn("Plan recorded."), tool_batch_turn([("read-next", "plan_read", {})]), final_turn("Plan recovered.")])
        service.provider = provider
        session = await service.create_session(project_id=None, title="Document work")
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        try:
            for index in range(2):
                started = await service.start_run(session_id=session.session_id, message="Continue document work.",
                    expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
                    writer_mode="repair_only", idempotency_key=f"plan-read-turn-{index}")
                assert (await wait_terminal(service, started.run.run_id)).status == RunStatus.SUCCEEDED
                calls = service.store.list_tool_calls(started.run.run_id)
                read = next(call for call in calls if call.tool_name == "plan_read")
                assert read.status == "succeeded" and read.output["plan"]["items"] == ITEMS
            assert tool_messages_in_order(provider)
            assert before == {p.name: p.read_bytes() for p in folder.iterdir()}
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_contracts_match_current_models():
    root = Path(__file__).resolve().parents[3] / "packages/contracts"
    for name, model in (("plan-read-arguments.schema.json", PlanReadArguments), ("session-plan-response.schema.json", SessionPlanResponse)):
        saved = json.loads((root / name).read_text(encoding="utf-8"))
        saved.pop("$schema"); saved.pop("$id")
        assert saved == model.model_json_schema(by_alias=True)
