from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session, enqueue
from test_agent_queue_scheduler import eventually, scheduler
from test_command_session_tools import script_command, wait_idle
from zhimeng_runtime.agent_provider import AgentProviderError, ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app
from zhimeng_runtime.command_completion import CommandCompletionStore
from zhimeng_runtime.agent_service import AgentUnavailableError


def facts(messages):
    result = []
    for item in messages:
        content = item.get("content")
        if isinstance(content, str) and content.startswith("后台命令的真实完成结果"):
            result.append(json.loads(content.split("\n", 1)[1]))
    return result


class CompletionProvider(ApiProvider):
    def __init__(self, command, target, *, fail_completion=False, hold_first=False):
        self.command = command
        self.target = target
        self.fail_completion = fail_completion
        self.hold_first = hold_first
        self.first_running = threading.Event()
        self.release_first = threading.Event()
        self.requests = []
        self.started_command = False
        self.completion_calls = 0

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.requests.append([dict(item) for item in messages])
        available_facts = facts(messages)
        if not self.started_command:
            self.started_command = True
            yield ProviderToolCall(0, "background-launch", "computer_run_command", {"command": self.command, "yield_time_ms": 100})
            yield ProviderCompleted("tool_calls", None)
            return
        if not available_facts:
            self.first_running.set()
            while self.hold_first and not self.release_first.is_set():
                if cancel_event.is_set():
                    return
                await asyncio.sleep(0.01)
            yield ProviderTextDelta("命令已在后台运行。")
            yield ProviderCompleted("stop", None)
            return
        self.completion_calls += 1
        if self.fail_completion:
            raise AgentProviderError("synthetic provider account unavailable", code="fixture_unavailable", retryable=False)
        read_done = any(item.get("role") == "assistant" and any(call.get("function", {}).get("name") == "read_file"
            for call in item.get("tool_calls", [])) for item in messages)
        if not read_done:
            yield ProviderToolCall(0, "background-readback", "read_file", {"path": str(self.target)})
            yield ProviderCompleted("tool_calls", None)
            return
        assert self.target.read_text(encoding="utf-8") == "# 汇总\n设计：30\n工程：50\n"
        yield ProviderTextDelta("后台统计已完成，读回确认设计 30、工程 50。")
        yield ProviderCompleted("stop", None)


def command_fixture(tmp_path):
    target = tmp_path / "summary.md"
    gate = tmp_path / "release-command"
    (tmp_path / "sample.csv").write_bytes("department,amount\n设计,10\n设计,20\n工程,50\n".encode())
    command = script_command(tmp_path,
        "from pathlib import Path\nimport csv,time\n"
        f"gate=Path({str(gate)!r})\n"
        "while not gate.exists(): time.sleep(0.01)\n"
        f"rows=list(csv.DictReader(Path({str(tmp_path / 'sample.csv')!r}).read_text(encoding='utf-8').splitlines()))\n"
        "totals={}\nfor row in rows: totals[row['department']]=totals.get(row['department'],0)+int(row['amount'])\n"
        f"Path({str(target)!r}).write_text('# 汇总\\n'+''.join(k+'：'+str(v)+'\\n' for k,v in totals.items()),encoding='utf-8')\n"
        "print('completed',flush=True)\n")
    return command, target, gate


def start(client, sid, key="background-first"):
    response = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": key},
        json={"message": "运行脚本统计 CSV，结束后读回汇总并告诉我结果。", "writer_mode": "author_review_change_proposal"})
    assert response.status_code == 202, response.text
    return response.json()["run"]["run_id"]


def test_idle_natural_completion_starts_internal_run_without_second_user_message(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        initial = client.get(f"/api/v1/agent/sessions/{sid}/background-events")
        assert initial.status_code == 200 and initial.json()["has_running_commands"] is True
        gate.write_bytes(b"go")
        store = client.app.state.agent_service.store
        # No HTTP/UI/event stream calls drive automatic dispatch from here.
        eventually(lambda: len(store.list_runs(sid)) == 2 and all(run.status == "succeeded" for run in store.list_runs(sid)), timeout=20)
        runs = store.list_runs(sid)
        assert [run.origin_kind for run in runs] == ["user", "command_completion"]
        assert [item.role for item in store.list_messages(sid)] == ["user", "assistant", "assistant"]
        assert len([item for run in runs for item in store.list_tool_calls(run.run_id) if item.tool_name == "computer_run_command"]) == 1
        background = client.get(f"/api/v1/agent/sessions/{sid}/background-events").json()
        assert len(background["events"]) == 1 and background["events"][0]["delivery_state"] == "delivered"
        assert background["events"][0]["delivered_run_id"] == runs[-1].run_id
        assert background["has_running_commands"] is False and background["pending_count"] == 0
        assert client.get(f"/api/v1/agent/sessions/{sid}/background-events", params={"after": background["next_cursor"]}).json()["events"] == []
        messages = client.get(f"/api/v1/agent/sessions/{sid}/messages").json()
        assert messages["run_origins"][runs[-1].run_id] == "command_completion"
        history = store.provider_history(sid)
        assert len(facts(history)) == 1
        assert len([item for item in history if item.get("role") == "user"]) == 1
        assert store.completed_history(sid)[1][-1].role == "assistant"
        continuation = client.get(f"/api/v1/agent/runs/{runs[-1].run_id}/continuation").json()
        assert continuation["can_continue"] is True
        assert client.get("/api/v1/agent/sessions/session-missing/background-events").status_code == 404
        assert client.get("/openapi.json").status_code == 200


def test_active_completion_enters_same_run_at_reply_boundary(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target, hold_first=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        assert provider.first_running.wait(10)
        gate.write_bytes(b"go")
        completions = CommandCompletionStore(client.app.state.agent_service.store.database)
        eventually(lambda: completions.pending(sid))
        provider.release_first.set()
        wait_for_success(client, rid)
        assert len(client.app.state.agent_service.store.list_runs(sid)) == 1
        event = completions.list_events(sid)[0]
        assert event["delivered_run_id"] == rid
        assert provider.completion_calls >= 1


def test_stopping_user_run_suppresses_exit_race(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target, hold_first=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        assert provider.first_running.wait(10)
        gate.write_bytes(b"go")
        completion_store = CommandCompletionStore(client.app.state.agent_service.store.database)
        eventually(lambda: completion_store.pending(sid))
        assert client.post(f"/api/v1/agent/runs/{rid}/cancel").status_code == 200
        eventually(lambda: client.app.state.agent_service.store.get_run(rid).status == "cancelled")
        wait_idle(client, rid)
        client.portal.call(worker.tick)
        assert len(client.app.state.agent_service.store.list_runs(sid)) == 1
        assert completion_store.list_events(sid)[0]["delivery_state"] == "suppressed"
        assert provider.completion_calls == 0


def test_completion_provider_failure_preserves_fact_for_later_history(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target, fail_completion=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        gate.write_bytes(b"go")
        store = client.app.state.agent_service.store
        eventually(lambda: len(store.list_runs(sid)) == 2 and store.list_runs(sid)[-1].status == "failed", timeout=20)
        assert len(facts(store.provider_history(sid))) == 1
        assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 1
        assert target.is_file()
        assert len([item for run in store.list_runs(sid) for item in store.list_tool_calls(run.run_id) if item.tool_name == "computer_run_command"]) == 1


def test_queued_user_turn_takes_priority_and_command_panel_does_not_consume_result(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        enqueue(client, sid, "user-priority-command", "结合已完成的结果，回答我这个新问题。")
        gate.write_bytes(b"go")
        completion_store = CommandCompletionStore(client.app.state.agent_service.store.database)
        eventually(lambda: completion_store.pending(sid))
        event = completion_store.pending(sid)[0]
        assert client.get(f"/api/v1/agent/sessions/{sid}/commands").status_code == 200
        assert client.get(f"/api/v1/agent/sessions/{sid}/commands/{event['command_session_id']}").status_code == 200
        assert client.get(f"/api/v1/agent/sessions/{sid}/background-events").json()["pending_count"] == 1
        client.portal.call(worker.start)
        store = client.app.state.agent_service.store
        eventually(lambda: len(store.list_runs(sid)) == 2 and store.list_runs(sid)[-1].status == "succeeded", timeout=20)
        assert all(run.origin_kind == "user" for run in store.list_runs(sid))
        assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 2
        assert completion_store.list_events(sid)[0]["delivered_run_id"] == store.list_runs(sid)[-1].run_id


def test_pending_completion_survives_restart_without_restarting_command(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        client.portal.call(scheduler(client).stop)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        gate.write_bytes(b"go")
        completion_store = CommandCompletionStore(client.app.state.agent_service.store.database)
        eventually(lambda: completion_store.pending(sid))
    resumed = CompletionProvider(command, target)
    resumed.started_command = True
    with TestClient(create_app(settings, agent_provider=resumed)) as client:
        scheduler(client)
        store = client.app.state.agent_service.store
        eventually(lambda: len(store.list_runs(sid)) == 2 and store.list_runs(sid)[-1].status == "succeeded", timeout=20)
        assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 1
        assert len([item for run in store.list_runs(sid) for item in store.list_tool_calls(run.run_id) if item.tool_name == "computer_run_command"]) == 1


def test_claimed_internal_run_interruption_keeps_fact_and_explicit_continuation(settings, tmp_path, monkeypatch):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        client.portal.call(scheduler(client).stop)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        gate.write_bytes(b"go")
        service = client.app.state.agent_service
        completion_store = CommandCompletionStore(service.store.database)
        eventually(lambda: completion_store.pending(sid))

        async def persist_only(run_id):
            return service.store.get_run(run_id)

        monkeypatch.setattr(service, "_finalize_run_start", persist_only)
        accepted = client.portal.call(service.start_completion_run, sid)
        assert accepted.origin_kind == "command_completion" and accepted.status == "queued"
        internal_id = accepted.run_id
        assert completion_store.list_events(sid)[0]["delivery_state"] == "delivered"
    resumed = CompletionProvider(command, target)
    resumed.started_command = True
    with TestClient(create_app(settings, agent_provider=resumed)) as client:
        store = client.app.state.agent_service.store
        assert store.get_run(internal_id).status == "interrupted"
        assert len(facts(store.provider_history(sid))) == 1
        assert client.get(f"/api/v1/agent/runs/{internal_id}/continuation").json()["can_continue"] is True
        response = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": "resume-completion-fact"},
            json={"message": "继续核对这次命令的结果。", "writer_mode": "author_review_change_proposal", "continuation_source_run_id": internal_id})
        assert response.status_code == 202, response.text
        wait_for_success(client, response.json()["run"]["run_id"])
        assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 2
        assert len([item for run in store.list_runs(sid) for item in store.list_tool_calls(run.run_id) if item.tool_name == "computer_run_command"]) == 1


def test_internal_assistant_history_forks_without_user_fabrication(settings, tmp_path, monkeypatch):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        gate.write_bytes(b"go")
        store = client.app.state.agent_service.store
        eventually(lambda: len(store.list_runs(sid)) == 2 and store.list_runs(sid)[-1].status == "succeeded", timeout=20)
        wait_idle(client, store.list_runs(sid)[-1].run_id)
        last = store.list_messages(sid)[-1]
        response = client.post(f"/api/v1/agent/sessions/{sid}/fork", json={"up_to_message_id": last.message_id})
        assert response.status_code == 201, response.text
        forked = response.json()["session_id"]
        assert len([item for item in store.list_messages(forked) if item.role == "user"]) == 1
        assert store.completed_history(forked)[1][-1].role == "assistant"
        assert store.get_run(store.list_messages(forked)[-1].run_id).origin_kind == "command_completion"
        async def summary(_provider, _messages):
            return "用户要求统计 CSV；后台命令已开始，最终结果在最近一轮保留。", "synthetic"
        monkeypatch.setattr(client.app.state.agent_service, "_stream_checkpoint_text", summary)
        compacted = client.post(f"/api/v1/agent/sessions/{sid}/compact")
        assert compacted.status_code == 200, compacted.text
        assert compacted.json()["through_ordinal"] == 2
        assert len(facts(store.provider_history(sid))) == 1
        # The retained newest turn is assistant-only and remains legal after its
        # original user request has moved into the saved checkpoint.
        assert store.completed_history(sid)[1][-1].role == "assistant"
        assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 1


@pytest.mark.parametrize("action", ["rebind", "close"])
def test_rebinding_or_closing_session_keeps_result_but_does_not_wake_old_task(settings, tmp_path, action):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        store = client.app.state.agent_service.store
        if action == "rebind":
            new_project = tmp_path / "different-project"
            new_project.mkdir()
            (new_project / "README.md").write_bytes(b"Untouched project\n")
            binding = store.database.sync_project_binding(project_id="project-" + "c" * 24, root_path=str(new_project),
                display_name="Different project", fingerprint="d" * 64)
            store.rebind_session(sid, project_id=binding.project_id)
        else:
            store.close_session(sid)
        gate.write_bytes(b"go")
        completion_store = CommandCompletionStore(store.database)
        eventually(lambda: completion_store.list_events(sid))
        client.portal.call(worker.tick)
        assert completion_store.list_events(sid)[0]["delivery_state"] == "suppressed"
        assert len(store.list_runs(sid)) == 1 and provider.completion_calls == 0
        assert target.is_file()
        if action == "rebind":
            assert list(new_project.iterdir()) == [new_project / "README.md"]
            assert (new_project / "README.md").read_bytes() == b"Untouched project\n"


def test_unavailable_provider_before_admission_defers_auto_retry_but_user_can_consume(settings, tmp_path, monkeypatch):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        gate.write_bytes(b"go")
        service = client.app.state.agent_service
        completion_store = CommandCompletionStore(service.store.database)
        eventually(lambda: completion_store.pending(sid))
        attempts = []
        original = client.app.state.agent_queue_service.refresh_provider

        async def fail_before_start(session_id):
            attempts.append(session_id)
            raise AgentUnavailableError("合成连接当前不可用")

        monkeypatch.setattr(client.app.state.agent_queue_service, "refresh_provider", fail_before_start)
        client.portal.call(worker.tick)
        client.portal.call(worker.tick)
        client.portal.call(worker.tick)
        assert attempts == [sid]
        assert len(service.store.list_runs(sid)) == 1
        assert completion_store.pending(sid)[0]["payload"]["automatic_delivery_error"] == "合成连接当前不可用"
        monkeypatch.setattr(client.app.state.agent_queue_service, "refresh_provider", original)
        response = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": "retry-fact-manually"},
            json={"message": "继续核对命令结果。", "writer_mode": "author_review_change_proposal"})
        assert response.status_code == 202, response.text
        wait_for_success(client, response.json()["run"]["run_id"])
        assert completion_store.list_events(sid)[0]["delivery_state"] == "delivered"
        assert completion_store.list_events(sid)[0]["payload"].get("automatic_delivery_error") is None


def test_active_project_completion_refreshes_pin_before_builtin_read(tmp_path):
    from test_agent_service import wait_terminal
    from test_project_memory_scope import build_project
    from zhimeng_runtime.database import latest_own_write_pin

    provider = CompletionProvider("pending", tmp_path / "pending", hold_first=True)
    service, binding, changes, project = build_project(tmp_path, provider)
    command, target, gate = command_fixture(project)
    provider.command, provider.target = command, target
    _, _, current = changes._snapshot(binding)

    async def scenario():
        try:
            sid = (await service.create_session(project_id=binding.project_id, title="普通 CSV 项目")).session_id
            accepted = await service.start_run(session_id=sid, message="后台统计完成后读取文件。",
                expected_project_revision=current.revision, expected_project_fingerprint=current.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="project-background-completion")
            async with asyncio.timeout(15):
                while not provider.first_running.is_set():
                    await asyncio.sleep(0.01)
            gate.write_bytes(b"go")
            completion_store = CommandCompletionStore(service.store.database)
            async with asyncio.timeout(15):
                while not completion_store.pending(sid):
                    await asyncio.sleep(0.01)
            provider.release_first.set()
            done = await wait_terminal(service, accepted.run.run_id)
            assert done.status == "succeeded", done
            calls = service.store.list_tool_calls(done.run_id)
            assert [item.tool_name for item in calls] == ["computer_run_command", "read_file"]
            assert all(item.status == "succeeded" for item in calls)
            with service.store.database.connection() as connection:
                assert latest_own_write_pin(connection, done.run_id)[0] > current.revision
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_completion_arriving_inside_final_commit_is_consumed_before_run_finishes(settings, tmp_path, monkeypatch):
    command, target, gate = command_fixture(tmp_path)
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        store = client.app.state.agent_service.store
        original = store.complete_run
        calls = []
        sid = session(client)

        def late_complete(*args, **kwargs):
            if not calls:
                calls.append("injected")
                gate.write_bytes(b"go")
                eventually(lambda: CommandCompletionStore(store.database).pending(sid))
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "complete_run", late_complete)
        rid = start(client, sid)
        wait_for_success(client, rid)
        assert len(store.list_runs(sid)) == 1
        assert CommandCompletionStore(store.database).list_events(sid)[0]["delivered_run_id"] == rid
        assert store.list_messages(sid)[-1].content.endswith("工程 50。")


def test_model_terminal_command_read_consumes_notification_but_does_not_duplicate_fact(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)

    class ObservingProvider(CompletionProvider):
        def __init__(self):
            super().__init__(command, target)
            self.observation_started = threading.Event()
            self.allow_read = threading.Event()
            self.phase = 0

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.requests.append([dict(item) for item in messages])
            assert not facts(messages)
            if self.phase == 0:
                self.phase = 1
                yield ProviderToolCall(0, "observed-launch", "computer_run_command", {"command": self.command, "yield_time_ms": 100})
                yield ProviderCompleted("tool_calls", None)
            elif self.phase == 1:
                self.phase = 2
                result = json.loads([item for item in messages if item.get("role") == "tool"][-1]["content"])
                command_id = result["output"]["session_id"]
                self.observation_started.set()
                while not self.allow_read.is_set():
                    await asyncio.sleep(0.01)
                yield ProviderToolCall(0, "observed-final-read", "command_session", {"action": "read", "session_id": command_id})
                yield ProviderCompleted("tool_calls", None)
            else:
                result = json.loads([item for item in messages if item.get("role") == "tool"][-1]["content"])
                assert result["output"]["status"] == "exited"
                yield ProviderTextDelta("已通过工具读取最终结果。")
                yield ProviderCompleted("stop", None)

    provider = ObservingProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        assert provider.observation_started.wait(10)
        gate.write_bytes(b"go")
        completion_store = CommandCompletionStore(client.app.state.agent_service.store.database)
        eventually(lambda: completion_store.pending(sid))
        provider.allow_read.set()
        wait_for_success(client, rid)
        wait_idle(client, rid)
        client.portal.call(worker.tick)
        assert len(client.app.state.agent_service.store.list_runs(sid)) == 1
        assert completion_store.list_events(sid)[0]["delivered_run_id"] == rid


def test_two_conversations_receive_only_their_own_background_completion(settings, tmp_path):
    delegates = {}
    gates = {}
    for name in ("scope-A", "scope-B"):
        folder = tmp_path / name
        folder.mkdir()
        command, target, gate = command_fixture(folder)
        delegates[name] = CompletionProvider(command, target)
        gates[name] = gate

    class MultipleProvider(ApiProvider):
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            name = next(item["content"] for item in messages if item.get("role") == "user" and item.get("content") in delegates)
            for fact in facts(messages):
                assert name in fact["result"]["command"]
            async for event in delegates[name].stream_chat(messages, cancel_event=cancel_event, tools=tools):
                yield event

    with TestClient(create_app(settings, agent_provider=MultipleProvider())) as client:
        scheduler(client)
        store = client.app.state.agent_service.store
        sids = {name: session(client) for name in delegates}
        for name, sid in sids.items():
            response = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": "concurrent-command-" + name},
                                   json={"message": name, "writer_mode": "author_review_change_proposal"})
            assert response.status_code == 202
            wait_for_success(client, response.json()["run"]["run_id"])
        gates["scope-B"].write_bytes(b"go")
        eventually(lambda: len(store.list_runs(sids["scope-B"])) == 2 and store.list_runs(sids["scope-B"])[-1].status == "succeeded", timeout=20)
        assert len(store.list_runs(sids["scope-A"])) == 1
        gates["scope-A"].write_bytes(b"go")
        eventually(lambda: len(store.list_runs(sids["scope-A"])) == 2 and store.list_runs(sids["scope-A"])[-1].status == "succeeded", timeout=20)
        completion_store = CommandCompletionStore(store.database)
        for name, sid in sids.items():
            assert len([item for item in store.list_messages(sid) if item.role == "user"]) == 1
            events = completion_store.list_events(sid)
            assert len(events) == 1 and events[0]["owner_session_id"] == sid


def test_nonzero_background_exit_delivers_real_error_and_existing_file_without_replay(settings, tmp_path):
    command, target, gate = command_fixture(tmp_path)
    script = tmp_path / "interactive.py"
    script.write_bytes(script.read_bytes() + b"import sys\nprint('synthetic exit seven',file=sys.stderr,flush=True)\nsys.exit(7)\n")
    provider = CompletionProvider(command, target)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        gate.write_bytes(b"go")
        store = client.app.state.agent_service.store
        eventually(lambda: len(store.list_runs(sid)) == 2 and store.list_runs(sid)[-1].status == "succeeded", timeout=20)
        event = CommandCompletionStore(store.database).list_events(sid)[0]
        assert event["exit_code"] == 7 and event["status"] == "exited"
        received = [fact for messages in provider.requests for fact in facts(messages)]
        assert received and all(fact["exit_code"] == 7 for fact in received)
        assert "synthetic exit seven" in received[0]["result"]["stderr_tail"]
        assert len([item for run in store.list_runs(sid) for item in store.list_tool_calls(run.run_id) if item.tool_name == "computer_run_command"]) == 1


def test_internal_followup_preserves_user_file_and_skill_context_without_inventing_focus_change(tmp_path):
    from test_agent_service import wait_terminal
    from test_project_memory_scope import build_project
    from zhimeng_runtime.agent_queue import AgentQueueService
    from zhimeng_runtime.agent_queue_scheduler import AgentQueueScheduler

    provider = CompletionProvider("pending", tmp_path / "pending")
    service, binding, changes, project = build_project(tmp_path, provider)
    command, target, gate = command_fixture(project)
    provider.command, provider.target = command, target
    _, _, current = changes._snapshot(binding)
    attached_id = next(item["file_id"] for item in service.tool_executor.reader.inspect(project)["files"] if item["relative_path"] == "data.csv")
    worker = AgentQueueScheduler(AgentQueueService(service), poll_seconds=0.025)

    async def scenario():
        try:
            sid = (await service.create_session(project_id=binding.project_id, title="后台文件引用核查")).session_id
            accepted = await service.start_run(session_id=sid, message="用附上的文件和指定技能核对后台统计。",
                expected_project_revision=current.revision, expected_project_fingerprint=current.fingerprint,
                active_chapter_id=None, open_file_path="data.csv", manual_file_ids=[attached_id], manual_skill_names=["CSV 核对"],
                writer_mode="author_review_change_proposal", idempotency_key="background-context-first")
            first = await wait_terminal(service, accepted.run.run_id)
            assert first.status == "succeeded"
            while service.is_run_executing(first.run_id):
                await asyncio.sleep(0.01)
            gate.write_bytes(b"go")
            worker.start()
            async with asyncio.timeout(20):
                while len(service.store.list_runs(sid)) < 2 or service.store.list_runs(sid)[-1].status != "succeeded":
                    await asyncio.sleep(0.02)
            internal_requests = [messages for messages in provider.requests if facts(messages)]
            assert internal_requests
            wire = "\n".join(str(item.get("content", "")) for item in internal_requests[0])
            assert "用户这时在编辑器里开着" in wire and "data.csv" in wire
            assert "alpha,1" in wire and "CSV 核对" in wire
            assert "用户这时没开着文件" not in wire
            internal = service.store.list_runs(sid)[-1]
            while service.is_run_executing(internal.run_id):
                await asyncio.sleep(0.01)
            after = service.store.database.get_project_binding(binding.project_id)
            normal = await service.start_run(session_id=sid, message="现在没有打开文件，直接继续聊天。",
                expected_project_revision=after.revision, expected_project_fingerprint=after.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="background-context-user")
            assert (await wait_terminal(service, normal.run.run_id)).status == "succeeded"
            assert "用户这时没开着文件" in "\n".join(str(item.get("content", "")) for item in provider.requests[-1])
        finally:
            await worker.stop()
            await service.shutdown()

    asyncio.run(scenario())
