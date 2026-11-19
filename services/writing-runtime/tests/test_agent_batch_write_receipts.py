"""A batch receipt must describe every destination, including after cancellation."""
from __future__ import annotations

import asyncio

import pytest

from test_memory_permissions import _harness
from test_agent_direct_write_tools import _call
from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
from zhimeng_runtime.execution_protocol import RunStatus


async def _start(service, binding):
    session = await service.create_session(project_id=binding.project_id, title="Batch files")
    run, _, _ = service.store.create_run(
        session_id=session.session_id, message="Save the report and its external copy.",
        expected_project_revision=binding.revision,
        expected_project_fingerprint=binding.fingerprint,
        active_chapter_id=None, writer_mode="author_review_change_proposal",
        idempotency_key="batch-receipt",
    )
    service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
    return service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")


@pytest.mark.parametrize("inside_count", [1, 2])
def test_second_destination_failure_preserves_receipt_and_undo(tmp_path, inside_count):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        outside = tmp_path / "external.txt"
        outside.mkdir()
        files = [{"path": f"report-{index}.txt", "content": "saved\n"} for index in range(inside_count)]
        files.append({"path": str(outside), "content": "copy\n"})
        try:
            run = await _start(service, binding)
            result = await service._execute_tool_call(
                run, provider_tool_call_id="batch", tool_name="write_file",
                arguments={"files": files}, cancel_event=asyncio.Event(),
            )
            receipt = result.tool_call
            for index in range(inside_count):
                assert (folder / f"report-{index}.txt").read_bytes() == b"saved\n"
            assert outside.is_dir()
            assert receipt.status == "failed"
            assert receipt.output["incomplete"] is True
            assert receipt.output["applied"] is True
            assert len(receipt.output["files"]) == inside_count
            assert receipt.output["outside_open_folder"]["files"][0]["path"] == str(outside)
            assert receipt.error is not None
            service.store.mark_cancelled(run.run_id)
            persisted = service.store.get_tool_call(receipt.tool_call_id)
            assert persisted.status == "failed"
            assert persisted.output == receipt.output
            # Undo uses the same versioned service as the public change endpoint.
            record = service.tool_executor.change_sets.get(receipt.output["change_set_id"])
            service.tool_executor.change_sets.rollback(record.change_set_id, ChangeSetRollbackRequest(
                expected_review_revision=record.review_revision,
            ))
            assert all(not (folder / f"report-{index}.txt").exists() for index in range(inside_count))
            assert outside.is_dir()
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("absolute_with_parent", [False, True])
@pytest.mark.parametrize("undo_external", [False, True])
def test_recovery_after_both_commits_keeps_both_undo_references(tmp_path, absolute_with_parent, undo_external):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        outside = tmp_path / "external.txt"
        try:
            run = await _start(service, binding)
            target = str(folder / "unused" / ".." / "report.txt") if absolute_with_parent else "report.txt"
            external_target = str(tmp_path / "unused" / ".." / "external.txt") if absolute_with_parent else str(outside)
            result = await _call(service.tool_executor, run, "write_file", {"files": [
                {"path": target, "content": "saved\n"},
                {"path": external_target, "content": "copy\n"},
            ]})
            if undo_external:
                service.tool_executor.file_journal.undo(
                    result.output["outside_open_folder"]["change_id"],
                )
            service.store.mark_cancelled(run.run_id)
            receipt = service.store.list_tool_calls(run.run_id)[0]
            assert receipt.status == "succeeded"
            assert receipt.output["incomplete"] is False
            assert receipt.output["outside_open_folder"]["change_id"] == result.output["outside_open_folder"]["change_id"]
            assert receipt.output["change_set_id"] == result.output["change_set_id"]
            assert (folder / "report.txt").read_bytes() == b"saved\n"
            if undo_external:
                assert receipt.output["outside_open_folder"]["status"] == "rolled_back"
                assert receipt.output["outside_open_folder"]["applied"] is False
                assert not outside.exists()
            else:
                assert outside.read_bytes() == b"copy\n"
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_failed_external_write_with_unchanged_project_does_not_claim_a_save(tmp_path):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        outside = tmp_path / "external.txt"
        outside.mkdir()
        original = (folder / "README.md").read_bytes()
        try:
            run = await _start(service, binding)
            result = await service._execute_tool_call(
                run, provider_tool_call_id="batch", tool_name="write_file",
                arguments={"files": [
                    {"path": "README.md", "content": original.decode()},
                    {"path": str(outside), "content": "copy\n"},
                ]}, cancel_event=asyncio.Event(),
            )
            assert result.tool_call.status == "failed"
            assert result.tool_call.output["incomplete"] is True
            assert result.tool_call.output["applied"] is False
            assert "change_set_id" not in result.tool_call.output
            assert (folder / "README.md").read_bytes() == original
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_recovery_preserves_partial_external_undo(tmp_path, monkeypatch):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        remaining, undone = tmp_path / "remaining.txt", tmp_path / "undone.txt"
        try:
            run = await _start(service, binding)
            result = await _call(service.tool_executor, run, "write_file", {"files": [
                {"path": "report.txt", "content": "saved\n"},
                {"path": str(remaining), "content": "first\n"},
                {"path": str(undone), "content": "second\n"},
            ]})
            journal = service.tool_executor.file_journal
            reverse = journal._reverse
            def fail_remaining(entry):
                if entry.path == str(remaining):
                    raise OSError("file is busy")
                return reverse(entry)
            monkeypatch.setattr(journal, "_reverse", fail_remaining)
            from zhimeng_runtime.file_journal import FileJournalError
            with pytest.raises(FileJournalError, match="1/2"):
                journal.undo(result.output["outside_open_folder"]["change_id"])
            service.store.mark_cancelled(run.run_id)
            receipt = service.store.list_tool_calls(run.run_id)[0]
            assert receipt.status == "succeeded"
            external = receipt.output["outside_open_folder"]["files"]
            assert "status" not in external[0]
            assert external[1]["status"] == "rolled_back"
            assert remaining.read_bytes() == b"first\n"
            assert not undone.exists()
            assert (folder / "report.txt").read_bytes() == b"saved\n"
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("inside_count", [1, 2])
def test_cancel_between_destinations_recovers_only_confirmed_files(tmp_path, monkeypatch, inside_count):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        outside = tmp_path / "external.txt"
        files = [{"path": f"report-{index}.txt", "content": "saved\n"} for index in range(inside_count)]
        files.append({"path": str(outside), "content": "copy\n"})
        try:
            run = await _start(service, binding)
            async def cancel_second(*args, **kwargs):
                raise asyncio.CancelledError
            monkeypatch.setattr(service.tool_executor, "_journal_write_many", cancel_second)
            with pytest.raises(asyncio.CancelledError):
                await service._execute_tool_call(
                    run, provider_tool_call_id="batch", tool_name="write_file",
                    arguments={"files": files}, cancel_event=asyncio.Event(),
                )
            service.store.mark_cancelled(run.run_id)
            receipt = service.store.list_tool_calls(run.run_id)[0]
            assert receipt.status == "cancelled"
            assert receipt.output["incomplete"] is True
            assert len(receipt.output["files"]) == inside_count
            assert receipt.output["unconfirmed_paths"] == [str(outside)]
            assert not outside.exists()
            assert all((folder / f"report-{index}.txt").read_bytes() == b"saved\n" for index in range(inside_count))
        finally:
            await service.shutdown()
    asyncio.run(scenario())
