from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_agent_images import VisionProvider, images_in, session, start, upload
from test_agent_parallel_tools import TurnProvider, final_turn, tool_batch_turn
from test_agent_service import wait_terminal
from test_agent_store import begin_tool_call, start_run
from test_memory_permissions import _harness
from zhimeng_runtime.agent_store import AgentStore, AgentStoreConflict, AgentStoreInvalidState
from zhimeng_runtime.app import create_app
from zhimeng_runtime.execution_protocol import RunStatus


@pytest.fixture
def setup(tmp_path):
    service, provider, binding, folder = _harness(tmp_path)
    store = service.store
    session = store.create_session(project_id=binding.project_id, title='Document review',
        executor=provider.identity, capabilities=provider.capabilities)
    return service, store, session, binding, folder


def create(setup, **overrides):
    _service, store, session, binding, _folder = setup
    return store.create_run(**{
        'session_id': session.session_id, 'message': 'Create report.md with an item table and total.',
        'expected_project_revision': binding.revision, 'expected_project_fingerprint': binding.fingerprint,
        'active_chapter_id': None, 'idempotency_key': uuid4().hex, **overrides,
    })


def complete(store, run, content='The item table and total are complete.'):
    start_run(store, run.run_id)
    store.complete_run(run.run_id, content=content, message_id='message-' + uuid4().hex, finish_reason='stop', usage=None)


def review(setup, source, **overrides):
    return create(setup, **{'message': 'Review the actual result.', 'review_source_run_id': source.run_id,
        'writer_mode': 'repair_only', **overrides})


def test_review_rebuilds_requests_candidate_attachments_and_current_change_states(setup):
    _service, store, session, _binding, folder = setup
    source, request, _ = create(setup, manual_file_ids=['file-source'], open_file_path='source.csv')
    start_run(store, source.run_id)
    begin_tool_call(store, source.run_id, tool_name='read_file')
    store.finish_tool_call(source.run_id, tool_call_id='toolcall-0123456789abcdef', status='succeeded',
        output={'content': 'STALE_TOOL_CONTENT'}, error=None, duration_ms=1)
    store.save_run_transcript(source.run_id, [{'role': 'assistant', 'content': 'OLD_REASONING_NOT_EVIDENCE'}])
    store.complete_run(source.run_id, content='CLAIM: all requirements met.', message_id='message-candidate', finish_reason='stop', usage=None)
    child, _, _ = review(setup, source)
    with store.database.connection() as connection:
        connection.execute("INSERT INTO file_change(change_id,seq,session_id,run_id,tool_call_id,title,operation,path,created_at,undone_at) VALUES(?,0,?,?,?,'report','create',?,?,?)",
            ('change-review', session.session_id, source.run_id, 'tool-file', str(folder / 'report.md'), source.created_at.isoformat(), source.created_at.isoformat()))
        connection.commit()
    restarted = AgentStore(store.database)
    messages = restarted.review_provider_messages(child.run_id, max_characters=120000)
    text = json.dumps(messages, ensure_ascii=False)
    assert source.objective in text and 'CLAIM: all requirements met.' in text
    assert 'OLD_REASONING_NOT_EVIDENCE' not in text and 'STALE_TOOL_CONTENT' not in text
    assert 'file-source' in text and 'source.csv' in text and 'undone_at' in text
    assert 'records_are_not_current_file_verification' in text
    meta = restarted.message_review_sources([child.run_id])[child.run_id]
    assert meta.source_run_id == source.run_id and meta.source_message_id == request.message_id
    assert restarted.message_review_sources([source.run_id]) == {}
    assert restarted.review_provider_messages(source.run_id, max_characters=120000) is None


def test_original_continuation_requests_are_included_and_review_continuation_stays_readonly(setup):
    _service, store, _session, _binding, _folder = setup
    source, _, _ = create(setup)
    start_run(store, source.run_id)
    store.mark_interrupted(source.run_id, detail='Stopped')
    continuation, _, _ = create(setup, message='Also include tax.', continuation_source_run_id=source.run_id)
    complete(store, continuation)
    child, _, _ = review(setup, continuation)
    start_run(store, child.run_id)
    store.mark_interrupted(child.run_id, detail='Review interrupted')
    resumed, _, _ = create(setup, message='Continue review.', continuation_source_run_id=child.run_id,
        writer_mode='author_review_change_proposal')
    assert store.get_writer_mode(resumed.run_id) == 'repair_only'
    text = json.dumps(store.review_provider_messages(resumed.run_id, max_characters=120000))
    assert source.objective in text and 'Also include tax.' in text
    assert store.message_review_sources([resumed.run_id])[resumed.run_id].source_run_id == continuation.run_id


def test_review_admission_idempotency_and_concurrency(setup):
    _service, store, session, _binding, _folder = setup
    source, _, _ = create(setup)
    complete(store, source)
    def attempt(_):
        return review(setup, source, idempotency_key='same-review-key')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results[0][0].run_id == results[1][0].run_id
    assert sum(result[2] for result in results) == 1
    with pytest.raises(AgentStoreConflict):
        create(setup, message='Review the actual result.', writer_mode='repair_only', idempotency_key='same-review-key')
    with pytest.raises(AgentStoreConflict):
        review(setup, source)
    assert len(store.list_runs(session.session_id)) == 2


@pytest.mark.parametrize('case', ['active', 'other_session', 'rebound', 'closed', 'full', 'mixed'])
def test_review_scope_and_mode_are_checked_before_admission(setup, case):
    service, store, session, _binding, _folder = setup
    source, _, _ = create(setup)
    if case != 'active':
        complete(store, source)
    kwargs = {}
    if case == 'other_session':
        other = store.create_session(project_id=None, title='Other', executor=service.provider.identity, capabilities=service.provider.capabilities)
        kwargs = {'session_id': other.session_id, 'expected_project_revision': None, 'expected_project_fingerprint': None}
    elif case == 'rebound':
        store.rebind_session(session.session_id, project_id=None)
        kwargs = {'expected_project_revision': None, 'expected_project_fingerprint': None}
    elif case == 'closed':
        store.close_session(session.session_id)
    elif case == 'full':
        kwargs = {'writer_mode': 'author_review_change_proposal'}
    elif case == 'mixed':
        kwargs = {'continuation_source_run_id': source.run_id}
    with pytest.raises(AgentStoreInvalidState if case == 'closed' else AgentStoreConflict):
        review(setup, source, **kwargs)


def test_long_review_excerpts_expose_omissions_and_retrievable_message_ids(setup):
    _service, store, _session, _binding, _folder = setup
    source, request, _ = create(setup, message='requirement ' * 4000)
    complete(store, source, content='candidate ' * 4000)
    child, _, _ = review(setup, source)
    text = json.dumps(store.review_provider_messages(child.run_id, max_characters=4000), ensure_ascii=False)
    assert request.message_id in text and 'requirement' in text and 'candidate' in text
    assert text.count('余下内容可按原消息编号读取') == 2
    assert len(text) < 6000


def test_review_loop_reads_current_file_and_normal_followup_sees_the_result(setup):
    service, store, session, binding, folder = setup
    path = folder / 'README.md'
    initial_content = path.read_text(encoding='utf-8')
    files = service.tool_executor.reader.inspect(folder)['files']
    attached_id = next(item['file_id'] for item in files if item['relative_path'] == 'README.md')
    source, _, _ = create(setup, message='Add an item table and total to the attached document.', manual_file_ids=[attached_id])
    complete(store, source)
    provider = TurnProvider([
        tool_batch_turn([('tool-review-read', 'read_file', {'path': str(path)})]),
        final_turn('The current report is missing the item table.'),
        final_turn('I can use the review to repair the report.'),
    ])
    service.provider = provider
    async def scenario():
        try:
            await service.update_session(session.session_id, rebind_provider=True)
            started = await service.start_run(session_id=session.session_id, message='Review the actual result.',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode='repair_only', review_source_run_id=source.run_id,
                idempotency_key='review-loop')
            terminal = await wait_terminal(service, started.run.run_id)
            assert terminal.status is RunStatus.SUCCEEDED
            assert '用户附上的文件 README.md' in str(provider.messages_by_turn[0])
            assert initial_content.strip() in str(provider.messages_by_turn[0])
            assert path.read_text(encoding='utf-8') == initial_content
            assert any(message.get('role') == 'tool' and initial_content.strip() in str(message) for message in provider.messages_by_turn[1])
            if task := service._tasks.get(started.run.run_id):
                await task
            latest = store.database.get_project_binding(binding.project_id)
            followup = await service.start_run(session_id=session.session_id, message='Use the review to repair the report.',
                expected_project_revision=latest.revision, expected_project_fingerprint=latest.fingerprint,
                active_chapter_id=None, writer_mode='author_review_change_proposal', idempotency_key='review-followup')
            assert (await wait_terminal(service, followup.run.run_id)).status is RunStatus.SUCCEEDED
            assert 'missing the item table' in str(provider.messages_by_turn[2])
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_review_api_preserves_images_and_page_metadata_after_restart(settings):
    from test_agent_api import wait_for_success
    provider = VisionProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        attachment, _ = upload(client)
        source = start(client, sid, [attachment['image_id']], message='Describe this diagram.').json()['run']['run_id']
        wait_for_success(client, source)
        result = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key': 'image-review'},
            json={'message': 'Review the diagram description.', 'writer_mode': 'repair_only', 'review_source_run_id': source})
        assert result.status_code == 202, result.text
        rid = result.json()['run']['run_id']
        wait_for_success(client, rid)
        assert len(images_in(provider.calls[-1])) == 1
        assert 'Describe this diagram.' in str(provider.calls[-1])
    with TestClient(create_app(settings, agent_provider=VisionProvider())) as client:
        page = client.get(f'/api/v1/agent/sessions/{sid}/messages').json()
        assert page['review_sources'][rid]['source_run_id'] == source
        assert page['review_sources'][rid]['source_run_ordinal'] == 1
        first = client.get(f'/api/v1/agent/sessions/{sid}/messages?before_ordinal=3').json()
        assert first['review_sources'] == {}
