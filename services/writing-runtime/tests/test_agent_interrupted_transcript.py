"""Ordinary follow-up turns retain completed work after interruption."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from test_agent_service import ScriptedProvider
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import (
    AgentProviderError, OpenAICompatibleAgentProvider, ProviderCompleted, ProviderTextDelta, ProviderToolCall,
)
from zhimeng_runtime.agent_service import AgentService


def assert_protocol_pairs(history):
    pending = []
    for message in history:
        if message.get("role") == "tool":
            assert pending.pop(0) == message["tool_call_id"]
        else:
            assert not pending
            pending = [call["id"] for call in message.get("tool_calls", [])]
    assert not pending
    _, responses = OpenAICompatibleAgentProvider._build_responses_input(history)
    assert [item["call_id"] for item in responses if item["type"] == "function_call"] == [
        item["call_id"] for item in responses if item["type"] == "function_call_output"
    ]
    provider = OpenAICompatibleAgentProvider(
        base_url="http://127.0.0.1:1/v1", model="offline", api_key=None,
        credential_reference=None, timeout_seconds=1, max_tokens=100, temperature=0,
    )
    _, anthropic = provider._build_anthropic_input(history)
    uses, results = [], []
    for message in anthropic:
        for part in message["content"] if isinstance(message["content"], list) else []:
            if part["type"] == "tool_use":
                uses.append(part["id"])
            elif part["type"] == "tool_result":
                results.append(part["tool_use_id"])
    assert uses == results


@pytest.mark.parametrize("ending", ["cancelled", "failed", "shutdown", "task_cancelled"])
def test_next_turn_receives_completed_tool_and_partial_answer(tmp_path, ending):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        target = tmp_path / "receipt.txt"
        reached = asyncio.Event()

        class Provider(ScriptedProvider):
            def __init__(self):
                super().__init__([])
                self.captured = []

            async def stream_chat(self, messages, *, cancel_event, tools=()):
                self.captured.append(copy.deepcopy(messages))
                if len(self.captured) == 1:
                    yield ProviderTextDelta("Preparing the receipt.")
                    yield ProviderToolCall(0, "write-receipt", "write_file", {
                        "path": str(target), "content": "completed-operation-marker",
                    })
                    yield ProviderCompleted("tool_calls", None)
                elif len(self.captured) == 2:
                    yield ProviderTextDelta("The receipt was saved; remaining work")
                    reached.set()
                    if ending == "failed":
                        raise AgentProviderError("Injected failure", code="offline_fatal", retryable=False)
                    await asyncio.Event().wait()
                else:
                    yield ProviderTextDelta("Follow-up received.")
                    yield ProviderCompleted("stop", None)

        provider = Provider()
        service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
        session = await service.create_session(project_id=None, title="Interrupted history")
        common = dict(session_id=session.session_id, expected_project_revision=None,
                      expected_project_fingerprint=None, active_chapter_id=None,
                      writer_mode="author_review_change_proposal")
        try:
            first = await service.start_run(**common, message="Save the receipt.", idempotency_key="first")
            task = service._tasks[first.run.run_id]
            await asyncio.wait_for(reached.wait(), 15)
            if ending == "cancelled":
                await service.cancel_run(first.run.run_id)
            elif ending == "shutdown":
                await service.shutdown()
            elif ending == "task_cancelled":
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
            assert target.read_bytes() == b"completed-operation-marker"
            assert len(service.store.list_tool_calls(first.run.run_id)) == 1
            if ending == "shutdown":
                service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
            second = await service.start_run(**common, message="What was already done?", idempotency_key="second")
            await asyncio.wait_for(service._tasks[second.run.run_id], 15)
            history = provider.captured[-1]
            assert_protocol_pairs(history)
            results = [m for m in history if m.get("role") == "tool"]
            assert len(results) == 1
            result = json.loads(results[0]["content"])
            assert result["status"] == "succeeded"
            assert result["output"]["path"] == str(target)
            assert [m.get("content") for m in history].count("Preparing the receipt.") == 1
            assert [m.get("content") for m in history].count("The receipt was saved; remaining work") == 1
            assert len(service.tool_executor.file_journal.session_changes(session.session_id)) == 1
        finally:
            await service.shutdown()
            await base.shutdown()

    asyncio.run(scenario())


def test_cancelled_batch_recovers_finished_calls_and_pairs_unstarted_calls(tmp_path, monkeypatch):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        first_path, last_path = tmp_path / "first.txt", tmp_path / "last.txt"
        reached = asyncio.Event()
        execute = base.tool_executor.execute

        async def pause_read(run, **kwargs):
            if kwargs["tool_name"] == "read_file":
                reached.set()
                await asyncio.Event().wait()
            return await execute(run, **kwargs)

        monkeypatch.setattr(base.tool_executor, "execute", pause_read)

        class Provider(ScriptedProvider):
            def __init__(self):
                super().__init__([])
                self.captured = []

            async def stream_chat(self, messages, *, cancel_event, tools=()):
                self.captured.append(copy.deepcopy(messages))
                if len(self.captured) == 1:
                    yield ProviderToolCall(0, "done", "write_file", {"path": str(first_path), "content": "saved"})
                    yield ProviderToolCall(1, "stopped", "read_file", {"path": str(first_path)})
                    yield ProviderToolCall(2, "pending", "write_file", {"path": str(last_path), "content": "pending"})
                    yield ProviderCompleted("tool_calls", None)
                else:
                    yield ProviderTextDelta("Reviewed the previous tool records.")
                    yield ProviderCompleted("stop", None)

        provider = Provider()
        service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
        session = await service.create_session(project_id=None, title="Batch interruption")
        common = dict(session_id=session.session_id, expected_project_revision=None,
                      expected_project_fingerprint=None, active_chapter_id=None,
                      writer_mode="author_review_change_proposal")
        try:
            first = await service.start_run(**common, message="Process the files.", idempotency_key="batch")
            task = service._tasks[first.run.run_id]
            await asyncio.wait_for(reached.wait(), 15)
            await service.cancel_run(first.run.run_id)
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
            next_run = await service.start_run(**common, message="Review what finished.", idempotency_key="followup")
            await asyncio.wait_for(service._tasks[next_run.run.run_id], 15)
            history = provider.captured[-1]
            assert_protocol_pairs(history)
            results = {part["tool_call_id"]: json.loads(part["content"]) for part in history if part.get("role") == "tool"}
            assert results["done"]["status"] == "succeeded"
            assert results["stopped"]["status"] == "cancelled"
            assert results["pending"]["status"] == "not_started"
            assert first_path.read_bytes() == b"saved"
            assert not last_path.exists()
            assert len(service.store.list_tool_calls(first.run.run_id)) == 2
            assert len(service.tool_executor.file_journal.session_changes(session.session_id)) == 1
        finally:
            await service.shutdown()
            await base.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["succeeded", "failed", "cancelled"])
def test_completed_runs_release_their_file_read_ledger(tmp_path, ending):
    async def scenario():
        base, _, binding, folder = _harness(tmp_path)
        reached = asyncio.Event()

        class Reader(ScriptedProvider):
            calls = 0

            async def stream_chat(self, messages, *, cancel_event, tools=()):
                self.calls += 1
                if self.calls == 1:
                    yield ProviderToolCall(0, "read", "read_file", {"path": str(folder / "README.md")})
                    yield ProviderCompleted("tool_calls", None)
                    return
                reached.set()
                if ending == "failed":
                    raise AgentProviderError("Injected failure", code="offline_fatal", retryable=False)
                if ending == "cancelled":
                    await asyncio.Event().wait()
                yield ProviderTextDelta("Read complete.")
                yield ProviderCompleted("stop", None)

        service = AgentService(base.store, Reader([]), tool_executor=base.tool_executor)
        try:
            session = await service.create_session(project_id=binding.project_id, title="Read lifecycle")
            result = await service.start_run(
                session_id=session.session_id, message="Read the document.",
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, idempotency_key="read-ledger",
            )
            run_id = result.run.run_id
            task = service._tasks[run_id]
            await asyncio.wait_for(reached.wait(), 15)
            if ending == "cancelled":
                await service.cancel_run(run_id)
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            assert service.store.get_run(run_id).status.value == ending
        finally:
            await service.shutdown()
            await base.shutdown()

    asyncio.run(scenario())
