"""隔着别的调用又回来的一模一样的调用、重读读过的段，在运行循环里怎么提醒（9-27 晚，照 Crush loop_detection 的窗口，只提醒不停）。

9-27 改稿题里 grok-4.6 的打转有两种样子，以前的「连着一模一样」计数一次都没响：
- 「读 → 改 → 读 → 改」交替：同一个 find 交了 4 次、同一段读了 6 次（edit4「并段」）；
- 同一个文件每次换几行读三十多遍（edit4「近似」）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_agent_service import TurnScriptedProvider, build_service, create_turn, wait_terminal
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_tools import AgentToolExecution
from zhimeng_runtime.execution_protocol import RunStatus

CHAPTER = "05_正文内容/第一卷文件夹/001-050章/002_铁剑门来访.md"


class ReadEditExecutor:
    """只认 read_file / edit_file 两个名字的假执行器：读回一段固定正文，改回「没找到」。"""

    def __init__(self) -> None:
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.definitions = (
            ProviderToolDefinition(name="read_file", version="1", description="Read a file", parameters=schema),
            ProviderToolDefinition(name="edit_file", version="1", description="Edit a file", parameters=schema),
        )
        self.calls = 0

    @staticmethod
    def request_hash(tool_name: str, tool_version: str, arguments: dict[str, object]) -> str:
        del tool_name, tool_version, arguments
        return "b" * 64

    async def execute(self, *args, **kwargs) -> AgentToolExecution:
        del args, kwargs
        self.calls += 1
        return AgentToolExecution(output={"text": "第二天，赵铁山来得很早。", "total_lines": 457})


def _call(index: int, name: str, arguments: dict[str, object]) -> list:
    return [
        ProviderToolCall(index=0, tool_call_id=f"call_loop_{index:04d}xxxx", tool_name=name, arguments=arguments),
        ProviderCompleted("tool_calls", None),
    ]


def _read(index: int, start: int, lines: int) -> list:
    return _call(index, "read_file", {"path": CHAPTER, "start_line": start, "max_lines": lines})


def _edit(index: int) -> list:
    return _call(index, "edit_file", {"path": CHAPTER, "find": "第二天，赵铁山来得很早。\n\n天才亮", "replace": "天才亮"})


def _last_user_note(messages: list[dict[str, object]]) -> str:
    last = messages[-1]
    return str(last["content"]) if last.get("role") == "user" else ""


def test_an_edit_that_keeps_coming_back_between_reads_gets_a_note(tmp_path: Path) -> None:
    async def scenario() -> None:
        steps = []
        for round_index in range(3):
            steps.append(_read(2 * round_index, 1, 30))
            steps.append(_edit(2 * round_index + 1))
        steps.append([ProviderTextDelta("改不动，原文对不上。"), ProviderCompleted("stop", None)])
        provider = TurnScriptedProvider(steps)
        executor = ReadEditExecutor()
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        # 读的三次都执行了；改的三次在这个挡位下被拦下（被拦的调用也照样计数，反复撞同一个拦正是要说的）。
        assert executor.calls >= 3
        notes = [_last_user_note(messages) for messages in provider.messages_by_turn]
        # 第 5 步（第三次读同一段）和第 6 步（第三次交同一个 find）之后各有一句，前面四步都没有。
        assert all("一模一样" not in note for note in notes[:5])
        assert "read_file" in notes[5] and "第 3 次" in notes[5]
        assert "edit_file" in notes[6] and "第 3 次" in notes[6]
        assert "不要" not in notes[5] + notes[6]
        await service.shutdown()

    asyncio.run(scenario())


def test_rereading_lines_already_read_gets_a_note_but_paging_forward_does_not(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                _read(0, 1, 100),
                _read(1, 101, 100),  # 往后翻页：新内容
                _read(2, 20, 60),  # 第 20–79 行读过了：第 1 次重读
                _read(3, 1, 50),  # 第 2 次重读 → 说一声
                [ProviderTextDelta("读完了。"), ProviderCompleted("stop", None)],
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=ReadEditExecutor())
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        notes = [_last_user_note(messages) for messages in provider.messages_by_turn]
        assert all("已经读过了" not in note for note in notes[:4])
        assert "第 1–50 行这一轮已经读过了" in notes[4]
        assert "002_铁剑门来访.md" in notes[4]
        assert "读了 4 次" in notes[4]
        await service.shutdown()

    asyncio.run(scenario())


def test_the_third_time_counts_even_when_it_directly_follows_the_second(tmp_path: Path) -> None:
    # A、B、A、A：第 3 次恰好紧跟第 2 次，算「连着第 2 次」，以前两边都不响（9-28 改稿题 edit6「并段」第 13、17 步就是这样）。
    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [
                _edit(0),
                _read(1, 1, 30),
                _edit(2),
                _edit(3),
                [ProviderTextDelta("还是改不动。"), ProviderCompleted("stop", None)],
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=ReadEditExecutor())
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        notes = [_last_user_note(messages) for messages in provider.messages_by_turn]
        assert all("一模一样" not in note for note in notes[:4])
        assert "edit_file" in notes[4] and "第 3 次" in notes[4]
        await service.shutdown()

    asyncio.run(scenario())
