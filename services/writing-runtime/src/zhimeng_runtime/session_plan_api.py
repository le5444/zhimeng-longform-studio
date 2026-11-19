from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.session_plan import SessionPlanResponse, read_session_plan


def create_session_plan_router(service):
    router = APIRouter(tags=["agent"])

    @router.get("/api/v1/agent/sessions/{session_id}/plan", response_model=SessionPlanResponse)
    async def plan(session_id: str):
        try:
            return await asyncio.to_thread(read_session_plan, service.store.database, session_id)
        except LookupError as exc:
            raise ApiError("对话不存在或已经关闭。", status_code=404, code="session_plan_not_found") from exc
        except ValueError as exc:
            raise ApiError("已保存的计划无法读取。", status_code=409, code="session_plan_invalid") from exc
        except sqlite3.Error as exc:
            raise ApiError("暂时无法读取工作计划，请重试。", status_code=503, code="session_plan_unavailable") from exc

    return router
