from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from test_agent_service import wait_terminal
from test_memory_files import _RecordingProvider
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_tools import AgentToolError
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_files import read_user_memory, set_memory_enabled, write_user_memory
from zhimeng_runtime.session_recall import SessionReadArguments, SessionRecall, SessionSearchArguments


@pytest.fixture
def history(tmp_path):
    service, provider, binding, _folder = _harness(tmp_path)
    store = service.store

    def session(project_id=None, title="Ordinary notes"):
        return store.create_session(project_id=project_id, title=title, executor=provider.identity, capabilities=provider.capabilities)

    def begin(target, message):
        run, user, _ = store.create_run(
            session_id=target.session_id, message=message,
            expected_project_revision=target.project_revision.revision if target.project_revision else None,
            expected_project_fingerprint=target.project_revision.fingerprint if target.project_revision else None,
            active_chapter_id=None, idempotency_key=uuid4().hex,
        )
        store.transition_run(run.run_id, RunStatus.STARTING, detail=None)
        run = store.transition_run(run.run_id, RunStatus.RUNNING, detail=None)
        return run, user

    def turn(target, message, response="Noted."):
        run, user = begin(target, message)
        store.complete_run(run.run_id, message_id=f"message-{uuid4().hex}", content=response, finish_reason="stop", usage=None)
        return run, user

    current = session(binding.project_id, "Current work")
    run, _user = begin(current, "Find an earlier decision.")
    recall = SessionRecall(store.database)
    return service, binding, session, turn, current, run, recall


def search(history, **kwargs):
    *_, current, run, recall = history
    return recall.search(current.session_id, run.run_id, SessionSearchArguments(**kwargs), threading.Event())


def read(history, **kwargs):
    *_, current, run, recall = history
    return recall.read(current.session_id, run.run_id, SessionReadArguments(**kwargs), threading.Event())


def test_chinese_recall_keeps_project_speaker_and_original_source(history):
    service, binding, session, turn, *_ = history
    past = session(binding.project_id, "采购决定")
    _run, user = turn(past, "采购预算定为 730 元，先买显示器支架。", "可以考虑 900 元。")
    outside = session(None, "Other work")
    turn(outside, "采购预算定为 1200 元。")
    result = search(history, query="采购 730")
    assert len(result["matches"]) == 1
    hit = result["matches"][0]
    assert hit["message_id"] == user.message_id
    assert hit["session_id"] == past.session_id
    assert hit["project_id"] == binding.project_id
    assert hit["role"] == "user"
    assert hit["content"] == user.content
    assert hit["created_at"] and hit["run_status"] == "succeeded"
    assert len(search(history, query="采购", scope="all")["matches"]) == 2
    assert search(history, query="900", role="user")["matches"] == []
    assert search(history, query="900", role="assistant")["matches"][0]["role"] == "assistant"
    assert read_user_memory(Path(service.store.database.path).parent) == ""


def test_free_chat_scope_and_explicit_session_selection(history):
    service, binding, session, turn, current, run, recall = history
    unbound = session()
    turn(unbound, "关于采购的自由对话。")
    other = session(binding.project_id)
    turn(other, "关于采购的项目对话。")
    result = recall.search(unbound.session_id, run.run_id, SessionSearchArguments(query="采购"), threading.Event())
    assert [row["session_id"] for row in result["matches"]] == [unbound.session_id]
    selected = search(history, query="采购", session_id=unbound.session_id)
    assert selected["scope"] == "session"
    assert selected["matches"][0]["project_id"] is None


def test_literal_sql_characters_unicode_and_stable_search_pagination(history):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    for index in range(5):
        turn(target, f"Straße 采购 %_ ' OR 1=1 -- {index}")
    with service.store.database.connection() as connection:
        connection.execute("UPDATE session_message SET created_at = '2026-10-17T10:00:00+00:00' WHERE session_id = ?", (target.session_id,))
    ids = []
    cursor = None
    while True:
        result = search(history, query="STRASSE %_", limit=2, before_message_id=cursor)
        ids.extend(row["message_id"] for row in result["matches"])
        if not result["has_more"]:
            break
        cursor = result["next_before_message_id"]
    assert len(ids) == len(set(ids)) == 5
    assert len(search(history, query="' OR 1=1 --")["matches"]) == 5
    assert search(history, query="no such term")["matches"] == []


def test_long_message_can_be_read_completely_after_search_without_leaking_split_secret(history):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    secret = "sk-" + "A" * 32
    source = "前文" * 6100 + "\n采购决定：" + secret + "\n" + "尾文" * 7200
    _run, user = turn(target, source)
    hit = search(history, query="采购决定")["matches"][0]
    assert "采购决定" in hit["content"]
    assert secret not in hit["content"]
    chunks = []
    offset = 0
    while True:
        result = read(history, session_id=target.session_id, message_id=user.message_id, start_offset=offset)
        message = result["messages"][0]
        chunks.append(message["content"])
        assert message["masked_secrets"] == 1
        if message["next_start_offset"] is None:
            break
        offset = message["next_start_offset"]
    from zhimeng_runtime.secret_mask import mask_secrets
    assert "".join(chunks) == mask_secrets(source)[0]
    assert secret not in "".join(chunks)
    assert service.store.list_messages(target.session_id)[0].content == source


def test_message_pages_keep_order_and_expand_each_excerpt(history):
    _service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    for index in range(3):
        turn(target, f"request {index}", f"answer {index}")
    page = read(history, session_id=target.session_id, limit=2)
    assert [row["ordinal"] for row in page["messages"]] == [1, 2]
    assert page["has_more"] is True
    second = read(history, session_id=target.session_id, after_ordinal=page["next_after_ordinal"], limit=5)
    assert [row["ordinal"] for row in second["messages"]] == [3, 4, 5, 6]
    assert second["has_more"] is False


def test_current_run_is_not_its_own_source_and_missing_messages_do_not_fall_back(history):
    _service, binding, session, turn, current, run, recall = history
    assert search(history, session_id="current")["matches"] == []
    assert read(history, session_id="current")["messages"] == []
    other = session(binding.project_id)
    _past, user = turn(other, "Source from another session")
    with pytest.raises(ValueError, match="not found"):
        read(history, session_id=current.session_id, message_id=user.message_id)
    with pytest.raises(ValueError, match="no longer exists"):
        search(history, session_id="missing-session")
    with pytest.raises(ValueError, match="cursor no longer exists"):
        search(history, before_message_id="missing-message")
    with pytest.raises(ValueError, match="beyond"):
        read(history, session_id=other.session_id, message_id=user.message_id, start_offset=10000)


@pytest.mark.parametrize("delete_mode", ["close", "remove"])
def test_compressed_history_stays_searchable_but_deleted_sources_do_not_return(history, delete_mode):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    _run, user = turn(target, "保留可核对的旧采购决定。")
    service.store.append_session_checkpoint(target.session_id, run_id=None, through_ordinal=2,
        source_message_count=2, content="A compressed summary without the original words.")
    assert search(history, query="旧采购决定")["matches"][0]["message_id"] == user.message_id
    if delete_mode == "close":
        service.store.close_session(target.session_id)
    else:
        with service.store.database.connection() as connection:
            connection.execute("DELETE FROM execution_session WHERE id = ?", (target.session_id,))
    assert search(history, query="旧采购决定")["matches"] == []
    with pytest.raises(ValueError):
        read(history, session_id=target.session_id, message_id=user.message_id)


def test_rebinding_a_conversation_does_not_relabel_old_messages(history):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    _run, old = turn(target, "采购决定属于原项目。")
    moved = service.store.rebind_session(target.session_id, project_id=None)
    _run, new = turn(moved, "采购决定属于新的自由对话。")
    scoped = search(history, query="采购决定")
    assert [hit["message_id"] for hit in scoped["matches"]] == [old.message_id]
    all_hits = search(history, query="采购决定", session_id=target.session_id)["matches"]
    assert [(hit["message_id"], hit["project_id"]) for hit in all_hits] == [(new.message_id, None), (old.message_id, binding.project_id)]


@pytest.mark.parametrize("with_project", [False, True])
@pytest.mark.parametrize("mode", ["repair_only", "author_review_change_proposal"])
@pytest.mark.parametrize("memory_on", [False, True])
def test_history_tools_are_available_independently_of_memory_writes(tmp_path, with_project, mode, memory_on):
    async def scenario():
        service, provider, binding, _folder = _harness(tmp_path)
        write_user_memory(tmp_path, "Existing stable preference.\n")
        set_memory_enabled(tmp_path, memory_on)
        try:
            session = await service.create_session(project_id=binding.project_id if with_project else None, title="Recall check")
            started = await service.start_run(session_id=session.session_id, message="What did we discuss before?",
                expected_project_revision=binding.revision if with_project else None,
                expected_project_fingerprint=binding.fingerprint if with_project else None,
                active_chapter_id=None, writer_mode=mode, idempotency_key="recall-catalog")
            assert (await wait_terminal(service, started.run.run_id)).status is RunStatus.SUCCEEDED
            assert {"session_search", "session_read"} <= set(provider.tool_names)
            assert read_user_memory(tmp_path) == "Existing stable preference.\n"
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_read_failure_is_structured_and_does_not_create_a_missing_database(history, monkeypatch):
    service, _binding, _session, _turn, _current, run, _recall = history
    missing = Path(service.store.database.path).with_name("missing.sqlite3")
    monkeypatch.setattr(service.tool_executor.store, "path", missing)
    with pytest.raises(AgentToolError) as error:
        asyncio.run(service.tool_executor.execute(run, tool_call_id="call-recall", tool_name="session_search",
            arguments={"query": "anything"}, request_hash=service.tool_executor.request_hash("session_search", "1", {"query": "anything"}),
            cancel_event=asyncio.Event()))
    assert error.value.code == "agent_history_unavailable"
    assert not missing.exists()


def test_model_roundtrip_search_read_and_answer_uses_actual_source(history):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id, "采购决定")
    _run, source = turn(target, "采购预算最终定为 730 元。", "之前建议的 900 元不采用。")

    class LookupProvider(_RecordingProvider):
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            results = [json.loads(item["content"]).get("output", {}) for item in messages if item.get("role") == "tool"]
            search_result = next((item for item in results if item.get("schema") == "zhimeng.tool-result.session-search.v1"), None)
            read_result = next((item for item in results if item.get("schema") == "zhimeng.tool-result.session-read.v1"), None)
            if search_result is None:
                yield ProviderToolCall(index=0, tool_call_id="search-source", tool_name="session_search", arguments={"query": "采购预算", "role": "user"})
                yield ProviderCompleted("tool_calls", None)
            elif read_result is None:
                hit = search_result["matches"][0]
                assert hit["message_id"] == source.message_id
                yield ProviderToolCall(index=0, tool_call_id="read-source", tool_name="session_read",
                    arguments={"session_id": hit["session_id"], "message_id": hit["message_id"]})
                yield ProviderCompleted("tool_calls", None)
            else:
                hit = read_result["messages"][0]
                assert hit["role"] == "user" and hit["content"] == source.content
                yield ProviderTextDelta(f"{hit['session_title']}：{hit['content']}")
                yield ProviderCompleted("stop", None)

    async def scenario():
        service.provider = LookupProvider()
        try:
            target = await service.create_session(project_id=binding.project_id, title="Find the decision")
            started = await service.start_run(session_id=target.session_id, message="查一下之前采购预算最后怎么定的。",
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode="repair_only", idempotency_key="recall-source-roundtrip")
            assert (await wait_terminal(service, started.run.run_id)).status is RunStatus.SUCCEEDED
            saved = service.store.list_messages(target.session_id)
            assert saved[-1].content == "采购决定：采购预算最终定为 730 元。"
            assert read_user_memory(Path(service.store.database.path).parent) == ""
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_cancelled_query_stops_sqlite_work(history):
    _service, _binding, _session, _turn, current, run, recall = history
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        with recall._snapshot(cancelled) as connection:
            connection.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()


def test_search_does_not_process_unindexed_stream_events(history, monkeypatch):
    service, binding, session, turn, *_ = history
    target = session(binding.project_id)
    run, _user = turn(target, "A final purchasing decision.")
    with service.store.database.connection() as connection:
        row = connection.execute("SELECT * FROM activity WHERE run_id = ? AND kind = 'message' LIMIT 1", (run.run_id,)).fetchone()
        count = connection.execute("SELECT count(*) FROM session_message").fetchone()[0]
        connection.execute("BEGIN")
        for index in range(300):
            values = dict(row)
            values.update(id=f"stream-{index}", sequence=1000 + index)
            connection.execute(f"INSERT INTO activity({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        connection.commit()
    evaluations = 0
    original_connect = sqlite3.connect

    class MeasuredConnection(sqlite3.Connection):
        def create_function(self, name, narg, func, **kwargs):
            def measured(value):
                nonlocal evaluations
                evaluations += 1
                return func(value)
            return super().create_function(name, narg, measured if name == "recall_fold" else func, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, **kwargs, factory=MeasuredConnection))
    assert search(history, query="absent-search-term", scope="all")["matches"] == []
    assert 0 < evaluations <= count


@pytest.mark.parametrize("name,model", [
    ("session-search-arguments.schema.json", SessionSearchArguments),
    ("session-read-arguments.schema.json", SessionReadArguments),
])
def test_recall_tool_contract_matches_export(name, model):
    root = Path(__file__).resolve().parents[3]
    exported = json.loads((root / "packages/contracts" / name).read_text(encoding="utf-8"))
    exported.pop("$schema")
    exported.pop("$id")
    assert exported == model.model_json_schema(by_alias=True)
