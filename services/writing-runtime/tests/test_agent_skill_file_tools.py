"""作者技能文件夹（网文知识库等）的只读翻阅工具：列目录 / 读文件 / 全文检索。

这些工具让 Agent 按需翻技能文件夹里的资料卡，而不是把整库塞进提示词；
边界：只读、只认已启用的技能、路径不能逃出技能根、不读链接与脚本。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_tools import (
    SKILL_FILE_TOOL_NAMES,
    AgentToolError,
    AgentToolExecutor,
)
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader
from zhimeng_runtime.user_skill_folder_service import UserSkillFolderService


def _skill(root: Path, name: str, description: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n怎么用。\n",
        encoding="utf-8",
    )
    return folder


def _card(folder: Path, relative: str, title: str, body: str) -> Path:
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n类型: 测试\n---\n\n# {title}\n\n{body}\n", encoding="utf-8")
    return path


def _build(tmp_path: Path, *, with_skills: bool = True):
    library = tmp_path / "library"
    library.mkdir(parents=True)
    reader = ProjectReader((library,))
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    service = (
        UserSkillFolderService(skills_root, tmp_path / "state") if with_skills else None
    )
    executor = AgentToolExecutor(
        reader,
        database,
        ProjectLockRegistry(),
        ChangeSetService(reader, database, tmp_path / "change-sets"),
        user_skills=service,
    )
    return executor, skills_root, service


def _run(executor: AgentToolExecutor, tool_name: str, arguments: dict) -> dict:
    request_hash = executor.request_hash(tool_name, "1", arguments)
    execution = asyncio.run(
        executor.execute(
            SimpleNamespace(run_id="run-skill-tools"),
            tool_call_id="tool-call-skill-0001",
            tool_name=tool_name,
            arguments=arguments,
            request_hash=request_hash,
            cancel_event=asyncio.Event(),
        )
    )
    return execution.output


def _error(executor: AgentToolExecutor, tool_name: str, arguments: dict) -> AgentToolError:
    with pytest.raises(AgentToolError) as info:
        _run(executor, tool_name, arguments)
    return info.value


def test_catalog_lists_skill_file_tools_only_with_a_skill_folder_service(tmp_path: Path) -> None:
    with_tools, _, _ = _build(tmp_path / "a")
    without_tools, _, _ = _build(tmp_path / "b", with_skills=False)

    names = {item.name for item in with_tools.definitions}
    assert SKILL_FILE_TOOL_NAMES <= names
    assert names - {item.name for item in without_tools.definitions} == SKILL_FILE_TOOL_NAMES
    assert SKILL_FILE_TOOL_NAMES.isdisjoint(item.name for item in without_tools.definitions)
    error = _error(without_tools, "skill_search", {"skill": "x", "query": "y"})
    assert error.code == "agent_tool_not_allowed"


def test_listing_without_skill_shows_enabled_skills_only(tmp_path: Path) -> None:
    executor, root, service = _build(tmp_path)
    _skill(root, "网文知识库", "网文机制卡库")
    _skill(root, "旧技能", "已经不用了")
    assert service is not None
    service.set_enabled("旧技能", False)

    output = _run(executor, "skill_list_files", {})

    assert output["schema"] == "zhimeng.tool-result.skill-list-files.v1"
    assert [item["skill"] for item in output["skills"]] == ["网文知识库"]
    assert output["skills"][0]["description"] == "网文机制卡库"
    assert output["total_skills"] == 1
    assert _error(executor, "skill_search", {"skill": "旧技能", "query": "机制"}).code == (
        "agent_tool_skill_not_found"
    )


def test_listing_folder_shows_subfolders_files_and_path_query(tmp_path: Path) -> None:
    executor, root, _ = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    _card(kb, "类型规律/玄幻/玄幻读者要什么.md", "玄幻读者要什么", "升级 爽点")
    _card(kb, "类型规律/悬疑/悬疑读者要什么.md", "悬疑读者要什么", "反转")
    _card(kb, "场景写法/玄幻/开篇钩子.md", "开篇钩子", "钩子前置")
    (kb / "README.md").write_text("# 索引\n", encoding="utf-8")
    (kb / "scripts").mkdir()
    (kb / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (kb / ".git").mkdir()
    (kb / ".git" / "config").write_text("x\n", encoding="utf-8")

    top = _run(executor, "skill_list_files", {"skill": "网文知识库"})
    assert top["path"] == ""
    # 按名字排序：ASCII 在前，随后按字码；.git 被跳过；scripts 里的脚本也是可读文本（2026-09-24 起）
    assert top["directories"] == [
        {"path": "scripts", "files": 1},
        {"path": "场景写法", "files": 1},
        {"path": "类型规律", "files": 2},
    ]
    assert {item["path"] for item in top["files"]} == {"README.md", "SKILL.md"}
    assert top["truncated"] is False

    nested = _run(executor, "skill_list_files", {"skill": "网文知识库", "path": "类型规律/玄幻"})
    assert nested["path"] == "类型规律/玄幻"
    assert nested["directories"] == []
    assert [item["path"] for item in nested["files"]] == ["类型规律/玄幻/玄幻读者要什么.md"]

    found = _run(executor, "skill_list_files", {"skill": "网文知识库", "query": "玄幻 读者"})
    assert [item["path"] for item in found["files"]] == ["类型规律/玄幻/玄幻读者要什么.md"]
    assert found["total_files"] == 1

    with_limit = _run(executor, "skill_list_files", {"skill": "网文知识库", "limit": 1})
    assert len(with_limit["directories"]) == 1 and with_limit["files"] == []
    assert with_limit["truncated"] is True


def test_read_file_is_line_bounded_and_continues(tmp_path: Path) -> None:
    executor, root, _ = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    (kb / "卡.md").write_text("".join(f"第{n}行\n" for n in range(1, 11)), encoding="utf-8")

    first = _run(
        executor,
        "skill_read_file",
        {"skill": "网文知识库", "path": "卡.md", "start_line": 3, "max_lines": 4},
    )
    assert first["schema"] == "zhimeng.tool-result.skill-read-file.v1"
    assert first["content"] == "第3行\n第4行\n第5行\n第6行\n"
    assert (first["start_line"], first["end_line"], first["total_lines"]) == (3, 6, 10)
    assert first["truncated"] is True and first["next_start_line"] == 7

    rest = _run(
        executor,
        "skill_read_file",
        {"skill": "网文知识库", "path": "卡.md", "start_line": 7, "max_lines": 400},
    )
    assert rest["content"] == "第7行\n第8行\n第9行\n第10行\n"
    assert rest["truncated"] is False and rest["next_start_line"] is None

    windows_style = _run(executor, "skill_read_file", {"skill": "网文知识库", "path": ".\\卡.md"})
    assert windows_style["path"] == "卡.md"


def test_skill_long_lines_are_fully_recoverable_without_skipping_content(tmp_path, monkeypatch):
    from zhimeng_runtime import agent_tools

    monkeypatch.setattr(agent_tools, "MAX_READ_CHARACTERS", 17)
    executor, root, _ = _build(tmp_path)
    folder = _skill(root, "data-processing", "Process ordinary data files")
    original = '{"description":"' + "甲乙丙" * 19 + '"}\nsecond row\nthird row'
    (folder / "sample.jsonl").write_bytes(original.encode("utf-8"))
    offset, parts = 0, []
    while True:
        result = _run(executor, "skill_read_file", {
            "skill": "data-processing", "path": "sample.jsonl", "start_offset": offset,
        })
        parts.append(result["content"])
        if not offset:
            assert result["end_line"] == 1
            assert result["next_start_line"] is None
        if not result["truncated"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert "".join(parts) == original


def test_read_refuses_paths_outside_scripts_hidden_and_disabled(tmp_path: Path) -> None:
    executor, root, service = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    (kb / "scripts").mkdir()
    (kb / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (kb / ".secret.md").write_text("x\n", encoding="utf-8")
    (kb / "cards.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    other = _skill(root, "另一个技能", "别的用途")
    (other / "私有.md").write_text("不该从这边读到\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("外面的文件\n", encoding="utf-8")

    def code(path: str, skill: str = "网文知识库") -> str:
        return _error(executor, "skill_read_file", {"skill": skill, "path": path}).code

    assert code("../另一个技能/私有.md") == "agent_tool_skill_path_invalid"
    assert code("../../outside.md") == "agent_tool_skill_path_invalid"
    assert code(str(outside)) == "agent_tool_skill_path_invalid"
    assert code("/etc/passwd") == "agent_tool_skill_path_invalid"
    assert code(".secret.md") == "agent_tool_skill_path_invalid"
    # 技能夹里任何 UTF-8 文本都能读：脚本与 .jsonl 一样（以前只放行六种后缀）。
    assert _run(executor, "skill_read_file", {"skill": "网文知识库", "path": "scripts/tool.py"})["content"] == "print(1)\n"
    assert _run(executor, "skill_read_file", {"skill": "网文知识库", "path": "cards.jsonl"})["content"] == '{"a":1}\n'
    assert code("不存在.md") == "agent_tool_skill_path_not_found"
    assert code("scripts") == "agent_tool_skill_path_not_found"
    assert code("SKILL.md", skill="没有这个技能") == "agent_tool_skill_not_found"
    assert service is not None
    service.set_enabled("网文知识库", False)
    assert code("SKILL.md") == "agent_tool_skill_not_found"


def test_read_refuses_links_inside_the_skill_folder(tmp_path: Path) -> None:
    executor, root, _ = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "泄露.md").write_text("外面的内容\n", encoding="utf-8")
    link = kb / "链接"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        else:
            os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
        pytest.skip(f"cannot create a directory link here: {exc}")

    error = _error(executor, "skill_read_file", {"skill": "网文知识库", "path": "链接/泄露.md"})
    assert error.code == "agent_tool_skill_path_invalid"
    listed = _run(executor, "skill_list_files", {"skill": "网文知识库"})
    assert "链接" not in {item["path"] for item in listed["directories"]}
    searched = _run(executor, "skill_search", {"skill": "网文知识库", "query": "外面的内容"})
    assert searched["hit_count"] == 0


def test_search_ranks_the_card_with_all_terms_first_and_gives_a_snippet(tmp_path: Path) -> None:
    executor, root, _ = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    _card(kb, "场景写法/玄幻/开篇钩子.md", "玄幻开篇钩子", "开篇先外化压力，钩子前置，再给可验证的小回报。")
    _card(kb, "类型规律/悬疑/悬疑读者.md", "悬疑读者要什么", "反转与线索拼图，开篇抛出异常。")
    _card(kb, "包装标题/书名.md", "书名承诺", "书名要把类型承诺写在前面。")

    output = _run(executor, "skill_search", {"skill": "网文知识库", "query": "玄幻 开篇 钩子"})

    assert output["schema"] == "zhimeng.tool-result.skill-search.v1"
    assert output["terms"] == ["开篇", "玄幻", "钩子"]
    assert output["scanned_files"] == 4  # SKILL.md + 3 张卡
    assert [hit["path"] for hit in output["hits"]] == [
        "场景写法/玄幻/开篇钩子.md",
        "类型规律/悬疑/悬疑读者.md",
    ]
    top = output["hits"][0]
    assert top["title"] == "玄幻开篇钩子"
    assert top["matched_terms"] == ["开篇", "玄幻", "钩子"]
    # 摘要取命中词最多的正文行，而不是标题行（标题已单独给出）
    assert "钩子前置" in top["snippet"]
    assert output["hits"][1]["matched_terms"] == ["开篇"]
    assert output["truncated"] is False and output["budget_exhausted"] is False

    card = _run(executor, "skill_read_file", {"skill": "网文知识库", "path": top["path"]})
    assert "钩子前置" in card["content"]


def test_search_skips_data_dumps_and_oversized_files_and_can_narrow_by_path(tmp_path: Path) -> None:
    executor, root, _ = _build(tmp_path)
    kb = _skill(root, "网文知识库", "网文机制卡库")
    _card(kb, "类型规律/玄幻/升级.md", "玄幻升级", "升级节奏要有债务。")
    _card(kb, "作品机制/玄幻/某书.md", "某书机制", "玄幻升级流的开篇。")
    (kb / "cards.jsonl").write_text('{"text": "玄幻升级"}\n', encoding="utf-8")
    (kb / "巨大.md").write_text("玄幻升级" + "x" * (2 * 1024 * 1024), encoding="utf-8")

    everywhere = _run(executor, "skill_search", {"skill": "网文知识库", "query": "玄幻升级"})
    assert {hit["path"] for hit in everywhere["hits"]} == {
        "类型规律/玄幻/升级.md",
        "作品机制/玄幻/某书.md",
    }

    narrowed = _run(
        executor,
        "skill_search",
        {"skill": "网文知识库", "query": "玄幻升级", "path": "类型规律"},
    )
    assert [hit["path"] for hit in narrowed["hits"]] == ["类型规律/玄幻/升级.md"]
    assert narrowed["path"] == "类型规律"

    limited = _run(executor, "skill_search", {"skill": "网文知识库", "query": "玄幻升级", "limit": 1})
    assert len(limited["hits"]) == 1 and limited["truncated"] is True and limited["hit_count"] == 2

    assert _error(executor, "skill_search", {"skill": "网文知识库", "query": "！！！"}).code == (
        "agent_tool_validation_failed"
    )
    assert _error(executor, "skill_search", {"skill": "网文知识库", "query": "玄幻", "path": "没有"}).code == (
        "agent_tool_skill_path_not_found"
    )


def test_skill_read_gives_a_whole_skill_md_by_default(tmp_path: Path) -> None:
    """9-27（Hermes 档案 5.8）：不传 max_lines 时 SKILL.md 一次给全——《小说蒸馏》252 行，以前默认 200 行读不全。"""
    from zhimeng_runtime.agent_tools import MAX_READ_LINES, SkillReadFileArguments

    assert SkillReadFileArguments(skill="小说创作", path="SKILL.md").max_lines == MAX_READ_LINES
    assert MAX_READ_LINES >= 300
