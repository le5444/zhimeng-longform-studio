"""Drain database users before replacing the runtime's persistent state."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


RESTORE_PATH = "/api/v1/runtime/backups/restore"
T = TypeVar("T")


class RuntimeMaintenance:
    def __init__(self) -> None:
        self.active = False
        self.generation = 0
        self._lock = asyncio.Lock()
        self._requests: set[asyncio.Task] = set()
        self._streams: set[asyncio.Task] = set()
        self._interrupted: set[asyncio.Task] = set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._background: set[asyncio.Task] = set()

    def start_background(self, operation: Callable[[], Awaitable[T]], *, name: str) -> asyncio.Task[T]:
        task = asyncio.create_task(operation(), name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def drain_background(self) -> None:
        # Let worker threads finish as well; cancelling the coroutine alone
        # cannot prevent its thread from accessing the replaced database.
        while self._background:
            await asyncio.gather(*tuple(self._background), return_exceptions=True)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async def execute() -> T:
            async with self.exclusive():
                return await operation()

        worker = asyncio.create_task(execute())
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A disconnected caller cannot abandon a database replacement.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise

    @asynccontextmanager
    async def exclusive(self):
        async with self._lock:
            self.active = True
            try:
                # Event streams may stay open indefinitely. Their readers reconnect
                # after maintenance; ordinary requests finish their current writes.
                for task in tuple(self._streams):
                    self._interrupted.add(task)
                    task.cancel()
                await self._drained.wait()
                await self.drain_background()
                yield
            finally:
                self.generation += 1
                self.active = False


class RuntimeMaintenanceMiddleware:
    def __init__(self, app: ASGIApp, maintenance: RuntimeMaintenance) -> None:
        self.app = app
        self.maintenance = maintenance

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = self.maintenance
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if state.active:
            await JSONResponse(
                {"error": {"code": "runtime_restoring", "message": "正在恢复备份，完成后可继续操作。", "retryable": True}},
                status_code=503, headers={"Retry-After": "1"},
            )(scope, receive, send)
            return
        if scope["path"] == RESTORE_PATH and scope["method"] == "POST":
            await self.app(scope, receive, send)
            return
        task = asyncio.current_task()
        assert task is not None
        state._requests.add(task)
        state._drained.clear()
        finished = False

        async def observe(message: Message) -> None:
            nonlocal finished
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                if headers.get(b"content-type", b"").startswith(b"text/event-stream"):
                    state._streams.add(task)
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True
            if state.active and task in state._streams and not finished:
                state._interrupted.add(task)
                raise asyncio.CancelledError

        try:
            await self.app(scope, receive, observe)
        except asyncio.CancelledError:
            if task not in state._interrupted:
                raise
            if not finished:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            state._requests.discard(task)
            state._streams.discard(task)
            state._interrupted.discard(task)
            if not state._requests:
                state._drained.set()
