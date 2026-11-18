"""A write prepared before an undo must not restore the undone bytes."""
from __future__ import annotations

import asyncio

import pytest

from test_agent_direct_write_tools import _call
from test_memory_permissions import _harness
from zhimeng_runtime.agent_tools import AgentToolError
from zhimeng_runtime.execution_protocol import RunStatus


@pytest.mark.parametrize("operation", ["edit", "write", "many"])
def test_undo_between_read_and_commit_keeps_restored_file(tmp_path, monkeypatch, operation):
    async def scenario():
        service, _, _, _ = _harness(tmp_path)
        executor = service.tool_executor
        session = await service.create_session(project_id=None, title="File race")
        run, _, _ = service.store.create_run(
            session_id=session.session_id, message="Update a configuration file.",
            expected_project_revision=None, expected_project_fingerprint=None,
            active_chapter_id=None, writer_mode="author_review_change_proposal",
            idempotency_key="write-race",
        )
        service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
        run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
        target, other = tmp_path / "config.txt", tmp_path / "other.txt"
        original = b"base=0\nvalue=0\n"
        target.write_bytes(original)
        other.write_bytes(b"other=0\n")
        try:
            first = await _call(executor, run, "write_file", {
                "path": str(target), "content": "base=1\nvalue=0\n",
            }, call_id="initial-write")
            apply = executor._journal_apply

            async def undo_before_commit(*args, **kwargs):
                await asyncio.to_thread(executor.file_journal.undo, first.output["change_id"])
                assert target.read_bytes() == original
                return await apply(*args, **kwargs)

            monkeypatch.setattr(executor, "_journal_apply", undo_before_commit)
            tool, arguments = {
                "edit": ("edit_file", {"path": str(target), "find": "value=0", "replace": "value=2"}),
                "write": ("write_file", {"path": str(target), "content": "base=1\nvalue=2\n"}),
                "many": ("write_file", {"files": [
                    {"path": str(other), "content": "other=2\n"},
                    {"path": str(target), "content": "base=1\nvalue=2\n"},
                ]}),
            }[operation]
            with pytest.raises(AgentToolError) as conflict:
                await _call(executor, run, tool, arguments, call_id="stale-write")
            assert conflict.value.code == "agent_tool_write_conflict"
            assert target.read_bytes() == original
            assert other.read_bytes() == b"other=0\n"
            assert executor.file_journal.get(first.output["change_id"]).undone_at is not None
            assert len(executor.file_journal.session_changes(session.session_id)) == 1
            monkeypatch.setattr(executor, "_journal_apply", apply)
            fresh = await _call(executor, run, "edit_file", {
                "path": str(target), "find": "value=0", "replace": "value=2",
            }, call_id="fresh-write")
            assert target.read_bytes() == b"base=0\nvalue=2\n"
            await asyncio.to_thread(executor.file_journal.undo, fresh.output["change_id"])
            assert target.read_bytes() == original
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_file_created_after_missing_snapshot_is_preserved(tmp_path, monkeypatch):
    async def scenario():
        service, _, _, _ = _harness(tmp_path)
        executor = service.tool_executor
        session = await service.create_session(project_id=None, title="New file race")
        run, _, _ = service.store.create_run(
            session_id=session.session_id, message="Create a file.",
            expected_project_revision=None, expected_project_fingerprint=None,
            active_chapter_id=None, writer_mode="author_review_change_proposal",
            idempotency_key="create-race",
        )
        service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
        run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
        target = tmp_path / "new.txt"
        apply = executor._journal_apply

        async def create_before_commit(*args, **kwargs):
            target.write_bytes(b"created by another session")
            return await apply(*args, **kwargs)

        monkeypatch.setattr(executor, "_journal_apply", create_before_commit)
        try:
            with pytest.raises(AgentToolError) as conflict:
                await _call(executor, run, "write_file", {"path": str(target), "content": "new"})
            assert conflict.value.code == "agent_tool_write_conflict"
            assert target.read_bytes() == b"created by another session"
            assert executor.file_journal.session_changes(session.session_id) == []
        finally:
            await service.shutdown()

    asyncio.run(scenario())
