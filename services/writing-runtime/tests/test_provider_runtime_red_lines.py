"""《Fable Agent Provider Runtime 完整指令》§102「绝对禁止」的 18 条硬红线。

本文件不新增任何能力，只把作者写死的 18 条禁令逐条钉成可执行断言：
以后任何人（包括任何 AI）改动接入层时踩到其中一条，会当场红灯，而不是等到
下一次线上 401 排查才被发现。

编号与原文 §102 的出现顺序一一对应，每条 docstring 引用原文那一句。
尚未接线的能力用 ``xfail(strict=True)`` 标注——接线之后该测试会「意外通过」
并因此报错，提醒把标记去掉。

与既有测试的分工：本文件只钉禁令。协议细节的正向覆盖仍在
``test_agent_provider_anthropic.py`` / ``test_agent_provider_responses.py`` /
``test_agent_provider_auth_headers.py`` / ``test_agent_provider_resilience.py``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
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
    _reset_resilience_registries,
)
from zhimeng_runtime.session_checkpoint import (
    CHECKPOINT_SECTION_TITLES,
    build_checkpoint_request_messages,
    normalize_checkpoint_text,
)
from zhimeng_runtime.model_context import resolve_context_window_spec

API_KEY = "sk-redline-SECRET-value-9xyz"
DEFAULT_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "写一段"}]


@pytest.fixture(autouse=True)
def _reset_resilience() -> Iterator[None]:
    """熔断与并发是模块级共享状态，逐条红线之间必须复位。"""
    _reset_resilience_registries()
    yield
    _reset_resilience_registries()


# ---------------------------------------------------------------------------
# 夹具：三种协议各自的最小可用 SSE，以及一个可捕获请求的 provider。
# ---------------------------------------------------------------------------


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


def stream_response(chunks: list[bytes]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=ChunkStream(chunks),
    )


def chat_sse() -> list[bytes]:
    return sse(
        {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
        "[DONE]",
    )


def responses_sse() -> list[bytes]:
    return sse(
        {"type": "response.output_text.delta", "delta": "好"},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "model": "test-model",
                "output": [{"type": "message", "id": "msg-1"}],
                "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            },
        },
    )


def anthropic_sse() -> list[bytes]:
    return sse(
        {
            "type": "message_start",
            "message": {"model": "test-model", "usage": {"input_tokens": 3}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "好"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    )


SSE_FOR_WIRE: dict[str, Callable[[], list[bytes]]] = {
    "chat": chat_sse,
    "responses": responses_sse,
    "anthropic_messages": anthropic_sse,
}


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


def build_provider(
    captured: list[httpx.Request],
    *,
    responder: Callable[[httpx.Request, int], httpx.Response] | None = None,
    chunks: list[bytes] | None = None,
    **overrides: Any,
) -> OpenAICompatibleAgentProvider:
    wire = overrides.get("wire_format", "chat")

    async def handler(request: httpx.Request) -> httpx.Response:
        index = len(captured)
        captured.append(request)
        if responder is not None:
            return responder(request, index)
        return stream_response(chunks if chunks is not None else SSE_FOR_WIRE[wire]())

    base: dict[str, Any] = {
        "base_url": "https://relay.test/v1",
        "model": "test-model",
        "api_key": API_KEY,
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    }
    base.update(overrides)
    return OpenAICompatibleAgentProvider(**base)


def collect(
    provider: OpenAICompatibleAgentProvider,
    *,
    messages: Sequence[dict[str, Any]] | None = None,
    tools: tuple[ProviderToolDefinition, ...] = (),
) -> list[Any]:
    payload = DEFAULT_MESSAGES if messages is None else list(messages)

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.stream_chat(
                payload,
                cancel_event=asyncio.Event(),
                tools=tools,
            )
        ]

    return asyncio.run(run())


def send_once(
    *,
    messages: Sequence[dict[str, Any]] | None = None,
    **overrides: Any,
) -> tuple[httpx.Request, dict[str, Any]]:
    """发一次请求，交回「请求对象 + 解析后的 body」。

    只关心「发出去长什么样」的红线用它：即使随后的流式解析失败也不影响断言。
    """
    captured: list[httpx.Request] = []
    try:
        collect(build_provider(captured, **overrides), messages=messages)
    except AgentProviderError:
        pass
    assert captured, "provider 没有发出任何请求"
    return captured[0], json.loads(captured[0].content)


# ---------------------------------------------------------------------------
# 第 1–4 条：不许把任何一种配置写死成全局默认。
# ---------------------------------------------------------------------------


def test_red_line_01_no_shared_hardcoded_url() -> None:
    """禁止所有 Provider 共用：一个固定 URL。"""
    alpha, _ = send_once(base_url="https://alpha.test/v1")
    beta, _ = send_once(base_url="https://beta.test/openai/v1")

    assert str(alpha.url) == "https://alpha.test/v1/chat/completions"
    # 作者粘来的地址里已有的路径段必须原样保留，不能被重写成统一形状。
    assert str(beta.url) == "https://beta.test/openai/v1/chat/completions"


def test_red_line_02_no_shared_hardcoded_api_key() -> None:
    """禁止所有 Provider 共用：一个固定 API Key。"""
    alpha, _ = send_once(api_key="sk-alpha-0001")
    beta, _ = send_once(api_key="sk-beta-0002")

    assert alpha.headers["authorization"] == "Bearer sk-alpha-0001"
    assert beta.headers["authorization"] == "Bearer sk-beta-0002"


def test_red_line_03_bearer_is_not_the_forced_default() -> None:
    """禁止所有 Provider 默认：Bearer。"""
    keyed, _ = send_once(auth_type="x-api-key")
    assert keyed.headers["x-api-key"] == API_KEY
    assert "authorization" not in keyed.headers

    anonymous, _ = send_once(auth_type="none")
    assert "authorization" not in anonymous.headers
    assert "x-api-key" not in anonymous.headers


def test_red_line_04_chat_completions_is_not_the_forced_default_path() -> None:
    """禁止所有 Provider 默认：/v1/chat/completions。"""
    paths = {
        wire: httpx.URL(send_once(wire_format=wire)[0].url).path
        for wire in ("chat", "responses", "anthropic_messages")
    }

    assert paths["chat"] == "/v1/chat/completions"
    assert paths["responses"] == "/v1/responses"
    assert paths["anthropic_messages"] == "/v1/messages"
    assert len(set(paths.values())) == 3


# ---------------------------------------------------------------------------
# 第 5–6 条：三种协议不是同一个 API，请求体与端点都必须原生。
# ---------------------------------------------------------------------------


def test_red_line_05_responses_is_not_chat_in_disguise() -> None:
    """禁止：Responses 当 Chat。"""
    request, body = send_once(wire_format="responses")

    assert httpx.URL(request.url).path.endswith("/responses")
    # Responses 的入参叫 input，不是 messages。
    assert "input" in body
    assert "messages" not in body

    # 事件名也必须走 Responses 那一套，而不是 choices[].delta。
    events = collect(build_provider([], wire_format="responses"))
    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == ["好"]


def test_red_line_06_anthropic_is_not_openai_in_disguise() -> None:
    """禁止：Anthropic 当 OpenAI。"""
    request, body = send_once(
        wire_format="anthropic_messages",
        messages=[
            {"role": "system", "content": "你是写作搭档。"},
            {"role": "user", "content": "写一段"},
        ],
    )

    assert httpx.URL(request.url).path.endswith("/messages")
    assert request.headers["anthropic-version"]
    # system 必须提到顶层，不能留在 messages 里充当一个 role。
    # 顶层 system 是 blocks 形态，最后一块带缓存断点（cache_control）。
    assert body["system"] == [
        {
            "type": "text",
            "text": "你是写作搭档。",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert all(message["role"] != "system" for message in body["messages"])
    # max_tokens 在 Anthropic Messages 里是必填。
    assert body["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# 第 7 条：思考强度不是采样温度。
# ---------------------------------------------------------------------------


def test_red_line_07_reasoning_never_becomes_temperature() -> None:
    """禁止：Reasoning 当 Temperature。"""
    for wire, reasoning_key in (("chat", "reasoning_effort"), ("responses", "reasoning")):
        _, low = send_once(wire_format=wire, reasoning_effort="low", temperature=0.2)
        _, high = send_once(wire_format=wire, reasoning_effort="high", temperature=0.2)

        # 改思考强度绝不能顺手动温度。
        assert low["temperature"] == 0.2
        assert high["temperature"] == 0.2
        # 差异必须且只能体现在 reasoning 字段上。
        assert low[reasoning_key] != high[reasoning_key]

    # Anthropic 线：思考走 thinking.budget_tokens。官方协议里 thinking 与自定义
    # temperature 互斥，所以开思考时不发 temperature——但也绝不是「换算成温度」。
    # max_tokens 要给够：官方要求 budget_tokens ≥ 1024 且 < max_tokens，
    # 用默认的 1024 会挤不下任何合法预算（那是另一条正确行为，不在本条红线里）。
    _, thinking_off = send_once(
        wire_format="anthropic_messages",
        reasoning_effort="none",
        temperature=0.2,
        max_tokens=16384,
    )
    _, thinking_on = send_once(
        wire_format="anthropic_messages",
        reasoning_effort="high",
        temperature=0.2,
        max_tokens=16384,
    )

    assert "thinking" not in thinking_off
    assert thinking_off["temperature"] == 0.2
    assert thinking_on["thinking"]["budget_tokens"] > 0
    assert "temperature" not in thinking_on


# ---------------------------------------------------------------------------
# 第 8–9 条：工具调用与思考内容都不是正文。
# ---------------------------------------------------------------------------


def test_red_line_08_tool_call_is_never_plain_text() -> None:
    """禁止：Tool Call 当普通文本。"""
    chunks = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "project_read_file",
                                    "arguments": '{"file_id": "f1"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    events = collect(build_provider([], chunks=chunks), tools=(tool_definition(),))

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_name == "project_read_file"
    assert calls[0].arguments == {"file_id": "f1"}

    # 工具名与参数绝不能同时作为正文文本漏给作者。
    text = "".join(e.text for e in events if isinstance(e, ProviderTextDelta))
    assert "project_read_file" not in text
    assert "file_id" not in text


def test_red_line_09_thinking_is_never_answer_text() -> None:
    """禁止：Thinking 当普通 answer text。"""
    chunks = sse(
        {
            "type": "message_start",
            "message": {"model": "test-model", "usage": {"input_tokens": 3}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "先想清楚再写"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": "好"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    )
    events = collect(
        build_provider([], wire_format="anthropic_messages", chunks=chunks)
    )

    reasoning = "".join(e.text for e in events if isinstance(e, ProviderReasoningDelta))
    text = "".join(e.text for e in events if isinstance(e, ProviderTextDelta))
    assert reasoning == "先想清楚再写"
    assert text == "好"
    assert "先想清楚" not in text


# ---------------------------------------------------------------------------
# 第 10–11 条：401 要给现场；模型列表不是推理的前置条件。
# ---------------------------------------------------------------------------


def test_red_line_10_401_never_just_blames_the_key() -> None:
    """禁止：401 直接说 Key 错了。"""

    def responder(request: httpx.Request, index: int) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid token"}})

    with pytest.raises(AgentProviderError) as excinfo:
        collect(build_provider([], responder=responder))

    details = excinfo.value.details or {}
    for field in (
        "final_url",
        "method",
        "upstream_status",
        "retry_count",
        "protocol",
        "model",
        "auth_type",
        "api_key_present",
        "api_key_suffix",
        "sent_header_names",
        "upstream_excerpt",
    ):
        assert field in details, f"401 诊断缺少定位字段 {field}"

    assert details["api_key_present"] is True
    assert details["api_key_suffix"] == API_KEY[-4:]
    assert details["final_url"] == "https://relay.test/v1/chat/completions"
    assert "authorization" in {name.lower() for name in details["sent_header_names"]}


def test_red_line_11_inference_never_depends_on_the_models_endpoint() -> None:
    """禁止：Models 失败就禁止 inference。

    服务层的对应覆盖见 ``test_parse_models_response.py`` 的
    ``test_list_models_failure_is_not_a_death_sentence``；这里钉运行时那一半：
    一次推理根本不会去碰 /models，所以模型列表挂掉不可能挡住推理。
    """
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request, index: int) -> httpx.Response:
        if httpx.URL(request.url).path.endswith("/models"):
            return httpx.Response(404, json={"error": "no such endpoint"})
        return stream_response(chat_sse())

    events = collect(build_provider(captured, responder=responder))

    assert any(isinstance(event, ProviderCompleted) for event in events)
    assert captured
    assert all(
        not httpx.URL(request.url).path.endswith("/models") for request in captured
    )


# ---------------------------------------------------------------------------
# 第 12–13 条：流式是逐块到达的，工具参数是逐块拼起来的。
# ---------------------------------------------------------------------------


def test_red_line_12_stream_true_is_not_one_json_blob() -> None:
    """禁止：stream=true 只等一个 JSON。"""
    _, body = send_once()
    assert body["stream"] is True

    chunks = sse(
        {"choices": [{"delta": {"content": "第一"}}]},
        {"choices": [{"delta": {"content": "第二"}}]},
        {"choices": [{"delta": {"content": "第三"}, "finish_reason": "stop"}]},
        "[DONE]",
    )
    events = collect(build_provider([], chunks=chunks))

    # 三段必须各自到达，而不是攒成一坨再一次性交出来。
    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == [
        "第一",
        "第二",
        "第三",
    ]


def test_red_line_13_tool_args_are_not_json_parsed_per_delta() -> None:
    """禁止：tool args 每一个 delta 都 JSON.parse。"""
    # 三个分片单独拿出来都不是合法 JSON，只有拼完才是。
    chunks = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "project_read_file",
                                    "arguments": '{"fi',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'le_id": "f'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )
    events = collect(build_provider([], chunks=chunks), tools=(tool_definition(),))

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {"file_id": "f1"}


# ---------------------------------------------------------------------------
# 第 14–15 条：超窗要折叠，不要截肢；折叠不许丢掉失败记录。
# ---------------------------------------------------------------------------


def test_red_line_14_overflow_folds_history_instead_of_dropping_the_front() -> None:
    """禁止：context overflow 时简单删除最前面的消息。"""
    history = [
        ("user", "主角叫沈砚，别写成沈言"),
        ("assistant", "记住了"),
        ("user", "第 3 章要反转"),
    ]
    request = build_checkpoint_request_messages(
        previous_checkpoint=None, messages=history
    )
    folded = request[-1]["content"]

    # 最早的几条必须整体进摘要素材，而不是被直接丢弃。
    for _role, content in history:
        assert content in folded, "最早的对话必须进摘要，而不是被删掉"

    # 最近几轮原样留在窗口里，不参与折叠（按字数留，至少最后一整轮）。
    assert resolve_context_window_spec("256k").checkpoint_keep_recent_characters > 0

    # 上一份摘要必须被带进下一次折叠，不能每次从零重来。
    carried = build_checkpoint_request_messages(
        previous_checkpoint="【当前目标】写完第 3 章", messages=history
    )
    assert "写完第 3 章" in carried[-1]["content"]


def test_red_line_15_compaction_keeps_the_failed_approaches() -> None:
    """禁止：compaction 把关键失败记录完全删除。"""
    assert "试过但不行的做法" in CHECKPOINT_SECTION_TITLES

    instruction = build_checkpoint_request_messages(
        previous_checkpoint=None, messages=[("user", "随便一句")]
    )[0]["content"]
    assert "试过但不行的做法" in instruction
    # 这一节被明确标成不可省略，否则模型整理时最容易先扔掉它。
    assert "绝不能漏" in instruction

    # 清洗环节也不得把这一节抹掉。
    summary = "【试过但不行的做法】把主角改成女性，作者当场否掉了。"
    assert normalize_checkpoint_text(summary) == summary


def test_red_line_14_15_compaction_is_actually_wired_into_the_agent_loop() -> None:
    """第 14/15 条要在真实运行里成立的前提：Agent 循环真的会调用会话摘要。

    端到端行为（什么时候折、折完下一轮是否用上、整理失败会不会连累已完成的
    Run）见 ``test_session_checkpoint_wiring.py``。这里只钉住接线本身不被
    悄悄摘掉——摘掉之后超窗就又变成「删最前面的消息」了。
    """
    import zhimeng_runtime.agent_service as agent_service
    from zhimeng_runtime.agent_store import AgentStore

    assert hasattr(AgentStore, "latest_session_checkpoint")
    assert hasattr(AgentStore, "append_session_checkpoint")
    assert hasattr(agent_service.AgentService, "_maybe_fold_session_history")

    source = Path(agent_service.__file__).read_text(encoding="utf-8")
    assert (
        "await self._maybe_fold_session_history(" in source
    ), "会话摘要没有被 Agent 循环调用"


# ---------------------------------------------------------------------------
# 第 16–17 条：重试不许把身份和协议弄丢。
# ---------------------------------------------------------------------------


def test_red_line_16_retry_keeps_authorization() -> None:
    """禁止：retry 时丢失 Authorization。"""

    def responder(request: httpx.Request, index: int) -> httpx.Response:
        if index == 0:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": "busy"}
            )
        return stream_response(chat_sse())

    captured: list[httpx.Request] = []
    events = collect(build_provider(captured, responder=responder))

    assert len(captured) == 2, "429 之后应当重试一次"
    assert captured[0].headers["authorization"] == captured[1].headers["authorization"]
    assert captured[1].headers["authorization"] == f"Bearer {API_KEY}"
    assert any(isinstance(event, ProviderCompleted) for event in events)


def test_red_line_17_retry_never_silently_switches_protocol() -> None:
    """禁止：fallback 时静默修改 protocol。

    跨模型 fallback 目前还没实现（作者 §103 排在第十优先）。在它落地之前，
    这条红线在重试路径上同样成立：重试不得偷偷换协议或换端点。
    """

    def responder(request: httpx.Request, index: int) -> httpx.Response:
        if index == 0:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": "busy"}
            )
        return stream_response(responses_sse())

    captured: list[httpx.Request] = []
    collect(build_provider(captured, responder=responder, wire_format="responses"))

    assert len(captured) == 2
    assert {httpx.URL(request.url).path for request in captured} == {"/v1/responses"}
    for request in captured:
        body = json.loads(request.content)
        assert "input" in body and "messages" not in body


# ---------------------------------------------------------------------------
# 第 18 条：完整密钥永远不许离开进程。
# ---------------------------------------------------------------------------


def test_red_line_18_full_key_never_reaches_logs_or_diagnostics() -> None:
    """禁止：完整 Key 出现在日志。"""

    def responder(request: httpx.Request, index: int) -> httpx.Response:
        # 最恶劣的情况：上游把整把密钥原样回显在错误正文里。
        return httpx.Response(
            401, json={"error": {"message": f"Invalid token: {API_KEY}"}}
        )

    with pytest.raises(AgentProviderError) as excinfo:
        collect(build_provider([], responder=responder))

    rendered = str(excinfo.value) + json.dumps(
        excinfo.value.details or {}, ensure_ascii=False
    )
    assert API_KEY not in rendered, "完整密钥漏进了错误消息或诊断字段"
    assert "***" in rendered, "上游回显的密钥应当被替换成 ***"
    assert API_KEY[-4:] in rendered, "只保留后 4 位供作者核对"
