from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_session_recall import history
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.secret_mask import mask_secrets
from zhimeng_runtime.session_source_api import SessionSourceResponse, create_session_source_router


@pytest.fixture
def client(history):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_session_source_router(history[0]))
    with TestClient(app) as client:
        yield client


def source(client, session_id, message_id, **params):
    return client.get(f'/api/v1/agent/sessions/{session_id}/messages/{message_id}/source', params=params)


def test_source_neighbors_preserve_original_roles_project_and_current_input(history, client):
    service, binding, session, turn, current, run, _ = history
    target = session(binding.project_id, 'Purchase decision')
    _, first = turn(target, 'Budget is 730.', 'Consider 900.')
    _, second = turn(target, 'Confirmed: keep 730.')
    service.store.append_session_checkpoint(target.session_id, run_id=None, through_ordinal=2,
        source_message_count=2, content='Compressed summary')
    page = source(client, target.session_id, first.message_id).json()
    assert page['message']['content'] == first.content
    assert page['message']['project_id'] == binding.project_id
    assert page['previous_message_id'] is None
    assistant = source(client, target.session_id, page['next_message_id']).json()
    assert assistant['message']['role'] == 'assistant'
    assert assistant['message']['content'] == 'Consider 900.'
    assert assistant['previous_message_id'] == first.message_id
    assert assistant['next_message_id'] == second.message_id
    current_input = service.store.list_messages(current.session_id)[0]
    response = source(client, current.session_id, current_input.message_id)
    assert response.status_code == 200
    assert response.json()['message']['run_id'] == run.run_id


def test_long_source_paging_masks_before_slicing_and_does_not_modify_original(history, client):
    service, _, session, turn, *_ = history
    target = session()
    secret = 'sk-' + 'A' * 32
    original = '\U0001f9ea' * 11990 + secret + '\r\n' + 'Z' * 13000
    _, user = turn(target, original)
    chunks, offset, digest = [], 0, None
    while True:
        params = {'start_offset': offset}
        if digest:
            params['expected_hash'] = digest
        response = source(client, target.session_id, user.message_id, **params)
        assert response.status_code == 200
        page = response.json()
        digest = page['content_hash']
        message = page['message']
        assert message['start_offset'] == offset
        assert message['masked_secrets'] == 1
        chunks.append(message['content'])
        if message['next_start_offset'] is None:
            assert message['end_offset'] == message['total_characters']
            break
        offset = message['next_start_offset']
    assert len(chunks) == 3
    assert ''.join(chunks) == mask_secrets(original)[0]
    assert secret not in ''.join(chunks)
    assert service.store.list_messages(target.session_id)[0].content == original
    assert len(service.store.list_messages(target.session_id)) == 2


@pytest.mark.parametrize('mode', ['wrong_session', 'closed', 'deleted'])
def test_unavailable_sources_never_fall_back_to_other_messages(history, client, mode):
    service, _, session, turn, current, *_ = history
    target = session()
    _, user = turn(target, 'Exact source only')
    selected = target.session_id
    if mode == 'wrong_session':
        selected = current.session_id
    elif mode == 'closed':
        service.store.close_session(target.session_id)
    else:
        with service.store.database.connection() as connection:
            connection.execute('DELETE FROM execution_session WHERE id=?', (target.session_id,))
    response = source(client, selected, user.message_id)
    assert response.status_code == 404
    assert response.json()['error']['code'] == 'session_source_not_found'


def test_changed_source_rejects_continuation_before_slicing_shorter_content(history, client):
    service, _, session, turn, *_ = history
    target = session()
    _, user = turn(target, 'A' * 13000)
    page = source(client, target.session_id, user.message_id).json()
    with service.store.database.connection() as connection:
        connection.execute("UPDATE activity SET payload_json=json_set(payload_json, '$.payload.content', ?) WHERE id=(SELECT activity_id FROM session_message WHERE message_id=?)", ('Changed.', user.message_id))
    response = source(client, target.session_id, user.message_id, start_offset=12000, expected_hash=page['content_hash'])
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'session_source_changed'
    assert source(client, target.session_id, user.message_id).json()['message']['content'] == 'Changed.'


def test_missing_database_is_unavailable_and_is_not_created(history, client, tmp_path):
    missing = tmp_path / 'missing.sqlite3'
    history[0].store.database.path = missing
    response = source(client, 'session', 'message')
    assert response.status_code == 503
    assert not missing.exists()


@pytest.mark.parametrize('params,status', [({'start_offset': -1}, 422), ({'expected_hash': 'bad'}, 422), ({'start_offset': 500}, 400)])
def test_invalid_continuation_does_not_silently_return_other_text(history, client, params, status):
    _, _, session, turn, *_ = history
    target = session()
    _, user = turn(target, 'Short message')
    assert source(client, target.session_id, user.message_id, **params).status_code == status


def test_source_contract_matches_response_model():
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/session-source-response.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == SessionSourceResponse.model_json_schema(by_alias=True)
