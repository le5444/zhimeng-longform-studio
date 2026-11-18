"""2026-09-26：技能文件夹和本机别的 agent 读同一份，技能目录挂在读技能的工具说明里。

作者原话：「我们的skills是不是有两份，codex一份，我们这个文件夹一份……我发现Claude还有其他agents
都会读取我codex的skills，我们这个也可以的话，是不是就改codex的skills就好了」。
纠偏指令 3.10：所有对话统一给一份简短目录（名字 + 什么时候用），挂在技能工具的说明里，全文按需读。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from test_correction_p0_20260926 import ScriptedProvider, _app, _turn
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_provider import ProviderEvent, ProviderToolDefinition
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_tools import (
    SKILL_FILE_TOOL_NAMES,
    SKILL_READ_FILE_DESCRIPTION,
    AgentToolExecutor,
    SkillListFilesArguments,
    SkillReadFileArguments,
)
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader
from zhimeng_runtime.user_skill_folder_service import (
    UserSkillFolderService,
    default_shared_skill_roots,
)


def _skill(root: Path, folder: str, name: str, description: str) -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n做法。\n",
        encoding="utf-8",
    )
    return skill_dir


def _four_folders(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """本软件 skills/ + 模拟的 ~/.codex/skills、~/.agents/skills、~/.claude/skills。"""
    app_root = tmp_path / "app-skills"
    codex = tmp_path / "home" / ".codex" / "skills"
    agents = tmp_path / "home" / ".agents" / "skills"
    claude = tmp_path / "home" / ".claude" / "skills"
    # 项目这份改过中文名，文件夹名和 Codex 那份一样。
    _skill(app_root, "novel-writing", "小说写法", "写长篇时翻的经验。")
    _skill(codex, "novel-writing", "novel-writing", "旧版小说技能。")
    _skill(codex, "pptx", "pptx", "Use this skill any time a .pptx file is involved.")
    _skill(agents, "pptx", "pptx", "older synced copy")
    _skill(claude, "wechat-search", "wechat-search", "搜索微信公众号文章。")
    # Codex 自带的 .system、同步工具的备份夹：点号开头的不读。
    _skill(codex / ".system", "skill-creator", "skill-creator", "Codex 内置")
    _skill(claude, ".sync-backup", "sync-backup", "备份")
    return app_root, codex, agents, claude


def test_default_shared_folders_are_codex_then_agents_then_claude(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert default_shared_skill_roots(home) == [
        home / ".codex" / "skills",
        home / ".agents" / "skills",
        home / ".claude" / "skills",
    ]


def test_project_copy_wins_by_name_or_folder_and_unused_copies_are_listed(tmp_path: Path) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", extra_roots=[codex, agents, claude])

    entries = {entry.name: entry for entry in service.scan()}

    assert set(entries) == {"小说写法", "pptx", "wechat-search"}
    assert entries["小说写法"].origin == "project"
    assert entries["小说写法"].shadowed == (str(codex / "novel-writing"),)
    assert entries["pptx"].root == str(codex)
    assert entries["pptx"].description.startswith("Use this skill")
    assert entries["pptx"].shadowed == (str(agents / "pptx"),)
    assert entries["wechat-search"].origin == "external"
    assert entries["wechat-search"].root == str(claude)

    view = service.public_view()
    assert [root["path"] for root in view["roots"]] == [str(app_root), str(codex), str(agents), str(claude)]
    assert all(root["exists"] for root in view["roots"])
    listed = {skill["name"]: skill for skill in view["skills"]}
    assert listed["pptx"]["shadowed"] == [str(agents / "pptx")]
    assert listed["pptx"]["root"] == str(codex)


def test_shared_skills_are_read_only_in_settings_and_the_message_says_where(tmp_path: Path) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", extra_roots=[codex, agents, claude])

    found, _content, editable = service.read_instructions_file("pptx")
    ok, message = service.save_instructions("pptx", "---\nname: pptx\ndescription: x\n---\n")

    assert found is True and editable is False
    assert ok is False
    assert str(codex / "pptx") in message
    assert (codex / "pptx" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: pptx\ndescription: Use")


def test_app_reads_the_shared_folders_unless_the_variable_replaces_them(tmp_path: Path, monkeypatch) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(app_root))
    monkeypatch.delenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", raising=False)
    # 9-28 起 app 按「本机各家 agent 的技能文件夹」（KNOWN_SKILL_SOURCES，相对用户主目录）读，Codex、通用、Claude 三家默认开。
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "default").mkdir()
    (tmp_path / "isolated").mkdir()
    with TestClient(_app(tmp_path / "default", ScriptedProvider([]))) as client:
        view = client.get("/api/v1/user-skills").json()
    assert [root["path"] for root in view["roots"]] == [str(app_root), str(codex), str(agents), str(claude)]
    assert {skill["name"] for skill in view["skills"]} == {"小说写法", "pptx", "wechat-search"}

    monkeypatch.setenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", "")
    with TestClient(_app(tmp_path / "isolated", ScriptedProvider([]))) as client:
        view = client.get("/api/v1/user-skills").json()
    assert [root["path"] for root in view["roots"]] == [str(app_root)]
    assert {skill["name"] for skill in view["skills"]} == {"小说写法"}


class CatalogRecordingProvider(ScriptedProvider):
    """记下每一轮收到的完整工具定义（ScriptedProvider 只记名字）。"""

    def __init__(self, script: list[tuple[str, dict[str, Any]] | None]) -> None:
        super().__init__(script)
        self.tool_descriptions: list[dict[str, str]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if tools:
            self.tool_descriptions.append({tool.name: tool.description for tool in tools})
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def test_every_chat_gets_the_catalog_in_the_read_tool_and_no_extra_message(tmp_path: Path, monkeypatch) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(app_root))
    monkeypatch.setenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", ";".join(str(root) for root in (codex, agents, claude)))
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = CatalogRecordingProvider([None, None, None])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()
        first = _turn(client, session["session_id"], "你好", "author_review_change_proposal", "skills-0001")
        second = _turn(client, session["session_id"], "再聊聊", "author_review_change_proposal", "skills-0002")
        read_only = _turn(client, session["session_id"], "只看看", "repair_only", "skills-0003")
    assert first["status"] == second["status"] == read_only["status"] == "succeeded"

    first_description = provider.tool_descriptions[0]["skill_read_file"]
    assert first_description.startswith(SKILL_READ_FILE_DESCRIPTION)
    assert "\n\nSkills on this computer:\n" in first_description
    assert "- pptx: Use this skill any time a .pptx file is involved." in first_description
    assert "- 小说写法: 写长篇时翻的经验。" in first_description
    assert "- wechat-search: 搜索微信公众号文章。" in first_description
    assert "旧版小说技能" not in first_description and "skill-creator" not in first_description
    # 一场对话里一字不差（工具定义这段前缀的缓存能命中）；只读挡位照样有目录。
    assert provider.tool_descriptions[1]["skill_read_file"] == first_description
    assert provider.tool_descriptions[2]["skill_read_file"] == first_description
    # 不另发一段技能菜单。
    for request in provider.requests:
        for message in request:
            assert "技能文件夹里有这些技能" not in str(message.get("content"))
            assert "Skills on this computer" not in str(message.get("content"))


def test_catalog_lists_the_rest_by_name_once_the_budget_is_used(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for index in range(60):
        _skill(root, f"skill-{index:02d}", f"skill-{index:02d}", "很长的用途说明" * 40)
    service = AgentService.__new__(AgentService)
    service.user_skills = UserSkillFolderService(root, tmp_path / "state")
    definition = ProviderToolDefinition(
        name="skill_read_file",
        version="1",
        description=SKILL_READ_FILE_DESCRIPTION,
        parameters={"type": "object", "properties": {}},
    )

    (result,) = asyncio.run(AgentService._with_skill_catalog(service, (definition,)))

    assert len(result.description) <= 4096
    assert "- skill-00: 很长的用途说明" in result.description
    assert result.description.count("…") >= 1
    assert "More skills (skill_list_files shows what they are for): " in result.description
    assert "skill-59" in result.description


def test_catalog_leaves_the_tool_alone_when_there_are_no_skills(tmp_path: Path) -> None:
    service = AgentService.__new__(AgentService)
    service.user_skills = UserSkillFolderService(tmp_path / "empty", tmp_path / "state")
    definition = ProviderToolDefinition(
        name="skill_read_file",
        version="1",
        description=SKILL_READ_FILE_DESCRIPTION,
        parameters={"type": "object", "properties": {}},
    )

    (result,) = asyncio.run(AgentService._with_skill_catalog(service, (definition,)))

    assert result.description == SKILL_READ_FILE_DESCRIPTION



def _executor(tmp_path: Path, service: UserSkillFolderService) -> AgentToolExecutor:
    library = tmp_path / "library"
    library.mkdir(parents=True, exist_ok=True)
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


def test_skill_tools_describe_folders_on_this_computer_without_book_words(tmp_path: Path) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", extra_roots=[codex, agents, claude])
    definitions = {
        definition.name: definition.description
        for definition in _executor(tmp_path, service).definitions
        if definition.name in SKILL_FILE_TOOL_NAMES
    }

    assert set(definitions) == set(SKILL_FILE_TOOL_NAMES)
    for name, description in definitions.items():
        lowered = description.lower()
        for word in ("author", "novel", "网文", "作者", "玄幻"):
            assert word not in lowered, (name, word)
    assert "folder it lives in" in definitions["skill_list_files"]
    assert definitions["skill_read_file"] == SKILL_READ_FILE_DESCRIPTION


def test_reading_a_skill_file_gives_its_full_location(tmp_path: Path) -> None:
    app_root, codex, agents, claude = _four_folders(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", extra_roots=[codex, agents, claude])
    executor = _executor(tmp_path, service)

    output = executor._read_skill_file(SkillReadFileArguments(skill="pptx", path="SKILL.md"))
    listing = asyncio.run(executor._skill_list_files(SkillListFilesArguments(), asyncio.Event())).output

    assert Path(output["file"]) == (codex / "pptx" / "SKILL.md").resolve()
    assert output["content"].startswith("---\nname: pptx")
    folders = {item["skill"]: item["folder"] for item in listing["skills"]}
    assert folders["pptx"] == str(codex / "pptx")
    assert folders["小说写法"] == str(app_root / "novel-writing")
