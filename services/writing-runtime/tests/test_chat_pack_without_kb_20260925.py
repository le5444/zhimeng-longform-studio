# -*- coding: utf-8 -*-
"""普通对话的资料包不再绑知识库快照（2026-09-25）。

以前前端每发一句话都要先查 / 重建这本书的知识库快照，再把快照编号塞进资料包；
后端建包时核对四五次、开工核对一次、每一轮模型请求前再核对一次、收尾再核对一次，
每次核对都把整本书扫一遍（九百多章的书一次十秒）。现在资料包不带快照也能开工，
不带就一次都不核对。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from test_context_agent_e2e import (
    AnswerOnlyProvider,
    DirectWriteProvider,
    StableKnowledgeAdapter,
    _wait_for_terminal,
    _write_project,
)
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderEvent, ProviderTextDelta, ProviderToolDefinition
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_kb_service import ProjectKbService


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


def _send(client: TestClient, project: Path, message: str, *, key: str, writer_mode: str) -> tuple[str, dict[str, Any]]:
    """和前端一样发一句话：建资料包（不带知识库快照）→ 开工。"""
    snapshot = client.post("/api/v1/projects/inspect", json={"root_path": str(project)}).json()
    project_id = snapshot["project"]["project_id"]
    session = client.post(f"/api/v1/projects/{project_id}/agent/sessions", json={"title": "对话"})
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]
    pack_response = client.post(
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
    assert pack_response.status_code == 201, pack_response.text
    pack = pack_response.json()["pack"]
    assert pack["project_kb_state_id"] is None
    assert pack["project_kb_manifest_hash"] is None
    run = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": f"{key}-turn"},
        json={
            "message": message,
            "expected_project_revision": pack["project_revision"],
            "expected_project_fingerprint": pack["project_fingerprint"],
            "active_chapter_id": pack["active_chapter_id"],
            "writer_mode": writer_mode,
            "context_pack_id": pack["context_pack_id"],
            "expected_context_pack_hash": pack["pack_hash"],
        },
    )
    assert run.status_code == 202, run.text
    return run.json()["run"]["run_id"], pack


def _count_kb_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = ProjectKbService.get_state

    def counting(self: ProjectKbService, project_id: str, **kwargs: Any):
        calls.append(project_id)
        return original(self, project_id, **kwargs)

    monkeypatch.setattr(ProjectKbService, "get_state", counting)
    return calls


def test_chat_turn_without_kb_snapshot_writes_and_never_scans_for_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = DirectWriteProvider()
    app, project = _app(tmp_path, provider)
    kb_checks = _count_kb_checks(monkeypatch)
    with TestClient(app) as client:
        run_id, pack = _send(client, project, "帮我在素材库建一个灵感文件。", key="no-kb-write", writer_mode="author_review_change_proposal")
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal
        assert (project / "08_素材库" / "灵感.md").read_text(encoding="utf-8") == "# 灵感\n\n一句题记。\n"
        # 资料包照样带着这本书的内容（系统上下文里有控制卡、设定）。
        assert "Lin refuses to sacrifice a witness." in pack["exact_provider_messages"][0]["content"]
        messages = client.get(f"/api/v1/agent/sessions/{terminal['session_id']}/messages").json()["messages"]
        assert messages[-1]["role"] == "assistant"
    assert kb_checks == [], "不带快照的资料包不该触发任何知识库核对（每次核对都要扫整本书）"


class EditsBookMidRunProvider(AnswerOnlyProvider):
    """回答途中，作者在外面的编辑器里改了一章（真实情况：作者边等边改）。"""

    def __init__(self, chapter: Path) -> None:
        super().__init__(answer="看过了，这一章节奏可以。")
        self.chapter = chapter

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.received_messages = [dict(message) for message in messages]
        self.chapter.write_text("Lin finds a blood-stained key at the old port. 作者刚补的一句。", encoding="utf-8")
        yield ProviderTextDelta(self.answer)
        yield ProviderCompleted("stop", None)


def test_author_editing_the_book_during_a_reply_does_not_fail_the_turn(tmp_path: Path) -> None:
    library_chapter = tmp_path / "library" / "novel" / "05_正文内容" / "第一卷" / "CH001_正文.md"
    provider = EditsBookMidRunProvider(library_chapter)
    app, project = _app(tmp_path, provider)
    with TestClient(app) as client:
        run_id, _pack = _send(client, project, "看看第一章节奏怎么样。", key="no-kb-edit", writer_mode="repair_only")
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal
        messages = client.get(f"/api/v1/agent/sessions/{terminal['session_id']}/messages").json()["messages"]
        assert messages[-1]["content"] == "看过了，这一章节奏可以。"


def test_kb_state_id_without_manifest_hash_is_rejected(tmp_path: Path) -> None:
    app, project = _app(tmp_path, AnswerOnlyProvider())
    with TestClient(app) as client:
        snapshot = client.post("/api/v1/projects/inspect", json={"root_path": str(project)}).json()
        project_id = snapshot["project"]["project_id"]
        session_id = client.post(f"/api/v1/projects/{project_id}/agent/sessions", json={"title": "对话"}).json()["session_id"]
        response = client.post(
            f"/api/v1/agent/sessions/{session_id}/context-packs",
            headers={"Idempotency-Key": "half-kb-context"},
            json={
                "objective": "看看",
                "profile": "chat",
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                "project_kb_state_id": "project-kb-state-" + "0" * 32,
            },
        )
        assert response.status_code == 422, response.text
