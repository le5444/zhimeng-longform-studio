"""Human-requested previews of local paths, independent of project registration."""
from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.office_reader import OFFICE_SUFFIXES, MAX_ARCHIVE_BYTES, OfficeError, extract_office_bytes
from zhimeng_runtime.project_reader import (
    MAX_TEXT_FILE_BYTES, ProjectChangedDuringReadError, ProjectReaderError, _read_stable_file_limited,
)
from zhimeng_runtime.text_encoding import decode_project_text


class LocalFilePreview(BaseModel):
    path: str
    kind: Literal["text", "office", "unavailable", "directory"]
    size_bytes: int | None = None
    sha256: str | None = None
    content: str | None = None
    encoding: str | None = None
    format: str | None = None
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class RevealLocalFileRequest(BaseModel):
    path: str = Field(min_length=1)


class RevealLocalFileResponse(BaseModel):
    path: str


def _absolute_path(raw: str) -> Path:
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ApiError("需要文件的完整路径。", status_code=422, code="fs_path_not_absolute")
    return target


def preview_local_file(paths: list[str]) -> LocalFilePreview:
    # Literal filenames win over :line / #L fragments. Only a missing path may
    # fall back; a permission or I/O failure must not silently show another file.
    targets = [_absolute_path(raw) for raw in paths]
    target = targets[0]
    for target in targets:
        try:
            info = target.stat()
            break
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno == 22 or getattr(exc, "winerror", None) == 123:
                continue
            raise ApiError("这个路径暂时无法读取，请检查访问权限。", status_code=403, code="fs_file_unreadable") from exc
    else:
        raise ApiError(f"文件不存在：{targets[0]}", status_code=404, code="fs_file_not_found")
    result = LocalFilePreview(path=str(target.absolute()), kind="unavailable", size_bytes=info.st_size)
    if stat.S_ISDIR(info.st_mode):
        result.kind, result.size_bytes = "directory", None
        return result
    if not stat.S_ISREG(info.st_mode):
        result.warnings = ["这个路径不是普通文件，无法按文字预览。"]
        return result
    office = target.suffix.lower() in OFFICE_SUFFIXES
    limit = MAX_ARCHIVE_BYTES if office else MAX_TEXT_FILE_BYTES
    if info.st_size > limit:
        result.warnings = ["这个文件超过当前文字预览的读取范围。可打开所在文件夹查看原文件，或让 Agent 分段处理。"]
        return result
    try:
        raw = _read_stable_file_limited(target, limit)
    except ProjectChangedDuringReadError as exc:
        raise ApiError("读取期间文件发生变化，请重新读取。", status_code=409, code="fs_file_changed") from exc
    except ProjectReaderError as exc:
        raise ApiError("文件暂时无法读取，请重新读取或检查访问权限。", status_code=403, code="fs_file_unreadable") from exc
    result.sha256 = hashlib.sha256(raw).hexdigest()
    try:
        if office:
            extracted = extract_office_bytes(raw, target.suffix)
            text = extracted.extracted_text
            result.kind, result.format = "office", extracted.kind
            result.warnings = list(extracted.warnings)
        else:
            text, result.encoding, _bom = decode_project_text(raw)
            result.kind = "text"
    except (ValueError, OfficeError) as exc:
        result.warnings = [str(exc) if isinstance(exc, OfficeError) else "这个文件暂时无法按文字预览，可打开所在文件夹查看原文件。"]
        return result
    result.content = text[:512_000]
    result.truncated = len(text) > len(result.content)
    if result.truncated:
        result.warnings.append("目前显示前 512,000 个字符；复制与预览引用仅包含显示部分。")
    return result


def create_local_file_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/fs", tags=["fs"])

    @router.get("/file", response_model=LocalFilePreview)
    async def preview(paths: Annotated[list[str], Query(alias="path", min_length=1, max_length=3)]) -> LocalFilePreview:
        return await asyncio.to_thread(preview_local_file, paths)

    @router.post("/reveal", response_model=RevealLocalFileResponse)
    async def reveal(request: RevealLocalFileRequest) -> RevealLocalFileResponse:
        from zhimeng_runtime.project_api import reveal_folder

        target = _absolute_path(request.path)
        try:
            # A deleted artifact can still be located by its containing folder.
            folder = target if target.is_dir() else target.parent
            if not folder.is_dir():
                raise ApiError("文件所在的文件夹不存在。", status_code=404, code="fs_folder_not_found")
            await asyncio.to_thread(reveal_folder, folder.resolve())
        except OSError as exc:
            raise ApiError("暂时无法打开文件所在的文件夹。", status_code=500, code="fs_reveal_failed") from exc
        return RevealLocalFileResponse(path=str(folder.resolve()))

    return router
