from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_command_sessions import command, until
from test_database import _create_session_and_run
from zhimeng_runtime.command_completion import CommandCompletionStore
from zhimeng_runtime.command_sessions import CommandSessionManager
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore


def setup(tmp_path):
    database = SqliteStore(tmp_path / "completion.sqlite3")
    database.initialize()
    _, session, run, agent = _create_session_and_run(database)
    return database, CommandCompletionStore(database), session, run


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


def fact(session, run, **updates):
    return dict(session_id="cmd_synthetic", owner_session_id=session.session_id, owner_run_id=run.run_id,
        status="exited", exit_code=0, ended_at=100.0, completion_version=1, completion_ready=True,
        completion_eligible=True, stop_generation=0, command="synthetic", cursor={"stdout": 5,"stderr": 0}, **updates)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_completion_is_durable_once_after_masked_output_drains(tmp_path, exit_code):
    database, store, session, run = setup(tmp_path)
    published = []
    def completed(payload):
        published.append(payload)
        text = Path(payload["stdout_file"]).read_text(encoding="utf-8")
        assert "last-line" in text
        assert "synthetic-private-secret" not in text
        store.record(payload)
    manager = CommandSessionManager(tmp_path / "commands", on_completion=completed)
    try:
        initial = manager.start(owner_session_id=session.session_id, owner_run_id=run.run_id,
            command=command(tmp_path, f"import time,sys\ntime.sleep(.2)\nprint('api_key=synthetic-private-secret')\nprint('last-line')\nsys.exit({exit_code})\n"),
            cwd=tmp_path, yield_time_ms=0)
        assert initial["status"] == "running" and initial["completion_eligible"]
        wait_until(lambda: len(store.pending(session.session_id)) == 1)
        manager.reconcile_completions()
        assert len(published) == 1
        event = store.pending(session.session_id)[0]
        assert event["exit_code"] == exit_code
        assert "stdout" not in event["payload"]
        assert event["payload"]["cursor"]["stdout"] == Path(event["payload"]["stdout_file"]).stat().st_size
        manager.list(session.session_id)
        manager.read_page(session.session_id, initial["session_id"])
        assert len(store.pending(session.session_id)) == 1
        store.mark_observed(manager.read(session.session_id, initial["session_id"]), run.run_id)
        assert not store.pending(session.session_id)
    finally:
        manager.close()
    restored = CommandSessionManager(tmp_path / "commands", on_completion=store.record)
    try:
        assert len(store.list_events(session.session_id)) == 1
        assert store.list_events(session.session_id)[0]["delivery_state"] == "delivered"
    finally:
        restored.close()


def test_completion_received_in_initial_tool_result_does_not_wake_again(tmp_path):
    _, store, session, run = setup(tmp_path)
    manager = CommandSessionManager(tmp_path / "commands", on_completion=store.record)
    try:
        payload = manager.start(owner_session_id=session.session_id, owner_run_id=run.run_id,
            command=command(tmp_path, "pass\n"), cwd=tmp_path, yield_time_ms=3000)
        assert payload["status"] == "exited" and not payload["completion_eligible"]
        wait_until(lambda: len(store.list_events(session.session_id)) == 1)
        assert store.pending(session.session_id) == []
    finally:
        manager.close()


def test_observed_before_callback_and_transaction_rollback(tmp_path):
    database, store, session, run = setup(tmp_path)
    payload = fact(session, run)
    store.mark_observed(payload, run.run_id)
    first = store.record(payload)
    assert first["delivery_state"] == "delivered"
    assert store.pending_session_ids() == []
    other = {**payload,"session_id":"cmd_transaction"}
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = store.record(other, connection=connection)
        store.mark_delivered([event["event_id"]], run.run_id, connection=connection)
        connection.rollback()
    assert len(store.list_events(session.session_id)) == 1


def test_failed_recovery_discovery_keeps_completion_pending_until_explicit_read(tmp_path):
    from zhimeng_runtime.agent_store import AgentStore
    from zhimeng_runtime.errors import ErrorDetail, ErrorEnvelope
    from zhimeng_runtime.execution_protocol import RunStatus

    database, completions, session, run = setup(tmp_path)
    agent = AgentStore(database)
    agent.transition_run(run.run_id, RunStatus.STARTING, detail=None)
    agent.transition_run(run.run_id, RunStatus.RUNNING, detail=None)
    payload = fact(session, run)
    completions.record(payload)
    agent.begin_tool_call(run.run_id, tool_call_id="missing-command", tool_name="command_session", tool_version="1",
                          arguments={"action": "read", "session_id": "verify"}, request_hash="a" * 64)
    agent.finish_tool_call(run.run_id, tool_call_id="missing-command", status="failed",
        output={"schema": "zhimeng.tool-result.command-session-recovery.v1", "sessions": [payload], "requested_session_id": "verify"},
        error=ErrorEnvelope(error=ErrorDetail(code="command_session_not_found", message="Missing command", request_id=run.run_id, retryable=False)), duration_ms=1)
    assert len(completions.pending(session.session_id)) == 1
    agent.begin_tool_call(run.run_id, tool_call_id="explicit-read", tool_name="command_session", tool_version="1",
                          arguments={"action": "read", "session_id": payload["session_id"]}, request_hash="b" * 64)
    agent.finish_tool_call(run.run_id, tool_call_id="explicit-read", status="succeeded",
        output={**payload, "schema": "zhimeng.tool-result.command-session.v1", "stdout": "done"}, error=None, duration_ms=1)
    assert completions.pending(session.session_id) == []


def test_stop_generation_suppresses_pending_and_late_old_results(tmp_path):
    database, store, session, run = setup(tmp_path)
    payload = fact(session, run)
    store.record(payload)
    with database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert store.bump_stop_generation(session.session_id, connection=connection) == 1
        connection.commit()
    store.record({**payload,"session_id":"cmd_late"})
    assert store.pending(session.session_id) == []
    assert all(event["delivery_state"] == "suppressed" for event in store.list_events(session.session_id))
    # Starting another conversation/run does not erase the old generation.
    assert store.current_generation(session.session_id) == 1
    assert store.run_generation(session.session_id, run.run_id) == 0


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "cancelling"])
def test_cancelled_owner_does_not_schedule_late_natural_exit(tmp_path, status):
    database, store, session, run = setup(tmp_path)
    with database.connection() as connection:
        connection.execute("UPDATE run SET status=? WHERE id=?", (status,run.run_id))
    assert store.record(fact(session, run))["delivery_state"] == "suppressed"


def test_failed_publication_recovers_and_old_history_does_not_flood(tmp_path):
    _, store, session, run = setup(tmp_path)
    attempts = []
    def unavailable(payload):
        attempts.append(payload)
        raise OSError("synthetic temporary storage error")
    manager = CommandSessionManager(tmp_path / "commands", on_completion=unavailable)
    try:
        payload = manager.start(owner_session_id=session.session_id,owner_run_id=run.run_id,
            command=command(tmp_path,"import time\ntime.sleep(.2)\nprint('ready')\n"),cwd=tmp_path,yield_time_ms=0)
        wait_until(lambda: bool(attempts))
    finally:
        manager.close()
    old = tmp_path / "commands" / "cmd_legacy"
    old.mkdir()
    for name in ("stdout.log","stderr.log"):
        (old/name).touch()
    legacy = {**attempts[0],"session_id":"cmd_legacy"}
    legacy.pop("completion_version")
    (old/"session.json").write_bytes(json.dumps(legacy).encode())
    restored = CommandSessionManager(tmp_path / "commands",on_completion=store.record)
    try:
        assert len(store.pending(session.session_id)) == 1
        assert store.list_events(session.session_id)[0]["command_session_id"] == payload["session_id"]
    finally:
        restored.close()


def test_interrupted_recovery_is_visible_without_touching_old_pid(tmp_path, monkeypatch):
    _, store, session, run = setup(tmp_path)
    folder = tmp_path / "commands" / "cmd_synthetic"
    folder.mkdir(parents=True)
    payload = {**fact(session, run), "schema":"zhimeng.command-session.v1", "status":"running", "pid":os.getpid(), "created_at":time.time()}
    (folder/"session.json").write_bytes(json.dumps(payload).encode())
    for name in ("stdout.log","stderr.log"):
        (folder/name).touch()
    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("recovery started a process"))
    manager = CommandSessionManager(tmp_path / "commands", on_completion=store.record)
    try:
        event = store.list_events(session.session_id)[0]
        assert event["status"] == "interrupted" and event["delivery_state"] == "suppressed"
    finally:
        manager.close()


def test_schema30_upgrade_preserves_run_and_adds_empty_completion_tables(tmp_path):
    database, store, session, run = setup(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE command_completion")
        connection.execute("DROP TABLE command_completion_scope")
        connection.execute("ALTER TABLE run DROP COLUMN origin_kind")
        connection.execute("ALTER TABLE run DROP COLUMN stop_generation")
        connection.execute("PRAGMA user_version=30")
    assert database.initialize().schema_version == SCHEMA_VERSION
    with database.connection() as connection:
        row = connection.execute("SELECT origin_kind,stop_generation FROM run WHERE id=?", (run.run_id,)).fetchone()
        assert tuple(row) == ("user",0)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.list_events(session.session_id) == []


def test_concurrent_publish_is_one_event_and_cursor_is_session_scoped(tmp_path):
    _, store, session, run = setup(tmp_path)
    payload = fact(session, run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.record(payload), range(12)))
    assert len({event["sequence"] for event in results}) == 1
    first = results[0]
    assert store.list_events("other-session") == []
    assert store.list_events(session.session_id, after=first["sequence"]) == []
    second = store.record({**payload, "session_id":"cmd_second"})
    assert [event["event_id"] for event in store.list_events(session.session_id, after=first["sequence"])] == [second["event_id"]]


def test_wrong_conversation_cannot_consume_or_replace_a_completion(tmp_path):
    database, store, session, run = setup(tmp_path)
    payload = fact(session, run)
    store.record(payload)
    with pytest.raises(ValueError, match="归属"):
        store.record({**payload,"owner_session_id":"somewhere-else"})
    with pytest.raises(ValueError, match="不存在"):
        store.mark_delivered(["command-completion:cmd_synthetic"], "wrong-run")
    assert len(store.pending(session.session_id)) == 1


def test_shutdown_records_cancelled_without_automatic_delivery(tmp_path):
    _, store, session, run = setup(tmp_path)
    manager = CommandSessionManager(tmp_path / "commands",on_completion=store.record)
    manager.start(owner_session_id=session.session_id,owner_run_id=run.run_id,
        command=command(tmp_path,"import time\ntime.sleep(60)\n"),cwd=tmp_path,yield_time_ms=0)
    manager.close()
    wait_until(lambda: bool(store.list_events(session.session_id)))
    assert store.list_events(session.session_id)[0]["status"] == "cancelled"
    assert store.pending(session.session_id) == []


def test_completion_project_observation_uses_latest_project_revision_not_event_time(tmp_path):
    from zhimeng_runtime.database import latest_own_write_pin
    database, _, _, run = setup(tmp_path)
    with database.connection() as connection:
        for identifier, revision, created_at in [("new-project-state", 3, "2026-10-12T01:00:00Z"), ("late-old-observation", 2, "2026-10-12T02:00:00Z")]:
            connection.execute("INSERT INTO artifact(id,run_id,artifact_type,content_hash,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                (identifier,run.run_id,"command_completion_observation","a"*64,
                 json.dumps({"event_ids":["synthetic"],"result_revision":revision,"result_fingerprint":str(revision)*64}),created_at))
        assert latest_own_write_pin(connection,run.run_id) == (3,"3"*64)


def test_automatic_admission_error_stops_poll_retry_but_user_can_consume(tmp_path):
    _, store, session, run = setup(tmp_path)
    event = store.record(fact(session,run))
    store.defer_automatic([event["event_id"]],"api_key=synthetic-private-credential model unavailable")
    assert store.pending_session_ids() == []
    pending = store.pending(session.session_id)
    assert len(pending) == 1
    assert "synthetic-private-credential" not in pending[0]["payload"]["automatic_delivery_error"]
    store.mark_delivered([event["event_id"]],run.run_id)
    visible = store.list_events(session.session_id)[0]
    assert visible["delivery_state"] == "delivered"
    assert "automatic_delivery_error" not in visible["payload"]
