# -*- coding: utf-8 -*-
"""2026-09-26 真机走查（隔离实例 + grok-4.6，分叉 / 整理记忆 / 停止再继续 / 换模型）抓到的工具层问题的回归。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_agent_direct_write_tools import _build_runtime, _call
from zhimeng_runtime.agent_tools import (
    AgentToolError,
    AgentToolExecutor,
    AiSmellScoreArguments,
    ComputerListFilesArguments,
    ComputerReadFileArguments,
    ComputerRunCommandArguments,
    ProjectListFilesArguments,
    ProjectReadFileArguments,
    ProjectUndoChangeArguments,
    SkillListFilesArguments,
    SkillSearchArguments,
)


def test_blank_optional_text_arguments_count_as_not_given() -> None:
    # grok-4.6 连着 7 次给 computer_list_files 传 "query": ""，次次校验失败，转去命令行里试 ls。
    assert ComputerListFilesArguments.model_validate({"path": "", "query": ""}).query is None
    assert ComputerListFilesArguments.model_validate({"query": "  "}).query is None
    assert ProjectListFilesArguments.model_validate({"query": ""}).query is None
    assert SkillListFilesArguments.model_validate({"skill": "", "query": ""}).skill is None
    # 空的编号当「撤这个对话里最近的一次」，不再因为不合编号格式整次拒掉。
    assert ProjectUndoChangeArguments.model_validate({"change_set_id": ""}).change_set_id is None
    read = ProjectReadFileArguments.model_validate({"relative_path": "05_正文内容/004.md", "file_id": ""})
    assert read.file_id is None and read.relative_path == "05_正文内容/004.md"
    smell = AiSmellScoreArguments.model_validate({"text": "", "relative_path": "05_正文内容/004.md", "file_id": ""})
    assert smell.text is None and smell.file_id is None


def test_count_arguments_out_of_range_use_the_nearest_bound() -> None:
    assert ComputerListFilesArguments.model_validate({"limit": 1000}).limit == 500
    assert ComputerListFilesArguments.model_validate({"limit": 0}).limit == 1
    assert SkillSearchArguments.model_validate({"skill": "小说创作", "query": "伏笔", "limit": 999}).limit == 50
    assert ComputerReadFileArguments.model_validate({"path": "C:/a.md", "start_line": 0}).start_line == 1
    assert ComputerRunCommandArguments.model_validate({"command": "dir", "timeout_seconds": 5000}).timeout_seconds == 1200


def test_required_text_and_real_type_errors_are_still_rejected() -> None:
    # 宽容只针对「可以不填的文字」和「只管结果多少的数」：必填的空字符串、类型错了照样报错。
    with pytest.raises(ValidationError):
        SkillSearchArguments.model_validate({"skill": "", "query": "伏笔"})
    with pytest.raises(ValidationError):
        ComputerRunCommandArguments.model_validate({"command": ""})
    with pytest.raises(ValidationError):
        ComputerListFilesArguments.model_validate({"limit": "很多"})
    with pytest.raises(ValidationError):
        ProjectReadFileArguments.model_validate({"relative_path": "", "file_id": ""})


@pytest.mark.skipif(os.name != "nt", reason="cmd 的报错只在 Windows 上出现")
def test_command_not_found_result_says_how_this_computer_takes_commands() -> None:
    # grok-4.6 在 cmd 里连试 60 多次 ls -la /mnt/c/…，每次只拿到「'ls' 不是内部或外部命令」。
    hint = AgentToolExecutor._command_hint(1, "'ls' 不是内部或外部命令，也不是可运行的程序\r\n或批处理文件。\r\n")
    # 9-27 起读和列只有 read_file / list_files 一套（纠偏 3.11），提示里指的是它们。
    assert hint is not None and "dir" in hint and "powershell" in hint and "read_file" in hint
    assert "computer_read_file" not in hint
    assert AgentToolExecutor._command_hint(1, "'ls' is not recognized as an internal or external command") is not None
    assert AgentToolExecutor._command_hint(0, "'ls' 不是内部或外部命令") is None
    assert AgentToolExecutor._command_hint(1, "找不到文件") is None


def test_missing_file_error_lists_what_the_nearest_folder_really_has(tmp_path: Path) -> None:
    # grok-4.6 对着自己编的「第004章_玉佩上的青云_控制卡.md」连读 5 次，每次只拿到「没有找到」。
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path, suffix="-nearby")
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "computer_read_file",
                          {"path": "05_正文内容/第一卷/CH001_控制卡.md"}, call_id="call-read-missing"))
    assert "没有找到这个文件" in str(caught.value)
    assert "CH001_正文.md" in str(caught.value)


def test_nearby_listing_walks_up_to_an_existing_folder_and_hides_secrets(tmp_path: Path) -> None:
    (tmp_path / "书").mkdir()
    (tmp_path / "书" / "001_开篇.md").write_text("x", encoding="utf-8")
    (tmp_path / "书" / "设定").mkdir()
    (tmp_path / "书" / ".env").write_text("KEY=1", encoding="utf-8")
    listing = AgentToolExecutor._nearby_listing(tmp_path / "书" / "打错的夹" / "更深" / "x.md")
    assert "001_开篇.md" in listing and "设定/" in listing
    assert ".env" not in listing


def test_list_query_treats_a_bar_as_or(tmp_path: Path) -> None:
    # 模型常照正则写「青云|chapter|第2章」；以前整串当一个词，什么都筛不出来。
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path, suffix="-bar-query")
    listing = asyncio.run(_call(executor, run, "computer_list_files",
                                {"path": "05_正文内容/第一卷/", "query": "控制卡|正文"}, call_id="call-list-bar"))
    assert [entry["name"] for entry in listing.output["entries"]] == ["CH001_正文.md"]
    both = asyncio.run(_call(executor, run, "computer_list_files",
                             {"path": "05_正文内容/第一卷/", "query": "CH001 控制卡"}, call_id="call-list-and"))
    assert both.output["entries"] == []
