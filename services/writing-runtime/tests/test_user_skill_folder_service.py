"""技能文件夹服务：扫描上架、启用禁用、多个文件夹同名听谁的、设置页改说明书。"""

from __future__ import annotations

from pathlib import Path

from zhimeng_runtime.user_skill_folder_service import UserSkillFolderService


def _write_skill(root: Path, folder: str, name: str, description: str, body: str = "正文说明") -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _service(tmp_path: Path) -> UserSkillFolderService:
    return UserSkillFolderService(
        tmp_path / "skills",
        tmp_path / "state",
    )


def test_author_can_create_read_and_edit_a_project_skill(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ok, message = service.create_skill("chapter-polish", "润色选中的小说段落")
    assert ok is True, message

    found, content, editable = service.read_instructions_file("chapter-polish")
    assert found is True
    assert editable is True
    assert "name: chapter-polish" in content
    assert "润色选中的小说段落" in content

    changed = content.replace("# 使用方法", "# 润色方法")
    saved, saved_message = service.save_instructions("chapter-polish", changed)
    assert saved is True, saved_message
    assert "# 润色方法" in service.read_instructions_file("chapter-polish")[1]


def test_author_can_use_a_chinese_skill_name(tmp_path: Path) -> None:
    service = _service(tmp_path)

    ok, message = service.create_skill("章节润色", "润色选中的小说段落")

    assert ok is True, message
    found, content, editable = service.read_instructions_file("章节润色")
    assert found is True
    assert editable is True
    assert "name: 章节润色" in content
    assert [entry.name for entry in service.menu()] == ["章节润色"]


def test_scan_discovers_skill_folders_and_skips_bad_ones(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "alpha", "检查章节钩子是否到位")
    _write_skill(root, "beta", "beta", "诊断黄金三章开篇")
    # 坏技能：没有 frontmatter
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "SKILL.md").write_text("没有 frontmatter 的说明", encoding="utf-8")
    # 普通文件不是技能
    (root / "readme.txt").write_text("无关文件", encoding="utf-8")

    entries = _service(tmp_path).scan()

    assert [entry.name for entry in entries] == ["alpha", "beta"]
    assert all(entry.enabled for entry in entries)


def test_scan_returns_empty_when_folder_missing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.scan() == []
    view = service.public_view()
    assert view["exists"] is False
    assert view["total"] == 0


def test_has_scripts_flag_detects_scripts_dir(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _write_skill(root, "gamma", "gamma", "带脚本的技能")
    (skill_dir / "scripts").mkdir()

    entry = _service(tmp_path).scan()[0]

    assert entry.has_scripts is True


def test_disable_then_enable_persists_authors_choice(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "alpha", "检查章节钩子")
    service = _service(tmp_path)

    assert service.set_enabled("alpha", False) is not None
    # 重新构造服务（模拟重启），禁用状态仍在
    reopened = _service(tmp_path)
    assert reopened.scan()[0].enabled is False
    assert reopened.menu() == []

    reopened.set_enabled("alpha", True)
    assert _service(tmp_path).scan()[0].enabled is True


def test_set_enabled_unknown_skill_returns_none(tmp_path: Path) -> None:
    assert _service(tmp_path).set_enabled("nope", False) is None








def test_menu_excludes_disabled_skills(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", "alpha", "技能甲")
    _write_skill(root, "beta", "beta", "技能乙")
    service = _service(tmp_path)
    service.set_enabled("alpha", False)

    assert [entry.name for entry in service.menu()] == ["beta"]


# ----------------------------------------------------------------------
# 多根目录：项目内主根 + 外部只读挂载根（如 ~/.codex/skills 四套）
# ----------------------------------------------------------------------


def test_scan_merges_external_roots_and_project_wins_name_conflicts(tmp_path: Path) -> None:
    project_root = tmp_path / "skills"
    _write_skill(project_root, "local-only", "local-only", "项目内技能")
    _write_skill(project_root, "shared", "shared", "项目内版本")
    external_root = tmp_path / "codex-skills"
    _write_skill(external_root, "external-only", "external-only", "外部挂载技能")
    _write_skill(external_root, "shared", "shared", "外部版本")

    service = UserSkillFolderService(
        project_root,
        tmp_path / "state",
        extra_roots=[external_root],
    )
    entries = {entry.name: entry for entry in service.scan()}

    assert set(entries) == {"local-only", "shared", "external-only"}
    assert entries["shared"].origin == "project"
    assert entries["shared"].description == "项目内版本"
    assert entries["external-only"].origin == "external"


def test_save_instructions_writes_project_skill_and_backs_up(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = _write_skill(root, "alpha", "alpha", "旧用途")
    service = _service(tmp_path)

    new_content = "---\nname: alpha\ndescription: 新用途说明\n---\n\n# 优化后的正文\n"
    ok, message = service.save_instructions("alpha", new_content)

    assert ok, message
    assert "已保存" in message
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == new_content
    assert (skill_dir / "SKILL.md.bak").is_file()
    # 重新扫描后描述已更新
    entry = next(item for item in service.scan() if item.name == "alpha")
    assert entry.description == "新用途说明"


def test_save_instructions_rejects_external_skill_and_bad_content(tmp_path: Path) -> None:
    project_root = tmp_path / "skills"
    _write_skill(project_root, "local", "local", "项目内技能")
    external_root = tmp_path / "codex-skills"
    _write_skill(external_root, "external", "external", "外部技能")
    service = UserSkillFolderService(project_root, tmp_path / "state", extra_roots=[external_root])

    ok, message = service.save_instructions("external", "---\nname: external\ndescription: x\n---\n")
    assert not ok
    assert str(external_root / "external") in message
    assert str(project_root) in message

    ok, message = service.save_instructions("local", "没有 frontmatter 的裸文本")
    assert not ok

    # frontmatter 里的 name 与被改技能不一致也要拒绝
    ok, message = service.save_instructions("local", "---\nname: other\ndescription: x\n---\n")
    assert not ok
    assert "不一致" in message




def test_create_skill_sanitizes_multiline_description(tmp_path: Path) -> None:
    """多行/带冒号的 description 不得打断 frontmatter，更不得注入 name 键。"""
    service = _service(tmp_path)
    ok, message = service.create_skill("evil-desc", "第一行\nname: injected\n第二行：用途")
    assert ok is True, message

    entry = next(item for item in service.scan() if item.name == "evil-desc")
    assert entry.name == "evil-desc"
    assert all(item.name != "injected" for item in service.scan())
    found, _content, _editable = service.read_instructions_file("evil-desc")
    assert found is True


def test_menu_lists_every_enabled_skill_without_a_cap(tmp_path: Path) -> None:
    """以前菜单只列前 24 个；现在目录的长短由 AgentService 按字数预算管，服务这边全给。"""
    service = _service(tmp_path)
    for index in range(30):
        ok, message = service.create_skill(f"filler-{index:02d}", f"填充技能{index}")
        assert ok is True, message

    assert len(service.menu()) == 30
