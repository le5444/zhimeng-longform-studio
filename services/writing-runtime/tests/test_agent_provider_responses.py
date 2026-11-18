"""Responses 线路的回归测试。

事件与请求形态取自作者渠道的真机抓包（.runtime/responses-wire-*.txt、
responses-clean-*.txt），不是照文档臆造的。

同时钉住一条兼容红线：Chat 线的 dispatch 指纹必须逐位不变——作者机器上
已有的 provider-io-boundary 收据是按那份配方算的，改了就全部对不上。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    OpenAICompatibleAgentProvider,
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
            yield chunk


def sse(*payloads: object) -> list[bytes]:
    chunks: list[bytes] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        chunks.append(f"data: {data}\n\n".encode())
    return chunks


def responses_provider_for(
    chunks: list[bytes],
    *,
    captured_requests: list[dict[str, Any]] | None = None,
    captured_paths: list[str] | None = None,
):
    async def handler(request: httpx.Request) -> httpx.Response:
        if captured_paths is not None:
            captured_paths.append(request.url.path)
        if captured_requests is not None:
            captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
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
        reasoning_effort="high",
        wire_format="responses",
        client=client,
    )
    return provider, client


def tool_definition() -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name="chapter_word_count",
        version="1",
        description="查询指定章节的当前字数。",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {"chapter_number": {"type": "integer"}},
            "required": ["chapter_number"],
        },
    )


def completed(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp-1",
            "status": "completed",
            "model": "test-model",
            "output": output,
            "usage": {
                "input_tokens": 315,
                "input_tokens_details": {"cached_tokens": 8},
                "output_tokens": 67,
                "total_tokens": 382,
            },
        },
    }


def run_stream(provider, messages, tools=()) -> list[Any]:
    async def collect() -> list[Any]:
        events: list[Any] = []
        async for event in provider.stream_chat(
            messages, cancel_event=asyncio.Event(), tools=tools
        ):
            events.append(event)
        return events

    return asyncio.run(collect())


def test_request_uses_responses_endpoint_and_shape() -> None:
    captured: list[dict[str, Any]] = []
    paths: list[str] = []
    provider, client = responses_provider_for(
        sse(
            {"type": "response.output_text.delta", "delta": "好"},
            completed([{"type": "message", "id": "msg-1"}]),
        ),
        captured_requests=captured,
        captured_paths=paths,
    )
    try:
        run_stream(
            provider,
            [
                {"role": "system", "content": "你是写作搭档。"},
                {"role": "system", "content": "只用中文回答。"},
                {"role": "user", "content": "第 4 章多少字？"},
            ],
            (tool_definition(),),
        )
    finally:
        asyncio.run(client.aclose())

    assert paths == ["/v1/responses"]
    body = captured[0]
    # system 上提到顶层 instructions，多条按顺序并起来
    assert body["instructions"] == "你是写作搭档。\n\n只用中文回答。"
    # 对话消息写全 type（有的线路缺了就回 400 missing input.type）
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第 4 章多少字？"}]}
    ]
    assert body["max_output_tokens"] == 1024
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "messages" not in body
    assert "max_tokens" not in body
    assert "reasoning_effort" not in body
    # 工具是扁平的，没有 Chat 那层 function 嵌套
    assert body["tools"] == [
        {
            "type": "function",
            "name": "chapter_word_count",
            "description": "查询指定章节的当前字数。",
            "parameters": tool_definition().parameters,
        }
    ]


def test_text_and_reasoning_streams_are_kept_apart() -> None:
    provider, client = responses_provider_for(
        sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "先看章节"},
            {"type": "response.reasoning_summary_text.delta", "delta": "\n\n"},
            {"type": "response.reasoning_summary_text.delta", "delta": "再核对文件"},
            {"type": "response.output_text.delta", "delta": "第 4 章"},
            {"type": "response.output_text.delta", "delta": "共 3263 字。"},
            completed([{"type": "message", "id": "msg-1"}]),
        )
    )
    try:
        events = run_stream(provider, [{"role": "user", "content": "问"}])
    finally:
        asyncio.run(client.aclose())

    assert [e.text for e in events if isinstance(e, ProviderTextDelta)] == [
        "第 4 章",
        "共 3263 字。",
    ]
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == ["先看章节", "\n\n", "再核对文件"]
    assert [e.visibility for e in events if isinstance(e, ProviderReasoningDelta)] == ["summary"] * 3


def test_function_call_is_decoded_into_a_structured_tool_call() -> None:
    provider, client = responses_provider_for(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "id": "fc_call_abc",
                    "type": "function_call",
                    "call_id": "call_abc12345",
                    "name": "chapter_word_count",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": '{"chapter_',
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": 'number": 4}',
            },
            completed(
                [
                    {"type": "reasoning", "id": "rs-1"},
                    {
                        "type": "function_call",
                        "call_id": "call_abc12345",
                        "name": "chapter_word_count",
                        "arguments": '{"chapter_number": 4}',
                    },
                ]
            ),
        )
    )
    try:
        events = run_stream(
            provider, [{"role": "user", "content": "查一下"}], (tool_definition(),)
        )
    finally:
        asyncio.run(client.aclose())

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_name == "chapter_word_count"
    assert calls[0].arguments == {"chapter_number": 4}


def test_tool_results_become_function_call_output_items() -> None:
    captured: list[dict[str, Any]] = []
    provider, client = responses_provider_for(
        sse(
            {"type": "response.output_text.delta", "delta": "3263 字。"},
            completed([{"type": "message", "id": "msg-1"}]),
        ),
        captured_requests=captured,
    )
    try:
        run_stream(
            provider,
            [
                {"role": "user", "content": "查一下"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc12345",
                            "type": "function",
                            "function": {
                                "name": "chapter_word_count",
                                "arguments": '{"chapter_number": 4}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc12345",
                    "content": '{"word_count": 3263}',
                },
            ],
            (tool_definition(),),
        )
    finally:
        asyncio.run(client.aclose())

    assert captured[0]["input"][1:] == [
        {
            "type": "function_call",
            "call_id": "call_abc12345",
            "name": "chapter_word_count",
            "arguments": '{"chapter_number": 4}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_abc12345",
            "output": '{"word_count": 3263}',
        },
    ]


def test_incomplete_on_output_budget_reports_truncation() -> None:
    provider, client = responses_provider_for(
        sse(
            {"type": "response.output_text.delta", "delta": "写到一半"},
            {
                "type": "response.incomplete",
                "response": {
                    "id": "resp-1",
                    "status": "incomplete",
                    "model": "test-model",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
            },
        )
    )
    try:
        with pytest.raises(AgentProviderError) as caught:
            run_stream(provider, [{"role": "user", "content": "写一章"}])
    finally:
        asyncio.run(client.aclose())
    assert caught.value.code == "agent_provider_output_truncated"


def test_error_event_surfaces_the_relay_message() -> None:
    provider, client = responses_provider_for(
        sse(
            {
                "type": "error",
                "code": "internal_server_error",
                "message": "upstream stream closed",
            }
        )
    )
    try:
        with pytest.raises(AgentProviderError) as caught:
            run_stream(provider, [{"role": "user", "content": "问"}])
    finally:
        asyncio.run(client.aclose())
    assert "upstream stream closed" in str(caught.value)


def test_usage_maps_directly_onto_message_usage() -> None:
    usage = OpenAICompatibleAgentProvider._responses_usage(completed([])["response"])
    assert usage is not None
    assert usage.input_tokens == 315
    assert usage.output_tokens == 67
    assert usage.total_tokens == 382
    assert usage.cached_input_tokens == 8


def test_wire_format_changes_identity_but_never_the_chat_fingerprint() -> None:
    base = {
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "credential_reference": "env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        "timeout_seconds": 30,
        "max_tokens": 1024,
        "temperature": 0.2,
    }
    chat = OpenAICompatibleAgentProvider(**base)
    responses = OpenAICompatibleAgentProvider(**base, wire_format="responses")

    assert chat.identity.implementation == "openai-compatible-chat-completions"
    assert responses.identity.implementation == "openai-compatible-responses"
    assert chat.dispatch_config_fingerprint != responses.dispatch_config_fingerprint
    # 红线：默认（chat）的指纹配方不许变——旧收据要还能对上。
    assert (
        chat.dispatch_config_fingerprint
        == OpenAICompatibleAgentProvider(
            **base, wire_format="chat"
        ).dispatch_config_fingerprint
    )


def test_invalid_wire_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleAgentProvider(
            base_url="https://provider.test/v1",
            model="test-model",
            api_key="secret",
            credential_reference="env:ZHIMENG_AGENT_PROVIDER_API_KEY",
            timeout_seconds=30,
            max_tokens=1024,
            temperature=0.2,
            wire_format="grpc",  # type: ignore[arg-type]
        )


def test_repetition_guard_trips_on_a_short_line_said_over_and_over() -> None:
    """复现 run_91cdc08f84 的形态：同一短句连着吐，必须被认出来。"""
    from zhimeng_runtime.agent_provider import _RepetitionGuard

    guard = _RepetitionGuard()
    tripped_at = None
    for index in range(1, 40):
        if guard.observe("\n\n请直接改正文。"):
            tripped_at = index
            break
    assert tripped_at is not None
    assert tripped_at <= 20
    assert guard.repeated_segment == "请直接改正文。"


def test_repetition_guard_trips_on_a_repeated_multi_sentence_block() -> None:
    """复现 run_25eb704cd2 的形态：整段指引被复读 40 遍。

    这一种句子本身不相邻重复，是「一组句子」在转圈，
    所以闸门必须找周期，而不是只比相邻两句。
    """
    from zhimeng_runtime.agent_provider import _RepetitionGuard

    block = (
        "这是普通的写作搭档对话，不是固定流程。"
        "先理解作者这句话本身：如果只是提问、讨论，就直接回答。"
        "作者要求修改正文时，按顺序完成：先找到目标章节文件。"
        "再用工具读它现在的内容。"
        "最后用一两句话说明改了什么。"
    )
    guard = _RepetitionGuard()
    tripped_at = None
    for index in range(1, 12):
        if guard.observe(block):
            tripped_at = index
            break
    assert tripped_at is not None, "整段复读没有被认出来"
    assert tripped_at <= 4, "转了四圈还没认出来就太晚了"


def test_repetition_guard_trips_on_a_long_block_of_prose_going_round() -> None:
    """复现 run_2bdf0af522（9-27「接着写第 5 章」）：写了一段正文后，把 31 句、约 490 字的一段转了 25 圈。

    旧闸门只找 20 句以内的循环节，一直吐到中转站 482 秒掐断连接。
    """
    from zhimeng_runtime.agent_provider import _RepetitionGuard

    block = "".join(f"第{index}句，院子里的人各忙各的，风从山门那边吹过来。\n\n" for index in range(31))
    guard = _RepetitionGuard()
    opening = "夜深了，破院里只剩一盏灯。\n\n" * 1 + "他把账本合上，想了很久。\n\n"
    assert not any(guard.observe(opening[i : i + 3]) for i in range(0, len(opening), 3))
    tripped_at = None
    for cycle in range(1, 10):
        if any(guard.observe(block[i : i + 3]) for i in range(0, len(block), 3)):
            tripped_at = cycle
            break
    assert tripped_at is not None, "长段落转圈没有被认出来"
    assert tripped_at <= 3


def test_repetition_guard_does_not_flag_normal_prose() -> None:
    """正常正文必须放行——误伤比漏判更糟，作者正在写小说。"""
    from zhimeng_runtime.agent_provider import _RepetitionGuard

    guard = _RepetitionGuard()
    prose = (
        "深秋的黄昏像一层薄薄的霜，无声地覆在河岸上。"
        "天光已经暗下去，只剩西边一痕暗红。"
        "风从河湾那头慢慢推过来，贴着地面，卷起几片蜷缩的叶子。"
        "河面结了一层冰，不厚，泛着青灰的光。"
        "他站在岸边看了一会儿，才把手从袖子里抽出来。"
        "冰面下有细小的气泡，像被冻住的呼吸。"
        "远处有人喊了一声，声音散在风里，听不清说什么。"
        "他没有回头。"
    )
    for character in prose:
        assert not guard.observe(character)

    # 连着说两三遍同一句（模型偶尔强调）也不该被掐
    guard2 = _RepetitionGuard()
    assert not guard2.observe("他没有回头。他没有回头。他没有回头。")


def test_stream_is_cut_off_once_the_model_starts_repeating() -> None:
    """端到端：流式过程中就掐掉，不让它烧穿整份输出预算。

    真实事故里模型吐了 2054 段、22528 字才撞上 max_tokens；有了这道闸门，
    十几段就停，作者拿到的是一句能照着办的话，而不是干等九分钟。
    """
    events = [{"type": "response.output_text.delta", "delta": "\n\n请直接改正文。"}] * 60
    provider, client = responses_provider_for(sse(*events, completed([])))
    try:
        with pytest.raises(AgentProviderError) as caught:
            run_stream(provider, [{"role": "user", "content": "改一下开头"}])
    finally:
        asyncio.run(client.aclose())

    assert caught.value.code == "agent_provider_output_repeating"
    # 报错里要带上它在复读的内容，作者面才能说清楚
    assert "请直接改正文。" in str(caught.value)


def test_settings_and_env_reported_model_aliases_are_merged(tmp_path) -> None:
    """界面里按渠道填的接受名，要和启动脚本/环境变量的全局名单合起来用。

    背景：中转对同一个模型报过 deepseek-v4-pro / deepseek/deepseek-v4-pro /
    deepseek-v4-pro-260425-fy 三种写法（.runtime/responses-*.txt 抓包实证）。
    旧实现只认环境变量里硬编码的一个别名，作者在界面上换模型就会撞
    rejected_mismatch 且不可重试。身份核对不放宽，但名单要够得着。
    """
    from zhimeng_runtime.agent_provider import build_agent_provider_from_connection
    from zhimeng_runtime.config import RuntimeSettings

    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        agent_provider_accepted_reported_models=("deepseek-ai/deepseek-v4-flash-0731",),
    )
    provider = build_agent_provider_from_connection(
        settings,
        base_url="https://relay.example/v1",
        model="[opencode]deepseek-v4-pro",
        api_key="secret",
        credential_reference="settings:provider-key:abc",
        accepted_reported_models=(
            "deepseek-v4-pro",
            "deepseek/deepseek-v4-pro",
            # 与环境变量重复的一条不应被算两次
            "deepseek-ai/deepseek-v4-flash-0731",
        ),
    )
    assert provider is not None
    assert provider.accepted_reported_models == (
        "deepseek-ai/deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
    )


def test_arguments_delivered_only_in_done_event_are_still_decoded() -> None:
    """有的中转不逐段推 arguments，只在 done 里给完整入参——不能因此丢掉工具调用。"""
    full = '{"chapter_number": 7}'
    item = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_only_done1",
        "name": "chapter_word_count",
    }
    provider, client = responses_provider_for(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc_1",
                "arguments": full,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {**item, "arguments": full},
            },
            completed([{**item, "arguments": full}]),
        )
    )
    try:
        events = run_stream(provider, [{"role": "user", "content": "查"}], (tool_definition(),))
    finally:
        asyncio.run(client.aclose())

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].tool_name == "chapter_word_count"
    assert calls[0].arguments == {"chapter_number": 7}


def test_done_after_deltas_does_not_duplicate_arguments() -> None:
    """真机抓包的形态：delta 逐段到齐后，done 再复述一遍完整入参。

    复述不能被当成新片段拼接第二次——否则 JSON 变成两份粘在一起，解析必失败。
    """
    full = '{"chapter_number": 4}'
    item = {
        "id": "fc_2",
        "type": "function_call",
        "call_id": "call_dup_check2",
        "name": "chapter_word_count",
    }
    provider, client = responses_provider_for(
        sse(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**item, "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": full[:9]},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": full[9:]},
            {"type": "response.function_call_arguments.done", "output_index": 1, "arguments": full},
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {**item, "arguments": full},
            },
            completed([{**item, "arguments": full}]),
        )
    )
    try:
        events = run_stream(provider, [{"role": "user", "content": "查"}], (tool_definition(),))
    finally:
        asyncio.run(client.aclose())

    calls = [event for event in events if isinstance(event, ProviderToolCall)]
    assert len(calls) == 1
    assert calls[0].arguments == {"chapter_number": 4}


def test_repetition_guard_ignores_punctuation_only_runs() -> None:
    """网文里「......」「！！！」「……」是省略号和感叹连打，不是循环复读，绝不能掐。

    修复前一串 12 个点就会把整次回答掐掉（实测），这里把几种常见写法都钉住。
    """
    from zhimeng_runtime.agent_provider import _RepetitionGuard

    for text in (
        "............",
        '"......"\n"......"\n"......"\n',
        "啊！！！！！！！！！！！！！！",
        "…" * 16,
        "............然后他转身走了。",
    ):
        guard = _RepetitionGuard()
        assert not any(guard.observe(character) for character in text), text

    # 真的循环复读仍然要认出来
    guard = _RepetitionGuard()
    assert any(guard.observe("请直接改正文。") for _ in range(40))


def test_responses_input_marks_every_conversation_message_with_its_type() -> None:
    """2026-09-26 真机：换成中转站上的 deepseek-v4-pro，缺 `type` 的消息 item 直接 400（missing input.type）。"""
    instructions, items = OpenAICompatibleAgentProvider._build_responses_input(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [{"id": "call-1", "function": {"name": "project_read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "text"},
            {"role": "assistant", "content": "done"},
        ]
    )

    assert instructions == "sys"
    assert [item.get("type") for item in items] == [
        "message", "message", "function_call", "function_call_output", "message",
    ]
    assert [item.get("role") for item in items if item["type"] == "message"] == ["user", "assistant", "assistant"]
