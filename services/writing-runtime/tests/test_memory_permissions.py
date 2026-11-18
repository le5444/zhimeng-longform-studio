"""Memory writes follow the same two access modes as ordinary file writes."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_direct_write_tools import _call
from test_agent_service import wait_terminal
from test_memory_files import _RecordingProvider
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import AgentToolError, AgentToolExecutor
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_files import read_user_memory, write_user_memory
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader


def _harness(tmp_path: Path):
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "README.md").write_bytes(b"A plain document project.\n")
    (folder / "AGENTS.md").write_bytes(b"Use concise explanations.\n")
    reader = ProjectReader((tmp_path,))
    identity = reader.inspect(folder)["project"]
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    binding = database.sync_project_binding(
        project_id=str(identity["project_id"]), root_path=str(folder),
        display_name="Documents", fingerprint=str(identity["fingerprint"]),
    )
    provider = _RecordingProvider()
    executor = AgentToolExecutor(
        reader, database, ProjectLockRegistry(),
        ChangeSetService(reader, database, tmp_path / "change-sets"),
    )
    service = AgentService(AgentStore(database), provider, tool_executor=executor)
    return service, provider, binding, folder


@pytest.mark.parametrize("with_project", [False, True])
@pytest.mark.parametrize("mode", ["repair_only", "author_review_change_proposal"])
def test_memory_tool_catalog_tracks_access_mode(tmp_path: Path, with_project: bool, mode: str) -> None:
    async def scenario() -> None:
        service, provider, binding, _folder = _harness(tmp_path)
        write_user_memory(tmp_path, "Explain the result first.\n")
        try:
            session = await service.create_session(
                project_id=binding.project_id if with_project else None,
                title="Memory access check",
            )
            started = await service.start_run(
                session_id=session.session_id, message="Please explain this document.",
                expected_project_revision=binding.revision if with_project else None,
                expected_project_fingerprint=binding.fingerprint if with_project else None,
                active_chapter_id=None, writer_mode=mode, idempotency_key="memory-catalog-check",
            )
            run = await wait_terminal(service, started.run.run_id)
            assert run.status is RunStatus.SUCCEEDED
            assert ("memory_write" in provider.tool_names) == (mode == "author_review_change_proposal")
            assert "read_file" in provider.tool_names
            assert any("Explain the result first." in str(message.get("content", "")) for message in provider.messages)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("with_project,scope", [(False, "user"), (True, "user"), (True, "project")])
@pytest.mark.parametrize("mode", ["repair_only", "author_review_change_proposal"])
def test_memory_executor_enforces_access_mode(tmp_path: Path, with_project: bool, scope: str, mode: str) -> None:
    async def scenario() -> None:
        service, _provider, binding, folder = _harness(tmp_path)
        write_user_memory(tmp_path, "Existing preference.\n")
        history_before = sorted((tmp_path / "memory-history").glob("*.json"))
        project_before = (folder / "AGENTS.md").read_bytes()
        try:
            session = await service.create_session(
                project_id=binding.project_id if with_project else None,
                title="Memory execution check",
            )
            run, _, _ = service.store.create_run(
                session_id=session.session_id, message="Remember that I prefer concise answers.",
                expected_project_revision=binding.revision if with_project else None,
                expected_project_fingerprint=binding.fingerprint if with_project else None,
                active_chapter_id=None, writer_mode=mode, idempotency_key="memory-execution-check",
            )
            service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
            run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
            call = _call(service.tool_executor, run, "memory_write", {"scope": scope, "text": "Prefer concise answers."})
            if mode == "repair_only":
                with pytest.raises(AgentToolError) as error:
                    await call
                assert error.value.code == "writer_change_set_forbidden"
                assert read_user_memory(tmp_path) == "Existing preference.\n"
                assert (folder / "AGENTS.md").read_bytes() == project_before
                assert sorted((tmp_path / "memory-history").glob("*.json")) == history_before
            else:
                result = await call
                assert result.output["applied"] is True
                content = read_user_memory(tmp_path) if scope == "user" else (folder / "AGENTS.md").read_text(encoding="utf-8")
                assert "Prefer concise answers." in content
        finally:
            await service.shutdown()

    asyncio.run(scenario())
