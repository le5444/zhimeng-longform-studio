from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.secret_mask import mask_secrets


class PlanReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class SessionPlan(BaseModel):
    run_id: str
    run_ordinal: int
    run_status: RunStatus
    tool_call_id: str
    updated_at: str
    source: Literal["tool_result", "legacy_arguments"]
    status_source: Literal["agent"] = "agent"
    items: list[SessionPlanItem] = Field(min_length=1, max_length=20)


class SessionPlanResponse(BaseModel):
    session_id: str
    plan: SessionPlan | None


def read_session_plan(database: SqliteStore, session_id: str) -> SessionPlanResponse:
    """Project the last confirmed plan without keeping a second mutable copy."""
    with closing(sqlite3.connect(Path(database.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if connection.execute(
            "SELECT 1 FROM execution_session WHERE id = ? AND status != 'closed'", (session_id,),
        ).fetchone() is None:
            raise LookupError("Conversation no longer exists or was not found")
        row = connection.execute(
            """
            SELECT t.id AS tool_call_id, t.input_json, t.output_json, t.finished_at,
                   r.id AS run_id, r.ordinal AS run_ordinal, r.status AS run_status
            FROM run r JOIN tool_call t ON t.run_id = r.id
            JOIN activity a ON a.id = t.result_activity_id
            WHERE r.session_id = ? AND t.tool_name = 'plan_update' AND t.status = 'succeeded'
            ORDER BY r.ordinal DESC, a.sequence DESC LIMIT 1
            """, (session_id,),
        ).fetchone()
    if row is None:
        return SessionPlanResponse(session_id=session_id, plan=None)
    output = json.loads(row["output_json"]) if row["output_json"] is not None else None
    legacy = output is None or output == {}
    payload = json.loads(row["input_json"]) if legacy else output
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Saved plan content is unavailable")
    plan = SessionPlan(
        run_id=row["run_id"], run_ordinal=row["run_ordinal"], run_status=row["run_status"],
        tool_call_id=row["tool_call_id"], updated_at=row["finished_at"],
        source="legacy_arguments" if legacy else "tool_result", items=payload["items"],
    )
    for item in plan.items:
        item.content = mask_secrets(item.content)[0]
    return SessionPlanResponse(session_id=session_id, plan=plan)
