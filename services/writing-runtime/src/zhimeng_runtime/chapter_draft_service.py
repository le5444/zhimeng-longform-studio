"""从项目真值生成下一章正文候选，并落成待作者审阅的 ChangeSet。

边界（与 PRODUCT_REQUIREMENTS 真值表一致）：

* 本服务**只产出候选**。ChangeSet 建成后处于 reviewing 状态，
  所有 hunk 都是 pending，必须由作者在工作台看 Diff、接受、Apply 才会写盘。
* 不绕过 Runtime 写项目文件：写回仍然只走 ChangeSetService。
* Provider 返回空、超时或异常时 fail closed，不产生半成品 ChangeSet。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from zhimeng_runtime.change_set_models import ChangeSetCreateRequest
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.ai_smell_service import AiSmellError, AiSmellService
from zhimeng_runtime.knowledge_context_service import (
    KnowledgeContextError,
    KnowledgeContextService,
)
from zhimeng_runtime.provider_settings_service import ProviderSettingsService
from zhimeng_runtime.project_reader import ProjectReader, ProjectReaderError

PROSE_SECTION = "05_正文内容"

# 组装写作上下文时读取的项目真值文件
TRUTH_GLOBS = (
    "01_项目定位/*.md",
    "02_世界观设定/*.md",
    "03_人物设定/*.md",
    "04_剧情大纲/*.md",
    "06_写作规范/*.md",
)

SYSTEM_PROMPT = (
    "你是一位资深中文长篇小说写手。你只输出小说正文，"
    "不输出任何解释、说明、markdown 代码块或写作建议。"
    "叙述克制具体，避免空洞抒情和 AI 腔：不要堆砌「不禁」「仿佛」「似乎」，"
    "不要每段都以人名开头，不要用总结性议论收尾。"
)


class ChapterDraftError(RuntimeError):
    """章节候选生成失败。"""

    code = "chapter_draft_failed"


class ChapterDraftUnavailable(ChapterDraftError):
    """未配置 Provider，无法生成候选。"""

    code = "chapter_draft_provider_unavailable"


@dataclass(frozen=True, slots=True)
class ChapterDraftResult:
    change_set_id: str
    relative_path: str
    chapter_number: int
    characters: int
    model: str
    # 这一章有没有按作者的控制卡来写（没有控制卡时为 None）
    control_card_path: str | None = None
    # AI 味评分（检测器不可用或失败时为 None，不阻断出稿）
    ai_smell: dict | None = None
    # 四套 Skills 装配的知识上下文与严格门禁状态（未建档时为 None）
    knowledge: dict | None = None


@dataclass(frozen=True, slots=True)
class PassageRewriteResult:
    change_set_id: str
    relative_path: str
    original_characters: int
    rewritten_characters: int
    model: str
    ai_smell: dict | None = None


def _read_truth(project_root: Path, limit_chars: int = 6000) -> str:
    blocks: list[str] = []
    for pattern in TRUTH_GLOBS:
        for path in sorted(project_root.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if text:
                blocks.append(f"### {path.relative_to(project_root).as_posix()}\n{text}")
    return "\n\n".join(blocks)[:limit_chars]


def _prose_files(project_root: Path) -> list[Path]:
    root = project_root / PROSE_SECTION
    if not root.is_dir():
        return []
    found = [
        path
        for path in root.rglob("*.md")
        if not any(marker in path.stem for marker in ("控制卡", "续写卡", "章纲", "细纲"))
        and any(character.isdigit() for character in path.stem)
    ]
    return sorted(found)


def _leading_number(stem: str) -> int | None:
    digits = ""
    for character in stem:
        if character.isdigit():
            digits += character
        elif digits:
            break
    return int(digits) if digits else None


def find_control_card(project_root: Path, chapter_number: int) -> Path | None:
    """找出这一章的控制卡（novel-creation-suite 的章节控制卡约定）。

    作者的章纲通常放在 `05_正文内容/**/章纲/第NNN章_标题_控制卡.md`。
    控制卡里写着本章的 obligationContract：必须写到什么、必须不碰什么、
    声望/进度的允许区间。这是作者对这一章最精确的意图，优先级高于大纲。
    """
    root = project_root / PROSE_SECTION
    if not root.is_dir():
        return None
    for path in sorted(root.rglob("*.md")):
        if "控制卡" not in path.stem:
            continue
        if _leading_number(path.stem.lstrip("第")) == chapter_number:
            return path
    return None


def _title_from_control_card(card: Path) -> str | None:
    """从控制卡文件名里取出作者定的章节标题。

    形如 `第005章_厨子的药膳与账房的符_控制卡` -> `厨子的药膳与账房的符`
    """
    stem = card.stem
    if stem.endswith("_控制卡"):
        stem = stem[: -len("_控制卡")]
    parts = stem.split("_", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


def _next_chapter(project_root: Path) -> tuple[str, int, Path | None]:
    chapters = _prose_files(project_root)
    numbers = [n for n in (_leading_number(p.stem) for p in chapters) if n is not None]
    number = (max(numbers) + 1) if numbers else 1

    if chapters:
        parent = chapters[-1].parent.relative_to(project_root).as_posix()
    else:
        parent = f"{PROSE_SECTION}/第一卷"

    card = find_control_card(project_root, number)
    title = _title_from_control_card(card) if card is not None else None
    # 有控制卡就沿用作者已经定好的章节名，不要另起一个"待定标题"。
    filename = f"第{number:03d}章_{title}.md" if title else f"第{number:03d}章_待定标题.md"
    return f"{parent}/{filename}", number, card


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class ChapterDraftService:
    """调用已配置的 Provider 写一章，产出待审阅 ChangeSet。"""

    def __init__(
        self,
        change_set_service: ChangeSetService,
        *,
        provider_settings: ProviderSettingsService,
        project_reader: ProjectReader,
        ai_smell: AiSmellService | None = None,
        knowledge: KnowledgeContextService | None = None,
        timeout_seconds: float = 300.0,
        max_tokens: int = 8000,
        temperature: float = 0.8,
    ) -> None:
        self._change_sets = change_set_service
        # 每次都现读设置：作者在界面里改完模型，下一次写作立即生效，不用重启。
        self._provider_settings = provider_settings
        self._project_reader = project_reader
        self._ai_smell = ai_smell
        self._knowledge = knowledge
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature

    @property
    def available(self) -> bool:
        return self._provider_settings.effective().configured

    def _post_chat(self, current, payload: dict) -> dict:
        request = urllib.request.Request(
            str(current.base_url).rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {current.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.load(response)

    def _call_provider(self, system: str, user: str) -> str:
        current = self._provider_settings.effective()
        payload = {
            "model": current.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        try:
            try:
                body = self._post_chat(current, payload)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                # 有些新模型不再接受 temperature（返回 400 并点名这个参数）。
                # 去掉它重试一次，而不是让作者对着 502 发呆。
                if exc.code == 400 and "temperature" in detail:
                    payload.pop("temperature", None)
                    body = self._post_chat(current, payload)
                else:
                    raise ChapterDraftError(f"模型返回错误 {exc.code}: {detail}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ChapterDraftError(f"模型返回错误 {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChapterDraftError(f"连接模型失败: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ChapterDraftError("模型返回的不是合法 JSON") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChapterDraftError("模型返回结构异常，缺少正文内容") from exc
        if not isinstance(content, str) or not content.strip():
            raise ChapterDraftError("模型返回了空正文")
        return content

    def rewrite_passage(
        self,
        *,
        project_id: str,
        project_root: Path,
        relative_path: str,
        file_id: str,
        passage: str,
        instruction: str,
        base_revision: int,
        base_fingerprint: str,
    ) -> "PassageRewriteResult":
        """按作者的要求重写正文里选中的一段。

        fail closed 的地方：
        * 选中的原文在文件里找不到，或出现多次 → 拒绝，不猜。
          （出现多次时替换哪一处是歧义，猜错会改坏别的段落。）
        * 模型返回空 → 拒绝。
        产出同样是**待审阅 ChangeSet**，作者不点接受就不写盘。
        """
        if not self.available:
            raise ChapterDraftUnavailable(
                "还没有配置写作模型。请点右上角的设置，填写接口地址和密钥，"
                "再点「获取模型」选一个模型。"
            )
        if not passage.strip():
            raise ChapterDraftError("没有选中要重写的内容。")

        # 必须复用 ProjectReader 的受信快照读取：它会拒绝绝对路径、`..` 逃逸、
        # symlink/junction/reparse/hardlink，并校验 file_id、文件大小和 SHA-256。
        # 不能直接拼接请求里的 relative_path，否则项目外文本可能被发送给 Provider。
        try:
            snapshot = self._project_reader.inspect(project_root)
            project = snapshot.get("project")
            if not isinstance(project, dict) or project.get("project_id") != project_id:
                raise ChapterDraftError("作品身份已变化，请刷新后重新选择要重写的段落。")
            current_revision = project.get("revision")
            current_fingerprint = project.get("fingerprint")
            if current_revision != base_revision or current_fingerprint != base_fingerprint:
                raise ChapterDraftError("作品内容已变化，请刷新后重新选择要重写的段落。")
            file_payload = self._project_reader.read_text_file_from_snapshot(
                snapshot,
                file_id,
                allowed_kinds=("prose",),
            )
        except ProjectReaderError as exc:
            raise ChapterDraftError(f"不能安全读取这个章节文件：{relative_path}") from exc

        file_record = file_payload.get("file")
        if not isinstance(file_record, dict) or file_record.get("relative_path") != relative_path:
            raise ChapterDraftError("章节路径与文件身份对不上，请刷新后重新选择。")
        if file_record.get("kind") != "prose":
            raise ChapterDraftError("只能重写章节正文文件。")
        original_value = file_payload.get("content")
        if not isinstance(original_value, str):
            raise ChapterDraftError(f"读不到这个章节文件：{relative_path}")
        original = original_value

        occurrences = original.count(passage)
        if occurrences == 0:
            raise ChapterDraftError(
                "选中的原文和磁盘上的正文对不上。可能是文件已被改动，请刷新后重新选择。"
            )
        if occurrences > 1:
            raise ChapterDraftError(
                f"选中的这段在本章出现了 {occurrences} 次，无法确定改哪一处。"
                "请多选一些上下文，让它在本章唯一。"
            )

        model_name = str(self._provider_settings.effective().model)
        truth = _read_truth(project_root, limit_chars=3000)

        # 给足前后文，模型才知道这一段在承接什么、要引向哪
        index = original.index(passage)
        before = original[max(0, index - 800) : index]
        after = original[index + len(passage) : index + len(passage) + 800]

        system = (
            "你是一位资深中文小说编辑兼写手。你只输出改写后的那一段正文，"
            "不输出解释、不加标题、不用 markdown 代码块、不重复前后文。"
            "保持与原文一致的人称、时态、语气和称谓。"
        )
        user = "\n".join(
            [
                "以下是这本书的设定与文风规则，改写时必须遵守：",
                "",
                truth if truth else "（暂无补充资料）",
                "",
                "=" * 50,
                "【这一段的前文】",
                before if before.strip() else "（本段位于章节开头）",
                "",
                "【需要重写的段落】",
                passage,
                "",
                "【这一段的后文】",
                after if after.strip() else "（本段位于章节结尾）",
                "=" * 50,
                "",
                f"作者的要求：{instruction.strip()}",
                "",
                "请输出重写后的这一段。要求：",
                "1. 只输出这一段，不要带前文和后文。",
                "2. 必须能和前后文自然衔接。",
                "3. 严格执行作者的要求，其余部分不要擅自发挥。",
            ]
        )

        rewritten = _strip_fence(self._call_provider(system, user))

        ai_smell_payload: dict | None = None
        if self._ai_smell is not None and self._ai_smell.available:
            try:
                ai_smell_payload = self._ai_smell.evaluate_text(rewritten).as_dict()
            except AiSmellError:
                ai_smell_payload = None

        record = self._change_sets.create(
            project_id,
            ChangeSetCreateRequest.model_validate(
                {
                    "base_revision": base_revision,
                    "base_fingerprint": base_fingerprint,
                    "title": "重写选中段落",
                    "summary": (
                        f"按作者要求重写：{instruction.strip()[:40]}"
                        f"（{model_name}）"
                        + (
                            f"；AI 味 {ai_smell_payload['score']:.0f} 分"
                            f"（{ai_smell_payload['level']}）"
                            if ai_smell_payload
                            else ""
                        )
                    ),
                    "origin": "manual",
                    "gate_required": False,
                    "changes": [
                        {
                            "operation": "update",
                            "file_id": file_id,
                            "after_content": original.replace(passage, rewritten, 1),
                            "rationale": f"作者要求重写选中段落：{instruction.strip()[:200]}",
                        }
                    ],
                }
            ),
        )

        return PassageRewriteResult(
            change_set_id=record.change_set_id,
            relative_path=relative_path,
            original_characters=len(passage),
            rewritten_characters=len(rewritten),
            model=model_name,
            ai_smell=ai_smell_payload,
        )

    def draft_next_chapter(
        self,
        *,
        project_id: str,
        project_root: Path,
        base_revision: int,
        base_fingerprint: str,
        target_characters: int = 2200,
        instruction: str | None = None,
    ) -> ChapterDraftResult:
        if not self.available:
            raise ChapterDraftUnavailable(
                "还没有配置写作模型。请点右上角的设置，填写接口地址和密钥，"
                "再点「获取模型」选一个模型。"
            )
        model_name = str(self._provider_settings.effective().model)

        truth = _read_truth(project_root)
        chapters = _prose_files(project_root)
        previous = ""
        previous_name = ""
        if chapters:
            previous_name = chapters[-1].name
            try:
                previous = chapters[-1].read_text(encoding="utf-8").strip()[-1800:]
            except (OSError, UnicodeDecodeError):
                previous = ""

        relative_path, chapter_number, control_card = _next_chapter(project_root)

        card_text = ""
        if control_card is not None:
            try:
                card_text = control_card.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                card_text = ""

        parts = [
            "以下是这本书的项目真值资料，请严格遵守其中的世界观、人物设定与文风规则。",
            "",
            truth if truth else "（暂无补充资料）",
            "",
        ]
        if previous:
            parts += [
                f"以下是上一章（{previous_name}）的结尾，请自然承接，不要重复已写过的内容：",
                "",
                previous,
                "",
            ]

        if card_text:
            # 控制卡是作者对本章最精确的意图，优先级高于大纲与自由发挥。
            parts += [
                "=" * 56,
                "【本章控制卡 —— 优先级最高，逐条照做】",
                "下面是作者为这一章写好的控制卡。其中的约定必须严格执行：",
                "· mustHitNow / requiredPayoffTouches / requiredCharacterAppearances：本章必须真正写到，不能只提一句带过。",
                "· mustPreserve / forbiddenCrossings：绝对不能越界，哪怕写起来更精彩也不行。",
                "· requiredGoalChanges：人物状态必须发生这些变化。",
                "· successRange：数值必须落在给定区间内。",
                "如果控制卡与前面的大纲资料冲突，以控制卡为准。",
                "=" * 56,
                "",
                card_text,
                "",
                "=" * 56,
                "",
            ]

        # 四套 Skills 的知识装配：角色真相矩阵 / 伏笔生命周期 / 信息边界图等。
        # 建档缺失或装配失败都只是跳过增强，绝不阻断出稿。
        knowledge_payload: dict | None = None
        if self._knowledge is not None and self._knowledge.available:
            kb_project = self._knowledge.find_project_for_root(project_root)
            if kb_project:
                try:
                    knowledge = self._knowledge.assemble(
                        project=kb_project,
                        chapter_id=f"CH{chapter_number:03d}",
                    )
                    knowledge_payload = knowledge.as_dict()
                    # 只在真有可读指导时才注入。空的 / 只剩 ID 和诊断的情况
                    # 一律不进提示词——实测灌 JSON 会把 AI 味从 low 拉到 medium。
                    if knowledge.guidance:
                        parts += [
                            "【知识库补充（可参考，不必逐条照搬）】",
                            "",
                            knowledge.guidance[:2500],
                            "",
                        ]
                except KnowledgeContextError:
                    knowledge_payload = None

        if instruction:
            parts += [f"作者对本章的额外要求：{instruction}", ""]

        parts += [
            f"请续写第 {chapter_number} 章正文，约 {target_characters} 字。",
            "要求：",
            "1. 第一行是章节标题，格式为「# 第X章 标题」。",
            "2. 要有具体的场景、动作和对话，推进剧情，不要原地打转。",
            "3. 结尾留一个让读者想继续看下去的钩子。",
            "4. 只输出正文本身。",
        ]
        if card_text:
            parts.append(
                "5. 写完前自查一遍控制卡：mustHitNow 是否条条落地、"
                "forbiddenCrossings 是否一条都没碰。"
            )

        prose = _strip_fence(self._call_provider(SYSTEM_PROMPT, "\n".join(parts)))

        # 打 AI 味分。检测失败不能挡住出稿——作者宁可先拿到稿子。
        ai_smell_payload: dict | None = None
        if self._ai_smell is not None and self._ai_smell.available:
            try:
                ai_smell_payload = self._ai_smell.evaluate_text(prose).as_dict()
            except AiSmellError:
                ai_smell_payload = None

        # 建成待审阅 ChangeSet：所有 hunk 为 pending，作者不点 Apply 就不会写盘。
        record = self._change_sets.create(
            project_id,
            ChangeSetCreateRequest.model_validate(
                {
                    "base_revision": base_revision,
                    "base_fingerprint": base_fingerprint,
                    "title": f"第 {chapter_number} 章正文候选",
                    "summary": (
                        f"由 {model_name} 生成，等待作者审阅"
                        + ("；已按本章控制卡写作" if card_text else "；本章没有控制卡")
                        + (
                            f"；AI 味 {ai_smell_payload['score']:.0f} 分"
                            f"（{ai_smell_payload['level']}）"
                            if ai_smell_payload
                            else ""
                        )
                    ),
                    "origin": "manual",
                    "gate_required": False,
                    "changes": [
                        {
                            "operation": "create",
                            "relative_path": relative_path,
                            "after_content": prose + "\n",
                            "rationale": f"写作模型 {model_name} 生成的章节候选，未经作者批准不写回",
                        }
                    ],
                }
            ),
        )

        return ChapterDraftResult(
            change_set_id=record.change_set_id,
            relative_path=relative_path,
            chapter_number=chapter_number,
            characters=len(prose),
            model=model_name,
            ai_smell=ai_smell_payload,
            knowledge=knowledge_payload,
            control_card_path=(
                control_card.relative_to(project_root).as_posix()
                if control_card is not None and card_text
                else None
            ),
        )
