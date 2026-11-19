from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from test_agent_service import TurnScriptedProvider
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStoreConflict
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.session_goal import GoalAdmission, GoalEndRequest, GoalStore, GoalUpdateArguments


def _request(session, key="goal-start", **extra):
    return dict(session_id=session.session_id, message="Create a report and verify the saved result.",
                expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
                writer_mode="author_review_change_proposal", idempotency_key=key, **extra)


def _running(base, session, **extra):
    run, _, _ = base.store.create_run(**_request(session, **extra))
    base.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
    return base.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")


def _answer(text):
    return [ProviderTextDelta(text), ProviderCompleted("stop", None)]


@pytest.mark.parametrize("outcome", ["completed", "blocked"])
def test_goal_continues_partial_answers_and_commits_outcome(tmp_path, outcome):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        report = tmp_path / "report.md"
        provider = TurnScriptedProvider([
            _answer("I will inspect the request first."),
            [ProviderToolCall(0, "save", "write_file", {"path": str(report), "content": "Checked total: 210"}), ProviderCompleted("tool_calls", None)],
            _answer("The report is saved; verification remains."),
            [ProviderToolCall(0, "read", "read_file", {"path": str(report)}), ProviderCompleted("tool_calls", None)],
            [ProviderToolCall(0, "finish", "goal_update", {"status": outcome, "progress": "Report saved and read back.", "evidence": "Read-back: Checked total: 210"}), ProviderCompleted("tool_calls", None)],
            _answer("Report checked."),
        ])
        service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
        try:
            session = await service.create_session(project_id=None, title="Continuous report")
            started = await service.start_run(**_request(session, goal=GoalAdmission(action="start")))
            await asyncio.wait_for(service._tasks[started.run.run_id], 30)
            assert service.store.get_run(started.run.run_id).status == RunStatus.SUCCEEDED
            goal = GoalStore(service.store.database).read(session.session_id).goal
            assert goal.status == outcome
            assert goal.stage_count == 3
            assert goal.status_source == "agent" and goal.requested_status is None
            assert report.read_text() == "Checked total: 210"
            messages = service.store.list_messages(session.session_id)
            assert [m.content for m in messages if m.role == "user"] == [_request(session)["message"]]
            assert len([m for m in messages if m.role == "assistant"]) == 3
            assert len(provider.messages_by_turn) == 6
            with service.store.database.connection() as connection:
                transcript = json.loads(connection.execute("SELECT messages_json FROM run_transcript WHERE run_id=?", (started.run.run_id,)).fetchone()[0])
                assert sum("运行时持续目标状态" in str(m.get("content")) for m in transcript) >= 2
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_pause_fences_late_outcome_and_resume_is_idempotent(tmp_path):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        session = await base.create_session(project_id=None, title="Goal races")
        run = _running(base, session, goal=GoalAdmission(action="start"))
        goals = GoalStore(base.store.database)
        goals.update(session.session_id, run.run_id, GoalUpdateArguments(status="completed", progress="Prepared", evidence="Read-back checked"))
        assert goals.read(session.session_id).goal.status == "active"
        base.store.cancel_run(run.run_id)
        paused = goals.read(session.session_id).goal
        assert paused.status == "paused" and paused.requested_status is None
        with pytest.raises(AgentStoreConflict):
            goals.update(session.session_id, run.run_id, GoalUpdateArguments(status="active", progress="late"))
        resume = GoalAdmission(action="resume", goal_id=paused.goal_id, expected_revision=paused.revision)
        with pytest.raises(AgentStoreConflict):
            base.store.create_run(**_request(session, "resume", goal=resume))
        base.store.mark_cancelled(run.run_id)
        new, _, replayed = base.store.create_run(**_request(session, "resume", goal=resume))
        assert not replayed
        again, _, replayed = base.store.create_run(**_request(session, "resume", goal=resume))
        assert replayed and again.run_id == new.run_id
        current = goals.read(session.session_id).goal
        assert current.objective == paused.objective and current.run_id == new.run_id
        assert current.goal_id == paused.goal_id and current.status == "active"
        with pytest.raises(AgentStoreConflict):
            goals.update(session.session_id, run.run_id, GoalUpdateArguments(status="active", progress="stale owner"))
        assert base.store.recover_interrupted() == 1
        assert goals.read(session.session_id).goal.status == "paused"
    asyncio.run(scenario())


def test_goal_outcome_and_answer_rollback_together(tmp_path, monkeypatch):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        session = await base.create_session(project_id=None, title="Atomic result")
        run = _running(base, session, goal=GoalAdmission(action="start"))
        goals = GoalStore(base.store.database)
        goals.update(session.session_id, run.run_id, GoalUpdateArguments(status="completed", progress="done", evidence="checked"))
        def fail(*args):
            raise OSError("injected transcript commit failure")
        monkeypatch.setattr(base.store, "_save_run_transcript_in_connection", fail)
        with pytest.raises(OSError):
            base.store.complete_run(run.run_id, message_id="message-answer", content="Done", finish_reason="stop", usage=None, transcript=[{"role": "assistant", "content": "Done"}])
        assert goals.read(session.session_id).goal.status == "active"
        assert base.store.get_run(run.run_id).status == RunStatus.RUNNING
        assert not [m for m in base.store.list_messages(session.session_id) if m.role == "assistant"]
    asyncio.run(scenario())


def test_goal_isolation_explicit_admission_and_end(tmp_path):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        session = await base.create_session(project_id=None, title="First")
        other = await base.create_session(project_id=None, title="Second")
        run = _running(base, session, goal=GoalAdmission(action="start"))
        goals = GoalStore(base.store.database)
        assert goals.read(other.session_id).goal is None
        with pytest.raises(AgentStoreConflict):
            goals.update(other.session_id, run.run_id, GoalUpdateArguments(status="active", progress="cross-session"))
        base.store.mark_interrupted(run.run_id, detail="hard exit")
        paused = goals.read(session.session_id).goal
        with pytest.raises(AgentStoreConflict):
            base.store.create_run(**_request(session, "replace", goal=GoalAdmission(action="start")))
        with pytest.raises(AgentStoreConflict):
            goals.end(session.session_id, GoalEndRequest(goal_id=paused.goal_id, expected_revision=1))
        assert goals.end(session.session_id, GoalEndRequest(goal_id=paused.goal_id, expected_revision=paused.revision)).goal.status == "ended"
        new, _, _ = base.store.create_run(**_request(session, "new-goal", goal=GoalAdmission(action="start")))
        assert goals.read(session.session_id).goal.run_id == new.run_id
        assert goals.read(session.session_id).goal.goal_id != paused.goal_id
    asyncio.run(scenario())


def test_ordinary_chat_still_finishes_without_a_goal(tmp_path):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        service = AgentService(base.store, TurnScriptedProvider([_answer("Hello")]), tool_executor=base.tool_executor, auto_memory=False)
        try:
            session = await service.create_session(project_id=None, title="Chat")
            started = await service.start_run(**_request(session))
            await service._tasks[started.run.run_id]
            assert service.store.get_run(started.run.run_id).status == RunStatus.SUCCEEDED
            assert GoalStore(service.store.database).read(session.session_id).goal is None
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_ordinary_followup_does_not_inherit_completed_goal_runtime_instruction(tmp_path):
    async def scenario():
        from zhimeng_runtime.session_goal import GOAL_CONTEXT_PREFIX
        base, _, _, _ = _harness(tmp_path)
        provider = TurnScriptedProvider([
            _answer("Stage saved."),
            [ProviderToolCall(0, "finish", "goal_update", {"status": "completed", "progress": "Answer checked", "evidence": "Checked the requested explanation"}), ProviderCompleted("tool_calls", None)],
            _answer("Finished."),
            _answer("Ordinary followup."),
        ])
        service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
        try:
            session = await service.create_session(project_id=None, title="Goal then ordinary chat")
            first = await service.start_run(**_request(session, goal=GoalAdmission(action="start")))
            await service._tasks[first.run.run_id]
            second = await service.start_run(**_request(session, "ordinary-followup"))
            await service._tasks[second.run.run_id]
            assert service.store.get_run(second.run.run_id).status == RunStatus.SUCCEEDED
            assert len(provider.messages_by_turn) == 4
            assert not any(str(m.get("content", "")).startswith(GOAL_CONTEXT_PREFIX) for m in provider.messages_by_turn[-1])
            assert GoalStore(service.store.database).read(session.session_id).goal.status == "completed"
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_existing_continue_task_action_resumes_its_paused_goal(tmp_path):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        session = await base.create_session(project_id=None, title="Continue goal")
        run = _running(base, session, goal=GoalAdmission(action="start"))
        base.store.cancel_run(run.run_id)
        base.store.mark_cancelled(run.run_id)
        goal = GoalStore(base.store.database).read(session.session_id).goal
        continued, _, _ = base.store.create_run(**_request(session, "continue-task", continuation_source_run_id=run.run_id))
        current = GoalStore(base.store.database).read(session.session_id).goal
        assert current.goal_id == goal.goal_id and current.run_id == continued.run_id
        assert current.status == "active" and current.objective == goal.objective
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "blocked"])
def test_terminal_goal_claim_requires_evidence(status):
    with pytest.raises(ValidationError):
        GoalUpdateArguments(status=status, progress="claim", evidence=" ")
