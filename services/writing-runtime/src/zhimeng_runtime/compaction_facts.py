"""压缩时由程序附在模型摘要后面的两段：工具事实，和作者的原话。

为什么：模型写的摘要会漏、会记错。9-28 压缩考题 comp2 里，作者说「第 3 章开头想过倒叙、从验宗那天写起，否掉了，照顺叙写」，
摘要写成「第3章开头用顺叙写，从验宗那天开始」，把被否掉的那个写法当成了定下的。这两段直接从库里的记录来，不经模型：
- 工具事实：改过的文件（按修改记录的最终状态：存了 / 还在等保存 / 没存进去 / 后来撤回了）、打开的文件夹以外改过的文件、
  执行过的命令、记进记忆的话、最近一次的计划、读过的技能。照 Droid（摘要后由程序附改过的文件清单 + 最新计划 + 读过的技能）。
- 作者的原话：折起来那段里作者说的每一句，照原样，长的（多半是贴进来的原文）只留开头和结尾；新的优先，放不下的旧的说一声。
  照 Codex 压缩后原样保留作者的话（最多约 2 万 token）。

本模块只放纯函数。从库里取记录在 ``agent_store.compaction_source_records``，拼进摘要在 ``agent_service._fold_session_history``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from zhimeng_runtime.agent_tools import (
    COMMAND_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    PLAN_TOOL_NAMES,
    SKILL_FILE_TOOL_NAMES,
)
from zhimeng_runtime.session_checkpoint import (
    CHECKPOINT_AUTHOR_WORDS_TITLE,
    CHECKPOINT_FACTS_TITLE,
)

MAX_FILES = 40
MAX_COMMANDS = 10
MAX_MEMORY_NOTES = 10
MAX_PLAN_ITEMS = 20
MAX_SKILLS = 20
# 作者一句话超过这么多字就只留开头 300 字、结尾 100 字：这么长的多半是贴进来的原文，交代一般写在开头。
# （9-28 fix1：每条留 800 字时，贴进来的五批原文在「作者的原话」里占了四千多字。）
AUTHOR_MESSAGE_MAX_CHARACTERS = 800
AUTHOR_EXCERPT_HEAD_CHARACTERS = 300
AUTHOR_EXCERPT_TAIL_CHARACTERS = 100

_OPERATION_WORDS = {
    "create": "新建",
    "created": "新建",
    "update": "改",
    "updated": "改",
    "edit": "改",
    "write": "改",
    "delete": "删除",
    "deleted": "删除",
    "move": "挪走",
    "moved": "挪走",
    "rename": "改名",
    "mkdir": "新建文件夹",
    "make_folder": "新建文件夹",
}
# change_set.status 的全部取值（database.py 的 CHECK）。
_CHANGE_SET_NOTES = {
    "applied": "",
    "applying": "",
    "reviewing": "（提了修改卡，还没保存）",
    "ready": "（提了修改卡，还没保存）",
    "conflicted": "（没存进去：文件在这期间被改过）",
    "failed": "（没存进去）",
    "discarded": "（作者没要，没存）",
    "rolling_back": "（正在撤回）",
    "rolled_back": "（后来撤回了）",
}
_PLAN_STATUS_WORDS = {"completed": "已完成", "in_progress": "进行中", "pending": "没开始"}


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _one_line(text: object, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _succeeded(calls: Iterable[Any], names: Iterable[str]) -> list[Any]:
    wanted = set(names)
    return [
        call for call in calls
        if _field(call, "tool_name") in wanted and _field(call, "status") == "succeeded"
    ]


def _arguments(call: Any) -> Mapping[str, Any]:
    value = _field(call, "arguments")
    return value if isinstance(value, Mapping) else {}


def _squeeze(words: Sequence[str]) -> str:
    """「改、改、改」写成「改 ×3」。"""
    parts: list[str] = []
    for word in words:
        if parts and parts[-1].split(" ×")[0] == word:
            count = int(parts[-1].split(" ×")[1]) + 1 if " ×" in parts[-1] else 2
            parts[-1] = f"{word} ×{count}"
        else:
            parts.append(word)
    return "；".join(parts)


def _file_lines(change_sets: Sequence[Any], file_changes: Sequence[Any]) -> list[str]:
    files: dict[str, list[str]] = {}
    for change_set in change_sets:
        status = str(_field(change_set, "status") or "")
        note = _CHANGE_SET_NOTES.get(status, f"（{status}）" if status else "")
        for change in _field(change_set, "changes") or ():
            path = str(_field(change, "relative_path") or "").strip()
            if not path:
                continue
            operation = str(_field(change, "operation") or "")
            target = str(_field(change, "target_relative_path") or "").strip()
            word = f"挪到 {target}" if target and target != path else _OPERATION_WORDS.get(operation, operation or "改")
            files.setdefault(path, []).append(word + note)
    for change in file_changes:
        path = str(_field(change, "path") or "").strip()
        if not path:
            continue
        operation = str(_field(change, "operation") or "")
        target = str(_field(change, "target_path") or "").strip()
        word = f"挪到 {target}" if target and target != path else _OPERATION_WORDS.get(operation, operation or "改")
        note = "（后来撤回了）" if _field(change, "undone_at") else ""
        files.setdefault(path, []).append(word + note)
    lines = [f"- {path}：{_squeeze(words)}" for path, words in files.items()]
    if len(lines) > MAX_FILES:
        lines = lines[:MAX_FILES] + [f"- 还有 {len(lines) - MAX_FILES} 个文件没列出来"]
    return lines


def tool_facts_block(
    *,
    change_sets: Sequence[Any] = (),
    file_changes: Sequence[Any] = (),
    tool_calls: Sequence[Any] = (),
) -> str | None:
    """「程序记下的事实」一段；什么都没有就返回 None。

    ``change_sets`` 是修改记录（带最终状态和 changes[].relative_path），``file_changes`` 是打开的文件夹以外的写入，
    ``tool_calls`` 按时间顺序（只看成功的）。
    """
    sections: list[str] = []
    file_lines = _file_lines(change_sets, file_changes)
    if file_lines:
        sections.append("改过的文件：\n" + "\n".join(file_lines))

    commands = [
        _one_line(_arguments(call).get("command"), 120)
        for call in _succeeded(tool_calls, COMMAND_TOOL_NAMES)
        if _arguments(call).get("command")
    ]
    if commands:
        shown = commands[-MAX_COMMANDS:]
        head = f"执行过的命令（最近 {len(shown)} 条；命令改了哪些文件这里看不出，也撤不回）：" if len(commands) > len(shown) else "执行过的命令（命令改了哪些文件这里看不出，也撤不回）："
        sections.append(head + "\n" + "\n".join(f"- {command}" for command in shown))

    notes = []
    for call in _succeeded(tool_calls, MEMORY_TOOL_NAMES):
        arguments = _arguments(call)
        text = _one_line(arguments.get("text"), 160)
        if text:
            where = "项目记忆" if arguments.get("scope") == "project" else "用户记忆"
            notes.append(f"- （{where}）{text}")
    if notes:
        sections.append("记进记忆的：\n" + "\n".join(notes[-MAX_MEMORY_NOTES:]))

    for plan in reversed(_succeeded(tool_calls, PLAN_TOOL_NAMES)):
        output = _field(plan, "output")
        source = _arguments(plan) if output is None or output == {} else output
        items = source.get("items") if isinstance(source, Mapping) else None
        if not isinstance(items, list):
            continue
        plan_lines = [
            f"- [{_PLAN_STATUS_WORDS.get(str(_field(item, 'status') or ''), str(_field(item, 'status') or '') or '没开始')}] "
            f"{_one_line(_field(item, 'content'), 160)}"
            for item in list(items)[:MAX_PLAN_ITEMS]
            if _field(item, "content")
        ]
        if plan_lines:
            sections.append("Agent 最近记录的计划（状态由 Agent 标记，未经独立核验）：\n" + "\n".join(plan_lines))
            break

    skills: list[str] = []
    for call in _succeeded(tool_calls, SKILL_FILE_TOOL_NAMES):
        name = str(_arguments(call).get("skill") or "").strip()
        if name and name not in skills:
            skills.append(name)
    if skills:
        sections.append("读过的技能：" + "、".join(skills[:MAX_SKILLS]))

    if not sections:
        return None
    return CHECKPOINT_FACTS_TITLE + "\n" + "\n".join(sections)


def _author_excerpt(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= AUTHOR_MESSAGE_MAX_CHARACTERS:
        return stripped
    head = AUTHOR_EXCERPT_HEAD_CHARACTERS
    tail = AUTHOR_EXCERPT_TAIL_CHARACTERS
    return f"{stripped[:head]}……（这一条共 {len(stripped):,} 字，中间省略）……{stripped[-tail:]}"


def author_words_block(texts: Sequence[str], *, max_characters: int) -> str | None:
    """「作者的原话」一段：按时间排，新的优先放；放不下的旧的写一句有几条没放。没有就返回 None。"""
    items = [_author_excerpt(text) for text in texts if text and text.strip()]
    if not items:
        return None
    chosen: list[str] = []
    total = 0
    for item in reversed(items):
        if chosen and total + len(item) > max_characters:
            break
        chosen.append(item)
        total += len(item)
    chosen.reverse()
    lines = [CHECKPOINT_AUTHOR_WORDS_TITLE]
    dropped = len(items) - len(chosen)
    if dropped:
        lines.append(f"（更早的 {dropped} 条没放进来，要点在上面的摘要里。）")
    lines.extend("- " + item.replace("\n", "\n  ") for item in chosen)
    return "\n".join(lines)


__all__ = [
    "AUTHOR_MESSAGE_MAX_CHARACTERS",
    "author_words_block",
    "tool_facts_block",
]
