from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.secret_mask import mask_secrets


class MemoryChangesRequest(BaseModel):
    run_ids: list[str] = Field(max_length=500)


class MemoryChangeRecord(BaseModel):
    # Historical writes can contain multiple lines; current write rules do not rewrite history.
    action: Literal["add", "replace", "remove"] = "add"
    text: str = ""
    old_text: str | None = None
    source_message_id: str | None = None
    evidence: str | None = None
    origin: str
    created_at: str


class MemoryChangesResponse(BaseModel):
    session_id: str
    changes: dict[str, list[MemoryChangeRecord]]
    pending: bool


def read_memory_changes(database, session_id: str, run_ids: list[str]) -> dict:
    ids = list(dict.fromkeys(run_ids))
    with database.connection() as connection:
        if connection.execute("SELECT id FROM execution_session WHERE id=? AND status!='closed'", (session_id,)).fetchone() is None:
            raise ApiError("找不到这条对话。", status_code=404, code="agent_resource_not_found")
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        runs = connection.execute(f"SELECT id FROM run WHERE session_id=? AND id IN ({placeholders})", (session_id, *ids)).fetchall()
        if len(runs) != len(ids):
            raise ApiError("记忆记录不属于当前对话。", status_code=404, code="agent_resource_not_found")
        automatic = connection.execute(
            f"SELECT run_id, payload_json, created_at FROM run_event WHERE run_id IN ({placeholders}) "
            "AND event_type='memory_auto_saved' ORDER BY id", ids,
        ).fetchall()
        explicit = connection.execute(
            f"SELECT run_id, input_json, output_json, finished_at FROM tool_call WHERE run_id IN ({placeholders}) "
            "AND tool_name='memory_write' AND status='succeeded' ORDER BY finished_at", ids,
        ).fetchall()
    result: dict[str, list[dict]] = {}

    def add(run_id, changes, origin, created_at):
        for change in changes:
            record = {**change, "origin": origin, "created_at": created_at}
            for key in ("text", "old_text", "evidence"):
                if isinstance(record.get(key), str):
                    record[key] = mask_secrets(record[key])[0]
            result.setdefault(run_id, []).append(record)

    for row in automatic:
        payload = json.loads(row["payload_json"])
        changes = payload.get("changes", [{"action": "add", "text": line} for line in payload.get("lines", [])])
        add(row["run_id"], changes, "automatic", row["created_at"])
    for row in explicit:
        output = json.loads(row["output_json"] or "{}")
        if output.get("scope") != "user" or output.get("applied") is not True:
            continue
        args = json.loads(row["input_json"])
        add(row["run_id"], output.get("changes", [{"action": "add", "text": args.get("text", "")}]), "tool", row["finished_at"])
    for changes in result.values():
        changes.sort(key=lambda change: change["created_at"])
    return result


def create_memory_changes_router(service):
    router = APIRouter(tags=["agent"])

    @router.post("/api/v1/agent/sessions/{session_id}/memory-changes", response_model=MemoryChangesResponse)
    async def changes(session_id: str, request: MemoryChangesRequest):
        # Observe pending work before reading, so a save during the query gets another poll.
        pending = any(not task.done() for run_id in request.run_ids if (task := service._tasks.get(run_id)) is not None)
        records = await asyncio.to_thread(read_memory_changes, service.store.database, session_id, request.run_ids)
        return MemoryChangesResponse(session_id=session_id, changes=records, pending=pending)

    return router
