import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from zhimeng_runtime.app import create_app
from zhimeng_runtime.file_changes_api import SessionFileChangeDetail, SessionFileChanges
from zhimeng_runtime.file_journal import FileJournalError, PlannedDelete, PlannedMkdir, PlannedMove, PlannedWrite


@pytest.fixture
def file_client(settings):
    app = create_app(settings, agent_provider=ApiProvider())
    with TestClient(app) as client:
        yield client


def session(client):
    response = client.post('/api/v1/agent/sessions', json={'title': 'Plain files'})
    assert response.status_code == 201, response.text
    return response.json()['session_id']


def record(client, sid, operations, call_id='file-call-1'):
    return client.app.state.file_change_journal.apply(
        session_id=sid, run_id='run-file-check', tool_call_id=call_id,
        title='整理文件', operations=operations,
    )


def url(sid, change_id=None):
    return f'/api/v1/agent/sessions/{sid}/file-changes' + (f'/{change_id}' if change_id else '')


def test_file_change_diff_and_undo_survive_restart(file_client, settings, tmp_path):
    sid = session(file_client)
    target = tmp_path / 'notes.txt'
    target.write_bytes(b'before\n')
    change = record(file_client, sid, [PlannedWrite(target, b'after\n')])
    detail_url = url(sid, change.change_id)
    detail = file_client.get(detail_url).json()
    assert detail['undo_available'] is True
    assert detail['previews'][0]['before_text'] == 'before\n'
    assert detail['previews'][0]['after_text'] == 'after\n'
    assert detail['previews'][0]['before_characters'] == 7
    assert detail['previews'][0]['after_characters'] == 6
    assert 'backup_path' not in str(detail)
    # 新的服务对象读取同一数据库，已写/已撤回都由持久记录恢复。
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        assert restarted.get(url(sid)).json()['changes'][0]['status'] == 'applied'
        undone = restarted.post(detail_url + '/undo')
        assert undone.status_code == 200, undone.text
        assert undone.json()['status'] == 'undone'
        assert target.read_bytes() == b'before\n'
        assert restarted.get(url(sid)).json()['changes'][0]['status'] == 'undone'
        assert restarted.post(detail_url + '/undo').status_code == 200
    preview = file_client.get(detail_url).json()['previews'][0]
    assert preview['after_text'] is None
    assert preview['before_characters'] is preview['after_characters'] is None


def test_other_session_cannot_read_or_undo_change(file_client, tmp_path):
    first, second = session(file_client), session(file_client)
    path = tmp_path / 'a.txt'
    change = record(file_client, first, [PlannedWrite(path, b'new')])
    assert file_client.get(url(second)).json()['changes'] == []
    assert file_client.get(url(second, change.change_id)).status_code == 404
    assert file_client.post(url(second, change.change_id) + '/undo').status_code == 404
    assert file_client.get(url('missing-session')).status_code == 404
    assert path.read_bytes() == b'new'


@pytest.mark.parametrize('operation_kind', ['create', 'update', 'delete', 'move', 'mkdir'])
def test_interrupted_file_record_checks_disk_and_can_finish_undo(file_client, tmp_path, operation_kind):
    sid = session(file_client)
    path, target = tmp_path / 'document.txt', tmp_path / 'renamed.txt'
    if operation_kind in {'update', 'delete', 'move'}:
        path.write_bytes(b'original')
    operations = {
        'create': PlannedWrite(path, b'updated'),
        'update': PlannedWrite(path, b'updated'),
        'delete': PlannedDelete(path),
        'move': PlannedMove(path, target),
        'mkdir': PlannedMkdir(path),
    }
    change = record(file_client, sid, [operations[operation_kind]])
    journal = file_client.app.state.file_change_journal
    # Disk has the old state, as after a crash before apply or after undo before its commit.
    journal._reverse(change.entries[0])
    with pytest.raises(FileJournalError, match='未能确认完整落盘'):
        record(file_client, sid, [operations[operation_kind]])
    detail_url = url(sid, change.change_id)
    detail = file_client.get(detail_url).json()
    assert detail['undo_available'] is True
    assert '修改前的状态' in detail['previews'][0]['preview_note']
    undone = file_client.post(detail_url + '/undo')
    assert undone.status_code == 200, undone.text
    assert undone.json()['status'] == 'undone'
    assert not target.exists()
    if operation_kind in {'update', 'delete', 'move'}:
        assert path.read_bytes() == b'original'
    else:
        assert not path.exists()


def test_interrupted_batch_preserves_external_edit_until_conflict_is_resolved(file_client, tmp_path):
    sid = session(file_client)
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_bytes(b'original')
    second.write_bytes(b'original')
    operations = [PlannedWrite(first, b'agent'), PlannedWrite(second, b'agent')]
    change = record(file_client, sid, operations)
    first.write_bytes(b'original')
    second.write_bytes(b'user edit')
    with pytest.raises(FileJournalError):
        record(file_client, sid, operations)
    detail_url = url(sid, change.change_id)
    assert file_client.post(detail_url + '/undo').status_code == 409
    assert first.read_bytes() == b'original'
    assert second.read_bytes() == b'user edit'
    second.write_bytes(b'agent')
    assert file_client.post(detail_url + '/undo').status_code == 200
    assert first.read_bytes() == second.read_bytes() == b'original'


def test_changed_file_is_preserved_and_never_shown_as_historical_diff(file_client, tmp_path):
    sid = session(file_client)
    path = tmp_path / 'changed.txt'
    path.write_bytes(b'old')
    change = record(file_client, sid, [PlannedWrite(path, b'agent')])
    path.write_bytes(b'user edit')
    detail_url = url(sid, change.change_id)
    detail = file_client.get(detail_url).json()
    assert detail['undo_available'] is False
    assert detail['previews'][0]['before_text'] is None
    assert detail['previews'][0]['after_text'] is None
    assert detail['previews'][0]['before_characters'] is detail['previews'][0]['after_characters'] is None
    assert file_client.post(detail_url + '/undo').status_code == 409
    assert path.read_bytes() == b'user edit'


def test_preview_io_failure_keeps_record_readable_and_allows_retry(file_client, tmp_path, monkeypatch):
    sid = session(file_client)
    path = tmp_path / 'busy.txt'
    path.write_bytes(b'original')
    change = record(file_client, sid, [PlannedWrite(path, b'updated')])
    detail_url = url(sid, change.change_id)
    original_open = Path.open

    def busy_open(current, *args, **kwargs):
        if current == path:
            raise OSError(5, 'fixture I/O failure')
        return original_open(current, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', busy_open)
        response = file_client.get(detail_url)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['status'] == 'applied'
        assert payload['undo_available'] is False
        assert '文件占用或权限' in payload['undo_reason']
        preview = payload['previews'][0]
        assert preview['before_text'] is preview['after_text'] is None
        assert preview['before_characters'] is preview['after_characters'] is None
        assert '无法读取' in preview['preview_note']
        failed = file_client.post(detail_url + '/undo')
        assert failed.status_code == 409, failed.text
        assert failed.json()['error']['code'] == 'file_change_io_failed'
        assert file_client.get(url(sid)).json()['changes'][0]['status'] == 'applied'

    assert path.read_bytes() == b'updated'
    assert Path(change.entries[0].backup_path).read_bytes() == b'original'
    assert file_client.get(detail_url).json()['undo_available'] is True
    assert file_client.post(detail_url + '/undo').status_code == 200
    assert path.read_bytes() == b'original'


def test_preview_masks_keys_without_touching_files(file_client, tmp_path):
    sid = session(file_client)
    secret = 'sk-' + 'abcdef1234567890' * 2
    path = tmp_path / 'settings.txt'
    contents = f'api_key={secret}\n'.encode()
    change = record(file_client, sid, [PlannedWrite(path, contents)])
    response = file_client.get(url(sid, change.change_id))
    assert response.status_code == 200
    assert secret not in response.text
    assert response.json()['previews'][0]['masked_secrets'] > 0
    assert response.json()['previews'][0]['before_characters'] == 0
    assert response.json()['previews'][0]['after_characters'] == len(contents.decode())
    assert path.read_bytes() == contents


def test_corrupt_backup_blocks_undo(file_client, tmp_path):
    sid = session(file_client)
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old')
    change = record(file_client, sid, [PlannedWrite(path, b'new')])
    Path(change.entries[0].backup_path).write_bytes(b'tampered')
    detail = file_client.get(url(sid, change.change_id)).json()
    assert detail['undo_available'] is False
    assert '备份已变化' in detail['undo_reason']
    assert file_client.post(url(sid, change.change_id) + '/undo').status_code == 409
    assert path.read_bytes() == b'new'


def test_partial_undo_is_recorded_and_retries_only_unfinished_files(file_client, tmp_path, monkeypatch):
    sid = session(file_client)
    paths = [tmp_path / f'{i}.txt' for i in range(3)]
    for path in paths:
        path.write_bytes(b'old')
    change = record(file_client, sid, [PlannedWrite(path, b'new') for path in paths])
    journal = file_client.app.state.file_change_journal
    original = journal._reverse

    def fail_middle(entry):
        if entry.path == str(paths[1]):
            raise PermissionError('fixture file busy')
        original(entry)

    monkeypatch.setattr(journal, '_reverse', fail_middle)
    detail_url = url(sid, change.change_id)
    failed = file_client.post(detail_url + '/undo')
    assert failed.status_code == 409, failed.text
    assert '1/3' in failed.json()['error']['message']
    partial = file_client.get(detail_url).json()
    assert partial['status'] == 'partially_undone'
    assert partial['undone_at'] is None
    assert [file['undone_at'] is not None for file in partial['files']] == [False, False, True]
    assert [path.read_bytes() for path in paths] == [b'new', b'new', b'old']
    assert len(journal.get(change.change_id).entries) == 3
    assert len(journal.open_changes(sid)[0].entries) == 2
    # 已撤回的文件由作者继续编辑过，续撤只处理剩余两份。
    paths[2].write_bytes(b'author edits after undo')
    monkeypatch.setattr(journal, '_reverse', original)
    completed = file_client.post(detail_url + '/undo')
    assert completed.status_code == 200, completed.text
    assert completed.json()['status'] == 'undone'
    assert [path.read_bytes() for path in paths] == [b'old', b'old', b'author edits after undo']
    assert journal.open_changes(sid) == []


def test_preflight_checks_all_files_before_any_undo(file_client, tmp_path):
    sid = session(file_client)
    paths = [tmp_path / 'one.txt', tmp_path / 'two.txt']
    change = record(file_client, sid, [PlannedWrite(path, b'new') for path in paths])
    paths[0].write_bytes(b'author edit')
    assert file_client.post(url(sid, change.change_id) + '/undo').status_code == 409
    assert paths[1].read_bytes() == b'new'


@pytest.mark.parametrize('operation', ['delete', 'move', 'mkdir'])
def test_file_operations_show_real_paths_and_undo(file_client, tmp_path, operation):
    sid = session(file_client)
    path, target = tmp_path / 'source.txt', tmp_path / 'moved.txt'
    if operation != 'mkdir':
        path.write_bytes(b'contents')
    planned = {'delete': PlannedDelete(path), 'move': PlannedMove(path, target), 'mkdir': PlannedMkdir(path)}[operation]
    change = record(file_client, sid, [planned])
    detail_url = url(sid, change.change_id)
    detail = file_client.get(detail_url).json()
    assert detail['files'][0]['operation'] == operation
    assert detail['files'][0]['path'] == str(path)
    assert file_client.post(detail_url + '/undo').status_code == 200
    if operation == 'mkdir':
        assert not path.exists()
    else:
        assert path.read_bytes() == b'contents'
        assert not target.exists()


def test_binary_preview_does_not_replace_data(file_client, tmp_path):
    sid = session(file_client)
    path = tmp_path / 'blob.bin'
    change = record(file_client, sid, [PlannedWrite(path, b'\x00\xff\xaa')])
    detail = file_client.get(url(sid, change.change_id)).json()
    assert detail['previews'][0]['after_text'] is None
    assert '非文本' in detail['previews'][0]['preview_note']
    assert detail['undo_available'] is True
    assert detail['previews'][0]['after_characters'] is None


@pytest.mark.parametrize('encoding', [
    'utf-8', 'utf-8-sig', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be',
])
def test_character_counts_use_verified_unicode_and_logical_newlines(file_client, tmp_path, encoding):
    sid = session(file_client)
    path = tmp_path / 'unicode.txt'
    before, after = '记录 😀\r\n保留空白 \r\n', '摘要\n'
    bom = {
        'utf-16-le': b'\xff\xfe', 'utf-16-be': b'\xfe\xff',
        'utf-32-le': b'\xff\xfe\x00\x00', 'utf-32-be': b'\x00\x00\xfe\xff',
    }.get(encoding, b'')
    original = bom + before.encode(encoding)
    updated = bom + after.encode(encoding)
    path.write_bytes(original)
    change = record(file_client, sid, [PlannedWrite(path, updated)])
    preview = file_client.get(url(sid, change.change_id)).json()['previews'][0]
    assert preview['before_text'] == before
    assert preview['after_text'] == after
    assert preview['before_characters'] == len(before) - before.count('\r\n')
    assert preview['after_characters'] == len(after)
    assert path.read_bytes() == updated
    assert file_client.post(url(sid, change.change_id) + '/undo').status_code == 200
    assert path.read_bytes() == original


def test_deleted_text_has_zero_after_characters(file_client, tmp_path):
    sid = session(file_client)
    path = tmp_path / 'delete.txt'
    path.write_bytes(b'old\r\n')
    change = record(file_client, sid, [PlannedDelete(path)])
    preview = file_client.get(url(sid, change.change_id)).json()['previews'][0]
    assert preview['before_characters'] == 4 and preview['after_characters'] == 0


def test_large_preview_leaves_character_counts_unknown(file_client, tmp_path):
    sid = session(file_client)
    path = tmp_path / 'large.txt'
    change = record(file_client, sid, [PlannedWrite(path, b'x' * 512_001)])
    preview = file_client.get(url(sid, change.change_id)).json()['previews'][0]
    assert preview['before_characters'] is preview['after_characters'] is None
    assert preview['after_text'] is None and '较大' in preview['preview_note']


@pytest.mark.parametrize('name,model', [
    ('session-file-changes', SessionFileChanges), ('session-file-change-detail', SessionFileChangeDetail),
])
def test_file_change_contracts_match_models(name, model):
    path = Path(__file__).resolve().parents[3] / 'packages/contracts' / f'{name}.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == model.model_json_schema()
