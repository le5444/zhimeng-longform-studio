"""9-27 真机改稿题（隔离实例、grok-4.6、四道题）抓到的两处：

1. 「先看修改」（change_set_propose）在打开文件夹的对话里一调就失败：9-27 起这种对话不编资料包，
   提案却还要挂资料包（「agent change proposal requires a bound Context Pack」）。四道题三道先调它，
   一道连调 6 次都失败、最后跟作者说「修改已保存到原文件」。作者设成「先看修改」也等于没用。
2. 只读了 457 行里的前 69 行就用 write_file 整份覆盖，一章从 3,480 字变成 692 字，工具只回「已经写好」。
   不拦（写前门禁 9-24 按作者的话拔了），把前后的字数、行数照实说出来。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_direct_write_tools import _build_runtime, _call
from test_context_api import _write_project
from test_correction_p0_20260926 import ScriptedProvider, _app
from test_open_folder_chat_20260927 import _bound_turn, _open_folder
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_tools import _overwrite_note

CHAPTER = "05_正文内容/第一卷/CH001_正文.md"


def test_a_proposal_in_an_open_folder_chat_waits_in_the_change_list_instead_of_failing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None])
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    chapter_file = project / "05_正文内容" / "第一卷" / "CH001_定稿.md"
    before = chapter_file.read_bytes()
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        file_id = next(item["file_id"] for item in snapshot["files"] if item["relative_path"].endswith("CH001_定稿.md"))
        provider.script = [
            (
                "change_set_propose",
                {
                    "title": "第一章开头收紧",
                    "summary": "匿名信那句改短。",
                    "changes": [
                        {"operation": "update", "file_id": file_id, "after_content": "第一章：林澈收到一封信。", "rationale": "收紧"}
                    ],
                },
            )
        ]
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "先看修改"}
        ).json()
        run = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "把第一章开头改短一点", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "pack-free-proposal-0001",
        )
        assert run["status"] == "succeeded", run
        activities = client.get(f"/api/v1/agent/runs/{run['run_id']}/activities?after_sequence=0").json()["activities"]
        results = [a["payload"] for a in activities if a["kind"] == "tool_result"]
        assert [r["status"] for r in results] == ["succeeded"], results
        assert results[0]["output"]["requires_author_review"] is True
        assert results[0]["output"]["applied"] is False
        proposals = [a["payload"] for a in activities if a["kind"] == "change_proposal"]
        assert len(proposals) == 1
        listed = client.get(f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets").json()
        waiting = [item for item in listed["change_sets"] if item["change_set_id"] == proposals[0]["proposal_id"]]
        assert waiting and waiting[0]["status"] == "reviewing"
        # 作者点头之前文件一个字节都不动。
        assert chapter_file.read_bytes() == before
        # 点保存（前端「直接保存」时自动点的就是这个）：存进书里。
        saved = client.post(
            f"/api/v1/change-sets/{waiting[0]['change_set_id']}/apply-direct",
            json={"expected_review_revision": waiting[0]["review_revision"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "applied"
    assert chapter_file.read_text(encoding="utf-8") == "第一章：林澈收到一封信。"


def test_a_proposal_that_drops_most_of_a_chapter_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None])
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    long_chapter = project / "05_正文内容" / "第一卷" / "CH004_定稿.md"
    long_chapter.write_text("".join(f"第四章第 {n} 段。\n\n" for n in range(1, 121)), encoding="utf-8")
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        file_id = next(item["file_id"] for item in snapshot["files"] if item["relative_path"].endswith("CH004_定稿.md"))
        provider.script = [
            (
                "change_set_propose",
                {
                    "title": "合并开头两段",
                    "summary": "合并。",
                    "changes": [
                        {"operation": "update", "file_id": file_id, "after_content": "第四章第 1 段。第四章第 2 段。\n", "rationale": "合并"}
                    ],
                },
            )
        ]
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "先看修改"}
        ).json()
        run = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "把第四章开头两段合成一段", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "pack-free-proposal-0002",
        )
        activities = client.get(f"/api/v1/agent/runs/{run['run_id']}/activities?after_sequence=0").json()["activities"]
    output = next(a["payload"]["output"] for a in activities if a["kind"] == "tool_result")
    assert output["notes"] and "这是整份覆盖" in output["notes"][0] and "CH004_定稿.md" in output["notes"][0]


def test_a_proposal_identical_to_the_file_is_explained_in_plain_words(tmp_path: Path, monkeypatch) -> None:
    """9-27 第四遍：grok-4.6 两次交了和原文一字不差的「新内容」，读不懂「update does not change file content」就一直重读到超时。"""
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None])
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        file_id = next(item["file_id"] for item in snapshot["files"] if item["relative_path"].endswith("CH001_定稿.md"))
        provider.script = [
            (
                "change_set_propose",
                {
                    "title": "并段",
                    "summary": "并段。",
                    "changes": [
                        {"operation": "update", "file_id": file_id, "after_content": "第一章：林澈收到匿名信。", "rationale": "并段"}
                    ],
                },
            )
        ]
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "先看修改"}
        ).json()
        run = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "把开头两段合成一段", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "pack-free-proposal-0003",
        )
        activities = client.get(f"/api/v1/agent/runs/{run['run_id']}/activities?after_sequence=0").json()["activities"]
    failed = next(a["payload"] for a in activities if a["kind"] == "tool_result")
    assert failed["status"] == "failed"
    message = failed["error"]["error"]["message"]
    assert "一模一样" in message and "CH001_定稿.md" in message and "edit_file" in message
    assert "does not change" not in message


def _propose(tmp_path: Path, monkeypatch, changes, *, prepare=None):
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None])
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    if prepare is not None:
        prepare(project)
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        provider.script = [("change_set_propose", {"title": "只改一段", "summary": "只改一段。", "changes": changes})]
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "先看修改"}
        ).json()
        run = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "改一句", "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
            "pack-free-proposal-edit",
        )
        activities = client.get(f"/api/v1/agent/runs/{run['run_id']}/activities?after_sequence=0").json()["activities"]
        result = next(a["payload"] for a in activities if a["kind"] == "tool_result")
        record = None
        if result["status"] == "succeeded":
            record = client.get(f"/api/v1/change-sets/{result['output']['change_set_id']}").json()
            saved = client.post(
                f"/api/v1/change-sets/{record['change_set_id']}/apply-direct",
                json={"expected_review_revision": record["review_revision"]},
            )
            assert saved.status_code == 200, saved.text
    return result, project


def test_a_proposal_can_change_just_one_passage_with_find_and_replace(tmp_path: Path, monkeypatch) -> None:
    """「先看修改」只改一段：不用把整章重发（9-27 真机 grok-4.6 用整份新内容把一章 3,480 字换成 2,122 字）。"""
    chapter = "05_正文内容/第一卷/CH004_定稿.md"
    long_text = "".join(f"第四章第 {n} 段，他说：“好的。”\n\n" for n in range(1, 61))

    def prepare(project: Path) -> None:
        (project / "05_正文内容" / "第一卷" / "CH004_定稿.md").write_text(long_text, encoding="utf-8")

    result, project = _propose(
        tmp_path,
        monkeypatch,
        [
            # 弯引号写成直引号也认（和 edit_file 一样放宽），两处按顺序叠在同一份上。
            {"operation": "edit", "path": chapter, "find": '第四章第 3 段，他说："好的。"', "replace": '第四章第 3 段，他说："不行。"'},
            {"operation": "edit", "path": chapter, "find": "第四章第 60 段", "replace": "第四章最后一段"},
        ],
        prepare=prepare,
    )
    assert result["status"] == "succeeded", result
    assert "notes" not in result["output"]
    text = (project / "05_正文内容" / "第一卷" / "CH004_定稿.md").read_text(encoding="utf-8")
    # 只动了这两处，别的一个字都没变（弯引号保留原样）。
    assert text == long_text.replace("第四章第 3 段，他说：“好的。”", "第四章第 3 段，他说：“不行。”").replace("第四章第 60 段", "第四章最后一段")


def test_a_proposal_edit_without_a_file_finds_the_one_file_that_has_the_passage(tmp_path: Path, monkeypatch) -> None:
    """9-27 真机：grok-4.6 三次用 edit 都没写文件，退回整份 update 时只交了一句，一章 5,282 字换成 19 字。"""
    result, project = _propose(
        tmp_path,
        monkeypatch,
        [{"operation": "edit", "find": "证人留下了带血钥匙", "replace": "证人留下了一把生锈的钥匙"}],
    )
    assert result["status"] == "succeeded", result
    assert (project / "05_正文内容" / "第一卷" / "CH003_定稿.md").read_text(encoding="utf-8") == "第三章：证人留下了一把生锈的钥匙。"


def test_a_proposal_edit_without_a_file_asks_for_a_path_when_the_passage_is_in_several(tmp_path: Path, monkeypatch) -> None:
    result, _project = _propose(tmp_path, monkeypatch, [{"operation": "edit", "find": "匿名信", "replace": "来信"}])
    assert result["status"] == "failed"
    message = result["error"]["error"]["message"]
    assert "2 个文件里都有" in message and "path" in message


def test_a_proposal_edit_accepts_an_absolute_path_inside_the_open_folder(tmp_path: Path, monkeypatch) -> None:
    absolute = str(tmp_path / "library" / "novel" / "05_正文内容" / "第一卷" / "CH002_定稿.md")
    result, project = _propose(tmp_path, monkeypatch, [{"operation": "edit", "path": absolute, "find": "旧港", "replace": "新港"}])
    assert result["status"] == "succeeded", result
    assert (project / "05_正文内容" / "第一卷" / "CH002_定稿.md").read_text(encoding="utf-8") == "第二章：匿名信指向新港。"


def test_a_proposal_edit_that_finds_nothing_pastes_back_the_passage(tmp_path: Path, monkeypatch) -> None:
    result, _project = _propose(
        tmp_path,
        monkeypatch,
        [{"operation": "edit", "path": "05_正文内容/第一卷/CH001_定稿.md", "find": "第一章：林澈收到了一封匿名来信", "replace": "x"}],
    )
    assert result["status"] == "failed"
    message = result["error"]["error"]["message"]
    assert "没有找到要替换的这段文字" in message and "第一章：林澈收到" in message


def test_overwrite_note_says_how_much_went_missing_only_when_a_lot_did() -> None:
    before = "\n".join(f"第 {n} 段，写了一些字。" for n in range(1, 101))
    note = _overwrite_note(before, "\n".join(before.split("\n")[:10]))
    assert "这是整份覆盖" in note and "100 行" in note and "10 行" in note and "少了" in note
    assert "edit_file" in note and "undo_change" in note
    assert _overwrite_note(before, before[: int(len(before) * 0.8)]) == ""
    assert _overwrite_note(None, "新文件") == ""
    assert _overwrite_note(before, before + "加一段") == ""


def test_write_file_that_drops_most_of_a_chapter_says_so(tmp_path: Path) -> None:
    long_chapter = "".join(f"第 {n} 段。\n\n" for n in range(1, 201))
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content=long_chapter, writer_mode="direct_write"
    )
    result = asyncio.run(_call(executor, run, "write_file", {"path": CHAPTER, "content": "第 1 段。\n\n第 2 段改了。\n"}))
    assert result.output["operation"] == "updated"
    assert result.output["before_characters"] == len(long_chapter)
    assert "这是整份覆盖" in result.output["message"]
    assert "少了" in result.output["message"]

    small = asyncio.run(
        _call(executor, run, "write_file", {"path": CHAPTER, "content": "第 1 段。\n\n第 2 段又改了。\n"}, call_id="call-0002")
    )
    assert "这是整份覆盖" not in small.output["message"]
