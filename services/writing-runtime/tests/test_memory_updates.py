import asyncio
import codecs
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_memory_switch_20260929 import _MemoryAwareProvider, _auto_memory_service, _one_turn
from test_agent_direct_write_tools import _call
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.agent_tools import MemoryWriteArguments
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_changes_api import MemoryChangesResponse, create_memory_changes_router, read_memory_changes
from zhimeng_runtime.memory_files import USER_MEMORY_FILE_NAME, read_user_memory, write_user_memory
from zhimeng_runtime.memory_updates import MemoryChange, apply_memory_changes, parse_memory_proposals, update_user_memory

AUTO_MEMORY = True


def test_correction_replaces_old_value_preserving_unrelated_bytes_and_can_restore(tmp_path):
    original = codecs.BOM_UTF8 + '- 回答尽量简短\r\n\r\n* 表格默认用 CSV\r\n'.encode()
    path = tmp_path / USER_MEMORY_FILE_NAME
    path.write_bytes(original)
    _, changes, skipped = update_user_memory(tmp_path, [MemoryChange(action='replace', old_text='回答尽量简短', text='回答先给结论，再展开理由')])
    assert skipped is None and len(changes) == 1
    assert path.read_bytes() == codecs.BOM_UTF8 + '- 回答先给结论，再展开理由\r\n\r\n* 表格默认用 CSV\r\n'.encode()
    saved = json.loads(next((tmp_path / 'memory-history').glob('*.json')).read_text(encoding='utf-8'))
    write_user_memory(tmp_path, saved['content'])
    assert path.read_bytes() == original


def test_delete_and_repeat_add_do_not_damage_other_entries(tmp_path):
    write_user_memory(tmp_path, 'Use Python\nKeep tables in CSV\n')
    update_user_memory(tmp_path, [MemoryChange(text='Use Python')])
    assert read_user_memory(tmp_path) == 'Use Python\nKeep tables in CSV\n'
    update_user_memory(tmp_path, [MemoryChange(action='remove', old_text='Use Python')])
    assert read_user_memory(tmp_path) == 'Keep tables in CSV\n'


def test_net_unchanged_batch_has_no_change_record_or_version(tmp_path):
    write_user_memory(tmp_path, 'Keep this preference\n')
    before = list((tmp_path / 'memory-history').glob('*'))
    _, changes, skipped = update_user_memory(tmp_path, [
        MemoryChange(text='Transient entry'),
        MemoryChange(action='remove', old_text='Transient entry'),
    ])
    assert changes == [] and skipped is None
    assert read_user_memory(tmp_path) == 'Keep this preference\n'
    assert list((tmp_path / 'memory-history').glob('*')) == before


@pytest.mark.parametrize('existing', ['Keep other entry\n', 'Old\nOld\n'])
def test_missing_or_ambiguous_old_entry_leaves_entire_batch_untouched(tmp_path, existing):
    write_user_memory(tmp_path, existing)
    before = len(list((tmp_path / 'memory-history').glob('*')))
    with pytest.raises(ValueError):
        update_user_memory(tmp_path, [MemoryChange(text='New'), MemoryChange(action='replace', old_text='Old', text='Updated')])
    assert read_user_memory(tmp_path) == existing
    assert len(list((tmp_path / 'memory-history').glob('*'))) == before


def test_changed_snapshot_does_not_resurrect_deleted_memory(tmp_path):
    write_user_memory(tmp_path, 'New manual preference\n')
    _, changes, reason = update_user_memory(tmp_path, [MemoryChange(text='Old deleted preference')], expected='Old preference\n')
    assert changes == [] and reason == 'memory_changed'
    assert read_user_memory(tmp_path) == 'New manual preference\n'


@pytest.mark.parametrize('payload', [
    {'changes': [{'text': 'Invented', 'source_message_id': 'other', 'evidence': 'I use CSV'}]},
    {'changes': [{'text': 'Invented', 'source_message_id': 'm1', 'evidence': 'Assistant recommended CSV'}]},
    {'remember': ['Old unsupported format']},
])
def test_automatic_changes_require_exact_user_source(payload):
    with pytest.raises(ValueError):
        parse_memory_proposals(json.dumps(payload), {'m1': 'I use CSV'})


def test_project_transform_preserves_heading_list_style_and_crlf():
    original = '# Project\r\n\r\n- Use CSV\r\n- Keep source files\r\n'
    content, _ = apply_memory_changes(original, [MemoryChange(action='replace', old_text='Use CSV', text='Use TSV')], project=True)
    assert content == original.replace('Use CSV', 'Use TSV')


class CorrectingProvider(_MemoryAwareProvider):
    async def stream_chat(self, messages, *, cancel_event, tools=()):
        if str(messages[0].get('content', '')).startswith('你帮一个 Agent 记住关于用户的事'):
            source = json.loads(messages[1]['content'])['user_messages']
            message_id, evidence = next(iter(source.items()))
            assert 'Assistant only preference' not in str(messages)
            yield ProviderTextDelta(json.dumps({'changes': [
                {'action': 'replace', 'old_text': '回答尽量简短', 'text': '回答先给结论，再展开理由', 'source_message_id': message_id, 'evidence': evidence},
            ]}, ensure_ascii=False))
            yield ProviderCompleted('stop', None)
            return
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def test_auto_correction_is_persisted_with_source_and_next_session_gets_new_value(tmp_path):
    async def scenario():
        provider = CorrectingProvider([])
        service = _auto_memory_service(tmp_path, provider)
        write_user_memory(tmp_path, '回答尽量简短\n表格默认用 CSV\n')
        try:
            run = await _one_turn(service, '以后回答先给结论，再展开理由，之前的简短要求不用了。', 'correction')
            assert read_user_memory(tmp_path) == '回答先给结论，再展开理由\n表格默认用 CSV\n'
            view = read_memory_changes(service.store.database, run.session_id, [run.run_id])
            change = view[run.run_id][0]
            assert change['action'] == 'replace' and change['evidence'].startswith('以后回答')
            source = service.store.list_messages(run.session_id)[0]
            assert change['source_message_id'] == source.message_id
            await _one_turn(service, '计算 17 加 25，并说明计算过程。', 'next-read', mode='repair_only')
            assert '回答先给结论，再展开理由' in str(provider.messages)
            assert '回答尽量简短' not in str(provider.messages)
            app = FastAPI()
            install_exception_handlers(app)
            app.include_router(create_memory_changes_router(service))
            with TestClient(app) as client:
                assert client.post(f'/api/v1/agent/sessions/{run.session_id}/memory-changes', json={'run_ids': [run.run_id]}).json()['changes'][run.run_id][0]['evidence'] == change['evidence']
                other = await service.create_session(project_id=None, title='Other')
                response = client.post(f'/api/v1/agent/sessions/{other.session_id}/memory-changes', json={'run_ids': [run.run_id]})
                assert response.status_code == 404
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('action,text,expected', [('replace', 'Use TSV', 'Use TSV\n'), ('remove', '', '')])
def test_tool_updates_and_removes_existing_user_entry(tmp_path, action, text, expected):
    async def scenario():
        service, provider, binding, folder = _harness(tmp_path)
        write_user_memory(tmp_path, 'Use CSV\n')
        try:
            session = await service.create_session(project_id=None, title='Memory tools')
            run, _, _ = service.store.create_run(session_id=session.session_id, message='Update preference', expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None, writer_mode='author_review_change_proposal', idempotency_key='tool-update')
            service.store.transition_run(run.run_id, RunStatus.STARTING, detail=None)
            run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail=None)
            result = await _call(service.tool_executor, run, 'memory_write', {'scope': 'user', 'action': action, 'old_text': 'Use CSV', 'text': text})
            assert result.output['applied'] is True
            assert result.output['changes'][0]['action'] == action
            assert read_user_memory(tmp_path) == expected
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_pending_read_cannot_miss_a_save_that_finishes_during_query(tmp_path, monkeypatch):
    from zhimeng_runtime import memory_changes_api
    from types import SimpleNamespace

    class Task:
        finished = False
        def done(self):
            return self.finished

    task = Task()
    def query(*args):
        task.finished = True
        return {}
    monkeypatch.setattr(memory_changes_api, 'read_memory_changes', query)
    app = FastAPI()
    app.include_router(create_memory_changes_router(SimpleNamespace(store=SimpleNamespace(database=None), _tasks={'r1': task})))
    with TestClient(app) as client:
        assert client.post('/api/v1/agent/sessions/s1/memory-changes', json={'run_ids': ['r1']}).json()['pending'] is True
        assert client.post('/api/v1/agent/sessions/s1/memory-changes', json={'run_ids': ['r1']}).json()['pending'] is False


def test_history_response_preserves_legacy_multiline_memory_write():
    legacy_text = 'Prefer concise explanations\nUse CSV tables'
    response = MemoryChangesResponse(session_id='s1', pending=False, changes={
        'r1': [{'action': 'add', 'text': legacy_text, 'origin': 'tool', 'created_at': '2026-09-29T10:00:00Z'}],
    })
    assert response.changes['r1'][0].text == legacy_text
    with pytest.raises(ValueError):
        MemoryChange(text=legacy_text)


@pytest.mark.parametrize('name', ['memory-write-arguments', 'memory-extraction', 'memory-changes-request', 'memory-changes-response'])
def test_memory_contracts_match_models(name):
    from zhimeng_runtime.memory_updates import MemoryExtraction
    from zhimeng_runtime.memory_changes_api import MemoryChangesRequest, MemoryChangesResponse
    models = {'memory-write-arguments': MemoryWriteArguments, 'memory-extraction': MemoryExtraction,
              'memory-changes-request': MemoryChangesRequest, 'memory-changes-response': MemoryChangesResponse}
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts' / f'{name}.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == models[name].model_json_schema(by_alias=True)
