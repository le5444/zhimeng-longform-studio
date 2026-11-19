from __future__ import annotations

import asyncio
import sqlite3
import threading

from fastapi import APIRouter, Query, Request

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.run_artifacts import ArtifactRevisionChanged, RunArtifactsResponse, read_run_artifacts


def create_run_artifacts_router(service):
    router = APIRouter(tags=['agent'])

    @router.get('/api/v1/agent/sessions/{session_id}/runs/{run_id}/artifacts', response_model=RunArtifactsResponse)
    async def artifacts(request: Request, session_id: str, run_id: str, offset: int = Query(0, ge=0),
                        limit: int = Query(20, ge=1, le=50), revision: str | None = Query(None, max_length=64)):
        cancelled = threading.Event()
        job = asyncio.create_task(asyncio.to_thread(read_run_artifacts, service.store.database, session_id, run_id,
            offset=offset, limit=limit, revision=revision, cancelled=cancelled))
        try:
            while not job.done():
                await asyncio.wait({job}, timeout=0.2)
                if await request.is_disconnected():
                    raise asyncio.CancelledError
            return await job
        except ArtifactRevisionChanged as exc:
            raise ApiError(str(exc), status_code=409, code='run_artifacts_changed') from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError('已保存的文件记录无法解析，暂时不能核对。', status_code=409, code='run_artifacts_invalid') from exc
        except LookupError as exc:
            raise ApiError(str(exc), status_code=404, code='run_artifacts_not_found') from exc
        except sqlite3.Error as exc:
            raise ApiError('暂时无法读取文件记录，请重试。', status_code=503, code='run_artifacts_unavailable') from exc
        finally:
            cancelled.set()
            if not job.done():
                job.cancel()

    return router
