"""定位随包（bundled）四套 Skills 程序与知识库的唯一入口。

为什么要有这个模块：

之前有三处各自写了一套查找逻辑——
* `four_skills_runtime_service`（已随死面裁掉）认识 `desktop_packaging/resources/four-skills-runtime`；
* `ai_smell_service` 只认一个不存在的 `zhimeng_runtime/data/ai-smell/`；
* `knowledge_context_service` 压根不认随包，直接掉 `~/.codex`。

结果是**状态面板报「换电脑不依赖本机 .codex」，而真正跑写作的两个服务还在用 .codex**。
这是名实不符，不是路径问题。所以查找顺序必须只有一份，三处共用。

查找顺序（先随包，后本机 profile）：
  1. 环境变量 `ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT`（打包后由启动器指定）
  2. `<runtime 状态目录>/four-skills-runtime`（安装后释放到用户数据区）
  3. `<仓库>/skills`（项目自有副本，作者可直接改，2026-08-17 从 .codex 复制而来）
  4. `<仓库>/desktop_packaging/resources/four-skills-runtime`（开发期就地用）
  5. `<仓库>/data/four-skills-runtime`
  6. `~/.codex/skills`（作者本机 profile，**只能是最后兜底**）

`bundled_only=True` 时不返回第 6 项——用于判断「这台机器上是否真的不依赖 .codex」。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

BUNDLE_ROOT_ENV = "ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT"
EXTERNAL_PROFILE_RELATIVE = Path(".codex/skills")


def repo_root() -> Path:
    """仓库根目录：本文件在 services/writing-runtime/src/zhimeng_runtime/ 下四层。"""
    return Path(__file__).resolve().parents[4]


def external_profile_root() -> Path | None:
    try:
        return Path.home() / EXTERNAL_PROFILE_RELATIVE
    except RuntimeError:
        # 某些服务账户下 Path.home() 会抛错；此时只当作没有本机 profile。
        return None


def bundle_roots(runtime_state_root: Path | None = None) -> tuple[Path, ...]:
    """只包含随包候选根，不含 `~/.codex`。"""
    roots: list[Path] = []
    configured = os.environ.get(BUNDLE_ROOT_ENV, "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    if runtime_state_root is not None:
        roots.append(Path(runtime_state_root) / "four-skills-runtime")
    root = repo_root()
    roots.append(root / "skills")
    roots.append(root / "desktop_packaging" / "resources" / "four-skills-runtime")
    roots.append(root / "data" / "four-skills-runtime")
    return tuple(roots)


def search_roots(
    runtime_state_root: Path | None = None,
    *,
    bundled_only: bool = False,
) -> tuple[Path, ...]:
    """随包候选根 + （可选）本机 `~/.codex/skills` 兜底。"""
    roots = list(bundle_roots(runtime_state_root))
    if not bundled_only:
        external = external_profile_root()
        if external is not None:
            roots.append(external)
    return tuple(roots)


def find_file(
    relative: str | Path,
    runtime_state_root: Path | None = None,
    *,
    bundled_only: bool = False,
) -> Path | None:
    """在候选根里按顺序找一个文件，返回第一个真实存在的常规文件。"""
    for root in search_roots(runtime_state_root, bundled_only=bundled_only):
        candidate = root / relative
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate.resolve()
        except OSError:
            continue
    return None


def find_directory(
    relative: str | Path,
    runtime_state_root: Path | None = None,
    *,
    bundled_only: bool = False,
    require_non_empty: bool = False,
    predicate: Callable[[Path], bool] | None = None,
) -> Path | None:
    """在候选根里按顺序找一个目录。

    `require_non_empty=True` 时跳过空目录——随包快照会预先建出 `library/domain`
    这类空骨架目录（`stage_four_skills_runtime.py:160`），光看 `is_dir()` 会把
    「目录在但一张卡都没有」误判成可用，导致知识装配静默降级。

    `predicate` 用于更强的内容判据（例如「必须能列出至少一个 `*.kb`」）。
    空骨架目录只靠 `iterdir()` 也能通过，因为它下面还有一层空子目录。
    """
    for root in search_roots(runtime_state_root, bundled_only=bundled_only):
        candidate = root / relative
        try:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            if require_non_empty and not any(candidate.iterdir()):
                continue
            if predicate is not None and not predicate(candidate):
                continue
            return candidate.resolve()
        except OSError:
            continue
    return None


def has_kb_entries(library: Path) -> bool:
    """kb-manager 的 library 是否真的装着东西。

    判据取自 kb_manager 自己的布局：分层库在 `global/<层>/<名>.kb`，
    项目库在 `projects/<名>.kb`。随包快照只建了空的 `domain/ops/...` 骨架，
    一张 `.kb` 都没有——那种目录不能算「配套齐全」。
    """
    try:
        if next(library.glob("global/*/*.kb"), None) is not None:
            return True
        if next(library.glob("projects/*.kb"), None) is not None:
            return True
        # 兼容把分层库直接放在 library/<层>/ 下的旧布局
        return next(library.glob("*/*.kb"), None) is not None
    except OSError:
        return False


def has_distillation_cards(library: Path) -> bool:
    """novel-distillation 的 library 是否真的有卡。

    判据就是 `detect_ai_smell.py` 实际读的那个文件：`cards.jsonl`
    （黑名单卡和 voice_prose 卡都从这里来）。随包快照会预建
    `cards/`、`raw/`、`reports/` 三个空目录，光看目录非空会误判。
    """
    try:
        return (library / "cards.jsonl").is_file()
    except OSError:
        return False


# 四套 Skills 在任一候选根下的相对位置（随包与 ~/.codex/skills 布局一致）。
AI_SMELL_SCRIPT = Path("novel-creation-suite/scripts/detect_ai_smell.py")
KB_MANAGER_SCRIPT = Path("novel-kb-manager/scripts/kb_manager.py")
KB_CONSUMER_SCRIPT = Path("novel-creation-suite/scripts/consume_kb_manager_context.py")
DISTILLATION_LIBRARY = Path("novel-distillation/library")
DISTILLATION_CARDS = DISTILLATION_LIBRARY / "cards.jsonl"
KB_MANAGER_LIBRARY = Path("novel-kb-manager/library")
FAMILY_EXECUTABLES = (
    Path("novel-distillation/scripts/distill_source_packet.py"),
    Path("novel-distillation/scripts/distill_kb.py"),
    KB_MANAGER_SCRIPT,
    AI_SMELL_SCRIPT,
)


def execution_bundle_root(runtime_state_root: Path | None = None) -> Path | None:
    """Return one complete regular bundle root that family adapters can execute."""

    for raw_root in bundle_roots(runtime_state_root):
        try:
            if not raw_root.is_dir() or raw_root.is_symlink():
                continue
            root = raw_root.resolve(strict=True)
            if all(
                (root / relative).is_file() and not (root / relative).is_symlink()
                for relative in FAMILY_EXECUTABLES
            ):
                return root
        except OSError:
            continue
    return None


__all__ = [
    "AI_SMELL_SCRIPT",
    "BUNDLE_ROOT_ENV",
    "DISTILLATION_CARDS",
    "DISTILLATION_LIBRARY",
    "FAMILY_EXECUTABLES",
    "KB_CONSUMER_SCRIPT",
    "KB_MANAGER_LIBRARY",
    "KB_MANAGER_SCRIPT",
    "bundle_roots",
    "distillation_library",
    "external_profile_root",
    "execution_bundle_root",
    "find_directory",
    "find_file",
    "has_distillation_cards",
    "has_kb_entries",
    "kb_manager_library",
    "repo_root",
    "search_roots",
]


def distillation_library(runtime_state_root: Path | None = None) -> Path | None:
    """novel-distillation 真正能用的 library 根（必须含 cards.jsonl）。"""
    return find_directory(
        DISTILLATION_LIBRARY,
        runtime_state_root,
        predicate=has_distillation_cards,
    )


def kb_manager_library(runtime_state_root: Path | None = None) -> Path | None:
    """kb-manager 真正能用的 library 根（必须含至少一个 `.kb`）。"""
    return find_directory(
        KB_MANAGER_LIBRARY,
        runtime_state_root,
        predicate=has_kb_entries,
    )
