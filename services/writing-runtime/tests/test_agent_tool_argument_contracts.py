"""The model-facing schema must describe the inputs the executor requires."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import httpx
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from zhimeng_runtime.agent_tools import CommandSessionArguments, ProjectWriteFileArguments, ProjectEditFileArguments, SkillReadFileArguments
from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider, ProviderToolCall, ProviderToolDefinition
from test_agent_provider import ChunkStream, sse


@pytest.mark.parametrize("body,valid", [
    ({}, False),
    ({"path": "report.md"}, False),
    ({"content": "report"}, False),
    ({"path": "report.md", "content": None}, False),
    ({"files": None}, False),
    ({"files": []}, False),
    ({"files": [{"path": "report.md"}]}, False),
    ({"path": "report.md", "content": "report"}, True),
    ({"path": "report.md", "content": ""}, True),
    ({"path": "report.md", "content": " \n", "files": None}, True),
    ({"files": [{"path": "report.md", "content": "report"}]}, True),
    ({"path": None, "content": None, "files": [{"path": "empty.txt", "content": ""}]}, True),
    ({"files": [{"path": "a.txt", "content": "A"}, {"path": "b.txt", "content": "B"}]}, True),
    ({"path": "a.txt", "content": "A", "files": [{"path": "b.txt", "content": "B"}]}, False),
])
def test_write_schema_and_executor_agree_on_complete_inputs(body, valid):
    schema = ProjectWriteFileArguments.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(body) is valid
    if valid:
        parsed = ProjectWriteFileArguments.model_validate(body)
        if "content" in body:
            assert parsed.content == body["content"]
    else:
        with pytest.raises(ValidationError):
            ProjectWriteFileArguments.model_validate(body)


@pytest.mark.parametrize("body,valid", [
    ({"action": "list"}, True),
    ({"action": "read"}, False),
    ({"action": "stop", "session_id": None}, False),
    ({"action": "read", "session_id": "cmd_example"}, True),
    ({"action": "read", "session_id": "cmd_example", "cursor": {"stdout": 24000, "stderr": 35}}, True),
    ({"action": "stop", "session_id": "cmd_example"}, True),
    ({"action": "input", "session_id": "cmd_example"}, False),
    ({"action": "input", "session_id": "cmd_example", "input": ""}, False),
    ({"action": "input", "session_id": "cmd_example", "input": "", "close_stdin": False}, False),
    ({"action": "input", "session_id": "cmd_example", "input": "hello\n"}, True),
    ({"action": "input", "session_id": "cmd_example", "input": "\n"}, True),
    ({"action": "input", "session_id": "cmd_example", "close_stdin": True}, True),
])
def test_command_schema_and_executor_agree_on_operation_inputs(body, valid):
    schema = CommandSessionArguments.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(body) is valid
    if valid:
        CommandSessionArguments.model_validate(body)
    else:
        with pytest.raises(ValidationError):
            CommandSessionArguments.model_validate(body)


@pytest.mark.parametrize("filename,model", [
    ("tool-edit-file-arguments.schema.json", ProjectEditFileArguments),
    ("tool-skill-read-file-arguments.schema.json", SkillReadFileArguments),
    ("tool-write-file-arguments.schema.json", ProjectWriteFileArguments),
    ("tool-command-session-arguments.schema.json", CommandSessionArguments),
])
def test_exported_tool_contract_matches_model(filename, model):
    root = Path(__file__).resolve().parents[3]
    actual = json.loads((root / "packages/contracts" / filename).read_bytes())
    expected = model.model_json_schema(by_alias=True)
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    expected["$id"] = f"https://zhimeng.local/contracts/{filename}"
    assert actual == expected


@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic_messages"])
@pytest.mark.parametrize("name,model,arguments", [
    ("write_file", ProjectWriteFileArguments, {"path": "report.md", "content": "合计 140.20 元\n余款 39.80 元"}),
    ("write_file", ProjectWriteFileArguments, {"path": "report.md"}),
    ("command_session", CommandSessionArguments, {"action": "read", "session_id": "cmd_example", "cursor": {"stdout": 24000, "stderr": 35}}),
])
@pytest.mark.anyio
async def test_protocol_preserves_argument_schema_and_streamed_fields(wire, name, model, arguments):
    raw = json.dumps(arguments, ensure_ascii=False)
    parts = [raw[:len(raw) // 2], raw[len(raw) // 2:]]
    call_id = "call-contract-123"
    if wire == "chat":
        events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]}}]}]
        events += [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": part}}]}}]} for part in parts]
        events += [{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, "[DONE]"]
    elif wire == "responses":
        events = [{"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name, "arguments": ""}}]
        events += [{"type": "response.function_call_arguments.delta", "output_index": 0, "delta": part} for part in parts]
        events += [{"type": "response.function_call_arguments.done", "output_index": 0, "arguments": raw}, {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "function_call"}]}}]
    else:
        events = [{"type": "message_start", "message": {"model": "test-model"}}, {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}}]
        events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": part}} for part in parts]
        events += [{"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}]
    captured = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(sse(*events)))

    schema = model.model_json_schema()
    definition = ProviderToolDefinition(name=name, version="1", description="Contract regression", parameters=schema)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleAgentProvider(base_url="https://provider.test/v1", model="test-model",
            api_key=None, credential_reference=None, timeout_seconds=30, max_tokens=1024, temperature=0.2,
            wire_format=wire, client=client)
        received = [event async for event in provider.stream_chat([{"role": "user", "content": "Save the report"}],
            cancel_event=asyncio.Event(), tools=(definition,))]
    advertised = captured[0]["tools"][0]
    if wire == "chat":
        advertised = advertised["function"]
    assert advertised["input_schema" if wire == "anthropic_messages" else "parameters"] == schema
    calls = [event for event in received if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_name == name
    assert calls[0].arguments == arguments
    assert calls[0].arguments_error is None
