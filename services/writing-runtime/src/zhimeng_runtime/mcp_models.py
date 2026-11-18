"""Local MCP connection configuration and public, credential-free views."""
from __future__ import annotations

from typing import Any, Literal
import re
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str | None = Field(default=None, pattern=r"^(?:[a-f0-9]{64}|missing)$")
    creation_id: str | None = Field(default=None, pattern=r"^mcp-[a-f0-9]{32}$")
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    remove_env: list[str] = Field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    remove_headers: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("名称和启动命令需要填写完整。")
        return value

    @field_validator("command")
    @classmethod
    def valid_command(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("启动命令含有无效字符。")
        return value.strip()

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(char in value for char in "\r\n\x00"):
                raise ValueError("请求头名称或内容无效。")
        return values

    @model_validator(mode="after")
    def connection_fields(self) -> McpServerUpdate:
        if self.transport == "stdio" and not self.command:
            raise ValueError("请填写本地服务的启动命令。")
        if self.url:
            self.url = self.url.strip()
        if self.transport == "http" and self.url:
            try:
                parsed = urlsplit(self.url)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("请填写完整的 HTTP 或 HTTPS 工具服务地址。")
            if parsed.username or parsed.password:
                raise ValueError("连接凭据请放在请求头中，服务地址填写不含账号密码的 URL。")
        if self.transport == "http" and not self.url:
            raise ValueError("请填写 HTTP 工具服务地址。")
        return self

    @field_validator("args")
    @classmethod
    def valid_args(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("启动参数含有无效字符。")
        return values

    @field_validator("env")
    @classmethod
    def valid_env(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or "=" in key or "\x00" in key or "\x00" in value for key, value in values.items()):
            raise ValueError("环境变量名称或内容无效。")
        return values


class McpServerConfig(BaseModel):
    revision: str = Field(default="", exclude=True)
    connection_id: str
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)


class McpServerView(BaseModel):
    revision: str = ""
    applied_revision: str | None = None
    configuration_status: Literal["applied", "pending"] = "applied"
    apply_error: str | None = None
    source: Literal["settings"] = "settings"
    connection_id: str
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str
    args: list[str]
    cwd: str
    enabled: bool
    env_names: list[str]
    url: str | None = None
    header_names: list[str] = Field(default_factory=list)
    state: Literal["disabled", "connecting", "ready", "error"]
    error: str | None = None
    tool_count: int = 0
    protocol_version: str | None = None


class McpDiscoveredTool(BaseModel):
    connection_id: str
    generation: int
    server_name: str
    raw_name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any]
    read_only: bool = False
    annotations: dict[str, Any] = Field(default_factory=dict)


class McpConnectionError(RuntimeError):
    """A safe diagnostic; a failed call is never automatically replayed."""
