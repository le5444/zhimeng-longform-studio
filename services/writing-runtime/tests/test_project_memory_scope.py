from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_service import TurnScriptedProvider, wait_terminal
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import AgentToolExecutor
from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_files import append_user_memory, read_user_memory, set_memory_enabled
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader


class ScopeProvider(TurnScriptedProvider):
    def __init__(self, scope: str | None):
        turns = [[ProviderTextDelta("已核对项目说明。"), ProviderCompleted("stop", None)]]
        if scope:
            turns.insert(0, [ProviderToolCall(0, "scope-memory-call", "memory_write", {
                "scope": scope, "text": "这个项目的数据表使用 UTF-8 编码。",
            }), ProviderCompleted("tool_calls", None)])
        super().__init__(turns)
        self.definitions = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        if tools:
            self.definitions = list(tools)
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def build_project(tmp_path: Path, provider: ScopeProvider):
    project = tmp_path / "spreadsheet-project"
    project.mkdir()
    (project / "AGENTS.md").write_bytes("项目约定：表格列名保留原始名称。\n".encode("utf-8"))
    (project / "data.csv").write_bytes(b"name,count\nalpha,1\n")
    reader = ProjectReader((tmp_path,))
    identity = reader.inspect(project)["project"]
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    binding = database.sync_project_binding(
        project_id=identity["project_id"], root_path=str(project),
        display_name="Spreadsheet project", fingerprint=identity["fingerprint"],
    )
    changes = ChangeSetService(reader, database, tmp_path / "changes")
    executor = AgentToolExecutor(reader, database, ProjectLockRegistry(), changes)
    service = AgentService(AgentStore(database), provider, tool_executor=executor)
    append_user_memory(tmp_path, "用户偏好：回答先给结论。")
    set_memory_enabled(tmp_path, False)
    return service, binding, changes, project


@pytest.mark.parametrize("writer_mode", ["author_review_change_proposal", "repair_only"])
def test_user_memory_switch_keeps_project_instructions_and_respects_file_mode(tmp_path: Path, writer_mode: str):
    async def scenario():
        provider = ScopeProvider(None)
        service, binding, _changes, project = build_project(tmp_path, provider)
        original = (project / "AGENTS.md").read_bytes()
        try:
            session = await service.create_session(project_id=binding.project_id, title="项目说明检查")
            accepted = await service.start_run(
                session_id=session.session_id, message="检查项目说明里的表格约定。",
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode=writer_mode, idempotency_key="project-scope-inspect",
            )
            run = await wait_terminal(service, accepted.run.run_id)
            assert run.status is RunStatus.SUCCEEDED
            wire = str(provider.messages_by_turn[0])
            assert "表格列名保留原始名称" in wire
            assert "用户偏好：回答先给结论" not in wire
            memory = next((item for item in provider.definitions if item.name == "memory_write"), None)
            if writer_mode == "repair_only":
                assert memory is None
            else:
                assert memory is not None
                assert "User memory is currently turned off" in memory.description
                assert "project memory remains available" in memory.description
            assert (project / "AGENTS.md").read_bytes() == original
            assert read_user_memory(tmp_path) == "用户偏好：回答先给结论。\n"
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_project_memory_still_writes_and_rolls_back_with_user_memory_off(tmp_path: Path):
    async def scenario():
        provider = ScopeProvider("project")
        service, binding, changes, project = build_project(tmp_path, provider)
        original = (project / "AGENTS.md").read_bytes()
        try:
            session = await service.create_session(project_id=binding.project_id, title="记住表格约定")
            accepted = await service.start_run(
                session_id=session.session_id, message="记住这个项目的数据表使用 UTF-8 编码。",
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="project-memory-write",
            )
            run = await wait_terminal(service, accepted.run.run_id)
            assert run.status is RunStatus.SUCCEEDED
            assert "这个项目的数据表使用 UTF-8 编码。" in (project / "AGENTS.md").read_text(encoding="utf-8")
            assert read_user_memory(tmp_path) == "用户偏好：回答先给结论。\n"
            created = [item for item in changes.list_for_project(binding.project_id).change_sets if item.source_run_id == run.run_id]
            assert len(created) == 1
            record = changes.get(created[0].change_set_id)
            assert record.status.value == "applied"
            changes.rollback(record.change_set_id, ChangeSetRollbackRequest(expected_review_revision=record.review_revision))
            assert (project / "AGENTS.md").read_bytes() == original
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_stale_user_memory_tool_call_stays_disabled_in_project(tmp_path: Path):
    async def scenario():
        provider = ScopeProvider("user")
        service, binding, _changes, project = build_project(tmp_path, provider)
        original = (project / "AGENTS.md").read_bytes()
        try:
            session = await service.create_session(project_id=binding.project_id, title="关闭用户记忆后检查")
            accepted = await service.start_run(
                session_id=session.session_id, message="检查现有的项目约定。",
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode="author_review_change_proposal", idempotency_key="user-memory-off-project",
            )
            run = await wait_terminal(service, accepted.run.run_id)
            assert run.status is RunStatus.SUCCEEDED
            assert "agent_tool_memory_disabled" in str(provider.messages_by_turn[-1])
            assert read_user_memory(tmp_path) == "用户偏好：回答先给结论。\n"
            assert (project / "AGENTS.md").read_bytes() == original
        finally:
            await service.shutdown()
    asyncio.run(scenario())
