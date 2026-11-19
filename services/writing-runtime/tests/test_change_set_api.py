from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.change_set_models import (
    ChangeSetApplyRequest,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
)
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS


def _create_project(root: Path) -> dict[str, Path]:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    chapter = root / "05_正文内容" / "第一卷" / "CH001_正文.md"
    chapter.parent.mkdir()
    chapter.write_text("alpha\nkeep\nomega\n", encoding="utf-8")
    outline = root / "04_剧情大纲" / "全书大纲.md"
    outline.write_text(
        "# 主线\n青崖要夺回旧港钥匙。\n\n# 伏笔：血钥来历\n血钥不能提前揭穿。\n",
        encoding="utf-8",
    )
    character = root / "03_人物设定" / "主角.md"
    character.write_text(
        "# 青崖\n擅长借势破局，当前在旧港。\n\n- 持有血钥线索\n",
        encoding="utf-8",
    )
    world = root / "02_世界观设定" / "规则.md"
    world.write_text(
        "# 信息边界\n读者知道血钥，角色尚未确认。\n",
        encoding="utf-8",
    )
    notes = root / "08_素材库" / "旧素材.md"
    notes.write_text("old notes\n", encoding="utf-8")
    state = root / "08_素材库" / "状态.json"
    state.write_text(
        '{\n  "a": 1,\n  "keep": true,\n  "b": 2\n}\n',
        encoding="utf-8",
    )
    return {
        "chapter": chapter,
        "outline": outline,
        "character": character,
        "world": world,
        "notes": notes,
        "state": state,
    }


def _client(tmp_path: Path, trusted_root: Path) -> TestClient:
    return TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(trusted_root,),
                allowed_origins=(),
            )
        )
    )


def _inspect(client: TestClient, project: Path) -> dict:
    response = client.post(
        "/api/v1/projects/inspect",
        json={"root_path": str(project)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _file(snapshot: dict, suffix: str) -> dict:
    return next(item for item in snapshot["files"] if item["relative_path"].endswith(suffix))


def _accept_or_reject(
    client: TestClient,
    change_set: dict,
    decisions: list[str],
) -> dict:
    hunks = [
        hunk
        for change in change_set["changes"]
        for hunk in change["hunks"]
    ]
    assert len(hunks) == len(decisions)
    response = client.patch(
        f"/api/v1/change-sets/{change_set['change_set_id']}/decisions",
        json={
            "expected_review_revision": change_set["review_revision"],
            "decisions": [
                {"hunk_id": hunk["hunk_id"], "decision": decision}
                for hunk, decision in zip(hunks, decisions, strict=True)
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_public_create_rejects_forged_agent_provenance(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Forged agent source",
                "summary": "A public client cannot claim Runtime provenance.",
                "origin": "agent",
                "source_run_id": "run_fake-agent-source",
                "source_tool_call_id": "call_fake-agent-source",
                "source_request_hash": "a" * 64,
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "forged\n",
                        "rationale": "Exercise the public provenance boundary.",
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "change_set_origin_untrusted"


def test_author_can_edit_a_candidate_hunk_before_accepting_it(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "作者修改候选",
                "summary": "AI 给出候选后由作者继续修改。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "AI版本\nkeep\nomega\n",
                        "rationale": "验证作者可以改候选文字。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        hunk = created["changes"][0]["hunks"][0]

        edited_response = client.patch(
            f"/api/v1/change-sets/{created['change_set_id']}/hunks/{hunk['hunk_id']}",
            json={
                "expected_review_revision": created["review_revision"],
                "after_text": "作者自己改的开头\n",
            },
        )
        assert edited_response.status_code == 200, edited_response.text
        edited = edited_response.json()
        edited_hunk = edited["changes"][0]["hunks"][0]
        assert edited["status"] == "reviewing"
        assert edited["review_revision"] == created["review_revision"] + 1
        expected_hunk_text = "作者自己改的开头\r\n"
        assert edited_hunk["after_text"] == expected_hunk_text
        assert edited_hunk["decision"] == "pending"
        assert edited_hunk["after_sha256"] == hashlib.sha256(
            expected_hunk_text.encode("utf-8")
        ).hexdigest()

        stale_response = client.patch(
            f"/api/v1/change-sets/{created['change_set_id']}/hunks/{hunk['hunk_id']}",
            json={
                "expected_review_revision": created["review_revision"],
                "after_text": "过期的修改\n",
            },
        )
        assert stale_response.status_code == 409

        ready = _accept_or_reject(client, edited, ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        assert paths["chapter"].read_text(encoding="utf-8") == (
            "作者自己改的开头\nkeep\nomega\n"
        )


def test_author_can_discard_a_pending_proposal_without_touching_files(
    tmp_path: Path,
) -> None:
    """作者不想逐条审的过期候选：一键作废，正文一个字节不动。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "不再想看的候选",
                "summary": "一键作废验证。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "永远不会被写入\nkeep\nomega\n",
                        "rationale": "丢弃后正文必须保持原样。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        assert created["status"] == "reviewing"

        # 列表摘要带 base 版本绑定，界面才能区分「可应用」和「已过期」。
        listed_response = client.get(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets"
        )
        assert listed_response.status_code == 200, listed_response.text
        summary = next(
            item
            for item in listed_response.json()["change_sets"]
            if item["change_set_id"] == created["change_set_id"]
        )
        assert summary["base_revision"] == snapshot["project"]["revision"]
        assert summary["base_fingerprint"] == snapshot["project"]["fingerprint"]

        stale_guard = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/discard",
            json={"expected_review_revision": created["review_revision"] + 7},
        )
        assert stale_guard.status_code == 409, stale_guard.text

        discard_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/discard",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert discard_response.status_code == 200, discard_response.text
        discarded = discard_response.json()
        assert discarded["status"] == "discarded"
        assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"

        again = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/discard",
            json={"expected_review_revision": discarded["review_revision"]},
        )
        assert again.status_code == 409, again.text


def test_direct_apply_writes_one_safe_file_and_rollback_ignores_unrelated_changes(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "直接改这一章",
                "summary": "像普通编辑器一样直接保存，同时保留旧版本。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "ALPHA\nkeep\nomega\n",
                        "rationale": "验证直接修改与撤销。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()

        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert applied["status"] == "applied"
        assert applied["review_revision"] == created["review_revision"] + 1
        assert paths["chapter"].read_text(encoding="utf-8") == "ALPHA\nkeep\nomega\n"

        # Retrying after a lost response is idempotent and never writes twice.
        retried = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["writeback"] == applied["writeback"]

        # Editing a different project file must not make this chapter's undo unusable.
        paths["outline"].write_text("# 主线\n作者后来补充的全书大纲。\n", encoding="utf-8")
        rolled_response = client.post(
            f"/api/v1/change-sets/{applied['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        assert rolled_response.json()["status"] == "rolled_back"
        assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
        assert paths["outline"].read_text(encoding="utf-8") == (
            "# 主线\n作者后来补充的全书大纲。\n"
        )


def test_direct_apply_creates_an_empty_file_in_an_authors_own_folder_and_rolls_it_back(
    tmp_path: Path,
) -> None:
    """资料页「建好这份资料」：新建时先写空文件，作者当场就能打字。

    文件夹名字不在 01-09 模板里也得认，撤销要把文件和空夹子一起收掉。
    """

    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        target = project / "配角" / "剑奴.md"
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "新建资料：剑奴",
                "summary": "先建一份空白资料，作者马上在里面打字。",
                "origin": "manual",
                "gate_required": False,
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": "配角/剑奴.md",
                        "after_content": "",
                        "rationale": "验证空白新建也能落盘并撤回。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()

        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert applied["status"] == "applied"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""
        written = next(
            item
            for item in applied["writeback"]["files"]
            if item["relative_path"] == "配角/剑奴.md"
        )
        assert written["result_file_id"]

        rolled_response = client.post(
            f"/api/v1/change-sets/{applied['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        assert rolled_response.json()["status"] == "rolled_back"
        assert not target.exists()


def test_direct_apply_rollback_never_overwrites_later_edits_to_the_same_chapter(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "直接改这一章",
                "summary": "保存后仍允许作者继续手写。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "ALPHA\nkeep\nomega\n",
                        "rationale": "验证撤销不会覆盖作者后写的内容。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()

        later_text = "ALPHA\nkeep\n作者后来亲手补写的结尾。\n"
        paths["chapter"].write_text(later_text, encoding="utf-8")
        rolled_response = client.post(
            f"/api/v1/change-sets/{applied['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_response.status_code == 409, rolled_response.text
        assert rolled_response.json()["error"]["code"] == "change_set_conflict"
        assert paths["chapter"].read_text(encoding="utf-8") == later_text


def test_direct_apply_updates_three_existing_files_and_rolls_them_back(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        outline = _file(snapshot, "全书大纲.md")
        character = _file(snapshot, "主角.md")
        world = _file(snapshot, "规则.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "补齐本章记录",
                "summary": "一次更新人物状态、伏笔和章节梗概。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "# 主线\n本章局面已经推进。\n",
                        "rationale": "补本章梗概。",
                    },
                    {
                        "operation": "update",
                        "file_id": character["file_id"],
                        "after_content": "# 青崖\n当前已离开旧港。\n",
                        "rationale": "更新人物位置。",
                    },
                    {
                        "operation": "update",
                        "file_id": world["file_id"],
                        "after_content": "# 信息边界\n角色已经确认血钥。\n",
                        "rationale": "更新已揭晓信息。",
                    },
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert applied["status"] == "applied"
        assert paths["outline"].read_text(encoding="utf-8") == "# 主线\n本章局面已经推进。\n"
        assert paths["character"].read_text(encoding="utf-8") == "# 青崖\n当前已离开旧港。\n"
        assert paths["world"].read_text(encoding="utf-8") == "# 信息边界\n角色已经确认血钥。\n"

        rolled_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        assert rolled_response.json()["status"] == "rolled_back"
        assert "青崖要夺回旧港钥匙" in paths["outline"].read_text(encoding="utf-8")
        assert "当前在旧港" in paths["character"].read_text(encoding="utf-8")
        assert "角色尚未确认" in paths["world"].read_text(encoding="utf-8")


def test_direct_apply_takes_delete_and_wide_changes_without_bounce(tmp_path: Path) -> None:
    """「直接保存」不再弹回（2026-09-20 P2 第 7 条）：含删除、超过 3 份的批
    都直接落盘；撤回链兜底——删掉的文件能撤回，多文件批能整体回滚。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        notes = _file(snapshot, "旧素材.md")

        # 含删除的批：直接保存直接删，不再 409 弹回。
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "删掉旧素材",
                "summary": "作者选了直接保存。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "delete",
                        "file_id": notes["file_id"],
                        "rationale": "作者要求删除。",
                    }
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        assert applied_response.json()["status"] == "applied"
        assert not paths["notes"].exists()

        # 撤回链兜底：删掉的文件原样撤回来。
        rolled_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/rollback",
            json={"expected_review_revision": applied_response.json()["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        assert paths["notes"].read_text(encoding="utf-8") == "old notes\n"

        # 超过 3 份的批：直接保存一次全收。
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        notes = _file(snapshot, "旧素材.md")
        outline = _file(snapshot, "全书大纲.md")
        character = _file(snapshot, "主角.md")
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "四份一起收",
                "summary": "作者选了直接保存。",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "new chapter\n",
                        "rationale": "第一份文件。",
                    },
                    {
                        "operation": "update",
                        "file_id": notes["file_id"],
                        "after_content": "new notes\n",
                        "rationale": "第二份文件。",
                    },
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "new outline\n",
                        "rationale": "第三份文件。",
                    },
                    {
                        "operation": "update",
                        "file_id": character["file_id"],
                        "after_content": "new character\n",
                        "rationale": "第四份文件。",
                    },
                ],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        applied_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/apply-direct",
            json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        assert applied_response.json()["status"] == "applied"
        assert paths["chapter"].read_text(encoding="utf-8") == "new chapter\n"
        assert paths["notes"].read_text(encoding="utf-8") == "new notes\n"
        assert paths["outline"].read_text(encoding="utf-8") == "new outline\n"
        assert paths["character"].read_text(encoding="utf-8") == "new character\n"

        # 整体回滚：四个文件一起回到改前。
        rolled_response = client.post(
            f"/api/v1/change-sets/{created['change_set_id']}/rollback",
            json={"expected_review_revision": applied_response.json()["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
        assert paths["notes"].read_text(encoding="utf-8") == "old notes\n"


def test_update_supports_per_hunk_apply_persistence_and_rollback(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Tighten two lines",
                "summary": "Keep the middle line unchanged.",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "ALPHA\nkeep\nOMEGA\n",
                        "rationale": "Exercise independent hunks.",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        change_set = created.json()
        assert change_set["status"] == "reviewing"
        assert len(change_set["changes"][0]["hunks"]) == 2

        duplicate_skills = {
            **change_set,
            "source_skill_ids": ["novel-style-deai", "novel-style-deai"],
        }
        with pytest.raises(ValueError, match="source_skill_ids must be unique"):
            ChangeSetResponse.model_validate(duplicate_skills)
        ready_with_pending = {**change_set, "status": "ready"}
        with pytest.raises(ValueError, match="only reviewing"):
            ChangeSetResponse.model_validate(ready_with_pending)
        unsafe_response = change_set | {
            "changes": [
                change_set["changes"][0]
                | {"relative_path": "../outside.md"}
            ]
        }
        with pytest.raises(ValueError, match="relative_path"):
            ChangeSetResponse.model_validate(unsafe_response)

        ready = _accept_or_reject(client, change_set, ["accepted", "rejected"])
        assert ready["status"] == "ready"
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert applied["status"] == "applied"
        assert applied["writeback"]["result_revision"] == 2
        assert applied["writeback"]["files"][0]["result_file_id"]
        assert paths["chapter"].read_text(encoding="utf-8") == "ALPHA\nkeep\nomega\n"

        listed = client.get(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets"
        )
        assert listed.status_code == 200
        listed_record = listed.json()["change_sets"][0]
        assert listed_record["status"] == "applied"
        assert listed_record["file_count"] == 1
        assert listed_record["hunk_count"] == 2
        assert listed_record["accepted_hunk_count"] == 1
        assert listed_record["rejected_hunk_count"] == 1
        assert "changes" not in listed_record

        rolled_response = client.post(
            f"/api/v1/change-sets/{applied['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_response.status_code == 200, rolled_response.text
        rolled = rolled_response.json()
        assert rolled["status"] == "rolled_back"
        assert rolled["rollback"]["result_revision"] == 3
        assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"

    artifact_root = tmp_path / "change-sets" / change_set["change_set_id"]
    assert (artifact_root / "writeback-manifest.json").is_file()
    assert (artifact_root / "rollback-manifest.json").is_file()

    with _client(tmp_path, library) as restarted:
        persisted = restarted.get(
            f"/api/v1/change-sets/{change_set['change_set_id']}"
        )
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "rolled_back"


def test_multifile_create_delete_apply_and_rollback(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        notes = _file(snapshot, "旧素材.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Replace source note",
                "summary": "Create a structured note and remove the obsolete one.",
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": "08_素材库/导入/批次/新素材.json",
                        "after_content": '{"source":"local"}\n',
                        "rationale": "Add traceable source metadata.",
                    },
                    {
                        "operation": "delete",
                        "file_id": notes["file_id"],
                        "rationale": "Remove the superseded note.",
                    },
                ],
            },
        )
        assert response.status_code == 201, response.text
        ready = _accept_or_reject(client, response.json(), ["accepted", "accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        created = project / "08_素材库" / "导入" / "批次" / "新素材.json"
        assert created.read_text(encoding="utf-8") == '{"source":"local"}\n'
        assert not paths["notes"].exists()

        rolled = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        assert not created.exists()
        assert not (project / "08_素材库" / "导入").exists()
        assert paths["notes"].read_text(encoding="utf-8") == "old notes\n"


def test_external_change_marks_ready_proposal_conflicted(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": 1,
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Stale proposal",
                "summary": "This must fail after an external edit.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "changed by proposal\n",
                        "rationale": "Exercise stale rejection.",
                    }
                ],
            },
        )
        ready = _accept_or_reject(client, response.json(), ["accepted"])
        paths["outline"].write_text("external change\n", encoding="utf-8")

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        stored = client.get(f"/api/v1/change-sets/{ready['change_set_id']}")

    assert applied.status_code == 409
    assert applied.json()["error"]["code"] == "change_set_conflict"
    assert stored.json()["status"] == "conflicted"
    assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"


def test_write_failure_restores_already_replaced_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    original_apply = ChangeSetService._apply_prepared
    calls = 0

    def fail_second(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        original_apply(item)

    monkeypatch.setattr(ChangeSetService, "_apply_prepared", staticmethod(fail_second))
    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        outline = _file(snapshot, "全书大纲.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": 1,
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Two-file failure",
                "summary": "The first replacement must be restored.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "new chapter\n",
                        "rationale": "First write.",
                    },
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "new outline\n",
                        "rationale": "Second write.",
                    },
                ],
            },
        )
        ready = _accept_or_reject(client, response.json(), ["accepted", "accepted"])
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        stored = client.get(f"/api/v1/change-sets/{ready['change_set_id']}")

    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "change_set_writeback_failed"
    assert stored.json()["status"] == "failed"
    assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
    assert paths["outline"].read_text(encoding="utf-8") == (
        "# 主线\n青崖要夺回旧港钥匙。\n\n"
        "# 伏笔：血钥来历\n血钥不能提前揭穿。\n"
    )


def test_restart_recovers_keyboard_interrupt_during_multifile_apply(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)
    original_apply = ChangeSetService._apply_prepared
    calls = 0

    def interrupt_second(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated process interruption")
        original_apply(item)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        outline = _file(snapshot, "全书大纲.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Interrupted apply",
                "summary": "Recovery must restore both files to the proposal base.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "new chapter\n",
                        "rationale": "First replacement.",
                    },
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "new outline\n",
                        "rationale": "Second replacement.",
                    },
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted", "accepted"])

    service = ChangeSetService(
        ProjectReader((library,)),
        SqliteStore(tmp_path / "runtime.sqlite3"),
        tmp_path / "change-sets",
    )
    monkeypatch.setattr(
        ChangeSetService,
        "_apply_prepared",
        staticmethod(interrupt_second),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        service.apply(
            ready["change_set_id"],
            ChangeSetApplyRequest(
                expected_review_revision=ready["review_revision"],
            ),
        )

    monkeypatch.setattr(
        ChangeSetService,
        "_apply_prepared",
        staticmethod(original_apply),
    )
    with _client(tmp_path, library) as restarted:
        recovered = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )
        readiness = restarted.get("/health/ready")

    assert readiness.status_code == 200, readiness.text
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "failed"
    assert "restored to the pre-write" in recovered.json()["error"]
    assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
    assert paths["outline"].read_text(encoding="utf-8") == (
        "# 主线\n青崖要夺回旧港钥匙。\n\n"
        "# 伏笔：血钥来历\n血钥不能提前揭穿。\n"
    )


def test_restart_recovers_keyboard_interrupt_during_multifile_rollback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)
    original_apply = ChangeSetService._apply_prepared

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        outline = _file(snapshot, "全书大纲.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Interrupted rollback",
                "summary": "Recovery must preserve the fully applied project state.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "applied chapter\n",
                        "rationale": "First replacement.",
                    },
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "applied outline\n",
                        "rationale": "Second replacement.",
                    },
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted", "accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()

    calls = 0

    def interrupt_second(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated rollback interruption")
        original_apply(item)

    service = ChangeSetService(
        ProjectReader((library,)),
        SqliteStore(tmp_path / "runtime.sqlite3"),
        tmp_path / "change-sets",
    )
    monkeypatch.setattr(
        ChangeSetService,
        "_apply_prepared",
        staticmethod(interrupt_second),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated rollback interruption"):
        service.rollback(
            ready["change_set_id"],
            ChangeSetRollbackRequest(
                expected_review_revision=applied["review_revision"],
            ),
        )

    monkeypatch.setattr(
        ChangeSetService,
        "_apply_prepared",
        staticmethod(original_apply),
    )
    with _client(tmp_path, library) as restarted:
        recovered = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )
        readiness = restarted.get("/health/ready")

    assert readiness.status_code == 200, readiness.text
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "applied"
    assert "restored to the applied" in recovered.json()["error"]
    assert paths["chapter"].read_text(encoding="utf-8") == "applied chapter\n"
    assert paths["outline"].read_text(encoding="utf-8") == "applied outline\n"


def test_restart_recreates_applied_directories_after_interrupted_rollback_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    created_path = project / "08_素材库" / "导入" / "批次" / "新素材.md"

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Interrupted directory cleanup",
                "summary": "Applied directories must survive an interrupted rollback.",
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": "08_素材库/导入/批次/新素材.md",
                        "after_content": "runtime-owned source\n",
                        "rationale": "Exercise nested directory recovery.",
                    }
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert created_path.read_text(encoding="utf-8") == "runtime-owned source\n"

    original_remove = ChangeSetService._remove_recorded_directories

    def interrupt_after_cleanup(root, relative_paths):
        original_remove(root, relative_paths)
        raise KeyboardInterrupt("simulated directory cleanup interruption")

    service = ChangeSetService(
        ProjectReader((library,)),
        SqliteStore(tmp_path / "runtime.sqlite3"),
        tmp_path / "change-sets",
    )
    monkeypatch.setattr(
        ChangeSetService,
        "_remove_recorded_directories",
        staticmethod(interrupt_after_cleanup),
    )
    with pytest.raises(KeyboardInterrupt, match="directory cleanup interruption"):
        service.rollback(
            ready["change_set_id"],
            ChangeSetRollbackRequest(
                expected_review_revision=applied["review_revision"],
            ),
        )
    assert not created_path.exists()
    assert not (project / "08_素材库" / "导入").exists()

    monkeypatch.setattr(
        ChangeSetService,
        "_remove_recorded_directories",
        staticmethod(original_remove),
    )
    with _client(tmp_path, library) as restarted:
        recovered = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "applied"
    assert created_path.read_text(encoding="utf-8") == "runtime-owned source\n"


def test_partial_json_hunks_are_revalidated_before_write(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)
    original = paths["state"].read_text(encoding="utf-8")

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        state_file = _file(snapshot, "状态.json")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Partial JSON review",
                "summary": "Interdependent JSON hunks must remain structurally valid.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": state_file["file_id"],
                        "after_content": (
                            '{\n  "a": {\n  "keep": true,\n  "b": 2\n  }\n}\n'
                        ),
                        "rationale": "Create a nested object using two separated edits.",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        assert len(created.json()["changes"][0]["hunks"]) == 2
        invalid_ready = _accept_or_reject(
            client,
            created.json(),
            ["accepted", "rejected"],
        )
        rejected = client.post(
            f"/api/v1/change-sets/{invalid_ready['change_set_id']}/apply",
            json={"expected_review_revision": invalid_ready["review_revision"]},
        )
        stored = client.get(
            f"/api/v1/change-sets/{invalid_ready['change_set_id']}"
        ).json()

        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "change_set_input_invalid"
        assert stored["status"] == "ready"
        assert paths["state"].read_text(encoding="utf-8") == original

        valid_ready = _accept_or_reject(
            client,
            stored,
            ["accepted", "accepted"],
        )
        applied = client.post(
            f"/api/v1/change-sets/{valid_ready['change_set_id']}/apply",
            json={"expected_review_revision": valid_ready["review_revision"]},
        )

    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert '"a": {' in paths["state"].read_text(encoding="utf-8")


def test_external_target_edit_before_commit_is_not_absorbed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)
    original_text = paths["chapter"].read_text(encoding="utf-8")

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "External edit during commit",
                "summary": "An external target edit cannot become the writeback result.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "agent result\n",
                        "rationale": "Exercise the final target verification.",
                    }
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted"])
        original_snapshot = ChangeSetService._snapshot
        calls = 0

        def edit_before_result_snapshot(self, binding):
            nonlocal calls
            calls += 1
            if calls == 2:
                paths["chapter"].write_text("external edit\n", encoding="utf-8")
            return original_snapshot(self, binding)

        monkeypatch.setattr(ChangeSetService, "_snapshot", edit_before_result_snapshot)
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        stored = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()

    assert failed.status_code == 500, failed.text
    assert stored["status"] == "applying"
    assert stored["writeback"] is None
    assert paths["chapter"].read_text(encoding="utf-8") == "external edit\n"

    monkeypatch.setattr(ChangeSetService, "_snapshot", original_snapshot)
    with _client(tmp_path, library) as blocked:
        assert blocked.get("/health/ready").status_code == 503
        assert paths["chapter"].read_text(encoding="utf-8") == "external edit\n"

    paths["chapter"].write_text(original_text, encoding="utf-8")
    with _client(tmp_path, library) as restarted:
        assert restarted.get("/health/ready").status_code == 200
        recovered = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )
    assert recovered.json()["status"] == "failed"
    assert paths["chapter"].read_text(encoding="utf-8") == original_text


def test_failed_post_write_verification_rescans_compensated_project(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    def fail_verification(*_args) -> None:
        raise OSError("injected post-write verification failure")

    monkeypatch.setattr(
        ChangeSetService,
        "_verify_prepared_result",
        staticmethod(fail_verification),
    )
    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Compensated rescan",
                "summary": "A failed commit must leave the binding aligned with disk.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "replacement\n",
                        "rationale": "Exercise compensation after the result scan.",
                    }
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted"])
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )

        binding = SqliteStore(tmp_path / "runtime.sqlite3").get_project_binding(
            snapshot["project"]["project_id"]
        )
        compensated_fingerprint = ProjectReader((library,)).inspect(project)["project"][
            "fingerprint"
        ]

    assert failed.status_code == 500, failed.text
    assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
    assert binding is not None
    assert binding.revision == 3
    assert binding.fingerprint == compensated_fingerprint


def test_rollback_rejects_backup_reference_outside_artifact_root(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Untrusted backup reference",
                "summary": "Rollback must only consume Runtime-owned artifacts.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "applied\n",
                        "rationale": "Create a verified writeback first.",
                    }
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()

        outside = tmp_path / "outside-backup.md"
        outside.write_text("alpha\nkeep\nomega\n", encoding="utf-8")
        store = SqliteStore(tmp_path / "runtime.sqlite3")
        payload = store.get_change_set(applied["change_set_id"])
        assert payload is not None
        payload["writeback"]["files"][0]["backup_ref"] = "../../outside-backup.md"
        store.save_change_set(payload)

        rejected = client.post(
            f"/api/v1/change-sets/{applied['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )

    assert rejected.status_code == 500, rejected.text
    assert rejected.json()["error"]["code"] == "change_set_writeback_failed"
    assert paths["chapter"].read_text(encoding="utf-8") == "applied\n"


def test_compensation_failure_stays_recoverable_until_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)
    original_apply = ChangeSetService._apply_prepared
    original_restore = ChangeSetService._restore_prepared
    calls = 0

    def fail_second(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second replacement failure")
        original_apply(item)

    monkeypatch.setattr(ChangeSetService, "_apply_prepared", staticmethod(fail_second))
    monkeypatch.setattr(
        ChangeSetService,
        "_restore_prepared",
        staticmethod(lambda _items: "injected compensation failure"),
    )
    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        outline = _file(snapshot, "全书大纲.md")
        created = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Compensation failure",
                "summary": "The startup journal must remain authoritative.",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "new chapter\n",
                        "rationale": "First replacement.",
                    },
                    {
                        "operation": "update",
                        "file_id": outline["file_id"],
                        "after_content": "new outline\n",
                        "rationale": "Second replacement.",
                    },
                ],
            },
        )
        ready = _accept_or_reject(client, created.json(), ["accepted", "accepted"])
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        stored = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()

    assert failed.status_code == 500, failed.text
    assert stored["status"] == "applying"
    assert paths["chapter"].read_text(encoding="utf-8") == "new chapter\n"
    assert paths["outline"].read_text(encoding="utf-8") == (
        "# 主线\n青崖要夺回旧港钥匙。\n\n"
        "# 伏笔：血钥来历\n血钥不能提前揭穿。\n"
    )

    monkeypatch.setattr(
        ChangeSetService,
        "_apply_prepared",
        staticmethod(original_apply),
    )
    monkeypatch.setattr(
        ChangeSetService,
        "_restore_prepared",
        staticmethod(original_restore),
    )
    with _client(tmp_path, library) as restarted:
        assert restarted.get("/health/ready").status_code == 200
        recovered = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )

    assert recovered.json()["status"] == "failed"
    assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
    assert paths["outline"].read_text(encoding="utf-8") == (
        "# 主线\n青崖要夺回旧港钥匙。\n\n"
        "# 伏笔：血钥来历\n血钥不能提前揭穿。\n"
    )


def test_create_rejects_traversal_and_invalid_json(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        base = {
            "base_revision": 1,
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "title": "Invalid create",
            "summary": "Invalid targets cannot enter review.",
        }
        traversal = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                **base,
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": "../outside.md",
                        "after_content": "bad",
                        "rationale": "Traversal.",
                    }
                ],
            },
        )
        invalid_json = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                **base,
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": "08_素材库/bad.json",
                        "after_content": "{bad",
                        "rationale": "Invalid JSON.",
                    }
                ],
            },
        )

    assert traversal.status_code == 422
    assert traversal.json()["error"]["code"] == "request_validation_failed"
    assert invalid_json.status_code == 422
    assert invalid_json.json()["error"]["code"] == "change_set_input_invalid"
    assert not (tmp_path / "outside.md").exists()


def test_move_apply_and_rollback(tmp_path: Path) -> None:
    """move = 目标改名 + 撤回挪回（一次 API 建案、落盘、撤回全链）。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Rename the chapter",
                "summary": "Move the chapter file to its new name.",
                "changes": [
                    {
                        "operation": "move",
                        "file_id": chapter["file_id"],
                        "target_relative_path": "05_正文内容/第一卷/第 003 章 试剑.md",
                        "rationale": "Rename per the author's request.",
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["changes"][0]["operation"] == "move"
        assert (
            created["changes"][0]["target_relative_path"]
            == "05_正文内容/第一卷/第 003 章 试剑.md"
        )
        ready = _accept_or_reject(client, created, ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        moved = project / "05_正文内容" / "第一卷" / "第 003 章 试剑.md"
        assert not paths["chapter"].exists()
        assert moved.read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
        result = applied["writeback"]["files"][0]
        assert result["operation"] == "move"
        assert result["relative_path"] == "05_正文内容/第一卷/CH001_正文.md"
        assert result["target_relative_path"] == "05_正文内容/第一卷/第 003 章 试剑.md"
        assert result["after_sha256"] == result["before_sha256"]

        rolled = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["status"] == "rolled_back"
        assert paths["chapter"].read_text(encoding="utf-8") == "alpha\nkeep\nomega\n"
        assert not moved.exists()


def test_move_rejected_when_target_exists(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _file(snapshot, "CH001_正文.md")
        notes = _file(snapshot, "旧素材.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Move onto an occupied path",
                "summary": "The target already exists and must be rejected.",
                "changes": [
                    {
                        "operation": "move",
                        "file_id": chapter["file_id"],
                        "target_relative_path": notes["relative_path"],
                        "rationale": "Exercise the occupied-target guard.",
                    }
                ],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "change_set_input_invalid"
        assert (project / "08_素材库" / "旧素材.md").read_text(encoding="utf-8") == "old notes\n"


def test_move_into_new_folder_roundtrip(tmp_path: Path) -> None:
    """挪进尚不存在的文件夹：落盘时顺手建目录，撤回时目录一并清掉。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    paths = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        notes = _file(snapshot, "旧素材.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Refile the note",
                "summary": "Move the note into a new subfolder.",
                "changes": [
                    {
                        "operation": "move",
                        "file_id": notes["file_id"],
                        "target_relative_path": "08_素材库/导入/旧素材搬移.md",
                        "rationale": "Refile into the import folder.",
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        ready = _accept_or_reject(client, response.json(), ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        moved = project / "08_素材库" / "导入" / "旧素材搬移.md"
        assert moved.read_text(encoding="utf-8") == "old notes\n"
        assert not paths["notes"].exists()
        assert applied["writeback"]["created_directories"] == ["08_素材库/导入"]

        rolled = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        assert paths["notes"].read_text(encoding="utf-8") == "old notes\n"
        assert not moved.exists()
        assert not (project / "08_素材库" / "导入").exists()


def test_move_rollback_refused_after_later_edit(tmp_path: Path) -> None:
    """挪走后作者又改了内容：撤回按既有规矩拒绝，现有内容留住。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        notes = _file(snapshot, "旧素材.md")
        response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Refile the note",
                "summary": "Move the note, then edit it in place.",
                "changes": [
                    {
                        "operation": "move",
                        "file_id": notes["file_id"],
                        "target_relative_path": "08_素材库/搬走的素材.md",
                        "rationale": "Rename for the rollback-conflict guard.",
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        ready = _accept_or_reject(client, response.json(), ["accepted"])
        applied_response = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        moved = project / "08_素材库" / "搬走的素材.md"
        moved.write_text("old notes\n作者在挪走后又补了一句\n", encoding="utf-8")

        rolled = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled.status_code == 409, rolled.text
        assert "不能直接撤销" in rolled.json()["error"]["message"]
        assert moved.read_text(encoding="utf-8") == "old notes\n作者在挪走后又补了一句\n"
        assert not (project / "08_素材库" / "旧素材.md").exists()
