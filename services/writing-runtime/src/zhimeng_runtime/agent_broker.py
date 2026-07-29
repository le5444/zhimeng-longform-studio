from __future__ import annotations

import asyncio


class AgentEventBroker:
    """Wake connected streams after durable events commit."""

    def __init__(self) -> None:
        self._conditions: dict[str, asyncio.Condition] = {}
        self._generations: dict[str, int] = {}

    def snapshot(self, run_id: str) -> int:
        return self._generations.get(run_id, 0)

    async def publish(self, run_id: str) -> None:
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            self._generations[run_id] = self.snapshot(run_id) + 1
            condition.notify_all()

    async def wait_for_change(
        self,
        run_id: str,
        generation: int,
        *,
        timeout: float,
    ) -> bool:
        condition = self._conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            if self.snapshot(run_id) != generation:
                return True
            try:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self.snapshot(run_id) != generation),
                    timeout=timeout,
                )
            except TimeoutError:
                return False
        return True
