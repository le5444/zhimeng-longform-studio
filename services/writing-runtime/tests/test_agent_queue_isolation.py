"""A slow preparation in one conversation must not freeze another's controls."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient

from test_agent_queue import enqueue, session, update
from test_agent_queue_scheduler import QueueProvider, eventually, runs, scheduler
from test_agent_steering import start
from zhimeng_runtime.app import create_app


def hold_project_refresh(client, held_session, monkeypatch):
    queue = client.app.state.agent_queue_service
    entered = threading.Event()
    release = asyncio.Event()
    original = queue.refresh_project

    async def refresh(session_id):
        if session_id == held_session:
            entered.set()
            await release.wait()
        await original(session_id)

    monkeypatch.setattr(queue, "refresh_project", refresh)
    return entered, release


def test_slow_session_does_not_block_other_session_atomic_steer(settings, monkeypatch):
    provider = QueueProvider(block=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        client.portal.call(scheduler(client).stop)
        first, second = session(client), session(client)
        rid = start(client, second)
        assert provider.entered.wait(10)
        enqueue(client, first, "slow-prepare-001")
        entered, release = hold_project_refresh(client, first, monkeypatch)
        queue = client.app.state.agent_queue_service

        async def slow_dispatch():
            return await queue.dispatch(first, "slow-prepare-001", refresh_project=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(client.portal.call, slow_dispatch)
            assert entered.wait(10)
            try:
                steer = pool.submit(client.post, f"/api/v1/agent/sessions/{second}/queue/other-steer-001/steer",
                                    json={"expected_run_id": rid, "input": {"item_id": "other-steer-001", "message": "只修改我的这条对话。"}})
                response = steer.result(timeout=3)
                assert response.status_code == 200, response.text
                assert response.json()["queue"]["active_run_id"] == rid
                assert not slow.done()
            finally:
                client.portal.call(release.set)
                provider.release.set()
                slow.result(timeout=10)
        eventually(lambda: all(item.status == "succeeded" for item in runs(client, second)))
        users = [item.content for item in client.app.state.agent_service.store.list_messages(second) if item.role == "user"]
        assert "只修改我的这条对话。" in users
        assert all("只修改我的这条对话。" not in item.content for item in client.app.state.agent_service.store.list_messages(first))


def test_background_session_progresses_while_other_project_refresh_waits(settings, monkeypatch):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        first, second = session(client), session(client)
        enqueue(client, first, "background-slow-001")
        enqueue(client, second, "background-independent-001")
        entered, release = hold_project_refresh(client, first, monkeypatch)
        client.portal.call(worker.start)
        try:
            assert entered.wait(10)
            # No HTTP polling/dispatch/streaming is required after enqueueing.
            eventually(lambda: runs(client, second) and runs(client, second)[0].status == "succeeded", timeout=3)
            assert not runs(client, first)
            assert len(provider.requests) == 1
        finally:
            client.portal.call(release.set)
        eventually(lambda: runs(client, first) and runs(client, first)[0].status == "succeeded")


def test_stopping_scheduler_drains_owned_preparations_and_restart_sends_once(settings, monkeypatch):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        sid = session(client)
        enqueue(client, sid, "shutdown-prepare-001")
        entered, release = hold_project_refresh(client, sid, monkeypatch)
        client.portal.call(worker.start)
        assert entered.wait(10)
        client.portal.call(worker.stop)
        assert worker._dispatching == {}
        assert not runs(client, sid)
        state = client.app.state.agent_queue_service.store.read(sid)
        assert len(state.items) == 1 and not state.items[0].sending
        client.portal.call(release.set)
        client.portal.call(worker.start)
        eventually(lambda: runs(client, sid) and runs(client, sid)[0].status == "succeeded")
        assert len(runs(client, sid)) == 1
        assert len(provider.requests) == 1


def test_pause_can_stop_own_queue_while_project_preparation_is_still_waiting(settings, monkeypatch):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        sid = session(client)
        enqueue(client, sid, "pause-during-prepare-001")
        entered, release = hold_project_refresh(client, sid, monkeypatch)
        client.portal.call(worker.start)
        assert entered.wait(10)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pause = pool.submit(update, client, sid, "pause")
            try:
                result = pause.result(timeout=3)
                assert result["pause_reason"] == "user"
                assert not runs(client, sid)
            finally:
                client.portal.call(release.set)
        client.portal.call(worker.tick)
        assert not runs(client, sid)
        state = client.app.state.agent_queue_service.store.read(sid)
        assert state.pause_reason == "user" and len(state.items) == 1
        update(client, sid, "resume")
        eventually(lambda: runs(client, sid) and runs(client, sid)[0].status == "succeeded")
        assert len(provider.requests) == 1
