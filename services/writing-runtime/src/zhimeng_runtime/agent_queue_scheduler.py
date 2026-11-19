"""Promote durable user inputs while the runtime is alive, without a browser."""
from __future__ import annotations

import asyncio
import logging
from zhimeng_runtime.command_completion import CommandCompletionStore

from zhimeng_runtime.agent_queue import AgentQueueService
from zhimeng_runtime.agent_store import AgentStoreConflict, AgentStoreNotFound

logger = logging.getLogger(__name__)


class AgentQueueScheduler:
    def __init__(self, queue: AgentQueueService, *, poll_seconds: float = 1.0):
        self.queue = queue
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._dispatching: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="agent-queue-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending = tuple(self._dispatching.values())
        for dispatch in pending:
            dispatch.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._dispatching.clear()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick(wait=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unable to inspect saved Agent queues")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, *, wait: bool = True) -> None:
        executor = self.queue.agent.tool_executor
        if executor is not None and hasattr(executor, "_command_session_manager"):
            manager = executor._command_session_manager()
            await asyncio.to_thread(manager.reconcile_completions)
        session_ids = await asyncio.to_thread(self.queue.store.pending_session_ids)
        completions = CommandCompletionStore(self.queue.store.database)
        session_ids = list(dict.fromkeys([*session_ids, *await asyncio.to_thread(completions.pending_session_ids)]))
        scheduled = []
        for session_id in session_ids:
            task = self._dispatching.get(session_id)
            if task is None or task.done():
                task = asyncio.create_task(self._dispatch_session(session_id), name=f"agent-queue-dispatch:{session_id}")
                self._dispatching[session_id] = task
                task.add_done_callback(lambda done, sid=session_id: self._dispatch_finished(sid, done))
            scheduled.append(task)
        if wait and scheduled:
            await asyncio.gather(*scheduled)

    def _dispatch_finished(self, session_id: str, task: asyncio.Task) -> None:
        if self._dispatching.get(session_id) is task:
            self._dispatching.pop(session_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Unable to finish saved queue dispatch: session=%s", session_id,
                         exc_info=(type(error), error, error.__traceback__))

    async def _dispatch_session(self, session_id: str) -> None:
        item_id = None
        completion_ids = []
        try:
            state = await asyncio.to_thread(self.queue.store.read, session_id)
            if state.pause_reason or state.active_run_id:
                return
            if await self.queue.execution_pending(session_id):
                return
            if not state.items:
                pending = await asyncio.to_thread(CommandCompletionStore(self.queue.store.database).pending, session_id)
                completion_ids = [item["event_id"] for item in pending]
                if not completion_ids:
                    return
                await self.queue.refresh_project(session_id)
                async with self.queue.lock_for(session_id):
                    state = await asyncio.to_thread(self.queue.store.read, session_id)
                    if state.items or state.pause_reason or state.active_run_id or await self.queue.execution_pending(session_id):
                        return
                    await self.queue.refresh_provider(session_id)
                    await self.queue.agent.start_completion_run(session_id)
                return
            item = state.items[0]
            if item.delivery == "steer_pending":
                return
            item_id = item.item_id
            await self.queue.dispatch(session_id, item_id, refresh_project=True)
        except (AgentStoreConflict, AgentStoreNotFound):
            # A browser send/steer, pause or project switch can win the race.
            # If the same input remains eligible, leave it paused for repair.
            if item_id:
                current = await asyncio.to_thread(self.queue.store.read, session_id)
                if current.items and current.items[0].item_id == item_id and not current.active_run_id and not current.pause_reason and not await self.queue.execution_pending(session_id):
                    await asyncio.to_thread(self.queue.store.failed, session_id, item_id)
                    logger.warning("Saved Agent queue needs attention: session=%s item=%s", session_id, item_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if item_id:
                await asyncio.to_thread(self.queue.store.failed, session_id, item_id)
            elif completion_ids:
                await asyncio.to_thread(CommandCompletionStore(self.queue.store.database).defer_automatic, completion_ids, str(exc))
            logger.exception("Saved Agent queue dispatch failed: session=%s item=%s", session_id, item_id)
