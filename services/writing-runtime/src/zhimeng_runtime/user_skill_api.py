"""作者技能文件夹的 HTTP 接口：看菜单、启用、禁用、保存优化后的内容。"""

from __future__ import annotations

import hashlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from zhimeng_runtime.user_skill_folder_service import UserSkillFolderService


class SaveSkillInstructionsRequest(BaseModel):
    content: str = Field(min_length=1, max_length=256 * 1024)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CreateSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=256 * 1024)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("请填写技能的使用方法")
        return value


def create_user_skill_router(service: UserSkillFolderService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/user-skills", tags=["user-skills"])

    @router.get("")
    async def list_user_skills() -> dict:
        return service.public_view()

    @router.post("")
    async def create_user_skill(request: CreateSkillRequest) -> dict:
        ok, message = service.create_skill(request.name, request.description, request.content)
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message, "skills": service.public_view()}

    @router.get("/{name}/instructions")
    async def read_user_skill_instructions(name: str) -> dict:
        ok, content, editable = service.read_instructions_file(name)
        if not ok:
            raise HTTPException(status_code=404, detail=content)
        return {"name": name, "content": content, "editable": editable,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}

    @router.post("/{name}/enable")
    async def enable_user_skill(name: str) -> dict:
        entry = service.set_enabled(name, True)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"skill '{name}' not found")
        return {"name": entry.name, "enabled": True}

    @router.post("/{name}/disable")
    async def disable_user_skill(name: str) -> dict:
        entry = service.set_enabled(name, False)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"skill '{name}' not found")
        return {"name": entry.name, "enabled": False}

    @router.post("/sources/{key}/enable")
    async def enable_skill_source(key: str) -> dict:
        """技能库：用上本机这一家 agent 的技能（原地读它的文件夹，不复制、不往里写）。"""
        if not service.set_source_enabled(key, True):
            raise HTTPException(status_code=404, detail=f"不认识的技能来源 '{key}'")
        return service.public_view()

    @router.post("/sources/{key}/disable")
    async def disable_skill_source(key: str) -> dict:
        if not service.set_source_enabled(key, False):
            raise HTTPException(status_code=404, detail=f"不认识的技能来源 '{key}'")
        return service.public_view()

    @router.post("/{name}/copy-to-library")
    async def copy_user_skill_to_library(name: str) -> dict:
        """把别家文件夹里的技能复制一份进织梦自己的技能库，以后在织梦里能改；原来那份不动。"""
        ok, message, _entry = service.copy_to_library(name)
        if not ok:
            code = 404 if "不存在" in message else 409
            raise HTTPException(status_code=code, detail=message)
        return {"ok": True, "message": message, "skills": service.public_view()}

    @router.put("/{name}/instructions")
    async def save_user_skill_instructions(name: str, request: SaveSkillInstructionsRequest) -> dict:
        """作者确认后把优化过的 SKILL.md 全文写回（只允许项目内技能）。"""
        ok, message = service.save_instructions(name, request.content, expected_sha256=request.expected_sha256)
        if not ok:
            code = 404 if "不存在" in message else 409
            raise HTTPException(status_code=code, detail=message)
        return {"name": name, "ok": True, "message": message}

    return router


__all__ = ["create_user_skill_router"]
