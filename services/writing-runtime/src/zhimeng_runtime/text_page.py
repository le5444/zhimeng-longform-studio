"""One cursor calculation for project, local and skill text reads."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextPage:
    content: str
    start_line: int
    end_line: int
    total_lines: int
    next_offset: int | None
    next_start_line: int | None
    note: str | None = None

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None


def read_text_page(text: str, *, start_line: int, max_lines: int,
                   start_offset: int = 0, max_characters: int) -> TextPage:
    """Offsets address normalized text; line metadata describes returned text.

    A page ending inside a physical line continues by offset, never by skipping
    that line. A page ending at a newline can also continue by line number.
    """
    lines = text.splitlines(keepends=True)
    note = None
    if start_offset:
        offset = min(start_offset, len(text))
        first = text.count("\n", 0, offset) + 1
        selected = text[offset:].splitlines(keepends=True)[:max_lines]
    else:
        index = min(start_line - 1, len(lines))
        if lines and start_line > len(lines):
            index = max(0, len(lines) - max_lines)
            note = (f"文件只有 {len(lines)} 行，第 {start_line} 行之后没有内容；"
                    f"这里给的是第 {index + 1}–{len(lines)} 行（结尾）。")
        first = index + 1
        offset = sum(map(len, lines[:index]))
        selected = lines[index:index + max_lines]
    content = "".join(selected)[:max_characters]
    consumed = offset + len(content)
    last = first + content.count("\n") - int(content.endswith("\n"))
    more = consumed < len(text)
    return TextPage(content, first, max(first, last), len(lines),
                    consumed if more else None,
                    last + 1 if more and content.endswith("\n") else None, note)
