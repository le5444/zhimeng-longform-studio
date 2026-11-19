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
    AgentProviderCancelled,
    AgentProviderError,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    ProviderEvent,
    ProviderReasoningDelta,
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


def test_empty_network_exception_keeps_its_type_and_diagnostics() -> None:
    async def handler(request):
        raise httpx.ConnectError('', request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleAgentProvider(base_url='https://provider.test/v1', model='test-model',
                api_key='synthetic-key', credential_reference='test', timeout_seconds=30, max_tokens=1024, temperature=0.2, client=client)
            with pytest.raises(AgentProviderError) as caught:
                await collect(provider)
            assert str(caught.value) == 'Provider request failed: ConnectError'
            assert caught.value.code == 'agent_provider_request_failed'
            assert caught.value.details['network_error_type'] == 'ConnectError'
    asyncio.run(scenario())


def test_provider_identity_binds_complete_dispatch_configuration_without_secret() -> None:
    base = {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "secret-one",
        "credential_reference": "secret-store:provider-primary",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
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
        identity(max_tool_argument_bytes=MAX_TOOL_ARGUMENT_BYTES + 1),
        identity(accepted_reported_models=("gateway-model",)),
    ]

    assert baseline.instance_id is not None
    assert len({baseline.instance_id, *(item.instance_id for item in variants)}) == 9
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


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_openai_compatible_provider_keeps_reasoning_separate_from_answer(field: str) -> None:
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {field: "先核对章节设定。"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "正文答案"}, "finish_reason": "stop"}]},
            "[DONE]",
        )
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert [event.text for event in events if isinstance(event, ProviderReasoningDelta)] == [
        "先核对章节设定。"
    ]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["正文答案"]


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
    assert "parallel_tool_calls" not in request
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


def test_provider_takes_the_last_value_for_duplicate_argument_keys() -> None:
    """真实模型在长参数里会把同一个键写两遍（本次真书验收抓到 kind 重复）。
    按 JSON 通行语义取最后一个值，与 codex/opencode/OpenAI SDK 一致。"""
    provider, client = provider_for(tool_call_stream('{"file_id":"a","file_id":"b"}'))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert calls[0].arguments == {"file_id": "b"}


@pytest.mark.parametrize(
    "arguments",
    [
        "[]",
        "{",
        '{"weight":NaN}',
        '{"weight":1e9999}',
    ],
)
def test_provider_marks_non_strict_tool_arguments_for_resend(arguments: str) -> None:
    """参数不是合法 JSON 不再整轮判死：调用带着 arguments_error 照常进场，
    由服务层把「请重发这次调用」作为工具结果回喂模型（计入失败计数）。"""
    provider, client = provider_for(tool_call_stream(arguments))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].arguments_error is not None


def test_provider_marks_gateway_truncated_tool_arguments_for_resend() -> None:
    """真实事故：网关把整章正文写进工具参数时剪断，却照常收尾。
    截断的参数同样不判整轮失败：调用带 arguments_error 进场，回喂模型重发。"""
    provider, client = provider_for(
        tool_call_stream('{"file_id":"a","after_content":"# 第六章\n\n正文写到一半')
    )
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].arguments_error is not None
    assert "cut the tool arguments short" in calls[0].arguments_error


def test_tool_calls_in_an_answer_cut_by_the_output_limit_are_not_run() -> None:
    """9-27（照 Pi agent-loop.ts:468-500）：写工具调用时撞上单次输出上限，参数多半没写完。
    这一条里的调用都带着原因进场（服务层回给模型、不执行），不再整轮判失败，
    也不再当成「正文写到半句」让模型在对话里接着写。"""
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "好，我把第五章写进文件。"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_length_0001",
                                    "type": "function",
                                    "function": {"name": "project_read_file", "arguments": '{"file_id":"a"}'},
                                }
                            ]
                        },
                        "finish_reason": "length",
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
    assert len(calls) == 1
    # 参数就算恰好能解析也不执行：截断的参数可能能过校验、内容却不全。
    assert calls[0].arguments == {}
    assert "单次输出的长度上限（1024 tokens）" in (calls[0].arguments_error or "")
    assert "分几次写" in (calls[0].arguments_error or "")
    completed = [event for event in events if isinstance(event, ProviderCompleted)]
    assert completed and completed[-1].finish_reason == "length"


def test_text_cut_by_the_output_limit_still_reports_truncation() -> None:
    provider, client = provider_for(
        sse({"choices": [{"delta": {"content": "故事从旧港开始，"}, "finish_reason": "length"}]}, "[DONE]")
    )
    try:
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert caught.value.code == "agent_provider_output_truncated"


def test_provider_marks_tool_arguments_over_the_byte_limit_for_resend() -> None:
    """参数超兜底上限也不判整轮失败：调用带 arguments_error 进场，
    由服务层回喂模型「缩短或分次重发」。"""
    provider, client = provider_for(
        tool_call_stream('{"file_id":"long-value"}'),
        max_tool_argument_bytes=16,
    )
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].arguments_error is not None
    assert "大小上限" in calls[0].arguments_error


def test_provider_marks_tool_arguments_with_invalid_unicode_for_resend() -> None:
    arguments = '{"value":"' + chr(0xD800) + '"}'
    provider, client = provider_for(tool_call_stream(arguments))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].arguments_error is not None
    assert "Unicode" in calls[0].arguments_error


def test_provider_accepts_a_response_with_many_tool_calls() -> None:
    """单次回复的工具调用数不再设判死上限：「读一批文件再动手」的正常并行批
    照常进场；防失控由 Run 级总次数兜底（打满切收尾轮，不判失败）。"""
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
    )
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 2
    assert all(call.arguments_error is None for call in calls)


def test_provider_synthesizes_an_id_when_the_relay_omits_it() -> None:
    provider, client = provider_for(
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
                                        "arguments": '{"file_id":"a"}',
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
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_call_id.startswith("auto0x")
    assert calls[0].arguments == {"file_id": "a"}


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
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


def test_provider_accepts_non_zero_based_tool_call_index() -> None:
    # OpenAI-compatible relays sometimes number a single tool call from 1 rather
    # than 0. The reported index only orders the calls, so it is renumbered
    # densely instead of failing an otherwise valid response. Structural
    # problems (missing id, duplicate id, non-integer index) are still refused.
    provider, client = provider_for(tool_call_stream("{}", index=1))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    assert [event for event in events if isinstance(event, ProviderToolCall)] == [
        ProviderToolCall(
            index=0,
            tool_call_id="call_0001",
            tool_name="project_read_file",
            arguments={},
        )
    ]


def test_provider_retries_without_temperature_when_provider_deprecates_it() -> None:
    # Claude-class models reachable through OpenAI-compatible relays answer
    # HTTP 400 "temperature is deprecated for this model". The provider must
    # drop that one parameter and retry once so those models stay usable; the
    # retry happens before any event is yielded, so no partial stream shows up.
    captured_requests: list[dict[str, Any]] = []
    error_body = (
        '{"error":{"message":"{\\"type\\":\\"error\\",\\"error\\":{'
        '\\"type\\":\\"invalid_request_error\\",\\"message\\":'
        '\\"`temperature` is deprecated for this model.\\"}}"}}'
    )
    response_queue = [
        httpx.Response(400, content=error_body.encode("utf-8")),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "好的"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return response_queue.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="claude-test",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert len(captured_requests) == 2
    assert captured_requests[0]["temperature"] == 0.2
    assert "temperature" not in captured_requests[1]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["好的"]


def test_provider_sends_reasoning_effort_and_falls_back_when_relay_rejects_it() -> None:
    captured_requests: list[dict[str, Any]] = []
    response_queue = [
        httpx.Response(
            400,
            content=b'{"error":{"message":"reasoning_effort is unsupported"}}',
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return response_queue.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="reasoning-model",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        reasoning_effort="high",
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert captured_requests[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in captured_requests[1]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["完成"]


def test_provider_downgrades_max_reasoning_to_high_before_dropping() -> None:
    captured_requests: list[dict[str, Any]] = []
    response_queue = [
        httpx.Response(
            400,
            content=b'{"error":{"message":"Invalid reasoning_effort value: max is unrecognized"}}',
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "兼容请求完成"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return response_queue.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="domestic-glm-model",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        reasoning_effort="max",
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert captured_requests[0]["reasoning_effort"] == "max"
    assert captured_requests[1]["reasoning_effort"] == "high"
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["兼容请求完成"]


def test_provider_retries_http_429_before_yielding_events() -> None:
    captured_status: list[int] = []
    busy_body = (
        b'{"error":{"message":"No deployments available for selected model, '
        b'Try again in 5 seconds. Passed model=claude-sonnet-5."}}'
    )
    response_queue = [
        httpx.Response(429, headers={"retry-after": "0"}, content=busy_body),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "改好了"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = response_queue.pop(0)
        captured_status.append(response.status_code)
        return response

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="claude-sonnet-5",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert captured_status == [429, 200]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["改好了"]


def test_provider_retries_http_502_before_yielding_events() -> None:
    captured_status: list[int] = []
    response_queue = [
        httpx.Response(
            502,
            headers={"retry-after": "0"},
            content=b'{"error":{"message":"upstream_error","code":"upstream_error"}}',
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "改好了"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = response_queue.pop(0)
        captured_status.append(response.status_code)
        return response

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="gpt-5.6-luna",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert captured_status == [502, 200]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["改好了"]


def test_provider_retries_cloudflare_524_gateway_timeouts() -> None:
    """套 CF 的中转站对首包慢的模型回 524（真书验收 deepseek-v4-pro 撞上）。
    重试而不是判死，且这一档最多试 2 次。"""
    captured_status: list[int] = []
    response_queue = [
        httpx.Response(524, content=b"<html>A timeout occurred</html>"),
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "改好了"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = response_queue.pop(0)
        captured_status.append(response.status_code)
        return response

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="deepseek-v4-pro",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert captured_status == [524, 200]
    assert [event.text for event in events if isinstance(event, ProviderTextDelta)] == ["改好了"]


def test_provider_stops_retrying_524_after_two_attempts() -> None:
    captured: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(524)
        return httpx.Response(524, content=b"<html>A timeout occurred</html>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="deepseek-v4-pro",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert len(captured) == 3  # 首次 + 两次重试
    assert error.value.code == "agent_provider_http_error"
    assert error.value.retryable is True


def test_provider_does_not_retry_other_http_errors() -> None:
    captured_requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(400, content=b'{"error":{"message":"bad request"}}')

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(30.0),
    )
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key="secret",
        credential_reference="secret-store:provider-primary",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        client=client,
    )
    try:
        with pytest.raises(AgentProviderError, match="HTTP 400"):
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    assert len(captured_requests) == 1


def test_provider_deduplicates_repeated_tool_call_ids_with_synthetic_ids() -> None:
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
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 2
    assert calls[0].tool_call_id == "same_call"
    assert calls[1].tool_call_id.startswith("auto1x")
    assert calls[0].tool_call_id != calls[1].tool_call_id


@pytest.mark.parametrize("call_id", ["short", "_invalid_id", "1"])
def test_provider_synthesizes_stable_ids_for_out_of_bounds_tool_call_ids(
    call_id: str,
) -> None:
    provider, client = provider_for(tool_call_stream("{}", call_id=call_id))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_call_id.startswith("auto0x")
    import re as _re

    assert _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", calls[0].tool_call_id)


def test_provider_rejects_tool_calls_when_no_tools_were_supplied() -> None:
    provider, client = provider_for(tool_call_stream("{}"))
    try:
        with pytest.raises(AgentProviderError) as unsupported:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert unsupported.value.code == "agent_provider_tool_call_unsupported"


def test_provider_passes_unadvertised_tool_calls_through_for_service_recovery() -> None:
    provider, client = provider_for(tool_call_stream("{}"))
    other_tool = tool_definition().model_copy(update={"name": "project_list_files"})
    try:
        events = asyncio.run(collect(provider, tools=[other_tool]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_name == "project_read_file"


def test_provider_accepts_tool_calls_reported_with_a_stop_finish_reason() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_stop_finish_0001",
                                    "type": "function",
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": '{"file_id":"a"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "stop",
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
    completions = [event for event in events if isinstance(event, ProviderCompleted)]
    assert len(calls) == 1
    assert calls[0].arguments == {"file_id": "a"}
    assert completions[-1].finish_reason == "stop"


def test_provider_accepts_empty_and_fenced_tool_arguments() -> None:
    provider, client = provider_for(tool_call_stream(""))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert calls[0].arguments == {}

    fenced = "```json\n{\"file_id\":\"a\"}\n```"
    provider, client = provider_for(tool_call_stream(fenced))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert calls[0].arguments == {"file_id": "a"}


def test_provider_accepts_raw_control_characters_inside_argument_strings() -> None:
    """gpt-5.6-luna 经真实中转站提交整章正文时，字符串里带裸换行。
    只放宽字符串内控制字符；重复键等仍拒。"""
    raw_arguments = '{"content":"第一段。\n\n第二段带\t制表符。"}'
    provider, client = provider_for(tool_call_stream(raw_arguments))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {"content": "第一段。\n\n第二段带\t制表符。"}


def test_provider_accepts_null_tool_call_type_fragments() -> None:
    """deepseek 经 new-api 网关的流式片段把 type 发成 null；当作 function。"""
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_null_type_0001",
                                    "type": None,
                                    "function": {
                                        "name": "project_read_file",
                                        "arguments": '{"file_id":"a"}',
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
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {"file_id": "a"}


def test_provider_normalizes_namespaced_tool_names() -> None:
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_namespaced_0001",
                                    "type": "function",
                                    "function": {
                                        "name": "functions.project_read_file",
                                        "arguments": '{"file_id":"a"}',
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
    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert calls[0].tool_name == "project_read_file"


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


def test_chat_wire_accepts_tool_call_delta_with_null_name_fragment() -> None:
    """中转网关（new-api / kittyrouter 这类）把工具调用流拆成多段时，
    后续增量的 `function.name` 会发成 JSON null 或干脆漏发。这次的功能增量
    应该被跳过、不是协议错——作者 10-03 撞过，glm-5.3-flash 被它整轮判死。"""
    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_001",
                                "type": "function",
                                "function": {
                                    "name": "project_read_file",
                                    "arguments": "",
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
                                "function": {"name": None, "arguments": '{"file_id": "f-1"}'},
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
                                "type": "function",
                                "function": {"arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        "[DONE]",
    ]
    provider, client = provider_for(sse(*payloads))
    try:
        events = asyncio.run(collect(provider, tools=[tool_definition()]))
    finally:
        asyncio.run(client.aclose())

    tool_calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_call_id == "call_001"
    assert tool_calls[0].tool_name == "project_read_file"
    assert tool_calls[0].arguments == {"file_id": "f-1"}
    completed = events[-1]
    assert isinstance(completed, ProviderCompleted)


def test_chat_wire_still_rejects_tool_call_delta_with_non_string_name() -> None:
    """增量 name 是**非字符串的非空值**（数字、对象）——这是协议错，不是「没发名字」。"""
    provider, client = provider_for(
        sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {"name": 42, "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
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
    assert error.value.code == "agent_provider_protocol_error"


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


def test_provider_treats_text_only_tool_calls_finish_as_a_normal_reply() -> None:
    """中转站偶尔把纯文本回复的 finish_reason 标成 tool_calls。
    只要有正文就当普通回复收下，不再终止。"""
    provider, client = provider_for(
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
        )
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    texts = [event for event in events if isinstance(event, ProviderTextDelta)]
    assert texts and texts[0].text == "没有真实调用"
    assert not [event for event in events if isinstance(event, ProviderToolCall)]


def test_openai_compatible_provider_rejects_non_sse_response() -> None:
    provider, client = provider_for([b"{}"], content_type="application/json")
    try:
        with pytest.raises(AgentProviderError) as error:
            asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    assert error.value.code == "agent_provider_protocol_error"


def test_openai_compatible_provider_captures_prefix_cache_hit_tokens() -> None:
    """DeepSeek/OpenAI 的前缀缓存命中 token 必须入账：作者要看得见省了多少钱。"""
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "回答"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 5,
                    "total_tokens": 1005,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                },
            },
            "[DONE]",
        ),
        captured_requests=None,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.usage is not None
    assert completed.usage.input_tokens == 1000
    assert completed.usage.cached_input_tokens == 800


def test_openai_compatible_provider_captures_openai_style_cached_tokens() -> None:
    """OpenAI 系把命中藏在 prompt_tokens_details.cached_tokens 里，同样入账。"""
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "回答"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 4,
                    "total_tokens": 504,
                    "prompt_tokens_details": {"cached_tokens": 400},
                },
            },
            "[DONE]",
        ),
        captured_requests=None,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.usage is not None
    assert completed.usage.cached_input_tokens == 400


def test_openai_compatible_provider_usage_without_cache_stays_none() -> None:
    """不支持缓存的线路：字段保持 None，不伪造 0。"""
    provider, client = provider_for(
        sse(
            {"choices": [{"delta": {"content": "回答"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
            "[DONE]",
        ),
        captured_requests=None,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())

    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.usage is not None
    assert completed.usage.cached_input_tokens is None


@pytest.mark.parametrize("wire_format", ["chat", "responses"])
@pytest.mark.parametrize("requested_effort", ["max", "xhigh", "ultra"])
@pytest.mark.parametrize("reject_count", [0, 1, 2])
def test_completion_reports_final_request_effort_after_compatibility_retries(
    wire_format: str, requested_effort: str, reject_count: int,
) -> None:
    captured: list[dict[str, Any]] = []
    key = "reasoning" if wire_format == "responses" else "reasoning_effort"

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if len(captured) <= reject_count:
            return httpx.Response(400, json={"error": {"message": f"Unsupported {key} value"}})
        chunks = (
            sse(
                {"type": "response.output_text.delta", "delta": "完成"},
                {"type": "response.completed", "response": {"status": "completed", "output": []}},
            )
            if wire_format == "responses" else
            sse({"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]}, "[DONE]")
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(chunks))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1", model="test-model", api_key=None,
        credential_reference=None, timeout_seconds=30, max_tokens=1024,
        temperature=0.2, reasoning_effort=requested_effort, wire_format=wire_format,
        client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    requested_values = [
        body.get("reasoning", {}).get("effort") if wire_format == "responses"
        else body.get("reasoning_effort") for body in captured
    ]
    assert requested_values == [requested_effort, "high", None][:reject_count + 1]
    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.effective_reasoning_effort == requested_values[-1]
    assert completed.effective_thinking_budget_tokens is None
    # A successful fallback must not silently change the author's preference.
    assert provider.reasoning_effort == requested_effort


@pytest.mark.parametrize("requested_effort, reported_effort", [("auto", None), ("none", "none")])
def test_unspecified_effort_is_distinct_from_explicit_disable(
    requested_effort: str, reported_effort: str | None,
) -> None:
    provider, client = provider_for(
        sse({"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]}, "[DONE]")
    )
    provider.reasoning_effort = requested_effort
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.effective_reasoning_effort == reported_effort


@pytest.mark.parametrize("max_tokens, expected_budget", [(1024, None), (4096, 4095), (40000, 32768)])
def test_anthropic_completion_reports_sent_budget_instead_of_effort_label(
    max_tokens: int, expected_budget: int | None,
) -> None:
    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        chunks = sse(
            {"type": "message_start", "message": {"model": "claude-test"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "完成"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(chunks))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1", model="claude-test", api_key=None,
        credential_reference=None, timeout_seconds=30, max_tokens=max_tokens,
        temperature=0.2, reasoning_effort="max", wire_format="anthropic_messages", client=client,
    )
    try:
        events = asyncio.run(collect(provider))
    finally:
        asyncio.run(client.aclose())
    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.effective_reasoning_effort is None
    assert completed.effective_thinking_budget_tokens == expected_budget
    assert captured[0].get("thinking", {}).get("budget_tokens") == expected_budget
