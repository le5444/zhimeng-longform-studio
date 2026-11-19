from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from zhimeng_runtime.source_import_models import MAX_UPLOADED_SOURCE_CHARACTERS
from zhimeng_runtime.fs_atomic import replace_with_retry


INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class SourceUploadError(RuntimeError):
    pass


class SourceUploadService:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise SourceUploadError("Managed source upload root cannot be a link")
        self.root = self.root.resolve(strict=True)

    def store_text(self, *, file_name: str, content: str) -> Path:
        source_name = file_name.strip()
        if Path(source_name).name != source_name:
            raise SourceUploadError("TXT source file name is invalid")
        if Path(source_name).suffix.casefold() != ".txt":
            raise SourceUploadError("Only TXT source files can be uploaded")
        if (
            INVALID_WINDOWS_NAME.search(source_name)
            or source_name.rstrip(" .") != source_name
            or Path(source_name).stem.upper() in WINDOWS_RESERVED_NAMES
        ):
            raise SourceUploadError("TXT source file name is invalid")

        normalized = content.removeprefix("\ufeff").replace("\r\n", "\n").replace(
            "\r", "\n"
        )
        if not normalized.strip():
            raise SourceUploadError("TXT source file is empty")
        if len(normalized) > MAX_UPLOADED_SOURCE_CHARACTERS:
            raise SourceUploadError(
                f"TXT source exceeds {MAX_UPLOADED_SOURCE_CHARACTERS} characters"
            )

        upload_root = self.root / f"upload-{uuid4().hex}"
        upload_root.mkdir()
        target = upload_root / source_name
        temporary = upload_root / f".{source_name}.tmp"
        try:
            temporary.write_text(normalized, encoding="utf-8", newline="\n")
            replace_with_retry(temporary, target)
            return target.resolve(strict=True)
        except Exception:
            shutil.rmtree(upload_root, ignore_errors=True)
            raise

    def discard(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SourceUploadError("Uploaded source is outside the managed root") from exc
        if resolved.parent == self.root or resolved.parent.parent != self.root:
            raise SourceUploadError("Uploaded source path is invalid")
        shutil.rmtree(resolved.parent, ignore_errors=True)


__all__ = ["SourceUploadError", "SourceUploadService"]
