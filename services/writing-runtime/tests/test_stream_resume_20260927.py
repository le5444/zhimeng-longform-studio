"""吐过字之后断流接着写、复读第一次先截掉多余的几遍再接着写（9-27，照 Qwen Code #7832 和 Gemini CLI 的两段式）。

以前这两种都是整轮失败：中转站 40–70 秒掐一次长连接，grok-4.6 写长章节时转圈 25 遍，写好的一千多字跟着作废，
作者只能点「重发这条」。现在已写的留在作者屏幕上，模型从断点接着写；同一场第二次判到复读才停。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from test_agent_service import (
    TurnScriptedProvider,
    _is_session_naming_request,
    build_service,
    create_turn,
    lifecycle_events,
    wait_terminal,
)
from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolDefinition,
    _RepetitionGuard,
)
from zhimeng_runtime.agent_service import _resume_prompt
from zhimeng_runtime.execution_protocol import RunStatus

CYCLE = "他推开门。风从山上下来。灯灭了。"


class BreakThenResumeProvider(TurnScriptedProvider):
    """每一轮先吐 texts，再按 errors 里同一位置的错误收场（None = 正常收场）。"""

    def __init__(self, texts: list[str], errors: list[AgentProviderError | None]) -> None:
        super().__init__([])
        self.texts = texts
        self.errors = errors

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        if _is_session_naming_request(messages):
            yield ProviderTextDelta("测试会话起名")
            yield ProviderCompleted("stop", None)
            return
        self.start_count += 1
        self.started.set()
        self.messages_by_turn.append([dict(message) for message in messages])
        index = self.start_count - 1
        yield ProviderTextDelta(self.texts[index])
        await asyncio.sleep(0.01)
        if self.errors[index] is not None:
            raise self.errors[index]
        yield ProviderCompleted("stop", None)


def _assistant_texts(service, session_id: str) -> list[str]:
    return [m.content for m in service.store.list_messages(session_id) if m.role == "assistant"]


def test_a_stream_that_breaks_after_text_resumes_instead_of_failing(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = BreakThenResumeProvider(
            ["故事从旧港开始，", "然后风向变了。"],
            [
                AgentProviderError("Provider request failed: peer closed connection", code="agent_provider_request_failed", retryable=True),
                None,
            ],
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert _assistant_texts(service, session.session_id) == ["故事从旧港开始，", "然后风向变了。"]
        resumed = [p for kind, p in lifecycle_events(service, run.run_id) if kind == "model_turn_stream_resumed"]
        assert len(resumed) == 1 and resumed[0]["continued"] is True
        prompt = provider.messages_by_turn[1][-1]
        assert prompt["role"] == "user" and "网络连接断了" in str(prompt["content"]) and "故事从旧港开始" in str(prompt["content"])
        await service.shutdown()

    asyncio.run(scenario())


def test_first_repetition_keeps_one_cycle_and_resumes(tmp_path: Path) -> None:
    written = "前言写好了。" + CYCLE * 4
    guard = _RepetitionGuard()
    assert guard.observe(written)
    keep = guard.keep_chars
    assert written[:keep] == "前言写好了。" + CYCLE

    async def scenario() -> None:
        provider = BreakThenResumeProvider(
            [written, "第二天，山门开了。"],
            [
                AgentProviderError(
                    "Provider output is looping on the same content",
                    code="agent_provider_output_repeating",
                    retryable=True,
                    details={"repeated_segment": "他推开门。", "keep_chars": keep},
                ),
                None,
            ],
        )
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        # 转圈的几遍不进对话：落库的第一条是截到第一遍结尾的那段。
        assert _assistant_texts(service, session.session_id) == ["前言写好了。" + CYCLE, "第二天，山门开了。"]
        events = [p for kind, p in lifecycle_events(service, run.run_id) if kind == "model_turn_output_repeating"]
        assert len(events) == 1 and events[0]["repeated_segment"] == "他推开门。"
        second_request = provider.messages_by_turn[1]
        assert second_request[-2] == {"role": "assistant", "content": "前言写好了。" + CYCLE}
        assert "反复打转" in str(second_request[-1]["content"])
        await service.shutdown()

    asyncio.run(scenario())


def test_a_second_repetition_in_the_same_run_stops_it(tmp_path: Path) -> None:
    def looping() -> AgentProviderError:
        return AgentProviderError(
            "Provider output is looping on the same content",
            code="agent_provider_output_repeating",
            retryable=True,
            details={"repeated_segment": "他推开门。", "keep_chars": len("开头。" + CYCLE)},
        )

    async def scenario() -> None:
        provider = BreakThenResumeProvider(["开头。" + CYCLE * 4, "又开头。" + CYCLE * 4], [looping(), looping()])
        service = build_service(tmp_path, provider)
        _session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.FAILED
        assert run.error is not None and run.error.error.code == "agent_provider_output_repeating"
        assert provider.start_count == 2
        await service.shutdown()

    asyncio.run(scenario())


def test_resume_prompts_say_where_it_stopped_without_forbidding_anything() -> None:
    for kind in ("truncated", "stream_broken", "repeating"):
        prompt = _resume_prompt(kind, "……他抬头看了一眼山门。", "他推开门。")
        assert "停在「…" in prompt and "他抬头看了一眼山门。" in prompt
        assert "不要" not in prompt and "不许" not in prompt and "禁止" not in prompt
    assert "被截断" in _resume_prompt("truncated", "半句")
