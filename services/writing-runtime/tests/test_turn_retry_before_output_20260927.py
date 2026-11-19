"""一步还没出字就临时失败时重来这一步，并在末尾补一句事实（9-27 晚）。

以前只有「断流」「空回答」两种会重发，而且是一字不差地原样重发；连接中途断了、超时、中转站回「Streaming request failed」
「Concurrency limit exceeded」都整场失败。隔离实例库里有 4 场跑到第 5–13 步才这样没了，前面的活全白干。
现在：这几种临时的毛病重来这一步（每次先歇一会儿，停止键随时有效），重来的请求末尾多一句话，不再一字不差；
额度没了、模型不存在这类照旧失败。空回答补的那句照 MiMo Code：直接回答，或者调用工具接着做。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from test_agent_service import (
    ImmediateNotesToolExecutor,
    TurnScriptedProvider,
    _is_session_naming_request,
    build_service,
    create_turn,
    lifecycle_events,
    wait_terminal,
)
from zhimeng_runtime import agent_service
from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import MAX_TURN_STREAM_RETRIES, _transient_before_output
from zhimeng_runtime.execution_protocol import RunStatus


def _error(code: str, message: str, **details) -> AgentProviderError:
    return AgentProviderError(message, code=code, retryable=True, details=details or None)


CONNECTION_CLOSED = _error(
    "agent_provider_request_failed",
    "Provider request failed: peer closed connection without sending complete message body (incomplete chunked read)",
    request_phase="read_body",
    failure_type="ReadError",
    elapsed_ms=1250,
    stream_lines_received=0,
)
CONCURRENCY = _error(
    "agent_provider_error_event", "Concurrency limit exceeded for account, please retry later"
)
RELAY_STREAM_FAILED = _error(
    "agent_provider_http_error",
    'Provider returned HTTP 400: {"error":{"message":"Streaming request failed","type":"provider_error"}}',
    upstream_status=400,
)
QUOTA_GONE = _error(
    "agent_provider_http_error",
    "当前模型渠道的额度不足，请在设置 → 模型服务切换可用渠道，或补足该渠道额度后继续。",
    upstream_status=403,
)
EMPTY = _error("agent_provider_empty_response", "Provider completed without assistant text or tool calls")


class FailThenAnswerProvider(TurnScriptedProvider):
    """每一步照 steps 走：一个异常 = 这一步什么都没出就失败；一串事件 = 照吐。"""

    def __init__(self, steps: list[AgentProviderError | list[ProviderEvent]]) -> None:
        super().__init__([])
        self.steps = steps

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
        step = self.steps[self.start_count - 1]
        await asyncio.sleep(0.01)
        if isinstance(step, AgentProviderError):
            raise step
        for event in step:
            yield event


def _answer(text: str) -> list[ProviderEvent]:
    return [ProviderTextDelta(text), ProviderCompleted("stop", None)]


def _notes_call(index: int) -> list[ProviderEvent]:
    return [
        ProviderToolCall(
            index=0,
            tool_call_id=f"call_notes_{index:04d}xxxx",
            tool_name="project_notes",
            arguments={"note_id": f"note-{index}"},
        ),
        ProviderCompleted("tool_calls", None),
    ]


def _retries(service, run_id: str) -> list[dict[str, object]]:
    return [payload for kind, payload in lifecycle_events(service, run_id) if kind == "model_turn_stream_retry"]


def test_which_failures_count_as_temporary() -> None:
    assert _transient_before_output(CONNECTION_CLOSED)
    assert _transient_before_output(CONCURRENCY)
    assert _transient_before_output(RELAY_STREAM_FAILED)
    assert _transient_before_output(EMPTY)
    assert _transient_before_output(_error("agent_provider_stream_idle_timeout", "Provider stream went idle"))
    # 配置不对、额度没了、模型不存在：重来也没用，照旧失败。
    assert not _transient_before_output(QUOTA_GONE)
    assert not _transient_before_output(
        _error("agent_provider_http_error", 'Provider returned HTTP 404: {"error":{"message":"NOT_FOUND"}}', upstream_status=404)
    )
    assert not _transient_before_output(
        _error("agent_provider_http_error", "Provider returned HTTP 400: missing `input.type` parameter", upstream_status=400)
    )


def test_a_connection_that_drops_midway_retries_that_step_instead_of_losing_the_run(tmp_path: Path) -> None:
    # 前两步查了资料，第三步还没出字连接就断了：重来第三步，整场照常做完，前面两步的结果都还在。
    async def scenario() -> None:
        provider = FailThenAnswerProvider([_notes_call(0), _notes_call(1), CONNECTION_CLOSED, _answer("两份笔记都查完了。")])
        tools = ImmediateNotesToolExecutor({"note": "短笔记"})
        service = build_service(tmp_path, provider, tool_executor=tools)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert tools.calls == 2
        assert provider.start_count == 4
        failed, retried = provider.messages_by_turn[2], provider.messages_by_turn[3]
        # 重来的请求 = 断掉的那个 + 末尾一句话：开头原样（缓存照样命中），但不是一字不差的同一个请求。
        assert retried[: len(failed)] == failed
        assert len(retried) == len(failed) + 1
        assert retried[-1]["role"] == "user"
        assert "还没收到回答就断了" in str(retried[-1]["content"])
        assert [m.content for m in service.store.list_messages(session.session_id) if m.role == "assistant"] == [
            "两份笔记都查完了。"
        ]
        retries = _retries(service, run.run_id)
        assert [item["error_code"] for item in retries] == ["agent_provider_request_failed"]
        assert retries[0]["error_details"] == CONNECTION_CLOSED.details
        assert retries[0]["cache_shape"]["reason"] == "append_only"
        await service.shutdown()

    asyncio.run(scenario())


def test_concurrency_limit_and_relay_stream_failure_are_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FailThenAnswerProvider([CONCURRENCY, RELAY_STREAM_FAILED, _answer("好了。")])
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        assert provider.start_count == 3
        assert [item["error_code"] for item in _retries(service, run.run_id)] == [
            "agent_provider_error_event",
            "agent_provider_http_error",
        ]
        # 第二次重来换掉上一次补的那句（带「第 2 次」），不往后堆两句。
        first_retry, second_retry = provider.messages_by_turn[1], provider.messages_by_turn[2]
        assert len(second_retry) == len(first_retry)
        assert first_retry[:-1] == second_retry[:-1]
        assert "第 1 次" in str(first_retry[-1]["content"])
        assert "第 2 次" in str(second_retry[-1]["content"])
        await service.shutdown()

    asyncio.run(scenario())


def test_an_empty_answer_gets_a_line_asking_for_an_answer_before_it_is_sent_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FailThenAnswerProvider([EMPTY, _answer("这就是回答。")])
        service = build_service(tmp_path, provider)
        session, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        retried = provider.messages_by_turn[1]
        assert retried[-1]["role"] == "user"
        assert "上一次回答是空的" in str(retried[-1]["content"])
        assert "调用工具接着做" in str(retried[-1]["content"])
        assert len(retried) == len(provider.messages_by_turn[0]) + 1
        await service.shutdown()

    asyncio.run(scenario())


def test_quota_errors_still_fail_right_away(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FailThenAnswerProvider([QUOTA_GONE, _answer("不该走到这里。")])
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_provider_http_error"
        assert provider.start_count == 1
        assert _retries(service, run.run_id) == []
        await service.shutdown()

    asyncio.run(scenario())


def test_retries_stop_after_the_usual_count_and_report_the_last_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FailThenAnswerProvider([CONNECTION_CLOSED] * (1 + MAX_TURN_STREAM_RETRIES))
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.error.code == "agent_provider_request_failed"
        assert provider.start_count == 1 + MAX_TURN_STREAM_RETRIES
        await service.shutdown()

    asyncio.run(scenario())


def test_stopping_while_it_waits_to_retry_ends_the_run_as_stopped(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(agent_service, "TURN_STREAM_RETRY_DELAYS", (30.0, 30.0))
        provider = FailThenAnswerProvider([CONNECTION_CLOSED, _answer("不该走到这里。")])
        service = build_service(tmp_path, provider)
        _, result = await create_turn(service)
        for _ in range(200):
            if _retries(service, result.run.run_id):
                break
            await asyncio.sleep(0.01)
        _, accepted = await service.cancel_run(result.run.run_id)
        assert accepted is True
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.CANCELLED
        assert provider.start_count == 1
        await service.shutdown()

    asyncio.run(scenario())
