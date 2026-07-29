from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LiveResponse(ApiModel):
    schema_id: Literal["zhimeng.health.v1"] = Field(
        default="zhimeng.health.v1",
        alias="schema",
    )
    service: str
    version: str
    status: Literal["live"]
    timestamp: datetime


class ReadyResponse(ApiModel):
    schema_id: Literal["zhimeng.health.v1"] = Field(
        default="zhimeng.health.v1",
        alias="schema",
    )
    service: str
    version: str
    status: Literal["ready"]
    database_schema_version: int
    timestamp: datetime


class DatabaseStatus(ApiModel):
    ready: bool
    journal_mode: str
    schema_version: int = Field(ge=0)


class RuntimeStatusResponse(ApiModel):
    schema_id: Literal["zhimeng.runtime-status.v1"] = Field(
        default="zhimeng.runtime-status.v1",
        alias="schema",
    )
    service: str
    version: str
    environment: str
    state: Literal["starting", "ready", "degraded"]
    started_at: datetime
    uptime_seconds: float = Field(ge=0)
    database: DatabaseStatus
    event_transport: Literal["sse"] = "sse"
