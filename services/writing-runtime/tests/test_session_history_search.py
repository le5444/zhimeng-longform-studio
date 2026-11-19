from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_session_recall import history
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.session_source_api import SessionHistorySearchResponse, create_session_source_router


@pytest.fixture
def client(history):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_session_source_router(history[0]))
    with TestClient(app) as client:
        yield client


def search(client, query='预算', **params):
    return client.get('/api/v1/agent/history/search', params={'query': query, **params})


def test_search_without_active_session_finds_body_and_reads_exact_source_without_writes(history, client):
    service, binding, session, turn, current, run, _ = history
    past = session(binding.project_id, '采购记录')
    _, user = turn(past, '采购预算是 730 元。', '预算可以设为 900 元。')
    service.store.append_session_checkpoint(past.session_id, run_id=None, through_ordinal=2,
        source_message_count=2, content='压缩过的历史')
    with service.store.database.connection() as db:
        before = db.execute('SELECT COUNT(*) FROM run').fetchone()[0]
        original = db.execute('SELECT * FROM session_message ORDER BY message_id').fetchall()
    result = search(client, '预算 730').json()
    assert result['scope'] == 'all' and result['project_id'] is None
    assert [hit['message_id'] for hit in result['matches']] == [user.message_id]
    hit = result['matches'][0]
    assert hit['session_title'] == '采购记录' and hit['role'] == 'user'
    source = client.get(f"/api/v1/agent/sessions/{hit['session_id']}/messages/{hit['message_id']}/source")
    assert source.status_code == 200 and source.json()['message']['content'] == user.content
    # Human search can find the current user input; Agent recall still excludes its own run.
    assert search(client, 'earlier decision').json()['matches'][0]['run_id'] == run.run_id
    with service.store.database.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM run').fetchone()[0] == before
        assert db.execute('SELECT * FROM session_message ORDER BY message_id').fetchall() == original


def test_scope_closed_conversations_and_unicode_literal_pagination(history, client):
    service, binding, session, turn, *_ = history
    project, loose, closed = session(binding.project_id), session(), session(binding.project_id)
    expected = []
    for index in range(5):
        _, user = turn(project, f'Straße %_ 预算 {index}')
        expected.append(user.message_id)
    _, outside = turn(loose, 'Straße %_ 预算 outside')
    turn(closed, 'Straße %_ 预算 closed')
    service.store.close_session(closed.session_id)
    found, cursor = [], None
    while True:
        params = dict(scope='project', project_id=binding.project_id, limit=2)
        if cursor:
            params['before_message_id'] = cursor
        response = search(client, 'STRASSE %_', **params)
        assert response.status_code == 200
        page = response.json()
        found.extend(hit['message_id'] for hit in page['matches'])
        cursor = page['next_before_message_id']
        if not page['has_more']:
            break
    assert len(found) == 5 and set(found) == set(expected)
    assert [hit['message_id'] for hit in search(client, 'STRASSE %_', scope='project').json()['matches']] == [outside.message_id]
    assert len(search(client, 'STRASSE %_').json()['matches']) == 6


def test_long_excerpt_masking_and_deleted_cursor_returns_recoverable_error(history, client):
    service, _, session, turn, *_ = history
    target = session(title='notes')
    secret = 'sk-' + 'A' * 32
    _, user = turn(target, ('intro\n' * 300) + '预算 730 ' + secret + ' ' + 'x' * 2000)
    hit = search(client, '预算').json()['matches'][0]
    assert hit['message_id'] == user.message_id
    assert hit['start_offset'] > 0 and hit['next_start_offset'] is not None
    assert '预算 730' in hit['content'] and secret not in hit['content']
    service.store.close_session(target.session_id)
    response = search(client, before_message_id=user.message_id)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'session_search_cursor_invalid'
    assert search(client).json()['matches'] == []


@pytest.mark.parametrize('params,status', [({'query': ''}, 422), ({'query': '   '}, 400),
    ({'query': 'a' * 501}, 422), ({'limit': 51}, 422), ({'scope': 'invalid'}, 422)])
def test_invalid_search_is_not_silent_browse(history, client, params, status):
    assert search(client, **params).status_code == status


def test_search_unavailable_is_not_empty_and_does_not_create_database(history, client, tmp_path):
    missing = tmp_path / 'missing.sqlite3'
    history[0].store.database.path = missing
    response = search(client)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'session_search_unavailable'
    assert not missing.exists()


def test_search_contract_matches_model():
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/session-history-search-response.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id'); schema.pop('$schema')
    assert schema == SessionHistorySearchResponse.model_json_schema(by_alias=True)
