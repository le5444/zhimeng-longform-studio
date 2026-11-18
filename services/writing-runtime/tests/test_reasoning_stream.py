from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from zhimeng_runtime.agent_provider import AgentProviderError, ProviderCompleted, ProviderReasoningDelta, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app
from zhimeng_runtime.execution_protocol import ReasoningPayload, RunStatus
from zhimeng_runtime.reasoning_stream import ReasoningStream


class PausedReasoningProvider(ApiProvider):
    def __init__(self, *, fail=False, tool_path=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail
        self.tool_path = tool_path
        self.calls = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            yield ProviderReasoningDelta("Compare the document headings. ")
            for _ in range(500):
                yield ProviderReasoningDelta("x")
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            if self.fail:
                raise AgentProviderError("broken", code="fixture_stream_failed", retryable=False)
            if self.tool_path:
                yield ProviderToolCall(0, "read-document", "read_file", {"path": str(self.tool_path)})
                yield ProviderCompleted("tool_calls", None)
                return
        yield ProviderTextDelta("The document has three sections.")
        yield ProviderCompleted("stop", None)


def start(client, sid):
    response = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": "reasoning-stream-001"}, json={"message": "Summarize the document structure."})
    assert response.status_code == 202, response.text
    return response.json()["run"]["run_id"]


def reasoning(client, rid):
    response = client.get(f"/api/v1/agent/runs/{rid}/activities")
    assert response.status_code == 200, response.text
    return [item for item in response.json()["activities"] if item["kind"] == "reasoning"]


@pytest.mark.parametrize("end", ["complete", "cancel", "fail"])
def test_public_reasoning_is_live_batched_durable_and_separate(settings, end):
    provider = PausedReasoningProvider(fail=end == "fail")
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert provider.entered.wait(10)
        # Still inside the provider request: the first public fragment is persisted.
        early = reasoning(client, rid)
        assert early[0]["payload"]["phase"] == "delta"
        assert early[0]["payload"]["delta"] == "Compare the document headings. "
        assert client.get(f"/api/v1/agent/runs/{rid}").json()["run"]["status"] == "running"
        # A timer flushes a quiet tail even though the provider has stopped yielding.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = reasoning(client, rid)
            if len(current) > 1:
                break
            time.sleep(0.02)
        assert "".join(item["payload"]["delta"] or "" for item in current) == "Compare the document headings. " + "x" * 500
        assert len(current) < 20  # 501 deltas are batched; this is not one write per token.
        if end == "cancel":
            assert client.post(f"/api/v1/agent/runs/{rid}/cancel").status_code == 200
        else:
            provider.release.set()
        terminal = wait_for_success(client, rid)
        assert terminal["status"] == {"complete": "succeeded", "cancel": "cancelled", "fail": "failed"}[end]
        items = reasoning(client, rid)
        assert len({item["payload"]["block_id"] for item in items}) == 1
        assert items[-1]["payload"]["phase"] == ("completed" if end == "complete" else "interrupted")
        assert items[-1]["payload"]["content"] == "Compare the document headings. " + "x" * 500
        assert items[-1]["payload"]["source"] == "model"
        history = client.app.state.agent_service.store.provider_history(sid)
        assert "Compare the document headings" not in json.dumps(history)
        if end == "complete":
            assert history[-1]["content"] == "The document has three sections."
    # A separate app sees the same complete or interrupted public content.
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        assert reasoning(restarted, rid)[-1]["payload"] == items[-1]["payload"]


def test_reasoning_finishes_before_real_tool_execution(settings, tmp_path):
    document = tmp_path / "sections.txt"
    document.write_bytes(b"Overview\nDetails\nNext steps\n")
    provider = PausedReasoningProvider(tool_path=document)
    provider.release.set()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert wait_for_success(client, rid)["status"] == "succeeded"
        items = client.get(f"/api/v1/agent/runs/{rid}/activities").json()["activities"]
        complete = next(item for item in items if item["kind"] == "reasoning" and item["payload"]["phase"] == "completed")
        tool = next(item for item in items if item["kind"] == "tool_call")
        assert complete["sequence"] < tool["sequence"]
        assert len(provider.calls) == 2
        assert any(message.get("role") == "tool" for message in provider.calls[-1])
        assert "Compare the document headings" not in json.dumps(provider.calls[-1])


def test_restart_closes_only_unfinished_public_blocks_without_migration(settings):
    provider = ApiProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.agent_service.store
        sid = session(client)
        run, _, _ = store.create_run(session_id=sid, message="Read a document", expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None, idempotency_key="orphan-reasoning")
        store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
        store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
        store.append_reasoning(run.run_id, summary="Legacy record", content="Older visible content")
        for delta in ("Compare ", "headings."):
            store.append_reasoning(run.run_id, summary="Visible reasoning", content=None, block_id="reasoning_orphan_0001", phase="delta", delta=delta)
        # This run has no task. Recovery models a process exiting after durable deltas.
        assert store.recover_interrupted() == 1
        items = reasoning(client, run.run_id)
        assert items[0]["payload"]["block_id"] is None
        assert items[0]["payload"]["phase"] == "completed"
        assert items[-1]["payload"]["phase"] == "interrupted"
        assert items[-1]["payload"]["content"] == "Compare headings."
        assert store.recover_interrupted() == 0
        assert len(reasoning(client, run.run_id)) == 4


def test_reasoning_payload_preserves_legacy_records_and_validates_phases():
    legacy = ReasoningPayload(summary="Summary", content="Visible text", visibility="full")
    assert legacy.phase == "completed"
    assert legacy.source == "model"
    with pytest.raises(ValidationError):
        ReasoningPayload(summary="Summary", content="Not a delta", visibility="full", phase="delta", delta="x")
    with pytest.raises(ValidationError):
        ReasoningPayload(summary="Summary", content="Final", visibility="full", phase="completed", delta="x")


def test_reasoning_contract_matches_current_payload_model():
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "packages/contracts/execution-protocol.schema.json").read_text(encoding="utf-8"))
    assert schema["$defs"]["reasoningPayload"] == ReasoningPayload.model_json_schema()


def test_cancellation_waits_for_inflight_delta_before_terminal_snapshot():
    class Store:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.saved = []

        def append_reasoning(self, rid, **payload):
            if payload["phase"] == "delta":
                self.entered.set()
                assert self.release.wait(10)
            self.saved.append(payload)

    class Broker:
        async def publish(self, rid):
            pass

    async def scenario():
        store = Store()
        stream = ReasoningStream(store, Broker(), "run_reasoning_race")
        sending = asyncio.create_task(stream.add("Already received public text."))
        assert await asyncio.to_thread(store.entered.wait, 10)
        sending.cancel()
        finishing = asyncio.create_task(stream.finish_preserving_cancellation("interrupted"))
        await asyncio.sleep(0.01)
        assert not finishing.done()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await finishing
        assert [item["phase"] for item in store.saved] == ["delta", "interrupted"]
        assert store.saved[-1]["content"] == "Already received public text."

    asyncio.run(scenario())


def test_visible_summary_is_not_cut_and_request_note_is_runtime_source(settings):
    visible = "Document heading evidence. " * 600

    class SummaryProvider(ApiProvider):
        reasoning_effort = "high"

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            yield ProviderReasoningDelta(visible, visibility="summary")
            yield ProviderTextDelta("Three sections.")
            yield ProviderCompleted("stop", None)

    with TestClient(create_app(settings, agent_provider=SummaryProvider())) as client:
        rid = start(client, session(client))
        assert wait_for_success(client, rid)["status"] == "succeeded"
        items = reasoning(client, rid)
        model = [item["payload"] for item in items if item["payload"]["source"] == "model"]
        runtime = [item["payload"] for item in items if item["payload"]["source"] == "runtime"]
        assert model[-1]["content"] == visible
        assert model[-1]["visibility"] == "summary"
        assert len(runtime) == 1
        assert "参数" in runtime[0]["content"]


@pytest.mark.parametrize("outcome", ["success", "tool_error", "cancel"])
def test_tool_progress_is_a_durable_message_and_replayed_once(settings, tmp_path, monkeypatch, outcome):
    document = tmp_path / "structure.txt"
    document.write_bytes(b"Overview\nDetails\n")
    second = document if outcome != "tool_error" else tmp_path / "absent.txt"

    class ProgressProvider(ApiProvider):
        def __init__(self):
            self.calls = []

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.calls.append([dict(message) for message in messages])
            if len(self.calls) == 1:
                yield ProviderReasoningDelta("Compare the headings first.")
                yield ProviderTextDelta("I will read both documents.")
                yield ProviderToolCall(0, "read-first", "read_file", {"path": str(document)})
                yield ProviderToolCall(1, "read-second", "read_file", {"path": str(second)})
                yield ProviderCompleted("tool_calls", None)
            else:
                yield ProviderTextDelta("The available document has two sections.")
                yield ProviderCompleted("stop", None)

    provider = ProgressProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        entered = threading.Event()
        if outcome == "cancel":
            async def blocked(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            monkeypatch.setattr(client.app.state.agent_service.tool_executor, "execute", blocked)
        sid = session(client)
        rid = start(client, sid)
        if outcome == "cancel":
            assert entered.wait(10)
            messages = client.get(f"/api/v1/agent/sessions/{sid}/messages").json()["messages"]
            assert messages[-1]["content"] == "I will read both documents."
            assert client.post(f"/api/v1/agent/runs/{rid}/cancel").status_code == 200
        terminal = wait_for_success(client, rid)
        assert terminal["status"] == ("cancelled" if outcome == "cancel" else "succeeded")
        store = client.app.state.agent_service.store
        messages = store.list_messages(sid)
        assert [item.content for item in messages if item.role == "assistant"][0] == "I will read both documents."
        history = store.provider_history(sid)
        assert sum(item.get("content") == "I will read both documents." for item in history) == 1
        items = [item.root for item in store.list_activities(rid)]
        progress = next(item for item in items if item.kind.value == "message" and item.payload.finish_reason == "tool_calls")
        assert progress.sequence < next(item.sequence for item in items if item.kind.value == "tool_call")
        if outcome != "cancel":
            assert sum(item.get("role") == "tool" for item in provider.calls[1]) == 2
            assert [item.content for item in messages if item.role == "assistant"] == ["I will read both documents.", "The available document has two sections."]
        if outcome == "tool_error":
            assert any(item.kind.value == "tool_result" and item.payload.status == "failed" for item in items)
