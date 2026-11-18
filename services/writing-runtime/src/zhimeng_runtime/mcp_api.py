"""Settings and diagnostics for the runtime-owned local MCP connections."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.mcp_models import McpConnectionError, McpServerUpdate, McpServerView
from zhimeng_runtime.mcp_tool_adapter import McpToolAdapter
from zhimeng_runtime.settings_versions import SettingsConflictError, check_settings_revision


class McpServerListResponse(BaseModel):
    servers: list[McpServerView]


class McpEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class McpToolDetail(BaseModel):
    alias: str
    raw_name: str
    title: str | None = None
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    annotations: dict[str, Any]
    schema_error: str | None = None


class McpToolListResponse(BaseModel):
    tools: list[McpToolDetail]


def create_mcp_router(manager: Any, adapter: McpToolAdapter) -> APIRouter:
    router = APIRouter(prefix="/api/v1/mcp/servers", tags=["mcp"])
    settings = manager.settings
    mutation_lock = asyncio.Lock()

    def api_error(exc: Exception) -> ApiError:
        if isinstance(exc, SettingsConflictError):
            return ApiError(str(exc), status_code=409, code=exc.code, retryable=False)
        if isinstance(exc, McpConnectionError):
            return ApiError(str(exc), status_code=503, code="mcp_connection_unavailable", retryable=True)
        if isinstance(exc, KeyError):
            return ApiError("找不到这个工具连接。", status_code=404, code="mcp_server_not_found", retryable=False)
        if isinstance(exc, ValueError):
            return ApiError(str(exc), status_code=422, code="mcp_config_invalid", retryable=False)
        # Storage errors can contain filesystem paths; encryption/config values
        # and raw SDK details are deliberately not included in this diagnostic.
        return ApiError("工具连接设置未能保存，请重试。", status_code=500, code="mcp_settings_failed", retryable=True)

    def view(connection_id: str) -> McpServerView:
        for item in manager.views(settings.list_configs()):
            if item.connection_id == connection_id:
                return item
        raise KeyError(connection_id)

    @router.get("", response_model=McpServerListResponse)
    async def list_servers() -> McpServerListResponse:
        try:
            configs = await asyncio.to_thread(settings.list_configs)
            return McpServerListResponse(servers=manager.views(configs))
        except Exception as exc:
            # Read errors must not be presented as a successful empty directory.
            if not isinstance(exc, McpConnectionError):
                message = "工具连接配置格式不正确，原文件已保留；请检查后重试。" if isinstance(exc, ValueError) else "工具连接配置读取失败，请检查文件占用或权限后重试。"
                raise ApiError(message, status_code=503,
                               code="mcp_settings_unreadable", retryable=True) from None
            raise api_error(exc) from None

    async def apply_saved(config) -> McpServerView:
        try:
            await manager.apply(config)
        except Exception as exc:
            # Persistence already succeeded. Return that fact plus the pending
            # application state; a retry must not create a duplicate connection.
            manager.record_apply_error(config, exc)
        return view(config.connection_id)

    async def save(update: McpServerUpdate, connection_id: str | None = None) -> McpServerView:
        async with mutation_lock:
            try:
                config = await asyncio.to_thread(settings.upsert, update, connection_id)
                return await apply_saved(config)
            except Exception as exc:
                raise api_error(exc) from None

    @router.post("", response_model=McpServerView, status_code=201)
    async def create_server(request: McpServerUpdate) -> McpServerView:
        return await save(request)

    @router.put("/{connection_id}", response_model=McpServerView)
    async def update_server(connection_id: str, request: McpServerUpdate) -> McpServerView:
        return await save(request, connection_id)

    @router.delete("/{connection_id}", status_code=204)
    async def delete_server(connection_id: str, expected_revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$")) -> Response:
        async with mutation_lock:
            try:
                await asyncio.to_thread(settings.remove, connection_id, expected_revision=expected_revision)
                await manager.remove(connection_id)
            except Exception as exc:
                raise api_error(exc) from None
        return Response(status_code=204)

    @router.patch("/{connection_id}/enabled", response_model=McpServerView)
    async def set_enabled(connection_id: str, request: McpEnabledRequest) -> McpServerView:
        async with mutation_lock:
            try:
                config = await asyncio.to_thread(settings.get, connection_id)
                check_settings_revision(request.expected_revision, config.revision)
                update = McpServerUpdate(name=config.name, transport=config.transport, url=config.url,
                                         command=config.command, args=config.args,
                                         cwd=config.cwd, enabled=request.enabled, expected_revision=config.revision)
                config = await asyncio.to_thread(settings.upsert, update, connection_id)
                return await apply_saved(config)
            except Exception as exc:
                raise api_error(exc) from None

    @router.post("/{connection_id}/reconnect", response_model=McpServerView)
    async def reconnect(connection_id: str, expected_revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$")) -> McpServerView:
        async with mutation_lock:
            try:
                config = await asyncio.to_thread(settings.get, connection_id)
                check_settings_revision(expected_revision, config.revision)
                return await apply_saved(config)
            except Exception as exc:
                raise api_error(exc) from None

    @router.get("/{connection_id}/tools", response_model=McpToolListResponse)
    async def tools(connection_id: str) -> McpToolListResponse:
        try:
            await asyncio.to_thread(settings.get, connection_id)
            return McpToolListResponse(tools=[McpToolDetail.model_validate(item) for item in adapter.details(connection_id)])
        except Exception as exc:
            raise api_error(exc) from None

    return router
