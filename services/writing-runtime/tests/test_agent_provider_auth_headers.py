"""四种认证形态、默认与自定义 UA 的请求头注入，以及 dispatch 指纹。

默认 UA 是实际发出的额外头，其头名纳入当前指纹；头值仍不进入指纹。
默认 bearer 与显式 bearer 使用同一配方，旧收据保留旧配方作为历史记录。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from zhimeng_runtime.agent_provider import (
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
)
from zhimeng_runtime.context_models import canonical_json_hash


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


def sse(*payloads: object) -> list[bytes]:
    return [
        f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n".encode()
        for payload in payloads
    ]


def provider_with_headers(
    captured_headers: list[httpx.Headers],
    **overrides: Any,
) -> OpenAICompatibleAgentProvider:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                sse(
                    {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
                    "[DONE]",
                )
            ),
        )

    base = {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    }
    base.update(overrides)
    return OpenAICompatibleAgentProvider(**base)


async def _collect(provider: OpenAICompatibleAgentProvider) -> list:
    return [
        event
        async for event in provider.stream_chat(
            [{"role": "user", "content": "写一段"}],
            cancel_event=asyncio.Event(),
        )
    ]


def headers_for(**overrides: Any) -> httpx.Headers:
    captured: list[httpx.Headers] = []
    provider = provider_with_headers(captured, **overrides)
    events = asyncio.run(_collect(provider))
    assert any(isinstance(event, ProviderCompleted) for event in events)
    assert len(captured) == 1
    return captured[0]


def test_bearer_auth_injects_authorization_header() -> None:
    headers = headers_for()
    assert headers["Authorization"] == "Bearer secret"
    assert "x-api-key" not in headers
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert headers["User-Agent"] == "codex-cli/0.55.0"


@pytest.mark.parametrize("header_name", ["User-Agent", "user-agent", "USER-AGENT"])
def test_custom_user_agent_overrides_default_without_duplicate_headers(header_name: str) -> None:
    headers = headers_for(extra_headers={header_name: "custom-client/1.0"})
    assert headers.get_list("user-agent") == ["custom-client/1.0"]


def test_x_api_key_auth_injects_x_api_key_header() -> None:
    headers = headers_for(auth_type="x-api-key")
    assert headers["x-api-key"] == "secret"
    assert "Authorization" not in headers


def test_none_auth_injects_no_credential_header() -> None:
    headers = headers_for(auth_type="none")
    assert "Authorization" not in headers
    assert "x-api-key" not in headers


def test_custom_auth_uses_only_extra_headers_and_they_may_override_authorization() -> None:
    headers = headers_for(
        auth_type="custom",
        extra_headers={
            "Authorization": "Token abc",
            "X-Project-ID": "novel",
            # 这两个是本协议请求的一部分，不许被覆盖
            "Content-Type": "text/plain",
            "Accept": "application/xml",
        },
    )
    # custom：不自动注入 Bearer，作者自己写的认证头生效（允许覆盖 Authorization）
    assert headers["Authorization"] == "Token abc"
    assert headers["X-Project-ID"] == "novel"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"


def test_extra_headers_merge_on_top_of_bearer() -> None:
    headers = headers_for(extra_headers={"anthropic-version": "2023-06-01"})
    assert headers["Authorization"] == "Bearer secret"
    assert headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("auth_type", "header_name", "replacement"),
    [
        ("bearer", "Authorization", "Token replacement"),
        ("bearer", "authorization", "Token replacement"),
        ("bearer", "AUTHORIZATION", "Token replacement"),
        ("x-api-key", "x-api-key", "replacement-key"),
        ("x-api-key", "X-Api-Key", "replacement-key"),
        ("x-api-key", "X-API-KEY", "replacement-key"),
    ],
)
def test_extra_auth_header_replaces_default_case_insensitively(
    auth_type: str, header_name: str, replacement: str,
) -> None:
    headers = headers_for(
        auth_type=auth_type,
        extra_headers={header_name: replacement},
    )
    # Check the transmitted header list: a mapping lookup joins duplicates and
    # would hide the fact that the old credential was sent as well.
    assert headers.get_list(header_name) == [replacement]


def test_extra_headers_with_case_variants_send_only_the_last_value() -> None:
    headers = headers_for(
        auth_type="custom",
        extra_headers={
            "Authorization": "Token first",
            "authorization": "Token second",
        },
    )
    assert headers.get_list("authorization") == ["Token second"]


def test_invalid_auth_type_and_headers_are_rejected() -> None:
    with pytest.raises(ValueError, match="auth_type"):
        provider_with_headers([], auth_type="oauth2")
    with pytest.raises(ValueError, match="header names"):
        provider_with_headers([], extra_headers={"Bad Name": "x"})
    with pytest.raises(ValueError, match="control-character-free"):
        provider_with_headers([], extra_headers={"X-Test": "a\nb"})


# ---------------------------------------------------------------- 指纹配方


def _legacy_chat_dispatch_config() -> dict:
    """改造前 chat 线的指纹 payload，逐键硬编码——这就是旧收据的配方。"""
    return {
        "schema": "zhimeng.openai-compatible-dispatch-config.v1",
        "endpoint": "https://provider.test/v1/chat/completions",
        "model": "test-model",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "reasoning_effort": "auto",
        "stream": True,
        "include_usage": True,
        "tool_choice": "auto",
        # 2026-09-24 T18：不再写死 parallel_tool_calls=false（模型一轮可发多个工具调用），
        # 键随之出配方；与下面 9-20 那次一样是一次性配方更新，旧收据留作历史。
        # 2026-09-20 P2 第3条：单次回复工具调用数上限（16）整道删除，键随之出配方；
        # 入参上限 512KB→8MB 且不再判死（超了回喂模型重发）。配方这一次性随之更新，
        # 旧收据留在库里作历史记录；本测试守的是「默认鉴权不改指纹」。
        "max_tool_argument_bytes": 8388608,
        "accepted_reported_models": [],
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
    }


def _fingerprint_base() -> dict:
    return {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
    }


def test_default_auth_fingerprint_includes_default_user_agent_header() -> None:
    provider = OpenAICompatibleAgentProvider(**_fingerprint_base())
    expected = {**_legacy_chat_dispatch_config(), "extra_header_names": ["User-Agent"]}
    assert provider.dispatch_config_fingerprint == canonical_json_hash(expected)
    assert provider.dispatch_config_fingerprint != canonical_json_hash(_legacy_chat_dispatch_config())
    # 显式写出默认值同样不许变
    explicit = OpenAICompatibleAgentProvider(
        **_fingerprint_base(), auth_type="bearer", extra_headers={}
    )
    assert explicit.dispatch_config_fingerprint == provider.dispatch_config_fingerprint

    custom_ua = OpenAICompatibleAgentProvider(
        **_fingerprint_base(), extra_headers={"User-Agent": "custom-client/1.0"}
    )
    assert custom_ua.dispatch_config_fingerprint == provider.dispatch_config_fingerprint


def test_non_default_auth_changes_fingerprint_deterministically() -> None:
    default = OpenAICompatibleAgentProvider(**_fingerprint_base())
    custom = OpenAICompatibleAgentProvider(
        **_fingerprint_base(),
        auth_type="custom",
        extra_headers={"X-Project-ID": "a", "anthropic-version": "2023-06-01"},
    )
    assert custom.dispatch_config_fingerprint != default.dispatch_config_fingerprint
    # 确定性：同样的输入（哪怕头名字典顺序不同）必须得到同一个指纹
    same = OpenAICompatibleAgentProvider(
        **_fingerprint_base(),
        auth_type="custom",
        extra_headers={"anthropic-version": "2023-06-01", "X-Project-ID": "a"},
    )
    assert same.dispatch_config_fingerprint == custom.dispatch_config_fingerprint
    # 头名不同指纹就不同
    other = OpenAICompatibleAgentProvider(
        **_fingerprint_base(),
        auth_type="custom",
        extra_headers={"X-Other": "a"},
    )
    assert other.dispatch_config_fingerprint != custom.dispatch_config_fingerprint
    # 头的值视同密钥，不进指纹：只改值不换指纹（密钥变更由 credential_reference 表达）
    changed_value = OpenAICompatibleAgentProvider(
        **_fingerprint_base(),
        auth_type="custom",
        extra_headers={"X-Project-ID": "b", "anthropic-version": "2024-01-01"},
    )
    assert changed_value.dispatch_config_fingerprint == custom.dispatch_config_fingerprint
    # 指纹序列化里不得出现任何头值
    fingerprint_material = json.dumps(
        [custom.dispatch_config_fingerprint, changed_value.dispatch_config_fingerprint]
    )
    assert "2023-06-01" not in fingerprint_material
