from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider, wait_for_success
from zhimeng_runtime.agent_models import AgentMessageListResponse
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.app import create_app
from zhimeng_runtime.execution_protocol import RunStatus


@pytest.fixture
def summary_client(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        yield client


def _session(client):
    response = client.post('/api/v1/agent/sessions', json={'title': '普通对话'})
    assert response.status_code == 201, response.text
    return response.json()['session_id']


def _run(store: AgentStore, sid: str):
    run, _, _ = store.create_run(
        session_id=sid, message='说明这个文件', expected_project_revision=None,
        expected_project_fingerprint=None, active_chapter_id=None,
        idempotency_key=f'process-summary-{uuid4().hex}',
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail='fixture preparing')
    store.transition_run(run.run_id, RunStatus.RUNNING, detail='fixture running')
    return run.run_id


def _complete(store, rid):
    store.complete_run(rid, message_id=f'message-{uuid4().hex}', content='说明完成。', finish_reason='stop', usage=None)


def _activity(store, rid, kind, payload):
    # Persist ordinary event metadata without starting any provider/network work.
    # The summary intentionally never needs a full ActivityItem deserialization.
    with store.database.connection() as connection:
        run = connection.execute('SELECT * FROM run WHERE id = ?', (rid,)).fetchone()
        sequence = run['last_activity_sequence'] + 1
        connection.execute('''INSERT INTO activity
            (id, session_id, run_id, sequence, kind, payload_json, project_revision,
             project_fingerprint, correlation_id, causation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)''',
            (f'activity-{uuid4().hex}', run['session_id'], rid, sequence, kind,
             json.dumps({'kind': kind, 'payload': payload}, ensure_ascii=False), run['correlation_id'], run['created_at']))
        connection.execute('UPDATE run SET last_activity_sequence = ? WHERE id = ?', (sequence, rid))


def _summary(client, sid):
    response = client.get(f'/api/v1/agent/sessions/{sid}/messages')
    assert response.status_code == 200, response.text
    return response.json()['run_process_summaries']


def test_pure_chat_excludes_runtime_parameters_and_bookkeeping(summary_client):
    client = summary_client
    sid = _session(client)
    store = client.app.state.agent_service.store
    rid = _run(store, sid)
    _activity(store, rid, 'reasoning', {'source': 'runtime', 'summary': '本次参数', 'content': '本次按服务设置发送'})
    _activity(store, rid, 'reasoning', {'summary': '本次发送参数', 'content': '这描述的是发送参数，旧数据没有source'})
    for kind in ('context_pack', 'writer_intent', 'context_injection_receipt'):
        _activity(store, rid, 'artifact', {'artifact_type': kind})
    _complete(store, rid)
    assert _summary(client, sid) == {rid: {'has_process': False, 'tool_count': 0}}


def test_real_message_api_plain_turn_remains_process_free_after_restart(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = _session(client)
        started = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key': 'plain-process-summary'}, json={'message': '你好，简单打个招呼。'})
        assert started.status_code == 202, started.text
        rid = started.json()['run']['run_id']
        assert wait_for_success(client, rid)['status'] == 'succeeded'
        assert _summary(client, sid) == {rid: {'has_process': False, 'tool_count': 0}}
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        assert _summary(restarted, sid) == {rid: {'has_process': False, 'tool_count': 0}}


def test_tool_calls_count_once_and_survive_cancel_without_loading_arguments(summary_client):
    client = summary_client
    sid = _session(client)
    store = client.app.state.agent_service.store
    rid = _run(store, sid)
    for index in range(2):
        args = {'path': f'input-{index}.txt'}
        kwargs = dict(tool_call_id=f'tool-summary-{index}', tool_name='read_file', tool_version='1', arguments=args,
            request_hash=str(index + 1) * 64, idempotency_key=f'tool-summary-key-{index}')
        store.begin_tool_call(rid, **kwargs)
        _, replayed = store.begin_tool_call(rid, **kwargs)
        assert replayed is True
    store.cancel_run(rid)
    store.mark_cancelled(rid)
    summary = _summary(client, sid)
    assert summary == {rid: {'has_process': True, 'tool_count': 2}}
    assert 'input-0.txt' not in json.dumps(summary)


@pytest.mark.parametrize(('kind', 'payload', 'has_process'), [
    ('artifact', {'artifact_type': 'generated_file', 'name': 'report.csv'}, True),
    ('skill_call', {'skill_id': 'summarize', 'status': 'succeeded'}, True),
    ('change_proposal', {'proposal_id': 'proposal-fixture'}, True),
    ('command', {'command': 'python script.py', 'status': 'failed'}, True),
    ('error', {'error': {'message': 'fixture failure'}}, True),
    ('reasoning', {'source': 'model', 'summary': '思考', 'content': '先核对两份文件。'}, True),
    ('reasoning', {'summary': '旧格式的思考', 'content': None}, True),
    ('reasoning', {'source': 'model', 'summary': '思考', 'phase': 'delta', 'delta': '还有一个条件'}, True),
    ('reasoning', {'source': 'model', 'summary': '思考', 'phase': 'delta', 'delta': ''}, False),
    ('reasoning', {'source': 'model', 'summary': '思考', 'content': '  '}, False),
])
def test_non_tool_process_is_not_lost(summary_client, kind, payload, has_process):
    client = summary_client
    sid = _session(client)
    store = client.app.state.agent_service.store
    rid = _run(store, sid)
    _activity(store, rid, kind, payload)
    _complete(store, rid)
    assert _summary(client, sid)[rid] == {'has_process': has_process, 'tool_count': 0}


def test_summary_covers_only_current_message_page_and_cannot_cross_sessions(summary_client):
    client = summary_client
    sid, other = _session(client), _session(client)
    store = client.app.state.agent_service.store
    first = _run(store, sid)
    _activity(store, first, 'tool_call', {'tool_call_id': 'first-tool', 'arguments': {}})
    _complete(store, first)
    second = _run(store, sid)
    _complete(store, second)
    outsider = _run(store, other)
    _activity(store, outsider, 'tool_call', {'tool_call_id': 'other-tool', 'arguments': {}})
    _complete(store, outsider)
    page = client.get(f'/api/v1/agent/sessions/{sid}/messages?latest=true&limit=1').json()
    assert page['run_process_summaries'] == {second: {'has_process': False, 'tool_count': 0}}
    earlier = client.get(f"/api/v1/agent/sessions/{sid}/messages?latest=true&limit=2&before_ordinal={page['messages'][0]['ordinal']}").json()
    assert set(earlier['run_process_summaries']) == {first, second}
    assert earlier['run_process_summaries'][first]['tool_count'] == 1
    assert set(store.run_process_summaries(sid, [first, second, outsider, 'missing-run'])) == {first, second}
    assert store.run_process_summaries(sid, []) == {}
    assert _summary(client, _session(client)) == {}


def test_summary_uses_one_batched_query_without_decoding_large_tool_bodies(summary_client, monkeypatch):
    client = summary_client
    sid = _session(client)
    store = client.app.state.agent_service.store
    run_ids = []
    for index in range(4):
        rid = _run(store, sid)
        _activity(store, rid, 'tool_call', {'tool_call_id': f'large-{index}', 'arguments': {'content': 'x' * 100_000}})
        _activity(store, rid, 'tool_result', {'tool_call_id': f'large-{index}', 'output': 'y' * 100_000})
        _complete(store, rid)
        run_ids.append(rid)
    original_connection = store.database.connection
    statements = []

    @contextmanager
    def traced_connection():
        with original_connection() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store.database, 'connection', traced_connection)
    monkeypatch.setattr(store, 'list_activities', lambda *args, **kwargs: pytest.fail('summary loaded full activities'))
    result = store.run_process_summaries(sid, run_ids + run_ids)
    assert len(result) == 4
    assert all(value.tool_count == 1 and value.has_process for value in result.values())
    assert len(statements) == 1
    assert 'GROUP BY r.id' in statements[0]
    assert '$.arguments' not in statements[0] and '$.output' not in statements[0]


def test_message_page_contract_is_current_and_old_payloads_remain_valid():
    path = Path(__file__).resolve().parents[3] / 'packages/contracts/agent-message-list.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    schema.pop('$schema')
    schema.pop('$id')
    assert schema == AgentMessageListResponse.model_json_schema(by_alias=True)
    legacy = AgentMessageListResponse(session_id='session-fixture', messages=[], last_ordinal=0)
    assert legacy.run_process_summaries == {}
