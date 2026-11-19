"""选文件夹用的接口（纠偏指令 P2、专题《项目即文件夹》第 2、4 节）：常用位置、列一层文件夹、新建文件夹。

浏览器自带的选文件夹框拿不到完整路径，所以框由前端自己画，文件夹由这里列（OpenCode 浏览器版、Craft Agents
远程版同一个做法）。这里只列文件夹、不读文件内容；打开成项目仍走「加进受信根 + 读项目」那两个现有接口。

- 常用位置：主目录、桌面、文档、下载，受信的文件夹（书库等），打开过的项目所在的上级文件夹，各个盘。
- 列一层：跳过点号开头的、带隐藏 / 系统属性的（回收站、System Volume Information 这类）；一次最多 500 个。
- 新建：名字按 Windows 的规矩当场查（不认的字、保留名、结尾的点和空格），有问题说人话。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from zhimeng_runtime.errors import ApiError

LIST_LIMIT = 500
_INVALID_CHARACTERS = set('\\/:*?"<>|')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_SKIPPED_NAMES = {"$recycle.bin", "system volume information", "$winreagent", "config.msi"}


class MakeFolderRequest(BaseModel):
    parent: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)


def folder_name_problem(name: str) -> str | None:
    """Windows 不认的文件夹名：返回一句人话；名字没问题返回 None。"""

    if not name.strip():
        return "名字不能是空的。"
    bad = sorted({character for character in name if character in _INVALID_CHARACTERS or ord(character) < 32})
    if bad:
        shown = "".join(character for character in bad if ord(character) >= 32)
        return f"名字里不能有这些字符：{shown or '控制字符'}"
    if name != name.rstrip(" ."):
        return "名字结尾不能是点或空格。"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        return f"「{name}」是 Windows 保留的名字，换一个。"
    return None


def _is_hidden(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith(".") or entry.name.casefold() in _SKIPPED_NAMES:
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)) if hasattr(stat, "FILE_ATTRIBUTE_HIDDEN") else False


def _inside_dot_folder(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _looks_like_book(folder: Path) -> bool:
    """小说领域的一个小标记（框里给文件夹打「书」字）：新书有《这本书.md》，老书有 01_项目定位。"""

    try:
        return (folder / "这本书.md").is_file() or (folder / "01_项目定位").is_dir()
    except OSError:
        return False


def _breadcrumbs(path: Path) -> list[dict[str, str]]:
    crumbs: list[dict[str, str]] = []
    current = path
    while True:
        crumbs.append({"name": current.name or str(current), "path": str(current)})
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(crumbs))


def _drives() -> list[str]:
    listdrives = getattr(os, "listdrives", None)
    if callable(listdrives):
        try:
            return [str(drive) for drive in listdrives()]
        except OSError:
            return []
    return ["/"]


def create_fs_router(*, trusted_roots: Any, project_roots: Any) -> APIRouter:
    """trusted_roots / project_roots：每次调用现取（受信根和项目会变）的无参函数，返回路径列表。"""

    router = APIRouter(prefix="/api/v1/fs", tags=["fs"])

    def project_keys() -> set[str]:
        try:
            return {os.path.normcase(str(Path(root))) for root in project_roots()}
        except Exception:
            return set()

    @router.get("/places")
    async def places() -> dict[str, Any]:
        home = Path.home()
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(label: str, path: Path, kind: str) -> None:
            key = os.path.normcase(str(path))
            if key in seen:
                return
            try:
                if not path.is_dir():
                    return
            except OSError:
                return
            seen.add(key)
            found.append({"label": label, "path": str(path), "kind": kind})

        add("主目录", home, "home")
        add("桌面", home / "Desktop", "desktop")
        add("文档", home / "Documents", "documents")
        add("下载", home / "Downloads", "downloads")
        try:
            trusted = [Path(root) for root in trusted_roots()]
        except Exception:
            trusted = []
        # 每打开过一本书，它自己的文件夹也会进受信根；那些在左栏「项目」里已经有了，这里只列书库这类上级文件夹。
        # 点号文件夹里面的（织梦自己的 .runtime\author-projects 这类）是机器用的，不当常用位置，也免得框一打开就停在那儿。
        opened = project_keys()
        for root in trusted:
            if os.path.normcase(str(root)) not in opened and not _inside_dot_folder(root):
                add(root.name or str(root), root, "trusted")
        try:
            parents = sorted({Path(root).parent for root in project_roots()}, key=lambda item: str(item).casefold())
        except Exception:
            parents = []
        for parent in parents:
            if not _inside_dot_folder(parent):
                add(parent.name or str(parent), parent, "recent")
        for drive in _drives():
            add(drive, Path(drive), "drive")
        return {"schema": "zhimeng.fs-places.v1", "places": found}

    @router.get("/list")
    async def list_folders(path: str = "") -> dict[str, Any]:
        raw = path.strip().strip('"')
        target = Path(raw).expanduser() if raw else Path.home()
        if not target.is_absolute():
            raise ApiError(f"要一个完整的位置（比如 C:\\Users\\…）：{raw}", status_code=422, code="fs_path_not_absolute")
        target = target.resolve(strict=False)
        if not target.is_dir():
            raise ApiError(f"这个文件夹不存在或打不开：{target}", status_code=404, code="fs_folder_not_found")
        keys = project_keys()
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(target) as scanner:
                children = sorted(scanner, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ApiError(f"这个文件夹读不了：{exc}", status_code=403, code="fs_folder_unreadable") from exc
        total = 0
        for entry in children:
            try:
                if not entry.is_dir(follow_symlinks=True) or _is_hidden(entry):
                    continue
            except OSError:
                continue
            total += 1
            if len(entries) >= LIST_LIMIT:
                continue
            child = Path(entry.path)
            is_link = entry.is_symlink() or bool(getattr(child, "is_junction", lambda: False)())
            entries.append(
                {
                    "name": entry.name,
                    "path": str(child),
                    "is_link": is_link,
                    "is_project": os.path.normcase(str(child)) in keys,
                    "looks_like_book": _looks_like_book(child),
                }
            )
        return {
            "schema": "zhimeng.fs-list.v1",
            "path": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "breadcrumbs": _breadcrumbs(target),
            "is_project": os.path.normcase(str(target)) in keys,
            "entries": entries,
            "total": total,
            "truncated": total > len(entries),
        }

    @router.post("/mkdir", status_code=201)
    async def make_folder(request: MakeFolderRequest) -> dict[str, Any]:
        problem = folder_name_problem(request.name)
        if problem is not None:
            raise ApiError(problem, status_code=422, code="fs_folder_name_invalid")
        parent = Path(request.parent.strip().strip('"')).expanduser()
        if not parent.is_absolute() or not parent.is_dir():
            raise ApiError(f"要建在哪儿？这个位置不存在：{request.parent}", status_code=404, code="fs_folder_not_found")
        target = parent.resolve(strict=False) / request.name
        if target.exists():
            raise ApiError(f"「{request.name}」已经有了，换个名字，或者直接打开它。", status_code=409, code="fs_folder_exists")
        try:
            target.mkdir()
        except OSError as exc:
            raise ApiError(f"文件夹没建成：{exc}", status_code=500, code="fs_folder_create_failed") from exc
        return {"schema": "zhimeng.fs-mkdir.v1", "path": str(target)}

    return router


__all__ = ["create_fs_router", "folder_name_problem", "LIST_LIMIT"]
