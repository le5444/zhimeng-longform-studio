"""回顾前几轮修改时照常完成；10-15 已退役按回答措辞生成用户补话的路径。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from test_context_api import _write_project
from test_correction_p0_20260926 import _app
from test_open_folder_chat_20260927 import _bound_turn, _open_folder
from test_said_but_not_done_20260927 import StepProvider
from zhimeng_runtime import environment_context
from zhimeng_runtime.database import SqliteStore

CHAPTER = "05_正文内容/第一卷/CH001_定稿.md"


def test_recalling_earlier_changes_does_not_create_a_new_instruction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = StepProvider(
        [
            ("edit_file", {"path": CHAPTER, "find": "匿名信", "replace": "一封没署名的信"}),
            "改好了。",
            # 第二轮：作者问改过什么，模型照实回顾，嘴上带了「已修改」，这一轮没调工具。
            f"已修改的文件：{CHAPTER}，把「匿名信」改成了「一封没署名的信」。",
            "对，就是第一轮改的那一处。",
        ]
    )
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "改稿"}
        ).json()
        first = _bound_turn(
            client, session["session_id"], snapshot,
            {"message": "第一章那句改狠一点", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "earlier-changes-0001",
        )
        assert first["status"] == "succeeded"
        second = _bound_turn(
            client, session["session_id"], _open_folder(client, project),
            {"message": "这场对话里我们改过哪些文件？", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "earlier-changes-0002",
        )
        assert second["status"] == "succeeded"
    with SqliteStore(tmp_path / "runtime.sqlite3").connection() as connection:
        rows = connection.execute(
            "SELECT run_id, payload_json FROM run_event WHERE event_type = 'model_turn_nudged' ORDER BY created_at",
        ).fetchall()
    nudges = [(row["run_id"], json.loads(row["payload_json"])) for row in rows]
    assert nudges == []
    assert len(provider.requests) == 3
    assert (project / CHAPTER).read_text(encoding="utf-8") == "第一章：林澈收到一封没署名的信。"
