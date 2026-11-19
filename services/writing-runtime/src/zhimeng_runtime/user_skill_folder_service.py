"""技能文件夹：像 Codex / Claude Code 一样「丢个文件夹就装好」。

约定与 `~/.codex/skills`、`~/.claude/skills`、`~/.agents/skills` 完全兼容：
每个技能一个子文件夹，里面放一份 `SKILL.md`（YAML frontmatter 声明
`name` + `description`），可选带 `scripts/`。每轮现扫，改了就生效。
启用技能的「名字 + 用途」做成一份简短目录挂在 skill_read_file 的工具说明里，
所有对话都有；模型用到哪个再自己读全文（渐进披露，和 Claude Code 的技能工具、
Codex 的技能清单同构）。

读哪些文件夹（2026-09-26 起默认就读，和本机别的 agent 读同一份）：
1. 本软件自己的 `skills/`（设置页能改、能新建）；
2. `~/.codex/skills`（作者的 Codex 每天在用的那份）；
3. `~/.agents/skills`（跨 agent 的通用位置，Codex、Kimi CLI 等都读）；
4. `~/.claude/skills`（Claude Code 的）。
同名的以前面的为准；文件夹名相同也算同一个技能（项目这份小说技能改过中文名，
和 Codex 那份文件夹同名）。没用上的同名副本记在 `shadowed` 里，设置页照实列出。
点号开头的文件夹（Codex 的 `.system`、同步工具的 `.sync-backup`）不读。
技能文件夹本身是链接（CC Switch、`npx skills` 默认这样往各家同步）照读，按它指向的真位置算；技能里面的链接不跟。

2026-09-28 起本机别家 agent 的技能文件夹也认（``KNOWN_SKILL_SOURCES``：ZCode、Kimi Code、Gemini CLI、WorkBuddy、
CC Switch 的总库……路径照各家源码和档案核过），在设置页「技能库」里一家一个开关——上面三家照旧默认开，
别的默认关、点一下就用上。作者 9-28：「这个应用有一个技能库，还可以其他的智能库，可以一键探索使用其他的」。
外面的技能可以「复制进织梦」：复制一份到自己的 `skills/`，同名以它为准，从此在织梦里随便改，原来那份不动。
和 CC Switch 反过来：它是一份总库往各家里链接 / 复制；织梦是原地读各家的，不往别人的文件夹里写。

技能文件夹里的脚本这里不执行；要跑，模型用执行命令工具按路径跑，和别的 agent 一样。
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.state_roots import ensure_state_root

try:  # yaml 已被 skill_adaptation_service 依赖，这里只做保险回退
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# 单个 SKILL.md 的读取上限（与 adaptation 同量级，防止把上下文撑爆）
MAX_USER_SKILL_MD_BYTES = 256 * 1024

_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9\u3400-\u9fff][a-z0-9._\-\u3400-\u9fff]{0,127}")


def default_shared_skill_roots(home: Path | None = None) -> list[Path]:
    """本机别的 agent 读的技能文件夹，按「同名听谁的」排好。

    Codex 那份排第一：作者的 Codex 每天在用，作者说「Claude 还有其他 agents 都会读取我 codex 的
    skills」；`~/.agents/skills` 是 Codex 现在推荐的用户位置（它自己的 `~/.codex/skills`
    标了旧位置但照读），`~/.claude/skills` 是 Claude Code 的。9-26 这两处都只有更早同步过去的副本。
    """

    base = Path.home() if home is None else Path(home)
    return [base / ".codex" / "skills", base / ".agents" / "skills", base / ".claude" / "skills"]


@dataclass(frozen=True, slots=True)
class SkillSource:
    """本机一家 agent 的技能文件夹：key 记开关用，label 给作者看，relative 相对用户主目录。"""

    key: str
    label: str
    relative: tuple[str, ...]
    default_enabled: bool = False

    def path(self, home: Path) -> Path:
        return Path(home).joinpath(*self.relative)


# 各家读技能的用户级文件夹（照各家源码、CC Switch 的 services/skill.rs 和 docs/research/吸收-20260926 各档案 5.8 核过）。
# 排在前面的同名优先：Codex、通用文件夹、Claude 三家照旧默认开，顺序和 9-26 一样；别的默认关。
KNOWN_SKILL_SOURCES: tuple[SkillSource, ...] = (
    SkillSource("codex", "Codex", (".codex", "skills"), True),
    SkillSource("agents", "通用文件夹（多家 agent 共用）", (".agents", "skills"), True),
    SkillSource("claude", "Claude Code", (".claude", "skills"), True),
    SkillSource("zcode", "ZCode", (".zcode", "skills")),
    SkillSource("kimi", "Kimi Code", (".kimi-code", "skills")),
    SkillSource("gemini", "Gemini CLI", (".gemini", "skills")),
    SkillSource("qwen", "Qwen Code", (".qwen", "skills")),
    SkillSource("opencode", "OpenCode", (".config", "opencode", "skills")),
    SkillSource("crush", "Crush", (".config", "crush", "skills")),
    SkillSource("cursor", "Cursor", (".cursor", "skills")),
    SkillSource("copilot", "GitHub Copilot", (".copilot", "skills")),
    SkillSource("cline", "Cline", (".cline", "skills")),
    SkillSource("roo", "Roo Code", (".roo", "skills")),
    SkillSource("kilo", "Kilo Code", (".kilo", "skills")),
    SkillSource("factory", "Droid（Factory）", (".factory", "skills")),
    SkillSource("trae", "Trae", (".trae", "skills")),
    SkillSource("pi", "Pi", (".pi", "agent", "skills")),
    SkillSource("openclaw", "OpenClaw", (".openclaw", "skills")),
    SkillSource("hermes", "Hermes", (".hermes", "skills")),
    SkillSource("grok", "Grok Build", (".grok", "skills")),
    SkillSource("mimo", "MiMo Code", (".config", "mimocode", "skills")),
    SkillSource("dsh", "DeepSeek Harness", (".dsh", "skills")),
    SkillSource("workbuddy", "WorkBuddy", (".workbuddy", "skills")),
    SkillSource("ccswitch", "CC Switch（它的总技能库）", (".cc-switch", "skills")),
)

# 复制进织梦时不带的东西（版本库、缓存）。
_COPY_IGNORE_PATTERNS = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "node_modules", ".DS_Store")


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """复制进织梦时不带的：版本库、缓存，还有技能里面的链接（读技能的工具也不跟里面的链接，
    跟着复制会把链接指到的整个外面文件夹抄进来）。"""
    skipped = set(_COPY_IGNORE_PATTERNS(directory, names))
    for name in names:
        path = Path(directory) / name
        try:
            if path.is_symlink() or path.is_junction():
                skipped.add(name)
        except OSError:
            skipped.add(name)
    return skipped


@dataclass(frozen=True, slots=True)
class UserSkillEntry:
    """一个已发现的文件夹技能。

    origin 标记来源根目录（project = 本软件自己的 skills/，设置页能改；external = 共享文件夹，
    设置页只读）。root 是找到它的那个技能根，shadowed 是没用上的同名副本所在文件夹。
    """

    name: str
    folder: str
    description: str
    has_scripts: bool
    enabled: bool
    origin: str = "project"
    root: str = ""
    shadowed: tuple[str, ...] = ()
    # frontmatter 的 allowed-tools（Agent Skills 规范里「这个技能用到的工具」）。
    # 织梦的几个随技能来的工具（如 ai_smell_score）只在有启用的技能声明它时才给模型。
    tools: tuple[str, ...] = ()
    # 来自哪一家（KNOWN_SKILL_SOURCES 的 key 和名字）；织梦自己的是 "zhimeng"。
    source: str = ""
    source_label: str = ""


class UserSkillFolderService:
    """扫描技能文件夹，维护启用状态，给技能目录和 skill_* 工具用。"""

    def __init__(
        self,
        skills_root: Path,
        state_root: Path,
        extra_roots: Sequence[Path] | None = None,
        *,
        sources: Sequence[SkillSource] | None = None,
        home: Path | None = None,
    ) -> None:
        """``extra_roots`` 是写死的几处（测试和隔离实例用环境变量给），一直读；
        ``sources`` 是本机各家 agent 的文件夹（正常运行时给 ``KNOWN_SKILL_SOURCES``），作者在设置页一家一个开关。"""
        self._skills_root = Path(skills_root)
        self._extra_roots = [Path(root) for root in (extra_roots or [])]
        self._sources = tuple(sources or ())
        self._home = Path.home() if home is None else Path(home)
        self._state_path = Path(ensure_state_root(state_root) / "user-skills.json")

    # ------------------------------------------------------------------
    # 扫描与上架
    # ------------------------------------------------------------------

    @property
    def skills_root(self) -> Path:
        return self._skills_root

    def roots(self) -> list[tuple[Path, str]]:
        """按「同名听谁的」排好的技能根：本软件自己的 skills/ 在前，共享文件夹在后。"""
        return [(root, origin) for root, origin, _key, _label in self._labelled_roots()]

    def _labelled_roots(self) -> list[tuple[Path, str, str, str]]:
        """(文件夹, project / external, 来自哪一家的 key, 名字)：织梦自己的、写死的几处、开着的各家。"""
        roots = [(self._skills_root, "project", "zhimeng", "织梦")]
        roots += [(root, "external", "", "") for root in self._extra_roots]
        switches = self._source_switches()
        for source in self._sources:
            if switches.get(source.key, source.default_enabled):
                roots.append((source.path(self._home), "external", source.key, source.label))
        return roots

    def scan(self) -> list[UserSkillEntry]:
        """遍历全部技能根，返回按名字排序的技能清单。

        文件夹不存在、子项缺 SKILL.md 或 frontmatter 不合法时一律跳过
        （fail-open：坏技能不能挡住好技能），只上报能解析的。
        同名（技能名或文件夹名相同）以排在前面的根为准，后面的记进 shadowed。
        """
        disabled = self._disabled_names()
        kept: list[UserSkillEntry] = []
        shadowed: dict[int, list[str]] = {}
        by_name: dict[str, int] = {}
        by_folder: dict[str, int] = {}
        seen_roots: set[str] = set()
        for root, origin, key, label in self._labelled_roots():
            # 同一个文件夹只读一遍（写死的几处和各家开关可能指到同一处，也有人把整个技能文件夹链接到另一家的）。
            marker = str(root.resolve()).casefold()
            if marker in seen_roots:
                continue
            seen_roots.add(marker)
            for entry in self._scan_root(root, origin, disabled, source=key, source_label=label):
                folder_key = Path(entry.folder).name.casefold()
                index = by_name.get(entry.name)
                if index is None:
                    index = by_folder.get(folder_key)
                if index is not None:
                    shadowed.setdefault(index, []).append(entry.folder)
                    continue
                by_name[entry.name] = len(kept)
                by_folder[folder_key] = len(kept)
                kept.append(entry)
        entries = [
            replace(entry, shadowed=tuple(shadowed[index])) if index in shadowed else entry
            for index, entry in enumerate(kept)
        ]
        entries.sort(key=lambda item: item.name)
        return entries

    def _scan_root(
        self,
        root: Path,
        origin: str,
        disabled: set[str],
        *,
        source: str = "",
        source_label: str = "",
    ) -> list[UserSkillEntry]:
        found: list[UserSkillEntry] = []
        if not root.is_dir():
            return found
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return found
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                # 技能文件夹本身是链接也读：CC Switch、`npx skills` 默认用链接往各家同步（坏链接 is_dir 为假，照样跳过）。
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue
                raw = skill_md.read_bytes()
                name, description = self._parse_frontmatter(raw, fallback_name=child.name)
            except (OSError, ValueError):
                continue
            found.append(
                UserSkillEntry(
                    name=name,
                    folder=str(child),
                    description=description,
                    has_scripts=(child / "scripts").is_dir(),
                    enabled=name not in disabled,
                    origin=origin,
                    root=str(root),
                    tools=self._allowed_tools(raw),
                    source=source,
                    source_label=source_label,
                )
            )
        return found

    def _allowed_tools(self, content: bytes) -> tuple[str, ...]:
        """frontmatter 里的 allowed-tools：规范写法是空格分隔的一串，也认逗号分隔和 YAML 列表。"""
        try:
            parsed = self._frontmatter_mapping(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return ()
        value = parsed.get("allowed-tools") if parsed else None
        if isinstance(value, list):
            words = [str(item) for item in value]
        elif isinstance(value, str):
            words = value.replace(",", " ").split()
        else:
            return ()
        return tuple(dict.fromkeys(word.strip() for word in words if word.strip()))

    def menu(self) -> list[UserSkillEntry]:
        """启用中的技能（技能目录用）。按名字排序：一场对话里每轮相同，提示缓存才命中。"""
        return [entry for entry in self.scan() if entry.enabled]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """skill_search 拆检索词用：英文数字按单词，中文按 2 字滑窗。"""
        tokens: set[str] = set()
        lowered = text.lower()
        for word in re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", lowered):
            tokens.add(word)
        chinese = re.findall(r"[\u4e00-\u9fff]+", lowered)
        for run in chinese:
            if len(run) == 1:
                tokens.add(run)
                continue
            for index in range(len(run) - 1):
                tokens.add(run[index : index + 2])
        return tokens

    # ------------------------------------------------------------------
    # 启用 / 禁用（作者的选择持久化，不被扫描覆盖）
    # ------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> UserSkillEntry | None:
        entry = next((item for item in self.scan() if item.name == name), None)
        if entry is None:
            return None
        disabled = self._disabled_names()
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        self._write_disabled(disabled)
        return replace(entry, enabled=enabled)

    # ------------------------------------------------------------------
    # 保存优化后的 SKILL.md（作者主导：只允许写项目内技能）
    # ------------------------------------------------------------------

    def read_instructions_file(self, name: str) -> tuple[bool, str, bool]:
        """读取完整 SKILL.md；第三个返回值表示能否在本项目内编辑。"""
        entry = next((item for item in self.scan() if item.name == name), None)
        if entry is None:
            return False, f"技能 '{name}' 不存在", False
        try:
            raw = (Path(entry.folder) / "SKILL.md").read_bytes()
        except OSError as exc:
            return False, f"读取失败：{exc}", False
        if len(raw) > MAX_USER_SKILL_MD_BYTES:
            return False, "技能文件太大，不能在设置里编辑", False
        return True, raw.decode("utf-8-sig", errors="replace"), entry.origin == "project"

    def create_skill(self, name: str, description: str, content: str | None = None) -> tuple[bool, str]:
        """把用途和 Markdown 正文一并保存为 SKILL.md；省略正文的旧调用仍生成模板。"""
        normalized_name = str(name or "").strip().lower()
        normalized_description = str(description or "").strip()
        if not _SKILL_NAME_PATTERN.fullmatch(normalized_name):
            return False, "技能名可以用中文、小写字母、数字、点、横线或下划线"
        if not normalized_description:
            return False, "请写一句这个技能是做什么的"
        if content is not None and not content.strip():
            return False, "请填写技能的使用方法"
        if any(entry.name == normalized_name for entry in self.scan()):
            return False, f"技能 '{normalized_name}' 已经存在"
        # JSON 字符串是合法的 YAML 标量；换行或冒号不会打断用途字段。
        description_line = json.dumps(" ".join(normalized_description.split())[:500], ensure_ascii=False)
        body = content if content is not None else (
            "# 使用方法\n\n请在这里写清楚：什么时候使用这个技能，以及具体该怎么做。\n"
        )
        text = (
            "---\n"
            f"name: {normalized_name}\n"
            f"description: {description_line}\n"
            "---\n\n"
            + body.rstrip() + "\n"
        )
        raw = text.encode("utf-8")
        try:
            self._parse_frontmatter(raw, fallback_name=normalized_name)
        except ValueError as exc:
            return False, f"技能内容无法保存：{exc}"
        folder = self._skills_root / normalized_name
        temporary = folder / f"SKILL.md.{uuid.uuid4().hex}.tmp"
        created_folder = False
        try:
            folder.mkdir(parents=True, exist_ok=False)
            created_folder = True
            temporary.write_bytes(raw)
            replace_with_retry(temporary, folder / "SKILL.md")
        except OSError as exc:
            if created_folder:
                # 只清理本次创建的临时文件和空目录，失败后可直接重试。
                try:
                    temporary.unlink(missing_ok=True)
                    folder.rmdir()
                except OSError:
                    pass
            return False, f"创建失败：{exc}"
        return True, f"已创建技能 '{normalized_name}'"

    def save_instructions(self, name: str, content: str, *, expected_sha256: str | None = None) -> tuple[bool, str]:
        """把新的 SKILL.md 全文写回磁盘。返回 (ok, message)。

        共享文件夹（~/.codex/skills 等）里的技能设置页不改：那是作者别的 agent 也在用的一份，
        作者说过 Codex 那份由他自己整体更新。新内容必须通过 frontmatter 校验。
        """
        entry = next((item for item in self.scan() if item.name == name), None)
        if entry is None:
            return False, f"技能 '{name}' 不存在"
        if entry.origin != "project":
            return False, (
                f"技能 '{name}' 在共享技能文件夹里（{entry.folder}），别的 agent 也读这一份，这里不改它。"
                f"要只在织梦里改，把它复制进 {self._skills_root} 再改——同名的以那边为准。"
            )
        text = content
        try:
            parsed_name, parsed_description = self._parse_frontmatter(
                text.encode("utf-8"), fallback_name=name
            )
        except ValueError as exc:
            return False, f"新内容不是合法的 SKILL.md：{exc}"
        if parsed_name != name:
            return False, (
                f"新内容 frontmatter 里的 name 是 '{parsed_name}'，"
                f"与被修改的技能 '{name}' 不一致，拒绝写入"
            )
        skill_md = Path(entry.folder) / "SKILL.md"
        backup = skill_md.with_name("SKILL.md.bak")
        try:
            original = skill_md.read_bytes()
            original_text = original.decode("utf-8-sig")
            if expected_sha256 is not None and hashlib.sha256(original_text.encode("utf-8")).hexdigest() != expected_sha256:
                return False, "技能文件已被其他操作修改。当前草稿仍保留，请重新读取并核对后再保存。"
            # Browser textareas normalize newlines; preserve the file's original format.
            if "\r\n" in original_text and "\n" not in original_text.replace("\r\n", ""):
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            encoded = text.encode("utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8")
            if len(encoded) > MAX_USER_SKILL_MD_BYTES:
                return False, "保留原文件编码和换行后超过技能文件大小上限，未写入。"
            backup.write_bytes(original)
            # 临时文件 + 原子替换：写一半崩溃不会留下半截 SKILL.md
            # （scan 对坏 frontmatter 是静默跳过，技能会无声消失）。
            temporary = skill_md.with_name(f"SKILL.md.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(encoded)
                if skill_md.read_bytes() != original:
                    return False, "技能文件在保存期间发生变化，未覆盖新内容。请重新读取后核对。"
                replace_with_retry(temporary, skill_md)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            return False, f"写入失败：{exc}"
        return True, (
            f"已保存技能 '{name}'（{len(parsed_description)} 字用途说明）。"
            f"旧版备份在 {backup.name}。"
        )

    # ------------------------------------------------------------------
    # frontmatter 解析（与 SKILL.md 生态约定一致，容错处理）
    # ------------------------------------------------------------------

    def _parse_frontmatter(self, content: bytes, *, fallback_name: str) -> tuple[str, str]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("SKILL.md 必须是 UTF-8") from exc
        if len(content) > MAX_USER_SKILL_MD_BYTES:
            raise ValueError("SKILL.md 超过大小上限")
        parsed = self._frontmatter_mapping(text)
        raw_name = parsed.get("name") if isinstance(parsed, dict) else None
        name = str(raw_name).strip().lower() if raw_name else fallback_name.lower()
        if not _SKILL_NAME_PATTERN.fullmatch(name):
            name = re.sub(r"[^a-z0-9._\-\u3400-\u9fff]", "-", name.lower()).strip("-")[:128]
            if not name or not _SKILL_NAME_PATTERN.fullmatch(name):
                name = fallback_name.lower()
                if not _SKILL_NAME_PATTERN.fullmatch(name):
                    raise ValueError("无法从 frontmatter 或文件夹名派生技能名")
        raw_description = parsed.get("description") if isinstance(parsed, dict) else None
        description = " ".join(str(raw_description).split()) if raw_description else ""
        if not description:
            raise ValueError("frontmatter description 不能为空")
        return name, description[:500]

    def _frontmatter_mapping(self, text: str) -> dict[str, Any]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("SKILL.md 必须以 frontmatter 开头")
        closing = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        if closing is None:
            raise ValueError("SKILL.md frontmatter 未闭合")
        block = "\n".join(lines[1:closing])
        parsed: dict[str, Any] | None = None
        if yaml is not None:
            try:
                loaded = yaml.safe_load(block)
                parsed = loaded if isinstance(loaded, dict) else None
            except yaml.YAMLError:
                parsed = None
        if parsed is None:
            parsed = self._naive_frontmatter(block)
        return parsed

    @staticmethod
    def _naive_frontmatter(block: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip("'\"")
        return result

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _disabled_names(self) -> set[str]:
        value = self._read_state().get("disabled")
        if not isinstance(value, list):
            return set()
        return {str(item) for item in value}

    def _source_switches(self) -> dict[str, bool]:
        """作者在「技能库」里拨过的开关（没拨过的按各家的默认）。"""
        value = self._read_state().get("sources")
        if not isinstance(value, dict):
            return {}
        return {str(key): bool(flag) for key, flag in value.items()}

    def _write_state(self, *, disabled: set[str] | None = None, sources: dict[str, bool] | None = None) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        payload = {
            "schema": "zhimeng.user-skills-state.v1",
            "disabled": sorted(disabled if disabled is not None else self._disabled_names()),
            "sources": dict(sorted((sources if sources is not None else self._source_switches()).items())),
        }
        for key, value in state.items():
            payload.setdefault(key, value)
        # 临时名带随机后缀：两个并发启用/禁用共用固定 tmp 名时，
        # 后写者会覆盖前写者或 replace 时报 FileNotFoundError。
        temporary = self._state_path.with_name(
            f"{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            replace_with_retry(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_disabled(self, disabled: set[str]) -> None:
        self._write_state(disabled=disabled)

    # ------------------------------------------------------------------
    # 本机别家 agent 的技能文件夹（技能库）
    # ------------------------------------------------------------------

    def sources_view(self) -> list[dict[str, Any]]:
        """各家一行：在不在这台电脑上、里面几个技能、织梦用不用。"""
        switches = self._source_switches()
        rows: list[dict[str, Any]] = []
        for source in self._sources:
            path = source.path(self._home)
            exists = path.is_dir()
            rows.append(
                {
                    "key": source.key,
                    "label": source.label,
                    "path": str(path),
                    "exists": exists,
                    "count": len(self._scan_root(path, "external", set())) if exists else 0,
                    "enabled": switches.get(source.key, source.default_enabled),
                    "default_enabled": source.default_enabled,
                }
            )
        return rows

    def set_source_enabled(self, key: str, enabled: bool) -> bool:
        """拨一家的开关；没有这一家返回 False。"""
        if not any(source.key == key for source in self._sources):
            return False
        switches = self._source_switches()
        switches[key] = bool(enabled)
        self._write_state(sources=switches)
        return True

    def copy_to_library(self, name: str) -> tuple[bool, str, UserSkillEntry | None]:
        """把别家文件夹里的一个技能复制一份进织梦自己的 skills/：同名以它为准，从此在织梦里能改，原来那份不动。"""
        entry = next((item for item in self.scan() if item.name == name), None)
        if entry is None:
            return False, f"技能 '{name}' 不存在", None
        if entry.origin == "project":
            return False, f"技能 '{name}' 已经在织梦自己的技能库里（{entry.folder}）", None
        source = Path(entry.folder)
        target = self._skills_root / source.name
        if target.exists():
            return False, f"织梦的技能库里已经有同名的文件夹 {target}，没有覆盖", None
        temporary = self._skills_root / f".{source.name}.{uuid.uuid4().hex}.copying"
        try:
            self._skills_root.mkdir(parents=True, exist_ok=True)
            # 先整份复制到临时名，再一次改名：复制到一半出错不会留下缺文件的半个技能。
            shutil.copytree(source, temporary, ignore=_copy_ignore)
            temporary.rename(target)
        except OSError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            return False, f"复制失败：{exc}", None
        copied = next((item for item in self.scan() if item.name == name and item.origin == "project"), None)
        where = entry.source_label or str(entry.root)
        return True, f"已把「{name}」从 {where} 复制进织梦的技能库（{target}），以后在织梦里用的是这一份。", copied

    def public_view(self) -> dict[str, Any]:
        entries = self.scan()
        return {
            "schema": "zhimeng.user-skills.v1",
            "skills_root": str(self._skills_root),
            "extra_roots": [str(root) for root in self._extra_roots],
            "roots": [
                {"path": str(root), "origin": origin, "exists": root.is_dir(), "source": key, "label": label}
                for root, origin, key, label in self._labelled_roots()
            ],
            "sources": self.sources_view(),
            "exists": self._skills_root.is_dir(),
            "total": len(entries),
            "enabled": sum(1 for entry in entries if entry.enabled),
            "skills": [
                {
                    "name": entry.name,
                    "description": entry.description,
                    "folder": entry.folder,
                    "root": entry.root,
                    "shadowed": list(entry.shadowed),
                    "has_scripts": entry.has_scripts,
                    "enabled": entry.enabled,
                    "origin": entry.origin,
                    "source": entry.source,
                    "source_label": entry.source_label,
                }
                for entry in entries
            ],
        }


__all__ = [
    "KNOWN_SKILL_SOURCES",
    "SkillSource",
    "UserSkillEntry",
    "UserSkillFolderService",
    "default_shared_skill_roots",
]
