"""每个对话开头的一小段环境信息：系统、用户主目录、当前文件夹、今天、挡位。只写事实，一行一条，不写要求。

纠偏指令 3.2：以前模型不知道自己在哪台电脑上、今天几号——问「今天几号」只能上网搜（web_probe 第 9 题）。
别家都给：Claude Code 的环境块（工作目录、平台、系统版本、今天日期）、Codex 每个对话一段
<environment_context>、Gemini CLI「Today's date is …」、OpenCode「Today's date: …」。

一场对话里这一段不变（日期、挡位取这场对话第一轮的），放在最前面；跨天了或换了挡位，
在末尾追加一条新的，前面的不改——前缀不变，上游的提示缓存才接得上。
9-20「不要内部提示词了」管的是被弱模型原样念出来的要求和把话题拽去写小说的说明，这段两样都不沾。
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_WEEKDAYS = "一二三四五六日"
FULL_ACCESS_MODE = "author_review_change_proposal"


def home_folder() -> Path:
    """用户主目录：环境信息里的那一行，也是没打开文件夹时相对路径和命令的起点（纠偏指令 3.5）。"""

    return Path.home()


def system_description() -> str:
    if sys.platform == "win32":
        # Python's platform.uname() queries WMI on Windows. A stalled WMI
        # service would block the Agent event loop before its first request.
        # GetVersionEx is a local OS call and does not depend on that service.
        try:
            windows = sys.getwindowsversion()
        except OSError:
            return "Windows"
        major, minor, build = windows.platform_version
        version = f"{major}.{minor}.{build}"
        if windows.product_type in {2, 3}:
            name = "Windows Server"
        elif major == 10:
            name = "Windows 11" if build >= 22000 else "Windows 10"
        else:
            name = "Windows"
        return f"{name}（{version}）"
    system = platform.system() or os.name
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    return f"{system} {platform.release()}".strip()


def date_text(moment: datetime) -> str:
    local = moment.astimezone()
    offset = local.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{local:%Y-%m-%d} 星期{_WEEKDAYS[local.weekday()]}（UTC{sign}{hours:02d}:{rest:02d}）"


def tier_text(writer_mode: str | None) -> str:
    return "完全访问" if writer_mode == FULL_ACCESS_MODE else "只读"


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    folder: str | None
    started_at: datetime
    tier: str


def opening_message(facts: EnvironmentFacts, *, home: Path) -> dict[str, object]:
    lines = [
        f"系统：{system_description()}",
        f"用户主目录：{home}",
        f"当前文件夹：{facts.folder or '未打开'}",
        f"今天：{date_text(facts.started_at)}",
        f"挡位：{facts.tier}",
    ]
    return {"role": "system", "content": "\n".join(lines)}


def update_message(opening: EnvironmentFacts, *, now: datetime, tier: str) -> dict[str, object] | None:
    lines: list[str] = []
    if now.astimezone().date() != opening.started_at.astimezone().date():
        lines.append(f"今天：{date_text(now)}")
    if tier != opening.tier:
        lines.append(f"挡位：{tier}")
    if not lines:
        return None
    return {"role": "system", "content": "\n".join(lines)}


_UPDATE_KEYS = ("今天", "挡位")


def is_update_content(text: str) -> bool:
    """这条是不是末尾补的那种环境变化（每行都是「今天：」或「挡位：」）。开头那段有别的行，不算。"""

    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(line.partition("：")[0] in _UPDATE_KEYS and "：" in line for line in lines)


def update_since(
    opening: EnvironmentFacts, *, now: datetime, tier: str, stated: Sequence[str] = ()
) -> dict[str, object] | None:
    """这一轮末尾要不要补一条：和模型已经知道的比——开头那段，加上历史里后来补过的几条——变了才说，说现在的。

    9-28 起下一轮照原样带上上一轮的往来（ZCode 档案 12.2），上一轮补过的那条也在历史里；还按「和开头比」算，
    跨天以后每轮都会再补一条一样的。没补过的时候（``stated`` 为空）和 ``update_message`` 一样。
    """

    known = {"今天": date_text(opening.started_at), "挡位": opening.tier}
    for text in stated:
        for line in text.splitlines():
            key, sep, value = line.partition("：")
            if sep and key in known:
                known[key] = value.strip()
    lines: list[str] = []
    today = date_text(now)
    # 只比日期（后面的星期、时区跟着日期走）。
    if today[:10] != known["今天"][:10]:
        lines.append(f"今天：{today}")
    if tier != known["挡位"]:
        lines.append(f"挡位：{tier}")
    if not lines:
        return None
    return {"role": "system", "content": "\n".join(lines)}
