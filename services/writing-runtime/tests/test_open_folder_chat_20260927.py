"""纠偏指令 3.9（2026-09-27）：打开文件夹的对话不再每轮先编资料包。

前端直接发这一轮，只带开着哪一章、亲手附上了哪些文件；后端在这一轮末尾告诉模型「开着哪个文件」（不带内容，
要用自己读），亲手附上的文件照 Claude Code 的 @文件 把内容带上。开头是环境事实 + 用户记忆 + 文件夹里的说明文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from test_context_api import _write_project
from test_correction_p0_20260926 import ScriptedProvider, _app, _turn
from zhimeng_runtime import environment_context


def _open_folder(client: TestClient, root: Path) -> dict[str, Any]:
    response = client.post("/api/v1/projects/inspect", json={"root_path": str(root)})
    assert response.status_code == 200, response.text
    return response.json()


def _bound_turn(client: TestClient, session_id: str, snapshot: dict[str, Any], body: dict[str, Any], key: str) -> dict[str, Any]:
    started = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={
            "expected_project_revision": snapshot["project"]["revision"],
            "expected_project_fingerprint": snapshot["project"]["fingerprint"],
            **body,
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run"]["run_id"]
    for _ in range(1500):
        run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return run
        __import__("time").sleep(0.01)
    raise AssertionError("run did not finish")


def test_open_folder_turn_points_at_the_open_chapter_and_carries_attached_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None, None])
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    (project / "AGENTS.md").write_text("## 记住的事\n\n- 主角叫林澈\n", encoding="utf-8")
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        chapter = next(item for item in snapshot["chapters"] if item["prose_file_ids"])
        files = {item["file_id"]: item for item in snapshot["files"]}
        chapter_path = files[chapter["prose_file_ids"][0]]["relative_path"]
        style = next(item for item in snapshot["files"] if item["relative_path"].endswith("风格.md"))
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions",
            json={"title": "打开文件夹"},
        )
        assert created.status_code == 201, created.text
        session = created.json()
        first = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {
                "message": "这一段收紧一点",
                "active_chapter_id": chapter["chapter_id"],
                "manual_file_ids": [style["file_id"]],
                "writer_mode": "repair_only",
            },
            "open-folder-0001",
        )
        second = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "好的", "active_chapter_id": None, "writer_mode": "repair_only"},
            "open-folder-0002",
        )
        artifacts = client.get(f"/api/v1/agent/runs/{first['run_id']}").json()["run"]
    assert first["status"] == "succeeded", first
    assert second["status"] == "succeeded", second
    assert artifacts.get("context_pack_id") in (None, "")

    request = provider.requests[0]
    assert request[0]["role"] == "system" and str(request[0]["content"]).startswith("系统：")
    assert f"当前文件夹：{project}" in str(request[0]["content"])
    assert request[1] == {
        "role": "user",
        "content": "这个文件夹里的说明文件：\n\n── AGENTS.md ──\n## 记住的事\n\n- 主角叫林澈",
    }
    pointer = request[-1]
    assert pointer["role"] == "system"
    assert f"用户这时在编辑器里开着：{chapter_path}" in pointer["content"]
    assert f"用户附上的文件 {style['relative_path']}：\n对话克制，事实先于抒情。" in pointer["content"]
    # 章节正文本身不自动塞进来：要用模型自己读。
    chapter_text = (project / chapter_path).read_text(encoding="utf-8")
    assert all(chapter_text not in str(message.get("content")) for message in request)
    # 以前那段「这是和作者的普通对话……」没有了。
    assert all("普通对话" not in str(message.get("content")) for message in request)
    # 9-28 起上一轮发给模型的那一段原样带进下一轮的历史（ZCode 档案 12.2）：指路消息跟在第一句话后面，不再每轮新附一遍；
    # 第二轮什么都没开，末尾说一声没开着。
    later = provider.requests[1]
    first_ask = next(index for index, message in enumerate(later) if message.get("content") == "这一段收紧一点")
    assert later[first_ask + 1] == pointer
    assert sum("用户附上的文件" in str(message.get("content")) for message in later) == 1
    assert later[-1] == {"role": "system", "content": "用户这时没开着文件。"}
    # 只读挡：写工具、执行命令、提案都不给；老的受管 skill_execute 也不给。
    offered = set(provider.catalogs[0])
    assert {"write_file", "edit_file", "computer_run_command", "change_set_propose", "skill_execute"}.isdisjoint(offered)
    assert {"read_file", "list_files"} <= offered
