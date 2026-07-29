from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["zhimeng.run-event.v1"] = Field(
        default="zhimeng.run-event.v1",
        alias="schema",
    )
    event_id: str
    event_type: str
    emitted_at: datetime
    data: dict[str, Any]


def new_runtime_event(event_type: str, data: dict[str, Any]) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=str(uuid4()),
        event_type=event_type,
        emitted_at=datetime.now(UTC),
        data=data,
    )


def encode_sse(event: RuntimeEvent) -> str:
    payload = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"


async def iter_runtime_events(
    request: Request,
    *,
    service_name: str,
    version: str,
    ready: bool,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    yield encode_sse(
        new_runtime_event(
            "ready",
            {
                "service": service_name,
                "version": version,
                "ready": ready,
            },
        )
    )

    sequence = 0
    while not await request.is_disconnected():
        await asyncio.sleep(heartbeat_seconds)
        if await request.is_disconnected():
            break
        sequence += 1
        yield encode_sse(
            new_runtime_event(
                "heartbeat",
                {
                    "service": service_name,
                    "sequence": sequence,
                    "ready": ready,
                },
            )
        )
