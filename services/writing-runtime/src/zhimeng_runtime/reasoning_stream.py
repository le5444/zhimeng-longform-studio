"""Persist public provider reasoning with stable blocks and bounded write frequency."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from zhimeng_runtime.agent_store import AgentStoreInvalidState, TERMINAL_RUN_STATUSES


class ReasoningStream:
    # These batch database writes; they do not limit model output.
    flush_seconds = 0.15
    flush_characters = 2048

    def __init__(self, store, broker, run_id: str) -> None:
        self.store = store
        self.broker = broker
        self.run_id = run_id
        self.block_id = f"reasoning_{uuid4().hex}"
        self.visibility = "full"
        self.parts: list[str] = []
        self.pending: list[str] = []
        self.pending_characters = 0
        self.started = False
        self.closed = False
        self.timer: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def add(self, text: str, *, visibility: str = "full") -> None:
        if not text:
            return
        self.visibility = visibility
        self.parts.append(text)
        self.pending.append(text)
        self.pending_characters += len(text)
        if not self.started or self.pending_characters >= self.flush_characters:
            await self.flush()
        else:
            if self.timer is not None and self.timer.done():
                self.timer.result()
                self.timer = None
            if self.timer is None:
                self.timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        while self.pending:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()

    async def _persist(self, **payload) -> None:
        # to_thread keeps running when its awaiter is cancelled. Wait for the
        # actual transaction before a terminal snapshot can acquire this lock.
        operation = asyncio.create_task(asyncio.to_thread(
            self.store.append_reasoning, self.run_id, **payload,
        ))
        cancelled = False
        while True:
            try:
                await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def flush(self) -> None:
        async with self.lock:
            if self.closed or not self.pending:
                return
            delta = "".join(self.pending)
            self.pending.clear()
            self.pending_characters = 0
            await self._persist(
                summary="模型正在返回推理摘要" if self.visibility == "summary" else "模型正在返回思考内容",
                content=None, block_id=self.block_id, phase="delta", delta=delta,
                visibility=self.visibility,
            )
            self.started = True
            await self.broker.publish(self.run_id)

    async def finish(self, phase: str = "completed") -> None:
        if self.closed or not self.parts:
            return
        if self.timer is not None:
            # Let an in-progress database write complete before its final snapshot.
            await asyncio.shield(self.timer)
        async with self.lock:
            if self.closed or not self.parts:
                return
            content = "".join(self.parts)
            summary = next((line.strip() for line in content.splitlines() if line.strip()), "模型返回的思考内容")
            if len(summary) > 160:
                summary = summary[:157].rstrip() + "..."
            await self._persist(
                summary=summary, content=content, block_id=self.block_id,
                phase=phase, visibility=self.visibility,
            )
            self.closed = True
            self.pending.clear()
            await self.broker.publish(self.run_id)

    async def finish_preserving_cancellation(self, phase: str) -> None:
        operation = asyncio.create_task(self.finish(phase))
        cancelled = False
        while True:
            try:
                await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                cancelled = True
            except AgentStoreInvalidState:
                run = await asyncio.to_thread(self.store.get_run, self.run_id)
                if run.status not in TERMINAL_RUN_STATUSES:
                    raise
                break
        if cancelled:
            raise asyncio.CancelledError
