from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from test_agent_parallel_tools import TurnProvider, final_turn, tool_batch_turn
from test_agent_service import wait_terminal
from test_task_review import complete, create, review, setup
from zhimeng_runtime.execution_protocol import RunStatus


def evidence(store, run_id, budget=120000):
    messages = store.review_provider_messages(run_id, max_characters=budget)
    return json.loads(messages[1]['content'].split('\n', 1)[1])


def test_claim_without_tools_has_no_file_verification(setup):
    _service, store, _session, _binding, folder = setup
    source, _, _ = create(setup)
    complete(store, source, 'report.md has been saved and verified.')
    child, _, _ = review(setup, source)
    facts = evidence(store, child.run_id)
    assert facts['execution']['tool_call_count'] == 0
    assert facts['current_artifacts']['record_count'] == 0
    assert facts['current_artifacts']['observations'] == []
    assert not (folder / 'report.md').exists()


@pytest.mark.parametrize('external', [False, True])
def test_review_observes_saved_changed_missing_and_undone_files(setup, external):
    service, store, session, binding, folder = setup
    path = (folder.parent if external else folder) / 'report.md'
    content = '# Report\r\n\r\n| Item | Total |\r\n| --- | ---: |\r\n| Paper | 12 |\r\n'
    provider = TurnProvider([
        tool_batch_turn([('tool-evidence-write', 'write_file', {'path': str(path), 'content': content})]),
        final_turn('Saved report.md.'),
    ])
    service.provider = provider

    async def scenario():
        try:
            await service.update_session(session.session_id, rebind_provider=True)
            started = await service.start_run(session_id=session.session_id, message='Create report.md with a table.',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode='author_review_change_proposal', idempotency_key='evidence-write')
            source = await wait_terminal(service, started.run.run_id)
            assert source.status is RunStatus.SUCCEEDED
            assert path.exists()
            if task := service._tasks.get(source.run_id):
                await task
            latest = store.database.get_project_binding(binding.project_id)
            child, _, _ = review(setup, source, expected_project_revision=latest.revision,
                                 expected_project_fingerprint=latest.fingerprint)
            def observation():
                return evidence(store, child.run_id)['current_artifacts']['observations'][0]
            saved = path.read_bytes()
            first = observation()
            assert first['matches_recorded_state'] is True
            assert first['current']['sha256'] == hashlib.sha256(saved).hexdigest()
            assert '| Paper | 12 |' in first['current']['text_excerpt']
            path.write_bytes(b'Changed after saving.\n')
            changed = observation()
            assert changed['matches_recorded_state'] is False
            assert changed['current']['text_excerpt'] == 'Changed after saving.\n'
            path.unlink()
            missing = observation()
            assert missing['current']['state'] == 'missing'
            assert missing['matches_recorded_state'] is False
            path.write_bytes(saved)
            if external:
                service.tool_executor.file_journal.undo(first['receipt_id'])
            else:
                from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
                changes = service.tool_executor.change_sets
                record = changes.get(first['receipt_id'])
                changes.rollback(first['receipt_id'], ChangeSetRollbackRequest(expected_review_revision=record.review_revision))
            undone = observation()
            assert undone['record_state'] == 'undone'
            assert undone['current']['state'] == 'missing'
            assert undone['matches_recorded_state'] is True
            assert not path.exists()
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['create', 'update', 'delete', 'move', 'mkdir'])
def test_external_receipts_track_each_operation_and_undo(setup, operation):
    from zhimeng_runtime.file_journal import PlannedWrite, PlannedDelete, PlannedMove, PlannedMkdir
    service, store, session, _binding, folder = setup
    path, target = folder.parent / 'notes.txt', folder.parent / 'moved.txt'
    if operation in {'update', 'delete', 'move'}:
        path.write_bytes(b'original')
    source, _, _ = create(setup)
    complete(store, source)
    journal = service.tool_executor.file_journal
    change = journal.apply(session_id=session.session_id, run_id=source.run_id, tool_call_id='receipt-op', title='Files',
        operations=[{'create': PlannedWrite(path, b'updated'), 'update': PlannedWrite(path, b'updated'),
                     'delete': PlannedDelete(path), 'move': PlannedMove(path, target), 'mkdir': PlannedMkdir(path)}[operation]])
    child, _, _ = review(setup, source)
    before = evidence(store, child.run_id)['current_artifacts']['observations']
    assert len(before) == (2 if operation == 'move' else 1)
    assert all(item['matches_recorded_state'] is True for item in before)
    journal.undo(change.change_id)
    after = evidence(store, child.run_id)['current_artifacts']['observations']
    assert all(item['record_state'] == 'undone' and item['matches_recorded_state'] is True for item in after)


def test_snapshot_handles_unicode_binary_large_unreadable_and_concurrent_changes(tmp_path, monkeypatch):
    from pathlib import Path
    from zhimeng_runtime.task_review_evidence import SNAPSHOT_BYTES, observe_file
    path = tmp_path / 'unicode.txt'
    data = '名称\r\n纸张\r\n'.encode('utf-16')
    path.write_bytes(data)
    result = observe_file(path)
    assert result['text_excerpt'] == '名称\r\n纸张\r\n'
    assert result['sha256'] == hashlib.sha256(data).hexdigest()
    path.write_bytes(b'\x00\x01\x02')
    binary = observe_file(path)
    assert binary['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert 'text_excerpt' not in binary
    path.write_bytes(b'a' * (SNAPSHOT_BYTES + 1))
    assert observe_file(path)['reason'] == 'automatic_snapshot_size_limit'
    path.write_bytes(b'stable')
    original = Path.open
    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError('not readable')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', unreadable)
    assert observe_file(path) == {'state': 'unverified', 'reason': 'PermissionError'}
    def replaced(self, *args, **kwargs):
        if self == path:
            replacement = path.with_suffix('.new')
            replacement.write_bytes(b'changed')
            replacement.replace(path)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', replaced)
    assert observe_file(path) == {'state': 'unverified', 'reason': 'changed_during_read'}


def test_evidence_budget_and_secret_masking_keep_omissions_explicit(setup):
    from zhimeng_runtime.file_journal import PlannedWrite
    service, store, session, _binding, folder = setup
    source, _, _ = create(setup)
    complete(store, source)
    secret = 'sk-' + 'abcd1234' * 8
    service.tool_executor.file_journal.apply(session_id=session.session_id, run_id=source.run_id,
        tool_call_id='large-batch', title='Files', operations=[
            PlannedWrite(folder.parent / f'output-{i}.txt', (secret + '\n' + 'data\n' * 1200).encode()) for i in range(40)])
    child, _, _ = review(setup, source)
    facts = evidence(store, child.run_id, budget=9000)
    assert secret not in json.dumps(facts)
    current = facts['current_artifacts']
    assert current['record_count'] == 40
    assert current['omitted_count'] > 0
    assert current['observations'][0]['current']['text_omitted_characters'] > 0
    assert len(json.dumps(store.review_provider_messages(child.run_id, max_characters=9000), ensure_ascii=False)) < 11000
