from __future__ import annotations

import asyncio
import json

from zhimeng_runtime.events import iter_runtime_events


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _event_payload(frame: str) -> dict[str, object]:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def test_event_iterator_emits_ready_then_heartbeat() -> None:
    async def collect() -> tuple[str, str]:
        stream = iter_runtime_events(
            ConnectedRequest(),  # type: ignore[arg-type]
            service_name="test-runtime",
            version="0.1.0",
            ready=True,
            heartbeat_seconds=0.001,
        )
        try:
            first = await anext(stream)
            second = await anext(stream)
            return first, second
        finally:
            await stream.aclose()

    ready_frame, heartbeat_frame = asyncio.run(collect())

    assert "event: ready" in ready_frame
    assert _event_payload(ready_frame)["data"]["ready"] is True
    assert "event: heartbeat" in heartbeat_frame
    assert _event_payload(heartbeat_frame)["data"]["sequence"] == 1
