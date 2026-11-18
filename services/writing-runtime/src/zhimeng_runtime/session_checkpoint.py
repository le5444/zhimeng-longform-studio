"""会话对话摘要（Session Checkpoint）：把较早的对话压成一份结构化的中文摘要。

为什么需要：Context Pack 留给对话历史的预算很小（默认只有总预算的四分之一，
约 12K 字 / 6K token），聊过几轮之后更早的对话就会整段掉出窗口，模型再也不知道
作者之前定过什么、否掉过什么、哪些做法已经试过不行。摘要把这些内容压成一块固定
结构的中文放进系统上下文，最近几轮对话仍然原样保留。

边界：
- 摘要不是作品事实。注入时明确写明：作品内容与作者原话优先于摘要。
- 摘要由模型生成，落库后逐字节不再变化；Context Pack 只引用已落库的那份，
  因此同一份 Pack 的重新校验结果稳定。
- 生成失败只记事件、不影响已经成功的 Run。

本模块只放纯函数：怎么向模型描述整理任务、怎么清洗模型交回来的摘要。存取
（agent_store）、触发（agent_service）与注入（context_models / context_service）
各在各处。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

SESSION_CHECKPOINT_SCHEMA = "zhimeng.session-checkpoint.v1"

# 触发阈值、原样留下的最近几轮、摘要上限都按「上下文窗口档位」从 model_context 的表里取
# （256K 档：60,000 字触发 / 摘要 8,000 字 / 留最近 15,000 字；1M 档 ×4）。本模块只留一个兼容别名。
from zhimeng_runtime.model_context import CONTEXT_WINDOW_SPECS

DEFAULT_CHECKPOINT_MAX_CHARACTERS = CONTEXT_WINDOW_SPECS["256k"].checkpoint_max_characters
# 整理时每条原始消息最多喂给模型的字数，防止一段超长正文把整理请求撑爆。
CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS = 4_000

CHECKPOINT_SECTION_TITLES: tuple[str, ...] = (
    "当前目标",
    "作者的要求与底线",
    "已经定下的事",
    "已经做完的事",
    "试过但不行的做法",
    "重要的名字、文件与章节",
    "还没做、接下来要做的事",
)
# 摘要超长时最后才截的三节：以前从末尾截，第一个丢的就是「还没做、接下来要做的事」。
CHECKPOINT_PROTECTED_SECTIONS: tuple[str, ...] = ("当前目标", "试过但不行的做法", "还没做、接下来要做的事")
# 程序附在模型摘要后面的两段（compaction_facts.py 拼）：下一次整理时只把模型写的那部分交回模型合并，这两段每次按库里的记录重拼。
CHECKPOINT_FACTS_TITLE = "【程序记下的事实】（系统按修改记录和工具记录整理的，不是模型写的）"
CHECKPOINT_AUTHOR_WORDS_TITLE = "【作者的原话】（折起来的这段里作者说的话，照原样；长的只留开头和结尾）"

_ROLE_LABELS = {"user": "作者", "assistant": "写作助手"}
_TRUNCATED_SUFFIX = "\n（摘要过长，末尾已截断）"
_SECTION_TRIMMED_SUFFIX = "\n（这一节太长，后面的省略了）"
# 一节最少留这么多字再截别的。
_SECTION_MIN_CHARACTERS = 160
_BLANK_LINES = re.compile(r"\n{3,}")


def checkpoint_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _checkpoint_instruction(max_characters: int) -> str:
    sections = "\n".join(
        f"【{title}】{hint}"
        for title, hint in zip(
            CHECKPOINT_SECTION_TITLES,
            (
                "作者现在想做成什么。",
                "作者明确提出的要求、偏好、禁止事项，尽量保留作者原话里的关键词。",
                "双方已经确认的决定与设定。",
                "助手已经完成并得到作者认可的工作。",
                "尝试过但失败、被作者否掉或证明无效的做法。这一节最重要，绝不能漏。",
                "人物名、地名、物件名、文件名、章节号等需要精确记住的名字。",
                "待办事项与下一步。",
            ),
            strict=True,
        )
    )
    return (
        "你是 Agent 的「对话记录整理员」。下面是用户与 Agent 之间较早的一段"
        "对话，之后它们会从模型的上下文里移出。请把这段对话里以后还用得上的信息"
        "整理成一份简体中文摘要，供 Agent 在后续对话中参考。\n\n"
        "严格按下面七个小节输出，小节标题原样保留，每个小节用短句或条目；"
        "没有内容的小节写「无」：\n"
        f"{sections}\n\n"
        "规则：只根据对话内容整理，不要编造，不要补充对话里没有的信息；不要评价，"
        "不要给建议；准确保留原有文件名、标识符、术语和用户原话；"
        "区分用户确认的事实和助手未经核实的完成声明，不把助手推测升级成事实；"
        "作者的原话和改过的文件清单，系统会另外原样附在摘要后面，摘要里写清结论就够了；"
        f"总长度不超过 {max_characters} 字。如果给了「上一份摘要」，请把它与这段"
        "新对话合并成一份完整摘要，不要丢掉上一份里仍然有效的内容。"
    )


def _source_message_text(role: str, content: str) -> str:
    label = _ROLE_LABELS.get(role, role)
    text = content.strip()
    if len(text) > CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS:
        head = CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS * 3 // 4
        tail = CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS - head
        omitted = len(text) - head - tail
        text = f"{text[:head]}\n……（中间省略约 {omitted} 字）……\n{text[-tail:]}"
    return f"{label}：{text}"


def build_checkpoint_request_messages(
    *,
    previous_checkpoint: str | None,
    messages: Sequence[tuple[str, str]],
    max_characters: int = DEFAULT_CHECKPOINT_MAX_CHARACTERS,
    retry_missing: Sequence[str] = (),
) -> list[dict[str, str]]:
    """构造发给模型的整理请求：一条系统说明 + 一条包含上一份摘要与待整理对话的用户消息。

    ``messages`` 是按时间顺序排列的 ``(role, content)``，只接受 user / assistant。
    ``previous_checkpoint`` 只交模型写的那部分（``model_summary_part``）：程序附的事实和原话每次按库里的记录重拼。
    ``retry_missing``：上一次交回来的摘要缺了哪几节，这次在末尾说一声（照 OpenClaw 的摘要闸门，只重来一次）。
    """
    if isinstance(max_characters, bool) or max_characters < 1:
        raise ValueError("max_characters must be a positive integer")
    if not messages:
        raise ValueError("a checkpoint needs at least one message to fold")
    for role, _content in messages:
        if role not in _ROLE_LABELS:
            raise ValueError(f"unsupported checkpoint message role: {role}")
    previous = (previous_checkpoint or "").strip() or "无"
    transcript = "\n\n".join(
        _source_message_text(role, content) for role, content in messages
    )
    retry_note = ""
    if retry_missing:
        missing = "、".join(f"【{title}】" for title in retry_missing)
        retry_note = f"\n\n（上一次整理少了这几节：{missing}。这次七个小节都写上，没有内容的写「无」。）"
    return [
        {"role": "system", "content": _checkpoint_instruction(max_characters)},
        {
            "role": "user",
            "content": (
                f"【上一份摘要】\n{previous}\n\n【要整理的对话】\n{transcript}{retry_note}"
            ),
        },
    ]


def missing_checkpoint_sections(text: str) -> list[str]:
    """模型交回来的摘要里缺了哪几节（按标题找）。"""
    return [title for title in CHECKPOINT_SECTION_TITLES if f"【{title}】" not in text]


def model_summary_part(content: str) -> str:
    """一份落库摘要里模型写的那部分：程序附的「事实」「作者的原话」两段之前。"""
    cut = len(content)
    for title in (CHECKPOINT_FACTS_TITLE, CHECKPOINT_AUTHOR_WORDS_TITLE):
        index = content.find(title)
        if index >= 0:
            cut = min(cut, index)
    return content[:cut].strip()


def compose_checkpoint_content(summary: str, *blocks: str | None) -> str:
    """落库的摘要 = 模型写的七节 + 程序附的几段（没有的段不带）。"""
    return "\n\n".join(part.strip() for part in (summary, *blocks) if part and part.strip())


def _split_sections(text: str) -> tuple[str, list[list[str]]] | None:
    """按小节标题切开：(第一个标题前的部分, [[标题, 正文], ...])；一个标题都找不到返回 None。"""
    positions = sorted(
        (index, title)
        for title in CHECKPOINT_SECTION_TITLES
        if (index := text.find(f"【{title}】")) >= 0
    )
    if not positions:
        return None
    sections: list[list[str]] = []
    for number, (index, title) in enumerate(positions):
        start = index + len(title) + 2
        end = positions[number + 1][0] if number + 1 < len(positions) else len(text)
        sections.append([title, text[start:end]])
    return text[: positions[0][0]], sections


def _join_sections(preamble: str, sections: list[list[str]]) -> str:
    joined = "".join(f"【{title}】{body.strip()}\n" for title, body in sections)
    return (preamble.strip() + "\n" + joined if preamble.strip() else joined).strip()


def _trim_section_body(body: str, keep: int) -> str:
    """一节正文留前 keep 字左右：尽量断在换行处，末尾写明截过。"""
    text = body.strip()
    if len(text) <= keep:
        return text
    cut = text.rfind("\n", 0, keep)
    if cut < keep // 2:
        cut = keep
    return text[:cut].rstrip() + _SECTION_TRIMMED_SUFFIX


def _fit_sections(text: str, max_characters: int) -> str:
    """超长的摘要按小节截：先截最长的那几节，当前目标 / 试过但不行的做法 / 接下来要做的事最后才动。

    以前一律从末尾截，截掉的头一个就是「还没做、接下来要做的事」（总指令 8.3 第 5 件）。
    """
    split = _split_sections(text)
    if split is not None:
        preamble, sections = split
        floor = _SECTION_MIN_CHARACTERS + len(_SECTION_TRIMMED_SUFFIX)
        for protected in (False, True):
            while True:
                joined = _join_sections(preamble, sections)
                if len(joined) <= max_characters:
                    return joined
                candidates = [
                    section for section in sections
                    if (section[0] in CHECKPOINT_PROTECTED_SECTIONS) is protected
                    and len(section[1].strip()) > floor
                ]
                if not candidates:
                    break
                longest = max(candidates, key=lambda section: len(section[1].strip()))
                excess = len(joined) - max_characters
                keep = max(
                    _SECTION_MIN_CHARACTERS,
                    len(longest[1].strip()) - excess - len(_SECTION_TRIMMED_SUFFIX),
                )
                longest[1] = _trim_section_body(longest[1], keep)
        text = _join_sections(preamble, sections)
    # 没有小节标题，或每节都截到最短还是超：只能从末尾截。
    keep = max(1, max_characters - len(_TRUNCATED_SUFFIX))
    return text[:keep].rstrip() + _TRUNCATED_SUFFIX


def normalize_checkpoint_text(
    text: str,
    *,
    max_characters: int = DEFAULT_CHECKPOINT_MAX_CHARACTERS,
) -> str | None:
    """清洗模型交回来的摘要：去首尾空白、压掉连续空行、超长则按小节截短并标明。

    空白摘要返回 ``None``，调用方据此放弃本次整理。
    """
    if isinstance(max_characters, bool) or max_characters < 1:
        raise ValueError("max_characters must be a positive integer")
    cleaned = _BLANK_LINES.sub("\n\n", text.replace("\r\n", "\n")).strip()
    if not cleaned:
        return None
    if len(cleaned) <= max_characters:
        return cleaned
    return _fit_sections(cleaned, max_characters)


def render_checkpoint_block(*, through_ordinal: int, content: str) -> str:
    """系统上下文里的摘要段落（人话版）。

    统一放在这里，Context Pack 的渲染与校验都调它，保证两边逐字节一致。
    """
    return (
        "【此前对话摘要】\n"
        f"这段摘要由系统整理自本次对话更早的部分（截至第 {through_ordinal} 条消息），"
        "之后的对话原样附在后面。摘要只是提醒：作品内容与作者的原话优先于摘要，"
        "两者冲突时以作品内容和作者原话为准。\n"
        f"{content}"
    )


__all__ = [
    "CHECKPOINT_AUTHOR_WORDS_TITLE",
    "CHECKPOINT_FACTS_TITLE",
    "CHECKPOINT_PROTECTED_SECTIONS",
    "CHECKPOINT_SECTION_TITLES",
    "CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS",
    "DEFAULT_CHECKPOINT_MAX_CHARACTERS",
    "SESSION_CHECKPOINT_SCHEMA",
    "build_checkpoint_request_messages",
    "checkpoint_content_hash",
    "compose_checkpoint_content",
    "missing_checkpoint_sections",
    "model_summary_part",
    "normalize_checkpoint_text",
    "render_checkpoint_block",
]
