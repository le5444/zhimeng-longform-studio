"""Recheck real persistent commands across concurrent, independent conversations."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from test_agent_queue import session
from test_agent_queue_scheduler import eventually
from test_command_session_tools import script_command
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app


class IsolatedCommandProvider(ApiProvider):
    def __init__(self, commands):
        self.commands = commands
        self.command_ids = {}
        self.entered = {name: threading.Event() for name in commands}
        self.release = {name: threading.Event() for name in commands}

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        name = next(item["content"] for item in messages if item.get("role") == "user" and item.get("content") in self.commands)
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            yield ProviderToolCall(0, "start-" + name, "computer_run_command", {"command": self.commands[name], "yield_time_ms": 100})
            yield ProviderCompleted("tool_calls", None)
            return
        envelope = json.loads(tool_messages[-1]["content"])
        output = envelope.get("output", envelope)
        self.command_ids[name] = output["session_id"]
        self.entered[name].set()
        if len(tool_messages) == 1:
            while not self.release[name].is_set():
                if cancel_event.is_set():
                    return
                await asyncio.sleep(0.01)
            yield ProviderToolCall(0, "input-" + name, "command_session", {
                "action": "input", "session_id": output["session_id"], "input": "完成 " + name + "\n", "wait_ms": 1000,
            })
            yield ProviderCompleted("tool_calls", None)
            return
        if output["status"] == "running":
            yield ProviderToolCall(0, f"read-{name}-{len(tool_messages)}", "command_session", {
                "action": "read", "session_id": output["session_id"], "wait_ms": 1000,
            })
            yield ProviderCompleted("tool_calls", None)
            return
        assert output["exit_code"] == 0
        yield ProviderTextDelta("命令结束，文件已写入。")
        yield ProviderCompleted("stop", None)


def test_cancel_one_conversation_preserves_other_process_and_background_file_task(settings, tmp_path):
    commands = {}
    for name in ("scope-A", "scope-B"):
        folder = tmp_path / name
        folder.mkdir()
        commands[name] = script_command(folder, "from pathlib import Path\n"
            "value = input('Ready> ')\n"
            f"Path({str(folder / 'result.txt')!r}).write_text(value, encoding='utf-8')\n"
            "print('written', flush=True)\n")
    provider = IsolatedCommandProvider(commands)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sids = {name: session(client) for name in commands}
        rids = {}
        for name in commands:
            response = client.post(f"/api/v1/agent/sessions/{sids[name]}/runs", headers={"Idempotency-Key": "isolated-" + name},
                                   json={"message": name, "writer_mode": "author_review_change_proposal"})
            assert response.status_code == 202, response.text
            rids[name] = response.json()["run"]["run_id"]
        assert all(event.wait(10) for event in provider.entered.values())
        assert len(set(provider.command_ids.values())) == 2
        manager = client.app.state.agent_service.tool_executor._command_sessions
        assert client.post(f"/api/v1/agent/runs/{rids['scope-A']}/cancel").status_code == 200
        store = client.app.state.agent_service.store
        eventually(lambda: store.get_run(rids["scope-A"]).status == "cancelled")
        assert manager.read(sids["scope-A"], provider.command_ids["scope-A"])["status"] == "cancelled"
        assert manager.read(sids["scope-B"], provider.command_ids["scope-B"])["status"] == "running"
        provider.release["scope-B"].set()
        # No event stream or browser requests keep B alive from this point.
        eventually(lambda: store.get_run(rids["scope-B"]).status == "succeeded")
        assert not (tmp_path / "scope-A" / "result.txt").exists()
        assert (tmp_path / "scope-B" / "result.txt").read_text(encoding="utf-8") == "完成 scope-B"
        assert manager.read(sids["scope-B"], provider.command_ids["scope-B"])["status"] == "exited"
        assert all(call.status == "succeeded" for call in store.list_tool_calls(rids["scope-B"]))
