"""Anthropic Messages 协议线：装配、头、thinking 映射、SSE 解码、指纹。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
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


def sse_events(*payloads: object) -> list[bytes]:
    return [
        f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n".encode()
        for payload in payloads
    ]


def anthropic_provider(
    captured: list[httpx.Request],
    stream_chunks: list[bytes],
    **overrides: Any,
) -> OpenAICompatibleAgentProvider:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(stream_chunks),
        )

    base = {
        "base_url": "https://provider.test/v1",
        "model": "claude-test",
        "api_key": "sk-ant-test",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "wire_format": "anthropic_messages",
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    }
    base.update(overrides)
    return OpenAICompatibleAgentProvider(**base)


def collect(provider, messages, tools=()):
    async def run() -> list:
        return [
            event
            async for event in provider.stream_chat(
                messages,
                cancel_event=asyncio.Event(),
                tools=tools,
            )
        ]

    return asyncio.run(run())


SIMPLE_STREAM = sse_events(
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "model": "claude-test",
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 5},
    },
    {"type": "message_stop"},
)


def tool_definition() -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name="project_read_file",
        version="1",
        description="Read a bounded range from one project file.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    )


# ---------------------------------------------------------------- 装配与头


def test_endpoint_and_default_headers() -> None:
    captured: list[httpx.Request] = []
    provider = anthropic_provider(captured, SIMPLE_STREAM)
    collect(provider, [{"role": "user", "content": "写一段"}])
    request = captured[0]
    assert request.url.path == "/v1/messages"
    # anthropic 线认证默认 x-api-key；版本头默认带上
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in request.headers
    assert request.headers["anthropic-version"] == "2023-06-01"


def test_anthropic_version_header_can_be_overridden_and_custom_auth_wins() -> None:
    captured: list[httpx.Request] = []
    provider = anthropic_provider(
        captured,
        SIMPLE_STREAM,
        auth_type="custom",
        extra_headers={
            "Authorization": "Bearer gateway-token",
            "anthropic-version": "2024-10-01",
        },
    )
    collect(provider, [{"role": "user", "content": "写一段"}])
    request = captured[0]
    assert request.headers["Authorization"] == "Bearer gateway-token"
    assert "x-api-key" not in request.headers
    assert request.headers["anthropic-version"] == "2024-10-01"


def test_system_hoisted_and_tool_history_converted() -> None:
    captured: list[httpx.Request] = []
    provider = anthropic_provider(captured, SIMPLE_STREAM)
    messages = [
        {"role": "system", "content": "你是写作搭档。"},
        {"role": "system", "content": "只聊这本书。"},
        {"role": "user", "content": "读一下第一章"},
        {
            "role": "assistant",
            "content": "我先读文件。",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "function": {
                        "name": "project_read_file",
                        "arguments": '{"file_id": "f1"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc123", "content": "第一章正文……"},
        {"role": "tool", "tool_call_id": "call_def456", "content": "另一个结果"},
        {"role": "user", "content": "继续"},
    ]
    collect(provider, messages, tools=[tool_definition()])
    payload = json.loads(captured[0].content)

    assert payload["system"] == [
        {
            "type": "text",
            "text": "你是写作搭档。\n\n只聊这本书。",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert payload["max_tokens"] == 1024  # 必填
    assert payload["stream"] is True
    assert payload["temperature"] == 0.2  # 未开 thinking 时照发
    assert "thinking" not in payload  # auto 不发 thinking 字段
    assert payload["tools"] == [
        {
            "name": "project_read_file",
            "description": "Read a bounded range from one project file.",
            "input_schema": tool_definition().parameters,
            # 缓存断点：工具清单最后一项
            "cache_control": {"type": "ephemeral"},
        }
    ]
    roles = [message["role"] for message in payload["messages"]]
    assert "system" not in roles
    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "text", "text": "我先读文件。"},
        {
            "type": "tool_use",
            "id": "call_abc123",
            "name": "project_read_file",
            "input": {"file_id": "f1"},
        },
    ]
    # 连续两条 tool 结果并进同一条 user 消息
    tool_results = payload["messages"][2]
    assert tool_results["role"] == "user"
    assert tool_results["content"] == [
        {"type": "tool_result", "tool_use_id": "call_abc123", "content": "第一章正文……"},
        {"type": "tool_result", "tool_use_id": "call_def456", "content": "另一个结果"},
    ]


@pytest.mark.parametrize(
    ("effort", "budget"),
    [("low", 1024), ("medium", 4096), ("high", 8192), ("xhigh", 16384), ("max", 32768)],
)
def test_thinking_budget_mapping(effort: str, budget: int) -> None:
    captured: list[httpx.Request] = []
    # max_tokens 放大到装得下 budget（官方要求 budget < max_tokens）
    provider = anthropic_provider(
        captured, SIMPLE_STREAM, reasoning_effort=effort, max_tokens=budget + 2048
    )
    collect(provider, [{"role": "user", "content": "写一段"}])
    payload = json.loads(captured[0].content)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": budget}
    # 开了 thinking 就不发 temperature（官方协议互斥）
    assert "temperature" not in payload


def test_thinking_budget_clamped_below_max_tokens() -> None:
    captured: list[httpx.Request] = []
    provider = anthropic_provider(
        captured, SIMPLE_STREAM, reasoning_effort="max", max_tokens=1024
    )
    collect(provider, [{"role": "user", "content": "写一段"}])
    payload = json.loads(captured[0].content)
    # 32768 收敛到 max_tokens-1=1023，仍低于下限 1024 → 干脆不发 thinking
    assert "thinking" not in payload


def test_thinking_budget_clamps_to_max_tokens_minus_one() -> None:
    captured: list[httpx.Request] = []
    provider = anthropic_provider(
        captured, SIMPLE_STREAM, reasoning_effort="ultra", max_tokens=4096
    )
    collect(provider, [{"role": "user", "content": "写一段"}])
    payload = json.loads(captured[0].content)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4095}


# ---------------------------------------------------------------- SSE 解码


def test_full_stream_maps_text_thinking_tools_usage_and_stop_reason() -> None:
    chunks = sse_events(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-reported",
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先想清楚"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "好"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "，"}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_01ABC", "name": "project_read_file", "input": {}},
        },
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"file_'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'id": "f1"}'}},
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    )
    provider = anthropic_provider([], chunks)
    events = collect(provider, [{"role": "user", "content": "读文件"}], tools=[tool_definition()])

    reasoning = [event.text for event in events if isinstance(event, ProviderReasoningDelta)]
    assert reasoning == ["先想清楚"]
    texts = [event.text for event in events if isinstance(event, ProviderTextDelta)]
    assert texts == ["好", "，"]
    tool_calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "project_read_file"
    assert tool_calls[0].tool_call_id == "toolu_01ABC"
    assert tool_calls[0].arguments == {"file_id": "f1"}
    completed = next(event for event in events if isinstance(event, ProviderCompleted))
    assert completed.finish_reason == "tool_calls"
    assert completed.reported_model == "claude-reported"
    assert completed.usage is not None
    assert completed.usage.input_tokens == 12
    assert completed.usage.output_tokens == 7
    assert completed.usage.total_tokens == 19


def test_max_tokens_stop_reason_maps_to_length() -> None:
    chunks = sse_events(
        {"type": "message_start", "message": {"model": "claude-test", "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "半"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "max_tokens"},
            "usage": {"output_tokens": 1024},
        },
        {"type": "message_stop"},
    )
    provider = anthropic_provider([], chunks)
    with pytest.raises(AgentProviderError) as caught:
        collect(provider, [{"role": "user", "content": "写"}])
    assert caught.value.code == "agent_provider_output_truncated"


def test_error_event_raises_error_event_code() -> None:
    chunks = sse_events(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    provider = anthropic_provider([], chunks)
    with pytest.raises(AgentProviderError) as caught:
        collect(provider, [{"role": "user", "content": "写"}])
    assert caught.value.code == "agent_provider_error_event"
    assert "Overloaded" in str(caught.value)


# ---------------------------------------------------------------- 指纹


def test_anthropic_fingerprint_is_distinct_and_stable() -> None:
    base = {
        "base_url": "https://provider.test/v1",
        "model": "claude-test",
        "api_key": "secret",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
    }
    chat = OpenAICompatibleAgentProvider(**base)
    responses = OpenAICompatibleAgentProvider(**base, wire_format="responses")
    anthropic = OpenAICompatibleAgentProvider(**base, wire_format="anthropic_messages")
    assert anthropic.identity.implementation == "openai-compatible-anthropic-messages"
    assert len(
        {
            chat.dispatch_config_fingerprint,
            responses.dispatch_config_fingerprint,
            anthropic.dispatch_config_fingerprint,
        }
    ) == 3
    again = OpenAICompatibleAgentProvider(**base, wire_format="anthropic_messages")
    assert again.dispatch_config_fingerprint == anthropic.dispatch_config_fingerprint


def test_cache_read_and_write_count_toward_total_input_so_the_hit_rate_is_real() -> None:
    """Anthropic 的 input_tokens 不含读缓存、写缓存两部分（2026-09-26 现状核查 B3）：
    以前拿它当总输入，读缓存 900 / 输入 50 被界面压成「命中 99%」；实际是 900 / 1000 = 90%。"""
    chunks = sse_events(
        {
            "type": "message_start",
            "message": {
                "id": "msg_cache",
                "model": "claude-test",
                "usage": {"input_tokens": 50, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "好"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    completed = next(event for event in collect(anthropic_provider([], chunks), [{"role": "user", "content": "在吗"}])
                     if isinstance(event, ProviderCompleted))
    assert completed.usage is not None
    assert completed.usage.input_tokens == 1000
    assert completed.usage.cached_input_tokens == 900
    assert completed.usage.total_tokens == 1003

    # 中转站把累计的输入和缓存数放在 message_delta 里时，以它为准。
    relayed = sse_events(
        {"type": "message_start", "message": {"id": "msg_relay", "model": "claude-test", "usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "好"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2, "input_tokens": 20, "cache_read_input_tokens": 180},
        },
        {"type": "message_stop"},
    )
    completed = next(event for event in collect(anthropic_provider([], relayed), [{"role": "user", "content": "在吗"}])
                     if isinstance(event, ProviderCompleted))
    assert completed.usage.input_tokens == 200
    assert completed.usage.cached_input_tokens == 180
