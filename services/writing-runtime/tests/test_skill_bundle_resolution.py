"""随包 Skills 路径解析的回归测试。

这个模块存在的理由是一次真实的名实不符：状态面板报「换电脑不依赖本机
.codex」，而实际跑写作的 ai_smell / knowledge_context 两个服务各自写了一套
查找逻辑，仍然只认 `~/.codex`。测试锁住三件事：

1. 随包优先于本机 profile；
2. 空骨架目录不算配套齐全（打包脚本只建目录不拷内容）；
3. 脚本来源与知识库来源可以来自不同的根（随包脚本 + 本机知识库）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhimeng_runtime import skill_bundle
from zhimeng_runtime.ai_smell_service import AiSmellService, resolve_library
from zhimeng_runtime.knowledge_context_service import KnowledgeContextService


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        skill_bundle.BUNDLE_ROOT_ENV,
        "ZHIMENG_AI_SMELL_SCRIPT",
        "ZHIMENG_AI_SMELL_LIBRARY",
        "ZHIMENG_KB_MANAGER_SCRIPT",
        "ZHIMENG_KB_CONSUMER_SCRIPT",
    ):
        monkeypatch.delenv(name, raising=False)
    # 仓库里带着真实的 staged bundle，会盖住 tmp_path 里的夹具；把仓库根指到
    # 一个空目录，让每个用例只看自己摆出来的文件。
    monkeypatch.setattr(skill_bundle, "repo_root", lambda: tmp_path / "empty-repo")


def _profile(root: Path) -> Path:
    """摆一份本机 `~/.codex/skills` 布局。"""
    script = root / ".codex" / "skills" / skill_bundle.AI_SMELL_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# external\n", encoding="utf-8")
    return script


def _bundle(root: Path) -> Path:
    script = root / skill_bundle.AI_SMELL_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# bundled\n", encoding="utf-8")
    return script


def test_bundled_script_wins_over_local_codex_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _profile(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    bundled = _bundle(tmp_path / "state" / "four-skills-runtime")

    resolved = AiSmellService(runtime_state_root=tmp_path / "state").script

    assert resolved == bundled.resolve()


def test_falls_back_to_local_codex_profile_when_nothing_is_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    external = _profile(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    resolved = AiSmellService(runtime_state_root=tmp_path / "state").script

    assert resolved == external.resolve()


def test_bundle_root_environment_variable_takes_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _profile(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _bundle(tmp_path / "state" / "four-skills-runtime")
    configured = _bundle(tmp_path / "installed")
    monkeypatch.setenv(skill_bundle.BUNDLE_ROOT_ENV, str(tmp_path / "installed"))

    resolved = AiSmellService(runtime_state_root=tmp_path / "state").script

    assert resolved == configured.resolve()


def test_execution_bundle_root_requires_all_family_programs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    partial = state / "four-skills-runtime"
    (partial / skill_bundle.AI_SMELL_SCRIPT).parent.mkdir(parents=True)
    (partial / skill_bundle.AI_SMELL_SCRIPT).write_text("# partial\n", encoding="utf-8")
    assert skill_bundle.execution_bundle_root(state) is None

    for relative in skill_bundle.FAMILY_EXECUTABLES:
        path = partial / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# bundled\n", encoding="utf-8")

    assert skill_bundle.execution_bundle_root(state) == partial.resolve()


def test_empty_card_library_is_not_treated_as_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空的 novel-distillation/library 不能当卡库用。

    随包时会预建 `library/cards`、`library/raw`、`library/reports` 三个空目录
    （stage_four_skills_runtime.py:128）。如果只看 is_dir()，detect_ai_smell
    的 voice_signature_distance 会静默降级成 available=false，而分数照样报出来
    —— 作者看到的分数和以前的稿子不可比，却没有任何提示。
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    bundled = _bundle(tmp_path / "state" / "four-skills-runtime")
    (tmp_path / "state" / "four-skills-runtime" / skill_bundle.DISTILLATION_LIBRARY).mkdir(
        parents=True, exist_ok=True
    )

    assert resolve_library(bundled, tmp_path / "state") is None


def test_kb_library_needs_real_kb_directories_not_just_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kb-manager 的 library 必须真有 `.kb`。

    这是最坑的一处：随包快照建了 domain/style/platform/ops/projects/reports
    六个空目录。`iterdir()` 非空，`is_dir()` 为真——看起来完全正常。
    但作者建档的书全在本机那份库里，跟着随包走会一本都查不到，
    知识增强静默失效。
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    library = tmp_path / "state" / "four-skills-runtime" / skill_bundle.KB_MANAGER_LIBRARY
    for name in ("domain", "style", "platform", "ops", "projects", "reports"):
        (library / name).mkdir(parents=True, exist_ok=True)

    assert skill_bundle.has_kb_entries(library) is False
    assert skill_bundle.kb_manager_library(tmp_path / "state") is None

    (library / "projects" / "宗门只剩七个人.kb").mkdir(parents=True)

    assert skill_bundle.has_kb_entries(library) is True
    assert skill_bundle.kb_manager_library(tmp_path / "state") == library.resolve()


def test_knowledge_context_can_use_bundled_script_with_external_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本随包、知识库仍在本机，是当前真实状态，必须支持。

    旧实现把两者绑在一起（脚本是随包的就只看随包 library），于是
    「随包脚本 + 本机知识库」这个中间态直接失效。
    """
    home = tmp_path / "home"
    profile = home / ".codex" / "skills"
    (profile / skill_bundle.KB_MANAGER_LIBRARY / "projects" / "书.kb").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    state = tmp_path / "state"
    bundle = state / "four-skills-runtime"
    for relative in (skill_bundle.KB_MANAGER_SCRIPT, skill_bundle.KB_CONSUMER_SCRIPT):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# bundled\n", encoding="utf-8")

    service = KnowledgeContextService(runtime_state_root=state)

    assert service.kb_manager == (bundle / skill_bundle.KB_MANAGER_SCRIPT).resolve()
    assert service.library == (profile / skill_bundle.KB_MANAGER_LIBRARY).resolve()
    assert service.available is True


def test_knowledge_context_is_unavailable_without_any_kb_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    state = tmp_path / "state"
    bundle = state / "four-skills-runtime"
    for relative in (skill_bundle.KB_MANAGER_SCRIPT, skill_bundle.KB_CONSUMER_SCRIPT):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# bundled\n", encoding="utf-8")

    service = KnowledgeContextService(runtime_state_root=state)

    assert service.kb_manager is not None
    assert service.library is None
    # 脚本在、库不在 —— 不能报 available，否则调用时才炸，且原因不明。
    assert service.available is False
