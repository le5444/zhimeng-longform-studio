from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_queue import AgentQueueStore
from zhimeng_runtime.agent_queue import QueueInput, QueueUpdate, QueueState, QueueDispatchResponse
from test_agent_api import ApiProvider, wait_for_success


def session(client):
    response = client.post('/api/v1/agent/sessions', json={'title': '资料整理'})
    assert response.status_code == 201, response.text
    return response.json()['session_id']


def enqueue(client, sid, item='message-0001', message='整理 README 中的三个要点'):
    response = client.post(f'/api/v1/agent/sessions/{sid}/queue', json={'item_id': item, 'message': message})
    assert response.status_code == 200, response.text
    return response.json()


def update(client, sid, action, **kwargs):
    current = client.get(f'/api/v1/agent/sessions/{sid}/queue').json()
    response = client.patch(f'/api/v1/agent/sessions/{sid}/queue', json={'revision': current['revision'], 'action': action, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def test_queue_survives_restart_and_edit_reorder_delete(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        enqueue(client, sid)
        enqueue(client, sid, 'message-0002', '检查配置文件')
        update(client, sid, 'pause')
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        state = client.get(f'/api/v1/agent/sessions/{sid}/queue').json()
        assert state['pause_reason'] == 'user'
        assert len(state['items']) == 2
        state = update(client, sid, 'edit', item_id='message-0001', message='检查 README 链接')
        assert state['items'][0]['message'] == '检查 README 链接'
        state = update(client, sid, 'move', item_id='message-0002', direction=-1)
        assert state['items'][0]['item_id'] == 'message-0002'
        state = update(client, sid, 'remove', item_id='message-0001')
        assert len(state['items']) == 1
        assert client.get('/openapi.json').status_code == 200


def test_queue_accepts_once_and_repeated_dispatch_does_not_repeat_run(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        enqueue(client, sid)
        assert len(enqueue(client, sid)['items']) == 1
        url = f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch'
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(url), range(2)))
        assert [result.status_code for result in responses] == [200, 200]
        runs = [result.json()['accepted']['run']['run_id'] for result in responses]
        assert runs[0] == runs[1]
        assert any(result.json()['accepted']['replayed'] for result in responses)
        assert wait_for_success(client, runs[0])['status'] == 'succeeded'
        assert client.get(f'/api/v1/agent/sessions/{sid}/queue').json()['items'] == []
        assert enqueue(client, sid)['items'] == []


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'interrupted'])
def test_unsuccessful_run_pauses_queue_until_explicit_resume(settings, status):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        # This store-reconciliation test rewrites a finished Run's status below.
        # Real automatic failure/cancel progression is covered by scheduler tests.
        client.portal.call(client.app.state.agent_queue_scheduler.stop)
        sid = session(client)
        enqueue(client, sid)
        enqueue(client, sid, 'message-0002')
        first = client.post(f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch').json()['accepted']['run']['run_id']
        wait_for_success(client, first)
        async def finish_cleanup():
            while client.app.state.agent_service.is_run_executing(first):
                await asyncio.sleep(0.001)
        client.portal.call(asyncio.wait_for, finish_cleanup(), 10)
        with client.app.state.agent_service.store.database.connection() as db:
            db.execute('UPDATE run SET status = ? WHERE id = ?', (status, first))
        state = client.get(f'/api/v1/agent/sessions/{sid}/queue').json()
        assert state['pause_reason'] == status
        assert state['items'][0]['item_id'] == 'message-0002'
        url = f'/api/v1/agent/sessions/{sid}/queue/message-0002/dispatch'
        assert client.post(url).status_code == 409
        update(client, sid, 'resume')
        result = client.post(url)
        assert result.status_code == 200, result.text


def test_failed_admission_keeps_message_and_can_retry(settings, monkeypatch):
    from zhimeng_runtime.agent_service import AgentUnavailableError
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        enqueue(client, sid)
        service = client.app.state.agent_service
        original = service.start_run
        async def fail(**kwargs):
            raise AgentUnavailableError('当前模型服务未连接')
        monkeypatch.setattr(service, 'start_run', fail)
        url = f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch'
        assert client.post(url).status_code == 503
        state = client.get(f'/api/v1/agent/sessions/{sid}/queue').json()
        assert state['pause_reason'] == 'send_failed'
        assert not state['items'][0]['sending']
        monkeypatch.setattr(service, 'start_run', original)
        update(client, sid, 'resume')
        assert client.post(url).status_code == 200


def test_accepted_run_recovers_when_acknowledgement_is_lost(settings, monkeypatch):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        enqueue(client, sid)
        service = client.app.state.agent_service
        original = service.start_run
        async def lost(**kwargs):
            await original(**kwargs)
            raise ConnectionError('response lost')
        monkeypatch.setattr(service, 'start_run', lost)
        result = client.post(f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch')
        assert result.status_code == 200, result.text
        assert result.json()['accepted']['replayed'] is True
        assert result.json()['queue']['items'] == []


def test_queue_scope_stale_edits_and_closed_sessions(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, other = session(client), session(client)
        # Editing validation needs a stationary queue now that idle queues run
        # in the backend without a browser dispatch request.
        update(client, sid, 'pause')
        old = enqueue(client, sid)
        state = enqueue(client, sid, 'message-0002')
        base = f'/api/v1/agent/sessions/{sid}/queue'
        assert client.patch(base, json={'revision': old['revision'], 'action': 'remove', 'item_id': 'message-0001'}).status_code == 409
        assert client.post(f'/api/v1/agent/sessions/{other}/queue/message-0001/dispatch').status_code == 409
        assert client.post(base, json={'item_id': 'message-0003', 'message': '检查文件', 'project_id': 'other-project'}).status_code == 409
        assert client.get(base).json()['items'] == state['items']
        assert client.patch(base, json={'revision': state['revision'], 'action': 'edit', 'item_id': 'message-0001', 'message': '   '}).status_code == 422
        client.delete(f'/api/v1/agent/sessions/{sid}')
        assert client.post(base, json={'item_id': 'message-0003', 'message': '检查文件'}).status_code == 409


def test_queue_prepared_payload_survives_until_admission(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        enqueue(client, sid)
        store = AgentQueueStore(client.app.state.agent_service.store)
        first = store.prepare(sid, 'message-0001')
        assert store.read(sid).items[0].sending
        assert store.prepare(sid, 'message-0001') == first
        state = client.get(f'/api/v1/agent/sessions/{sid}/queue').json()
        result = client.patch(f'/api/v1/agent/sessions/{sid}/queue', json={'revision': state['revision'], 'action': 'remove', 'item_id': 'message-0001'})
        assert result.status_code == 409
        assert client.post(f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch').status_code == 200


@pytest.mark.parametrize('name,model', [
    ('input', QueueInput), ('update', QueueUpdate), ('state', QueueState), ('dispatch', QueueDispatchResponse),
])
def test_queue_contracts_match_models(name, model):
    path = Path(__file__).resolve().parents[3] / 'packages/contracts' / f'agent-queue-{name}.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == model.model_json_schema()


def test_queue_retains_project_and_explicit_skill_context(settings):
    from test_agent_api import bind_project, create_session
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        bind_project(client)
        sid = create_session(client)
        project_id = client.get(f'/api/v1/agent/sessions/{sid}').json()['project_id']
        entry = {'item_id': 'context-0001', 'message': '整理文件说明', 'project_id': project_id,
                 'manual_skill_names': ['文档整理'], 'writer_mode': 'repair_only', 'open_file_path': 'README.md'}
        response = client.post(f'/api/v1/agent/sessions/{sid}/queue', json=entry)
        assert response.status_code == 200, response.text
        store = AgentQueueStore(client.app.state.agent_service.store)
        request = store.prepare(sid, 'context-0001')
        assert request.manual_skill_names == ['文档整理']
        assert request.open_file_path == 'README.md'
        assert request.expected_project_revision == 1
        invalid = {**entry, 'item_id': 'context-0002', 'manual_file_ids': ['invalid id']}
        assert client.post(f'/api/v1/agent/sessions/{sid}/queue', json=invalid).status_code == 422
