from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_api import _iter_run_events


@pytest.mark.parametrize("initial_event_count", [0, 1])
def test_terminal_commit_between_reads_delivers_all_tail_events(initial_event_count: int) -> None:
    """The final transaction can commit after listing events and before reading Run."""
    committed = False
    queried_cursors: list[int] = []
    timestamp = datetime.now(UTC)

    def stored_event(sequence: int) -> SimpleNamespace:
        activity = SimpleNamespace(
            root=SimpleNamespace(created_at=timestamp),
            model_dump=lambda **_: {"sequence": sequence, "content": f"result {sequence}"},
        )
        return SimpleNamespace(sequence=sequence, activity=activity)

    def list_run_events(_run_id: str, *, after_sequence: int, limit: int | None = None) -> list[SimpleNamespace]:
        queried_cursors.append(after_sequence)
        available = 2 if committed else initial_event_count
        # Non-activity run events consume event sequence numbers too.
        return [stored_event(sequence) for sequence in (7, 12)[:available] if sequence > after_sequence]

    def get_run(_run_id: str) -> SimpleNamespace:
        nonlocal committed
        committed = True
        return SimpleNamespace(
            status="succeeded",
            last_activity_sequence=2,
            model_dump=lambda **_: {"status": "succeeded", "last_activity_sequence": 2},
        )

    async def is_disconnected() -> bool:
        return False

    async def collect() -> list[dict]:
        service = SimpleNamespace(
            store=SimpleNamespace(list_run_events=list_run_events, get_run=get_run),
            broker=SimpleNamespace(snapshot=lambda _: 0),
        )
        chunks = [chunk async for chunk in _iter_run_events(
            SimpleNamespace(is_disconnected=is_disconnected),
            service=service,
            run_id="run-tail-race",
            after_sequence=0,
            heartbeat_seconds=0.05,
        )]
        return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines() if line.startswith("data: ")]

    events = asyncio.run(collect())
    assert [event["event_type"] for event in events] == ["activity", "activity", "done"]
    assert [event["event_id"] for event in events] == ["run-tail-race:7", "run-tail-race:12", "run-tail-race:12"]
    assert events[1]["data"]["activity"]["content"] == "result 12"
    assert len(queried_cursors) >= 2


def test_running_replay_drains_bounded_pages_without_waiting_for_new_events() -> None:
    reads = []
    waits = []
    count = 753
    timestamp = datetime.now(UTC)

    def read(_run_id, *, after_sequence, limit=None):
        assert limit is not None and 0 < limit <= 250, 'replay must not materialize the entire backlog'
        numbers = list(range(3, count * 3 + 1, 3))
        selected = [n for n in numbers if n > after_sequence][:limit]
        reads.append(selected)
        return [SimpleNamespace(sequence=n, activity=SimpleNamespace(
            root=SimpleNamespace(created_at=timestamp),
            model_dump=lambda n=n, **_: {'sequence': n},
        )) for n in selected]

    async def wait(*args, **kwargs):
        waits.append(True)
        assert sum(map(len, reads)) == count, 'backlog must drain before waiting for the heartbeat'
        return False

    async def disconnected():
        return bool(waits)

    async def collect():
        service = SimpleNamespace(store=SimpleNamespace(list_run_events=read, get_run=lambda _: SimpleNamespace(status='running')),
                                  broker=SimpleNamespace(snapshot=lambda _: 0, wait_for_change=wait))
        return [chunk async for chunk in _iter_run_events(SimpleNamespace(is_disconnected=disconnected),
            service=service, run_id='replay', after_sequence=0, heartbeat_seconds=15)]

    chunks = asyncio.run(collect())
    # 一批共享传输块，各自的 SSE 帧和编号仍完整存在。
    assert len(chunks) == 4
    assert sum(line.startswith('data: ') for chunk in chunks for line in chunk.splitlines()) == count
    assert [n for batch in reads for n in batch] == list(range(3, count * 3 + 1, 3))
    assert len(reads) == 4 and len(waits) == 1


def test_disconnect_during_page_read_does_not_serialize_or_deliver_that_page() -> None:
    disconnected = False

    def read(*args, **kwargs):
        nonlocal disconnected
        disconnected = True
        # Deliberately no activity data: a disconnected reader must not serialize it.
        return [SimpleNamespace(sequence=1)]

    async def is_disconnected():
        return disconnected

    async def collect():
        service = SimpleNamespace(store=SimpleNamespace(list_run_events=read, get_run=lambda _: SimpleNamespace(status='running')),
                                  broker=SimpleNamespace(snapshot=lambda _: 0))
        return [chunk async for chunk in _iter_run_events(SimpleNamespace(is_disconnected=is_disconnected),
            service=service, run_id='replay', after_sequence=0, heartbeat_seconds=15)]

    assert asyncio.run(collect()) == []
