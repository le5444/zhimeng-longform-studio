"""Persist answer text in short batches before publishing it to clients."""
from __future__ import annotations

import asyncio

from zhimeng_runtime.agent_store import AgentStoreInvalidState, TERMINAL_RUN_STATUSES


class AssistantStream:
    # Batching controls write frequency, not the length of a model response.
    flush_seconds = 0.1
    flush_characters = 2048

    def __init__(self, store, broker, run_id: str, message_id: str) -> None:
        self.store = store
        self.broker = broker
        self.run_id = run_id
        self.message_id = message_id
        self.pending: list[str] = []
        self.pending_characters = 0
        self.started = False
        self.closed = False
        self.timer: asyncio.Task | None = None
        self.flush_requested = asyncio.Event()
        self.lock = asyncio.Lock()

    async def add(self, text: str) -> None:
        if not text:
            return
        if self.timer is not None and self.timer.done():
            self.timer.result()
            self.timer = None
        self.pending.append(text)
        self.pending_characters += len(text)
        if not self.started or self.pending_characters >= self.flush_characters:
            await self.flush()
        elif self.timer is None:
            self.flush_requested.clear()
            self.timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        while self.pending:
            try:
                await asyncio.wait_for(self.flush_requested.wait(), self.flush_seconds)
            except TimeoutError:
                pass
            await self.flush()

    async def flush(self) -> None:
        async with self.lock:
            if self.closed or not self.pending:
                return
            delta = ''.join(self.pending)
            self.pending.clear()
            self.pending_characters = 0
            operation = asyncio.create_task(asyncio.to_thread(
                self.store.append_assistant_delta, self.run_id,
                message_id=self.message_id, delta=delta,
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
            self.started = True
            await self.broker.publish(self.run_id)
            if cancelled:
                raise asyncio.CancelledError

    async def finish(self) -> None:
        self.flush_requested.set()
        if self.timer is not None:
            await asyncio.shield(self.timer)
        await self.flush()
        self.closed = True

    async def finish_preserving_cancellation(self) -> None:
        operation = asyncio.create_task(self.finish())
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
