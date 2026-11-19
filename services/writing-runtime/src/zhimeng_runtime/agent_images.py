"""Persist pasted images once; message history stores stable image references."""
from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError


class AgentImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_id: str
    name: str
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    url: str


class AgentImageUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "图片"
    data_url: str


FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def image_metadata(row, session_id: str | None = None) -> dict:
    prefix = f"/api/v1/agent/sessions/{session_id}" if session_id else "/api/v1/agent"
    return {"image_id": row["id"], "name": row["name"], "mime_type": row["mime_type"],
            "width": row["width"], "height": row["height"], "url": f"{prefix}/images/{row['id']}"}


def claim_images(connection, session_id: str, image_ids: list[str] | None) -> list[dict]:
    from zhimeng_runtime.agent_store import AgentStoreConflict, AgentStoreNotFound

    result = []
    for image_id in dict.fromkeys(image_ids or []):
        row = connection.execute("SELECT id,session_id,name,mime_type,width,height FROM agent_image WHERE id=?", (image_id,)).fetchone()
        if row is None:
            raise AgentStoreNotFound("图片不存在，请重新添加")
        if row["session_id"] is not None and row["session_id"] != session_id:
            raise AgentStoreConflict("这张图片属于另一场对话，请在当前对话重新添加")
        connection.execute("UPDATE agent_image SET session_id=? WHERE id=? AND session_id IS NULL", (session_id, image_id))
        result.append(image_metadata(row, session_id))
    return result


def provider_image_message(role: str, text: str, images: list) -> dict:
    if not images:
        return {"role": role, "content": text}
    parts = [{"type": "text", "text": text}]
    parts.extend({"type": "image_ref", "image_id": image.image_id if isinstance(image, AgentImage) else image["image_id"]} for image in images)
    return {"role": role, "content": parts}


def fork_images(connection, images: list[dict], session_id: str) -> list[dict]:
    copied = []
    for image in images:
        image_id = f"image-{uuid4().hex}"
        connection.execute(
            "INSERT INTO agent_image(id,session_id,name,mime_type,width,height,data,created_at) "
            "SELECT ?,?,name,mime_type,width,height,data,created_at FROM agent_image WHERE id=?",
            (image_id, session_id, image["image_id"]),
        )
        row = connection.execute("SELECT id,name,mime_type,width,height FROM agent_image WHERE id=?", (image_id,)).fetchone()
        if row is not None:
            copied.append(image_metadata(row, session_id))
    return copied


def hydrate_images(database, session_id: str, messages: list[dict]) -> list[dict]:
    """Prepare wire-only data URLs. The canonical transcript remains ID-only."""
    refs = {part["image_id"] for message in messages if isinstance(message.get("content"), list)
            for part in message["content"] if isinstance(part, dict) and part.get("type") == "image_ref"}
    if not refs:
        return messages
    from zhimeng_runtime.agent_store import AgentStoreNotFound
    encoded = {}
    with database.connection() as connection:
        for image_id in refs:
            row = connection.execute("SELECT mime_type,data FROM agent_image WHERE id=? AND session_id=?", (image_id, session_id)).fetchone()
            if row is None:
                raise AgentStoreNotFound("图片附件无法读取，请重新添加")
            encoded[image_id] = f"data:{row['mime_type']};base64,{base64.b64encode(row['data']).decode('ascii')}"
    return [{**message, "content": [
        {"type": "image_url", "image_url": {"url": encoded[part["image_id"]]}}
        if isinstance(part, dict) and part.get("type") == "image_ref" else part
        for part in message["content"]
    ]} if isinstance(message.get("content"), list) else message for message in messages]


def create_agent_image_router(get_database) -> APIRouter:
    router = APIRouter(tags=["agent-images"])

    def upload(request: AgentImageUpload):
        database = get_database()
        try:
            header, encoded = request.data_url.split(",", 1)
            if not header.startswith("data:") or not header.endswith(";base64"):
                raise ValueError("not a base64 image")
            data = base64.b64decode(encoded, validate=True)
            with Image.open(BytesIO(data)) as image:
                mime = FORMATS.get(image.format or "")
                width, height = image.size
                image.verify()
            if mime is None:
                raise ValueError("unsupported image format")
            # Fully decode the first frame as well; verify() alone can miss a
            # truncated JPEG body. Preserve the original uploaded image bytes.
            with Image.open(BytesIO(data)) as image:
                image.load()
        except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ApiError("无法读取这张图片，请添加完整的 PNG、JPEG、WEBP 或 GIF 图片。", status_code=422, code="agent_image_invalid") from exc
        image_id = f"image-{uuid4().hex}"
        name = request.name.strip().replace("\\", "/").rsplit("/", 1)[-1] or "图片"
        with database.connection() as connection:
            connection.execute("INSERT INTO agent_image(id,session_id,name,mime_type,width,height,data,created_at) VALUES (?,NULL,?,?,?,?,?,?)",
                (image_id, name, mime, width, height, data, datetime.now(UTC).isoformat()))
        return AgentImage(image_id=image_id, name=name, mime_type=mime, width=width, height=height, url=f"/api/v1/agent/images/{image_id}")

    @router.post("/api/v1/agent/images", response_model=AgentImage, status_code=201)
    async def upload_image(request: AgentImageUpload):
        return await run_in_threadpool(upload, request)

    def read(image_id: str, session_id: str | None = None):
        database = get_database()
        with database.connection() as connection:
            row = connection.execute("SELECT mime_type,data,session_id FROM agent_image WHERE id=?", (image_id,)).fetchone()
        if row is None or (session_id is not None and row["session_id"] != session_id):
            raise ApiError("图片不存在", status_code=404, code="agent_image_not_found")
        return Response(content=bytes(row["data"]), media_type=row["mime_type"], headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})

    @router.get("/api/v1/agent/images/{image_id}")
    async def read_uploaded_image(image_id: str):
        return await run_in_threadpool(read, image_id)

    @router.get("/api/v1/agent/sessions/{session_id}/images/{image_id}")
    async def read_session_image(session_id: str, image_id: str):
        return await run_in_threadpool(read, image_id, session_id)

    return router
