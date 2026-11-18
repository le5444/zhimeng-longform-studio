"""Outside edits must survive preparation, retry, commit and undo windows."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from zhimeng_runtime.database import SqliteStore
import zhimeng_runtime.file_commit as commit
import zhimeng_runtime.file_journal as module
from zhimeng_runtime.file_journal import FileChangeJournal, FileJournalError, PlannedDelete, PlannedMove, PlannedWrite
from zhimeng_runtime.file_changes_api import _detail


@pytest.fixture
def journal(tmp_path):
    store = SqliteStore(tmp_path / 'runtime.sqlite3')
    store.initialize()
    return FileChangeJournal(store, tmp_path / 'state')


def apply(journal, operations, call='write'):
    return journal.apply(session_id='ordinary', run_id='run-files', tool_call_id=call,
                         title='Update ordinary documents', operations=operations)


@pytest.mark.parametrize('operation', ['write', 'delete', 'move'])
def test_external_edit_after_preparation_is_preserved(journal, tmp_path, monkeypatch, operation):
    path, target = tmp_path / 'notes.txt', tmp_path / 'renamed.txt'
    path.write_bytes(b'original')
    atomic = module._write_atomic

    def edit_after_backup(backup, data):
        atomic(backup, data)
        path.write_bytes(b'saved by another editor')

    if operation == 'move':
        prepare = journal._plan

        def edit_after_move_plan(item, backup):
            result = prepare(item, backup)
            path.write_bytes(b'saved by another editor')
            return result

        monkeypatch.setattr(journal, '_plan', edit_after_move_plan)
    else:
        monkeypatch.setattr(module, '_write_atomic', edit_after_backup)
    plan = {'write': PlannedWrite(path, b'agent'), 'delete': PlannedDelete(path),
            'move': PlannedMove(path, target)}[operation]
    with pytest.raises(FileJournalError) as error:
        apply(journal, [plan])
    assert error.value.code == 'write_conflict'
    assert path.read_bytes() == b'saved by another editor'
    assert not target.exists()
    assert journal.session_changes('ordinary') == []


def test_rejected_second_commit_does_not_compensate_an_equal_outside_edit(journal, tmp_path, monkeypatch):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_bytes(b'original first')
    second.write_bytes(b'original second')
    perform = journal._perform

    def outside_before_second(entry, operation):
        if operation.path == second:
            second.write_bytes(b'agent proposed bytes')
        perform(entry, operation)

    monkeypatch.setattr(journal, '_perform', outside_before_second)
    with pytest.raises(FileJournalError) as error:
        apply(journal, [PlannedWrite(first, b'agent first'), PlannedWrite(second, b'agent proposed bytes')])
    assert error.value.code == 'write_conflict'
    assert first.read_bytes() == b'original first'
    assert second.read_bytes() == b'agent proposed bytes'
    assert journal.session_changes('ordinary') == []


def test_temporary_write_failure_does_not_undo_equal_outside_bytes(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    create_temp = commit.tempfile.mkstemp

    def no_space(*args, **kwargs):
        if kwargs.get('dir') == tmp_path:
            path.write_bytes(b'agent proposed bytes')
            raise OSError(28, 'no temporary space')
        return create_temp(*args, **kwargs)

    monkeypatch.setattr(commit.tempfile, 'mkstemp', no_space)
    with pytest.raises(FileJournalError) as error:
        apply(journal, [PlannedWrite(path, b'agent proposed bytes')])
    assert error.value.code == 'write_busy'
    assert path.read_bytes() == b'agent proposed bytes'
    assert journal.session_changes('ordinary') == []


@pytest.mark.parametrize('encoding,bom', [('utf-8', b'\xef\xbb\xbf'), ('utf-16-le', b'\xff\xfe')])
def test_native_commit_and_undo_preserve_exact_encoded_bytes(journal, tmp_path, encoding, bom):
    path = tmp_path / '普通😀.txt'
    original = bom + '原文\r\n第二行\r\n'.encode(encoding)
    edited = bom + '修改\r\n第二行\r\n'.encode(encoding)
    path.write_bytes(original)
    change = apply(journal, [PlannedWrite(path, edited, hashlib.sha256(original).hexdigest())])
    assert path.read_bytes() == edited
    journal.undo(change.change_id)
    assert path.read_bytes() == original


def test_create_never_replaces_a_name_occupied_during_install(journal, tmp_path, monkeypatch):
    path = tmp_path / 'new.txt'
    install = commit._install

    def outside_create(source, target):
        target.write_bytes(b'created outside')
        install(source, target)

    monkeypatch.setattr(commit, '_install', outside_create)
    with pytest.raises(FileJournalError) as error:
        apply(journal, [PlannedWrite(path, b'agent')])
    assert error.value.code == 'write_conflict'
    assert path.read_bytes() == b'created outside'
    assert journal.session_changes('ordinary') == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows mandatory sharing guard')
def test_update_keeps_a_file_created_in_the_destination_interval(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    install = commit._install

    def outside_create(source, target):
        assert not target.exists()
        target.write_bytes(b'outside replacement')
        install(source, target)

    monkeypatch.setattr(commit, '_install', outside_create)
    with pytest.raises(FileJournalError) as error:
        apply(journal, [PlannedWrite(path, b'agent')])
    assert error.value.code == 'write_conflict'
    assert path.read_bytes() == b'outside replacement'
    assert journal.session_changes('ordinary') == []
    assert not list(tmp_path.glob('*.replaced'))
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows mandatory sharing guard')
def test_version_is_rechecked_after_a_sharing_retry(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    update = commit._windows_update
    calls = []

    def busy_once(source, target, expected):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError('brief sharing conflict')
        update(source, target, expected)

    monkeypatch.setattr(commit, '_windows_update', busy_once)
    monkeypatch.setattr(commit.time, 'sleep', lambda _: path.write_bytes(b'outside during retry'))
    with pytest.raises(FileJournalError) as error:
        apply(journal, [PlannedWrite(path, b'agent')])
    assert error.value.code == 'write_conflict'
    assert len(calls) == 2
    assert path.read_bytes() == b'outside during retry'
    assert journal.session_changes('ordinary') == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows mandatory sharing guard')
def test_in_place_writes_and_replacements_are_excluded_during_guard(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    outside = tmp_path / 'outside.tmp'
    outside.write_bytes(b'outside')
    rename = commit._WindowsFile.rename
    checked = []

    def try_outside_before_rename(lease, target):
        if target.suffix == '.replaced':
            with pytest.raises(PermissionError):
                path.write_bytes(b'outside in-place')
            with pytest.raises(PermissionError):
                os.replace(outside, path)
            checked.append(True)
        rename(lease, target)

    monkeypatch.setattr(commit._WindowsFile, 'rename', try_outside_before_rename)
    change = apply(journal, [PlannedWrite(path, b'agent')])
    assert checked == [True]
    assert path.read_bytes() == b'agent'
    assert Path(change.entries[0].backup_path).read_bytes() == b'original'
    journal.undo(change.change_id)
    assert path.read_bytes() == b'original'


@pytest.mark.skipif(os.name != 'nt', reason='Windows mandatory sharing guard')
def test_open_outside_writer_is_reported_busy_without_changing_bytes(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    monkeypatch.setattr(commit.time, 'sleep', lambda _: None)
    with path.open('r+b') as writer:
        with pytest.raises(FileJournalError) as error:
            apply(journal, [PlannedWrite(path, b'agent')])
        assert error.value.code == 'write_busy'
        writer.seek(0)
        assert writer.read() == b'original'
    assert path.read_bytes() == b'original'
    assert journal.session_changes('ordinary') == []


def test_undo_rechecks_the_file_after_its_preflight(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'agent')])
    reverse = journal._reverse

    def outside_before_reverse(entry):
        path.write_bytes(b'outside after undo check')
        reverse(entry)

    monkeypatch.setattr(journal, '_reverse', outside_before_reverse)
    with pytest.raises(FileJournalError) as error:
        journal.undo(change.change_id)
    assert error.value.code == 'conflict'
    assert path.read_bytes() == b'outside after undo check'
    assert journal.get(change.change_id).undone_at is None


def test_undo_validates_the_bytes_actually_read_from_backup(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'agent')])
    reverse = journal._reverse

    def corrupt_after_preflight(entry):
        Path(entry.backup_path).write_bytes(b'changed backup')
        reverse(entry)

    monkeypatch.setattr(journal, '_reverse', corrupt_after_preflight)
    with pytest.raises(FileJournalError) as error:
        journal.undo(change.change_id)
    assert error.value.code == 'conflict'
    assert path.read_bytes() == b'agent'
    assert journal.get(change.change_id).undone_at is None


def test_pending_update_with_missing_destination_can_restore_backup(journal, tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'agent')])
    with journal.store.connection() as db:
        db.execute("UPDATE file_change SET apply_phase='pending' WHERE change_id=?", (change.change_id,))
    path.unlink()
    restarted = FileChangeJournal(journal.store, tmp_path / 'state')
    pending = restarted.get(change.change_id)
    assert _detail(restarted, pending).undo_available
    restored = restarted.undo(change.change_id)
    assert restored.undone_at is not None
    assert path.read_bytes() == b'original'


def test_missing_pending_update_does_not_replace_a_later_file(journal, tmp_path, monkeypatch):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'agent')])
    with journal.store.connection() as db:
        db.execute("UPDATE file_change SET apply_phase='pending' WHERE change_id=?", (change.change_id,))
    path.unlink()
    install = commit._install

    def create_during_restore(source, target):
        target.write_bytes(b'outside after restart')
        install(source, target)

    monkeypatch.setattr(commit, '_install', create_during_restore)
    with pytest.raises(FileJournalError) as error:
        journal.undo(change.change_id)
    assert error.value.code == 'conflict'
    assert path.read_bytes() == b'outside after restart'
    assert journal.get(change.change_id).undone_at is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows native commit interruption')
def test_process_termination_in_commit_interval_keeps_a_recoverable_backup(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'original document')
    marker = tmp_path / 'commit-entered'
    script = '''
import sys,time
from pathlib import Path
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.file_journal import FileChangeJournal,PlannedWrite
import zhimeng_runtime.file_commit as commit
root=Path(sys.argv[1])
store=SqliteStore(root/'runtime.sqlite3');store.initialize()
journal=FileChangeJournal(store,root/'state')
install=commit._install
def pause(source,target):
    (root/'commit-entered').write_bytes(b'entered')
    time.sleep(60)
    install(source,target)
commit._install=pause
journal.apply(session_id='ordinary',run_id='run-files',tool_call_id='crash',title='Ordinary document',
              operations=[PlannedWrite(root/'notes.txt',b'new document')])
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(tmp_path)], env=environment,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), 'child never entered the controlled commit interval'
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    store = SqliteStore(tmp_path / 'runtime.sqlite3')
    store.initialize()
    restarted = FileChangeJournal(store, tmp_path / 'state')
    change = restarted.session_changes('ordinary')[0]
    assert change.apply_phase == 'pending'
    assert Path(change.entries[0].backup_path).read_bytes() == b'original document'
    assert _detail(restarted, change).undo_available
    assert restarted.undo(change.change_id).undone_at is not None
    assert path.read_bytes() == b'original document'
