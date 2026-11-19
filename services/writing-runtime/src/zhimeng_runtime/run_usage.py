"""Read reported main/compaction usage without rewriting historical messages."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.execution_protocol import MessageUsage


class UsageGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: int = Field(ge=0)
    completed: int = Field(ge=0)
    usage: MessageUsage | None


class RunUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["main_and_tool_compaction"] = "main_and_tool_compaction"
    main: UsageGroup
    compaction: UsageGroup
    total: MessageUsage | None


def _sum_usage(values: list[MessageUsage | None]) -> MessageUsage | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    complete = len(known) == len(values)
    cached = [value.cached_input_tokens for value in known if value.cached_input_tokens is not None]
    return MessageUsage(
        input_tokens=sum(value.input_tokens for value in known),
        output_tokens=sum(value.output_tokens for value in known),
        total_tokens=sum(value.total_tokens for value in known),
        cached_input_tokens=sum(cached) if cached else None,
        input_tokens_reported=complete and all(value.input_tokens_reported for value in known),
        output_tokens_reported=complete and all(value.output_tokens_reported for value in known),
        cached_input_tokens_complete=complete and len(cached) == len(values)
        and all(value.cached_input_tokens_complete for value in known),
    )


def summarize_usage(events: list[tuple[str, dict]]) -> RunUsage:
    groups = []
    all_values = []
    for prefix in ("model_turn", "run_context_summary"):
        # Started/completed are two receipts for one logical request. Never add
        # a final message's cumulative usage to these individual completions.
        requests: dict[tuple, MessageUsage | None] = {}
        completed = set()
        for kind, payload in events:
            if kind not in (prefix + "_started", prefix + "_completed"):
                continue
            identity = (payload["turn"], payload.get("part"))
            requests.setdefault(identity, None)
            if kind.endswith("_completed"):
                completed.add(identity)
                raw = payload.get("usage")
                requests[identity] = MessageUsage.model_validate(raw) if raw is not None else None
        values = list(requests.values())
        all_values.extend(values)
        groups.append(UsageGroup(requests=len(requests), completed=len(completed), usage=_sum_usage(values)))
    return RunUsage(main=groups[0], compaction=groups[1], total=_sum_usage(all_values))


def read_run_usage(database, session_id: str, run_ids: list[str]) -> dict[str, RunUsage]:
    selected = list(dict.fromkeys(run_ids))
    if not selected:
        return {}
    placeholders = ",".join("?" for _ in selected)
    with database.connection() as connection:
        rows = connection.execute(
            f"""SELECT e.run_id, e.event_type, e.payload_json FROM run_event e
                JOIN run r ON r.id = e.run_id
                WHERE r.session_id = ? AND e.run_id IN ({placeholders})
                AND e.event_type IN ('model_turn_started', 'model_turn_completed',
                    'run_context_summary_started', 'run_context_summary_completed')
                ORDER BY e.run_id, e.sequence""",
            (session_id, *selected),
        ).fetchall()
    events = defaultdict(list)
    for row in rows:
        events[row["run_id"]].append((row["event_type"], json.loads(row["payload_json"])))
    return {run_id: summarize_usage(items) for run_id, items in events.items()}
