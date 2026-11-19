# -*- coding: utf-8 -*-
"""2026-09-25 真机走查（验收剧本第三节，隔离实例 + grok-4.6）抓到的问题的回归。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_agent_direct_write_tools import _build_runtime, _call
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import (
    DIRECT_WRITE_TOOL_NAMES,
    AgentToolError,
    PlanUpdateArguments,
    ProjectWriteFileArguments,
)


def _recorder(executor, run, database):
    """像真实链路那样把成功的工具结果写回 tool_call（写后版本钉子的来源）。"""

    store = AgentStore(database)

    def record(tool_name: str, arguments: dict, call_id: str):
        execution = asyncio.run(_call(executor, run, tool_name, arguments, call_id=call_id))
        store.finish_tool_call(
            run.run_id,
            tool_call_id=call_id,
            status="succeeded",
            output=execution.output,
            error=None,
            duration_ms=1,
        )
        return execution

    return record


def test_read_file_by_relative_path_survives_a_save(tmp_path: Path) -> None:
    """按路径读：保存之后 file_id 会换（按磁盘文件身份算），路径不会。"""
    executor, run, chapter, database, _cs, _project = _build_runtime(
        tmp_path, suffix="-read-path"
    )
    record = _recorder(executor, run, database)
    path = "05_正文内容/第一卷/CH001_正文.md"
    first = record("project_read_file", {"relative_path": path}, "call-read-1")
    assert first.output["content"] == chapter.read_bytes().decode("utf-8")
    assert first.output["relative_path"] == path
    old_id = first.output["file_id"]

    record(
        "project_edit_file",
        {"relative_path": path, "find": "第二行。", "replace": "第二行，改过。"},
        "call-edit-1",
    )
    again = record("project_read_file", {"relative_path": path.replace("/", "\\")}, "call-read-2")
    assert "第二行，改过。" in again.output["content"]

    if again.output["file_id"] != old_id:
        with pytest.raises(AgentToolError) as stale:
            asyncio.run(_call(executor, run, "project_read_file", {"file_id": old_id}, call_id="call-read-3"))
        assert stale.value.code == "agent_tool_file_not_found"
        assert "relative_path" in str(stale.value)

    with pytest.raises(AgentToolError) as missing:
        asyncio.run(
            _call(executor, run, "project_read_file", {"relative_path": "没有这个.md"}, call_id="call-read-4")
        )
    assert missing.value.code == "agent_tool_file_not_found"


def test_reading_past_the_end_returns_the_ending_with_a_note(tmp_path: Path) -> None:
    """真机：185 行的章从第 300 行读起，读回空的，模型没重试就凭印象写了一章。越界就给结尾并说明总行数。"""
    lines = "".join(f"第{index}行。\n" for index in range(1, 11))
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content=lines, suffix="-past-end"
    )
    result = asyncio.run(
        _call(
            executor,
            run,
            "project_read_file",
            {"relative_path": "05_正文内容/第一卷/CH001_正文.md", "start_line": 300, "max_lines": 3},
            call_id="call-read-past-end",
        )
    )
    assert result.output["content"].replace("\r\n", "\n") == "第8行。\n第9行。\n第10行。\n"
    assert result.output["start_line"] == 8
    assert result.output["total_lines"] == 10
    assert "只有 10 行" in result.output["note"]


def test_computer_tools_resolve_relative_paths_inside_the_open_book(tmp_path: Path) -> None:
    """模型把书里的相对路径交给电脑工具：先按当前这本书的文件夹找（真机原样）。"""
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path, suffix="-computer-rel")
    listing = asyncio.run(
        _call(executor, run, "computer_list_files", {"path": "05_正文内容/第一卷/"}, call_id="call-list-rel")
    )
    assert [entry["name"] for entry in listing.output["entries"]] == ["CH001_正文.md"]
    read = asyncio.run(
        _call(
            executor,
            run,
            "computer_read_file",
            {"path": "05_正文内容/第一卷/CH001_正文.md"},
            call_id="call-read-rel",
        )
    )
    assert "第一行。" in read.output["content"]


def test_undo_change_rolls_back_the_latest_changes_of_this_conversation(
    tmp_path: Path
) -> None:
    """作者说「刚那一章撤回」：Agent 用 project_undo_change，一次撤一步，新的先撤。"""
    executor, run, chapter, database, change_sets, project = _build_runtime(
        tmp_path, suffix="-undo"
    )
    # 9-26 起叫 undo_change；这条测试照旧按旧名字 project_undo_change 调，顺带核对旧名字在执行层还认。
    assert "undo_change" in DIRECT_WRITE_TOOL_NAMES
    record = _recorder(executor, run, database)
    new_path = "05_正文内容/第一卷/CH002_新章.md"
    created = record("project_write_file", {"relative_path": new_path, "content": "柳青烟很冷静。\n"}, "call-undo-write")
    edited = record(
        "project_edit_file",
        {"relative_path": new_path, "find": "很冷静", "replace": "慌了"},
        "call-undo-edit",
    )
    target = project / "05_正文内容" / "第一卷" / "CH002_新章.md"
    assert target.read_text(encoding="utf-8") == "柳青烟慌了。\n"

    first = record("project_undo_change", {}, "call-undo-0001")
    assert first.output["change_set_id"] == edited.output["change_set_id"]
    assert first.output["files"] == [{"relative_path": new_path, "undone_operation": "update"}]
    # 撤完还剩哪一次，如实告诉模型（整章要撤好几次时别只撤一次就说全撤了）。
    assert [item["change_set_id"] for item in first.output["remaining_in_conversation"]] == [
        created.output["change_set_id"]
    ]
    assert "还有 1 次" in first.output["message"]
    assert target.read_text(encoding="utf-8") == "柳青烟很冷静。\n"
    assert change_sets.get(edited.output["change_set_id"]).status.value == "rolled_back"

    second = record("project_undo_change", {}, "call-undo-0002")
    assert second.output["change_set_id"] == created.output["change_set_id"]
    assert second.output["remaining_in_conversation"] == []
    assert "都撤完了" in second.output["message"]
    assert not target.exists()
    assert chapter.exists()

    with pytest.raises(AgentToolError) as nothing_left:
        asyncio.run(_call(executor, run, "project_undo_change", {}, call_id="call-undo-0003"))
    assert nothing_left.value.code == "agent_tool_undo_nothing"

    with pytest.raises(AgentToolError) as again:
        asyncio.run(
            _call(
                executor,
                run,
                "project_undo_change",
                {"change_set_id": created.output["change_set_id"]},
                call_id="call-undo-0004",
            )
        )
    assert again.value.code == "agent_tool_undo_not_applied"


def test_undo_change_is_not_offered_in_read_only_mode(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, writer_mode="repair_only", suffix="-undo-ro"
    )
    with pytest.raises(AgentToolError) as refused:
        asyncio.run(_call(executor, run, "project_undo_change", {}, call_id="call-undo-ro"))
    assert refused.value.code == "writer_change_set_forbidden"


def test_numbered_notes_at_the_book_root_are_not_chapters() -> None:
    """真机：宗门书根的《00_标准化迁移说明.md》在目录里成了「第 000 章」。根目录只靠开头数字的说明不算章。"""
    from zhimeng_runtime.project_reader import classify_project_file_kind

    for note in (
        "00_项目总览.md",
        "00_标准化迁移说明.md",
        "10_导出.md",
        # 标着「勿作为真值」的旧稿副本夹：里面的旧章也不进目录（真机：宗门旧归档夹的第 1–3 章排在正文最前面）
        "00_归档_旧结构_勿作为真值/06_正文/第001章_穿越成破产掌门.md",
    ):
        assert classify_project_file_kind(note) == "document", note
    for chapter in (
        "001_开篇.md",
        "第005章_厨子的药膳与账房的符.md",
        "CH003_试剑.md",
        "05_正文内容/第一卷/01_开篇.md",
        "正文/001_开篇.md",
    ):
        assert classify_project_file_kind(chapter) == "prose", chapter


def test_write_file_ignores_a_garbled_files_duplicate_when_single_file_args_are_complete() -> None:
    """真机原样：路径和正文都给全了，又附带一段没写完的 files 字符串 → 按单文件写，不白写一遍。"""
    parsed = ProjectWriteFileArguments.model_validate(
        {
            "relative_path": "05_正文内容/第005章_厨子的药膳与账房的符.md",
            "content": "# 第005章 厨子的药膳与账房的符\n\n山门外……",
            "files": '[{"relative_path":"05_正文内容/第005章_厨子的药膳与账房的符.md","content":"# 第005章',
            "note": '本章已按控制卡要求完成。"}]',
        }
    )
    # 9-26 起参数叫 path，旧名 relative_path 照收。
    assert parsed.path == "05_正文内容/第005章_厨子的药膳与账房的符.md"
    assert parsed.files is None


def test_write_file_accepts_files_that_repeat_the_single_file() -> None:
    """真机第二回：files 解得开，指的就是同一个文件（路径写法略有不同）→ 按单文件收下。"""
    parsed = ProjectWriteFileArguments.model_validate(
        {
            "relative_path": "05_正文内容/第005章.md",
            "content": "正文",
            "files": json.dumps(
                [{"relative_path": ".\\05_正文内容\\第005章.md", "content": "正文"}], ensure_ascii=False
            ),
        }
    )
    assert parsed.path == "05_正文内容/第005章.md"
    assert parsed.files is None


def test_edit_file_matches_a_find_text_the_model_escaped_twice(
    tmp_path: Path
) -> None:
    """模型把换行写成字面的 \\n（真机原样）：原样找不到时按解开的再找一次。"""
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content="第一行。\n第二行。\n", suffix="-escaped"
    )
    result = asyncio.run(
        _call(
            executor,
            run,
            "project_edit_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "find": "第一行。\\n第二行。",
                "replace": "第一行。\\n第二行，她慌了。",
            },
            call_id="call-edit-escaped",
        )
    )
    assert result.output["applied"] is True
    assert chapter.read_bytes().decode("utf-8").replace("\r\n", "\n") == "第一行。\n第二行，她慌了。\n"


def test_json_encoded_list_arguments_are_decoded() -> None:
    """数组参数被包成 JSON 字符串时照常收下。"""
    files = ProjectWriteFileArguments.model_validate(
        {"files": json.dumps([{"relative_path": "设定/人物.md", "content": "柳青烟"}], ensure_ascii=False)}
    )
    assert files.files is not None
    assert [item.path for item in files.files] == ["设定/人物.md"]
    plan = PlanUpdateArguments.model_validate(
        {"items": json.dumps([{"content": "读第 4 章", "status": "in_progress"}], ensure_ascii=False)}
    )
    assert [item.content for item in plan.items] == ["读第 4 章"]


def test_ambiguous_or_broken_write_arguments_are_still_rejected() -> None:
    """解得开的 files 数组 + 单文件参数仍是两种写法混用；解不开又没有单文件参数的照常报错。"""
    with pytest.raises(ValidationError):
        ProjectWriteFileArguments.model_validate(
            {
                "relative_path": "a.md",
                "content": "x",
                "files": json.dumps([{"relative_path": "b.md", "content": "y"}]),
            }
        )
    with pytest.raises(ValidationError):
        ProjectWriteFileArguments.model_validate({"files": "不是数组"})
