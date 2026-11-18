from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_task_review import complete, create, setup
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.file_journal import PlannedWrite
from zhimeng_runtime.run_artifacts import RunArtifactsResponse, read_run_artifacts
from zhimeng_runtime.run_artifacts_api import create_run_artifacts_router
from zhimeng_runtime.task_review_evidence import SNAPSHOT_BYTES, observe_file


@pytest.fixture
def case(setup):
    service, store, session, _, folder = setup
    run, _, _ = create(setup)
    complete(store, run)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_run_artifacts_router(service))
    with TestClient(app) as client:
        yield service, store, session, run, folder, client


def write(case, name='result.txt', content=b'first'):
    service, _, session, run, folder, _ = case
    return service.tool_executor.file_journal.apply(session_id=session.session_id, run_id=run.run_id,
        tool_call_id='write-' + name + str(len(content)), title='Ordinary files',
        operations=[PlannedWrite(folder.parent / name, content)])


def url(case):
    return f'/api/v1/agent/sessions/{case[2].session_id}/runs/{case[3].run_id}/artifacts'


def test_latest_write_per_path_and_undo_reads_actual_bytes(case):
    first = write(case)
    second = write(case, content=b'second version')
    service, store, session, run, folder, client = case
    result = client.get(url(case)).json()
    assert result['total_paths'] == 1
    item = result['observations'][0]
    assert item['receipt_id'] == second.change_id and item['matches_recorded_state'] is True
    assert item['current']['sha256'] == hashlib.sha256(b'second version').hexdigest()
    service.tool_executor.file_journal.undo(second.change_id)
    undone = client.get(url(case)).json()['observations'][0]
    assert undone['record_state'] == 'undone' and undone['matches_recorded_state'] is True
    assert (folder.parent / 'result.txt').read_bytes() == b'first'
    service.tool_executor.file_journal.undo(first.change_id)
    gone = client.get(url(case)).json()['observations'][0]
    assert gone['receipt_id'] == first.change_id and gone['current']['state'] == 'missing'
    assert gone['matches_recorded_state'] is True


def test_pagination_revision_scope_and_get_does_not_change_history(case):
    service, store, session, run, folder, client = case
    receipt = service.tool_executor.file_journal.apply(session_id=session.session_id, run_id=run.run_id,
        tool_call_id='batch', title='Many files', operations=[PlannedWrite(folder.parent / f'{i}.txt', b'content') for i in range(23)])
    def snapshot():
        with store.database.connection() as connection:
            return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('run', 'session_message', 'tool_call', 'file_change', 'change_set')}
    before = snapshot()
    page = client.get(url(case)).json()
    assert len(page['observations']) == 20 and page['next_offset'] == 20
    tail = client.get(url(case), params={'offset': 20, 'revision': page['revision']}).json()
    assert len(tail['observations']) == 3 and tail['next_offset'] is None
    assert len({item['path'] for item in page['observations'] + tail['observations']}) == 23
    assert snapshot() == before
    assert client.get(url(case).replace(session.session_id, 'another-session')).status_code == 404
    assert client.get(url(case), params={'offset': 20}).status_code == 409
    service.tool_executor.file_journal.undo(receipt.change_id)
    assert client.get(url(case), params={'offset': 20, 'revision': page['revision']}).status_code == 409
    assert client.get(url(case)).json()['observations'][0]['record_state'] == 'undone'


def test_empty_closed_and_bad_records_are_distinct(case):
    _, store, session, _, _, client = case
    assert client.get(url(case)).json()['observations'] == []
    receipt = write(case)
    with store.database.connection() as connection:
        connection.execute("UPDATE file_change SET path='relative.txt', apply_phase='pending' WHERE change_id=?", (receipt.change_id,))
    item = client.get(url(case)).json()['observations'][0]
    assert item['expected_state'] == 'unknown' and item['matches_recorded_state'] is None
    assert item['current']['state'] == 'unverified'
    with store.database.connection() as connection:
        connection.execute("UPDATE execution_session SET status='closed' WHERE id=?", (session.session_id,))
    assert client.get(url(case)).status_code == 404


def test_file_changes_missing_and_io_failure_do_not_masquerade_as_success(case, monkeypatch):
    write(case)
    _, _, _, _, folder, client = case
    path = folder.parent / 'result.txt'
    path.write_bytes(b'edited elsewhere')
    item = client.get(url(case)).json()['observations'][0]
    assert item['matches_recorded_state'] is False and item['current']['state'] == 'file'
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == path: raise PermissionError('denied')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', denied)
    item = client.get(url(case)).json()['observations'][0]
    assert item['matches_recorded_state'] is None and item['current']['state'] == 'unverified'
    path.unlink()
    item = client.get(url(case)).json()['observations'][0]
    assert item['matches_recorded_state'] is False and item['current']['state'] == 'missing'


def test_file_hash_streams_large_files_without_text_and_can_cancel(tmp_path):
    path = tmp_path / 'large.bin'
    data = b'\x00\x01' * SNAPSHOT_BYTES
    path.write_bytes(data)
    result = observe_file(path, include_text=False)
    assert result == {'state': 'file', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    cancelled = threading.Event()
    cancelled.set()
    assert observe_file(path, include_text=False, cancelled=cancelled)['state'] == 'unverified'


def test_malformed_receipt_does_not_look_like_missing_run(case, monkeypatch):
    import zhimeng_runtime.task_review_evidence as module
    # A persisted manifest missing a required field raises KeyError, which is
    # also a LookupError. The API must preserve "cannot parse" rather than 404.
    import zhimeng_runtime.run_artifacts as reader
    def malformed(*args):
        return module.artifact_targets([{'operation': 'create'}])
    monkeypatch.setattr(reader, '_receipts', malformed)
    response = case[-1].get(url(case))
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'run_artifacts_invalid'


def test_rollback_during_read_requires_refresh_instead_of_mixed_snapshot(case, monkeypatch):
    import zhimeng_runtime.run_artifacts as module
    receipt = write(case)
    service, _, _, _, _, client = case
    original = module.observe_artifact
    def undo_then_read(*args, **kwargs):
        service.tool_executor.file_journal.undo(receipt.change_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'observe_artifact', undo_then_read)
    response = client.get(url(case))
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'run_artifacts_changed'


def test_run_artifact_contract_matches_model():
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / 'packages/contracts/run-artifacts-response.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id'); schema.pop('$schema')
    assert schema == RunArtifactsResponse.model_json_schema(by_alias=True)


@pytest.mark.parametrize('operation', ['write_file', 'delete_file', 'move_file'])
def test_project_manifests_follow_last_write_and_inverse_rollback(setup, operation):
    from test_agent_store import start_run
    from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
    service, store, session, _, folder = setup
    run, _, _ = create(setup, writer_mode='author_review_change_proposal')
    start_run(store, run.run_id)
    path = folder / 'report.txt'
    target = folder / 'moved.txt'
    async def call(name, args, call_id):
        result = await service._execute_tool_call(run, provider_tool_call_id=call_id,
            tool_name=name, arguments=args, cancel_event=asyncio.Event())
        assert result.tool_call.status == 'succeeded', result.tool_call
    async def scenario():
        try:
            await call('write_file', {'path': str(path), 'content': 'first'}, 'write-first')
            args = {'path': str(path), 'content': 'second'} if operation == 'write_file' else (
                {'path': str(path)} if operation == 'delete_file' else {'path': str(path), 'target_path': str(target)})
            await call(operation, args, 'change-last')
            facts = read_run_artifacts(store.database, session.session_id, run.run_id)
            assert facts.total_paths == (2 if operation == 'move_file' else 1)
            assert all(item.matches_recorded_state for item in facts.observations)
            change_id = facts.observations[0].receipt_id
            changes = service.tool_executor.change_sets
            changes.rollback(change_id, ChangeSetRollbackRequest(expected_review_revision=changes.get(change_id).review_revision))
            undone = read_run_artifacts(store.database, session.session_id, run.run_id)
            assert all(item.matches_recorded_state and item.record_state == 'undone' for item in undone.observations)
            assert path.read_bytes() == b'first' and not target.exists()
        finally:
            await service.shutdown()
    asyncio.run(scenario())
