"""改稿题 edit5「结尾三段」一章 5,282 字只剩 282 字之后的三处修补（9-27 晚真机 grok-4.6，隔离实例）。

那一道的调用序列：三次「先看修改」的 edit 都把自己新写的句子当 find（对不上原文）→ 退回整份 update，
只交了最后三段的新写法（252 字），说明里写「请作者确认后再保存」→ 前端默认的「直接保存」一挂出来就存了。
之后又交了六次只有 find、没有 replace 的 edit，每次都被收成「删掉这一段」（这几次撞上了已改的文件，没存进去）。

1. edit / edit_file 的 replace 必填：没写就报一句人话，不再当成删掉 find 那段；要删照样能删（replace: ""）。
2. 「先看修改」的说明不再说「作者点头之前什么都不会写」：默认的「直接保存」一挂出来就存；update 是整份文件。
3. 「先看修改」的 update 少了三成以上时，结果照「先看修改」说：存进去以后就只剩这些，只改一段交 edit。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_direct_write_tools import _build_runtime, _call
from test_write_honesty_20260927 import CHAPTER, _propose
from zhimeng_runtime.agent_tools import AgentToolError, _overwrite_note


def test_edit_file_without_replace_is_not_a_deletion(tmp_path: Path) -> None:
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content="第一段。\n\n第二段。\n", writer_mode="direct_write"
    )
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "第二段。"}))
    assert "没写 replace" in str(caught.value) and 'replace 为空字符串 ""' in str(caught.value)
    assert "不要" not in str(caught.value)
    assert chapter.read_text(encoding="utf-8") == "第一段。\n\n第二段。\n"

    # 写明 replace 为空字符串就是删掉那一段。
    asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "\n\n第二段。", "replace": ""}, call_id="call-0002"))
    assert chapter.read_text(encoding="utf-8") == "第一段。\n"


def test_a_proposal_edit_without_replace_is_not_a_deletion(tmp_path: Path, monkeypatch) -> None:
    result, project = _propose(
        tmp_path,
        monkeypatch,
        [{"operation": "edit", "path": "05_正文内容/第一卷/CH003_定稿.md", "find": "证人留下了带血钥匙"}],
    )
    assert result["status"] == "failed"
    assert "没写 replace" in result["error"]["error"]["message"]
    assert (project / "05_正文内容" / "第一卷" / "CH003_定稿.md").read_text(encoding="utf-8") == "第三章：证人留下了带血钥匙。"


def test_a_shrinking_proposal_update_says_what_is_left_and_how_to_change_one_passage() -> None:
    before = "\n".join(f"第 {n} 段，写了一些字。" for n in range(1, 101))
    note = _overwrite_note(before, "最后三段的新写法。", proposal=True)
    # 工具结果直接说明整份覆盖的后果和局部修改的做法。
    assert note.startswith("这是整份覆盖") and "只想改" in note
    assert "存进去以后" in note and "100 行" in note and "少 " in note
    assert "find 写文件里现在的原文" in note and "直接保存" in note and "undo_change" in note
    assert "不要" not in note
    # write_file 那边的说法不变。
    assert "edit_file" in _overwrite_note(before, "一句。")


def test_the_proposal_tool_tells_the_model_when_cards_are_saved(tmp_path: Path) -> None:
    executor, _run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content="第一段。\n", writer_mode="direct_write"
    )
    definition = next(item for item in executor.definitions if item.name == "change_set_propose")
    assert "Nothing touches disk" not in definition.description
    assert "直接保存" in definition.description and "先看修改" in definition.description
    assert "whole file" in definition.description and "undo_change" in definition.description
    for tool in ("edit_file", "change_set_propose"):
        schema = next(item for item in executor.definitions if item.name == tool).parameters
        text = str(schema)
        assert "'replace'" in text
    edit_schema = next(item for item in executor.definitions if item.name == "edit_file").parameters
    assert "replace" in edit_schema["required"]
