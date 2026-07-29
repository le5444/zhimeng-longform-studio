from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from zhimeng_runtime.project_reader import SECTION_LABELS


MAX_IMPORTED_TEXT_CHARACTERS = 8 * 1024 * 1024
MAX_IMPORTED_CHAPTERS = 10_000
INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CHAPTER_HEADING = re.compile(
    r"^\s*第\s*(?:0*\d{1,6}|[零〇一二三四五六七八九十百千万两]{1,12})"
    r"\s*章(?:\s*[：:._-]?\s*.*)?$"
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class TextProjectImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportedTextProject:
    root_path: Path
    title: str
    source_file_name: str
    chapter_count: int


def _safe_name(value: str, *, fallback: str, max_length: int = 80) -> str:
    cleaned = INVALID_WINDOWS_NAME.sub(" ", value)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}-作品"
    return cleaned[:max_length].rstrip(" .") or fallback


def _split_chapters(text: str, title: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if len(line.strip()) <= 120 and CHAPTER_HEADING.fullmatch(line)
    ]
    if not starts:
        return [(title, text.strip())]
    if len(starts) > MAX_IMPORTED_CHAPTERS:
        raise TextProjectImportError(
            f"TXT contains more than {MAX_IMPORTED_CHAPTERS} chapter headings"
        )

    prefix = "\n".join(lines[: starts[0]]).strip()
    chapters: list[tuple[str, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        heading = lines[start].strip()
        content = "\n".join(lines[start:end]).strip()
        if position == 0 and prefix:
            content = f"{prefix}\n\n{content}"
        chapters.append((heading, content))
    return chapters


class TextProjectImportService:
    def __init__(self, managed_root: Path) -> None:
        self.managed_root = managed_root.expanduser().resolve(strict=False)
        self._lock = threading.Lock()

    def import_text(self, *, file_name: str, content: str) -> ImportedTextProject:
        source_name = Path(file_name).name.strip()
        if not source_name or source_name != file_name.strip():
            raise TextProjectImportError("TXT file name is invalid")
        if Path(source_name).suffix.casefold() != ".txt":
            raise TextProjectImportError("Only .txt works can be imported")

        normalized = content.removeprefix("\ufeff").replace("\r\n", "\n").replace(
            "\r", "\n"
        )
        if not normalized.strip():
            raise TextProjectImportError("TXT file is empty")
        if len(normalized) > MAX_IMPORTED_TEXT_CHARACTERS:
            raise TextProjectImportError(
                f"TXT exceeds the {MAX_IMPORTED_TEXT_CHARACTERS} character limit"
            )

        title = _safe_name(Path(source_name).stem, fallback="未命名作品")
        chapters = _split_chapters(normalized, title)
        with self._lock:
            return self._write_project(
                title=title,
                source_file_name=source_name,
                chapters=chapters,
            )

    def _write_project(
        self,
        *,
        title: str,
        source_file_name: str,
        chapters: list[tuple[str, str]],
    ) -> ImportedTextProject:
        self.managed_root.mkdir(parents=True, exist_ok=True)
        if self.managed_root.is_symlink():
            raise TextProjectImportError("Managed project root cannot be a link")

        final_root = self._available_root(title)
        temp_root = Path(
            tempfile.mkdtemp(prefix=".txt-import-", dir=str(self.managed_root))
        )
        try:
            for index, label in SECTION_LABELS.items():
                (temp_root / f"{index:02d}_{label}").mkdir()
            volume_root = temp_root / "05_正文内容" / "第一卷"
            volume_root.mkdir()
            width = max(3, len(str(len(chapters))))
            for index, (heading, content) in enumerate(chapters, start=1):
                chapter_name = _safe_name(
                    heading,
                    fallback=f"第{index}章",
                    max_length=60,
                )
                chapter_path = volume_root / f"{index:0{width}d}_{chapter_name}.md"
                chapter_path.write_text(
                    f"{content.strip()}\n",
                    encoding="utf-8",
                    newline="\n",
                )
            os.replace(temp_root, final_root)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

        return ImportedTextProject(
            root_path=final_root,
            title=title,
            source_file_name=source_file_name,
            chapter_count=len(chapters),
        )

    def _available_root(self, title: str) -> Path:
        candidate = self.managed_root / title
        if not candidate.exists():
            return candidate
        for index in range(2, 10_000):
            candidate = self.managed_root / f"{title} ({index})"
            if not candidate.exists():
                return candidate
        raise TextProjectImportError("Too many imported works use the same title")


__all__ = [
    "ImportedTextProject",
    "MAX_IMPORTED_TEXT_CHARACTERS",
    "TextProjectImportError",
    "TextProjectImportService",
]
