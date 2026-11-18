"""纠偏 3.11 第一步（9-27）：读和列只有 list_files / read_file 一套。

- 打开的文件夹里、快照里有的文件走项目那条读：带 file_id（change_set_propose 要它）、长行按字符接着读；
- 快照里没有的（点号文件夹里的）、文件夹外面的走电脑那条读；
- 旧的四个名字不再发给模型，但照样能执行。
"""

from __future__ import annotations

import asyncio

from test_agent_direct_write_tools import _build_runtime, _call


def test_read_file_inside_the_open_folder_comes_back_with_its_file_id(tmp_path) -> None:
    executor, run, chapter, *_ = _build_runtime(tmp_path)

    result = asyncio.run(_call(executor, run, "read_file", {"path": "05_正文内容/第一卷/CH001_正文.md"}))

    assert result.output["schema"] == "zhimeng.tool-result.project-read-file.v1"
    assert result.output["file_id"].startswith("file-")
    assert result.output["relative_path"] == "05_正文内容/第一卷/CH001_正文.md"
    # 项目那条读给的是原样字节（Windows 换行照留），和以前 project_read_file 一样。
    assert result.output["content"] == chapter.read_bytes().decode("utf-8")


def test_read_file_takes_an_absolute_path_inside_the_folder_too(tmp_path) -> None:
    executor, run, chapter, *_ = _build_runtime(tmp_path)

    result = asyncio.run(_call(executor, run, "read_file", {"path": str(chapter)}))

    assert result.output["schema"] == "zhimeng.tool-result.project-read-file.v1"
    assert result.output["content"] == chapter.read_bytes().decode("utf-8")


def test_read_file_falls_back_to_a_plain_read_for_files_the_snapshot_skips(tmp_path) -> None:
    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    hidden = project / ".notes" / "想法.md"
    hidden.parent.mkdir()
    hidden.write_text("点号文件夹里的笔记\n", encoding="utf-8")

    result = asyncio.run(_call(executor, run, "read_file", {"path": ".notes/想法.md"}))

    assert result.output["schema"] == "zhimeng.tool-result.computer-read-file.v1"
    assert result.output["content"] == "点号文件夹里的笔记\n"


def test_read_file_reads_anywhere_on_the_computer(tmp_path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    outside = tmp_path / "别处" / "说明.txt"
    outside.parent.mkdir()
    outside.write_text("一\n二\n三\n", encoding="utf-8")

    result = asyncio.run(_call(executor, run, "read_file", {"path": str(outside), "start_line": 2, "max_lines": 1}))

    assert result.output["path"] == str(outside.resolve())
    assert result.output["content"] == "二\n"
    assert result.output["next_start_line"] == 3


def test_plain_read_continues_a_very_long_line_by_offset_and_gives_the_tail_past_the_end(tmp_path, monkeypatch) -> None:
    from zhimeng_runtime import agent_tools

    monkeypatch.setattr(agent_tools, "MAX_READ_CHARACTERS", 10)
    executor, run, *_ = _build_runtime(tmp_path)
    outside = tmp_path / "长段落.txt"
    outside.write_text("甲" * 25 + "\n最后一行\n", encoding="utf-8")

    first = asyncio.run(_call(executor, run, "read_file", {"path": str(outside)}, "call-long-0001"))
    assert first.output["content"] == "甲" * 10
    assert first.output["next_offset"] == 10
    second = asyncio.run(
        _call(executor, run, "read_file", {"path": str(outside), "start_offset": first.output["next_offset"]}, "call-long-0002")
    )
    assert second.output["content"] == "甲" * 10

    tail = asyncio.run(_call(executor, run, "read_file", {"path": str(outside), "start_line": 50, "max_lines": 1}, "call-long-0003"))
    assert tail.output["content"] == "最后一行\n"
    assert "只有 2 行" in tail.output["note"]


def test_old_read_tool_names_are_not_offered_but_still_run(tmp_path) -> None:
    executor, run, chapter, *_ = _build_runtime(tmp_path)
    offered = {item.name for item in executor.definitions}
    assert {"list_files", "read_file"} <= offered
    assert offered.isdisjoint({"project_list_files", "project_read_file", "computer_list_files", "computer_read_file"})

    old_project = asyncio.run(
        _call(executor, run, "project_read_file", {"relative_path": "05_正文内容/第一卷/CH001_正文.md"}, "call-old-0001")
    )
    old_computer = asyncio.run(_call(executor, run, "computer_read_file", {"path": str(chapter)}, "call-old-0002"))
    # 项目那条读原样给（含 Windows 换行），电脑那条读统一成 \n：内容一样。
    assert old_project.output["content"].replace("\r\n", "\n") == old_computer.output["content"] == chapter.read_text(encoding="utf-8")
    listed = asyncio.run(_call(executor, run, "list_files", {"path": "05_正文内容/第一卷"}, "call-old-0003"))
    assert [entry["name"] for entry in listed.output["entries"]] == ["CH001_正文.md"]


def test_reading_a_folder_says_it_is_a_folder_and_which_tool_to_use(tmp_path) -> None:
    """9-27 真机：grok-4.6 连着三次把文件夹当文件读；回话直接说是文件夹、用 list_files。"""
    import pytest

    from zhimeng_runtime.agent_tools import AgentToolError

    executor, run, *_ = _build_runtime(tmp_path)
    folder = tmp_path / "别处的文件夹"
    folder.mkdir()
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "read_file", {"path": str(folder)}))
    assert "这是一个文件夹" in str(caught.value) and "list_files" in str(caught.value)


def test_read_and_list_accept_relative_paths_counted_from_the_home_folder(tmp_path, monkeypatch) -> None:
    """9-27 真机：打开了书，grok-4.6 照环境里的「主目录」写从主目录算的相对路径（Desktop/…），按打开的文件夹全落空。
    读和列：打开的文件夹里没有、主目录下有，就用主目录下的。"""
    from zhimeng_runtime import environment_context

    home = tmp_path / "home"
    (home / "Desktop" / "资料").mkdir(parents=True)
    (home / "Desktop" / "资料" / "a.md").write_text("主目录下的\n", encoding="utf-8")
    monkeypatch.setattr(environment_context, "home_folder", lambda: home)
    executor, run, *_ = _build_runtime(tmp_path)

    listed = asyncio.run(_call(executor, run, "list_files", {"path": "Desktop/资料"}, "call-home-0001"))
    assert [entry["name"] for entry in listed.output["entries"]] == ["a.md"]
    read = asyncio.run(_call(executor, run, "read_file", {"path": "Desktop/资料/a.md"}, "call-home-0002"))
    assert read.output["content"] == "主目录下的\n"
    # 打开的文件夹里有的，照旧先认打开的文件夹。
    inside = asyncio.run(_call(executor, run, "list_files", {"path": "05_正文内容"}, "call-home-0003"))
    assert [entry["name"] for entry in inside.output["entries"]] == ["第一卷"]
