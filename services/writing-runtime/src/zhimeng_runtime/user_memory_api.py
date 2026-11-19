"""用户记忆《用户记忆.md》的读写接口和记忆开关：设置页「记忆」用（纠偏指令 3.7；开关 9-29）。

这份文件在每个对话开头作为一条 role="user" 消息带上；memory_write(scope=user) 往里追加。
项目记忆是打开的文件夹里的 AGENTS.md，本身就是项目里的普通文件，不走这里。

设置页按行列出、逐条改删，存的时候带上读到时的 sha256：这中间 Agent 刚好又记了一条，就回 409，
界面重读一遍再改，不把刚记的那条盖掉。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.memory_files import (
    MAX_USER_MEMORY_BYTES,
    PROJECT_MEMORY_FILE_NAME,
    memory_enabled,
    read_user_memory,
    set_memory_enabled,
    user_memory_path,
    user_memory_transaction,
    write_user_memory,
)


class SaveUserMemoryRequest(BaseModel):
    content: str = Field(max_length=MAX_USER_MEMORY_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MemorySettingsRequest(BaseModel):
    enabled: bool


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def create_user_memory_router(state_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory/user", tags=["memory"])

    def read_current() -> str:
        try:
            return read_user_memory(state_root, strict=True)
        except (OSError, UnicodeError) as exc:
            raise ApiError(
                "无法完整读取现有记忆，请检查文件权限和 UTF-8 编码；原文件未被覆盖。",
                status_code=503, code="user_memory_unreadable",
            ) from exc

    def view() -> dict:
        content = read_current()
        path = user_memory_path(state_root)
        settings_error = None
        try:
            enabled = memory_enabled(state_root, strict=True)
        except (OSError, ValueError):
            enabled = False
            settings_error = "记忆开关暂时无法读取，Agent 已暂停使用和自动保存用户记忆。原有记忆已保留；重新开启开关可保存你的选择。"
        return {
            "schema": "zhimeng.user-memory.v1",
            "path": str(path),
            "exists": path.exists(),
            "content": content,
            "sha256": _sha256(content),
            "enabled": enabled,
            "settings_error": settings_error,
            "project_memory_file": PROJECT_MEMORY_FILE_NAME,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.exists() else None,
        }

    @router.get("")
    async def read_memory() -> dict:
        return view()

    @router.put("")
    async def save_memory(request: SaveUserMemoryRequest) -> dict:
        with user_memory_transaction(state_root):
            current = read_current()
            if request.expected_sha256 is not None and _sha256(current) != request.expected_sha256:
                raise ApiError(
                    "记忆在你改的时候又多了内容（多半是 Agent 刚记了一条），重新读一遍再改。",
                    status_code=409,
                    code="user_memory_changed",
                )
            try:
                path = write_user_memory(state_root, request.content)
            except (OSError, UnicodeError) as exc:
                raise ApiError(
                    "记忆保存未完成，请重新读取核对；现有版本和备份未被删除。",
                    status_code=503, code="user_memory_write_failed",
                ) from exc
            except ValueError as exc:
                raise ApiError(str(exc), status_code=422, code="user_memory_invalid") from exc
            return {**view(), "path": str(path), "ok": True}

    @router.put("/settings")
    async def save_settings(request: MemorySettingsRequest) -> dict:
        set_memory_enabled(state_root, request.enabled)
        return view()

    @router.get("/history")
    async def history(offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)) -> dict:
        paths = sorted((Path(state_root) / "memory-history").glob("*.json"), reverse=True)
        items = []
        for path in paths[offset:offset + limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                content = data["content"]
                items.append({"version": path.stem, "saved_at": data["saved_at"], "characters": len(content)})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return {"items": items, "next_offset": offset + limit if offset + limit < len(paths) else None}

    @router.get("/history/{version}")
    async def history_version(version: str) -> dict:
        if not re.fullmatch(r"[0-9]{20}-[0-9a-f]{32}", version):
            raise ApiError("找不到这个记忆版本。", status_code=404, code="memory_version_missing")
        try:
            return json.loads((Path(state_root) / "memory-history" / f"{version}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ApiError("找不到这个记忆版本。", status_code=404, code="memory_version_missing") from None

    return router
