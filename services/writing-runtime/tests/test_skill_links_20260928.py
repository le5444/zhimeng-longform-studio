"""技能文件夹本身是链接也照读（9-28）。

CC Switch、`npx skills` 默认用链接把一份技能同步进各家的技能文件夹（CC Switch 在 Windows 上用
symlink_dir，建不了才退回复制）。以前织梦扫描跳过符号链接；联接点（junction）不跳，
可读技能文件的工具又拒绝链接形式的技能文件夹——列出来却读不了。
技能里面的链接照旧不跟（test_agent_skill_file_tools 的 test_read_refuses_links_inside_the_skill_folder）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_tools import AgentToolError, AgentToolExecutor
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader
from zhimeng_runtime.user_skill_folder_service import KNOWN_SKILL_SOURCES, UserSkillFolderService


def _link_dir(kind: str, target: Path, link: Path) -> None:
    if kind == "symlink":
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlinks are not available here: {exc}")
        return
    if os.name != "nt":
        pytest.skip("junctions only exist on Windows")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _skill(folder: Path, name: str, description: str) -> Path:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n先读 references/写法.md。\n",
        encoding="utf-8",
    )
    (folder / "references").mkdir()
    (folder / "references" / "写法.md").write_text("# 写法\n\n开篇先外化压力，钩子前置。\n", encoding="utf-8")
    return folder


def _linked_setup(tmp_path: Path, kind: str) -> tuple[Path, Path, Path]:
    """CC Switch 的样子：总库里一份真技能，Claude 的技能文件夹里是指向它的链接。"""
    home = tmp_path / "home"
    real = _skill(home / ".cc-switch" / "skills" / "hook-writing", "hook-writing", "写开篇钩子的经验")
    claude_skills = home / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    _link_dir(kind, real, claude_skills / "hook-writing")
    app_root = tmp_path / "app-skills"
    app_root.mkdir()
    return home, app_root, real


def _executor(tmp_path: Path, service: UserSkillFolderService) -> AgentToolExecutor:
    library = tmp_path / "library"
    library.mkdir(parents=True)
    reader = ProjectReader((library,))
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    return AgentToolExecutor(
        reader,
        database,
        ProjectLockRegistry(),
        ChangeSetService(reader, database, tmp_path / "change-sets"),
        user_skills=service,
    )


def _run(executor: AgentToolExecutor, tool_name: str, arguments: dict) -> dict:
    request_hash = executor.request_hash(tool_name, "1", arguments)
    execution = asyncio.run(
        executor.execute(
            SimpleNamespace(run_id="run-skill-links"),
            tool_call_id="tool-call-skill-links-0001",
            tool_name=tool_name,
            arguments=arguments,
            request_hash=request_hash,
            cancel_event=asyncio.Event(),
        )
    )
    return execution.output


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_linked_skill_folder_is_listed_and_readable(tmp_path: Path, kind: str) -> None:
    home, app_root, _real = _linked_setup(tmp_path, kind)
    service = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)

    entries = {entry.name: entry for entry in service.scan()}
    assert "hook-writing" in entries
    assert entries["hook-writing"].source == "claude"
    claude_row = next(row for row in service.sources_view() if row["key"] == "claude")
    assert claude_row["count"] == 1

    executor = _executor(tmp_path, service)
    read = _run(executor, "skill_read_file", {"skill": "hook-writing", "path": "SKILL.md"})
    assert "先读 references/写法.md" in read["content"]
    card = _run(executor, "skill_read_file", {"skill": "hook-writing", "path": "references/写法.md"})
    assert "钩子前置" in card["content"]
    listed = _run(executor, "skill_list_files", {"skill": "hook-writing"})
    assert [item["path"] for item in listed["directories"]] == ["references"]
    searched = _run(executor, "skill_search", {"skill": "hook-writing", "query": "开篇 钩子"})
    assert "references/写法.md" in [hit["path"] for hit in searched["hits"]]


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_links_inside_a_linked_skill_folder_are_still_not_followed(tmp_path: Path, kind: str) -> None:
    home, app_root, real = _linked_setup(tmp_path, kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "泄露.md").write_text("外面的内容\n", encoding="utf-8")
    _link_dir(kind, outside, real / "链接")
    service = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)
    executor = _executor(tmp_path, service)

    with pytest.raises(AgentToolError) as info:
        _run(executor, "skill_read_file", {"skill": "hook-writing", "path": "链接/泄露.md"})
    assert info.value.code == "agent_tool_skill_path_invalid"
    searched = _run(executor, "skill_search", {"skill": "hook-writing", "query": "外面的内容"})
    assert searched["hit_count"] == 0


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_copy_to_library_copies_the_real_files_and_leaves_inner_links_out(tmp_path: Path, kind: str) -> None:
    home, app_root, real = _linked_setup(tmp_path, kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "大文件夹.md").write_text("不该被复制进来\n", encoding="utf-8")
    _link_dir(kind, outside, real / "链接")
    service = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)

    ok, message, entry = service.copy_to_library("hook-writing")

    assert ok, message
    assert entry is not None and entry.origin == "project"
    copied = app_root / "hook-writing"
    assert not copied.is_symlink()
    assert (copied / "references" / "写法.md").read_text(encoding="utf-8").startswith("# 写法")
    assert not (copied / "链接").exists()
    # 原来那份和链接都还在。
    assert (real / "SKILL.md").is_file()
    assert (home / ".claude" / "skills" / "hook-writing" / "SKILL.md").is_file()
