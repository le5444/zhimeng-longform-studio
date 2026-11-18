"""技能库（9-28）：本机各家 agent 的技能文件夹一家一个开关，外面的技能能一键复制进织梦。

作者原话：「我的codex的四套skills貌似其他ai agent也可以用，那可以有个选项，同步本机其他agents skills……
这个应用有一个技能库，还可以其他的智能库，可以一键探索使用其他的……你看看cc swich怎么搞的」。
CC Switch 是一份总库往各家里链接 / 复制；织梦反过来，原地读各家的文件夹（不往别人的文件夹里写），
Codex、通用 .agents、Claude 三家照旧默认开，别的默认关、点一下就用上；想在织梦里改，就复制一份进自己的 skills/。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_correction_p0_20260926 import ScriptedProvider, _app
from zhimeng_runtime.user_skill_folder_service import KNOWN_SKILL_SOURCES, UserSkillFolderService


def _skill(root: Path, folder: str, name: str, description: str) -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n做法。\n", encoding="utf-8"
    )
    return skill_dir


def _machine(tmp_path: Path) -> tuple[Path, Path]:
    """织梦自己的 skills/ + 一台装了 Codex、WorkBuddy、CC Switch 的电脑。"""
    app_root = tmp_path / "app-skills"
    home = tmp_path / "home"
    _skill(app_root, "novel-writing", "小说写法", "写长篇时翻的经验。")
    _skill(home / ".codex" / "skills", "novel-writing", "novel-writing", "Codex 那份原版。")
    _skill(home / ".codex" / "skills", "wechat-search", "wechat-search", "搜索微信公众号文章。")
    buddy = _skill(home / ".workbuddy" / "skills", "baize-novel", "baize-novel", "白泽小说系统。")
    (buddy / "scripts").mkdir()
    (buddy / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (buddy / "__pycache__").mkdir()
    (buddy / "__pycache__" / "x.pyc").write_bytes(b"\0")
    _skill(home / ".cc-switch" / "skills", "huashu-nuwa", "huashu-nuwa", "女娲造人。")
    return app_root, home


def test_every_agent_folder_on_the_machine_gets_a_switch(tmp_path: Path) -> None:
    app_root, home = _machine(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)

    rows = {row["key"]: row for row in service.sources_view()}
    # 这台电脑上有的照实列出几个技能；三家默认开，别的默认关。
    assert rows["codex"]["exists"] and rows["codex"]["count"] == 2 and rows["codex"]["enabled"]
    assert rows["workbuddy"]["exists"] and rows["workbuddy"]["count"] == 1 and not rows["workbuddy"]["enabled"]
    assert rows["ccswitch"]["count"] == 1 and rows["ccswitch"]["label"].startswith("CC Switch")
    assert rows["zcode"]["exists"] is False and rows["zcode"]["count"] == 0
    assert {skill.name for skill in service.scan()} == {"小说写法", "wechat-search"}

    # 点一下就用上；新开一个服务照样记得（开关存在状态文件里），停用的技能也还在。
    assert service.set_enabled("wechat-search", False) is not None
    assert service.set_source_enabled("workbuddy", True) is True
    again = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)
    listed = {skill.name: skill for skill in again.scan()}
    assert set(listed) == {"小说写法", "wechat-search", "baize-novel"}
    assert listed["baize-novel"].source == "workbuddy" and listed["baize-novel"].source_label == "WorkBuddy"
    assert listed["wechat-search"].enabled is False
    # 同名的照旧以织梦自己的为准，Codex 那份记在没用上的里面。
    assert listed["小说写法"].shadowed == (str(home / ".codex" / "skills" / "novel-writing"),)
    assert again.set_source_enabled("codex", False) is True
    assert {skill.name for skill in again.scan()} == {"小说写法", "baize-novel"}
    assert again.set_source_enabled("no-such-agent", True) is False


def test_source_switch_recreates_a_removed_state_directory(tmp_path: Path) -> None:
    app_root, home = _machine(tmp_path)
    state_root = tmp_path / "state"
    service = UserSkillFolderService(app_root, state_root, sources=KNOWN_SKILL_SOURCES, home=home)
    state_root.rmdir()
    assert service.set_source_enabled("workbuddy", True)
    assert (state_root / "user-skills.json").is_file()
    reopened = UserSkillFolderService(app_root, state_root, sources=KNOWN_SKILL_SOURCES, home=home)
    assert "baize-novel" in {skill.name for skill in reopened.scan()}


def test_copying_into_the_library_makes_it_editable_and_leaves_the_original(tmp_path: Path) -> None:
    app_root, home = _machine(tmp_path)
    service = UserSkillFolderService(app_root, tmp_path / "state", sources=KNOWN_SKILL_SOURCES, home=home)
    service.set_source_enabled("workbuddy", True)
    original = home / ".workbuddy" / "skills" / "baize-novel"

    ok, message, copied = service.copy_to_library("baize-novel")

    assert ok, message
    assert copied is not None and copied.origin == "project" and copied.folder == str(app_root / "baize-novel")
    assert (app_root / "baize-novel" / "scripts" / "run.py").is_file()
    assert not (app_root / "baize-novel" / "__pycache__").exists()
    # 复制进来的这份以后在织梦里能改；别家那份一个字没动，记在没用上的里面。
    saved, _ = service.save_instructions("baize-novel", "---\nname: baize-novel\ndescription: 改过的用途。\n---\n")
    assert saved
    assert (original / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: baize-novel\ndescription: 白泽")
    entry = next(skill for skill in service.scan() if skill.name == "baize-novel")
    assert entry.description == "改过的用途。" and entry.shadowed == (str(original),)
    # 已经在织梦里的不再复制；同名文件夹已经有了不覆盖。
    assert service.copy_to_library("baize-novel")[0] is False
    assert service.copy_to_library("小说写法")[0] is False
    ok, message, _ = service.copy_to_library("novel-writing")
    assert ok is False
    assert not list(app_root.glob(".*.copying"))


def test_the_settings_api_lists_sources_switches_them_and_copies(tmp_path: Path, monkeypatch) -> None:
    app_root, home = _machine(tmp_path)
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(app_root))
    monkeypatch.delenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    (tmp_path / "runtime").mkdir()
    with TestClient(_app(tmp_path / "runtime", ScriptedProvider([]))) as client:
        view = client.get("/api/v1/user-skills").json()
        sources = {row["key"]: row for row in view["sources"]}
        assert sources["codex"]["enabled"] and not sources["workbuddy"]["enabled"]
        assert sources["workbuddy"]["count"] == 1
        assert {skill["name"] for skill in view["skills"]} == {"小说写法", "wechat-search"}

        turned_on = client.post("/api/v1/user-skills/sources/workbuddy/enable")
        assert turned_on.status_code == 200, turned_on.text
        skills = {skill["name"]: skill for skill in turned_on.json()["skills"]}
        assert skills["baize-novel"]["source_label"] == "WorkBuddy" and skills["baize-novel"]["origin"] == "external"
        assert client.post("/api/v1/user-skills/sources/nobody/enable").status_code == 404

        copied = client.post("/api/v1/user-skills/baize-novel/copy-to-library")
        assert copied.status_code == 200, copied.text
        after = {skill["name"]: skill for skill in copied.json()["skills"]["skills"]}
        assert after["baize-novel"]["origin"] == "project" and after["baize-novel"]["source"] == "zhimeng"
        assert client.post("/api/v1/user-skills/baize-novel/copy-to-library").status_code == 409
        assert client.post("/api/v1/user-skills/nothing-here/copy-to-library").status_code == 404

        turned_off = client.post("/api/v1/user-skills/sources/codex/disable")
        assert "wechat-search" not in {skill["name"] for skill in turned_off.json()["skills"]}
