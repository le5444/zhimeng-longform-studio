"""A failed compensating write keeps the original bytes and a usable undo record."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_direct_write_tools import _call
from test_memory_permissions import _harness
from zhimeng_runtime.agent_tools import AgentToolError
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.file_journal import (
    FileChangeJournal, FileJournalError, PlannedDelete, PlannedMkdir, PlannedMove, PlannedWrite,
)
from zhimeng_runtime.file_changes_api import _summary


@pytest.fixture
def journal(tmp_path):
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    return FileChangeJournal(store, tmp_path / "state")


def apply(journal, operations):
    return journal.apply(
        session_id="session-recovery", run_id="run-recovery", tool_call_id="call-recovery",
        title="Update ordinary files", operations=operations,
    )


def test_failed_batch_does_not_overwrite_a_later_external_edit(journal, tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"original")
    second.write_bytes(b"original")
    perform = journal._perform

    def fail_second(entry, operation):
        if operation.path == second:
            first.write_bytes(b"saved in another editor")
            raise PermissionError("second file is busy")
        perform(entry, operation)

    monkeypatch.setattr(journal, "_perform", fail_second)
    with pytest.raises(FileJournalError) as failed:
        apply(journal, [PlannedWrite(first, b"agent"), PlannedWrite(second, b"agent")])
    assert failed.value.code == "rollback_failed"
    assert first.read_bytes() == b"saved in another editor"
    assert second.read_bytes() == b"original"
    change = journal.session_changes("session-recovery")[0]
    assert change.apply_phase == 'failed'
    assert _summary(FileChangeJournal(journal.store, tmp_path / 'state').get(change.change_id)).status == 'failed'
    assert [entry.path for entry in change.entries] == [str(first)]
    assert Path(change.entries[0].backup_path).read_bytes() == b"original"
    with pytest.raises(FileJournalError) as conflict:
        journal.undo(change.change_id)
    assert conflict.value.code == "conflict"
    first.write_bytes(b"agent")
    assert _summary(journal.undo(change.change_id)).status == 'undone'
    assert first.read_bytes() == b"original"


def test_successful_save_keeps_its_phase_after_external_edit(journal, tmp_path):
    path = tmp_path / 'ordinary.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'saved')])
    path.write_bytes(b'external edit after success')
    reopened = FileChangeJournal(journal.store, tmp_path / 'state')
    assert _summary(reopened.get(change.change_id)).status == 'applied'
    with pytest.raises(FileJournalError) as error:
        reopened.undo(change.change_id)
    assert error.value.code == 'conflict'
    assert path.read_bytes() == b'external edit after success'


def test_unconfirmed_save_survives_restart_without_becoming_success(journal, tmp_path):
    path = tmp_path / 'ordinary.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'saved')])
    with journal.store.connection() as connection:
        connection.execute("UPDATE file_change SET apply_phase='pending' WHERE change_id=?", (change.change_id,))
    reopened = FileChangeJournal(journal.store, tmp_path / 'state')
    assert _summary(reopened.get(change.change_id)).status == 'unverified'
    with pytest.raises(FileJournalError) as error:
        apply(reopened, [PlannedWrite(path, b'saved')])
    assert error.value.code == 'rollback_failed'
    assert path.read_bytes() == b'saved'
    assert _summary(reopened.undo(change.change_id)).status == 'undone'
    assert path.read_bytes() == b'original'


def test_v31_migration_preserves_history_without_inventing_save_confirmation(journal, tmp_path):
    path = tmp_path / 'ordinary.txt'
    path.write_bytes(b'original')
    change = apply(journal, [PlannedWrite(path, b'saved')])
    with journal.store.connection() as connection:
        connection.execute('ALTER TABLE file_change DROP COLUMN apply_phase')
        connection.execute('PRAGMA user_version = 31')
    assert journal.store.initialize().schema_version == 33
    migrated = journal.get(change.change_id)
    assert migrated.apply_phase == 'legacy'
    assert _summary(migrated).status == 'unverified'
    assert Path(migrated.entries[0].backup_path).read_bytes() == b'original'
    assert path.read_bytes() == b'saved'
    journal.undo(change.change_id)
    assert path.read_bytes() == b'original'


@pytest.mark.parametrize("operation", ["write", "delete", "move", "mkdir"])
def test_error_after_mutation_restores_the_attempted_operation(journal, tmp_path, monkeypatch, operation):
    first, second, target = tmp_path / "first.txt", tmp_path / "second.txt", tmp_path / "moved.txt"
    first.write_bytes(b"original")
    if operation != "mkdir":
        second.write_bytes(b"original")
    planned = {
        "write": PlannedWrite(second, b"agent"), "delete": PlannedDelete(second),
        "move": PlannedMove(second, target), "mkdir": PlannedMkdir(second),
    }[operation]
    perform = journal._perform

    def fail_after_second(entry, item):
        perform(entry, item)
        if item.path == second:
            raise OSError("operation failed after changing disk")

    monkeypatch.setattr(journal, "_perform", fail_after_second)
    with pytest.raises(OSError, match="after changing disk"):
        apply(journal, [PlannedWrite(first, b"agent"), planned])
    assert first.read_bytes() == b"original"
    assert not target.exists()
    if operation == "mkdir":
        assert not second.exists()
    else:
        assert second.read_bytes() == b"original"
    assert journal.session_changes("session-recovery") == []


def test_failed_move_keeps_partial_target_and_a_record_for_reconciliation(journal, tmp_path, monkeypatch):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    source.write_bytes(b"original complete document")

    def failed_copy(entry, operation):
        target.write_bytes(b"original")
        raise OSError("cross-volume copy ran out of space")

    monkeypatch.setattr(journal, "_perform", failed_copy)
    with pytest.raises(FileJournalError) as failed:
        apply(journal, [PlannedMove(source, target)])
    assert failed.value.code == "rollback_failed"
    change = journal.session_changes("session-recovery")[0]
    assert source.read_bytes() == b"original complete document"
    assert target.read_bytes() == b"original"
    assert change.entries[0].target_path == str(target)
    with pytest.raises(FileJournalError) as conflict:
        journal.undo(change.change_id)
    assert conflict.value.code == "conflict"
    # Once the partial copy is reconciled, undo completes the existing record.
    target.unlink()
    assert journal.undo(change.change_id).undone_at is not None
    assert source.read_bytes() == b"original complete document"


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_file_batch_keeps_only_unreverted_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool,
) -> None:
    async def scenario() -> None:
        service, _provider, _binding, _folder = _harness(tmp_path)
        journal = service.tool_executor.file_journal
        paths = [tmp_path / f"document-{i}.txt" for i in range(3)]
        for path in paths:
            path.write_bytes(b"original")
        perform, reverse = journal._perform, journal._reverse

        def fail_third(entry, operation):
            if entry.path == str(paths[2]):
                raise PermissionError("third file is busy")
            perform(entry, operation)

        def maybe_fail_first_rollback(entry):
            if rollback_fails and entry.path == str(paths[0]):
                raise PermissionError("first file is busy during rollback")
            reverse(entry)

        monkeypatch.setattr(journal, "_perform", fail_third)
        monkeypatch.setattr(journal, "_reverse", maybe_fail_first_rollback)
        try:
            session = await service.create_session(project_id=None, title="File recovery check")
            run, _, _ = service.store.create_run(
                session_id=session.session_id, message="Update these plain text documents.",
                expected_project_revision=None, expected_project_fingerprint=None,
                active_chapter_id=None, writer_mode="author_review_change_proposal",
                idempotency_key="file-recovery-check",
            )
            service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
            run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
            call_id = "call-file-recovery-001"
            change_id = journal.change_id_for(run.run_id, call_id)
            with pytest.raises((AgentToolError, PermissionError)) as error:
                await _call(
                    service.tool_executor, run, "write_file",
                    {"files": [{"path": str(path), "content": "updated"} for path in paths]},
                    call_id=call_id,
                )
            assert paths[1].read_bytes() == paths[2].read_bytes() == b"original"
            record = journal.get(change_id)
            if rollback_fails:
                assert isinstance(error.value, AgentToolError)
                assert error.value.code == "agent_tool_write_rollback_failed"
                assert change_id in str(error.value)
                assert paths[0].read_bytes() == b"updated"
                assert record is not None and len(record.entries) == 1
                assert record.entries[0].path == str(paths[0])
                assert Path(record.entries[0].backup_path).read_bytes() == b"original"
                with pytest.raises(FileJournalError, match="上次写入未完整完成"):
                    journal.apply(
                        session_id=session.session_id, run_id=run.run_id, tool_call_id=call_id,
                        title="retry", operations=[PlannedWrite(path, b"updated") for path in paths],
                    )
                monkeypatch.setattr(journal, "_reverse", reverse)
                restored = journal.undo(change_id)
                assert restored.undone_at is not None
                assert all(path.read_bytes() == b"original" for path in paths)
            else:
                assert paths[0].read_bytes() == b"original"
                assert record is None
                assert not (journal.backup_root / change_id).exists()
        finally:
            await service.shutdown()

    asyncio.run(scenario())
