from __future__ import annotations

from zhimeng_runtime.agent_service import (
    REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS,
    REPEAT_TOOL_WINDOW,
    RepeatToolChain,
    _read_call_range,
    repeat_tool_reminder_text,
    reread_reminder_text,
    scattered_repeat_reminder_text,
    update_pending_repeat_reminder,
)


def test_repeat_chain_counts_consecutive_same_calls_and_resets_on_change() -> None:
    chain = RepeatToolChain()
    args = '{"path":"01.md"}'
    assert chain.observe("read_file", args) == 1
    assert chain.observe("read_file", args) == 2
    assert chain.observe("read_file", args) == 3
    assert chain.observe("read_file", '{"path":"02.md"}') == 1
    assert chain.observe("skill_execute", '{"path":"02.md"}') == 1
    assert chain.observe("skill_execute", '{"path":"02.md"}') == 2


def test_reminder_text_only_fires_at_thresholds() -> None:
    args = '{"skill_id":"creation.prewrite_gate"}'
    assert repeat_tool_reminder_text("skill_execute", 1, args) is None
    assert repeat_tool_reminder_text("skill_execute", 2, args) is None
    first = repeat_tool_reminder_text("skill_execute", 3, args)
    assert first is not None
    assert "重复调用同一个工具" in first
    assert "skill_execute" not in first
    detailed = repeat_tool_reminder_text("skill_execute", 5, args)
    assert detailed is not None
    assert "skill_execute" in detailed
    assert "连续次数：5" in detailed
    assert args in detailed
    eighth = repeat_tool_reminder_text("skill_execute", 8, args)
    assert eighth is not None
    assert "连续次数：8" in eighth
    assert repeat_tool_reminder_text("skill_execute", 4, args) is None
    assert repeat_tool_reminder_text("skill_execute", 6, args) is None


def test_detailed_reminder_truncates_large_arguments() -> None:
    args = "x" * (REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS + 80)
    text = repeat_tool_reminder_text("write_file", 5, args)
    assert text is not None
    assert "另省略 80 字符" in text
    assert args not in text
    assert ("x" * REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS) in text
    assert ("x" * (REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS + 1)) not in text


def test_pending_reminder_survives_overshoot_and_clears_on_reset() -> None:
    chain = RepeatToolChain()
    args = '{"path":"01.md"}'
    pending: str | None = None
    for _ in range(4):
        count = chain.observe("read_file", args)
        pending = update_pending_repeat_reminder(
            pending,
            count,
            repeat_tool_reminder_text("read_file", count, args),
        )
    assert pending is not None
    assert "重复调用同一个工具" in pending

    count = chain.observe("read_file", '{"path":"02.md"}')
    pending = update_pending_repeat_reminder(
        pending,
        count,
        repeat_tool_reminder_text("read_file", count, '{"path":"02.md"}'),
    )
    assert pending is None


def test_consecutive_reminder_keeps_speaking_after_the_eighth_time() -> None:
    # 9-27 前第 9 次起就不吭声了（阈值只有 3 / 5 / 8）；grok 连交同一个调用十几次时，后面几次一句话都没有。
    args = '{"path":"01.md"}'
    assert repeat_tool_reminder_text("read_file", 7, args) is None
    for count in (8, 9, 12, 20):
        text = repeat_tool_reminder_text("read_file", count, args)
        assert text is not None
        assert f"连续次数：{count}" in text


def test_reminders_state_facts_without_saying_dont() -> None:
    # 作者 9-20：提示词里不塞「不要 / 不许」。
    args = '{"path":"01.md"}'
    texts = [
        repeat_tool_reminder_text("read_file", 3, args),
        repeat_tool_reminder_text("read_file", 5, args),
        scattered_repeat_reminder_text("edit_file", 3),
        reread_reminder_text("004.md", 1, 30, 4, 2),
    ]
    for text in texts:
        assert text is not None
        assert "不要" not in text and "不许" not in text


def test_window_count_sees_calls_that_come_back_after_something_else() -> None:
    chain = RepeatToolChain()
    edit = '{"find":"沈青云刚要开口","path":"004.md"}'
    read = '{"max_lines":30,"path":"004.md","start_line":1}'
    for _ in range(3):
        assert chain.observe("edit_file", edit) == 1
        assert chain.observe("read_file", read) == 1
    assert chain.window_count("edit_file", edit) == 3
    assert chain.window_count("read_file", read) == 3
    for index in range(REPEAT_TOOL_WINDOW):
        chain.observe("list_files", f'{{"path":"dir-{index}"}}')
    assert chain.window_count("edit_file", edit) == 0


def test_scattered_reminder_thresholds() -> None:
    assert scattered_repeat_reminder_text("edit_file", 2) is None
    assert scattered_repeat_reminder_text("edit_file", 4) is None
    for count in (3, 5, 8, 10):
        text = scattered_repeat_reminder_text("edit_file", count)
        assert text is not None and f"第 {count} 次" in text and "edit_file" in text


def test_rereads_count_only_lines_already_read_since_the_last_change() -> None:
    chain = RepeatToolChain()
    assert chain.observe_read("第一卷/004.md", 1, 100) == (1, 0)
    assert chain.observe_read("第一卷/004.md", 101, 200) == (2, 0)  # 往后翻页读新的部分，不算重读
    assert chain.observe_read("第一卷/004.md", 20, 79) == (3, 1)
    assert chain.observe_read("第一卷/004.md", 1, 50) == (4, 2)
    chain.observe_change()  # 这一轮改过文件：之后再读是核对
    assert chain.observe_read("第一卷/004.md", 1, 50) == (5, 0)


def test_reread_reminder_thresholds() -> None:
    assert reread_reminder_text("004.md", 1, 30, 3, 1) is None
    assert reread_reminder_text("004.md", 1, 30, 5, 3) is None
    for rereads in (2, 4, 6, 9):
        text = reread_reminder_text("004.md", 1, 30, rereads + 1, rereads)
        assert text is not None and "第 1–30 行" in text and "一次读完" in text


def test_read_call_range_matches_relative_and_absolute_paths() -> None:
    relative = _read_call_range({"path": "05_正文内容/第一卷文件夹/001-050章/004_玉佩上的青云.md", "start_line": 700, "max_lines": 50})
    absolute = _read_call_range(
        {"path": r"C:\Users\x\books\宗门\05_正文内容\第一卷文件夹\001-050章\004_玉佩上的青云.md", "start_line": 700, "max_lines": 50}
    )
    assert relative is not None and absolute is not None
    assert relative[0] == absolute[0]
    assert relative[1] == "004_玉佩上的青云.md"
    assert relative[2:] == (700, 749)
    assert _read_call_range({"path": "a.md"})[2:] == (1, 800)
    assert _read_call_range({"path": "a.md", "start_offset": 1200}) is None
    assert _read_call_range({}) is None
