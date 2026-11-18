from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from test_agent_queue import enqueue, session, update
from test_agent_steering import start
from zhimeng_runtime.agent_provider import AgentProviderError, ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.agent_queue import AgentQueueService, QueueInput, QueueUpdate
from zhimeng_runtime.agent_queue_scheduler import AgentQueueScheduler
from zhimeng_runtime.app import create_app


class QueueProvider(ApiProvider):
    def __init__(self, *, block=False, fail=False):
        self.block = block
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests = []
        self.active = 0
        self.max_active = 0

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.requests.append([dict(message) for message in messages])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            while self.block and not self.release.is_set():
                if cancel_event.is_set():
                    return
                await asyncio.sleep(0.005)
            if self.fail:
                raise AgentProviderError("Local test failure", code="test_failure", retryable=False)
            yield ProviderTextDelta("Local completed response.")
            yield ProviderCompleted("stop", None)
        finally:
            self.active -= 1


def eventually(condition, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Saved queue did not reach expected state")


def scheduler(client):
    value = client.app.state.agent_queue_scheduler
    value.poll_seconds = 0.025
    return value


def runs(client, sid):
    return client.app.state.agent_service.store.list_runs(sid)


def test_saved_queue_runs_two_messages_in_order_without_browser_requests(settings):
    provider = QueueProvider(block=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        scheduler(client)
        sid = session(client)
        enqueue(client, sid, "background-item-1", "Summarize the README.")
        assert provider.entered.wait(10)
        enqueue(client, sid, "background-item-2", "Now turn those points into a checklist.")
        provider.release.set()
        # From here, no HTTP polling, dispatch request, SSE or browser is involved.
        eventually(lambda: len(runs(client, sid)) == 2 and all(r.status == "succeeded" for r in runs(client, sid)))
        assert provider.max_active == 1
        assert len(provider.requests) == 2
        users = [m.content for m in client.app.state.agent_service.store.list_messages(sid) if m.role == "user"]
        assert users == ["Summarize the README.", "Now turn those points into a checklist."]
        assert client.app.state.agent_queue_service.store.read(sid).items == []


@pytest.mark.parametrize("end", ["failed", "cancelled"])
def test_failure_or_cancel_pauses_remaining_work_without_browser(settings, end):
    provider = QueueProvider(block=True, fail=end == "failed")
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        enqueue(client, sid, "background-item-1")
        assert provider.entered.wait(10)
        enqueue(client, sid, "background-item-2")
        if end == "cancelled":
            rid = runs(client, sid)[0].run_id
            assert client.post(f"/api/v1/agent/runs/{rid}/cancel").status_code == 200
        else:
            provider.release.set()
        queue = client.app.state.agent_queue_service.store
        eventually(lambda: queue.read(sid).pause_reason == end)
        client.portal.call(worker.tick)
        assert len(runs(client, sid)) == 1
        assert len(provider.requests) == 1
        assert queue.read(sid).items[0].item_id == "background-item-2"
        provider.fail = False
        provider.release.set()
        update(client, sid, "resume")
        eventually(lambda: len(runs(client, sid)) == 2 and all(r.status in {end, "succeeded"} for r in runs(client, sid)))


@pytest.mark.parametrize("pause", [False, True])
def test_restart_dispatches_saved_idle_queue_but_preserves_user_pause(settings, pause):
    with TestClient(create_app(settings, agent_provider=QueueProvider())) as client:
        client.portal.call(scheduler(client).stop)
        sid = session(client)
        enqueue(client, sid, "restart-item-01")
        enqueue(client, sid, "restart-item-02")
        if pause:
            update(client, sid, "pause")
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        if pause:
            client.portal.call(worker.tick)
            assert not runs(client, sid)
            assert client.app.state.agent_queue_service.store.read(sid).pause_reason == "user"
            update(client, sid, "resume")
        eventually(lambda: len(runs(client, sid)) == 2 and all(r.status == "succeeded" for r in runs(client, sid)))
        assert len(provider.requests) == 2


def test_restart_pauses_interrupted_run_and_unadmitted_prepare(settings):
    with TestClient(create_app(settings, agent_provider=QueueProvider(block=True))) as client:
        worker = scheduler(client)
        sid = session(client)
        enqueue(client, sid, "interrupted-item-1")
        assert client.app.state.agent_service.provider.entered.wait(10)
        enqueue(client, sid, "interrupted-item-2")
        client.portal.call(worker.stop)
        other = session(client)
        enqueue(client, other, "prepared-item-01")
        client.app.state.agent_queue_service.store.prepare(other, "prepared-item-01")
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.tick)
        queue = client.app.state.agent_queue_service.store
        assert queue.read(sid).pause_reason == "interrupted"
        assert queue.read(other).pause_reason == "interrupted"
        assert not queue.read(other).items[0].sending
        assert not provider.requests
        assert len(runs(client, sid)) == 1 and not runs(client, other)


def test_background_and_http_dispatch_race_has_one_admission(settings):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        sid = session(client)
        enqueue(client, sid, "dispatch-race-01")
        url = f"/api/v1/agent/sessions/{sid}/queue/dispatch-race-01/dispatch"
        with ThreadPoolExecutor(max_workers=3) as pool:
            tick = pool.submit(client.portal.call, worker.tick)
            requests = [pool.submit(client.post, url) for _ in range(2)]
            tick.result()
            replies = [request.result() for request in requests]
        assert [reply.status_code for reply in replies] == [200, 200]
        assert replies[0].json()["accepted"]["run"]["run_id"] == replies[1].json()["accepted"]["run"]["run_id"]
        eventually(lambda: runs(client, sid)[0].status == "succeeded")
        assert len(runs(client, sid)) == 1
        assert len(provider.requests) == 1


def test_atomic_enqueue_steer_cannot_be_stolen_by_background(settings, monkeypatch):
    provider = QueueProvider(block=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        assert provider.entered.wait(10)
        queue = client.app.state.agent_queue_service.store
        entered, release = threading.Event(), threading.Event()
        original = queue.enqueue

        def held_enqueue(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(10)
            return result

        monkeypatch.setattr(queue, "enqueue", held_enqueue)
        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(client.post, f"/api/v1/agent/sessions/{sid}/queue/atomic-steer-01/steer", json={
                "expected_run_id": rid, "input": {"item_id": "atomic-steer-01", "message": "Use a checklist."},
            })
            assert entered.wait(10)
            tick = pool.submit(client.portal.call, worker.tick)
            release.set()
            response = request.result()
            tick.result()
        assert response.status_code == 200, response.text
        provider.release.set()
        eventually(lambda: runs(client, sid)[0].status == "succeeded")
        assert len(runs(client, sid)) == 1
        assert len(provider.requests) == 2
        assert queue.read(sid).items == []


def test_late_atomic_steer_stays_paused_instead_of_becoming_a_new_run(settings):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        sid = session(client)
        rid = start(client, sid)
        eventually(lambda: runs(client, sid)[0].status == "succeeded")
        response = client.post(f"/api/v1/agent/sessions/{sid}/queue/late-steer-0001/steer", json={
            "expected_run_id": rid, "input": {"item_id": "late-steer-0001", "message": "Amend the current response."},
        })
        assert response.status_code == 409
        client.portal.call(worker.tick)
        state = client.app.state.agent_queue_service.store.read(sid)
        assert state.pause_reason == "user"
        assert state.items[0].item_id == "late-steer-0001"
        assert len(runs(client, sid)) == 1
        update(client, sid, "resume")
        eventually(lambda: len(runs(client, sid)) == 2 and all(r.status == "succeeded" for r in runs(client, sid)))


def test_failed_admission_is_paused_and_acknowledgement_loss_does_not_replay(settings, monkeypatch):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        client.portal.call(worker.stop)
        service = client.app.state.agent_service
        original = service.start_run
        calls = []

        async def unavailable(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("local unavailable")

        monkeypatch.setattr(service, "start_run", unavailable)
        sid = session(client)
        enqueue(client, sid, "admission-item-1")
        client.portal.call(worker.tick)
        client.portal.call(worker.tick)
        assert len(calls) == 1
        assert client.app.state.agent_queue_service.store.read(sid).pause_reason == "send_failed"

        async def lost_ack(**kwargs):
            await original(**kwargs)
            raise ConnectionError("lost acknowledgement")

        monkeypatch.setattr(service, "start_run", lost_ack)
        update(client, sid, "resume")
        client.portal.call(worker.tick)
        client.portal.call(worker.tick)
        eventually(lambda: runs(client, sid)[0].status == "succeeded")
        assert len(runs(client, sid)) == 1
        assert len(provider.requests) == 1
        assert client.app.state.agent_queue_service.store.read(sid).items == []


def test_background_refreshes_project_identity_from_current_files(tmp_path):
    from test_memory_permissions import _harness

    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        provider = QueueProvider()
        service.provider = provider
        queue = AgentQueueService(service)
        worker = AgentQueueScheduler(queue)
        current = await service.create_session(project_id=binding.project_id, title="Document task")
        queue.store.enqueue(current.session_id, QueueInput(item_id="project-refresh-1", message="Summarize the updated README.", project_id=binding.project_id))
        (folder / "README.md").write_bytes(b"Changed between queueing and dispatch.\n")
        try:
            await worker.tick()
            from test_agent_service import wait_terminal
            run = service.store.list_runs(current.session_id)[0]
            terminal = await wait_terminal(service, run.run_id)
            assert terminal.status == "succeeded", terminal.error
            latest = service.store.database.get_project_binding(binding.project_id)
            assert latest.revision == binding.revision + 1
            assert latest.fingerprint != binding.fingerprint
            with service.store.database.connection() as db:
                actual = db.execute("SELECT project_revision, project_fingerprint FROM run WHERE id = ?", (run.run_id,)).fetchone()
            assert actual["project_revision"] == latest.revision
            assert actual["project_fingerprint"] == latest.fingerprint
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("hook", ["_maybe_name_session", "_maybe_remember"])
def test_terminal_run_finishes_postprocessing_before_next_dispatch(settings, monkeypatch, hook):
    provider = QueueProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        worker = scheduler(client)
        service = client.app.state.agent_service
        entered, release = threading.Event(), threading.Event()
        original = getattr(service, hook)

        async def held_transcript(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, hook, held_transcript)
        sid = session(client)
        enqueue(client, sid, "cleanup-item-001")
        assert entered.wait(10)
        assert runs(client, sid)[0].status == "succeeded"
        enqueue(client, sid, "cleanup-item-002")
        client.portal.call(worker.tick)
        assert len(runs(client, sid)) == 1
        assert client.app.state.agent_queue_service.store.read(sid).pause_reason is None
        release.set()
        eventually(lambda: len(runs(client, sid)) == 2 and all(r.status == "succeeded" for r in runs(client, sid)))
        assert len(provider.requests) == 2
