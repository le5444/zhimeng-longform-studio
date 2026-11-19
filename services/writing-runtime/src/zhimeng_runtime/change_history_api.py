from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from fastapi import APIRouter, Query

from zhimeng_runtime.change_history import ChangeHistoryDetail, ChangeHistoryPage, HistoryChanged
from zhimeng_runtime.change_set_service import ChangeSetError
from zhimeng_runtime.errors import ApiError


def create_change_history_router(reader):
    router = APIRouter(tags=['file-changes'])

    async def read(callback, *args, **kwargs):
        try:
            return await asyncio.to_thread(callback, *args, **kwargs)
        except (HistoryChanged, ValueError, KeyError, TypeError) as exc:
            raise ApiError('修改记录已变化或暂时无法解析，请刷新。', status_code=409, code='change_history_changed') from exc
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code='change_history_not_found') from exc
        except (sqlite3.Error, RuntimeError, ChangeSetError) as exc:
            raise ApiError('暂时无法读取修改记录，请重试。', status_code=503, code='change_history_unavailable') from exc

    @router.get('/api/v1/agent/sessions/{session_id}/changes', response_model=ChangeHistoryPage)
    async def history(session_id: str, scope: Literal['session', 'project'] = 'session',
                      offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100),
                      revision: str | None = Query(None, max_length=64)):
        return await read(reader.list, session_id, scope=scope, offset=offset, limit=limit, revision=revision)

    @router.get('/api/v1/agent/sessions/{session_id}/changes/{change_id}', response_model=ChangeHistoryDetail)
    async def detail(session_id: str, change_id: str, scope: Literal['session', 'project'] = 'session'):
        return await read(reader.detail, session_id, change_id, scope=scope)

    return router
