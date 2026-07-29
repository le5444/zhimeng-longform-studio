from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from zhimeng_runtime.agent_provider import (
    MAX_TOOL_ARGUMENT_BYTES,
    MAX_TOOL_CALLS_PER_RESPONSE,
    AgentProviderCancelled,
    AgentProviderError,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def __aiter__(self):
        self.started.set()
        await self.release.wait()
        yield b'data: {"choices": []}\n\n'


def sse(*payloads: object) -> list[bytes]:
    chunks: list[bytes] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        chunks.append(f"data: {data}\n\n".encode())
    return chunks


def provider_for(
    chunks: list[bytes],
    *,
    content_type: str = "text/event-stream",
    captured_requests: list[dict[str, Any]] | None = None,
    max_tool_calls_per_response: int = MAX_TOOL_CALLS_PER_RESPONSE,
    max_tool_argument_bytes: int = MAX_TOOL_ARGUMENT_BYTES,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        if captured_requests is not None:
            captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": content_type},
            stream=ChunkStream(chunks),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key="secret",
        credential_reference="env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
        max_tool_calls_per_response=max_tool_calls_per_response,
        max_tool_argument_bytes=max_tool_argument_bytes,
    )
    return provider, client


def tool_definition() -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name="project_read_file",
        version="1",
        description="Read a bounded range from one project file.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "file_id": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
            },
            "required": ["file_id"],
        },
    )


def test_provider_identity_binds_complete_dispatch_configuration_without_secret() -> None:
    base = {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "secret-one",
        "credential_reference": "secret-store:provider-primary",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "max_tool_calls_per_response": MAX_TOOL_CALLS_PER_RESPONSE,
        "max_tool_argument_bytes": MAX_TOOL_ARGUMENT_BYTES,
    }

    def identity(**changes: object):
        return OpenAICompatibleAgentProvider(**(base | changes)).identity

    baseline = identity()
    variants = [
        identity(base_url="https://other-provider.test/v1"),
        identity(model="other-model"),
        identity(credential_reference="secret-store:provider-secondary"),
        identity(timeout_seconds=31),
        identity(max_tokens=2048),
        identity(temperature=0.3),
        identity(max_tool_calls_per_response=MAX_TOOL_CALLS_PER_RESPONSE + 1),
        identity(max_tool_argument_bytes=MAX_TOOL_ARGUMENT_BYTES + 1),
        identity(accepted_reported_models=("gateway-model",)),
    ]

    assert baseline.instance_id is not None
    assert len({baseline.instance_id, *(item.instance_id for item in variants)}) == 10
    assert identity(api_key="secret-two") == baseline
    assert identity(base_url="https://provider.test/v1/") == baseline
    serialized = baseline.model_dump_json()
    assert "secret-one" not in serialized
    assert "provider.test" not in serialized


async def collect(
    provider: OpenAICompatibleAgentProvider,
    *,
    messages: Sequence[Mapping[str, Any]] | None = None,
    tools: Sequence[ProviderToolDefinition] = (),
    cancel_event: asyncio.Event | None = None,
) -> list[ProviderEvent]:
    return [
        event
        async for event in provider.stream_chat(
            messages or [{"role": "user", "content": "写一段"}],
            cancel_event=cancel_event or asyncio.Event(),
            tools=tools,
        )
    ]


def test_openai_compatible_provider_streams_text_and_terminal_usage() -> None:
    captured_requests: list[dict[str, Any]] = []
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "第一段"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "。"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 3,
                    "total_tokens": 11,
                },
            },
            "[DONE]",
        ),
        captured_requests=captured_requests,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == [
        "第一段",
        "。",
    ]
    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.finish_reason == "stop"
    assert completed.usage is not None
    assert completed.usage.total_tokens == 11
    assert "tools" not in captured_requests[0]
    assert "tool_choice" not in captured_requests[0]
    assert "parallel_tool_calls" not in captured_requests[0]


def test_provider_reports_one_consistent_sse_model_identity() -> None:
    provider, client = provider_for(
        sse(
            {
                "model": "test-model",
                "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
            },
            {
                "model": "test-model",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            "[DONE]",
        )
    )
    try:
        completed = next(
            event
            for event in asyncio.run(collect(provider))
            if isinstance(event, ProviderCompleted)
        )
    finally:
        asyncio.run(client.aclose())

    assert completed.reported_model == "test-model"


def test_provider_rejects_conflicting_sse_model_identities() -> None:
    provider, client = provider_for(
        sse(
            {
                "model": "test-model",
                "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
            },
            {
                "model": "other-model",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            "[DONE]",
        )
    )
    try:
        with pytest.raises(AgentProviderError) as conflict:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert conflict.value.code == "agent_provider_reported_model_conflict"


@pytest.mark.parametrize("reported_model", [123, "   ", "x" * 513])
def test_provider_rejects_invalid_reported_model_identity(reported_model: object) -> None:
    provider, client = provider_for(
        sse(
            {
                "model": reported_model,
                "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
            },
            "[DONE]",
        )
    )
    try:
        with pytest.raises(AgentProviderError) as invalid:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert invalid.value.code == "agent_provider_reported_model_invalid"


def test_provider_serializes_versioned_tools_and_reassembles_fragmented_call() -> None:
    captured_requests: list[dict[str, Any]] = []
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_",
                                    "type": "function",
                                    "function": {
                                        "name": "project_",
                                        "arguments": '{"file',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "001",
                                    "function": {
                                        "name": "read_",
                                        "arguments": '_id":"file-1",',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "file",
                                        "arguments": '"start_line":2}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 9,
                    "total_tokens": 26,
                },
            },
            "[DONE]",
        ),
        captured_requests=captured_requests,
    )
    messages = [
        {"role": "user", "content": "读取文件"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "prior_call",
                    "type": "function",
                    "function": {"name": "project_read_file", "arguments": "{}"},
                }
            ],
        },
    ]
    try:
        events = asyncio.run(
            collect(
                provider,
                messages=messages,
                tools=[tool_definition()],
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert len(events) == 2
    assert events[0] == ProviderToolCall(
        index=0,
        tool_call_id="call_001",
        tool_name="project_read_file",
        arguments={"file_id": "file-1", "start_line": 2},
    )
    completed = events[-1]
    assert isinstance(completed, ProviderCompleted)
    assert completed.finish_reason == "tool_calls"
    assert completed.usage is not None
    assert completed.usage.total_tokens == 26
    request = captured_requests[0]
    assert request["messages"] == messages
    assert request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is False
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "project_read_file",
                "description": "Read a bounded range from one project file.",
                "parameters": tool_definition().parameters,
            },
        }
    ]
    assert "version" not in request["tools"][0]["function"]


def test_provider_orders_multiple_tool_calls_by_index() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_0002",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": '{"file_id":"file-2"}',
                                    },
                                },
                                {
                                    "index": 0,
                                    "id": "call_0001",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": '{"file_id":"file-1"}',
                                    },
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        )
    )
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert [call.index for call in calls] == [0, 1]
    assert [call.tool_call_id for call in calls] == ["call_0001", "call_0002"]


def test_provider_accepts_repeated_and_fragmented_id_and_name_fields() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_",
                                    "type": "function",
                                    "function": {
                                        "name": "project_",
                                        "arguments": '{"file',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_",
                                    "function": {
                                        "name": "project_",
                                        "arguments": '_id":"file-1"',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "0001",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        )
    )
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    call = next(event for event in events if isinstance(event, ProviderToolCall))
    assert call.tool_call_id == "call_0001"
    assert call.tool_name == "project_read_file"
    assert call.arguments == {"file_id": "file-1"}


def test_provider_rejects_ambiguous_cumulative_id_name_snapshots() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_",
                                    "type": "function",
                                    "function": {
                                        "name": "project_",
                                        "arguments": '{"file',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0001",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": '_id":"file-1"',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_",
                                    "function": {
                                        "name": "project_",
                                        "arguments": "}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        )
    )
    try:
        with pytest.raises(AgentProviderError, match="ambiguous cumulative fragment"):
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())


def tool_call_stream(arguments: str, *, index: int = 0, call_id: str = "call_0001"):
    return sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "project_read_file",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        "[DONE]",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        '{"file_id":"a","file_id":"b"}',
        '{"filter":{"kind":"a","kind":"b"}}',
        "[]",
        "{",
        '{"weight":NaN}',
        '{"weight":1e9999}',
    ],
)
def test_provider_rejects_non_strict_tool_argument_objects(arguments: str) -> None:
    provider, client = provider_for(tool_call_stream(arguments))
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    assert error.value.code == "agent_provider_tool_arguments_invalid"
    assert error.value.retryable is False


def test_provider_rejects_tool_arguments_over_the_byte_limit() -> None:
    provider, client = provider_for(
        tool_call_stream('{"file_id":"long-value"}'),
        max_tool_argument_bytes=16,
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    assert error.value.code == "agent_provider_tool_arguments_too_large"
    assert error.value.retryable is False


def test_provider_rejects_tool_arguments_with_invalid_unicode() -> None:
    arguments = '{"value":"' + chr(0xD800) + '"}'
    provider, client = provider_for(tool_call_stream(arguments))
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    assert error.value.code == "agent_provider_tool_arguments_invalid"
    assert error.value.retryable is False


def test_provider_rejects_too_many_tool_calls_before_completion() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0001",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": "{}",
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_0002",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": "{}",
                                    },
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ),
        max_tool_calls_per_response=1,
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    assert error.value.code == "agent_provider_tool_call_limit_exceeded"
    assert error.value.retryable is False


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        (tool_call_stream("{}", index=1), "agent_provider_tool_call_invalid"),
        (
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "type": "function",
                                        "function": {
                                            "name": "project_read_file",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                "[DONE]",
            ),
            "agent_provider_tool_call_invalid",
        ),
        (
            sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": True,
                                        "id": "call_0001",
                                        "type": "function",
                                        "function": {
                                            "name": "project_read_file",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                "[DONE]",
            ),
            "agent_provider_protocol_error",
        ),
        (
            sse(
                {
                    "choices": [
                        {
                            "delta": {"tool_calls": None},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "[DONE]",
            ),
            "agent_provider_protocol_error",
        ),
    ],
)
def test_provider_rejects_structurally_invalid_tool_calls(
    chunks: list[bytes],
    code: str,
) -> None:
    provider, client = provider_for(chunks)
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == code


def test_provider_rejects_duplicate_completed_tool_call_ids() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": "same_call",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": "{}",
                                    },
                                }
                                for index in range(2)
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        )
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_tool_call_invalid"


@pytest.mark.parametrize("call_id", ["short", "x" * 129, "_invalid_id"])
def test_provider_rejects_tool_call_ids_outside_runtime_bounds(call_id: str) -> None:
    provider, client = provider_for(tool_call_stream("{}", call_id=call_id))
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_tool_call_invalid"


def test_provider_rejects_unadvertised_or_unsupplied_tool_calls() -> None:
    unknown_tool_chunks = tool_call_stream("{}")
    provider, client = provider_for(unknown_tool_chunks)
    try:
        with pytest.raises(AgentProviderError) as unsupported:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert unsupported.value.code == "agent_provider_tool_call_unsupported"

    provider, client = provider_for(unknown_tool_chunks)
    other_tool = tool_definition().model_copy(update={"name": "project_list_files"})
    try:
        with pytest.raises(AgentProviderError) as unknown:
            asyncio.run(collect(provider, tools=[other_tool]))
    finally:
        asyncio.run(client.aclose())
    assert unknown.value.code == "agent_provider_tool_call_unknown"


def test_provider_rejects_duplicate_advertised_tool_names_before_http() -> None:
    captured_requests: list[dict[str, Any]] = []
    provider, client = provider_for(
        tool_call_stream("{}"),
        captured_requests=captured_requests,
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(
                collect(
                    provider,
                    tools=[tool_definition(), tool_definition()],
                )
            )
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_tool_definition_invalid"
    assert captured_requests == []


def test_provider_honors_cancellation_before_emitting_events() -> None:
    captured_requests: list[dict[str, Any]] = []
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "不应到达"}, "finish_reason": "stop"}]},
            "[DONE]",
        ),
        captured_requests=captured_requests,
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    try:
        with pytest.raises(AgentProviderCancelled):
            asyncio.run(collect(provider, cancel_event=cancel_event))
    finally:
        asyncio.run(client.aclose())
    assert captured_requests == []


def test_provider_honors_cancellation_while_waiting_for_the_next_sse_line() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=BlockingStream(started, release),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleAgentProvider(
            base_url="https://provider.test/v1",
            model="test-model",
            api_key=None,
            credential_reference=None,
            timeout_seconds=30,
            max_tokens=1024,
            temperature=0.2,
            client=client,
        )
        cancel_event = asyncio.Event()
        task = asyncio.create_task(collect(provider, cancel_event=cancel_event))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            cancel_event.set()
            with pytest.raises(AgentProviderCancelled):
                await asyncio.wait_for(task, timeout=1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())


def test_provider_honors_cancellation_while_waiting_for_response_headers() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream(sse("[DONE]")),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleAgentProvider(
            base_url="https://provider.test/v1",
            model="test-model",
            api_key=None,
            credential_reference=None,
            timeout_seconds=30,
            max_tokens=1024,
            temperature=0.2,
            client=client,
        )
        cancel_event = asyncio.Event()
        task = asyncio.create_task(collect(provider, cancel_event=cancel_event))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            cancel_event.set()
            with pytest.raises(AgentProviderCancelled):
                await asyncio.wait_for(task, timeout=1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())


def test_provider_rejects_multiple_choices_in_one_stream_chunk() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "first"}, "finish_reason": None},
                    {"index": 1, "delta": {"content": "second"}, "finish_reason": "stop"},
                ]
            },
            "[DONE]",
        )
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_protocol_error"


def test_tool_definition_is_versioned_and_rejects_incompatible_names_or_schema() -> None:
    definition = tool_definition()
    assert definition.schema_id == "zhimeng.provider-tool-definition.v1"
    assert definition.version == "1"

    with pytest.raises(ValidationError):
        ProviderToolDefinition(
            name="project.read.file",
            version="1",
            description="Invalid compatible function name.",
            parameters={"type": "object"},
        )
    with pytest.raises(ValidationError):
        ProviderToolDefinition(
            name="project_read_file",
            version="1",
            description="Invalid root schema.",
            parameters={"type": "array"},
        )


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        (
            sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}, "[DONE]"),
            "agent_provider_empty_response",
        ),
        (
            sse({"choices": [{"delta": {"content": "未完"}, "finish_reason": None}]}),
            "agent_provider_incomplete_stream",
        ),
        (
            sse(
                {"choices": [{"delta": {"content": "截断"}, "finish_reason": "length"}]},
                "[DONE]",
            ),
            "agent_provider_output_truncated",
        ),
        (
            sse(
                {
                    "choices": [
                        {
                            "delta": {"content": "没有真实调用"},
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                "[DONE]",
            ),
            "agent_provider_tool_call_invalid",
        ),
        (
            sse(
                {
                    "choices": [
                        {
                            "delta": {"content": "旧协议"},
                            "finish_reason": "function_call",
                        }
                    ]
                },
                "[DONE]",
            ),
            "agent_provider_tool_call_unsupported",
        ),
        ([b"data: {not-json}\n\n"], "agent_provider_protocol_error"),
    ],
)
def test_openai_compatible_provider_rejects_false_success(
    chunks: list[bytes],
    code: str,
) -> None:
    provider, client = provider_for(chunks)
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == code


def test_openai_compatible_provider_rejects_non_sse_response() -> None:
    provider, client = provider_for([b"{}"], content_type="application/json")
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_protocol_error"
