"""会话摘要接进 Agent 循环之后的端到端行为。

分工：``test_session_checkpoint.py`` 管纯函数、``test_session_checkpoint_store.py``
管落库与注入，本文件管「什么时候真的会去折叠」以及折完之后下一轮是否用上了。

这里也是 §102 第 14/15 条在真实运行里成立的证据：对话变长时最早的内容不是
被删掉，而是被折成一段摘要继续跟着走。

9-28 改：原样留下最近几轮按字数（至少最后一整轮），更早的一次折完——以前固定留 N 轮、一次最多折 4 万字，
过线以后每说一句都要整理一次（压缩考题 base1）。摘要缺节重来一次；程序在摘要后面附工具事实和作者原话。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.model_context import (
    CONTEXT_WINDOW_SPECS,
    ContextWindowSpec,
    resolve_context_window_spec,
)
from zhimeng_runtime.session_checkpoint import (
    CHECKPOINT_AUTHOR_WORDS_TITLE,
    CHECKPOINT_SECTION_TITLES,
    render_checkpoint_block,
)

from test_agent_service import (  # noqa: E402 - 复用既有的 Agent 服务测试夹具
    FINGERPRINT,
    PROJECT_ID,
    TurnScriptedProvider,
    build_service,
    wait_terminal,
)

ANSWER = "答" * 4000


def _summary(goal: str, failed: str = "把主角改成女性，作者当场否掉了。") -> str:
    """七节都有的一份摘要（摘要闸门要七节齐）。"""
    body = {"当前目标": goal, "试过但不行的做法": failed}
    return "\n".join(f"【{title}】{body.get(title, '无')}" for title in CHECKPOINT_SECTION_TITLES)


SUMMARY = _summary("写完第 3 章。")


def full_summary(text: str) -> str:
    """别的用例用：把一句话包成七节齐全的摘要（第一节是这句话，其余写「无」）。"""
    return _summary(text, "无")


SUMMARY_2 = _summary("写完第 4 章。")

# 折叠机制的端到端用例用一份小阈值档位，几千字就能触发；
# 档位表的真实数值在 test_model_context.py 里钉。
# 一轮约 4,000 字：第 3 轮答完过 9,000 字的线；原样只留得下最后一轮（4,500 字以内）。
SMALL_SPEC = ContextWindowSpec(
    tier="256k",
    label="256K",
    window_tokens=256_000,
    context_token_budget=209_920.0,
    checkpoint_trigger_characters=9_000,
    checkpoint_max_characters=3_600,
    checkpoint_keep_recent_characters=4_500,
    checkpoint_author_words_characters=2_000,
    pack_max_characters=200_000,
    pack_max_tokens=100_000,
)


@pytest.fixture
def small_window_spec(monkeypatch):
    monkeypatch.setitem(CONTEXT_WINDOW_SPECS, "256k", SMALL_SPEC)


def _answer_turn(text: str = ANSWER) -> list[object]:
    return [ProviderTextDelta(text), ProviderCompleted("stop", None)]


def _summary_turn(text: str) -> list[object]:
    return [ProviderTextDelta(text), ProviderCompleted("stop", None)]


async def _ask(service, session_id: str, index: int):
    """问一轮，并且等这一轮的后台任务彻底收尾。

    折叠发生在「作者已经拿到回答」之后：Run 先转成终态，摘要才开始整理。
    所以只等终态是不够的，还要等这条 Run 的任务真正结束。
    """
    result = await service.start_run(
        session_id=session_id,
        message=f"第 {index} 问",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key=f"turn-{index:08d}",
    )
    run = await wait_terminal(service, result.run.run_id)
    for _ in range(1000):
        if service._tasks.get(result.run.run_id) is None:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("Run task did not finish")


def _events(service, run_id: str) -> list[str]:
    with service.store.database.connection() as connection:
        rows = connection.execute(
            "SELECT event_type FROM run_event WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
    return [row[0] for row in rows]


def test_short_conversations_are_never_folded(tmp_path: Path) -> None:
    """没到阈值就不该多打一次模型——省钱，也别把短对话搅乱。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider([_answer_turn("短答")])
        service = build_service(tmp_path, provider)
        session = await service.create_session(
            project_id=PROJECT_ID, title="Writing Agent"
        )
        run = await _ask(service, session.session_id, 1)

        assert run.status is RunStatus.SUCCEEDED
        assert service.store.latest_session_checkpoint(session.session_id) is None
        # 只打了一次模型：没有额外的整理请求。
        assert provider.start_count == 1
        await service.shutdown()

    asyncio.run(scenario())


def test_long_history_is_folded_once_and_the_next_turn_does_not_fold_again(tmp_path: Path, small_window_spec) -> None:
    """§102 第 14 条的端到端证据：最早的对话被折成摘要，而不是被删掉；折完留下的不到线，下一轮不再整理。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                _answer_turn(),  # 第 1 轮
                _answer_turn(),  # 第 2 轮
                _answer_turn(),  # 第 3 轮：答完过线，折掉第 1、2 轮，原样留第 3 轮
                _summary_turn(SUMMARY),
                _answer_turn(),  # 第 4 轮：带着摘要出发；答完只有第 3、4 轮 8,000 字，不到线，不整理
                _answer_turn(),  # 第 5 轮：又过线，折掉第 3、4 轮
                _summary_turn(SUMMARY_2),
            ]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(
            project_id=PROJECT_ID, title="Writing Agent"
        )

        for index in (1, 2, 3):
            assert (await _ask(service, session.session_id, index)).status is RunStatus.SUCCEEDED

        record = service.store.latest_session_checkpoint(session.session_id)
        assert record is not None, "越过阈值之后应当折出一份摘要"
        # 一次折到只剩最后一轮：第 1、2 轮（序号 1–4）。
        assert record.through_ordinal == 4
        assert record.source_message_count == 4
        assert record.run_id is not None
        # 落库的 = 模型写的七节 + 程序附的作者原话（这个用例没有工具调用，所以没有「程序记下的事实」）。
        assert record.content.startswith(SUMMARY)
        assert CHECKPOINT_AUTHOR_WORDS_TITLE in record.content
        assert "- 第 1 问" in record.content and "- 第 2 问" in record.content
        assert "第 3 问" not in record.content

        # 整理请求本身：带着七小节的整理说明，把被折叠的原文喂了进去，留下的那一轮没喂。
        compaction_request = provider.messages_by_turn[3]
        assert compaction_request[0]["role"] == "system"
        assert "试过但不行的做法" in str(compaction_request[0]["content"])
        assert "第 1 问" in str(compaction_request[-1]["content"])
        assert "第 2 问" in str(compaction_request[-1]["content"])
        assert "第 3 问" not in str(compaction_request[-1]["content"])

        # 下一轮：摘要出现在最前面，被折叠的原文不再逐条重发。
        run_4 = await _ask(service, session.session_id, 4)
        assert run_4.status is RunStatus.SUCCEEDED
        contents = [str(item["content"]) for item in provider.messages_by_turn[4]]
        summary_block = render_checkpoint_block(through_ordinal=4, content=record.content)
        assert summary_block in contents
        assert contents.index(summary_block) < contents.index("第 3 问")
        assert "第 1 问" not in contents
        # 第 4 轮答完没有再整理：以前过线以后每一轮都要整理一次。
        assert provider.start_count == 5
        assert service.store.latest_session_checkpoint(session.session_id).through_ordinal == 4
        assert "session_checkpoint_written" not in _events(service, run_4.run_id)

        # 第 5 轮答完又过线：再折一次，上一份摘要只把模型写的那部分交回去合并。
        assert (await _ask(service, session.session_id, 5)).status is RunStatus.SUCCEEDED
        assert provider.start_count == 7
        second = service.store.latest_session_checkpoint(session.session_id)
        assert second.through_ordinal == 8
        second_request = str(provider.messages_by_turn[6][-1]["content"])
        assert second_request.startswith(f"【上一份摘要】\n{SUMMARY}\n\n【要整理的对话】")
        assert CHECKPOINT_AUTHOR_WORDS_TITLE not in second_request
        # 作者原话按整段已折叠的历史重拼：第 1–4 问都在。
        assert second.content.startswith(SUMMARY_2)
        for index in (1, 2, 3, 4):
            assert f"- 第 {index} 问" in second.content

        await service.shutdown()

    asyncio.run(scenario())


def test_a_summary_missing_sections_is_retried_once_with_a_note(tmp_path: Path, small_window_spec) -> None:
    """摘要闸门（OpenClaw）：缺节就说一声重来一次；第二份齐了才落库。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                _answer_turn(),
                _answer_turn(),
                _answer_turn(),
                _summary_turn("【当前目标】写完第 3 章。"),  # 缺六节
                _summary_turn(SUMMARY),
            ]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        for index in (1, 2, 3):
            await _ask(service, session.session_id, index)

        assert provider.start_count == 5
        retry = str(provider.messages_by_turn[4][-1]["content"])
        assert "上一次整理少了这几节" in retry and "【试过但不行的做法】" in retry
        record = service.store.latest_session_checkpoint(session.session_id)
        assert record is not None and record.content.startswith(SUMMARY)
        await service.shutdown()

    asyncio.run(scenario())


def test_a_summary_still_missing_sections_is_not_folded_while_the_history_is_near_the_line(
    tmp_path: Path, small_window_spec
) -> None:
    """重来一次还缺节、还没过线太多：先不折（原文都还在上下文里），记一条事件，下一轮再整理。"""

    async def scenario() -> None:
        partial_summary = "【当前目标】写完第 3 章。"
        provider = TurnScriptedProvider(
            [
                _answer_turn(),
                _answer_turn(),
                _answer_turn(),
                _summary_turn(partial_summary),
                _summary_turn(partial_summary),
            ]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        for index in (1, 2):
            await _ask(service, session.session_id, index)
        run_3 = await _ask(service, session.session_id, 3)

        assert provider.start_count == 5
        assert service.store.latest_session_checkpoint(session.session_id) is None
        assert "session_checkpoint_rejected" in _events(service, run_3.run_id)
        await service.shutdown()

    asyncio.run(scenario())


def test_a_summary_still_missing_sections_is_kept_once_the_history_is_far_past_the_line(
    tmp_path: Path, small_window_spec
) -> None:
    """过线 1.5 倍以后还缺节：照收，缺的写明，别让原文一直涨到被截掉。"""

    async def scenario() -> None:
        partial_summary = "【当前目标】写完第 3 章。"
        long_answer = "答" * 7_000
        provider = TurnScriptedProvider(
            [
                _answer_turn(long_answer),
                _answer_turn(long_answer),  # 两轮 14,000 字 > 9,000 × 1.5
                _summary_turn(partial_summary),
                _summary_turn(partial_summary),
            ]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        for index in (1, 2):
            await _ask(service, session.session_id, index)

        record = service.store.latest_session_checkpoint(session.session_id)
        assert record is not None
        assert record.content.startswith(partial_summary)
        assert "【还没做、接下来要做的事】（整理时没写出这一节）" in record.content
        await service.shutdown()

    asyncio.run(scenario())


def test_a_failed_summary_never_breaks_the_finished_run(tmp_path: Path, small_window_spec) -> None:
    """整理失败只记事件：作者那一轮已经答完了，不许被连累。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                _answer_turn(),
                _answer_turn(),
                _answer_turn(),  # 第 3 轮结束过线，才有东西可折
                # 整理这一轮交回空白，等于整理失败。
                [ProviderTextDelta("   "), ProviderCompleted("stop", None)],
            ]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(
            project_id=PROJECT_ID, title="Writing Agent"
        )

        for index in (1, 2, 3):
            run = await _ask(service, session.session_id, index)
            assert run.status is RunStatus.SUCCEEDED

        # 整理请求确实发出去了（第 4 次模型调用），只是交回了空白。
        assert provider.start_count == 4
        # 没折出摘要，但第 3 轮依然是成功的。
        assert service.store.latest_session_checkpoint(session.session_id) is None
        messages = service.store.list_messages(session.session_id)
        assert [item.role for item in messages][-1] == "assistant"
        await service.shutdown()

    asyncio.run(scenario())


def test_trigger_threshold_matches_the_documented_budget() -> None:
    """触发阈值按上下文窗口档位查表——改动它等于改动折叠时机，必须是显式决定。"""
    assert resolve_context_window_spec("256k").checkpoint_trigger_characters == 60_000
    assert resolve_context_window_spec("1m").checkpoint_trigger_characters == 240_000
