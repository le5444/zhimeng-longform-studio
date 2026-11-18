# -*- coding: utf-8 -*-
"""输出打满上限、自动续写过的那一轮之后，同一条绑书对话还能接着聊（2026-09-26）。

截断时已写出的前半段先落成一条回答，模型接着写的后半段另落一条，于是这一轮是「一句提问 + 两条回答」。
以前建资料包要求每轮恰好一问一答：这一轮之后，同一条绑书对话每发一句都 409，重发也一样；
手动「整理记忆」又把每条回答都当一轮，恰好从这一轮中间切开，切完照样坏，只能新开对话。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from test_context_agent_e2e import AnswerOnlyProvider, StableKnowledgeAdapter, _wait_for_terminal, _write_project
from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import _SESSION_TITLE_PROMPT
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings

TRUNCATED = object()


class ScriptedTurnsProvider(AnswerOnlyProvider):
    """按顺序回对话请求；起名、整理记忆的请求单独回，不占对话的顺序。"""

    def __init__(self, turns: list[object]) -> None:
        super().__init__()
        self.turns = list(turns)
        self.conversation_calls: list[list[dict[str, Any]]] = []
        self.summary_calls: list[list[dict[str, Any]]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if any(m.get("role") == "system" and m.get("content") == _SESSION_TITLE_PROMPT for m in messages):
            yield ProviderTextDelta("旧港的故事")
            yield ProviderCompleted("stop", None)
            return
        if str(messages[-1].get("content", "")).startswith("【上一份摘要】"):
            self.summary_calls.append([dict(m) for m in messages])
            yield ProviderTextDelta("摘要：作者在写旧港的故事。")
            yield ProviderCompleted("stop", None)
            return
        self.conversation_calls.append([dict(m) for m in messages])
        step = self.turns.pop(0)
        if isinstance(step, tuple) and step[0] is TRUNCATED:
            yield ProviderTextDelta(step[1])
            raise AgentProviderError(
                "Provider stopped because the output token limit was reached",
                code="agent_provider_output_truncated",
                retryable=True,
            )
        yield ProviderTextDelta(str(step))
        yield ProviderCompleted("stop", None)


def _app(tmp_path: Path, provider: object):
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )
    return app, project


def _new_session(client: TestClient, project: Path) -> str:
    snapshot = client.post("/api/v1/projects/inspect", json={"root_path": str(project)}).json()
    session = client.post(f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "对话"})
    assert session.status_code == 201, session.text
    return session.json()["session_id"]


def _pack(client: TestClient, project: Path, session_id: str, message: str, *, key: str):
    """和前端一样：每句话先带着对话历史建资料包。"""
    snapshot = client.post("/api/v1/projects/inspect", json={"root_path": str(project)}).json()
    return client.post(
        f"/api/v1/agent/sessions/{session_id}/context-packs",
        headers={"Idempotency-Key": f"{key}-context"},
        json={
            "objective": message,
            "profile": "chat",
            "active_chapter_id": "VOL001-CH001",
            "expected_project_revision": snapshot["project"]["revision"],
            "expected_project_fingerprint": snapshot["project"]["fingerprint"],
            "include_session_history": True,
        },
    )


def _say(client: TestClient, project: Path, session_id: str, message: str, *, key: str) -> dict[str, Any]:
    pack_response = _pack(client, project, session_id, message, key=key)
    assert pack_response.status_code == 201, pack_response.text
    pack = pack_response.json()["pack"]
    run = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": f"{key}-turn"},
        json={
            "message": message,
            "expected_project_revision": pack["project_revision"],
            "expected_project_fingerprint": pack["project_fingerprint"],
            "active_chapter_id": pack["active_chapter_id"],
            "writer_mode": "author_review_change_proposal",
            "context_pack_id": pack["context_pack_id"],
            "expected_context_pack_hash": pack["pack_hash"],
        },
    )
    assert run.status_code == 202, run.text
    terminal = _wait_for_terminal(client, run.json()["run"]["run_id"])
    assert terminal["status"] == "succeeded", terminal
    return pack


def _messages(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/v1/agent/sessions/{session_id}/messages").json()["messages"]


def test_bound_chat_keeps_working_after_a_truncated_and_continued_answer(tmp_path: Path) -> None:
    provider = ScriptedTurnsProvider([(TRUNCATED, "故事从旧港开始，"), "然后风向变了，故事继续。", "第二句也答上了。"])
    app, project = _app(tmp_path, provider)
    with TestClient(app) as client:
        session_id = _new_session(client, project)
        _say(client, project, session_id, "写一段旧港的开头。", key="first")
        assert [m["role"] for m in _messages(client, session_id)] == ["user", "assistant", "assistant"]

        # 同一条对话的下一句：以前这里 409「A succeeded Run must have one complete user/assistant message pair」。
        pack = _say(client, project, session_id, "接着写。", key="second")
        history = [m for m in pack["exact_provider_messages"] if m["role"] in ("user", "assistant")]
        assert [m["role"] for m in history] == ["user", "assistant", "user"]
        assert history[1]["content"] == "故事从旧港开始，\n\n然后风向变了，故事继续。"
        assert history[-1]["content"] == "接着写。"
        # 模型这一轮真的收到了接起来的上一轮回答。
        sent = provider.conversation_calls[-1]
        assert any(m["role"] == "assistant" and "然后风向变了" in str(m["content"]) for m in sent)


def test_manual_compaction_does_not_cut_a_continued_answer_in_half(tmp_path: Path) -> None:
    provider = ScriptedTurnsProvider(
        ["第一轮的回答。", (TRUNCATED, "第二轮前半段，"), "第二轮后半段。", "第三句的回答。"]
    )
    app, project = _app(tmp_path, provider)
    with TestClient(app) as client:
        session_id = _new_session(client, project)
        _say(client, project, session_id, "第一句。", key="one")
        _say(client, project, session_id, "第二句。", key="two")
        messages = _messages(client, session_id)
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "assistant"]

        # 「整理记忆」保留最近一整轮：切点应落在第一轮末尾，而不是第二轮两段回答中间。
        compacted = client.post(f"/api/v1/agent/sessions/{session_id}/compact")
        assert compacted.status_code == 200, compacted.text
        assert compacted.json()["through_ordinal"] == messages[1]["ordinal"]
        assert "第一轮的回答" in provider.summary_calls[-1][-1]["content"]
        assert "第二轮前半段" not in provider.summary_calls[-1][-1]["content"]

        pack = _say(client, project, session_id, "第三句。", key="three")
        history = [m for m in pack["exact_provider_messages"] if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in history] == ["第二句。", "第二轮前半段，\n\n第二轮后半段。", "第三句。"]


def test_conversation_split_by_an_older_compaction_can_still_build_a_pack(tmp_path: Path) -> None:
    provider = ScriptedTurnsProvider(["第一轮的回答。", (TRUNCATED, "第二轮前半段，"), "第二轮后半段。", "救回来了。"])
    app, project = _app(tmp_path, provider)
    with TestClient(app) as client:
        session_id = _new_session(client, project)
        _say(client, project, session_id, "第一句。", key="one")
        _say(client, project, session_id, "第二句。", key="two")
        messages = _messages(client, session_id)
        # 旧版整理记忆留下的摘要：切在第二轮两段回答中间（它把每条回答都当一轮）。
        app.state.agent_service.store.append_session_checkpoint(
            session_id,
            run_id=None,
            through_ordinal=messages[3]["ordinal"],
            source_message_count=4,
            content="旧摘要：作者问了两句。",
        )
        pack = _say(client, project, session_id, "第三句。", key="three")
        history = [m for m in pack["exact_provider_messages"] if m["role"] in ("user", "assistant")]
        # 被切开那一轮剩下的续写段接不上前文，跳过；这条对话不再永远建不了资料包。
        assert [m["content"] for m in history] == ["第三句。"]
        assert "旧摘要" in pack["exact_provider_messages"][0]["content"] or any(
            "旧摘要" in str(m["content"]) for m in pack["exact_provider_messages"]
        )
