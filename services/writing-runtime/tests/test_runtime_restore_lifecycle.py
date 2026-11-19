"""Restoring SQLite drains the old runtime and leaves a usable new runtime."""
from __future__ import annotations

import asyncio
from functools import partial
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_agent_service import ScriptedProvider
from test_runtime_backup_api import _client
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.agent_queue import QueueInput, QueueUpdate
from zhimeng_runtime.backup_service import BackupService, BackupStateError
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.runtime_maintenance import RuntimeMaintenance, RuntimeMaintenanceMiddleware
from zhimeng_runtime.project_reader import ProjectReader


def test_restore_waits_for_scan_thread_and_invalidates_old_scan_result(tmp_path, monkeypatch):
    project = tmp_path / 'ordinary-folder'
    project.mkdir()
    document = project / 'README.md'
    original_bytes = b'Ordinary project\r\nOriginal content.\r\n'
    document.write_bytes(original_bytes)
    entered, release, finished, replacing = (threading.Event() for _ in range(4))
    original_inspect = ProjectReader.inspect
    original_restore = BackupService.restore_backup

    def inspect(reader, root, *args, **kwargs):
        if Path(root) == project:
            entered.set()
            assert release.wait(20), 'test did not release scan'
        result = original_inspect(reader, root, *args, **kwargs)
        finished.set()
        return result

    def restore(service, request):
        assert finished.is_set()
        replacing.set()
        return original_restore(service, request)

    monkeypatch.setattr(ProjectReader, 'inspect', inspect)
    monkeypatch.setattr(BackupService, 'restore_backup', restore)
    client, _ = _client(tmp_path)
    with client:
        backup = client.post('/api/v1/runtime/backups').json()['receipt']
        scan = client.post('/api/v1/projects/inspect-jobs', json={'root_path': str(project)})
        assert scan.status_code == 200
        job_id = scan.json()['job_id']
        assert entered.wait(10)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, '/api/v1/runtime/backups/restore', json={
                'backup_id': backup['backup_id'], 'expected_manifest_hash': backup['manifest']['manifest_hash'],
            })
            try:
                deadline = time.monotonic() + 10
                while not client.app.state.runtime_maintenance.active and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert client.app.state.runtime_maintenance.active
                assert not future.done() and not replacing.is_set()
            finally:
                release.set()
            response = future.result(timeout=20)
        assert response.status_code == 200, response.text
        assert replacing.is_set()
        assert client.get('/api/v1/projects').json()['projects'] == []
        old = client.get(f'/api/v1/projects/inspect-jobs/{job_id}').json()
        assert old['status'] == 'failed' and old['result'] is None
        new = client.post('/api/v1/projects/inspect-jobs', json={'root_path': str(project)}).json()['job_id']
        client.portal.call(client.app.state.runtime_maintenance.drain_background)
        assert client.get(f'/api/v1/projects/inspect-jobs/{new}').json()['status'] == 'done'
        assert len(client.get('/api/v1/projects').json()['projects']) == 1
        assert client.get('/health/ready').json()['status'] == 'ready'
    assert document.read_bytes() == original_bytes


@pytest.mark.parametrize("restore_fails", [False, True])
def test_restore_drains_active_agent_and_commands_then_accepts_new_work(tmp_path, monkeypatch, restore_fails):
    entered, stopped = threading.Event(), threading.Event()

    class WaitingProvider(ScriptedProvider):
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield ProviderTextDelta("unreachable")

    client, _ = _client(tmp_path)
    service = client.app.state.agent_service
    service._provider_selector = None
    service._auto_memory = False
    waiting = WaitingProvider([])
    service._provider_resolver = lambda: waiting
    with client:
        saved_session = client.portal.call(partial(service.create_session, project_id=None, title="Saved queue"))
        queue = client.app.state.agent_queue_service.store
        state = queue.read(saved_session.session_id)
        queue.update(saved_session.session_id, QueueUpdate(action="pause", revision=state.revision))
        queue.enqueue(saved_session.session_id, QueueInput(item_id="saved-input-001", message="Continue the saved work."))
        backup = client.post("/api/v1/runtime/backups").json()["receipt"]
        session = client.portal.call(partial(service.create_session, project_id=None, title="Restore lifecycle"))
        start = dict(session_id=session.session_id, message="Wait for more data.",
                     expected_project_revision=None, expected_project_fingerprint=None,
                     active_chapter_id=None, idempotency_key="before-restore")
        run = client.portal.call(partial(service.start_run, **start)).run
        assert entered.wait(15)
        manager = service.tool_executor._command_session_manager()
        command = manager.start(owner_session_id=session.session_id, owner_run_id=run.run_id,
                                command=f'"{sys.executable}" -c "import time; time.sleep(60)"',
                                cwd=tmp_path, yield_time_ms=0)
        assert command["status"] == "running"
        if restore_fails:
            def fail_restore(self, request):
                raise BackupStateError("Injected restore failure")
            monkeypatch.setattr(BackupService, "restore_backup", fail_restore)
        response = client.post("/api/v1/runtime/backups/restore", json={
            "backup_id": backup["backup_id"],
            "expected_manifest_hash": backup["manifest"]["manifest_hash"],
        })
        assert response.status_code == (409 if restore_fails else 200), response.text
        assert stopped.is_set()
        assert not service._tasks
        assert not service._closing
        assert manager.list(session.session_id)[0]["status"] != "running"
        assert service.tool_executor._command_session_manager() is not manager
        assert client.get("/health/ready").status_code == 200
        restored_queue = queue.read(saved_session.session_id)
        assert restored_queue.pause_reason == "user"
        assert [item.item_id for item in restored_queue.items] == ["saved-input-001"]
        ready = ScriptedProvider([ProviderTextDelta("Ready after restore."), ProviderCompleted("stop", None)])
        service._provider_resolver = lambda: ready
        new_session = client.portal.call(partial(service.create_session, project_id=None, title="After restore"))
        start.update(session_id=new_session.session_id, idempotency_key="after-restore")

        async def complete():
            result = await service.start_run(**start)
            await service._tasks[result.run.run_id]
            return service.store.get_run(result.run.run_id)

        assert client.portal.call(complete).status is RunStatus.SUCCEEDED


def test_maintenance_drains_requests_closes_streams_and_serializes_restore():
    async def scenario():
        maintenance = RuntimeMaintenance()
        slow_started, stream_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        responses = {}

        async def app(scope, receive, send):
            path = scope["path"]
            if path == "/slow":
                slow_started.set()
                await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"text/event-stream" if path == "/stream" else b"text/plain"),
            ]})
            if path == "/stream":
                stream_started.set()
                await asyncio.Event().wait()
            await send({"type": "http.response.body", "body": b"done", "more_body": False})

        middleware = RuntimeMaintenanceMiddleware(app, maintenance)

        async def request(path):
            responses[path] = []
            async def send(message):
                responses[path].append(message)
            await middleware({"type": "http", "method": "GET", "path": path}, None, send)

        slow, stream = asyncio.create_task(request("/slow")), asyncio.create_task(request("/stream"))
        await slow_started.wait()
        await stream_started.wait()
        restored = asyncio.Event()
        async def restore():
            assert slow.done() and stream.done()
            restored.set()
        restore_task = asyncio.create_task(maintenance.run(restore))
        while not maintenance.active:
            await asyncio.sleep(0)
        await request("/during")
        assert responses["/during"][0]["status"] == 503
        assert not restored.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(slow, stream, restore_task), 5)
        assert responses["/stream"][-1]["more_body"] is False
        assert restored.is_set()
        await request("/after")
        assert responses["/after"][0]["status"] == 200
    asyncio.run(scenario())


def test_cancelled_restore_request_waits_for_state_replacement():
    async def scenario():
        maintenance = RuntimeMaintenance()
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def restore():
            started.set()
            await release.wait()
            finished.set()
        task = asyncio.create_task(maintenance.run(restore))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert maintenance.active
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not maintenance.active
    asyncio.run(scenario())
