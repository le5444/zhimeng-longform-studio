"""Hashline：让编辑带上「读到过」的证据。

织梦版的最小可行做法（对照 oh-my-pi 的 hashline，但按织梦的工具形态改）：

- ``read_file`` 给每行打 `行号#HASH:` 的指纹，并把整份文件的 `file_tag` 一起给模型。
- ``edit_file`` / ``write_file`` 校验「模型给的内容是不是它最近读到过的」：
  - `edit_file` 的 `find` 必须在最近一次 `read_file` 返回的内容里出现过；
  - `write_file` 整份覆盖时，文件如果模型没读过且内容少了三成以上，拒绝并提示先读。
- 新建文件（`existing is None`）不查指纹。

账本只记「这一 run 里每个 path 最近一次读到的指纹 + 行集合」，内存版、不跨进程持久化。
会话恢复后从「没读过」重新开始（和 oh-my-pi / DSH 的 fs-observation-policy 一致）。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field


def file_tag(text: str) -> str:
    """整份文件的内容指纹：每行剥行尾空格/tab/CR，行之间以 \n 分隔，crc32 取低 16 位，4 位大写十六进制。

    剥行尾的空格/tab/CR 是为了 CRLF/LF 和行尾空白差异不会让 tag 变（同 oh-my-pi 的归一化）。
    """
    hasher = 0
    for line in text.splitlines():
        kept = line.rstrip(" \t\r")
        hasher = zlib.crc32(kept.encode("utf-8"), hasher)
        hasher = zlib.crc32(b"\n", hasher)
    return f"{hasher & 0xFFFF:04X}"


def line_tags(lines: list[str]) -> dict[int, str]:
    """每一行的指纹（1-based 行号 -> 4 位 HEX）。"""
    tags: dict[int, str] = {}
    for index, line in enumerate(lines, start=1):
        kept = line.rstrip(" \t\r\n")
        tags[index] = f"{zlib.crc32(kept.encode('utf-8')) & 0xFFFF:04X}"
    return tags


def tag_content(lines: list[str], line_start: int) -> str:
    """把内容加上 `行号#HASH:` 前缀，模型照着 `行号#HASH` 复述要改的行。"""
    out: list[str] = []
    for offset, line in enumerate(lines):
        kept = line.rstrip(" \t\r\n")
        tag = f"{zlib.crc32(kept.encode('utf-8')) & 0xFFFF:04X}"
        out.append(f"{line_start + offset}#{tag}:{line}")
    return "".join(out)


@dataclass
class ReadRecord:
    """一次 read_file 留下的记录：路径 -> 读到的行(1-based)的指纹。"""

    file_tag: str
    line_tags: dict[int, str]
    excerpt_lines: set[int] = field(default_factory=set)


class ReadLedger:
    """按 run_id 记这一轮每个 path 最近一次读到的记录。内存版，不持久化。"""

    def __init__(self) -> None:
        self._by_run: dict[str, dict[str, ReadRecord]] = {}

    def record(self, run_id: str, path: str, record: ReadRecord) -> None:
        self._by_run.setdefault(run_id, {})[path] = record

    def last_read(self, run_id: str, path: str) -> ReadRecord | None:
        return self._by_run.get(run_id, {}).get(path)

    def has_read(self, run_id: str, path: str) -> bool:
        return (run_id in self._by_run) and (path in self._by_run[run_id])

    def clear_run(self, run_id: str) -> None:
        self._by_run.pop(run_id, None)

    # ------------------------------------------------------------------
    # 编辑前观察（照 YAgent 的 file-observation-gate）：
    # 读过之后文件没变 -> 放行；读过之后文件变了 -> 拒绝 + 附现在的指纹让模型重读。

    def evaluate(self, run_id: str, path: str, current_revision: str) -> tuple[bool, str | None]:
        """决定一次写/改放不放。

        返回 (allowed, stale_message|None)。
        - 这一 run 没读过这个文件：放行（新建的、或者模型没读到的，按 YAgent 的规则不拦）。
        - 读过、当前指纹和读的时候一样：放行。
        - 读过、对不上：拒绝，附「文件变了，重读」的消息。
        """
        rec = self.last_read(run_id, path)
        if rec is None:
            return True, None
        if current_revision == rec.file_tag:
            return True, None
        msg = (
            f"文件 {path} 在你读它之后又被改了（读时指纹 {rec.file_tag}，现在 {current_revision}）。"
            " 这次修改没落盘是因为你是按旧内容写的；先 read_file 重读，"
            "再把要改的地方对着现在的内容写一遍。如果这是别处正改这个文件，保留对方的改动，别盖掉。"
        )
        return False, msg


LEDGER = ReadLedger()
