"""改的那段不在给的文件里时，报出打开的文件夹里哪个文件第几行有（9-28 改稿题 edit7「台词」，隔离实例 grok-4.6）。

那一道任务是第 2 章，它在第 1 章里找「苍云宗。」，工具只回「可能不是这个文件」，没说在哪个文件，
它就把第 1 章从头读了 35 遍，6 分钟超时。edit_file 和「先看修改」的 edit 都一样：找不到时把别处有这段的文件和行号说出来。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_direct_write_tools import _build_runtime, _call
from test_write_honesty_20260927 import CHAPTER, _propose
from zhimeng_runtime.agent_tools import AgentToolError, AgentToolExecutor


def _second_chapter(project: Path, text: str) -> Path:
    target = project / "05_正文内容" / "第一卷" / "CH002_正文.md"
    target.write_text(text, encoding="utf-8")
    return target


def test_edit_file_says_which_file_has_the_passage(tmp_path: Path) -> None:
    executor, run, chapter, _db, _cs, project = _build_runtime(
        tmp_path, chapter_content="第一章。\n\n沈青云推开门。\n", writer_mode="direct_write"
    )
    other = _second_chapter(project, "第二章。\n\n剑鞘敲着石阶。\n\n“苍云宗。”\n\n他身后弟子跟着笑。\n")
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "“苍云宗。”", "replace": "“苍云宗！”"}))
    message = str(caught.value)
    first_line = message.split("\n", 1)[0]
    assert "「CH001_正文.md」里没有" in first_line
    assert "「05_正文内容/第一卷/CH002_正文.md」第 5 行" in first_line
    assert "把 path 换成它" in first_line
    # 原来的比对结果照样在后面。
    assert "在文件里没有找到要替换的这段文字" in message
    assert "不要" not in message
    # 两个文件都没动。
    assert chapter.read_text(encoding="utf-8") == "第一章。\n\n沈青云推开门。\n"
    assert "“苍云宗。”" in other.read_text(encoding="utf-8")


def test_the_other_file_is_found_even_when_quotes_are_written_differently(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, chapter_content="第一章。\n", writer_mode="direct_write"
    )
    _second_chapter(project, "第二章。\n\n赵铁山说：“苍云宗。”\n")
    with pytest.raises(AgentToolError) as caught:
        # 模型抄成了直引号：原样找不到，放宽标点再找。
        asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": '赵铁山说："苍云宗。"', "replace": "x"}))
    assert "CH002_正文.md」第 3 行" in str(caught.value)


def test_no_hint_when_the_passage_is_nowhere(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, chapter_content="第一章。\n", writer_mode="direct_write"
    )
    _second_chapter(project, "第二章。\n")
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "哪里都没有的一句话", "replace": "x"}))
    assert "打开的文件夹里" not in str(caught.value)
    assert str(caught.value).startswith("在文件里没有找到要替换的这段文字")


def test_a_proposal_edit_aimed_at_the_wrong_file_says_where_the_passage_is(tmp_path: Path, monkeypatch) -> None:
    result, project = _propose(
        tmp_path,
        monkeypatch,
        [{"operation": "edit", "path": "05_正文内容/第一卷/CH001_定稿.md", "find": "证人留下了带血钥匙", "replace": "证人留下了一把钥匙"}],
    )
    assert result["status"] == "failed"
    message = result["error"]["error"]["message"]
    assert "「CH001_定稿.md」里没有" in message and "「05_正文内容/第一卷/CH003_定稿.md」第 1 行" in message
    assert (project / "05_正文内容" / "第一卷" / "CH003_定稿.md").read_text(encoding="utf-8") == "第三章：证人留下了带血钥匙。"


def test_passage_elsewhere_skips_the_target_and_lists_at_most_three(tmp_path: Path) -> None:
    names = [f"第{index}章.md" for index in range(1, 6)]
    for name in names:
        (tmp_path / name).write_text("开头。\n同一句话。\n", encoding="utf-8")
    hint = AgentToolExecutor._passage_elsewhere(tmp_path, names, "同一句话。", names[0])
    assert "「第1章.md」里没有" in hint
    assert "「第1章.md」第" not in hint
    assert hint.count("第 2 行") == 3 and "等 4 个文件" in hint
    assert AgentToolExecutor._passage_elsewhere(tmp_path, names, "   ", names[0]) == ""
