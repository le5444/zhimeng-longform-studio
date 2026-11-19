"""Responses 线每个请求带 prompt_cache_key = 对话 ID（9-27 晚）。

作者的主力线路（中转站上的 grok-4.6）走的是 Responses 协议。xAI 官方文档：缓存按服务器存，Responses 接口靠请求体里的
prompt_cache_key 把同一场对话钉到同一台服务器（Chat 接口才用 x-grok-conv-id 头——9-27 下午试的正是这个头，看不出作用）。
9-27 的缓存考题里，书里那场开头一字没变、只在末尾追加的 16 次请求有 4 次几乎零命中，像是被分到了别的服务器。
Codex 每个 Responses 请求都带这个字段，值是会话 ID。上游点名不认这个字段时摘掉重试一次，不为它整轮失败。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from test_agent_provider import ChunkStream, collect, sse
from test_agent_service import TurnScriptedProvider, build_service, create_turn, wait_terminal
from zhimeng_runtime.agent_provider import (
    PROMPT_CACHE_KEY,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    ProviderTextDelta,
)
from zhimeng_runtime.execution_protocol import RunStatus

RESPONSES_OK = sse(
    {"type": "response.output_text.delta", "delta": "完成"},
    {"type": "response.completed", "response": {"status": "completed", "output": []}},
)
CHAT_OK = sse({"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]}, "[DONE]")


def _provider(wire_format: str, handler) -> tuple[OpenAICompatibleAgentProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1", model="test-model", api_key=None,
        credential_reference=None, timeout_seconds=30, max_tokens=1024,
        temperature=0.2, wire_format=wire_format, client=client,
    )
    return provider, client


def _run_with_key(provider, key: str | None):
    async def scenario():
        if key is not None:
            PROMPT_CACHE_KEY.set(key)
        return await collect(provider)

    return asyncio.run(scenario())


def test_responses_requests_carry_the_conversation_id_as_prompt_cache_key() -> None:
    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(RESPONSES_OK))

    provider, client = _provider("responses", handler)
    try:
        events = _run_with_key(provider, "session_abc")
    finally:
        asyncio.run(client.aclose())
    assert any(isinstance(event, ProviderCompleted) for event in events)
    assert captured[0]["prompt_cache_key"] == "session_abc"


def test_no_key_is_sent_outside_a_conversation_or_on_the_chat_wire() -> None:
    captured: list[dict[str, Any]] = []

    async def responses(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(RESPONSES_OK))

    async def chat(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(CHAT_OK))

    provider, client = _provider("responses", responses)
    try:
        _run_with_key(provider, None)  # 比如起标题、测活：不在哪场对话的运行里
    finally:
        asyncio.run(client.aclose())
    provider, client = _provider("chat", chat)
    try:
        _run_with_key(provider, "session_abc")
    finally:
        asyncio.run(client.aclose())
    assert "prompt_cache_key" not in captured[0]
    assert "prompt_cache_key" not in captured[1]


def test_an_upstream_that_rejects_the_field_gets_one_retry_without_it() -> None:
    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: prompt_cache_key"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream(RESPONSES_OK))

    provider, client = _provider("responses", handler)
    try:
        events = _run_with_key(provider, "session_abc")
    finally:
        asyncio.run(client.aclose())
    assert any(isinstance(event, ProviderCompleted) for event in events)
    assert len(captured) == 2
    assert captured[0]["prompt_cache_key"] == "session_abc"
    assert "prompt_cache_key" not in captured[1]


class KeyRecordingProvider(TurnScriptedProvider):
    """记下每次调模型时 PROMPT_CACHE_KEY 是什么（真 provider 从这里取值）。"""

    def __init__(self, turns) -> None:
        super().__init__(turns)
        self.keys: list[str | None] = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.keys.append(PROMPT_CACHE_KEY.get())
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def test_a_run_sets_its_own_conversation_id_for_every_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = KeyRecordingProvider([[ProviderTextDelta("好的。"), ProviderCompleted("stop", None)]])
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)
        assert run.status is RunStatus.SUCCEEDED
        # 只看第一个：这场运行调模型的那次（后面起标题的请求另算）。
        assert provider.keys[0] == session.session_id
        # 运行在自己的任务里设的值，不会漏到外面。
        assert PROMPT_CACHE_KEY.get() is None
        await service.shutdown()

    asyncio.run(scenario())
