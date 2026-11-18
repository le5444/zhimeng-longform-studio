"""对话里项目外文件的持久修改记录和直接撤回入口。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from zhimeng_runtime.agent_store import AgentStore, AgentStoreNotFound
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.file_journal import FileChange, FileChangeEntry, FileChangeJournal, FileJournalError
from zhimeng_runtime.secret_mask import mask_secrets
from zhimeng_runtime.file_preview import verified_text as _verified_text


class FileChangeItem(BaseModel):
    sequence: int
    operation: Literal["create", "update", "delete", "move", "mkdir"]
    path: str
    target_path: str | None
    undone_at: datetime | None


class SessionFileChange(BaseModel):
    change_id: str
    session_id: str
    run_id: str
    title: str
    created_at: datetime
    undone_at: datetime | None
    status: Literal["applied", "failed", "unverified", "partially_undone", "undone"]
    files: list[FileChangeItem]


class FileChangePreview(FileChangeItem):
    before_text: str | None = None
    after_text: str | None = None
    before_characters: int | None = None
    after_characters: int | None = None
    preview_note: str | None = None
    masked_secrets: int = 0


class SessionFileChangeDetail(SessionFileChange):
    previews: list[FileChangePreview]
    undo_available: bool
    undo_reason: str | None


class SessionFileChanges(BaseModel):
    session_id: str
    changes: list[SessionFileChange]


def _item(entry: FileChangeEntry) -> FileChangeItem:
    return FileChangeItem(
        sequence=entry.sequence, operation=entry.operation, path=mask_secrets(entry.path)[0],
        target_path=mask_secrets(entry.target_path)[0] if entry.target_path else None,
        undone_at=entry.undone_at,
    )


def _summary(change: FileChange) -> SessionFileChange:
    return SessionFileChange(
        change_id=change.change_id, session_id=change.session_id, run_id=change.run_id,
        title=mask_secrets(change.title)[0], created_at=change.created_at, undone_at=change.undone_at,
        status="undone" if change.undone_at else (
            "partially_undone" if any(item.undone_at for item in change.entries) else (
                change.apply_phase if change.apply_phase in {"applied", "failed"} else "unverified"
            )
        ),
        files=[_item(entry) for entry in change.entries],
    )


def _preview(entry: FileChangeEntry) -> FileChangePreview:
    result = FileChangePreview(**_item(entry).model_dump())
    if entry.undone_at:
        result.preview_note = "这个文件已撤回；这份历史记录未保存改后副本，仅显示操作记录。"
        return result
    try:
        if FileChangeJournal._matches_before(entry) and not FileChangeJournal._matches_after(entry):
            result.preview_note = "当前文件已经是修改前的状态；这条记录可能在落盘或撤回记账时中断，可以撤回以核对并完成记录。"
            return result
        if entry.operation in {"move", "mkdir"}:
            result.preview_note = "文件内容未改写。" if entry.operation == "move" else "创建了这个文件夹。"
            return result
        before = "" if entry.operation == "create" else _verified_text(Path(entry.backup_path or ""), entry.before_sha256)
        after = "" if entry.operation == "delete" else _verified_text(Path(entry.path), entry.after_sha256)
    except ValueError as exc:
        result.preview_note = str(exc)
        return result
    except OSError:
        result.preview_note = "当前文件或原内容备份无法读取，暂时不能核验前后对照。"
        return result
    # Match text tools: one logical newline counts as one character on Windows too.
    result.before_characters = len(before) - before.count("\r\n")
    result.after_characters = len(after) - after.count("\r\n")
    result.before_text, before_count = mask_secrets(before)
    result.after_text, after_count = mask_secrets(after)
    result.masked_secrets = before_count + after_count
    if result.masked_secrets:
        result.preview_note = "预览中的密钥已遮住，文件内容保持原样。"
    return result


def _detail(journal: FileChangeJournal, change: FileChange) -> SessionFileChangeDetail:
    reason = "这次修改已全部撤回。" if change.undone_at else None
    if not reason:
        try:
            reason = next((problem for entry in change.entries if not entry.undone_at
                           if (problem := journal._undo_problem(entry, restore_missing=change.apply_phase in {"pending", "failed"}))), None)
        except OSError:
            reason = "暂时无法核对文件状态，请检查文件占用或权限后刷新。"
    return SessionFileChangeDetail(
        **_summary(change).model_dump(), previews=[_preview(entry) for entry in change.entries],
        undo_available=reason is None, undo_reason=mask_secrets(reason)[0] if reason else None,
    )


def create_file_changes_router(journal: FileChangeJournal, sessions: AgentStore) -> APIRouter:
    router = APIRouter(tags=["file-changes"])

    def require_session(session_id: str) -> None:
        try:
            sessions.get_session(session_id)
        except AgentStoreNotFound as exc:
            raise ApiError("没有找到这场对话。", status_code=404, code="agent_resource_not_found", retryable=False) from exc

    def require_change(session_id: str, change_id: str) -> FileChange:
        require_session(session_id)
        change = journal.get(change_id)
        if change is None or change.session_id != session_id:
            raise ApiError("这场对话里没有这条修改记录。", status_code=404, code="file_change_not_found", retryable=False)
        return change

    def list_records(session_id: str) -> SessionFileChanges:
        require_session(session_id)
        return SessionFileChanges(session_id=session_id, changes=[_summary(item) for item in journal.session_changes(session_id)])

    def read_record(session_id: str, change_id: str) -> SessionFileChangeDetail:
        return _detail(journal, require_change(session_id, change_id))

    def undo_record(session_id: str, change_id: str) -> SessionFileChangeDetail:
        change = require_change(session_id, change_id)
        if not change.undone_at:
            try:
                change = journal.undo(change_id)
            except FileJournalError as exc:
                if exc.code == "already_undone":
                    return read_record(session_id, change_id)
                raise ApiError(mask_secrets(str(exc))[0], status_code=409,
                               code=f"file_change_{exc.code}", retryable=exc.code == "undo_failed") from exc
            except OSError as exc:
                raise ApiError("暂时无法核对或还原文件，记录和原内容备份已保留，请检查文件占用或权限后重试。",
                               status_code=409, code="file_change_io_failed", retryable=True) from exc
        return _detail(journal, change)

    @router.get("/api/v1/agent/sessions/{session_id}/file-changes", response_model=SessionFileChanges)
    async def list_file_changes(session_id: str) -> SessionFileChanges:
        return await asyncio.to_thread(list_records, session_id)

    @router.get("/api/v1/agent/sessions/{session_id}/file-changes/{change_id}", response_model=SessionFileChangeDetail)
    async def get_file_change(session_id: str, change_id: str) -> SessionFileChangeDetail:
        return await asyncio.to_thread(read_record, session_id, change_id)

    @router.post("/api/v1/agent/sessions/{session_id}/file-changes/{change_id}/undo", response_model=SessionFileChangeDetail)
    async def undo_file_change(session_id: str, change_id: str) -> SessionFileChangeDetail:
        return await asyncio.to_thread(undo_record, session_id, change_id)

    return router
