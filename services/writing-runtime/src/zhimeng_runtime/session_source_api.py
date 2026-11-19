from __future__ import annotations

import asyncio
import sqlite3
import threading
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.session_recall import SearchCursorError, SessionRecall, SessionSearchArguments, SourceChangedError


class SourceMessage(BaseModel):
    session_id: str
    session_title: str
    project_id: str | None
    project_name: str | None
    message_id: str
    ordinal: int
    role: Literal["user", "assistant"]
    created_at: str
    run_id: str
    run_status: str
    origin_kind: str
    content: str
    start_offset: int
    end_offset: int
    total_characters: int
    next_start_offset: int | None
    masked_secrets: int
    offset_basis: Literal["masked_text"]


class SessionSourceResponse(BaseModel):
    message: SourceMessage
    content_hash: str
    previous_message_id: str | None
    next_message_id: str | None


class SessionHistorySearchResponse(BaseModel):
    query: str
    scope: Literal["project", "all"]
    project_id: str | None
    matches: list[SourceMessage]
    has_more: bool
    next_before_message_id: str | None


def create_session_source_router(service):
    router = APIRouter(tags=["agent"])
    recall = SessionRecall(service.store.database)

    @router.get("/api/v1/agent/history/search", response_model=SessionHistorySearchResponse)
    async def search(query: str = Query(min_length=1, max_length=500), scope: Literal["project", "all"] = "all",
                     project_id: str | None = Query(default=None, min_length=1, max_length=200),
                     limit: int = Query(default=20, ge=1, le=50),
                     before_message_id: str | None = Query(default=None, min_length=1, max_length=200)):
        cancelled = threading.Event()
        try:
            args = SessionSearchArguments(query=query, scope=scope, limit=limit, before_message_id=before_message_id)
            return await asyncio.to_thread(recall.search_messages, args, cancelled, project_id=project_id)
        except SearchCursorError as exc:
            raise ApiError("搜索位置已失效，请重新搜索。", status_code=400, code="session_search_cursor_invalid") from exc
        except ValueError as exc:
            raise ApiError(str(exc), status_code=400, code="session_search_invalid") from exc
        except (sqlite3.Error, OSError) as exc:
            raise ApiError("暂时无法搜索历史正文，请重试。", status_code=503, code="session_search_unavailable", retryable=True) from exc
        finally:
            cancelled.set()

    @router.get("/api/v1/agent/sessions/{session_id}/messages/{message_id}/source", response_model=SessionSourceResponse)
    async def source(session_id: str, message_id: str, start_offset: int = Query(default=0, ge=0),
                     expected_hash: str | None = Query(default=None, pattern="^[0-9a-f]{64}$")):
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(recall.source, session_id, message_id, cancelled,
                                           start_offset=start_offset, expected_hash=expected_hash)
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code="session_source_not_found") from exc
        except SourceChangedError as exc:
            raise ApiError(str(exc), status_code=409, code="session_source_changed") from exc
        except ValueError as exc:
            raise ApiError(str(exc), status_code=400, code="session_source_invalid") from exc
        except (sqlite3.Error, OSError) as exc:
            raise ApiError("暂时无法读取原消息，请重试。", status_code=503, code="session_source_unavailable", retryable=True) from exc
        finally:
            cancelled.set()

    return router
