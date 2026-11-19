from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_correction_p0_20260926 import _app, _turn
from test_said_but_not_done_20260927 import StepProvider
from zhimeng_runtime import environment_context
from zhimeng_runtime.database import SqliteStore


@pytest.mark.parametrize(
    "answer",
    [
        "你想要哪种方式？或者直接告诉我“直接帮我清理”，我马上执行！",
        "如果需要清理，告诉我，我这就处理。",
        "可选方案是合并重复文件，接下来我会等你选择。",
        "我先去看看文件。请稍等，我马上调用工具。",
        '下面是示例代码：\n```python\nprint("已修改")\n```',
        "你可以把提示文字改成：已修改。需要我实际修改文件吗？",
        "日志中的‘已保存’只是一条消息，不能证明文件写入成功。",
        "我刚才说‘已修改’并不准确，文件实际上没动。",
    ],
)
def test_prose_does_not_create_a_continuation_request(answer: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    target = tmp_path / "notes.txt"
    target.write_bytes(b"original notes\n")
    provider = StepProvider([answer, ("delete_file", {"path": str(target)}), "已删除。"])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "解释提示语"}).json()
        run = _turn(client, session["session_id"], "解释这个提示语，不修改文件", "author_review_change_proposal", "intent-prose-001")
    assert run["status"] == "succeeded", run
    assert target.exists(), "Runtime created a follow-up instruction from answer wording"
    assert target.read_bytes() == b"original notes\n"
    assert len(provider.requests) == 1


def test_inspection_offer_does_not_trigger_a_following_delete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    target = tmp_path / "report.txt"
    original = b"keep this report\r\n"
    target.write_bytes(original)
    offer = "目录检查完成。要我清理重复文件吗？直接告诉我“帮我清理”，我马上执行！"
    provider = StepProvider([
        ("read_file", {"path": str(target)}),
        offer,
        ("delete_file", {"path": str(target)}),
        "处理完成。",
    ])

    with TestClient(_app(tmp_path, provider)) as client:
        response = client.post("/api/v1/agent/sessions", json={"title": "检查文件"})
        assert response.status_code == 201, response.text
        session_id = response.json()["session_id"]
        first = _turn(client, session_id, "检查目录中的文件情况", "author_review_change_proposal", "intent-inspect-001")
        assert first["status"] == "succeeded", first
        assert target.exists(), "An inspection answer caused an automatic delete"
        assert target.read_bytes() == original
        assert len(provider.requests) == 2
        assert {"write_file", "delete_file", "computer_run_command"} <= set(provider.offered[0])

        second = _turn(client, session_id, "删除 report.txt", "author_review_change_proposal", "intent-delete-002")
        assert second["status"] == "succeeded", second
        assert not target.exists()

    with SqliteStore(tmp_path / "runtime.sqlite3").connection() as connection:
        nudges = connection.execute(
            "SELECT payload_json FROM run_event WHERE run_id=? AND event_type='model_turn_nudged'",
            (first["run_id"],),
        ).fetchall()
        transcript = connection.execute("SELECT messages_json FROM run_transcript WHERE run_id=?", (first["run_id"],)).fetchone()
    assert nudges == []
    assert transcript is not None
    saved = json.loads(transcript[0])
    assert all(message.get("role") != "user" for message in saved)
    assert saved[-1]["content"] == offer


def test_direct_tool_calls_still_complete_without_an_extra_user_turn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    target = tmp_path / "notes.txt"
    provider = StepProvider([
        ("write_file", {"path": str(target), "content": "task completed\n"}),
        "文件已写入。",
    ])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "写文件"}).json()
        run = _turn(client, session["session_id"], "新建 notes.txt，写入 task completed", "author_review_change_proposal", "intent-write-001")
    assert run["status"] == "succeeded", run
    assert target.read_bytes() == b"task completed\n"
    assert len(provider.requests) == 2


def test_intended_shortening_stops_and_explicit_undo_still_works(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    target = tmp_path / "notes.txt"
    original = "".join(f"old note {index}\n" for index in range(120))
    target.write_bytes(original.encode("utf-8"))
    provider = StepProvider([
        ("write_file", {"path": str(target), "content": "short summary\n"}),
        "已按要求只保留简短摘要。",
        ("undo_change", {}),
        "已撤回。",
    ])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "精简笔记"}).json()
        first = _turn(client, session["session_id"], "把 notes.txt 整份精简为 short summary", "author_review_change_proposal", "intent-shrink-001")
        assert first["status"] == "succeeded", first
        assert target.read_bytes() == b"short summary\n", "Runtime undid an intended shortening"
        assert len(provider.requests) == 2
        changes = client.get(f"/api/v1/agent/sessions/{session['session_id']}/file-changes").json()["changes"]
        assert len(changes) == 1 and changes[0]["status"] == "applied"
        detail = client.get(f"/api/v1/agent/sessions/{session['session_id']}/file-changes/{changes[0]['change_id']}").json()
        assert detail["undo_available"] is True
        assert detail["previews"][0]["before_text"] == original
        assert detail["previews"][0]["after_text"] == "short summary\n"
        second = _turn(client, session["session_id"], "撤回刚才的修改", "author_review_change_proposal", "intent-undo-002")
        assert second["status"] == "succeeded", second
        assert target.read_bytes() == original.encode("utf-8")
    with SqliteStore(tmp_path / "runtime.sqlite3").connection() as connection:
        row = connection.execute("SELECT messages_json FROM run_transcript WHERE run_id=?", (first["run_id"],)).fetchone()
        assert row is not None
        assert all(message.get("role") != "user" for message in json.loads(row[0]))
