from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session, enqueue
from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_queue import AgentQueueStore, QueueInput, QueueSteerRequest, QueueSteerResponse
from zhimeng_runtime.agent_steering import AgentSteeringStore
from zhimeng_runtime.agent_store import AgentSteeringPending, AgentStoreConflict
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.database import SCHEMA_VERSION


class BoundaryProvider(ApiProvider):
    def __init__(self, *, tool_path=None, block=True):
        self.calls = []
        self.catalogs = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.tool_path = tool_path
        self.block = block

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.calls.append(copy.deepcopy(messages))
        self.catalogs.append({tool.name for tool in tools})
        if len(self.calls) == 1:
            if self.tool_path:
                yield ProviderToolCall(0, 'steer-read-call', 'read_file', {'path': str(self.tool_path)})
                yield ProviderCompleted('tool_calls', None)
                return
            yield ProviderTextDelta('Initial answer.')
            self.entered.set()
            while self.block and not self.release.is_set():
                if cancel_event.is_set():
                    return
                await asyncio.sleep(0.01)
        else:
            yield ProviderTextDelta('Answer with the additional input.')
        yield ProviderCompleted('stop', None)


def start(client, sid, key='initial-request-1', message='Explain this document.'):
    response = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key': key},
        json={'message': message, 'writer_mode': 'repair_only'})
    assert response.status_code == 202, response.text
    return response.json()['run']['run_id']


def steer(client, sid, run_id, item_id='message-0001'):
    return client.post(f'/api/v1/agent/sessions/{sid}/queue/{item_id}/steer', json={'expected_run_id': run_id})


def test_input_during_final_answer_continues_same_run_once_and_history_keeps_order(settings):
    provider = BoundaryProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert provider.entered.wait(10)
        enqueue(client, sid, message='Use a table instead.')
        accepted = steer(client, sid, rid)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['queue']['items'][0]['delivery'] == 'steer_pending'
        assert accepted.json()['queue']['items'][0]['steering_run_id'] == rid
        assert steer(client, sid, rid).status_code == 200
        provider.release.set()
        terminal = wait_for_success(client, rid)
        assert terminal['status'] == 'succeeded', terminal
        store = client.app.state.agent_service.store
        assert len(store.list_runs(sid)) == 1
        users = [message for message in store.list_messages(sid) if message.role == 'user']
        assert [message.content for message in users] == ['Explain this document.', 'Use a table instead.']
        assert all(message.run_id == rid for message in users)
        assert client.get(f'/api/v1/agent/sessions/{sid}/queue').json()['items'] == []
        assert steer(client, sid, rid).status_code == 200
        assert client.post(f'/api/v1/agent/sessions/{sid}/queue/message-0001/dispatch').status_code == 409
        assert enqueue(client, sid, message='Use a table instead.')['items'] == []
        history = store.provider_history(sid)
        text = [part.get('content') for part in history]
        assert text.count('Use a table instead.') == 1
        assert text.index('Initial answer.') < text.index('Use a table instead.') < text.index('Answer with the additional input.')
        next_run = start(client, sid, 'next-request-2', 'Summarize the decision.')
        assert wait_for_success(client, next_run)['status'] == 'succeeded'
        assert sum(part.get('content') == 'Use a table instead.' for part in provider.calls[-1]) == 1


def test_steering_arrives_during_tool_batch_after_all_tool_results(settings, tmp_path, monkeypatch):
    path = tmp_path / 'plain.txt'
    path.write_bytes(b'Plain tool evidence.')
    provider = BoundaryProvider(tool_path=path)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        service = client.app.state.agent_service
        entered, release = threading.Event(), threading.Event()
        original = service.tool_executor.execute

        async def blocked(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        monkeypatch.setattr(service.tool_executor, 'execute', blocked)
        sid = session(client)
        rid = start(client, sid)
        assert entered.wait(10)
        enqueue(client, sid, message='Focus on the second point.')
        assert steer(client, sid, rid).status_code == 200
        release.set()
        terminal = wait_for_success(client, rid)
        assert terminal['status'] == 'succeeded', terminal
        second = provider.calls[1]
        correction_index = next(i for i, part in enumerate(second) if part.get('content') == 'Focus on the second point.')
        assert second[correction_index - 1]['role'] == 'tool'
        invocation = second[correction_index - 2]
        assert invocation['role'] == 'assistant'
        assert invocation['tool_calls'][0]['id'] == second[correction_index - 1]['tool_call_id']
        assert len(service.store.list_runs(sid)) == 1


def test_late_steering_wins_completion_transaction_race(settings, monkeypatch):
    provider = BoundaryProvider(block=False)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.agent_service.store
        original = store.complete_run
        entered, release = threading.Event(), threading.Event()
        attempts = 0

        def delayed(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                assert release.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, 'complete_run', delayed)
        sid = session(client)
        rid = start(client, sid)
        assert entered.wait(10)
        enqueue(client, sid, message='The late correction must be handled.')
        assert steer(client, sid, rid).status_code == 200
        release.set()
        terminal = wait_for_success(client, rid)
        assert terminal['status'] == 'succeeded', terminal
        assert attempts == 2
        assert len(store.list_runs(sid)) == 1
        assert any(part.get('content') == 'The late correction must be handled.' for part in provider.calls[1])


@pytest.mark.parametrize('end', ['cancel', 'restart'])
def test_pending_input_returns_to_queue_after_cancel_or_restart(settings, end):
    provider = BoundaryProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert provider.entered.wait(10)
        enqueue(client, sid, message='Keep this unsent correction.')
        assert steer(client, sid, rid).status_code == 200
        if end == 'cancel':
            assert client.post(f'/api/v1/agent/runs/{rid}/cancel').status_code == 200
            terminal = wait_for_success(client, rid)
            assert terminal['status'] == 'cancelled'
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        queue = restarted.get(f'/api/v1/agent/sessions/{sid}/queue').json()
        assert len(queue['items']) == 1
        assert queue['items'][0]['message'] == 'Keep this unsent correction.'
        assert queue['items'][0]['delivery'] == 'queued'
        assert queue['items'][0]['steering_run_id'] is None
        assert queue['pause_reason'] in {'cancelled', 'interrupted'}
        assert steer(restarted, sid, rid).status_code == 409


def test_finished_or_wrong_run_rejects_without_removing_queued_input(settings):
    with TestClient(create_app(settings, agent_provider=BoundaryProvider(block=False))) as client:
        sid = session(client)
        rid = start(client, sid)
        assert wait_for_success(client, rid)['status'] == 'succeeded'
        enqueue(client, sid)
        assert steer(client, sid, rid).status_code == 409
        other = session(client)
        assert steer(client, other, rid).status_code == 409
        assert len(client.get(f'/api/v1/agent/sessions/{sid}/queue').json()['items']) == 1


def test_attachment_skills_and_readonly_mode_are_preserved(tmp_path):
    from test_memory_permissions import _harness
    from test_agent_service import wait_terminal

    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        provider = BoundaryProvider()
        service.provider = provider
        snapshot = service.tool_executor.reader.inspect(folder)
        file_id = next(item['file_id'] for item in snapshot['files'] if item['relative_path'] == 'README.md')
        try:
            current = await service.create_session(project_id=binding.project_id, title='Steering attachment check')
            result = await service.start_run(session_id=current.session_id, message='Explain this document.',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode='repair_only', idempotency_key='attachment-steer-1')
            for _ in range(1000):
                if provider.entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert provider.entered.is_set()
            queue = AgentQueueStore(service.store)
            queue.enqueue(current.session_id, QueueInput(item_id='attachment-input-1', message='Use this attached source.',
                project_id=binding.project_id, writer_mode='author_review_change_proposal', manual_file_ids=[file_id],
                manual_skill_names=['文档整理'], open_file_path='README.md'))
            queue.steer(current.session_id, 'attachment-input-1', result.run.run_id)
            provider.release.set()
            terminal = await wait_terminal(service, result.run.run_id)
            assert terminal.status == RunStatus.SUCCEEDED, terminal.error
            second = '\n'.join(str(part.get('content', '')) for part in provider.calls[1])
            assert 'A plain document project.' in second
            assert 'README.md' in second and '文档整理' in second
            assert service.store.get_writer_mode(result.run.run_id) == 'repair_only'
            assert 'write_file' not in provider.catalogs[1]
        finally:
            provider.release.set()
            await service.shutdown()
    asyncio.run(scenario())


def test_partial_transcript_does_not_hide_later_visible_assistant_on_cancel(tmp_path):
    from test_agent_store import initialized_store, create_session, create_run, start_run
    store = initialized_store(tmp_path)
    current = create_session(store)
    run, _, _ = create_run(store, current.session_id)
    start_run(store, run.run_id)
    queue = AgentQueueStore(store)
    queue.enqueue(current.session_id, QueueInput(item_id='partial-input-1', message='Supplement.', project_id=current.project_id))
    queue.steer(current.session_id, 'partial-input-1', run.run_id)
    with pytest.raises(AgentSteeringPending):
        store.complete_run(run.run_id, message_id='message-finish', content='premature', finish_reason='stop', usage=None)
    steering = AgentSteeringStore(store)
    steering.consume(run.run_id, [('partial-input-1', [{'role': 'user', 'content': 'Supplement.'}])], [])
    store.finalize_assistant_message(run.run_id, message_id='message-visible', content='VISIBLE_AFTER_STEER')
    store.mark_cancelled(run.run_id)
    history = store.provider_history(current.session_id)
    assert [part.get('content') for part in history].count('Supplement.') == 1
    assert any(part.get('content') == 'VISIBLE_AFTER_STEER' for part in history)


def test_force_compaction_keeps_latest_steered_run_whole(tmp_path, monkeypatch):
    from test_memory_permissions import _harness
    from test_session_checkpoint_wiring import full_summary

    async def scenario():
        service, _, _, _ = _harness(tmp_path)
        current = await service.create_session(project_id=None, title='Steering compaction evidence')

        def begin(key, text):
            run, _, _ = service.store.create_run(session_id=current.session_id, message=text,
                expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
                idempotency_key=key)
            service.store.transition_run(run.run_id, RunStatus.STARTING, detail='start')
            service.store.transition_run(run.run_id, RunStatus.RUNNING, detail='run')
            return run

        first = begin('compact-first-1', 'Older question.')
        service.store.complete_run(first.run_id, message_id='message-old-answer', content='Older answer.', finish_reason='stop', usage=None)
        latest = begin('compact-latest-2', 'Latest question.')
        service.store.finalize_assistant_message(latest.run_id, message_id='message-first-part', content='Latest first answer.')
        queue = AgentQueueStore(service.store)
        queue.enqueue(current.session_id, QueueInput(item_id='compact-input-1', message='Latest correction.'))
        queue.steer(current.session_id, 'compact-input-1', latest.run_id)
        prefix = [{'role': 'assistant', 'content': 'Latest first answer.'}]
        addition = AgentSteeringStore(service.store).consume(latest.run_id,
            [('compact-input-1', [{'role': 'user', 'content': 'Latest correction.'}])], prefix)
        service.store.complete_run(latest.run_id, message_id='message-final-part', content='Latest final answer.', finish_reason='stop', usage=None,
            transcript=[*prefix, *addition, {'role': 'assistant', 'content': 'Latest final answer.'}])
        _, completed = service.store.completed_history(current.session_id)
        assert [message.role for message in completed] == ['user', 'assistant', 'user', 'assistant', 'user', 'assistant']

        async def summarize(*args, **kwargs):
            return full_summary('Older question and answer.'), None
        monkeypatch.setattr(service, '_stream_checkpoint_text', summarize)
        try:
            checkpoint = await service._fold_session_history(current.session_id, force=True)
            assert checkpoint.through_ordinal == 2
            history = [part.get('content') for part in service.store.provider_history(current.session_id)]
            assert history.count('Latest question.') == 1
            assert history.count('Latest correction.') == 1
            assert history.index('Latest first answer.') < history.index('Latest correction.') < history.index('Latest final answer.')
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_schema_27_upgrade_retains_transcript_and_adds_steering_receipts(tmp_path):
    from test_agent_store import initialized_store, create_session, create_run, start_run
    store = initialized_store(tmp_path)
    current = create_session(store)
    run, _, _ = create_run(store, current.session_id)
    start_run(store, run.run_id)
    store.complete_run(run.run_id, message_id='message-migration', content='Preserved answer.', finish_reason='stop', usage=None)
    store.save_run_transcript(run.run_id, [{'role': 'assistant', 'content': 'Preserved answer.'}])
    with store.database.connection() as db:
        db.execute('ALTER TABLE run_transcript DROP COLUMN through_message_ordinal')
        db.execute('DROP TABLE session_steering')
        db.execute('PRAGMA user_version=27')
    assert store.database.initialize().schema_version == SCHEMA_VERSION
    assert [part['content'] for part in store.provider_history(current.session_id)].count('Preserved answer.') == 1
    assert AgentQueueStore(store).read(current.session_id).items == []


@pytest.mark.parametrize('name,model', [('request', QueueSteerRequest), ('response', QueueSteerResponse)])
def test_steer_contracts_match_models(name, model):
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts' / f'agent-queue-steer-{name}.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == model.model_json_schema()
