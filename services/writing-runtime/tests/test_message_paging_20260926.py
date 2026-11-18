# -*- coding: utf-8 -*-
"""长对话打开时取最新的那些消息，再按需往前补（2026-09-26）。

以前界面一次取「从最早数起的 500 条」：对话超过 500 条以后，最新的问答反而不在界面上，
每轮结束一刷新，刚得到的回答就从屏幕上消失。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_truncated_turn_history_20260926 import ScriptedTurnsProvider, _app, _new_session, _say


def _page(client: TestClient, session_id: str, query: str) -> dict:
    response = client.get(f"/api/v1/agent/sessions/{session_id}/messages?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_latest_page_returns_the_newest_messages_and_says_there_are_earlier_ones(tmp_path: Path) -> None:
    provider = ScriptedTurnsProvider(["一", "二", "三"])
    app, project = _app(tmp_path, provider)
    with TestClient(app) as client:
        session_id = _new_session(client, project)
        for index, text in enumerate(["第一句", "第二句", "第三句"]):
            _say(client, project, session_id, text, key=f"turn-{index}")

        everything = _page(client, session_id, "limit=500")
        assert [m["content"] for m in everything["messages"]] == ["第一句", "一", "第二句", "二", "第三句", "三"]
        assert everything["has_earlier"] is False

        # 旧的默认读法不变：从最早数起。
        oldest = _page(client, session_id, "limit=3")
        assert [m["content"] for m in oldest["messages"]] == ["第一句", "一", "第二句"]

        latest = _page(client, session_id, "limit=3&latest=true")
        assert [m["content"] for m in latest["messages"]] == ["二", "第三句", "三"]
        assert latest["has_earlier"] is True

        earlier = _page(client, session_id, f"limit=3&latest=true&before_ordinal={latest['messages'][0]['ordinal']}")
        assert [m["content"] for m in earlier["messages"]] == ["第一句", "一", "第二句"]
        assert earlier["has_earlier"] is False

        whole = _page(client, session_id, "limit=500&latest=true")
        assert [m["content"] for m in whole["messages"]] == [m["content"] for m in everything["messages"]]
        assert whole["has_earlier"] is False
