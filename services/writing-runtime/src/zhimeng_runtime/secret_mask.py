"""工具读回来的内容里，像密钥的值只留末 4 位再交给模型、写进对话库。

读文件、执行命令读到的东西会原样发给模型、存进对话库；作者的规矩是「密钥不进 git、日志、文档，敏感信息脱敏」
（吸收总指令草稿 §11）。9-26 真机：让 agent「读本机 Claude 和 Codex 的记忆」，它读了 `~/.claude/settings.json`，
把里面 `ANTHROPIC_AUTH_TOKEN` 的值原样写进了回答。文件本身一个字节都不动，只遮住交给模型的那一份；
遮了几处在工具结果里照实说（`masked_secrets`）。和各处报错只留「密钥后四位」是同一个做法。
"""

from __future__ import annotations

import re
from typing import Final

# 键名以这些词结尾（前面是开头或 _ . - 分隔）才算：OPENAI_API_KEY、ANTHROPIC_AUTH_TOKEN、client_secret、password……
# 「max_tokens」「tokenizer」「author」这种不算。驼峰写法（sessionToken、apiKey）另认一遍。
_SECRET_WORDS: Final = r"(?:api[_\-]?key|apikey|access[_\-]?key|secret[_\-]?key|secret|token|auth[_\-]?token|password|passwd|private[_\-]?key|credentials?)"
# 键名的每一段、段数都有上限，而且键名前面必须是开头或分隔符：几十万字的单行文件里也只在词头试一次，不会越试越慢
# （9-26 第一版写成 `[a-z][A-Za-z0-9]*…`，读一个超长单行文件时成了平方级，测试直接卡死）。
_KEY_NAME: Final = rf"(?:[A-Za-z0-9]{{1,32}}[_.\-]){{0,6}}{_SECRET_WORDS}|[a-z][A-Za-z0-9]{{0,40}}?(?:Token|ApiKey|Secret|Password)"
# 键 = 值 / "键": "值" / 键: 值（env、ini、yaml、json、toml 都是这几种写法）
_ASSIGNMENT: Final = re.compile(
    rf"""(?<![A-Za-z0-9_.\-])(?P<head>["']?(?P<key>{_KEY_NAME})["']?\s*[:=]\s*)(?P<quote>["']?)(?P<value>[^\s"',;{{}}\[\]]{{8,}})(?P=quote)""",
    re.IGNORECASE,
)
_BEARER: Final = re.compile(r"(?P<head>\bbearer\s+)(?P<value>[A-Za-z0-9._~+/\-]{12,}=*)", re.IGNORECASE)
# 认得出格式的密钥，出现在哪都遮：sk-…（OpenAI / Anthropic / 各家中转站）、GitHub、AWS、Slack、Google。
_KNOWN_FORMATS: Final = re.compile(
    r"\b(?P<value>sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{30,})"
)


def is_secret_name(name: str) -> bool:
    return name.lower() in {"authorization", "proxy-authorization", "cookie", "set-cookie"} or re.fullmatch(_KEY_NAME, name, re.IGNORECASE) is not None


def _hidden(value: str) -> str:
    return f"…（已遮住，末 4 位 {value[-4:]}）"


def mask_secrets(text: str) -> tuple[str, int]:
    """返回遮好的文字和遮了几处。纯数字（max_tokens: 4096 这类）不算密钥。"""

    if not text:
        return text, 0
    count = 0

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group("value")
        if value.isdigit() or value.lower() in {"true", "false", "null", "none", "undefined"}:
            return match.group(0)
        if value.startswith("…（已遮住"):
            return match.group(0)
        count += 1
        quote = match.group("quote")
        return f"{match.group('head')}{quote}{_hidden(value)}{quote}"

    def plain(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group("value")
        count += 1
        head = match.groupdict().get("head") or ""
        return f"{head}{_hidden(value)}"

    masked = _ASSIGNMENT.sub(assignment, text)
    masked = _BEARER.sub(plain, masked)
    masked = _KNOWN_FORMATS.sub(plain, masked)
    return masked, count
