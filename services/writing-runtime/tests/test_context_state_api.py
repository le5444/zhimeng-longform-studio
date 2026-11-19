from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from test_agent_steering import start
from zhimeng_runtime.app import create_app
from zhimeng_runtime.context_state_api import SessionContextState


def test_context_state_reports_real_history_and_compaction_without_changing_messages(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        endpoint = f'/api/v1/agent/sessions/{sid}/context-state'
        empty = client.get(endpoint)
        assert empty.status_code == 200
        assert empty.json()['message_count'] == 0
        assert empty.json()['latest_checkpoint'] is None
        first = start(client, sid, 'context-first-turn', 'Read this ordinary document.')
        wait_for_success(client, first)
        second = start(client, sid, 'context-second-turn', 'Summarize the document.')
        wait_for_success(client, second)
        store = client.app.state.agent_service.store
        before = store.list_messages(sid)
        checkpoint = store.append_session_checkpoint(session_id=sid, run_id=None,
            through_ordinal=before[1].ordinal, source_message_count=2, content='We read a document.', reported_model=None)
        view = client.get(endpoint).json()
        assert view['message_count'] == 4
        assert view['compacted_message_count'] == 2
        assert view['recent_message_count'] == 2
        assert view['latest_checkpoint']['checkpoint_id'] == checkpoint.checkpoint_id
        assert store.list_messages(sid) == before
        client.app.state.agent_service._compacting_sessions.add(sid)
        try:
            busy = client.get(endpoint).json()
            assert busy['compacting'] is True and busy['background_pending'] is True
        finally:
            client.app.state.agent_service._compacting_sessions.remove(sid)
        assert client.get('/api/v1/agent/sessions/missing-session/context-state').status_code == 404


def test_context_state_background_tail_and_session_scope(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        one, two = session(client), session(client)
        run_id = start(client, one, 'context-tail-turn')
        wait_for_success(client, run_id)
        class PendingTask:
            def done(self): return False
        service = client.app.state.agent_service
        existing = service._tasks.get(run_id)
        service._tasks[run_id] = PendingTask()
        try:
            assert client.get(f'/api/v1/agent/sessions/{one}/context-state').json()['background_pending'] is True
            other = client.get(f'/api/v1/agent/sessions/{two}/context-state').json()
            assert other['background_pending'] is False
            assert other['message_count'] == 0
        finally:
            if existing is not None: service._tasks[run_id] = existing
            else: service._tasks.pop(run_id, None)


def test_context_state_schema_matches_model():
    path = Path(__file__).resolve().parents[3] / 'packages/contracts/session-context-state.schema.json'
    expected = SessionContextState.model_json_schema()
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved.pop('$schema')
    saved.pop('$id')
    assert saved == expected


def test_run_compaction_is_durable_and_interrupted_state_is_not_reported_running(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, other = session(client), session(client)
        rid = start(client, sid, 'context-large-results')
        wait_for_success(client, rid)
        store = client.app.state.agent_service.store
        endpoint = f'/api/v1/agent/sessions/{sid}/context-state'
        store.append_run_event(rid, 'run_context_compaction', {'state': 'running', 'tokens_before': 481642})
        assert client.get(endpoint).json()['run_compaction']['state'] == 'interrupted'
        store.append_run_event(rid, 'run_context_compaction', {'state': 'succeeded', 'folded_results': 6,
            'tokens_before': 481642, 'tokens_after': 125000})
        record = client.get(endpoint).json()['run_compaction']
        assert record == {'run_id': rid, 'state': 'succeeded', 'folded_results': 6,
                          'tokens_before': 481642, 'tokens_after': 125000}
        assert client.get(f'/api/v1/agent/sessions/{other}/context-state').json()['run_compaction'] is None
