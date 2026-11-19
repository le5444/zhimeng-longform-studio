from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_agent_store import (
    FINGERPRINT, PROJECT_ID, begin_tool_call, create_run, create_session,
    initialized_store, start_run,
)
from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_models import AgentRunContinuationResponse, AgentRunCreateRequest
from zhimeng_runtime.agent_store import AgentStore, AgentStoreConflict


def stopped_run(tmp_path):
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    store.mark_interrupted(run.run_id, detail="Runtime stopped")
    return store, session, run


def continue_run(store, session_id, run_id, *, key="continue-0001"):
    return store.create_run(
        session_id=session_id, message="接着完成剩余任务", expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT, active_chapter_id=None,
        idempotency_key=key, continuation_source_run_id=run_id,
    )


def test_continuation_is_durable_and_idempotent_and_only_one_child(tmp_path):
    store, session, source = stopped_run(tmp_path)
    assert store.get_run_continuation(source.run_id).can_continue
    child, _, replayed = continue_run(store, session.session_id, source.run_id)
    assert not replayed
    replay, _, replayed = continue_run(store, session.session_id, source.run_id)
    assert replayed and replay.run_id == child.run_id
    reloaded = AgentStore(store.database)
    state = reloaded.get_run_continuation(source.run_id)
    assert not state.can_continue and state.reason_code == "already_continued"
    assert state.continued_by_run_id == child.run_id
    assert source.objective in reloaded.continuation_provider_message(child.run_id)["content"]
    with pytest.raises(AgentStoreConflict):
        continue_run(store, session.session_id, source.run_id, key="different-0002")
    assert len(store.list_runs(session.session_id)) == 2


def test_concurrent_continuations_create_exactly_one_child(tmp_path):
    store, session, source = stopped_run(tmp_path)
    def attempt(index):
        try:
            return continue_run(store, session.session_id, source.run_id, key=f"continue-{index:04}")[0].run_id
        except AgentStoreConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert store.get_run_continuation(source.run_id).continued_by_run_id in results


def test_only_latest_same_session_and_project_can_continue(tmp_path):
    store, session, source = stopped_run(tmp_path)
    another_session = create_session(store)
    with pytest.raises(AgentStoreConflict):
        continue_run(store, another_session.session_id, source.run_id)
    store.rebind_session(session.session_id, project_id=None)
    assert store.get_run_continuation(source.run_id).reason_code == "project_rebound"
    with pytest.raises(AgentStoreConflict):
        continue_run(store, session.session_id, source.run_id)
    store.rebind_session(session.session_id, project_id=PROJECT_ID)
    newer, _, _ = create_run(store, session.session_id, key="normal-next-0002")
    assert store.get_run_continuation(source.run_id).reason_code == "not_latest"
    assert store.get_run_continuation(newer.run_id).reason_code == "run_active"


def test_success_only_continues_when_budget_stop_was_recorded_and_preserves_root(tmp_path):
    store, session, source = stopped_run(tmp_path)
    child, _, _ = continue_run(store, session.session_id, source.run_id)
    start_run(store, child.run_id)
    store.complete_run(child.run_id, content="先完成了一部分。", message_id="message-final", finish_reason="stop", usage=None)
    assert store.get_run_continuation(child.run_id).reason_code == "already_completed"
    store.append_run_event(child.run_id, "run_limit_reached", {"reason": "tool_calls", "detail": "budget"})
    assert store.get_run_continuation(child.run_id).stop_reason == "tool_calls"
    grandchild, _, _ = continue_run(store, session.session_id, child.run_id, key="continue-0002")
    state = store.get_run_continuation(grandchild.run_id)
    assert state.objective == source.objective
    assert store.get_run_continuation(source.run_id).continued_by_run_id == child.run_id


def test_managed_runs_cannot_be_continued_with_plain_agent(tmp_path):
    store, session, source = stopped_run(tmp_path)
    with store.database.connection() as connection:
        connection.execute("UPDATE run SET run_type = 'conversation_fork' WHERE id = ?", (source.run_id,))
        connection.commit()
    assert store.get_run_continuation(source.run_id).reason_code == "managed_run"
    with pytest.raises(AgentStoreConflict):
        continue_run(store, session.session_id, source.run_id)


def test_handoff_uses_real_tool_results_and_current_file_change_status(tmp_path):
    store = initialized_store(tmp_path)
    session = create_session(store)
    source, _, _ = create_run(store, session.session_id)
    start_run(store, source.run_id)
    begin_tool_call(store, source.run_id, tool_call_id="tool-read", tool_name="project_read_file", idempotency_key="read-0001")
    store.finish_tool_call(source.run_id, tool_call_id="tool-read", status="succeeded", output={"text": "已读取的真实内容"}, error=None, duration_ms=2)
    begin_tool_call(store, source.run_id, tool_call_id="tool-command", tool_name="computer_run_command", idempotency_key="cmd-0001")
    store.mark_interrupted(source.run_id, detail="Interrupted while command might have changed files")
    # The persisted command cancellation is not proof that its side effects never happened.
    state = store.get_run_continuation(source.run_id)
    assert state.completed_tool_count == 1
    assert state.uncertain_tool_count == 1
    now = source.created_at.isoformat()
    with store.database.connection() as connection:
        connection.execute(
            "INSERT INTO change_set(id,project_binding_id,status,base_revision,base_fingerprint,review_revision,payload_json,created_at,updated_at) VALUES(?,?,'applied',1,?,0,?,?,?)",
            ("change-continuation", PROJECT_ID, FINGERPRINT, json.dumps({"source_run_id": source.run_id, "changes": [{"relative_path": "notes.md"}]}), now, now),
        )
        connection.commit()
    store.append_run_event(source.run_id, "tool_batch_deferred", {"calls": [{"tool_name": "project_write_file", "arguments": {"relative_path": "later.md"}}], "reason": "tool_calls"})
    child, _, _ = continue_run(store, session.session_id, source.run_id)
    with store.database.connection() as connection:
        connection.execute("UPDATE change_set SET status='rolled_back' WHERE id='change-continuation'")
        connection.commit()
    message = store.continuation_provider_message(child.run_id)["content"]
    assert "已读取的真实内容" in message and '"state":"uncertain"' in message
    assert "notes.md" in message and "rolled_back" in message and "later.md" in message
    assert "先查证而不是直接重跑" in message
    assert store.list_tool_calls(child.run_id) == []


def test_handoff_budget_never_hides_side_effect_targets_and_blocks_when_too_large(tmp_path):
    from zhimeng_runtime.agent_continuation import MAX_HANDOFF_CHARS, HandoffTooLarge, provider_message
    record = {"objective": "继续", "tools": [
        {"run_id": "old", "tool_call_id": f"tool-{i}", "tool_name": "computer_run_command",
         "state": "uncertain", "side_effects_possible": True, "target": {"command": "python task.py", "workdir": f"C:/books/book-{i}"}, "result": {"exit_code": 1}}
        for i in range(300)
    ]}
    with pytest.raises(HandoffTooLarge):
        provider_message(record)
    record["tools"] = record["tools"][:12]
    message = provider_message(record)["content"]
    assert len(message) <= MAX_HANDOFF_CHARS
    assert all(f"C:/books/book-{i}" in message for i in range(12))
    store, session, source = stopped_run(tmp_path)
    # Simulate many prior side-effect records through a durable chain event; the
    # state and atomic create path must both reject rather than omit identities.
    with store.database.connection() as connection:
        for i in range(150):
            connection.execute("INSERT INTO change_set(id,project_binding_id,status,base_revision,base_fingerprint,review_revision,payload_json,created_at,updated_at) VALUES(?,?,'applied',1,?,0,?,?,?)", (
                f"changes-{i}", PROJECT_ID, FINGERPRINT, json.dumps({"source_run_id": source.run_id, "changes": [{"relative_path": f"{i}-" + "a" * 300 + ".md"}]}), source.created_at.isoformat(), source.created_at.isoformat(),
            ))
        connection.commit()
    assert store.get_run_continuation(source.run_id).reason_code == "handoff_too_large"
    with pytest.raises(AgentStoreConflict, match="操作记录过长"):
        continue_run(store, session.session_id, source.run_id)
    assert len(store.list_runs(session.session_id)) == 1


def test_command_targets_include_workdir_and_signature_and_scalar_read_result():
    from zhimeng_runtime.agent_continuation import _brief_tool
    row = {"run_id": "old", "id": "call", "tool_name": "computer_run_command", "status": "succeeded", "result_hash": "a" * 64, "error_json": None}
    first = _brief_tool({**row, "input_json": json.dumps({"command": "python task.py", "workdir": "C:/books/a"}), "output_json": json.dumps({"exit_code": 1, "workdir": "C:/books/a"})})
    second = _brief_tool({**row, "input_json": json.dumps({"command": "python task.py", "workdir": "C:/books/b"}), "output_json": json.dumps({"exit_code": 0, "workdir": "C:/books/b"})})
    assert first["target"]["workdir"] != second["target"]["workdir"]
    assert first["target"]["command_hash"] == second["target"]["command_hash"]
    assert first["state"] == "uncertain"
    read = _brief_tool({**row, "tool_name": "project_read_file", "input_json": "{}", "output_json": json.dumps("real scalar evidence")})
    assert "real scalar evidence" in read["result"]["result_excerpt"]


def test_continuation_api_reads_state_and_forwards_source(tmp_path):
    store, session, source = stopped_run(tmp_path)
    received = {}
    async def start_run(**kwargs):
        received.update(kwargs)
        # 真 AgentService 会把 manual_skill_names 消费在服务层（按 run 暂存），
        # 不落 store；这个假服务照做，免得 store.create_run 收到不认识的参数。
        kwargs.pop("manual_skill_names", None)
        run, user_message, replayed = store.create_run(**kwargs)
        return SimpleNamespace(run=run, user_message=user_message, replayed=replayed)
    app = FastAPI()
    app.include_router(create_agent_router(SimpleNamespace(store=store, start_run=start_run), heartbeat_seconds=1))
    with TestClient(app) as client:
        state = client.get(f"/api/v1/agent/runs/{source.run_id}/continuation")
        assert state.status_code == 200 and state.json()["can_continue"]
        assert state.json()["schema"] == "zhimeng.agent-run-continuation.v1"
        response = client.post(f"/api/v1/agent/sessions/{session.session_id}/runs", headers={"Idempotency-Key": "api-continue-0001"}, json={
            "message": "继续", "continuation_source_run_id": source.run_id,
            "expected_project_revision": 1, "expected_project_fingerprint": FINGERPRINT,
        })
        assert response.status_code == 202, response.text
        assert received["continuation_source_run_id"] == source.run_id
        linked = client.get(f"/api/v1/agent/runs/{source.run_id}/continuation").json()
        assert linked["continued_by_run_id"] == response.json()["run"]["run_id"]


@pytest.mark.parametrize("filename,model", [
    ("agent-run-create-request.schema.json", AgentRunCreateRequest),
    ("agent-run-continuation.schema.json", AgentRunContinuationResponse),
])
def test_continuation_contracts_match_models(filename, model):
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "packages/contracts" / filename).read_text(encoding="utf-8"))
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.pop("$id") == f"https://zhimeng.local/contracts/{filename}"
    assert schema == model.model_json_schema(by_alias=True)
