"""Cancellation returns both the provider request slot and any half-open probe."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from zhimeng_runtime import agent_provider
from zhimeng_runtime.agent_provider import (
    AgentProviderCancelled,
    OpenAICompatibleAgentProvider,
    ProviderCompleted,
    _acquire_limiter,
    _circuit_breaker_for,
    _concurrency_limiter_for,
)


class ObservedSemaphore(asyncio.BoundedSemaphore):
    """Expose arrival without sleeps; BoundedSemaphore catches double returns."""

    def __init__(self) -> None:
        super().__init__(1)
        self.entered = asyncio.Event()

    async def acquire(self) -> bool:
        self.entered.set()
        return await super().acquire()


@asynccontextmanager
async def cancellation_timeout(deadlines: list[asyncio.Timeout]):
    async with asyncio.timeout(None) as deadline:
        deadlines.append(deadline)
        yield


def cancel_waiter(
    mode: str,
    task: asyncio.Task,
    cancel_event: asyncio.Event,
    deadlines: list[asyncio.Timeout],
) -> type[BaseException]:
    if mode == "event":
        cancel_event.set()
        return AgentProviderCancelled
    if mode == "timeout":
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        return TimeoutError
    task.cancel()
    return asyncio.CancelledError


@pytest.mark.parametrize("mode", ["event", "task", "timeout"])
@pytest.mark.parametrize("release_before_cancel", [False, True])
def test_cancelled_waiter_returns_exactly_one_slot(
    mode: str, release_before_cancel: bool
) -> None:
    async def scenario() -> None:
        limiter = ObservedSemaphore()
        await limiter.acquire()  # An active request holds the only slot.
        limiter.entered.clear()
        cancel_event = asyncio.Event()
        deadlines: list[asyncio.Timeout] = []

        async def wait_for_slot() -> None:
            async with cancellation_timeout(deadlines):
                await _acquire_limiter(limiter, cancel_event)

        waiter = asyncio.create_task(wait_for_slot())
        await limiter.entered.wait()
        if release_before_cancel:
            # Wake Semaphore.acquire in the same event-loop tick as cancellation.
            limiter.release()
        error_type = cancel_waiter(mode, waiter, cancel_event, deadlines)
        with pytest.raises(error_type):
            await waiter
        if not release_before_cancel:
            limiter.release()

        # A leaked old waiter consumes this release; a double return raises above.
        await asyncio.wait_for(_acquire_limiter(limiter, asyncio.Event()), 1)
        assert limiter.locked()
        limiter.release()
        assert limiter._value == 1
        assert not limiter._waiters

    asyncio.run(scenario())


def test_cancellation_during_listener_cleanup_returns_acquired_slot() -> None:
    async def scenario() -> None:
        cleanup_started = asyncio.Event()

        class SlowCleanupEvent(asyncio.Event):
            async def wait(self) -> bool:
                try:
                    return await super().wait()
                finally:
                    cleanup_started.set()
                    await asyncio.Event().wait()

        limiter = ObservedSemaphore()
        waiter = asyncio.create_task(_acquire_limiter(limiter, SlowCleanupEvent()))
        await cleanup_started.wait()
        assert limiter.locked()  # Acquired, but not yet transferred to the caller.
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(_acquire_limiter(limiter, asyncio.Event()), 1)
        assert limiter.locked()
        limiter.release()
        assert limiter._value == 1

    asyncio.run(asyncio.wait_for(scenario(), 3))


def make_provider() -> OpenAICompatibleAgentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    return OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="test-model",
        api_key=None,
        credential_reference=None,
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        max_concurrent_requests=1,
        circuit_breaker_threshold=1,
        circuit_breaker_cooldown_seconds=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("mode", ["event", "task", "timeout"])
def test_cancelled_half_open_waiter_allows_another_probe(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        provider = make_provider()
        breaker = _circuit_breaker_for(provider._resilience_key)
        breaker.on_failure(countable=True, threshold=1)
        # Advance only the circuit state, without a timing-sensitive sleep.
        breaker._opened_at -= 2
        limiter = _concurrency_limiter_for(provider._resilience_key, 1)
        await limiter.acquire()
        entered = asyncio.Event()
        original_acquire = agent_provider._acquire_limiter

        async def observed_acquire(semaphore, cancel_event):
            entered.set()
            await original_acquire(semaphore, cancel_event)

        monkeypatch.setattr(agent_provider, "_acquire_limiter", observed_acquire)
        cancel_event = asyncio.Event()
        deadlines: list[asyncio.Timeout] = []

        async def collect():
            async with cancellation_timeout(deadlines):
                return [
                    event
                    async for event in provider.stream_chat(
                        [{"role": "user", "content": "hello"}],
                        cancel_event=cancel_event,
                    )
                ]

        waiter = asyncio.create_task(collect())
        await entered.wait()
        assert breaker._probe_inflight
        error_type = cancel_waiter(mode, waiter, cancel_event, deadlines)
        with pytest.raises(error_type):
            await waiter
        assert not breaker._probe_inflight
        limiter.release()

        events = [
            event
            async for event in provider.stream_chat(
                [{"role": "user", "content": "continue"}],
                cancel_event=asyncio.Event(),
            )
        ]
        assert any(isinstance(event, ProviderCompleted) for event in events)
        assert breaker._opened_at is None
        assert limiter._value == 1
        await provider._client.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 3))
