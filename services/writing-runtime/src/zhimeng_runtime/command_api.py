"""Conversation-scoped visibility and stop controls for runtime-owned commands."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal, NoReturn, TYPE_CHECKING

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.agent_store import AgentStoreNotFound
from zhimeng_runtime.command_sessions import CommandSessionError
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.command_recovery import CommandFileRecovery, CommandRecoveryRequest, CommandRecoveryResponse, RecoveryReceipt
from zhimeng_runtime.file_journal import FileJournalError

if TYPE_CHECKING:
    from zhimeng_runtime.agent_service import AgentService


class CommandFileProblem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    reason: str


class CommandFileTracking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    paths: list[str]
    status: Literal["observing", "recorded", "partial", "unavailable", "unverified", "interrupted"]
    problems: list[CommandFileProblem] = Field(default_factory=list)
    change_id: str | None = None
    file_count: int | None = Field(default=None, ge=0)
    note: str | None = None


class CommandSessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_session_id: str
    run_id: str
    command: str
    cwd: str
    status: Literal["running", "exited", "cancelled", "interrupted"]
    exit_code: int | None
    created_at: float
    ended_at: float | None
    duration_ms: int = Field(ge=0)
    stdout_bytes: int | None = Field(ge=0)
    stderr_bytes: int | None = Field(ge=0)
    output_error: str | None = None
    timed_out: bool
    interruption_reason: str | None
    transport: Literal["pipes", "pty"] = "pipes"
    terminal_rows: int | None = None
    terminal_columns: int | None = None
    terminal_windows_build: int | None = None
    file_tracking: CommandFileTracking | None = None


class CommandInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input: str = Field(min_length=1)


class CommandResizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rows: int = Field(ge=1, le=32767)
    columns: int = Field(ge=1, le=32767)


class CommandSessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[CommandSessionSummary]


class CommandLogPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    start_offset: int = Field(ge=0, description="UTF-8 byte offset at the first complete character.")
    next_offset: int = Field(ge=0, description="UTF-8 byte offset for the next incremental request.")
    total_bytes: int = Field(ge=0)
    truncated_before: bool
    has_more: bool


class CommandSessionReadResponse(CommandSessionSummary):
    stdout: CommandLogPage
    stderr: CommandLogPage


def _command_error(exc: CommandSessionError) -> NoReturn:
    raise ApiError(str(exc), code=exc.code,
                   status_code=404 if exc.code == "command_session_not_found" else 503 if exc.code == "command_output_unavailable" else 422) from exc


def create_command_router(service: AgentService) -> APIRouter:
    router = APIRouter(tags=["agent-commands"])
    prefix = "/api/v1/agent/sessions/{session_id}/commands"

    async def manager_for(session_id: str):
        try:
            await asyncio.to_thread(service.store.get_session, session_id)
        except AgentStoreNotFound as exc:
            raise ApiError(str(exc), status_code=404, code="agent_resource_not_found") from exc
        # Use the executor's one runtime-owned manager. This initialization is
        # synchronous on the same event loop as model command dispatch; creating
        # a second manager would incorrectly mark live processes interrupted.
        factory = getattr(getattr(service, "tool_executor", None), "_command_session_manager", None)
        if not callable(factory):
            raise ApiError("命令服务尚未就绪，请稍后再试。", status_code=503,
                           code="command_service_unavailable", retryable=True)
        try:
            return factory()
        except (OSError, CommandSessionError) as exc:
            raise ApiError("命令记录暂时无法读取，请稍后再试。", status_code=503,
                           code="command_service_unavailable", retryable=True) from exc

    @router.get(prefix, response_model=CommandSessionListResponse)
    async def list_commands(session_id: str):
        manager = await manager_for(session_id)
        return {"commands": await asyncio.to_thread(manager.summaries, session_id)}

    @router.get(prefix + "/{command_session_id}", response_model=CommandSessionReadResponse)
    async def read_command(session_id: str, command_session_id: str,
                           stdout_offset: int | None = Query(default=None, ge=0),
                           stderr_offset: int | None = Query(default=None, ge=0),
                           stdout_before: int | None = Query(default=None, ge=0),
                           stderr_before: int | None = Query(default=None, ge=0),
                           max_bytes: int = Query(default=65536, ge=4, le=1048576)):
        manager = await manager_for(session_id)
        try:
            return await asyncio.to_thread(manager.read_page, session_id, command_session_id,
                stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                stdout_before=stdout_before, stderr_before=stderr_before, max_bytes=max_bytes)
        except CommandSessionError as exc:
            _command_error(exc)

    @router.post(prefix + "/{command_session_id}/stop", response_model=CommandSessionSummary)
    async def stop_command(session_id: str, command_session_id: str):
        manager = await manager_for(session_id)
        try:
            return await asyncio.to_thread(manager.stop_summary, session_id, command_session_id)
        except CommandSessionError as exc:
            _command_error(exc)

    @router.post(prefix + "/{command_session_id}/input", response_model=CommandSessionSummary)
    async def input_command(session_id: str, command_session_id: str, request: CommandInputRequest):
        manager = await manager_for(session_id)
        try:
            return await asyncio.to_thread(manager.input_summary, session_id, command_session_id, text=request.input)
        except CommandSessionError as exc:
            _command_error(exc)

    @router.post(prefix + "/{command_session_id}/resize", response_model=CommandSessionSummary)
    async def resize_command(session_id: str, command_session_id: str, request: CommandResizeRequest):
        manager = await manager_for(session_id)
        try:
            return await asyncio.to_thread(manager.resize, session_id, command_session_id, rows=request.rows, columns=request.columns)
        except CommandSessionError as exc:
            _command_error(exc)

    async def recover_files(session_id: str, command_session_id: str, request: CommandRecoveryRequest | None = None, preview_path: str | None = None):
        manager = await manager_for(session_id)
        try:
            observation_id, run_id = await asyncio.to_thread(manager.recovery_context, session_id, command_session_id)
            recovery = CommandFileRecovery(service.tool_executor._command_file_tracker())
            if request is None:
                return await asyncio.to_thread(recovery.inspect, observation_id, session_id, run_id, preview_path=preview_path)
            return await asyncio.to_thread(recovery.restore, observation_id, session_id, run_id, request)
        except CommandSessionError as exc:
            _command_error(exc)
        except FileJournalError as exc:
            raise ApiError(str(exc), code='command_recovery_' + exc.code,
                           status_code=404 if exc.code == 'not_found' else 409) from exc
        except (OSError, ValueError, KeyError, TypeError, MemoryError, sqlite3.Error) as exc:
            raise ApiError('文件或备份暂时无法核对，原记录已保留；请检查占用或连接后重试。',
                           code='command_recovery_unavailable', status_code=503, retryable=True) from exc

    @router.get(prefix + '/{command_session_id}/file-recovery', response_model=CommandRecoveryResponse)
    async def inspect_file_recovery(session_id: str, command_session_id: str, path: str | None = Query(default=None)):
        return await recover_files(session_id, command_session_id, preview_path=path)

    @router.post(prefix + '/{command_session_id}/file-recovery', response_model=RecoveryReceipt)
    async def restore_command_file(session_id: str, command_session_id: str, request: CommandRecoveryRequest):
        return await recover_files(session_id, command_session_id, request)

    return router
