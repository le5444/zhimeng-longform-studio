"""transport 韧性：熔断器、并发限流、max_retries 覆盖、流式空闲超时、401 诊断。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderCancelled,
    AgentProviderError,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    ProviderEvent,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class StalledStream(httpx.AsyncByteStream):
    """吐一段之后永远不再出字：模拟挂死的 SSE 流。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        await asyncio.Event().wait()


def sse(*payloads: object) -> list[bytes]:
    return [
        f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n".encode()
        for payload in payloads
    ]


OK_CHUNKS = sse(
    {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
    "[DONE]",
)


def make_provider(
    handler,
    **overrides: Any,
) -> OpenAICompatibleAgentProvider:
    base = {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "sk-live-1234567890abcdef",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    }
    base.update(overrides)
    return OpenAICompatibleAgentProvider(**base)


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=ChunkStream(OK_CHUNKS),
    )


async def collect(provider: OpenAICompatibleAgentProvider) -> list[ProviderEvent]:
    return [
        event
        async for event in provider.stream_chat(
            [{"role": "user", "content": "写一段"}],
            cancel_event=asyncio.Event(),
        )
    ]


def status_handler_factory(status: int, body: str = "boom", counter: list[int] | None = None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(1)
        return httpx.Response(status, text=body)

    return handler


# ---------------------------------------------------------------- 熔断器


def test_circuit_opens_after_threshold_and_fails_fast() -> None:
    calls: list[int] = []
    provider = make_provider(
        status_handler_factory(500, counter=calls),
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_seconds=60,
    )
    for _attempt in range(2):
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.code == "agent_provider_http_error"
    assert len(calls) == 2

    # 开路：快速失败，不再打上游
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    assert caught.value.code == "agent_provider_circuit_open"
    assert caught.value.retryable is True
    assert "秒后再试" in str(caught.value)
    assert caught.value.details["consecutive_failures"] == 2
    assert len(calls) == 2


def test_settings_probe_does_not_trip_or_clear_the_conversation_breaker(tmp_path):
    from zhimeng_runtime.provider_settings_service import ProviderSettingsService

    service = ProviderSettingsService(tmp_path / 'provider.json')
    probe = service._probe_provider_for(
        provider_id='shared', base='https://provider.test/v1', key='test-key',
        model='test-model', timeout_seconds=10, max_tokens=100,
    )
    probe._client = httpx.AsyncClient(transport=httpx.MockTransport(status_handler_factory(500)))
    conversation = make_provider(ok_handler, provider_id='shared', circuit_breaker_threshold=1)
    try:
        for _ in range(2):
            with pytest.raises(AgentProviderError) as failed_probe:
                asyncio.run(collect(probe))
            assert failed_probe.value.code == 'agent_provider_http_error'
        assert any(isinstance(event, ProviderCompleted) for event in asyncio.run(collect(conversation)))
        failing = make_provider(status_handler_factory(500), provider_id='shared', circuit_breaker_threshold=1)
        try:
            with pytest.raises(AgentProviderError):
                asyncio.run(collect(failing))
        finally:
            asyncio.run(failing._client.aclose())
        asyncio.run(probe._client.aclose())
        probe._client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
        assert any(isinstance(event, ProviderCompleted) for event in asyncio.run(collect(probe)))
        with pytest.raises(AgentProviderError) as still_open:
            asyncio.run(collect(conversation))
        assert still_open.value.code == 'agent_provider_circuit_open'
    finally:
        asyncio.run(probe._client.aclose())
        asyncio.run(conversation._client.aclose())


def test_circuit_does_not_count_auth_or_client_errors() -> None:
    calls: list[int] = []
    provider = make_provider(
        status_handler_factory(401, counter=calls),
        circuit_breaker_threshold=2,
    )
    for _attempt in range(4):
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.code == "agent_provider_http_error"
    # 401 是配置错误不是上游故障，连续多少次都不开路
    assert len(calls) == 4


def test_circuit_half_open_probe_closes_on_success() -> None:
    state = {"fail": True}
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if state["fail"]:
            return httpx.Response(500, text="boom")
        return ok_handler(request)

    provider = make_provider(
        handler,
        circuit_breaker_threshold=1,
        circuit_breaker_cooldown_seconds=0.05,
    )
    with pytest.raises(AgentProviderError):
        asyncio.run(collect(provider))
    # 冷却后半开：试探请求真的打到了上游，且成功后闭合复位
    state["fail"] = False

    async def probe_and_confirm() -> None:
        await asyncio.sleep(0.06)
        events = await collect(provider)
        assert any(isinstance(event, ProviderCompleted) for event in events)
        # 已闭合：下一次也正常走
        events = await collect(provider)
        assert any(isinstance(event, ProviderCompleted) for event in events)

    asyncio.run(probe_and_confirm())
    assert len(calls) == 3


def test_circuit_half_open_probe_failure_reopens() -> None:
    calls: list[int] = []
    provider = make_provider(
        status_handler_factory(500, counter=calls),
        circuit_breaker_threshold=1,
        circuit_breaker_cooldown_seconds=0.05,
    )
    with pytest.raises(AgentProviderError):
        asyncio.run(collect(provider))

    async def probe() -> None:
        await asyncio.sleep(0.06)
        with pytest.raises(AgentProviderError) as caught:
            await collect(provider)
        assert caught.value.code == "agent_provider_http_error"
        # 试探失败重新开路：下一次立刻快速失败
        with pytest.raises(AgentProviderError) as caught_again:
            await collect(provider)
        assert caught_again.value.code == "agent_provider_circuit_open"

    asyncio.run(probe())
    assert len(calls) == 2


# ---------------------------------------------------------------- 并发限流


def test_concurrency_limit_queues_excess_requests() -> None:
    running = {"current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        running["current"] += 1
        running["peak"] = max(running["peak"], running["current"])
        try:
            await asyncio.sleep(0.02)
            return ok_handler(request)
        finally:
            running["current"] -= 1

    provider = make_provider(handler, max_concurrent_requests=2)

    async def fire() -> None:
        await asyncio.gather(*(collect(provider) for _ in range(5)))

    asyncio.run(fire())
    # 5 个请求同一个 provider，任意时刻在打开+读取阶段的不超过 2 个
    assert running["peak"] == 2


def test_queued_request_can_be_cancelled() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        # 第一个请求一直占着额度（流不出字），直到测试结束
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=StalledStream(OK_CHUNKS[:1]),
        )

    provider = make_provider(handler, max_concurrent_requests=1)

    async def scenario() -> None:
        first_cancel = asyncio.Event()
        first_task = asyncio.create_task(
            asyncio.wait_for(_collect_with_cancel(provider, first_cancel), timeout=5)
        )
        # 等第一个请求确实占住了额度
        await asyncio.sleep(0.1)
        second_cancel = asyncio.Event()
        second_task = asyncio.create_task(
            _collect_with_cancel(provider, second_cancel)
        )
        await asyncio.sleep(0.05)
        second_cancel.set()
        with pytest.raises(AgentProviderCancelled):
            await second_task
        first_cancel.set()
        with pytest.raises(AgentProviderCancelled):
            await first_task

    asyncio.run(scenario())


async def _collect_with_cancel(
    provider: OpenAICompatibleAgentProvider,
    cancel_event: asyncio.Event,
) -> list[ProviderEvent]:
    return [
        event
        async for event in provider.stream_chat(
            [{"role": "user", "content": "写一段"}],
            cancel_event=cancel_event,
        )
    ]


# ---------------------------------------------------------------- max_retries 覆盖


def test_max_retries_zero_disables_busy_retries() -> None:
    calls: list[int] = []
    provider = make_provider(
        status_handler_factory(429, counter=calls),
        max_retries=0,
    )
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    assert caught.value.code == "agent_provider_http_error"
    assert len(calls) == 1


def test_max_retries_override_changes_retry_budget() -> None:
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="cooldown", headers={"retry-after": "0"})

    provider = make_provider(handler, max_retries=1)
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    assert caught.value.code == "agent_provider_http_error"
    # 默认会重试 4 次（共 5 次请求）；配了 1 就只重试 1 次
    assert len(calls) == 2
    assert caught.value.details["retry_count"] == 1


# ---------------------------------------------------------------- 流式空闲超时


def test_stream_idle_timeout_fires_on_stalled_stream() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=StalledStream(OK_CHUNKS[:1]),
        )

    provider = make_provider(handler, stream_idle_timeout_seconds=0.05)
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    assert caught.value.code == "agent_provider_stream_idle_timeout"
    assert caught.value.retryable is True
    assert caught.value.details["idle_timeout_seconds"] == 0.05


def test_stream_without_idle_config_behaves_as_before() -> None:
    provider = make_provider(ok_handler)
    events = asyncio.run(collect(provider))
    assert any(isinstance(event, ProviderCompleted) for event in events)


# ---------------------------------------------------------------- 401 诊断


def test_auth_failure_carries_sanitized_diagnostics() -> None:
    full_key = "sk-live-1234567890abcdef"
    provider = make_provider(
        status_handler_factory(401, f'{{"error": "invalid key {full_key} provided"}}'),
    )
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    exc = caught.value
    assert exc.code == "agent_provider_http_error"
    assert exc.retryable is False
    details = exc.details
    assert details["final_url"] == "https://provider.test/v1/chat/completions"
    assert details["method"] == "POST"
    assert details["protocol"] == "chat"
    assert details["model"] == "test-model"
    assert details["auth_type"] == "bearer"
    assert details["api_key_present"] is True
    assert details["api_key_suffix"] == "cdef"
    assert details["sent_header_names"] == sorted(details["sent_header_names"])
    assert "Authorization" in details["sent_header_names"]
    assert details["retry_count"] == 0
    assert details["upstream_status"] == 401
    # 完整密钥绝不出现：details、消息全文扫断言
    assert len(details["upstream_excerpt"]) <= 200
    assert full_key not in json.dumps(details, ensure_ascii=False)
    assert full_key not in str(exc)
    assert "***" in details["upstream_excerpt"]


def test_non_auth_http_error_carries_basic_diagnostics_only() -> None:
    full_key = "sk-live-1234567890abcdef"
    provider = make_provider(status_handler_factory(500))
    with pytest.raises(AgentProviderError) as caught:
        asyncio.run(collect(provider))
    details = caught.value.details
    assert details["upstream_status"] == 500
    assert details["final_url"] == "https://provider.test/v1/chat/completions"
    assert details["retry_count"] == 0
    assert "api_key_suffix" not in details
    assert "sent_header_names" not in details
    assert full_key not in json.dumps(details, ensure_ascii=False)


# ---------------------------------------------------------------- 作者面话术（agent_service 助手）


def test_author_facing_403_keeps_verification_reason_and_quota_classification() -> None:
    from zhimeng_runtime.agent_service import _author_facing_provider_failure

    denied = AgentProviderError('Provider returned HTTP 403', code='agent_provider_http_error', retryable=False,
        details={'upstream_status': 403, 'upstream_message': 'Complete identity verification.'})
    message = _author_facing_provider_failure(denied)
    assert 'Complete identity verification.' in message
    assert '密钥被拒' not in message and '过期' not in message
    quota = AgentProviderError('Provider returned HTTP 403', code='agent_provider_http_error', retryable=False,
        details={'upstream_status': 403, 'upstream_message': 'Unavailable.',
                 'upstream_excerpt': '{"code":"insufficient_quota"}'})
    assert '额度不足' in _author_facing_provider_failure(quota)


def test_author_facing_message_for_circuit_open_and_401() -> None:
    from zhimeng_runtime.agent_service import _author_facing_provider_failure

    circuit = AgentProviderError(
        "模型服务连续失败，已暂时停试，约 42 秒后再试。",
        code="agent_provider_circuit_open",
        retryable=True,
        details={"cooldown_remaining_seconds": 42},
    )
    assert "42 秒后再试" in _author_facing_provider_failure(circuit)

    auth = AgentProviderError(
        "Provider returned HTTP 401: unauthorized",
        code="agent_provider_http_error",
        retryable=False,
        details={"upstream_status": 401, "api_key_suffix": "cdef"},
    )
    message = _author_facing_provider_failure(auth)
    assert 'HTTP 401: unauthorized' in message
    assert "密钥被拒" in message
    assert "……cdef" in message
    assert "运行详情" in message
    # 其他错误保持原始技术消息（details 已另行入库）
    other = AgentProviderError(
        "Provider stream timed out",
        code="agent_provider_timeout",
        retryable=True,
    )
    assert _author_facing_provider_failure(other) == "Provider stream timed out"
