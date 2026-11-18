"""Read-only conversation observation; visiting this API never consumes facts."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from zhimeng_runtime.command_completion import CommandCompletionStore
from zhimeng_runtime.agent_store import AgentStoreNotFound
from zhimeng_runtime.execution_protocol import Run
from zhimeng_runtime.errors import ApiError


class CommandBackgroundEvent(BaseModel):
    sequence: int
    event_id: str
    command_session_id: str
    owner_session_id: str
    owner_run_id: str
    status: str
    exit_code: int | None
    ended_at: float | None
    delivery_state: Literal["pending", "delivered", "suppressed"]
    delivered_run_id: str | None = None
    payload: dict[str, Any]


class CommandBackgroundResponse(BaseModel):
    events: list[CommandBackgroundEvent]
    next_cursor: int
    active_run: Run | None
    latest_run: Run | None
    has_running_commands: bool
    pending_count: int


def create_command_background_router(service) -> APIRouter:
    router = APIRouter(tags=["agent"])

    @router.get("/api/v1/agent/sessions/{session_id}/background-events", response_model=CommandBackgroundResponse)
    async def background_events(session_id: str, after: int = Query(default=0, ge=0)):
        try:
            await asyncio.to_thread(service.store.get_session, session_id)
        except AgentStoreNotFound:
            raise ApiError("找不到这条对话。", status_code=404, code="agent_resource_not_found", retryable=False) from None
        store = CommandCompletionStore(service.store.database)
        events = await asyncio.to_thread(store.list_events, session_id, after=after)
        pending = await asyncio.to_thread(store.pending, session_id)
        runs = await asyncio.to_thread(service.store.list_runs, session_id, limit=1)
        latest = runs[0] if runs else None
        active = latest if latest and latest.status not in {"succeeded", "failed", "cancelled", "interrupted"} else None
        manager = service.tool_executor._command_session_manager() if service.tool_executor is not None else None
        commands = await asyncio.to_thread(manager.summaries, session_id) if manager else []
        return CommandBackgroundResponse(
            events=[CommandBackgroundEvent.model_validate(item) for item in events],
            next_cursor=max([after, *[item["sequence"] for item in events]]),
            active_run=active, latest_run=latest, pending_count=len(pending),
            has_running_commands=any(item["status"] == "running" for item in commands),
        )

    return router
