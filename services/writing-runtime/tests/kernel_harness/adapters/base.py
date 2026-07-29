from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, Protocol, TextIO

from pydantic import Field, JsonValue, model_validator

from kernel_harness.models import CleanupStatus, EvidenceReference
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    ExecutorIdentity,
    StableId,
    StrictModel,
)


ADAPTER_STREAM_EVIDENCE_LIMIT_BYTES = 8 * 1024 * 1024
EVIDENCE_ROOT_LIMIT_BYTES = 16 * 1024 * 1024
EVENT_BUFFER_ITEM_LIMIT = 256
EVENT_BUFFER_BYTE_LIMIT = 2 * 1024 * 1024
EVENT_PAYLOAD_BYTE_LIMIT = 256 * 1024


class KernelSessionRef(StrictModel):
    upstream_session_id: str
    normalized_session_id: StableId
    persistent: bool


class KernelRunRef(StrictModel):
    upstream_session_id: str
    upstream_run_id: str
    normalized_run_id: StableId


class KernelSkill(StrictModel):
    name: str
    path: str
    enabled: bool
    metadata: JsonValue


class KernelEnvelope(StrictModel):
    sequence: int = Field(ge=1)
    kind: str
    method: str | None
    request_id: str | int | None
    payload: JsonValue


class EvidenceByteBudget:
    def __init__(self, limit_bytes: int = ADAPTER_STREAM_EVIDENCE_LIMIT_BYTES) -> None:
        if limit_bytes < 1:
            raise ValueError("evidence byte limit must be positive")
        self.limit_bytes = limit_bytes
        self.bytes_written = 0
        self.truncated = False

    def write_text(self, handle: TextIO, value: str) -> None:
        payload = value.encode("utf-8")
        remaining = self.limit_bytes - self.bytes_written
        if len(payload) <= remaining:
            handle.write(value)
            self.bytes_written += len(payload)
            handle.flush()
            return
        self.truncated = True
        if remaining <= 0:
            return
        prefix = payload[:remaining].decode("utf-8", errors="ignore")
        handle.write(prefix)
        self.bytes_written += len(prefix.encode("utf-8"))
        handle.flush()


class BoundedEventBuffer:
    def __init__(
        self,
        *,
        item_limit: int = EVENT_BUFFER_ITEM_LIMIT,
        byte_limit: int = EVENT_BUFFER_BYTE_LIMIT,
        per_event_byte_limit: int = EVENT_PAYLOAD_BYTE_LIMIT,
    ) -> None:
        if min(item_limit, byte_limit, per_event_byte_limit) < 1:
            raise ValueError("event buffer limits must be positive")
        self.item_limit = item_limit
        self.byte_limit = byte_limit
        self.per_event_byte_limit = per_event_byte_limit
        self._queue: asyncio.Queue[tuple[KernelEnvelope | None, int]] = asyncio.Queue(
            maxsize=item_limit
        )
        self.current_bytes = 0
        self.overflowed = False
        self.closed = False

    def put(self, event: KernelEnvelope) -> None:
        if self.closed:
            raise RuntimeError("event buffer is closed")
        size = len(event.model_dump_json(by_alias=True).encode("utf-8"))
        if size > self.per_event_byte_limit:
            self.overflowed = True
            raise RuntimeError("kernel event exceeds the per-event byte limit")
        if self.current_bytes + size > self.byte_limit:
            self.overflowed = True
            raise RuntimeError("kernel event buffer exceeds its total byte limit")
        try:
            self._queue.put_nowait((event, size))
        except asyncio.QueueFull as exc:
            self.overflowed = True
            raise RuntimeError("kernel event buffer exceeds its item limit") from exc
        self.current_bytes += size

    async def get(self) -> KernelEnvelope | None:
        event, size = await self._queue.get()
        self.current_bytes -= size
        return event

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self._queue.put_nowait((None, 0))
                return
            except asyncio.QueueFull:
                self.overflowed = True
                _, size = self._queue.get_nowait()
                self.current_bytes -= size

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "item_limit": self.item_limit,
            "byte_limit": self.byte_limit,
            "per_event_byte_limit": self.per_event_byte_limit,
            "queued_items": self._queue.qsize(),
            "queued_bytes": self.current_bytes,
            "overflowed": self.overflowed,
        }


class ProbeCheck(StrictModel):
    check_id: StableId
    passed: bool
    summary: str
    evidence_ref: EvidenceReference


class TransportCapabilitySnapshot(StrictModel):
    declared: list[CapabilityName]
    measured: list[CapabilityName]

    @model_validator(mode="after")
    def validate_snapshot(self) -> TransportCapabilitySnapshot:
        if len(self.declared) != len(set(self.declared)):
            raise ValueError("declared transport capabilities must be unique")
        if len(self.measured) != len(set(self.measured)):
            raise ValueError("measured transport capabilities must be unique")
        undeclared = set(self.measured) - set(self.declared)
        if undeclared:
            names = ", ".join(sorted(item.value for item in undeclared))
            raise ValueError(f"measured transport capabilities must be declared: {names}")
        return self


class TransportProbeResult(StrictModel):
    schema_id: Literal["zhimeng.kernel-transport-probe.v2"] = Field(
        default="zhimeng.kernel-transport-probe.v2",
        alias="schema",
    )
    adapter: ExecutorIdentity
    binary_path: str
    binary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capabilities: TransportCapabilitySnapshot
    session: KernelSessionRef | None
    checks: list[ProbeCheck] = Field(min_length=1)
    required_check_ids: list[StableId] = Field(min_length=1)
    metadata: JsonValue

    @model_validator(mode="after")
    def validate_probe(self) -> TransportProbeResult:
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("transport check ids must be unique")
        if len(self.required_check_ids) != len(set(self.required_check_ids)):
            raise ValueError("required transport check ids must be unique")
        missing = set(self.required_check_ids) - set(check_ids)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"transport probe is missing required checks: {names}")
        return self

    @property
    def passed(self) -> bool:
        by_id = {check.check_id: check for check in self.checks}
        return all(
            by_id[check_id].passed for check_id in self.required_check_ids
        ) and all(check.passed for check in self.checks)


class CleanupReport(StrictModel):
    schema_id: Literal["zhimeng.kernel-harness-cleanup.v3"] = Field(
        default="zhimeng.kernel-harness-cleanup.v3",
        alias="schema",
    )
    overall_status: CleanupStatus
    source_fixture_unchanged: bool
    disposable_project_snapshot_unchanged: bool
    write_denial_exercised: bool
    provider_used: bool
    user_root_snapshot_measured: bool
    root_process_close_status: Literal["pass", "fail"]
    process_tree_cleanup_status: Literal["pass", "fail", "not_measured"]
    process_cleanup_error: str | None
    timed_step_deadlines: dict[StableId, int]
    execution_contract: JsonValue
    unmeasured_cleanup_dimensions: list[str]
    resource_limits_passed: bool
    adapter_resource_limits: JsonValue
    evidence_root_byte_limit: int = Field(ge=1)
    evidence_root_bytes_after_adapter_close: int = Field(ge=0)
    evidence_secret_scan_passed: bool
    evidence_secret_value_count: int = Field(ge=0)
    evidence_secret_redaction_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_cleanup(self) -> CleanupReport:
        if self.overall_status is CleanupStatus.NOT_RUN:
            raise ValueError("a cleanup receipt cannot report not_run")
        if len(self.unmeasured_cleanup_dimensions) != len(
            set(self.unmeasured_cleanup_dimensions)
        ):
            raise ValueError("unmeasured cleanup dimensions must be unique")
        if self.root_process_close_status == "pass" and self.process_cleanup_error:
            raise ValueError("successful root process close cannot carry an error")
        if self.root_process_close_status == "fail":
            if self.process_cleanup_error is None or not self.process_cleanup_error.strip():
                raise ValueError("failed root process close requires a non-empty error")
        if not self.evidence_secret_scan_passed:
            raise ValueError("final cleanup receipt requires a passed evidence secret scan")
        if self.evidence_root_byte_limit != EVIDENCE_ROOT_LIMIT_BYTES:
            raise ValueError("cleanup receipt uses an unknown evidence root byte limit")
        if self.evidence_root_bytes_after_adapter_close > self.evidence_root_byte_limit:
            raise ValueError("evidence root bytes exceed the declared limit")
        if self.overall_status is CleanupStatus.CLEAN:
            clean = (
                self.source_fixture_unchanged
                and self.disposable_project_snapshot_unchanged
                and self.root_process_close_status == "pass"
                and self.process_tree_cleanup_status == "pass"
                and not self.unmeasured_cleanup_dimensions
                and self.resource_limits_passed
            )
            if not clean:
                raise ValueError("clean status requires every cleanup dimension to pass")
        return self


class AgentKernelAdapter(Protocol):
    async def start(self) -> ExecutorIdentity: ...

    async def probe(self) -> TransportProbeResult: ...

    async def create_session(self, options: JsonValue) -> KernelSessionRef: ...

    async def resume_session(
        self,
        upstream_session_id: str,
        options: JsonValue,
    ) -> KernelSessionRef: ...

    async def start_run(
        self,
        session: KernelSessionRef,
        inputs: JsonValue,
        options: JsonValue,
    ) -> KernelRunRef: ...

    async def cancel_run(self, run: KernelRunRef) -> None: ...

    async def list_skills(
        self,
        roots: list[str],
        *,
        force_reload: bool = False,
    ) -> list[KernelSkill]: ...

    async def resolve_server_request(
        self,
        request_id: str | int,
        result: JsonValue,
    ) -> None: ...

    def events(self) -> AsyncIterator[KernelEnvelope]: ...

    async def close(self) -> None: ...
