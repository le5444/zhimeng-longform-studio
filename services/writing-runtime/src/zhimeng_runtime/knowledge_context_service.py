"""调用 novel-kb-manager + novel-creation-suite，为某一章装配知识上下文。

这是四套 Skills 自己设计好的相邻边，不另造：

    kb_manager assemble-context --out ctx.json      # 全量装配（含大量诊断）
      -> consume_kb_manager_context ctx.json        # 压成写作层能吃的输入

**知识增强与正式门禁是解耦的**（这一点很重要）：

`consume_kb_manager_context` 在项目证据不齐时会以退出码 78 硬闸拦截
（`formal_draft_allowed=false` + blockers 列表），但**它仍然会产出 `must_consume`**。

所以本服务的策略是：
* `must_consume` 拿来增强写作提示 —— 作者立刻受益；
* `blockers` 如实带回给作者看 —— 告诉他离"严格模式"还差什么；
* 全程失败不阻断出稿 —— 装配挂了就退回纯项目真值写作。

⚠️ 副作用：`assemble-context` 会往 `<project>.kb/chapter_context/` 写文件，不是只读。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# consume_kb_manager_context 用退出码 78 表示"门禁拦截"，不是崩溃
GATE_BLOCKED_EXIT_CODE = 78
DEFAULT_TIMEOUT_SECONDS = 300

KB_MANAGER_RELATIVE = Path(".codex/skills/novel-kb-manager/scripts/kb_manager.py")
CONSUMER_RELATIVE = Path(".codex/skills/novel-creation-suite/scripts/consume_kb_manager_context.py")


class KnowledgeContextError(RuntimeError):
    code = "knowledge_context_failed"


DISTILLATION_CARDS_RELATIVE = Path(".codex/skills/novel-distillation/library/cards.jsonl")

# 从真卡里挑出「人能读、对写作有用」的字段。其余（校验梯、分数、来源、
# 时间戳）是给系统看的，塞进提示词只会稀释信号，还会把文笔带偏。
CARD_READABLE_FIELDS = ("summary", "reader_promise", "reader_effect")


def _resolve_cards(card_ids: set[str], cards_path: Path) -> dict[str, dict]:
    """按 card_id 从 cards.jsonl 捞出真卡。

    kb-manager 的设计是「只存 active card 入口，不复制全文」，
    所以 must_consume 里只有 ID；卡的正文必须回 novel-distillation 的库里取。
    """
    if not card_ids or not cards_path.is_file():
        return {}
    found: dict[str, dict] = {}
    try:
        with cards_path.open(encoding="utf-8") as handle:
            for line in handle:
                if len(found) == len(card_ids):
                    break
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                identifier = card.get("card_id")
                if identifier in card_ids:
                    found[identifier] = card
    except OSError:
        return {}
    return found


def build_writing_guidance(must_consume: dict, cards_path: Path) -> str:
    """把机器可读的 must_consume 转成给写手看的人话。

    只保留三类真正有用的东西：
      1. 命中的机制卡（标题 + 一句话机制 + 读者承诺）
      2. 未收线的伏笔与待接线索（非空时才给）
      3. 续写层硬性提醒

    刻意丢弃：card_id、校验梯、分数、路由健康度、空数组——
    这些是诊断，塞进提示词会让模型忙着「交差」而不是写好看。
    """
    if not isinstance(must_consume, dict):
        return ""

    wanted: set[str] = set()
    for key in ("selected_distillation_entries", "selected_ops_rules"):
        for entry in must_consume.get(key) or []:
            if isinstance(entry, dict) and entry.get("card_id"):
                wanted.add(str(entry["card_id"]))
    cards = _resolve_cards(wanted, cards_path)

    sections: list[str] = []

    if cards:
        lines = ["【可用的写作机制】"]
        for card in cards.values():
            title = str(card.get("title") or "").strip()
            if not title:
                continue
            lines.append(f"· {title}")
            for field_name in CARD_READABLE_FIELDS:
                value = card.get(field_name)
                if isinstance(value, str) and value.strip():
                    lines.append(f"    {value.strip()}")
                    break
        if len(lines) > 1:
            sections.append("\n".join(lines))

    state = must_consume.get("continuation_state_summary")
    if isinstance(state, dict):
        threads = [t for t in (state.get("open_threads") or []) if t]
        foreshadowing = [f for f in (state.get("active_foreshadowing") or []) if f]
        if threads:
            sections.append(
                "【尚未收线的剧情线】\n"
                + "\n".join(f"· {json.dumps(t, ensure_ascii=False)}" for t in threads[:8])
            )
        if foreshadowing:
            sections.append(
                "【已埋下、尚未兑现的伏笔（不要遗忘，也不要提前揭穿）】\n"
                + "\n".join(f"· {json.dumps(f, ensure_ascii=False)}" for f in foreshadowing[:8])
            )

    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    project: str
    chapter_id: str
    route: str
    #  已转成人话、可直接进提示词的写作指导（可能为空）
    guidance: str
    #  严格模式是否放行；未放行时 blockers 说明差什么
    formal_draft_allowed: bool
    blockers: tuple[str, ...] = ()
    context_health: str = "unknown"
    route_health: str = "unknown"

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "chapter_id": self.chapter_id,
            "route": self.route,
            "guidance_characters": len(self.guidance),
            "formal_draft_allowed": self.formal_draft_allowed,
            "blockers": list(self.blockers),
            "context_health": self.context_health,
            "route_health": self.route_health,
        }


def _resolve(explicit: str | os.PathLike[str] | None, env_name: str, relative: Path) -> Path | None:
    for candidate in (explicit, os.environ.get(env_name), Path.home() / relative):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


@dataclass
class KnowledgeContextService:
    kb_manager_path: str | os.PathLike[str] | None = None
    consumer_path: str | os.PathLike[str] | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    _clean_env: dict = field(default_factory=dict, init=False, repr=False)

    @property
    def kb_manager(self) -> Path | None:
        return _resolve(self.kb_manager_path, "ZHIMENG_KB_MANAGER_SCRIPT", KB_MANAGER_RELATIVE)

    @property
    def consumer(self) -> Path | None:
        return _resolve(self.consumer_path, "ZHIMENG_KB_CONSUMER_SCRIPT", CONSUMER_RELATIVE)

    @property
    def available(self) -> bool:
        return self.kb_manager is not None and self.consumer is not None

    def find_project_for_root(self, project_root: Path) -> str | None:
        """反查某本小说在 kb 库里叫什么项目名。

        kb 库里每个 `<名>.kb/manifest.json` 记着它绑定的 `creation_project_root`。
        没建档的书返回 None —— 调用方据此跳过知识增强，不报错。
        """
        kb_manager = self.kb_manager
        if kb_manager is None:
            return None
        projects_dir = kb_manager.parent.parent / "library" / "projects"
        if not projects_dir.is_dir():
            return None

        target = os.path.normcase(str(project_root.resolve()))
        for manifest in projects_dir.glob("*.kb/manifest.json"):
            try:
                bound = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "creation_project_root"
                )
            except (OSError, json.JSONDecodeError):
                continue
            if not bound:
                continue
            try:
                if os.path.normcase(str(Path(bound).resolve())) == target:
                    return manifest.parent.stem
            except OSError:
                continue
        return None

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        # 这些脚本只读知识库、算路由，不该拿到任何模型密钥
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ZHIMENG_AGENT_PROVIDER", "ANTHROPIC_", "OPENAI_"))
        }
        return subprocess.run(
            command,
            capture_output=True,
            timeout=self.timeout_seconds,
            env=environment,
        )

    def assemble(
        self,
        *,
        project: str,
        chapter_id: str,
        genre: str | None = None,
        platform: str | None = None,
        style: str | None = None,
        stage: str = "draft",
    ) -> KnowledgeContext:
        kb_manager = self.kb_manager
        consumer = self.consumer
        if kb_manager is None or consumer is None:
            raise KnowledgeContextError("没找到 kb_manager.py 或 consume_kb_manager_context.py")

        with tempfile.TemporaryDirectory(prefix="zhimeng-kb-ctx-") as workspace:
            context_pack = Path(workspace) / "ctx.json"
            writer_input = Path(workspace) / "writer_input.json"

            command = [
                sys.executable, str(kb_manager), "assemble-context",
                "--project", project,
                "--chapter-id", chapter_id,
                "--stage", stage,
                "--max-chars", "2600",
                "--out", str(context_pack),
            ]
            for flag, value in (("--genre", genre), ("--platform", platform), ("--style", style)):
                if value:
                    command += [flag, value]

            try:
                assembled = self._run(command)
            except subprocess.TimeoutExpired as exc:
                raise KnowledgeContextError("装配上下文超时") from exc
            except OSError as exc:
                raise KnowledgeContextError(f"无法运行 kb_manager: {exc}") from exc

            if assembled.returncode != 0 or not context_pack.is_file():
                detail = assembled.stderr.decode("utf-8", errors="replace")[:300]
                raise KnowledgeContextError(f"装配上下文失败: {detail}")

            try:
                consumed = self._run([
                    sys.executable, str(consumer), str(context_pack), "--out", str(writer_input)
                ])
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise KnowledgeContextError(f"压缩写作输入失败: {exc}") from exc

            # 78 = 门禁拦截，但 writer_input 仍然产出，知识照样可用
            if consumed.returncode not in (0, GATE_BLOCKED_EXIT_CODE):
                detail = consumed.stderr.decode("utf-8", errors="replace")[:300]
                raise KnowledgeContextError(
                    f"压缩写作输入失败（退出码 {consumed.returncode}）: {detail}"
                )

            try:
                report = json.loads(consumed.stdout.decode("utf-8", errors="replace") or "{}")
            except json.JSONDecodeError:
                report = {}

            guidance = ""
            if writer_input.is_file():
                try:
                    payload = json.loads(writer_input.read_text(encoding="utf-8"))
                    guidance = build_writing_guidance(
                        payload.get("must_consume") or {},
                        Path.home() / DISTILLATION_CARDS_RELATIVE,
                    )
                except (OSError, json.JSONDecodeError):
                    guidance = ""

            pack_health = {}
            try:
                pack = json.loads(context_pack.read_text(encoding="utf-8"))
                pack_health = {
                    "context_health": str(pack.get("context_health", "unknown")),
                    "route_health": str(pack.get("route_health", "unknown")),
                }
            except (OSError, json.JSONDecodeError):
                pass

        return KnowledgeContext(
            project=project,
            chapter_id=chapter_id,
            route=str(report.get("route", "")),
            guidance=guidance,
            formal_draft_allowed=bool(report.get("formal_draft_allowed", False)),
            blockers=tuple(str(b) for b in (report.get("blockers") or [])),
            context_health=pack_health.get("context_health", "unknown"),
            route_health=pack_health.get("route_health", "unknown"),
        )
