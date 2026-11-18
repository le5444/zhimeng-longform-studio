"""Runtime-owned stdio MCP clients; SDK v2 owns negotiation and request semantics."""
from __future__ import annotations

import asyncio
import codecs
from contextlib import asynccontextmanager, suppress
import ctypes
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Any, AsyncIterator

import anyio
import httpx2
from mcp import Client
import mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from zhimeng_runtime.command_sessions import _StreamMask, _WindowsJob
from zhimeng_runtime.mcp_models import McpConnectionError, McpDiscoveredTool, McpServerConfig, McpServerView
from zhimeng_runtime.mcp_settings import McpSettingsService
from zhimeng_runtime.secret_mask import is_secret_name, mask_secrets


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "；".join(dict.fromkeys(_error_text(child) for child in exc.exceptions))
    return str(exc) or type(exc).__name__


def _connection_closed(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_connection_closed(child) for child in exc.exceptions)
    return isinstance(exc, MCPError) and exc.code == mcp_types.CONNECTION_CLOSED


class _LegacyReconnectRequired(RuntimeError):
    """The SDK's discover timeout needs a fresh legacy bridge transport."""


def _legacy_reconnect_required(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_legacy_reconnect_required(child) for child in exc.exceptions)
    return isinstance(exc, _LegacyReconnectRequired)


def login_environment() -> dict[str, str]:
    """Build a fresh account environment, independent of an AI host's process env."""
    if os.name != "nt":
        return dict(os.environ)
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("无法读取当前登录用户的环境。")
    block = ctypes.c_void_p()
    try:
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise OSError("无法建立当前登录用户的环境。")
        result: dict[str, str] = {}
        address = block.value
        while address:
            entry = ctypes.wstring_at(address)
            if not entry:
                break
            name, _, value = entry.partition("=")
            if name:
                result[name] = value
            address += (len(entry) + 1) * ctypes.sizeof(ctypes.c_wchar)
        return result
    finally:
        if block.value:
            userenv.DestroyEnvironmentBlock(block)
        kernel.CloseHandle(token)


class _SecretStream:
    """Hold only a possible configured-secret suffix, before the generic masker."""
    def __init__(self, values: list[str]):
        self.values = sorted({value for value in values if value}, key=len, reverse=True)
        self.pending = ""
        self.generic = _StreamMask()
        self.prefixes: dict[str, list[int]] = {}
        for value in self.values:
            table = [0] * len(value)
            matched = 0
            for index in range(1, len(value)):
                while matched and value[index] != value[matched]:
                    matched = table[matched - 1]
                if value[index] == value[matched]:
                    matched += 1
                table[index] = matched
            self.prefixes[value] = table

    def feed(self, text: str, *, final: bool = False) -> str:
        data = self.pending + text
        for value in self.values:
            data = data.replace(value, "[已遮住]")
        keep = 0
        if not final:
            for value in self.values:
                matched = 0
                table = self.prefixes[value]
                for char in data:
                    while matched and (matched == len(value) or char != value[matched]):
                        matched = table[matched - 1]
                    if char == value[matched]:
                        matched += 1
                if matched < len(value):
                    keep = max(keep, matched)
        self.pending = data[-keep:] if keep else ""
        return self.generic.feed(data[:-keep] if keep else data, final=final)


@dataclass
class _Connection:
    config: McpServerConfig
    generation: int
    state: str = "connecting"
    error: str | None = None
    protocol_version: str | None = None
    tools: tuple[McpDiscoveredTool, ...] = ()
    client: Client | None = None
    task: asyncio.Task[None] | None = None
    scope: anyio.CancelScope | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: bool = False
    retired: bool = False
    handshake_methods: list[str] = field(default_factory=list)
    http_error: str | None = None


class _SafeMcpLogs(logging.Filter):
    def __init__(self, service: McpClientService):
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += " " + str(record.exc_info[1])
        record.msg = self.service.redact(None, message)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


class McpClientService:
    def __init__(self, settings: McpSettingsService, runtime_dir: Path):
        self.settings = settings
        self.runtime_dir = Path(runtime_dir) / "mcp-logs"
        self._connections: dict[str, _Connection] = {}
        self._generation = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._secrets: set[str] = set()
        self._closed = False
        self.settings_error: str | None = None
        self._apply_errors: dict[str, tuple[str, str]] = {}
        self._log_filter = _SafeMcpLogs(self)
        self._filtered_loggers: list[logging.Logger] = []

    def redact(self, connection_id: str | None, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            for representation in {value, json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]}:
                text = text.replace(representation, "[已遮住]")
        return mask_secrets(text)[0]

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(None, value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(None, str(key)): self._redact_value(item) for key, item in value.items()}
        return value

    async def start(self) -> None:
        for name, logger in logging.Logger.manager.loggerDict.copy().items():
            if name.startswith(("mcp.", "httpx2", "httpcore")) and isinstance(logger, logging.Logger) and logger not in self._filtered_loggers:
                logger.addFilter(self._log_filter)
                self._filtered_loggers.append(logger)
        try:
            configs = self.settings.list_configs()
        except Exception as exc:
            self.settings_error = "工具连接配置读取失败：" + self.redact(None, str(exc))
            return
        for config in configs:
            await self.apply(config)

    async def apply(self, config: McpServerConfig) -> None:
        if self._closed:
            raise McpConnectionError("工具连接服务已经关闭。")
        old = self._connections.get(config.connection_id)
        if old:
            self._retire(old)
        self._generation += 1
        connection = _Connection(config=config, generation=self._generation, state="connecting" if config.enabled else "disabled")
        # Ordinary configuration values also appear in tool schemas and results.
        for name, value in (*config.env.items(), *config.headers.items()):
            if value and (is_secret_name(name) or mask_secrets(value)[1]):
                self._secrets.add(value)
                if name.lower() in {"authorization", "proxy-authorization"} and " " in value:
                    self._secrets.add(value.split(" ", 1)[1])
        self._connections[config.connection_id] = connection
        self._apply_errors.pop(config.connection_id, None)
        self.settings_error = None
        if config.enabled:
            task = asyncio.create_task(self._own(connection, old.task if old else None), name=f"mcp-{config.connection_id}")
            connection.task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _retire(self, connection: _Connection) -> None:
        connection.retired = True
        connection.tools = ()
        connection.changed.set()
        # A ready HTTP owner can leave its wait loop normally, letting the SDK
        # send DELETE for its legacy session. Connecting/listing work may be
        # blocked, so that phase still gets cancellation to close promptly.
        graceful_http = connection.config.transport == "http" and connection.state == "ready"
        if connection.scope and not graceful_http:
            connection.scope.cancel()

    async def remove(self, connection_id: str) -> None:
        self._apply_errors.pop(connection_id, None)
        connection = self._connections.pop(connection_id, None)
        if connection:
            self._retire(connection)

    async def refresh(self, connection_id: str) -> None:
        # Refresh also recovers failed/dead processes; old mappings are retired now.
        config = self.settings.get(connection_id)
        await self.apply(config)

    def record_apply_error(self, config: McpServerConfig, exc: Exception) -> None:
        message = str(exc)
        for value in (*config.env.values(), *config.headers.values()):
            if value:
                for representation in {value, json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1]}:
                    message = message.replace(representation, "[已遮住]")
        self._apply_errors[config.connection_id] = (config.revision, self.redact(config.connection_id, message))

    def views(self, configs: list[McpServerConfig] | None = None) -> list[McpServerView]:
        if self.settings_error and configs is None:
            raise McpConnectionError(self.settings_error)
        result = []
        for config in configs if configs is not None else [item.config for item in self._connections.values()]:
            item = self._connections.get(config.connection_id)
            applied = item is not None and item.config.revision == config.revision
            failure = self._apply_errors.get(config.connection_id)
            apply_error = failure[1] if failure and failure[0] == config.revision else None
            result.append(McpServerView(
                **config.model_dump(), revision=config.revision,
                applied_revision=item.config.revision if item else None,
                configuration_status="applied" if applied else "pending", apply_error=apply_error,
                env_names=sorted(config.env), header_names=sorted(config.headers),
                state=item.state if applied else ("error" if config.enabled else "disabled"),
                error=item.error if applied else "配置已保存，尚未应用到工具连接。",
                tool_count=len(item.tools) if applied else 0,
                protocol_version=item.protocol_version if applied else None,
            ))
        return result

    def catalog(self) -> tuple[McpDiscoveredTool, ...]:
        try:
            saved = {config.connection_id: config.revision for config in self.settings.list_configs()}
        except Exception:
            # Retain last-known descriptions on an unreadable file. Dispatch
            # still checks the source and reports the failure before a call.
            saved = {key: item.config.revision for key, item in self._connections.items()}
        return tuple(tool for key in sorted(self._connections) for tool in self._connections[key].tools
                     if self._connections[key].state == "ready" and not self._connections[key].retired
                     and self._connections[key].config.revision == saved.get(key))

    async def call(self, connection_id: str, generation: int, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self._connections.get(connection_id)
        if not item or item.retired or not item.config.enabled or item.state != "ready" or not item.client:
            raise McpConnectionError("这个工具连接当前不可用，请在设置中查看或重新连接。")
        if item.generation != generation or not any(tool.raw_name == tool_name for tool in item.tools):
            raise McpConnectionError("工具目录已经更新，请读取当前工具后再调用。")
        try:
            saved = await asyncio.to_thread(self.settings.get, connection_id)
        except KeyError:
            raise McpConnectionError("这个工具连接的配置已被移除，本次未调用工具。") from None
        except Exception:
            raise McpConnectionError("工具连接配置暂时无法读取，本次未调用工具；请在设置中检查后重试。") from None
        if saved.revision != item.config.revision:
            raise McpConnectionError("工具连接配置已更改，本次未调用工具；请重新连接以应用新配置。")
        # Reading from disk yields to reconnect/remove and discovery updates.
        # Never dispatch through the previously captured client after that wait.
        if (self._connections.get(connection_id) is not item or item.retired
                or not item.config.enabled or item.state != "ready" or not item.client
                or item.generation != generation
                or not any(tool.raw_name == tool_name for tool in item.tools)):
            raise McpConnectionError("工具连接已经更新，本次未调用工具；请读取当前工具后再调用。")
        try:
            # Low-level SDK call avoids its high-level implicit input/re-list retry.
            # No replay after a request has potentially produced a side effect.
            result = await item.client.session.call_tool(tool_name, arguments)
            raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            return self._redact_value(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = self.redact(connection_id, item.http_error or _error_text(exc))
            if item.config.transport == "http" and _connection_closed(exc):
                item.http_error = detail
                item.error = detail
                item.disconnected = True
                item.state = "error"
                item.tools = ()
                item.changed.set()
            raise McpConnectionError(f"{detail}；本次请求未取得完整回执，外部操作可能已经执行。") from None

    async def _list_tools(self, item: _Connection, client: Client) -> tuple[McpDiscoveredTool, ...]:
        cursor = None
        seen: set[str] = set()
        names: set[str] = set()
        found = []
        while True:
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for tool in page.tools:
                if tool.name in names:
                    raise McpConnectionError("服务返回了重复的原始工具名：" + tool.name)
                names.add(tool.name)
                annotations = tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) if tool.annotations else {}
                found.append(McpDiscoveredTool(connection_id=item.config.connection_id, generation=item.generation,
                    server_name=item.config.name, raw_name=tool.name, title=tool.title,
                    description=self.redact(item.config.connection_id, tool.description or ""),
                    input_schema=tool.input_schema, read_only=annotations.get("readOnlyHint") is True, annotations=annotations))
            cursor = page.next_cursor
            if not cursor:
                return tuple(sorted(found, key=lambda tool: tool.raw_name))
            if cursor in seen:
                raise McpConnectionError("服务的工具分页反复返回同一个游标，目录未更新。")
            seen.add(cursor)

    async def _own(self, item: _Connection, previous: asyncio.Task[None] | None) -> None:
        try:
            if previous:
                await asyncio.shield(previous)
            if item.retired:
                return
            with anyio.CancelScope() as scope:
                item.scope = scope
                for mode in ("auto", "legacy"):
                    item.handshake_methods.clear()
                    item.disconnected = False
                    try:
                        await self._serve(item, mode)
                        break
                    except Exception as exc:
                        # Some legacy stdio-to-HTTP bridges terminate their pipe on
                        # server/discover instead of returning method-not-found.
                        # A fresh legacy handshake is safe only before initialization
                        # or any tool/list/call traffic. SDK still owns both eras.
                        discover_eof = (item.handshake_methods == ["server/discover"]
                                        and item.disconnected and _connection_closed(exc))
                        if (item.config.transport == "stdio" and mode == "auto" and item.protocol_version is None and not item.retired
                                and (discover_eof or _legacy_reconnect_required(exc))):
                            continue
                        raise
        except Exception as exc:
            item.state = "error"
            item.error = self.redact(item.config.connection_id, item.http_error or _error_text(exc)) or "工具服务连接失败。"
        finally:
            item.client = None
            item.tools = ()
            item.scope = None

    async def _serve(self, item: _Connection, mode: str) -> None:
        async def notice(message: Any) -> None:
            root = getattr(message, "root", message)
            if getattr(root, "method", None) == "notifications/tools/list_changed":
                item.changed.set()

        async with Client(self._transport(item), mode=mode, cache=None, message_handler=notice) as client:
            item.client = client
            item.protocol_version = client.protocol_version
            while not item.retired:
                item.changed.clear()
                if item.disconnected:
                    raise McpConnectionError(item.http_error or "工具服务进程已退出。")
                tools = await self._list_tools(item, client)
                if item.retired:
                    break
                item.tools = tools
                item.state = "ready"
                item.error = None
                await item.changed.wait()
                if item.disconnected:
                    raise McpConnectionError(item.http_error or "工具服务进程已退出。")
                if not item.retired:
                    self._generation += 1
                    item.generation = self._generation
                    item.tools = ()
                    item.state = "connecting"

    @asynccontextmanager
    async def _transport(self, item: _Connection) -> AsyncIterator[Any]:
        config = item.config
        if config.transport == "http":
            async with self._http_transport(item) as streams:
                yield streams
            return
        env = login_environment()
        # Windows env names are case-insensitive; avoid duplicate Path/PATH slots.
        for name, value in config.env.items():
            if os.name == "nt":
                for existing in list(env):
                    if existing.casefold() == name.casefold():
                        del env[existing]
            env[name] = value
        configured_command = Path(config.command)
        if not configured_command.is_absolute() and ("/" in config.command or "\\" in config.command):
            # An explicit relative path belongs to this service's configured cwd,
            # independent of the runtime's own working directory.
            command = str(Path(config.cwd) / configured_command)
        else:
            command = shutil.which(config.command, path=env.get("PATH", env.get("Path", ""))) or config.command
        options: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                   "cwd": config.cwd, "env": env, "bufsize": 0}
        job = _WindowsJob() if os.name == "nt" else None
        if job:
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        else:
            options["start_new_session"] = True
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen([command, *config.args], **options)
            if job:
                job.attach_and_resume(process)
            reader_send, reader = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            writer, writer_receive = anyio.create_memory_object_stream[SessionMessage](0)
            discover_id: str | int | None = None
            discover_answered = False
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.runtime_dir / f"{config.connection_id}.log"

            async def read_stdout() -> None:
                nonlocal discover_answered
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                pending = ""
                try:
                    async with reader_send:
                        while chunk := await anyio.to_thread.run_sync(process.stdout.read, 65536, abandon_on_cancel=True):
                            pending += decoder.decode(chunk)
                            lines = pending.split("\n")
                            pending = lines.pop()
                            for line in lines:
                                try:
                                    parsed = mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                                except ValueError:
                                    await reader_send.send(McpConnectionError("工具服务输出了无效的 JSON-RPC 消息。"))
                                else:
                                    if discover_id is not None and getattr(parsed, "id", None) == discover_id:
                                        discover_answered = True
                                    await reader_send.send(SessionMessage(parsed))
                except (OSError, anyio.ClosedResourceError, anyio.BrokenResourceError):
                    pass
                finally:
                    item.disconnected = True
                    item.changed.set()

            async def write_stdin() -> None:
                nonlocal discover_id, discover_answered
                def write_all(payload: bytes) -> None:
                    view = memoryview(payload)
                    while view:
                        written = process.stdin.write(view)
                        if not written:
                            raise BrokenPipeError("工具服务的输入管道已关闭。")
                        view = view[written:]
                    process.stdin.flush()

                async with writer_receive:
                    async for message in writer_receive:
                        method = getattr(message.message, "method", None)
                        if method == "server/discover":
                            discover_id = getattr(message.message, "id", None)
                            discover_answered = False
                        elif method == "initialize" and discover_id is not None and not discover_answered:
                            # Client(auto) has already waited its own discovery-only
                            # timeout. A legacy HTTP bridge can remain wedged by the
                            # unanswered request, so initialize a fresh owned process.
                            # Nothing after negotiation is retried by this branch.
                            raise _LegacyReconnectRequired("服务未响应协议发现，改用新的兼容连接。")
                        if item.protocol_version is None and method and not method.startswith("notifications/"):
                            item.handshake_methods.append(method)
                        payload = (message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode("utf-8")
                        await anyio.to_thread.run_sync(write_all, payload, abandon_on_cancel=True)

            async def read_stderr() -> None:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                masker = _SecretStream(list(config.env.values()))
                with log_path.open("a", encoding="utf-8", newline="") as log:
                    while chunk := await anyio.to_thread.run_sync(process.stderr.read, 65536, abandon_on_cancel=True):
                        log.write(masker.feed(decoder.decode(chunk)))
                        log.flush()
                    log.write(masker.feed(decoder.decode(b"", final=True), final=True))

            async with anyio.create_task_group() as group:
                group.start_soon(read_stdout)
                group.start_soon(write_stdin)
                group.start_soon(read_stderr)
                try:
                    yield reader, writer
                finally:
                    # EOF offers the server its normal shutdown path. The two-second
                    # grace is transport teardown, never a running-tool deadline.
                    with anyio.CancelScope(shield=True):
                        with suppress(OSError):
                            process.stdin.close()
                        with anyio.move_on_after(2):
                            while process.poll() is None:
                                await anyio.sleep(0.01)
                        # Job membership was acquired before the first instruction;
                        # closing it also reaps descendants after a graceful parent.
                        if job:
                            job.close()
                        elif process.poll() is None:
                            with suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                        await anyio.to_thread.run_sync(process.wait)
                        await reader.aclose()
                        await writer.aclose()
                    group.cancel_scope.cancel()
        finally:
            if job:
                job.close()
            if process:
                if process.poll() is None:
                    process.kill()
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe:
                        pipe.close()

    @asynccontextmanager
    async def _http_transport(self, item: _Connection) -> AsyncIterator[Any]:
        async def request_hook(request: httpx2.Request) -> None:
            if request.method == "DELETE":
                # Session teardown gets a bounded network cleanup period; tool
                # requests and their response streams have no read deadline.
                request.extensions["timeout"] = {key: 5.0 for key in ("connect", "read", "write", "pool")}

        async def response_hook(response: httpx2.Response) -> None:
            if response.status_code in (401, 403):
                item.http_error = f"连接认证失败（HTTP {response.status_code}），请检查请求头或服务端授权。"
            elif response.status_code == 404 and item.protocol_version is not None:
                item.http_error = "远程工具会话已失效（HTTP 404），请重新连接后继续。"
            else:
                return
            item.disconnected = True
            item.tools = ()
            item.changed.set()

        # SDK2 follows only same-origin redirects, including arbitrary supplied
        # headers. It translates modern cancellation into closing that response
        # stream, and legacy cancellation into the protocol notification.
        async with httpx2.AsyncClient(headers=item.config.headers, timeout=None, trust_env=False,
                follow_redirects=False, event_hooks={"request": [request_hook], "response": [response_hook]}) as http:
            async with streamable_http_client(item.config.url, http_client=http, max_sse_event_size=None) as streams:
                yield streams

    async def shutdown(self) -> None:
        self._closed = True
        for item in self._connections.values():
            self._retire(item)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._connections.clear()
        for logger in self._filtered_loggers:
            logger.removeFilter(self._log_filter)
