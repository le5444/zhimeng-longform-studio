"""找不到文件 / 文件夹时，报错里说出同名的在哪（9-27 真机「接着写第 5 章」）。

grok-4.6 照《正文总览》去读「05_正文内容/章纲/第005章_…控制卡.md」，实际在「05_正文内容/第一卷文件夹/章纲/」下面；
它只拿到「没有找到」和上一层的列表，没再找，章纲没读就开写了。
"""

from __future__ import annotations

import asyncio

import pytest

from test_agent_direct_write_tools import _build_runtime, _call


def _control_card(project):
    card = project / "05_正文内容" / "第一卷" / "章纲" / "第005章_控制卡.md"
    card.parent.mkdir(parents=True)
    card.write_text("第 5 章要写的事\n", encoding="utf-8")
    return card


def test_read_file_names_where_the_same_file_really_is(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolError

    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    _control_card(project)

    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "read_file", {"path": "05_正文内容/章纲/第005章_控制卡.md"}))

    message = str(caught.value)
    assert "同名的文件在：05_正文内容/第一卷/章纲/第005章_控制卡.md" in message
    # 上一层有什么照旧列着。
    assert "里有：" in message


def test_list_files_names_where_the_same_folder_really_is(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolError

    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    _control_card(project)

    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "list_files", {"path": "章纲"}))

    assert "同名的文件夹在：05_正文内容/第一卷/章纲" in str(caught.value)


def test_edit_file_on_a_missing_path_names_the_same_file_elsewhere(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolError

    executor, run, _chapter, _database, _change_sets, project = _build_runtime(
        tmp_path, writer_mode="direct_write"
    )
    _control_card(project)

    with pytest.raises(AgentToolError) as caught:
        asyncio.run(
            _call(
                executor,
                run,
                "edit_file",
                {"path": "05_正文内容/章纲/第005章_控制卡.md", "find": "要写", "replace": "得写"},
            )
        )

    assert caught.value.code == "agent_tool_edit_target_missing"
    assert "同名的文件在：05_正文内容/第一卷/章纲/第005章_控制卡.md" in str(caught.value)


def test_no_same_name_file_means_no_extra_words(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolError

    executor, run, *_ = _build_runtime(tmp_path)

    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "read_file", {"path": "05_正文内容/没有的文件.md"}))

    message = str(caught.value)
    assert "同名" not in message
    assert "里有：" in message


def test_dot_folders_are_not_searched(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolExecutor

    project = tmp_path / "书"
    (project / ".zhimeng" / "旧").mkdir(parents=True)
    (project / ".zhimeng" / "旧" / "大纲.md").write_text("机器留下的\n", encoding="utf-8")
    (project / "04_剧情大纲").mkdir()

    hint = AgentToolExecutor._same_name_elsewhere(project / "04_剧情大纲" / "大纲.md", project)

    assert hint == ""


def test_outside_the_open_folder_searches_down_from_the_nearest_folder_that_exists(tmp_path) -> None:
    from zhimeng_runtime.agent_tools import AgentToolExecutor

    elsewhere = tmp_path / "别处"
    (elsewhere / "深" / "更深").mkdir(parents=True)
    real = elsewhere / "深" / "更深" / "说明.txt"
    real.write_text("在这里\n", encoding="utf-8")

    hint = AgentToolExecutor._same_name_elsewhere(elsewhere / "不存在" / "说明.txt", None)

    assert hint == f"同名的文件在：{real}"
