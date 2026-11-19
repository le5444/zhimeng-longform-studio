"""Client for the visible desktop browser. The host owns pages and action receipts."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.fs_atomic import replace_with_retry


def _action_schema(schema: dict[str, Any]) -> None:
    requirements = [
        (["observe", "click", "fill", "press", "scroll", "navigate", "back", "forward", "reload", "screenshot", "close"], "tab_id", 1),
        (["click", "fill", "press", "scroll", "navigate", "back", "forward", "reload", "close"], "observation_id", 1),
        (["open", "navigate"], "url", 1), (["click", "fill"], "ref", 1),
        (["fill"], "text", 0), (["press"], "key", 1), (["receipt"], "receipt_id", 1),
    ]
    schema["allOf"] = [
        {"if": {"properties": {"action": {"enum": actions}}, "required": ["action"]},
         "then": {"required": [field], "properties": {field: {"type": "string", "minLength": length}}}}
        for actions, field, length in requirements
    ]


class SharedBrowserArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra=_action_schema)
    action: Literal["list", "open", "observe", "click", "fill", "press", "scroll", "navigate", "back", "forward", "reload", "screenshot", "close", "receipt"]
    tab_id: str | None = Field(default=None, description="Page id returned by open/list; always scoped to this conversation.")
    observation_id: str | None = Field(default=None, description="Latest observation of this page, required for an action.")
    url: str | None = None
    ref: str | None = Field(default=None, description="Element reference from observe.")
    text: str | None = None
    key: str | None = Field(default=None, description="For press: Electron key name, for example Enter, Tab, Escape, ArrowDown.")
    x: int = 0
    y: int = 0
    receipt_id: str | None = Field(default=None, description="Request id from an earlier uncertain action; receipt reads it without replay.")

    @model_validator(mode="after")
    def required_action_fields(self) -> SharedBrowserArguments:
        if self.action not in {"list", "open", "receipt"} and not self.tab_id:
            raise ValueError("此动作需要 tab_id。")
        if self.action in {"open", "navigate"} and not self.url:
            raise ValueError("打开网页需要 url。")
        if self.action not in {"list", "open", "observe", "screenshot", "receipt"} and not self.observation_id:
            raise ValueError("请先观察页面，再用 observation_id 执行动作。")
        if self.action in {"click", "fill"} and not self.ref:
            raise ValueError("点击或输入需要观察结果里的 ref。")
        if self.action == "fill" and self.text is None:
            raise ValueError("输入需要 text（空字符串可以清空输入框）。")
        if self.action == "press" and not self.key:
            raise ValueError("按键需要 key。")
        if self.action == "receipt" and not self.receipt_id:
            raise ValueError("读取操作记录需要 receipt_id。")
        return self


class SharedBrowserError(RuntimeError):
    def __init__(self, message: str, *, code: str = "browser_unavailable") -> None:
        super().__init__(message)
        self.code = code


def browser_directory() -> Path:
    override = os.environ.get("ZHIMENG_BROWSER_DATA")
    return Path(override) if override else Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "织梦" / "browser"


class SharedBrowserClient:
    def __init__(self, artifact_root: Path, *, directory: Path | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.directory = directory
        self.artifact_root = artifact_root
        self.transport = transport

    def connection(self) -> dict[str, Any]:
        try:
            value = json.loads(((self.directory or browser_directory()) / "browser-bridge.json").read_text(encoding="utf-8"))
            url = urlsplit(value["url"])
            if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"} or url.username or url.password or url.path:
                raise ValueError("invalid local browser URL")
            if not isinstance(value["token"], str) or len(value["token"]) != 64:
                raise ValueError("invalid local browser credential")
            return value
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SharedBrowserError("共享浏览器尚未连接，请先打开织梦桌面入口；已打开时请核对桌面连接。") from error

    async def request(self, method: str, endpoint: str, body: dict[str, Any] | None = None, *, timeout: float = 40) -> dict[str, Any]:
        connection = await asyncio.to_thread(self.connection)
        try:
            async with httpx.AsyncClient(base_url=connection["url"], headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False, timeout=timeout, transport=self.transport) as client:
                response = await client.request(method, endpoint, json=body)
                payload = response.json()
                if response.is_error:
                    raise SharedBrowserError(payload.get("error", "浏览器操作没有完成。"), code=payload.get("code", "browser_request_failed"))
                return payload
        except (httpx.HTTPError, ValueError) as error:
            # Do not include request headers or the descriptor in diagnostics.
            raise SharedBrowserError("与桌面浏览器的连接中断，操作结果需要核对。", code="browser_connection_lost") from error

    async def cancel(self, session_id: str) -> None:
        try:
            await self.request("POST", f"/sessions/{quote(session_id, safe='')}/cancel", {}, timeout=5)
        except SharedBrowserError:
            pass  # The caller still records cancellation; a disconnected host cannot prove rollback.

    async def execute(self, session_id: str, run_id: str, tool_call_id: str, arguments: SharedBrowserArguments) -> dict[str, Any]:
        session_path = f"/sessions/{quote(session_id, safe='')}"
        base = session_path + "/tabs"
        request_id = "browser-" + hashlib.sha256(f"{run_id}:{tool_call_id}".encode()).hexdigest()
        if arguments.action == "list":
            return await self.request("GET", base)
        if arguments.action == "receipt":
            return await self.request("GET", session_path + "/requests/" + quote(arguments.receipt_id or "", safe=""))
        tab = base + "/" + quote(arguments.tab_id or "", safe="")
        if arguments.action == "observe":
            return await self.request("POST", tab + "/observe", {})
        if arguments.action == "screenshot":
            try:
                result = await self.request("POST", tab + "/screenshot", {})
            except asyncio.CancelledError:
                await asyncio.shield(self.cancel(session_id))
                raise
            data = base64.b64decode(result["data"], validate=True)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise SharedBrowserError("浏览器没有返回有效的 PNG 截图。")
            target = self.artifact_root / (request_id + ".png")
            await asyncio.to_thread(self._save_screenshot, target, data)
            return {"path": str(target), "mime_type": "image/png", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "tab_id": arguments.tab_id}
        body = arguments.model_dump(exclude_none=True)
        body.pop("tab_id", None)
        body["request_id"] = request_id
        endpoint = base if arguments.action == "open" else tab + "/action"
        try:
            return await self.request("POST", endpoint, body)
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel(session_id))
            raise
        except SharedBrowserError as error:
            if error.code != "browser_connection_lost":
                raise
            # One read-back, never an automatic second click/navigation after a lost reply.
            try:
                receipt = await self.request("GET", session_path + "/requests/" + request_id, timeout=5)
            except SharedBrowserError:
                receipt = None
            if receipt and receipt.get("status") == "completed":
                return {**receipt["result"], "request_id": request_id, "receipt_recovered": True}
            raise SharedBrowserError(f"浏览器操作回执尚未确认（{request_id}）。可读取 receipt 并重新观察页面核对；原动作未自动重放。", code="browser_action_uncertain") from error

    @staticmethod
    def _save_screenshot(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        replace_with_retry(temporary, target)
