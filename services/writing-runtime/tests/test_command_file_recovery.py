from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from test_agent_queue import session
from zhimeng_runtime.app import create_app


def interrupted_command(client, folder):
    sid = session(client)
    service = client.app.state.agent_service
    tracker = service.tool_executor._command_file_tracker()
    target = folder / 'report.txt'
    target.write_bytes('执行前报告\r\n'.encode('utf-16'))
    observation = tracker.prepare(session_id=sid, run_id='recovery-run', tool_call_id='recovery-tool',
                                  workdir=folder, paths=['report.txt', 'new.csv'])
    target.write_bytes(b'command result\n')
    (folder / 'new.csv').write_bytes(b'total\n49.50\n')
    command_folder = Path(service.tool_executor.store.path).parent / 'command-sessions' / 'cmd-crash'
    command_folder.mkdir(parents=True)
    (command_folder / 'session.json').write_text(json.dumps({
        'schema': 'zhimeng.command-session.v1', 'session_id': 'cmd-crash',
        'owner_session_id': sid, 'owner_run_id': 'recovery-run', 'status': 'running',
        'file_tracking': observation,
    }), encoding='utf-8')
    # Recreate the process-owned manager just as a new runtime does on startup.
    previous = service.tool_executor._command_sessions
    if previous is not None:
        previous.close()
    service.tool_executor._command_sessions = None
    return sid, target, f'/api/v1/agent/sessions/{sid}/commands/cmd-crash/file-recovery'


def test_interrupted_command_can_compare_backups_without_replaying_or_writing(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, target, url = interrupted_command(client, tmp_path)
        response = client.get(url, params={'path': str(target)})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['source'] == 'current_check'
        item = next(e for e in data['entries'] if e['path'] == str(target))
        assert item['status'] == 'available'
        assert item['before_text'] == '执行前报告\r\n'
        assert item['current_text'] == 'command result\n'
        assert target.read_bytes() == b'command result\n'
        assert client.get(f'/api/v1/agent/sessions/{sid}/file-changes').json()['changes'] == []


@pytest.mark.parametrize('kind', ['update', 'deleted', 'created'])
def test_restore_is_versioned_idempotent_and_itself_undoable(settings, tmp_path, kind):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, target, url = interrupted_command(client, tmp_path)
        if kind == 'deleted':
            target.unlink()
        if kind == 'created':
            target = tmp_path / 'new.csv'
        current = target.read_bytes() if target.exists() else None
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        request = {'path': str(target), 'revision': item['revision'], 'request_id': 'restore-1'}
        result = client.post(url, json=request)
        assert result.status_code == 200, result.text
        receipt = result.json()
        assert receipt['status'] == 'applied'
        if kind == 'created':
            assert not target.exists()
        else:
            assert target.read_bytes() == '执行前报告\r\n'.encode('utf-16')
        assert client.post(url, json=request).json() == receipt
        changes = client.get(f'/api/v1/agent/sessions/{sid}/file-changes').json()['changes']
        assert len(changes) == 1
        undo = client.post(f"/api/v1/agent/sessions/{sid}/file-changes/{receipt['change_id']}/undo")
        assert undo.status_code == 200, undo.text
        assert (target.read_bytes() if target.exists() else None) == current
        assert client.post(url, json=request).json()['status'] == 'undone'
        assert (target.read_bytes() if target.exists() else None) == current


def test_later_edit_and_other_session_are_not_overwritten(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, target, url = interrupted_command(client, tmp_path)
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        target.write_bytes(b'later human edit')
        request = {'path': str(target), 'revision': item['revision'], 'request_id': 'stale'}
        assert client.post(url, json=request).status_code == 409
        other = session(client)
        assert client.get(url.replace(sid, other)).status_code == 404
        assert client.post(url.replace(sid, other), json=request).status_code == 404
        assert target.read_bytes() == b'later human edit'


def test_original_end_snapshot_keeps_later_human_version_out_of_recovery(settings, tmp_path, monkeypatch):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        _, target, url = interrupted_command(client, tmp_path)
        tracker = client.app.state.agent_service.tool_executor._command_file_tracker()
        observation = next(tracker.root.glob('*/observation.json'))
        state = json.loads(observation.read_bytes())
        with monkeypatch.context() as patch:
            def fail(**_): raise OSError('receipt unavailable')
            patch.setattr(tracker.journal, 'record_observed', fail)
            assert tracker.finish_safely(state)['status'] == 'unverified'
        target.write_bytes(b'later human edit')
        data = client.get(url).json()
        assert data['source'] == 'command_end'
        item = next(e for e in data['entries'] if e['path'] == str(target))
        assert item['status'] == 'conflict'
        assert client.post(url, json={'path': str(target), 'revision': item['revision'], 'request_id': 'end-conflict'}).status_code == 409
        assert target.read_bytes() == b'later human edit'


def test_lost_restore_response_can_be_found_without_rewriting_later_edits(settings, tmp_path, monkeypatch):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        _, target, url = interrupted_command(client, tmp_path)
        tracker = client.app.state.agent_service.tool_executor._command_file_tracker()
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        request = {'path': str(target), 'revision': item['revision'], 'request_id': 'lost-response'}
        apply = tracker.journal.apply
        def lost(**kwargs):
            apply(**kwargs)
            raise OSError('lost response after commit')
        with monkeypatch.context() as patch:
            patch.setattr(tracker.journal, 'apply', lost)
            assert client.post(url, json=request).status_code == 503
        target.write_bytes(b'human after recovery')
        result = client.post(url, json=request)
        assert result.status_code == 200 and result.json()['status'] == 'applied'
        assert target.read_bytes() == b'human after recovery'
        assert client.get(url).json()['restorations'][0]['request_id'] == 'lost-response'


def test_missing_backup_is_unavailable_and_cannot_be_restored(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        _, target, url = interrupted_command(client, tmp_path)
        tracker = client.app.state.agent_service.tool_executor._command_file_tracker()
        next(tracker.root.glob('*/*.before')).write_bytes(b'corrupt backup')
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        assert item['status'] == 'unavailable'
        assert client.post(url, json={'path': str(target), 'revision': item['revision'], 'request_id': 'corrupt'}).status_code == 409
        assert target.read_bytes() == b'command result\n'


def test_new_file_changed_between_scan_and_delete_is_preserved(settings, tmp_path, monkeypatch):
    from zhimeng_runtime.file_journal import PlannedDelete
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        _, _, url = interrupted_command(client, tmp_path)
        target = tmp_path / 'new.csv'
        tracker = client.app.state.agent_service.tool_executor._command_file_tracker()
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        plan = tracker.journal._plan
        def changed(operation, backup):
            if isinstance(operation, PlannedDelete): target.write_bytes(b'human replacement')
            return plan(operation, backup)
        monkeypatch.setattr(tracker.journal, '_plan', changed)
        response = client.post(url, json={'path': str(target), 'revision': item['revision'], 'request_id': 'delete-race'})
        assert response.status_code == 409
        assert target.read_bytes() == b'human replacement'


def test_recovery_contracts_match_models():
    from zhimeng_runtime.command_recovery import CommandRecoveryRequest, CommandRecoveryResponse, RecoveryReceipt
    root = Path(__file__).resolve().parents[3] / 'packages/contracts'
    for filename, model in [('command-file-recovery.schema.json', CommandRecoveryResponse),
                            ('command-file-restore-request.schema.json', CommandRecoveryRequest),
                            ('command-file-restore-receipt.schema.json', RecoveryReceipt)]:
        saved = json.loads((root / filename).read_text(encoding='utf-8'))
        saved.pop('$schema', None)
        assert saved == model.model_json_schema(by_alias=True)


def test_incomplete_end_scan_is_not_mislabelled_as_a_later_edit(settings, tmp_path, monkeypatch):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        _, target, url = interrupted_command(client, tmp_path)
        tracker = client.app.state.agent_service.tool_executor._command_file_tracker()
        state = json.loads(next(tracker.root.glob('*/observation.json')).read_bytes())
        with monkeypatch.context() as patch:
            patch.setattr(tracker, '_scan', lambda *args, **kwargs: ({}, [{'path': str(tmp_path), 'reason': 'end scan unavailable'}]))
            patch.setattr(tracker.journal, 'record_observed', lambda **kwargs: (_ for _ in ()).throw(OSError('receipt lost')))
            assert tracker.finish_safely(state)['status'] == 'unverified'
        item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
        assert item['status'] == 'unavailable'
        assert item['note'] == 'end scan unavailable'
        assert target.read_bytes() == b'command result\n'


def test_running_command_and_unreadable_state_do_not_write(settings, tmp_path, monkeypatch):
    import sqlite3
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, target, url = interrupted_command(client, tmp_path)
        executor = client.app.state.agent_service.tool_executor
        manager = executor._command_session_manager()
        record = manager._get(sid, 'cmd-crash')
        record.meta['status'] = 'running'
        assert client.get(url).status_code == 422
        record.meta['status'] = 'interrupted'
        tracker = executor._command_file_tracker()
        with monkeypatch.context() as patch:
            def unreadable(*args, **kwargs): raise sqlite3.OperationalError('database unavailable')
            patch.setattr(tracker.journal, 'apply', unreadable)
            item = next(e for e in client.get(url).json()['entries'] if e['path'] == str(target))
            result = client.post(url, json={'path': str(target), 'revision': item['revision'], 'request_id': 'db-outage'})
            assert result.status_code == 503
        assert client.get(url).json()['restorations'][0]['status'] == 'unconfirmed'
        next(tracker.root.glob('*/observation.json')).write_bytes(b'{broken')
        assert client.get(url).status_code == 503
        assert target.read_bytes() == b'command result\n'
