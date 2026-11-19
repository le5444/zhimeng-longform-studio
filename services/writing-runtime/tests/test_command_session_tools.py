from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_tools import AgentToolError, CommandSessionArguments
from zhimeng_runtime.app import create_app


def script_command(tmp_path: Path, content: str) -> str:
    path = tmp_path / "interactive.py"
    # Match the tool's native stdin default regardless of the test host's UTF-8 mode.
    source = "import locale, sys\nsys.stdin.reconfigure(encoding=locale.getencoding(), errors='strict')\n" + content
    path.write_bytes(source.encode("utf-8"))
    return f'"{sys.executable}" -u "{path}"'


@pytest.mark.parametrize("body", [{}, {"input": ""}])
def test_input_requires_text_or_eof(body):
    with pytest.raises(ValidationError, match="input 没有提供要发送的内容") as caught:
        CommandSessionArguments.model_validate({"action": "input", "session_id": "cmd_example", **body})
    assert 'hello\\n' in str(caught.value)
    assert 'close_stdin' in str(caught.value)


@pytest.mark.parametrize("text", ["\n", " ", " \t\n"])
def test_input_preserves_empty_line_and_whitespace(text):
    arguments = CommandSessionArguments.model_validate({"action": "input", "session_id": "cmd_example", "input": text})
    assert arguments.input == text


@pytest.mark.parametrize("body", [{"close_stdin": True}, {"input": "", "close_stdin": True}])
def test_input_allows_eof_without_text(body):
    arguments = CommandSessionArguments.model_validate({"action": "input", "session_id": "cmd_example", **body})
    assert arguments.close_stdin is True
    assert arguments.input == ""


def test_read_needs_no_input_and_wait_uses_integer_milliseconds():
    arguments = CommandSessionArguments.model_validate({"action": "read", "session_id": "cmd_example", "wait_ms": 100})
    assert arguments.wait_ms == 100
    with pytest.raises(ValidationError):
        CommandSessionArguments.model_validate({"action": "read", "session_id": "cmd_example", "wait_ms": "0.1"})


class InteractiveProvider(ApiProvider):
    """Local protocol fixture: execute real processes through the actual tool loop."""

    def __init__(self, command: str, *, hold_after_start: bool = False, input_text="ordinary document"):
        self.command = command
        self.hold_after_start = hold_after_start
        self.input_text = input_text
        self.turn = 0
        self.outputs: list[dict] = []
        self.catalogs: list[set[str]] = []
        self.started = threading.Event()
        self.session_id: str | None = None
        self.seen_tool_ids: set[str] = set()
        self.phase = "starting"

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.turn += 1
        self.catalogs.append({item.name for item in tools})
        if self.turn == 1:
            yield ProviderToolCall(0, "command-start", "computer_run_command", {
                "command": self.command, "yield_time_ms": 200,
            })
            yield ProviderCompleted("tool_calls", None)
            return
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if tool_messages and tool_messages[-1].get("tool_call_id") not in self.seen_tool_ids:
            self.seen_tool_ids.add(tool_messages[-1].get("tool_call_id"))
            envelope = json.loads(tool_messages[-1]["content"])
            output = envelope.get("output", envelope)
            self.outputs.append(output)
            if output.get("session_id"):
                self.session_id = output["session_id"]
        captured = "".join(item["stdout"] for item in self.outputs)
        has_prompt = "Document title:" in captured or "Ready" in captured
        if self.turn < 100 and self.phase == "starting" and self.outputs[-1]["status"] == "running" and not has_prompt:
            yield ProviderToolCall(0, f"command-prompt-{self.turn}", "command_session", {
                "action": "read", "session_id": self.session_id,
                "cursor": self.outputs[-1]["cursor"], "wait_ms": 1500,
            })
            yield ProviderCompleted("tool_calls", None)
            return
        if self.turn < 100 and self.phase == "starting":
            self.phase = "ready"
            self.started.set()
            while self.hold_after_start and not cancel_event.is_set():
                await asyncio.sleep(0.01)
        elif self.turn < 100 and self.phase == "ready":
            self.phase = "finishing"
            yield ProviderToolCall(0, "command-input", "command_session", {
                "action": "input", "session_id": self.session_id,
                "input": self.input_text + "\n", "wait_ms": 1500,
                "cursor": self.outputs[-1]["cursor"],
            })
            yield ProviderCompleted("tool_calls", None)
            return
        if self.turn < 100 and self.phase == "finishing" and self.outputs[-1]["status"] == "running":
            yield ProviderToolCall(0, f"command-read-{self.turn}", "command_session", {
                "action": "read", "session_id": self.session_id,
                "cursor": self.outputs[-1]["cursor"], "wait_ms": 1500,
            })
            yield ProviderCompleted("tool_calls", None)
            return
        yield ProviderTextDelta("Command state received.")
        yield ProviderCompleted("stop", None)


def start(client, sid, key="command-turn-001", *, read_only=False):
    response = client.post(
        f"/api/v1/agent/sessions/{sid}/runs",
        headers={"Idempotency-Key": key},
        json={
            "message": "Work with the local document utility.",
            "writer_mode": "repair_only" if read_only else "author_review_change_proposal",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run"]["run_id"]


def wait_idle(client, rid):
    async def settled():
        service = client.app.state.agent_service
        while service.is_run_executing(rid):
            await asyncio.sleep(0.005)
    client.portal.call(settled)


def executor_call(client, rid, tool_name, arguments, *, cancel_event=None):
    service = client.app.state.agent_service
    executor = service.tool_executor
    run = service.store.get_run(rid)
    async def invoke():
        definition = executor._definitions_by_name[tool_name]
        return await executor.execute(
            run, tool_call_id="direct-session-call", tool_name=tool_name, arguments=arguments,
            request_hash=executor.request_hash(tool_name, definition.version, arguments),
            cancel_event=cancel_event or asyncio.Event(),
        )
    return client.portal.call(invoke).output


@pytest.mark.parametrize("title", ["ordinary document", "普通文档"])
def test_interactive_process_continues_across_turns_and_writes_real_document(settings, tmp_path, title):
    target = tmp_path / "summary.txt"
    provider = InteractiveProvider(script_command(tmp_path,
        "from pathlib import Path\n"
        "answer = input('Document title: ')\n"
        f"Path({str(target)!r}).write_text(answer, encoding='utf-8')\n"
        "print('Saved:' + answer, flush=True)\n"), input_text=title)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        first = start(client, sid)
        assert wait_for_success(client, first)["status"] == "succeeded"
        wait_idle(client, first)
        assert provider.outputs[0]["status"] == "running"
        assert "Document title:" in "".join(item["stdout"] for item in provider.outputs)
        assert provider.outputs[0]["timeout_seconds"] is None
        initial_outputs = len(provider.outputs)
        assert not target.exists()
        second = start(client, sid, "command-turn-002")
        assert wait_for_success(client, second)["status"] == "succeeded"
        result = provider.outputs[-1]
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        new_output = "".join(item["stdout"] for item in provider.outputs[initial_outputs:])
        assert "Saved:" + title in new_output
        assert "Document title:" not in new_output
        assert target.read_text(encoding="utf-8") == title
        listed = executor_call(client, second, "command_session", {"action": "list"})
        assert [item["session_id"] for item in listed["sessions"]] == [provider.session_id]
        assert Path(result["stdout_file"]).is_file()
        assert "command_session" in provider.catalogs[0]


def test_cancel_run_stops_already_yielded_process(settings, tmp_path):
    provider = InteractiveProvider(script_command(tmp_path,
        "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n"), hold_after_start=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert provider.started.wait(10)
        response = client.post(f"/api/v1/agent/runs/{rid}/cancel")
        assert response.status_code == 200, response.text
        wait_idle(client, rid)
        manager = client.app.state.agent_service.tool_executor._command_sessions
        assert manager.read(sid, provider.session_id)["status"] == "cancelled"


def test_missing_input_fails_before_writing_then_whitespace_and_eof_reach_process(settings, tmp_path):
    target = tmp_path / "stdin.txt"
    provider = InteractiveProvider(script_command(tmp_path,
        "from pathlib import Path\nimport sys\n"
        "print('Ready', flush=True)\n"
        "text = sys.stdin.read()\n"
        f"Path({str(target)!r}).write_text(text, encoding='utf-8')\n"))
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        wait_idle(client, rid)
        for missing in ({}, {"input": ""}):
            with pytest.raises(AgentToolError, match="input 没有提供要发送的内容"):
                executor_call(client, rid, "command_session", {
                    "action": "input", "session_id": provider.session_id, **missing,
                })
        assert not target.exists()
        result = executor_call(client, rid, "command_session", {
            "action": "input", "session_id": provider.session_id,
            "input": "\n \t\n", "close_stdin": True,
            "cursor": provider.outputs[-1]["cursor"], "wait_ms": 1500,
        })
        deadline = time.monotonic() + 5
        while result["status"] == "running":
            assert time.monotonic() < deadline
            result = executor_call(client, rid, "command_session", {
                "action": "read", "session_id": provider.session_id,
                "cursor": result["cursor"], "wait_ms": 1000,
            })
        assert result["exit_code"] == 0
        assert target.read_text(encoding="utf-8") == "\n \t\n"


def test_runtime_shutdown_stops_process_after_successful_turn(settings, tmp_path):
    provider = InteractiveProvider(script_command(tmp_path,
        "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n"))
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        manager = client.app.state.agent_service.tool_executor._command_sessions
        assert manager.read(sid, provider.session_id)["status"] == "running"
    assert manager.read(sid, provider.session_id)["status"] != "running"


def test_command_session_respects_session_ownership_and_read_only_mode(settings, tmp_path):
    provider = InteractiveProvider(script_command(tmp_path,
        "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n"))
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        provider.turn = 100
        other = session(client)
        other_rid = start(client, other, "command-other-001")
        wait_for_success(client, other_rid)
        missing = executor_call(client, other_rid, "command_session", {
            "action": "input", "session_id": provider.session_id, "input": "hello\n",
        })
        assert missing["sessions"] == []
        assert missing["requested_session_id"] == provider.session_id
        assert client.app.state.agent_service.tool_executor._command_sessions.read(sid, provider.session_id)["status"] == "running"
        ro = start(client, sid, "command-readonly-001", read_only=True)
        wait_for_success(client, ro)
        assert "computer_run_command" not in provider.catalogs[-1]
        assert "command_session" not in provider.catalogs[-1]
        with pytest.raises(AgentToolError, match="只问不改"):
            executor_call(client, ro, "command_session", {"action": "list"})
        result = executor_call(client, rid, "command_session", {
            "action": "stop", "session_id": provider.session_id,
        })
        assert result["status"] == "cancelled"


def test_cancel_inflight_read_stops_previous_turn_process(settings, tmp_path):
    provider = InteractiveProvider(script_command(tmp_path,
        "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n"))
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        wait_for_success(client, rid)
        wait_idle(client, rid)
        service = client.app.state.agent_service
        executor = service.tool_executor
        # A second run has its own identity while operating the earlier process.
        provider.turn = 100
        second = start(client, sid, "command-second-read")
        wait_for_success(client, second)
        wait_idle(client, second)
        async def cancel_read():
            run = service.store.get_run(second)
            event = asyncio.Event()
            arguments = {"action": "read", "session_id": provider.session_id,
                         "cursor": provider.outputs[-1]["cursor"], "wait_ms": 300_000}
            definition = executor._definitions_by_name["command_session"]
            task = asyncio.create_task(executor.execute(
                run, tool_call_id="cancel-read", tool_name="command_session", arguments=arguments,
                request_hash=executor.request_hash("command_session", definition.version, arguments),
                cancel_event=event,
            ))
            deadline = time.monotonic() + 5
            while not executor._active_command_sessions.get(second):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            event.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        client.portal.call(cancel_read)
        assert executor._command_sessions.read(sid, provider.session_id)["status"] == "cancelled"


def test_resumed_command_refreshes_project_revision_before_reading_file(tmp_path):
    from test_agent_service import wait_terminal
    from test_project_memory_scope import build_project
    from zhimeng_runtime.database import latest_own_write_pin

    target = tmp_path / "spreadsheet-project" / "data.csv"
    provider = InteractiveProvider(script_command(tmp_path,
        "from pathlib import Path\n"
        "answer = input('Document title: ')\n"
        f"Path({str(target)!r}).write_text('name,count\\n' + answer + ',2\\n', encoding='utf-8')\n"
        "print('Saved', flush=True)\n"))
    service, binding, _changes, _project = build_project(tmp_path, provider)

    async def scenario():
        try:
            current = await service.create_session(project_id=binding.project_id, title="CSV utility")
            for index in range(2):
                accepted = await service.start_run(
                    session_id=current.session_id, message="Continue the CSV utility.",
                    expected_project_revision=binding.revision,
                    expected_project_fingerprint=binding.fingerprint,
                    active_chapter_id=None, writer_mode="author_review_change_proposal",
                    idempotency_key=f"csv-command-{index:04}",
                )
                run = await wait_terminal(service, accepted.run.run_id)
                assert run.status == "succeeded"
                while service.is_run_executing(run.run_id):
                    await asyncio.sleep(0.005)
            with service.store.database.connection() as connection:
                pin = latest_own_write_pin(connection, run.run_id)
            assert pin is not None
            assert pin[0] > binding.revision
            executor = service.tool_executor
            args = {"path": "data.csv"}
            definition = executor._definitions_by_name["read_file"]
            result = await executor.execute(
                run, tool_call_id="read-produced-csv", tool_name="read_file", arguments=args,
                request_hash=executor.request_hash("read_file", definition.version, args),
                cancel_event=asyncio.Event(),
            )
            assert "ordinary document,2" in str(result.output)
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["nonzero", "timeout", "stop"])
def test_process_outcome_is_saved_and_returned_to_model(settings, tmp_path, outcome):
    command = script_command(tmp_path, "raise SystemExit(3)\n" if outcome == "nonzero"
                             else "import time\ntime.sleep(300)\n")

    class OutcomeProvider(ApiProvider):
        def __init__(self):
            self.results = []
            self.calls = 0

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.calls += 1
            if self.calls == 1:
                arguments = {"command": command, "yield_time_ms": 10 if outcome == "stop" else 2000}
                if outcome == "timeout":
                    arguments["timeout_seconds"] = 1
                yield ProviderToolCall(0, "outcome-start", "computer_run_command", arguments)
                yield ProviderCompleted("tool_calls", None)
                return
            result = next(json.loads(message["content"]) for message in reversed(messages)
                          if message.get("role") == "tool")
            self.results.append(result)
            if self.calls == 2 and outcome == "stop":
                yield ProviderToolCall(0, "outcome-stop", "command_session", {
                    "action": "stop", "session_id": result["output"]["session_id"],
                })
                yield ProviderCompleted("tool_calls", None)
                return
            yield ProviderTextDelta("The process result is available.")
            yield ProviderCompleted("stop", None)

    provider = OutcomeProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        result = provider.results[-1]
        expected = "succeeded" if outcome == "stop" else "failed"
        assert result["status"] == expected, provider.results
        if outcome == "stop":
            assert result["output"]["status"] == "cancelled"
        elif outcome == "timeout":
            assert result["output"]["timed_out"] is True
        else:
            assert result["output"]["exit_code"] == 3
        activities = client.get(f"/api/v1/agent/runs/{rid}/activities").json()["activities"]
        saved = [item["payload"] for item in activities if item["kind"] == "tool_result"]
        assert saved[-1]["status"] == expected
        assert saved[-1]["output"] == result["output"]


@pytest.mark.parametrize("action", ["read", "input", "stop"])
@pytest.mark.parametrize("has_command", [False, True])
def test_missing_session_returns_owned_records_as_failed_result_without_replaying(settings, tmp_path, action, has_command):
    from zhimeng_runtime.command_sessions import CommandSessionManager

    class RecoveryProvider(ApiProvider):
        def __init__(self):
            self.turn = 0
            self.results = []
            self.missing_id = "verify"

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.turn += 1
            if self.turn == 1:
                arguments = {"action": action, "session_id": self.missing_id}
                if action == "input":
                    arguments["input"] = "unexpected input\n"
                yield ProviderToolCall(0, "missing-command", "command_session", arguments)
                yield ProviderCompleted("tool_calls", None)
                return
            result = next(json.loads(message["content"]) for message in reversed(messages) if message.get("role") == "tool")
            self.results.append(result)
            if self.turn == 2 and result["output"]["sessions"]:
                yield ProviderToolCall(0, "recovered-read", "command_session", {
                    "action": "read", "session_id": result["output"]["sessions"][0]["session_id"], "wait_ms": 0,
                })
                yield ProviderCompleted("tool_calls", None)
                return
            yield ProviderTextDelta("Available command records checked.")
            yield ProviderCompleted("stop", None)

    provider = RecoveryProvider()
    target = tmp_path / "unexpected.txt"
    command = script_command(tmp_path, "from pathlib import Path\nprint('Ready', flush=True)\nvalue = input()\n"
                             f"Path({str(target)!r}).write_text(value, encoding='utf-8')\n")
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        other = session(client)
        manager = CommandSessionManager(tmp_path / "fixture-commands")
        client.app.state.agent_service.tool_executor._command_sessions = manager
        foreign = manager.start(owner_session_id=other, owner_run_id="foreign-run", command=command, cwd=tmp_path, yield_time_ms=100)
        own = manager.start(owner_session_id=sid, owner_run_id="earlier-run", command=command, cwd=tmp_path, yield_time_ms=100) if has_command else None
        if action == "input":
            provider.missing_id = foreign["session_id"]
        rid = start(client, sid)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        wait_idle(client, rid)
        failed = provider.results[0]
        assert failed["status"] == "failed"
        assert failed["error"]["error"]["code"] == "command_session_not_found"
        output = failed["output"]
        assert output["schema"] == "zhimeng.tool-result.command-session-recovery.v1"
        assert output["requested_session_id"] == provider.missing_id
        assert output["requested_action"] == action
        assert [item["session_id"] for item in output["sessions"]] == ([own["session_id"]] if own else [])
        assert "foreign-run" not in json.dumps(output)
        if own:
            assert provider.results[-1]["status"] == "succeeded"
            assert provider.results[-1]["output"]["session_id"] == own["session_id"]
            assert manager.read(sid, own["session_id"])["status"] == "running"
        else:
            assert "没有可接续的命令" in output["hint"]
        assert manager.read(other, foreign["session_id"])["status"] == "running"
        assert not target.exists()
        calls = client.app.state.agent_service.store.list_tool_calls(rid)
        assert all(call.tool_name == "command_session" for call in calls)
        assert calls[0].status == "failed"
        assert calls[0].output == output


def test_tool_loop_reads_all_output_pages_and_persists_consumed_cursors(settings, tmp_path):
    expected = {"stdout": "前段🙂\n" * 3000 + "MIDDLE-KEEP\n" + "后段🙂\n" * 3000,
                "stderr": "diagnostic\n" * 7000}
    command = script_command(tmp_path,
        f"sys.stdout.buffer.write({expected['stdout']!r}.encode('utf-8'))\n"
        f"sys.stderr.buffer.write({expected['stderr']!r}.encode('utf-8'))\n")

    class PagingProvider(ApiProvider):
        def __init__(self):
            self.outputs = []
            self.calls = 0

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.calls += 1
            assert self.calls < 30, "command cursor did not finish"
            if self.calls == 1:
                yield ProviderToolCall(0, "paged-start", "computer_run_command", {
                    "command": command, "yield_time_ms": 1000,
                })
            else:
                envelope = next(json.loads(m["content"]) for m in reversed(messages) if m.get("role") == "tool")
                assert envelope["status"] == "succeeded"
                page = envelope["output"]
                self.outputs.append(page)
                if page["status"] != "running" and not page["stdout_has_more"] and not page["stderr_has_more"]:
                    yield ProviderTextDelta("Both saved output streams have been read.")
                    yield ProviderCompleted("stop", None)
                    return
                yield ProviderToolCall(0, f"paged-read-{self.calls}", "command_session", page["next_read"])
            yield ProviderCompleted("tool_calls", None)

    provider = PagingProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        wait_idle(client, rid)
        assert len(provider.outputs) > 1
        for name, text in expected.items():
            assert "".join(p[name] for p in provider.outputs) == text
            consumed = 0
            for page in provider.outputs:
                assert len(page[name].encode()) <= 24_000
                consumed += len(page[name].encode())
                assert page["cursor"][name] == consumed
                assert page[name + "_truncated"] == page[name + "_has_more"]
            assert Path(provider.outputs[-1][name + "_file"]).read_bytes() == text.encode()
        calls = client.app.state.agent_service.store.list_tool_calls(rid)
        assert [call.output for call in calls] == provider.outputs
        assert provider.outputs[-1]["exit_code"] == 0
        assert "next_read" not in provider.outputs[-1]
