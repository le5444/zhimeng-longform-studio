from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from test_command_session_tools import start
from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider, ProviderToolCall, ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.app import create_app
from zhimeng_runtime.mcp_models import McpDiscoveredTool
from zhimeng_runtime.mcp_tool_adapter import McpToolAdapter, tool_alias


SCHEMA = {"type": "object", "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
          "required": ["value"], "additionalProperties": False, "$defs": {"nested": {"type": "object", "properties": {"x": {"type": "number"}}}}}


def tool(name="查询.中文", *, read_only=True, generation=1, schema=None):
    return McpDiscoveredTool(connection_id="fixture-connection", generation=generation, server_name="合成服务",
                             raw_name=name, description="Read a synthetic value.", input_schema=schema or deepcopy(SCHEMA),
                             read_only=read_only, annotations={"readOnlyHint": read_only})


class FakeManager:
    def __init__(self, settings=None, runtime_dir=None):
        self.settings = settings
        self.items = [tool(), tool("save", read_only=False)]
        self.calls = []
        self.fail = False
        self.result_error = False

    def catalog(self):
        return tuple(self.items)

    def redact(self, connection_id, text):
        return text.replace("synth-private-value", "[已隐藏]")

    async def start(self):
        pass

    async def shutdown(self):
        pass

    async def call(self, connection_id, generation, name, arguments):
        self.calls.append((connection_id, generation, name, arguments))
        if self.fail:
            raise RuntimeError("connection lost synth-private-value; external write outcome unknown")
        assert any(item.raw_name == name and item.generation == generation for item in self.items), "catalog changed"
        return {"content": [{"type": "text", "text": "real result synth-private-value"}],
                "structuredContent": {"answer": arguments.get("value"), "secret": "synth-private-value"}, "isError": self.result_error}


def test_catalog_aliases_schema_versions_and_long_description_preserve_identity():
    manager = FakeManager()
    names = ["查询.中文", "查询_中文", "查询中文", "x" * 10000, "same"]
    manager.items = [tool(name) for name in names] + [tool("same").model_copy(update={"connection_id": "second"})]
    manager.items[0] = manager.items[0].model_copy(update={"description": "说明" * 3000})
    adapter = McpToolAdapter(manager)
    definitions = adapter.bind(read_only=False)
    assert len(definitions) == len(names) + 1
    assert len({item.name for item in definitions}) == len(definitions)
    assert all(len(item.name) <= 64 and item.name.startswith("mcp__") for item in definitions)
    assert all(item.parameters == SCHEMA for item in definitions)
    assert max(len(item.description) for item in definitions) == 4096
    assert "Read a synthetic value." in next(item.description for item in definitions if item.name == tool_alias("fixture-connection", "x" * 10000))
    assert len(adapter.details("fixture-connection")[0]["description"]) == 6000
    manager.items.reverse()
    assert definitions == adapter.bind(read_only=False)
    assert tool_alias("id", "a.b") != tool_alias("id", "a_b")


def test_read_only_invalid_schema_and_per_task_snapshot():
    manager = FakeManager()
    manager.items.append(tool("invalid", schema={"type": "array"}))
    adapter = McpToolAdapter(manager)

    async def run():
        snapshot = adapter.bind(read_only=True)
        assert len(snapshot) == 1
        assert adapter.details("fixture-connection")[-1]["schema_error"]
        manager.items[0] = tool(generation=2, schema={"type": "object"})
        # Metadata cannot drift underneath an in-flight provider request.
        assert adapter.lookup(snapshot[0].name).source.generation == 1
        with pytest.raises(ValueError, match="catalog changed"):
            await adapter.execute(snapshot[0].name, {"value": "x"}, asyncio.Event())
        adapter.bind(read_only=False)
        assert adapter.lookup(snapshot[0].name).source.generation == 2
        with pytest.raises(ValueError, match="不在"):
            await adapter.execute("mcp__missing", {}, asyncio.Event())

    asyncio.run(run())


def test_results_keep_structured_content_mask_known_secrets_and_never_retry():
    manager = FakeManager()
    adapter = McpToolAdapter(manager)

    async def run():
        name = adapter.bind(read_only=False)[0].name
        result = await adapter.execute(name, {"value": None}, asyncio.Event())
        assert result["structuredContent"]["answer"] is None
        assert "synth-private-value" not in json.dumps(result)
        manager.fail = True
        with pytest.raises(ValueError, match="outcome unknown") as caught:
            await adapter.execute(name, {"value": "x"}, asyncio.Event())
        assert "synth-private-value" not in str(caught.value)
        assert len(manager.calls) == 2
        with pytest.raises(ValueError):
            await adapter.execute(name, {"extra": 3}, asyncio.Event())
        assert len(manager.calls) == 2

    asyncio.run(run())


def test_cancellation_reaches_only_the_own_call():
    manager = FakeManager()
    cancelled = []

    async def call(connection_id, generation, name, arguments):
        if arguments["value"] == "other":
            return {"content": [{"type": "text", "text": "other succeeded"}]}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    manager.call = call
    adapter = McpToolAdapter(manager)

    async def run():
        name = adapter.bind(read_only=False)[0].name
        stop = asyncio.Event()
        pending = asyncio.create_task(adapter.execute(name, {"value": "wait"}, stop))
        await asyncio.sleep(0)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(cancelled) == 1
        assert "other succeeded" in json.dumps(await adapter.execute(name, {"value": "other"}, asyncio.Event()))

    asyncio.run(run())


def test_parallel_conversations_keep_independent_access_catalogs():
    manager = FakeManager()
    adapter = McpToolAdapter(manager)
    write_name = tool_alias("fixture-connection", "save")

    async def run():
        full_bound = asyncio.Event()
        read_bound = asyncio.Event()

        async def full():
            adapter.bind(read_only=False)
            full_bound.set()
            await read_bound.wait()
            assert adapter.lookup(write_name) is not None
            return await adapter.execute(write_name, {"value": "full"}, asyncio.Event())

        async def read():
            await full_bound.wait()
            adapter.bind(read_only=True)
            read_bound.set()
            assert adapter.lookup(write_name) is None
            with pytest.raises(ValueError, match="不在"):
                await adapter.execute(write_name, {"value": "readonly"}, asyncio.Event())

        result, _ = await asyncio.gather(full(), read())
        assert result["structuredContent"]["answer"] == "full"
        assert len(manager.calls) == 1

    asyncio.run(run())


class McpProvider(ApiProvider):
    def __init__(self, *, refresh=False):
        self.catalogs = []
        self.refresh = refresh
        self.manager = None
        self.output = None

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.catalogs.append(tuple(tools))
        if len(self.catalogs) == 1:
            selected = next(item for item in tools if item.name == tool_alias("fixture-connection", "查询.中文"))
            yield ProviderToolCall(0, "mcp-first", selected.name, {"value": "synthetic"})
            yield ProviderCompleted("tool_calls", None)
            return
        self.output = json.loads([item for item in messages if item.get("role") == "tool"][-1]["content"])
        yield ProviderTextDelta("已读取合成数据。")
        yield ProviderCompleted("stop", None)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("is_error", [False, True])
def test_no_folder_run_mcp_uses_normal_tool_history_and_readonly_filter(settings, monkeypatch, read_only, is_error):
    monkeypatch.setattr(importlib.import_module("zhimeng_runtime.app"), "McpClientService", FakeManager)
    provider = McpProvider()
    app = create_app(settings, agent_provider=provider)
    app.state.mcp_service.result_error = is_error
    with TestClient(app) as client:
        sid = session(client)
        rid = start(client, sid, read_only=read_only)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        assert len(app.state.mcp_service.calls) == 1
        names = {item.name for item in provider.catalogs[0]}
        assert (tool_alias("fixture-connection", "save") in names) is not read_only
        calls = app.state.agent_service.store.list_tool_calls(rid)
        assert len(calls) == 1
        assert calls[0].status == ("failed" if is_error else "succeeded")
        assert calls[0].output["structuredContent"]["answer"] == "synthetic"
        assert "synth-private-value" not in str(calls[0])
        assert calls[0].tool_version == next(item.version for item in provider.catalogs[0] if item.name == calls[0].tool_name)
        assert len(provider.catalogs) == 2


@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic_messages"])
def test_mcp_native_schema_and_tool_results_reach_three_protocol_payloads(wire):
    definition = McpToolAdapter(FakeManager()).bind(read_only=True)[0]
    captured = []
    result_text = json.dumps({"content": [{"type": "text", "text": "CSV total"}], "structuredContent": {"total": 42}}, ensure_ascii=False)
    messages = [{"role": "user", "content": "Summarize CSV."},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": definition.name, "arguments": '{"value":null}'}}]},
                {"role": "tool", "tool_call_id": "call1", "content": result_text}]

    async def handler(request):
        captured.append(json.loads(request.content))
        if wire == "responses":
            events = [{"type": "response.output_text.delta", "delta": "Done."}, {"type": "response.completed", "response": {"status": "completed", "output": []}}]
        elif wire == "anthropic_messages":
            events = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done."}}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
        else:
            events = [{"choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": "stop"}]}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleAgentProvider(base_url="https://mcp-synthetic.test/v1", model="synthetic", api_key="synthetic",
                credential_reference="synthetic", timeout_seconds=5, max_tokens=1024, temperature=0.2, reasoning_effort="auto", wire_format=wire, client=client)
            return [item async for item in provider.stream_chat(messages, cancel_event=asyncio.Event(), tools=[definition])]

    assert asyncio.run(run())
    body = captured[0]
    if wire == "chat":
        assert body["tools"][0]["function"]["parameters"] == SCHEMA
        assert body["messages"][-1]["content"] == result_text
    elif wire == "responses":
        assert body["tools"][0]["parameters"] == SCHEMA
        assert body["input"][-1]["output"] == result_text
    else:
        assert body["tools"][0]["input_schema"] == SCHEMA
        assert body["messages"][-1]["content"][0]["content"] == result_text


def test_existing_run_refreshes_mcp_catalog_on_next_provider_turn(settings, monkeypatch):
    monkeypatch.setattr(importlib.import_module("zhimeng_runtime.app"), "McpClientService", FakeManager)
    provider = McpProvider()
    app = create_app(settings, agent_provider=provider)
    manager = app.state.mcp_service
    original_call = manager.call

    async def changed(*args):
        result = await original_call(*args)
        manager.items = [tool("新的工具", generation=2)]
        return result

    manager.call = changed
    with TestClient(app) as client:
        rid = start(client, session(client))
        assert wait_for_success(client, rid)["status"] == "succeeded"
        assert tool_alias("fixture-connection", "查询.中文") in {item.name for item in provider.catalogs[0]}
        assert tool_alias("fixture-connection", "新的工具") in {item.name for item in provider.catalogs[1]}
        assert tool_alias("fixture-connection", "查询.中文") not in {item.name for item in provider.catalogs[1]}


def test_large_mcp_error_keeps_failure_and_full_nontext_result_in_spill(settings, monkeypatch):
    monkeypatch.setattr(importlib.import_module("zhimeng_runtime.app"), "McpClientService", FakeManager)
    provider = McpProvider()
    app = create_app(settings, agent_provider=provider)
    manager = app.state.mcp_service

    async def large_error(*args):
        return {"isError": True, "content": [{"type": "text", "text": "synthetic " * 12000},
                                             {"type": "image", "mimeType": "image/png", "data": "synthetic-binary-placeholder"}],
                "structuredContent": {"failure": "synth-private-value"}}

    manager.call = large_error
    with TestClient(app) as client:
        rid = start(client, session(client))
        assert wait_for_success(client, rid)["status"] == "succeeded"
        call = app.state.agent_service.store.list_tool_calls(rid)[0]
        assert call.status == "failed" and call.error.error.code == "agent_mcp_tool_error"
        assert call.output["schema"] == "zhimeng.tool-result.spilled.v1"
        assert call.output["source_mcp"] is True
        assert call.output["read_only"] is True
        raw = json.loads(Path(call.output["spill_path"]).read_text(encoding="utf-8"))
        assert raw["isError"] is True
        assert raw["content"][1]["data"] == "synthetic-binary-placeholder"
        assert "尚未作为图片" in raw["content_note"]
        assert "synth-private-value" not in json.dumps(raw)


class CsvMcpProvider(ApiProvider):
    """Controlled choices, real API/Agent/SDK/pipes/external file effects."""
    def __init__(self, root):
        self.root = root
        self.connection_id = None
        self.step = 0
        self.summary = None
        self.outputs = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if tool_messages:
            envelope = json.loads(tool_messages[-1]["content"])
            self.outputs.append(envelope.get("output", envelope))
        if self.step == 0:
            name, arguments = tool_alias(self.connection_id, "read.csv"), {}
        elif self.step == 1:
            rows = json.loads(self.outputs[-1]["content"][0]["text"])
            totals = {}
            for row in rows:
                totals[row["department"]] = totals.get(row["department"], 0) + int(row["amount"])
            self.summary = "# 部门汇总\n" + "\n".join(f"{department}：{total}" for department, total in totals.items()) + "\n"
            name, arguments = tool_alias(self.connection_id, "write.summary"), {"text": self.summary}
        elif self.step == 2:
            name, arguments = "read_file", {"path": str(self.root / "summary.md")}
        else:
            assert "设计：30" in json.dumps(self.outputs[-1], ensure_ascii=False)
            yield ProviderTextDelta("已生成并读回核对：设计 30，工程 50。")
            yield ProviderCompleted("stop", None)
            return
        assert name in {item.name for item in tools}
        self.step += 1
        yield ProviderToolCall(0, f"csv-step-{self.step}", name, arguments)
        yield ProviderCompleted("tool_calls", None)


@pytest.mark.parametrize("mode", ["legacy", "modern"])
def test_settings_api_to_no_folder_agent_real_stdio_csv_summary_and_readback(settings, tmp_path, mode):
    from test_mcp_runtime import alive, events

    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_fixture.py"
    (tmp_path / "sample.csv").write_bytes("department,amount\n设计,10\n设计,20\n工程,50\n".encode("utf-8"))
    provider = CsvMcpProvider(tmp_path)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        base = "/api/v1/mcp/servers"
        created = client.post(base, json={"name": "合成 CSV 工具", "command": sys.executable,
                              "args": [str(fixture.resolve()), mode], "cwd": str(tmp_path)})
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        provider.connection_id = cid
        deadline = time.monotonic() + 12
        while True:
            view = client.get(base).json()["servers"][0]
            if view["state"] in {"ready", "error"}:
                break
            assert time.monotonic() < deadline, view
            time.sleep(0.03)
        assert view["state"] == "ready", view
        assert view["tool_count"] == 7
        details = client.get(base + "/" + cid + "/tools").json()["tools"]
        assert len(details) == 7
        assert any(item["raw_name"] == "echo.secret" for item in details)  # discovered on page 2
        sid = session(client)
        rid = start(client, sid, key="real-mcp-csv-" + mode)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        calls = client.app.state.agent_service.store.list_tool_calls(rid)
        assert len(calls) == 3 and all(item.status == "succeeded" for item in calls)
        assert (tmp_path / "summary.md").read_text(encoding="utf-8") == provider.summary
        assert provider.summary == "# 部门汇总\n设计：30\n工程：50\n"
        assert [item["call"] for item in events(tmp_path) if "call" in item] == ["read.csv", "write.summary"]
    first = events(tmp_path)[0]
    assert not alive(first["pid"]) and not alive(first["child_pid"])


class ProjectMcpProvider(ApiProvider):
    def __init__(self, outcome):
        self.outcome = outcome
        self.connection_id = None
        self.step = 0
        self.outputs = []
        self.path = "side-effects.txt" if outcome == "disconnected" else "summary.md"
        self.original = "once" if outcome == "disconnected" else "设计：30"

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if tool_messages:
            envelope = json.loads(tool_messages[-1]["content"])
            self.outputs.append(envelope.get("output", envelope))
        if self.step == 0:
            raw_name = "disconnect.write" if self.outcome == "disconnected" else "write.summary"
            name = tool_alias(self.connection_id, raw_name)
            arguments = {"text": "# 汇总\n设计：30\n", "fail": self.outcome == "error"}
        elif self.step == 1:
            assert self.outputs[-1]["result_revision"] >= 1
            name, arguments = "read_file", {"path": self.path}
        elif self.step == 2:
            assert self.original in json.dumps(self.outputs[-1], ensure_ascii=False)
            name, arguments = "edit_file", {"path": self.path, "find": self.original, "replace": "核对完成"}
        else:
            yield ProviderTextDelta("外部修改已读回并完成接续编辑。")
            yield ProviderCompleted("stop", None)
            return
        assert name in {item.name for item in tools}
        self.step += 1
        yield ProviderToolCall(0, f"project-mcp-{self.step}", name, arguments)
        yield ProviderCompleted("tool_calls", None)


@pytest.mark.parametrize("outcome", ["succeeded", "error", "disconnected"])
def test_real_project_mcp_write_refreshes_pin_for_builtin_read_and_edit(tmp_path, outcome):
    from test_agent_service import wait_terminal
    from test_mcp_runtime import ready
    from test_project_memory_scope import build_project
    from zhimeng_runtime.database import latest_own_write_pin
    from zhimeng_runtime.mcp_client_service import McpClientService
    from zhimeng_runtime.mcp_models import McpServerUpdate
    from zhimeng_runtime.mcp_settings import McpSettingsService

    provider = ProjectMcpProvider(outcome)
    service, binding, changes, project = build_project(tmp_path, provider)
    settings = McpSettingsService(tmp_path / "mcp-settings.json")
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_fixture.py"
    config = settings.upsert(McpServerUpdate(name="合成项目修改", command=sys.executable,
                             args=[str(fixture.resolve()), "legacy"], cwd=str(project)))
    manager = McpClientService(settings, tmp_path)
    service.tool_executor.mcp_adapter = McpToolAdapter(manager)
    provider.connection_id = config.connection_id

    async def scenario():
        try:
            await manager.start()
            await ready(manager)
            _, _, current = changes._snapshot(binding)
            session = await service.create_session(project_id=binding.project_id, title="普通项目外部工具")
            accepted = await service.start_run(session_id=session.session_id, message="生成汇总，读回来，然后编辑。",
                expected_project_revision=current.revision, expected_project_fingerprint=current.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="project-mcp-" + outcome)
            run = await wait_terminal(service, accepted.run.run_id)
            assert run.status == "succeeded", run
            calls = service.store.list_tool_calls(run.run_id)
            assert len(calls) == 3
            assert calls[0].status == ("succeeded" if outcome == "succeeded" else "failed")
            assert calls[0].output["source_mcp"] is True and calls[0].output["read_only"] is False
            assert calls[0].output["result_revision"] > current.revision
            assert calls[1].status == calls[2].status == "succeeded"
            with service.store.database.connection() as connection:
                assert latest_own_write_pin(connection, run.run_id) is not None
            assert "核对完成" in (project / provider.path).read_text(encoding="utf-8")
            if outcome == "disconnected":
                assert (project / "side-effects.txt").read_text(encoding="utf-8").count("核对完成") == 1
        finally:
            await service.shutdown()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("repeated_cancel", [False, True])
def test_cancelled_mcp_write_keeps_observed_project_pin_and_followup_reads_file(tmp_path, repeated_cancel):
    from test_agent_service import wait_terminal
    from test_mcp_runtime import events, ready, until
    from test_project_memory_scope import build_project
    from zhimeng_runtime.mcp_client_service import McpClientService
    from zhimeng_runtime.mcp_models import McpServerUpdate
    from zhimeng_runtime.mcp_settings import McpSettingsService

    class CancelProvider(ApiProvider):
        def __init__(self):
            self.connection_id = None
            self.phase = "write"

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            if self.phase == "write":
                self.phase = "read"
                yield ProviderToolCall(0, "cancel-external-write", tool_alias(self.connection_id, "write.summary"),
                                       {"text": "取消前已经写入。\n", "wait_after_write": True})
                yield ProviderCompleted("tool_calls", None)
            elif self.phase == "read":
                self.phase = "answer"
                yield ProviderToolCall(0, "followup-read", "read_file", {"path": "summary.md"})
                yield ProviderCompleted("tool_calls", None)
            else:
                result = [item for item in messages if item.get("role") == "tool"][-1]
                assert "取消前已经写入" in result["content"]
                yield ProviderTextDelta("当前文件确实包含取消前写入的内容。")
                yield ProviderCompleted("stop", None)

    provider = CancelProvider()
    service, binding, changes, project = build_project(tmp_path, provider)
    settings = McpSettingsService(tmp_path / "mcp-settings.json")
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_fixture.py"
    config = settings.upsert(McpServerUpdate(name="取消时已有修改", command=sys.executable,
                             args=[str(fixture.resolve()), "legacy"], cwd=str(project)))
    manager = McpClientService(settings, tmp_path)
    service.tool_executor.mcp_adapter = McpToolAdapter(manager)
    provider.connection_id = config.connection_id
    observe_entered = threading.Event()
    release_observe = threading.Event()
    original_observe = service.store.observe_cancelled_mcp_call
    if repeated_cancel:
        def held_observe(*args, **kwargs):
            observe_entered.set()
            assert release_observe.wait(10), "Test did not release the cancellation observation"
            return original_observe(*args, **kwargs)
        service.store.observe_cancelled_mcp_call = held_observe

    async def scenario():
        try:
            await manager.start()
            await ready(manager)
            _, _, current = changes._snapshot(binding)
            session = await service.create_session(project_id=binding.project_id, title="取消外部操作")
            accepted = await service.start_run(session_id=session.session_id, message="写完后等待。",
                expected_project_revision=current.revision, expected_project_fingerprint=current.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="mcp-cancel-write")
            await until(lambda: (project / "summary.md").exists())
            await service.cancel_run(accepted.run.run_id)
            if repeated_cancel:
                await until(observe_entered.is_set)
                await service.cancel_run(accepted.run.run_id)
                await asyncio.sleep(0)
                assert service.store.get_run(accepted.run.run_id).status == "cancelling"
                release_observe.set()
            cancelled = await wait_terminal(service, accepted.run.run_id)
            await until(lambda: not service.is_run_executing(cancelled.run_id))
            assert cancelled.status == "cancelled"
            output = service.store.list_tool_calls(cancelled.run_id)[0].output
            assert output["source_mcp"] is True and output["read_only"] is False
            assert output["cancelled"] is True and output["side_effects"] == "unconfirmed"
            assert output["result_revision"] > current.revision
            activities = [event.activity.root.model_dump(mode="json") for event in service.store.list_run_events(cancelled.run_id, after_sequence=0)]
            observation = [item["sequence"] for item in activities if item["kind"] == "tool_result"
                           and item["payload"]["output"].get("source_mcp") is True]
            terminal = [item["sequence"] for item in activities if item["kind"] == "run_state" and item["payload"]["status"] == "cancelled"]
            assert observation and terminal and max(observation) < min(terminal)
            assert (project / "summary.md").read_text(encoding="utf-8") == "取消前已经写入。\n"
            # Protocol cancellation is asynchronous; finish recording its fixture
            # audit event before pinning the next ordinary user turn.
            await until(lambda: any("cancelled" in item for item in events(project)))
            _, _, after = changes._snapshot(binding)
            followup = await service.start_run(session_id=session.session_id, message="读一下现在的文件。",
                expected_project_revision=after.revision, expected_project_fingerprint=after.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="mcp-cancel-followup")
            completed = await wait_terminal(service, followup.run.run_id)
            assert completed.status == "succeeded"
            assert service.store.list_tool_calls(completed.run_id)[0].status == "succeeded"
        finally:
            release_observe.set()
            await service.shutdown()
            await manager.shutdown()

    asyncio.run(scenario())
