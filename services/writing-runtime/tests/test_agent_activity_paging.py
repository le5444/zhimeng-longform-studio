from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from test_agent_process_summary import _session, _run
from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_models import AgentActivityListResponse


def test_activity_pages_bound_each_read_and_reconnect_with_event_cursor(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        store = client.app.state.agent_service.store
        rid = _run(store, _session(client))
        for i in range(7):
            store.append_run_event(rid, 'model_turn', {'index': i})
            store.append_reasoning(rid, summary='核对采购表', content=f'处理记录 {i}')
        url = f'/api/v1/agent/runs/{rid}/activities'
        expected = client.get(url).json()['activities']
        first = client.get(url, params={'limit': 3}).json()
        assert len(first['activities']) == 3
        assert first['last_sequence'] == expected[-1]['sequence']
        assert first['last_event_sequence'] > first['last_sequence']
        collected = first['activities']
        # 新增活动不改变这一遍分页的范围，留给后续 SSE。
        late = store.append_reasoning(rid, summary='后续记录', content='新文件已经读到')
        page = first
        while page['next_after_sequence'] is not None:
            response = client.get(url, params={'limit': 3, 'after_sequence': page['next_after_sequence'], 'through_sequence': first['last_sequence']})
            assert response.status_code == 200
            page = response.json()
            assert len(page['activities']) <= 3
            assert page['last_event_sequence'] == first['last_event_sequence']
            collected.extend(page['activities'])
        assert collected == expected
        events = store.list_run_events(rid, after_sequence=first['last_event_sequence'])
        assert [event.activity.root.activity_id for event in events] == [late.root.activity_id]
        event_pages = []
        event_cursor = 0
        while batch := store.list_run_events(rid, after_sequence=event_cursor, limit=3):
            assert len(batch) <= 3
            event_pages.extend(batch)
            event_cursor = batch[-1].sequence
        assert event_pages == store.list_run_events(rid)
        assert client.get(url).json()['activities'][-1]['activity_id'] == late.root.activity_id
        store.complete_run(rid, message_id='answer-activity-paging', content='采购表核对完成。', finish_reason='stop', usage=None)
        response = client.get(f'/api/v1/agent/runs/{rid}/events', headers={'Last-Event-ID': f"{rid}:{first['last_event_sequence']}"})
        assert response.status_code == 200
        events = [json.loads(line.removeprefix('data: ')) for line in response.text.splitlines() if line.startswith('data: ')]
        activities = [event['data']['activity'] for event in events if event['event_type'] == 'activity']
        assert activities[0]['activity_id'] == late.root.activity_id
        assert not {a['activity_id'] for a in expected}.intersection(a['activity_id'] for a in activities)
        assert events[-1]['event_type'] == 'done'


def test_empty_and_invalid_activity_pages_and_contract(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        store = client.app.state.agent_service.store
        rid = _run(store, _session(client))
        url = f'/api/v1/agent/runs/{rid}/activities'
        tail = client.get(url).json()
        empty = client.get(url, params={'limit': 2, 'after_sequence': tail['last_sequence']}).json()
        assert empty['activities'] == [] and empty['next_after_sequence'] is None
        assert empty['last_event_sequence'] == tail['last_event_sequence']
        for params in ({'limit': 0}, {'limit': 1001}, {'after_sequence': -1}, {'through_sequence': -1}):
            assert client.get(url, params=params).status_code == 422
        assert client.get('/api/v1/agent/runs/run_missing/activities', params={'limit': 2}).status_code == 404
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/agent-activity-list.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == AgentActivityListResponse.model_json_schema(by_alias=True)
