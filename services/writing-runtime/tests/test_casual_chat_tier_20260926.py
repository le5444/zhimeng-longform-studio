# -*- coding: utf-8 -*-
"""不绑书的对话里挡位也作数（2026-09-26）。

以前前端在不绑书时写死「只读」发、却把挡位按钮藏起来；后台不绑书时不记挡位，执行命令前的检查查不到记录就放行——
请求里写着只读，命令照样真跑；斜杠「切换只读」还会提示「已切到只读」。现在每一轮都记挡位：
只读只读资料、回答，不给执行命令、不写笔记（和绑书时的只读一样）；完全访问照旧全给。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from test_context_agent_e2e import AnswerOnlyProvider
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import _SESSION_TITLE_PROMPT
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


class TriesACommandProvider(AnswerOnlyProvider):
    """第一轮不管给没给命令工具都硬调一次（弱模型真会这样），之后收尾。"""

    def __init__(self, workdir: Path) -> None:
        super().__init__()
        self.workdir = workdir
        self.catalogs: list[list[str]] = []
        self.requests: list[list[dict[str, Any]]] = []
        self.turns = 0

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if any(m.get("role") == "system" and m.get("content") == _SESSION_TITLE_PROMPT for m in messages):
            yield ProviderTextDelta("随便聊")
            yield ProviderCompleted("stop", None)
            return
        self.catalogs.append(sorted(tool.name for tool in tools))
        self.requests.append([dict(m) for m in messages])
        self.turns += 1
        if self.turns % 2 == 1:
            yield ProviderToolCall(
                index=0,
                tool_call_id=f"call_cmd_{self.turns:04d}",
                tool_name="computer_run_command",
                arguments={"command": "echo ran> marker.txt", "workdir": str(self.workdir)},
            )
            yield ProviderCompleted("tool_calls", None)
            return
        yield ProviderTextDelta("好了。")
        yield ProviderCompleted("stop", None)


def _app(tmp_path: Path, provider: object):
    library = tmp_path / "library"
    library.mkdir()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        agent_provider=provider,
    )
    return app, library


def _casual_turn(client: TestClient, body: dict[str, Any], key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"})
    assert session.status_code == 201, session.text
    started = client.post(
        f"/api/v1/agent/sessions/{session.json()['session_id']}/runs",
        headers={"Idempotency-Key": key},
        json={"active_chapter_id": None, **body},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run"]["run_id"]
    for _ in range(1000):
        run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            break
        time.sleep(0.01)
    activities = client.get(f"/api/v1/agent/runs/{run_id}/activities?after_sequence=0").json()["activities"]
    return run, activities


def test_read_only_casual_chat_gets_no_command_or_notes_tool_and_a_forced_command_does_not_run(tmp_path: Path) -> None:
    provider = TriesACommandProvider(tmp_path / "library")
    app, library = _app(tmp_path, provider)
    with TestClient(app) as client:
        run, activities = _casual_turn(
            client, {"message": "看看这台电脑上有什么", "writer_mode": "repair_only"}, "casual-readonly-0001"
        )
    assert run["status"] == "succeeded", run
    catalog = provider.catalogs[0]
    assert "computer_run_command" not in catalog
    assert "memory_write" not in catalog
    assert {"list_files", "read_file", "web_fetch", "search_web", "skill_read_file"} <= set(catalog)
    # 模型硬调了命令：没给的工具不执行，下一次请求里回给它「本回合不可用」；电脑上什么都没发生。
    assert not [a for a in activities if a["kind"] == "tool_result" and a["payload"].get("status") == "succeeded"]
    fed_back = "\n".join(str(m.get("content", "")) for m in provider.requests[1])
    assert "这个工具在本回合不可用：computer_run_command" in fed_back
    assert not (library / "marker.txt").exists()
    # 这一轮记下了挡位。
    intents = [a["payload"] for a in activities if a["kind"] == "artifact" and a["payload"].get("artifact_type") == "writer_intent"]
    assert len(intents) == 1


def test_full_access_casual_chat_still_runs_commands(tmp_path: Path) -> None:
    provider = TriesACommandProvider(tmp_path / "library")
    app, library = _app(tmp_path, provider)
    with TestClient(app) as client:
        run, activities = _casual_turn(
            client,
            {"message": "在书库里建个标记文件", "writer_mode": "author_review_change_proposal"},
            "casual-full-0001",
        )
    assert run["status"] == "succeeded", run
    catalog = provider.catalogs[0]
    assert "computer_run_command" in catalog
    assert "memory_write" in catalog
    results = [a["payload"] for a in activities if a["kind"] == "tool_result"]
    assert results and results[0]["status"] == "succeeded", json.dumps(results[0], ensure_ascii=False)[:400]
    assert (library / "marker.txt").read_text(encoding="utf-8", errors="replace").strip() == "ran"


def test_casual_chat_without_a_tier_is_read_only(tmp_path: Path) -> None:
    """接口不带挡位时按只读（界面总是带着作者选的挡位发，默认完全访问）。"""
    provider = TriesACommandProvider(tmp_path / "library")
    app, library = _app(tmp_path, provider)
    with TestClient(app) as client:
        run, _activities = _casual_turn(client, {"message": "随便看看"}, "casual-default-0001")
    assert run["status"] == "succeeded", run
    assert "computer_run_command" not in provider.catalogs[0]
    assert not (library / "marker.txt").exists()
