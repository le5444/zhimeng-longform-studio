"""纠偏指令 P1（2026-09-26）：3.6 工具说明不写小说 / 作者，3.8 小说专用的 ai_smell_score 随技能出现。

3.8 的做法：内核不默认给 ai_smell_score；某个启用的技能在 SKILL.md 的 allowed-tools（Agent Skills 规范字段）
里写了它，而且这场对话里读过那份 SKILL.md，下一步才给。随便聊「37×24」看不到它。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from test_correction_p0_20260926 import ScriptedProvider, _app, _turn
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_tools import SKILL_GATED_TOOL_NAMES, AgentToolExecutor
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader
from zhimeng_runtime.user_skill_folder_service import UserSkillFolderService


def _skill(root: Path, folder: str, frontmatter: str) -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# {folder}\n\n做法。\n", encoding="utf-8")
    return skill_dir


def test_allowed_tools_are_read_from_the_skill_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _skill(root, "spaces", "name: spaces\ndescription: 规范写法\nallowed-tools: ai_smell_score Read")
    _skill(root, "commas", 'name: commas\ndescription: 逗号\nallowed-tools: "ai_smell_score, Bash"')
    _skill(root, "listed", "name: listed\ndescription: 列表\nallowed-tools:\n  - ai_smell_score")
    _skill(root, "plain", "name: plain\ndescription: 没写")
    entries = {entry.name: entry for entry in UserSkillFolderService(root, tmp_path / "state").scan()}

    assert entries["spaces"].tools == ("ai_smell_score", "Read")
    assert entries["commas"].tools == ("ai_smell_score", "Bash")
    assert entries["listed"].tools == ("ai_smell_score",)
    assert entries["plain"].tools == ()


def _chat(tmp_path: Path, monkeypatch, script, *, with_declaring_skill: bool = True) -> ScriptedProvider:
    skills = tmp_path / "skills"
    if with_declaring_skill:
        _skill(skills, "novel-writing", "name: 小说写法\ndescription: 写长篇时翻\nallowed-tools: ai_smell_score")
    _skill(skills, "notes", "name: 记笔记\ndescription: 整理笔记时翻")
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", "")
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    return ScriptedProvider(script)


def test_ai_smell_score_comes_with_a_skill_that_declares_it(tmp_path: Path, monkeypatch) -> None:
    """内核不给 ai_smell_score：技能声明了才有，停用那个技能就没了。"""

    provider = _chat(tmp_path, monkeypatch, [None, None, None, None])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()
        runs = [
            _turn(client, session["session_id"], text, mode, f"p1-gate-{index:04d}")
            for index, (text, mode) in enumerate(
                [("37×24 等于多少", "author_review_change_proposal"), ("我要写小说了", "author_review_change_proposal"), ("只看看", "repair_only")]
            )
        ]
        assert all(run["status"] == "succeeded" for run in runs), runs
        assert client.post("/api/v1/user-skills/小说写法/disable").status_code == 200
        after = _turn(client, session["session_id"], "还在吗", "author_review_change_proposal", "p1-gate-0005")
        assert after["status"] == "succeeded", after

    # 声明了它的技能启用着，每一轮都给（只读挡也给：它不写文件）——不用先读那份 SKILL.md
    # （9-26 真机：读了才给的话 grok-4.6 不去读，自己编分数）。
    assert len(provider.catalogs) == 4
    assert all("ai_smell_score" in catalog for catalog in provider.catalogs[:-1])
    # 停用以后就不给了。
    assert "ai_smell_score" not in provider.catalogs[-1]
    assert all(catalog == sorted(catalog) for catalog in provider.catalogs)


def test_no_skill_declares_it_so_the_tool_is_not_offered(tmp_path: Path, monkeypatch) -> None:
    provider = _chat(
        tmp_path,
        monkeypatch,
        [("skill_read_file", {"skill": "记笔记", "path": "SKILL.md"}), None],
        with_declaring_skill=False,
    )
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()
        first = _turn(client, session["session_id"], "整理一下笔记", "author_review_change_proposal", "p1-gate-0101")
        second = _turn(client, session["session_id"], "好的", "author_review_change_proposal", "p1-gate-0102")
    assert first["status"] == second["status"] == "succeeded"
    assert all("ai_smell_score" not in catalog for catalog in provider.catalogs)


def test_default_tool_descriptions_do_not_talk_about_books_or_authors(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    reader = ProjectReader((library,))
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    executor = AgentToolExecutor(
        reader,
        database,
        ProjectLockRegistry(),
        ChangeSetService(reader, database, tmp_path / "change-sets"),
        search_service=SimpleNamespace(),
        user_skills=UserSkillFolderService(tmp_path / "skills", tmp_path / "state"),
    )
    # memory_write 等 3.7（记忆改名）一起改；随技能出现的工具只在读过小说技能以后才看得见。
    checked = {
        definition.name: definition.description
        for definition in executor.definitions
        if definition.name not in SKILL_GATED_TOOL_NAMES | {"memory_write"}
    }

    assert {"list_files", "read_file", "project_search", "plan_update", "change_set_propose"} <= set(checked)
    for name, description in checked.items():
        lowered = description.lower()
        for word in ("novel", "author", "book", "chapter", "作者", "小说", "网文", "书"):
            assert word not in lowered, (name, word)
