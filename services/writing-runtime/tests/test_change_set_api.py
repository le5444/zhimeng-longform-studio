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

        evidence_after_apply = client.get(
            f"/api/v1/change-sets/{applied['change_set_id']}/writeback-evidence"
        )
        assert evidence_after_apply.status_code == 200, evidence_after_apply.text
        applied_evidence = evidence_after_apply.json()
        assert applied_evidence["schema"] == "zhimeng.writeback-evidence-response.v1"
        assert applied_evidence["receipt"]["schema"] == "zhimeng.writeback-evidence-receipt.v1"
        assert applied_evidence["receipt"]["outcome"] == "applied"
        assert applied_evidence["receipt"]["formal_draft_allowed"] is False
        assert applied_evidence["receipt"]["quality_assessed"] is False
        assert applied_evidence["receipt"]["novel_state_synced"] is False
        assert applied_evidence["receipt"]["result_revision"] == 2
        assert applied_evidence["receipt"]["formal_draft_authorization_receipt_id"].startswith(
            "formal-draft-auth-"
        )
        assert len(applied_evidence["receipt"]["formal_draft_authorization_receipt_hash"]) == 64
        assert len(applied_evidence["history"]) == 1

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

        evidence_after_rollback = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/writeback-evidence"
        )
        assert evidence_after_rollback.status_code == 200, evidence_after_rollback.text
        rolled_evidence = evidence_after_rollback.json()
        assert rolled_evidence["receipt"]["outcome"] == "rolled_back"
        assert rolled_evidence["receipt"]["result_revision"] == 3
        assert rolled_evidence["receipt"]["formal_draft_allowed"] is False
        assert rolled_evidence["receipt"]["novel_state_synced"] is False
        assert rolled_evidence["receipt"]["formal_draft_authorization_receipt_id"].startswith(
            "formal-draft-auth-"
        )
        assert len(rolled_evidence["receipt"]["formal_draft_authorization_receipt_hash"]) == 64
        assert len(rolled_evidence["history"]) == 2
        assert [item["outcome"] for item in rolled_evidence["history"]] == [
            "applied",
            "rolled_back",
        ]

        empty_sync = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-writeback-sync"
        )
        assert empty_sync.status_code == 200, empty_sync.text
        assert empty_sync.json()["schema"] == "zhimeng.nlcl-writeback-sync-response.v1"
        assert empty_sync.json()["receipt"] is None

        sync_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-writeback-sync",
            json={
                "expected_writeback_evidence_receipt_hash": rolled_evidence[
                    "receipt"
                ]["receipt_hash"]
            },
        )
        assert sync_response.status_code == 200, sync_response.text
        synced = sync_response.json()
        assert synced["schema"] == "zhimeng.nlcl-writeback-sync-response.v1"
        receipt = synced["receipt"]
        assert receipt["schema"] == "zhimeng.nlcl-writeback-sync-receipt.v1"
        assert receipt["outcome"] == "rolled_back"
        assert receipt["writeback_evidence_receipt_hash"] == rolled_evidence[
            "receipt"
        ]["receipt_hash"]
        assert receipt["sync_scope"] == "product_native_evidence_continuity"
        assert receipt["original_nlcl_chapter_ops_usage_synced"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["novel_state_synced"] is False
        assert "05_正文内容/第一卷/CH001_正文.md" in receipt["affected_relative_paths"]
        assert receipt["chapter_relative_paths"] == [
            "05_正文内容/第一卷/CH001_正文.md"
        ]
        assert len(synced["history"]) == 1

        replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-writeback-sync",
            json={
                "expected_writeback_evidence_receipt_hash": rolled_evidence[
                    "receipt"
                ]["receipt_hash"]
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert replay.json()["receipt"]["receipt_hash"] == receipt["receipt_hash"]

        stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-writeback-sync",
            json={"expected_writeback_evidence_receipt_hash": "0" * 64},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "change_set_conflict"

        empty_usage = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-call-usage-continuity"
        )
        assert empty_usage.status_code == 200, empty_usage.text
        assert empty_usage.json()["schema"] == (
            "zhimeng.nlcl-call-usage-continuity-response.v1"
        )
        assert empty_usage.json()["receipt"] is None

        usage_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-call-usage-continuity",
            json={
                "expected_nlcl_writeback_sync_receipt_hash": receipt["receipt_hash"]
            },
        )
        assert usage_response.status_code == 200, usage_response.text
        usage = usage_response.json()
        assert usage["schema"] == "zhimeng.nlcl-call-usage-continuity-response.v1"
        usage_receipt = usage["receipt"]
        assert usage_receipt["schema"] == (
            "zhimeng.nlcl-call-usage-continuity-receipt.v1"
        )
        assert usage_receipt["outcome"] == "rolled_back"
        assert usage_receipt["nlcl_writeback_sync_receipt_hash"] == receipt[
            "receipt_hash"
        ]
        assert usage_receipt["usage_scope"] == (
            "product_native_selected_card_continuity"
        )
        assert usage_receipt["record_mode"] == "product_native_writeback_bound"
        assert usage_receipt["original_nlcl_call_usage_recorded"] is False
        assert usage_receipt["original_nlcl_chapter_ops_usage_synced"] is False
        assert usage_receipt["formal_draft_allowed"] is False
        assert usage_receipt["novel_state_synced"] is False
        assert usage_receipt["chapter_relative_paths"] == [
            "05_正文内容/第一卷/CH001_正文.md"
        ]
        # Rollback voids the product-native used set for this outcome.
        assert usage_receipt["used_card_ids"] == []
        assert usage_receipt["unused_card_ids"] == []
        assert usage_receipt["missed_card_ids"] == []
        assert len(usage["history"]) == 1

        usage_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-call-usage-continuity",
            json={
                "expected_nlcl_writeback_sync_receipt_hash": receipt["receipt_hash"]
            },
        )
        assert usage_replay.status_code == 200, usage_replay.text
        assert usage_replay.json()["receipt"]["receipt_id"] == usage_receipt[
            "receipt_id"
        ]
        assert usage_replay.json()["receipt"]["receipt_hash"] == usage_receipt[
            "receipt_hash"
        ]

        usage_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-call-usage-continuity",
            json={"expected_nlcl_writeback_sync_receipt_hash": "0" * 64},
        )
        assert usage_stale.status_code == 409, usage_stale.text
        assert usage_stale.json()["error"]["code"] == "change_set_conflict"

        empty_export = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-call-usage-export"
        )
        assert empty_export.status_code == 200, empty_export.text
        assert empty_export.json()["schema"] == (
            "zhimeng.nlcl-original-call-usage-export-response.v1"
        )
        assert empty_export.json()["receipt"] is None

        export_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-call-usage-export",
            json={
                "expected_nlcl_call_usage_continuity_receipt_hash": usage_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert export_response.status_code == 200, export_response.text
        exported = export_response.json()
        assert exported["schema"] == (
            "zhimeng.nlcl-original-call-usage-export-response.v1"
        )
        export_receipt = exported["receipt"]
        assert export_receipt["schema"] == (
            "zhimeng.nlcl-original-call-usage-export-receipt.v1"
        )
        assert export_receipt["outcome"] == "rolled_back"
        assert export_receipt["nlcl_call_usage_continuity_receipt_hash"] == (
            usage_receipt["receipt_hash"]
        )
        assert export_receipt["chapter_id"] == "CH001"
        assert export_receipt["canonical_chapter_key"] == "chapter:1"
        assert export_receipt["export_relative_path"] == (
            "07_运营数据/nlcl_feedback/call_usage.jsonl"
        )
        assert export_receipt["record_mode"] == "product_bridge_from_continuity"
        assert export_receipt["original_nlcl_call_usage_recorded"] is True
        assert export_receipt["original_nlcl_chapter_ops_usage_synced"] is False
        assert export_receipt["formal_draft_allowed"] is False
        assert export_receipt["quality_assessed"] is False
        assert export_receipt["novel_state_synced"] is False
        assert export_receipt["export_byte_offset"] == 0
        assert len(exported["history"]) == 1

        export_file = (
            project
            / "07_运营数据"
            / "nlcl_feedback"
            / "call_usage.jsonl"
        )
        assert export_file.is_file()
        export_lines = [
            line
            for line in export_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(export_lines) == 1
        export_row = json.loads(export_lines[0])
        assert export_row["bridge_schema"] == (
            "zhimeng.original-nlcl-call-usage-bridge-row.v1"
        )
        assert export_row["chapter_id"] == "CH001"
        assert export_row["canonical_chapter_key"] == "chapter:1"
        assert export_row["record_mode"] == "product_bridge_from_continuity"
        assert export_row["formal_draft_allowed"] is False
        assert export_row["product_continuity_receipt_hash"] == usage_receipt[
            "receipt_hash"
        ]
        assert export_receipt["export_file_sha256"] == hashlib.sha256(
            export_file.read_bytes()
        ).hexdigest()
        assert export_receipt["export_row_sha256"] == hashlib.sha256(
            (export_lines[0] + "\n").encode("utf-8")
        ).hexdigest()

        export_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-call-usage-export",
            json={
                "expected_nlcl_call_usage_continuity_receipt_hash": usage_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert export_replay.status_code == 200, export_replay.text
        assert export_replay.json()["receipt"]["receipt_id"] == export_receipt[
            "receipt_id"
        ]
        assert export_replay.json()["receipt"]["receipt_hash"] == export_receipt[
            "receipt_hash"
        ]
        replay_lines = [
            line
            for line in export_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(replay_lines) == 1

        export_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-call-usage-export",
            json={"expected_nlcl_call_usage_continuity_receipt_hash": "0" * 64},
        )
        assert export_stale.status_code == 409, export_stale.text
        assert export_stale.json()["error"]["code"] == "change_set_conflict"

        empty_ops = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-chapter-ops-usage-export"
        )
        assert empty_ops.status_code == 200, empty_ops.text
        assert empty_ops.json()["schema"] == (
            "zhimeng.nlcl-original-chapter-ops-usage-export-response.v1"
        )
        assert empty_ops.json()["receipt"] is None

        ops_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-chapter-ops-usage-export",
            json={
                "expected_nlcl_original_call_usage_export_receipt_hash": export_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert ops_response.status_code == 200, ops_response.text
        ops_exported = ops_response.json()
        assert ops_exported["schema"] == (
            "zhimeng.nlcl-original-chapter-ops-usage-export-response.v1"
        )
        ops_receipt = ops_exported["receipt"]
        assert ops_receipt["schema"] == (
            "zhimeng.nlcl-original-chapter-ops-usage-export-receipt.v1"
        )
        assert ops_receipt["outcome"] == "rolled_back"
        assert ops_receipt["nlcl_original_call_usage_export_receipt_hash"] == (
            export_receipt["receipt_hash"]
        )
        assert ops_receipt["chapter_id"] == "CH001"
        assert ops_receipt["canonical_chapter_key"] == "chapter:1"
        assert ops_receipt["export_relative_path"] == (
            "07_运营数据/nlcl_feedback/chapter_ops_usage.jsonl"
        )
        assert ops_receipt["record_mode"] == (
            "product_bridge_chapter_ops_from_call_usage_export"
        )
        assert ops_receipt["original_nlcl_call_usage_recorded"] is True
        assert ops_receipt["original_nlcl_chapter_ops_usage_synced"] is True
        assert ops_receipt["formal_draft_allowed"] is False
        assert ops_receipt["quality_assessed"] is False
        assert ops_receipt["novel_state_synced"] is False
        assert ops_receipt["export_byte_offset"] == 0
        assert len(ops_exported["history"]) == 1

        ops_file = (
            project
            / "07_运营数据"
            / "nlcl_feedback"
            / "chapter_ops_usage.jsonl"
        )
        assert ops_file.is_file()
        ops_lines = [
            line
            for line in ops_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(ops_lines) == 1
        ops_row = json.loads(ops_lines[0])
        assert ops_row["bridge_schema"] == (
            "zhimeng.original-nlcl-chapter-ops-usage-bridge-row.v1"
        )
        assert ops_row["chapter_id"] == "CH001"
        assert ops_row["canonical_chapter_key"] == "chapter:1"
        assert ops_row["record_mode"] == (
            "product_bridge_chapter_ops_from_call_usage_export"
        )
        assert ops_row["original_nlcl_chapter_ops_usage_synced"] is True
        assert ops_row["formal_draft_allowed"] is False
        assert ops_row["novel_state_synced"] is False
        assert ops_row["product_call_usage_export_receipt_hash"] == export_receipt[
            "receipt_hash"
        ]
        assert ops_receipt["export_file_sha256"] == hashlib.sha256(
            ops_file.read_bytes()
        ).hexdigest()
        assert ops_receipt["export_row_sha256"] == hashlib.sha256(
            (ops_lines[0] + "\n").encode("utf-8")
        ).hexdigest()

        ops_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-chapter-ops-usage-export",
            json={
                "expected_nlcl_original_call_usage_export_receipt_hash": export_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert ops_replay.status_code == 200, ops_replay.text
        assert ops_replay.json()["receipt"]["receipt_id"] == ops_receipt["receipt_id"]
        assert ops_replay.json()["receipt"]["receipt_hash"] == ops_receipt[
            "receipt_hash"
        ]
        replay_ops_lines = [
            line
            for line in ops_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(replay_ops_lines) == 1

        ops_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-chapter-ops-usage-export",
            json={
                "expected_nlcl_original_call_usage_export_receipt_hash": "0" * 64
            },
        )
        assert ops_stale.status_code == 409, ops_stale.text
        assert ops_stale.json()["error"]["code"] == "change_set_conflict"

        empty_state = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-bridge"
        )
        assert empty_state.status_code == 200, empty_state.text
        assert empty_state.json()["schema"] == (
            "zhimeng.nlcl-original-novel-state-bridge-response.v1"
        )
        assert empty_state.json()["receipt"] is None

        state_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-bridge",
            json={
                "expected_nlcl_original_chapter_ops_usage_export_receipt_hash": ops_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert state_response.status_code == 200, state_response.text
        state_exported = state_response.json()
        assert state_exported["schema"] == (
            "zhimeng.nlcl-original-novel-state-bridge-response.v1"
        )
        state_receipt = state_exported["receipt"]
        assert state_receipt["schema"] == (
            "zhimeng.nlcl-original-novel-state-bridge-receipt.v1"
        )
        assert state_receipt["outcome"] == "rolled_back"
        assert state_receipt["nlcl_original_chapter_ops_usage_export_receipt_hash"] == (
            ops_receipt["receipt_hash"]
        )
        assert state_receipt["export_relative_path"] == (
            "07_运营数据/nlcl_feedback/novel_state_bridge.jsonl"
        )
        assert state_receipt["record_mode"] == (
            "product_bridge_novel_state_inventory_from_chapter_ops"
        )
        assert state_receipt["original_nlcl_call_usage_recorded"] is True
        assert state_receipt["original_nlcl_chapter_ops_usage_synced"] is True
        assert state_receipt["original_nlcl_novel_state_bridge_recorded"] is True
        assert state_receipt["formal_draft_allowed"] is False
        assert state_receipt["quality_assessed"] is False
        assert state_receipt["novel_state_synced"] is False
        assert isinstance(state_receipt["character_state_refs"], list)
        assert isinstance(state_receipt["plot_state_refs"], list)
        assert isinstance(state_receipt["foreshadowing_state_refs"], list)
        assert isinstance(state_receipt["project_status_refs"], list)
        total_refs = (
            len(state_receipt["character_state_refs"])
            + len(state_receipt["plot_state_refs"])
            + len(state_receipt["foreshadowing_state_refs"])
            + len(state_receipt["project_status_refs"])
        )
        assert total_refs >= 1
        assert len(state_receipt["plot_state_refs"]) >= 1
        assert all(
            item["domain"] == "plot" for item in state_receipt["plot_state_refs"]
        )
        assert len(state_exported["history"]) == 1

        state_file = (
            project
            / "07_运营数据"
            / "nlcl_feedback"
            / "novel_state_bridge.jsonl"
        )
        assert state_file.is_file()
        state_lines = [
            line
            for line in state_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(state_lines) == 1
        state_row = json.loads(state_lines[0])
        assert state_row["bridge_schema"] == (
            "zhimeng.original-nlcl-novel-state-bridge-row.v1"
        )
        assert state_row["original_nlcl_novel_state_bridge_recorded"] is True
        assert state_row["novel_state_synced"] is False
        assert state_receipt["export_file_sha256"] == hashlib.sha256(
            state_file.read_bytes()
        ).hexdigest()

        state_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-bridge",
            json={
                "expected_nlcl_original_chapter_ops_usage_export_receipt_hash": ops_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert state_replay.status_code == 200, state_replay.text
        assert state_replay.json()["receipt"]["receipt_id"] == state_receipt["receipt_id"]
        replay_state_lines = [
            line
            for line in state_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(replay_state_lines) == 1

        state_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-bridge",
            json={
                "expected_nlcl_original_chapter_ops_usage_export_receipt_hash": "0" * 64
            },
        )
        assert state_stale.status_code == 409, state_stale.text
        assert state_stale.json()["error"]["code"] == "change_set_conflict"

        empty_sync = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-sync"
        )
        assert empty_sync.status_code == 200, empty_sync.text
        assert empty_sync.json()["schema"] == (
            "zhimeng.nlcl-original-novel-state-sync-response.v1"
        )
        assert empty_sync.json()["receipt"] is None

        sync_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-sync",
            json={
                "expected_nlcl_original_novel_state_bridge_receipt_hash": state_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert sync_response.status_code == 200, sync_response.text
        sync_body = sync_response.json()
        sync_receipt = sync_body["receipt"]
        assert sync_receipt["schema"] == (
            "zhimeng.nlcl-original-novel-state-sync-receipt.v1"
        )
        assert sync_receipt["novel_state_synced"] is True
        assert sync_receipt["original_nlcl_novel_state_synced"] is True
        assert sync_receipt["original_nlcl_truth_matrix_synced"] is True
        assert sync_receipt["formal_draft_allowed"] is False
        assert sync_receipt["quality_assessed"] is False
        assert sync_receipt["state_root_relative_path"] == "state"
        assert len(sync_receipt["written_files"]) == 7
        domains = {item["domain"] for item in sync_receipt["written_files"]}
        assert domains == {
            "character",
            "plot",
            "foreshadowing",
            "project_status",
            "truth_character_matrix",
            "truth_foreshadow_lifecycle",
            "truth_info_boundary",
        }
        for item in sync_receipt["written_files"]:
            path = project.joinpath(*item["relative_path"].split("/"))
            assert path.is_file()
            assert item["source_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["_meta"]["last_updated_from_chapter"] == sync_receipt["chapter_id"]

        status_payload = json.loads(
            (project / "state" / "project_status.json").read_text(encoding="utf-8")
        )
        assert status_payload["last_updated_from_chapter"] == "CH001"
        assert status_payload["state_sync_status"] == "product_bridge_writeback_synced"
        truth_character = json.loads(
            (project / "state" / "truth_character_matrix.json").read_text(encoding="utf-8")
        )
        assert truth_character["_meta"]["external_alignment"] == "真相文件_角色状态矩阵"
        assert "characters" in truth_character
        truth_foreshadow = json.loads(
            (project / "state" / "truth_foreshadow_lifecycle.json").read_text(encoding="utf-8")
        )
        assert truth_foreshadow["_meta"]["external_alignment"] == "真相文件_伏笔生命周期"
        assert "open_debts" in truth_foreshadow
        truth_info = json.loads(
            (project / "state" / "truth_info_boundary.json").read_text(encoding="utf-8")
        )
        assert truth_info["_meta"]["external_alignment"] == "真相文件_信息边界图"
        assert "forbidden_cross_knowledge" in truth_info

        sync_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-sync",
            json={
                "expected_nlcl_original_novel_state_bridge_receipt_hash": state_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert sync_replay.status_code == 200, sync_replay.text
        assert sync_replay.json()["receipt"]["receipt_id"] == sync_receipt["receipt_id"]

        sync_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-novel-state-sync",
            json={
                "expected_nlcl_original_novel_state_bridge_receipt_hash": "0" * 64
            },
        )
        assert sync_stale.status_code == 409, sync_stale.text
        assert sync_stale.json()["error"]["code"] == "change_set_conflict"

        empty_extract = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-truth-content-extract"
        )
        assert empty_extract.status_code == 200, empty_extract.text
        assert empty_extract.json()["schema"] == (
            "zhimeng.nlcl-original-truth-content-extract-response.v1"
        )
        assert empty_extract.json()["receipt"] is None

        extract_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-truth-content-extract",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert extract_response.status_code == 200, extract_response.text
        extract_body = extract_response.json()
        extract_receipt = extract_body["receipt"]
        assert extract_receipt["schema"] == (
            "zhimeng.nlcl-original-truth-content-extract-receipt.v1"
        )
        assert extract_receipt["truth_content_extracted"] is True
        assert extract_receipt["formal_draft_allowed"] is False
        assert extract_receipt["quality_assessed"] is False
        assert extract_receipt["extracted_character_count"] >= 1
        assert extract_receipt["extracted_foreshadow_count"] >= 1
        assert len(extract_receipt["written_files"]) == 6
        char_state = json.loads(
            (project / "state" / "character_state.json").read_text(encoding="utf-8")
        )
        assert any("青崖" in str(v.get("name", "")) for v in char_state.get("characters", {}).values())
        truth_char = json.loads(
            (project / "state" / "truth_character_matrix.json").read_text(encoding="utf-8")
        )
        assert any("青崖" in str(v.get("name", "")) for v in truth_char.get("characters", {}).values())
        foreshadow = json.loads(
            (project / "state" / "truth_foreshadow_lifecycle.json").read_text(encoding="utf-8")
        )
        assert any("伏笔" in str(item.get("title", "")) for item in foreshadow.get("open_debts", []))

        extract_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-truth-content-extract",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert extract_replay.status_code == 200, extract_replay.text
        assert extract_replay.json()["receipt"]["receipt_id"] == extract_receipt["receipt_id"]

        extract_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-truth-content-extract",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": "0" * 64
            },
        )
        assert extract_stale.status_code == 409, extract_stale.text
        assert extract_stale.json()["error"]["code"] == "change_set_conflict"

        empty_plan = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-plan"
        )
        assert empty_plan.status_code == 200, empty_plan.text
        assert empty_plan.json()["schema"] == (
            "zhimeng.nlcl-original-mandatory-usage-plan-response.v1"
        )
        assert empty_plan.json()["receipt"] is None

        plan_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-plan",
            json={
                "expected_nlcl_original_truth_content_extract_receipt_hash": extract_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert plan_response.status_code == 200, plan_response.text
        plan_receipt = plan_response.json()["receipt"]
        assert plan_receipt["schema"] == (
            "zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1"
        )
        assert plan_receipt["mandatory_usage_plan_synced"] is True
        assert plan_receipt["mandatory_usage_closure_closed"] is False
        assert plan_receipt["formal_draft_allowed"] is False
        assert plan_receipt["quality_assessed"] is False
        assert len(plan_receipt["items"]) >= 1
        assert (
            project / "chapter_context" / f"{plan_receipt['chapter_id']}_mandatory_usage_plan_latest.json"
        ).is_file()
        assert (
            project / "chapter_context" / f"{plan_receipt['chapter_id']}_mandatory_usage_plan_latest.md"
        ).is_file()

        plan_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-plan",
            json={
                "expected_nlcl_original_truth_content_extract_receipt_hash": extract_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert plan_replay.status_code == 200, plan_replay.text
        assert plan_replay.json()["receipt"]["receipt_id"] == plan_receipt["receipt_id"]

        plan_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-plan",
            json={
                "expected_nlcl_original_truth_content_extract_receipt_hash": "0" * 64
            },
        )
        assert plan_stale.status_code == 409, plan_stale.text
        assert plan_stale.json()["error"]["code"] == "change_set_conflict"

        empty_closure = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-closure"
        )
        assert empty_closure.status_code == 200, empty_closure.text
        assert empty_closure.json()["schema"] == (
            "zhimeng.nlcl-original-mandatory-usage-closure-response.v1"
        )
        assert empty_closure.json()["receipt"] is None

        incomplete_closure = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-closure",
            json={
                "expected_nlcl_original_mandatory_usage_plan_receipt_hash": plan_receipt[
                    "receipt_hash"
                ],
                "decisions": [
                    {
                        "item_id": plan_receipt["items"][0]["item_id"],
                        "status": "used",
                        "note": "only first item",
                    }
                ],
                "finalize": True,
            },
        )
        assert incomplete_closure.status_code == 409, incomplete_closure.text

        closure_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-closure",
            json={
                "expected_nlcl_original_mandatory_usage_plan_receipt_hash": plan_receipt[
                    "receipt_hash"
                ],
                "decisions": [
                    {
                        "item_id": item["item_id"],
                        "status": "used" if item["kind"] in {"character", "foreshadow", "plot_thread"} else "suppressed_for_project_truth",
                        "note": "author decided in product bridge test",
                    }
                    for item in plan_receipt["items"]
                ],
                "finalize": True,
            },
        )
        assert closure_response.status_code == 200, closure_response.text
        closure_receipt = closure_response.json()["receipt"]
        assert closure_receipt["schema"] == (
            "zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1"
        )
        assert closure_receipt["mandatory_usage_closure_closed"] is True
        assert closure_receipt["route_validated"] is False
        assert closure_receipt["formal_draft_allowed"] is False
        assert closure_receipt["quality_assessed"] is False
        assert closure_receipt["overall_status"] == "closed"
        assert len(closure_receipt["items"]) == len(plan_receipt["items"])
        assert (
            project
            / "chapter_context"
            / f"{closure_receipt['chapter_id']}_mandatory_usage_closure_latest.json"
        ).is_file()

        closure_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-closure",
            json={
                "expected_nlcl_original_mandatory_usage_plan_receipt_hash": plan_receipt[
                    "receipt_hash"
                ],
                "decisions": [
                    {
                        "item_id": item["item_id"],
                        "status": "used" if item["kind"] in {"character", "foreshadow", "plot_thread"} else "suppressed_for_project_truth",
                        "note": "author decided in product bridge test",
                    }
                    for item in plan_receipt["items"]
                ],
                "finalize": True,
            },
        )
        assert closure_replay.status_code == 200, closure_replay.text
        assert closure_replay.json()["receipt"]["receipt_id"] == closure_receipt["receipt_id"]

        closure_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-mandatory-usage-closure",
            json={
                "expected_nlcl_original_mandatory_usage_plan_receipt_hash": "0" * 64,
                "decisions": [
                    {
                        "item_id": plan_receipt["items"][0]["item_id"],
                        "status": "used",
                    }
                ],
                "finalize": True,
            },
        )
        assert closure_stale.status_code == 409, closure_stale.text
        assert closure_stale.json()["error"]["code"] == "change_set_conflict"

        empty_package = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-operation-package-like"
        )
        assert empty_package.status_code == 200, empty_package.text
        assert empty_package.json()["receipt"] is None

        package_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-operation-package-like",
            json={
                "expected_nlcl_original_mandatory_usage_closure_receipt_hash": closure_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert package_response.status_code == 200, package_response.text
        package_receipt = package_response.json()["receipt"]
        assert package_receipt["operation_package_like_synced"] is True
        assert package_receipt["readiness"] == "risk_draft_only"
        assert package_receipt["formal_draft_allowed"] is False
        assert package_receipt["route_validated"] is False
        assert (
            project
            / "chapter_context"
            / f"{package_receipt['chapter_id']}_operation_package_like_latest.json"
        ).is_file()

        package_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-operation-package-like",
            json={
                "expected_nlcl_original_mandatory_usage_closure_receipt_hash": closure_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert package_replay.status_code == 200, package_replay.text
        assert package_replay.json()["receipt"]["receipt_id"] == package_receipt["receipt_id"]

        package_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-operation-package-like",
            json={
                "expected_nlcl_original_mandatory_usage_closure_receipt_hash": "0" * 64
            },
        )
        assert package_stale.status_code == 409, package_stale.text
        assert package_stale.json()["error"]["code"] == "change_set_conflict"

        empty_layout = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-inventory"
        )
        assert empty_layout.status_code == 200, empty_layout.text
        assert empty_layout.json()["receipt"] is None

        layout_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-inventory",
            json={
                "expected_nlcl_original_operation_package_like_receipt_hash": package_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert layout_response.status_code == 200, layout_response.text
        layout_receipt = layout_response.json()["receipt"]
        assert layout_receipt["library_layout_inventoried"] is True
        assert layout_receipt["library_layout_complete"] is False
        assert layout_receipt["whole_library_load_forbidden"] is True
        assert layout_receipt["formal_draft_allowed"] is False
        assert layout_receipt["present_slot_count"] >= 1
        assert (
            project
            / "chapter_context"
            / f"{layout_receipt['chapter_id']}_library_layout_inventory_latest.json"
        ).is_file()

        layout_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-inventory",
            json={
                "expected_nlcl_original_operation_package_like_receipt_hash": package_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert layout_replay.status_code == 200, layout_replay.text
        assert layout_replay.json()["receipt"]["receipt_id"] == layout_receipt["receipt_id"]

        layout_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-inventory",
            json={
                "expected_nlcl_original_operation_package_like_receipt_hash": "0" * 64
            },
        )
        assert layout_stale.status_code == 409, layout_stale.text
        assert layout_stale.json()["error"]["code"] == "change_set_conflict"

        empty_complete = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-completeness"
        )
        assert empty_complete.status_code == 200, empty_complete.text
        assert empty_complete.json()["receipt"] is None

        complete_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-completeness",
            json={
                "expected_nlcl_original_library_layout_inventory_receipt_hash": layout_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert complete_response.status_code == 200, complete_response.text
        complete_receipt = complete_response.json()["receipt"]
        assert complete_receipt["library_layout_assessed"] is True
        assert complete_receipt["whole_library_load_forbidden"] is True
        assert complete_receipt["formal_draft_allowed"] is False
        assert complete_receipt["library_layout_complete"] is (
            complete_receipt["missing_required_slot_count"] == 0
        )
        assert complete_receipt["completeness_status"] in {"complete", "incomplete"}
        assert (
            project
            / "chapter_context"
            / f"{complete_receipt['chapter_id']}_library_layout_completeness_latest.json"
        ).is_file()

        complete_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-completeness",
            json={
                "expected_nlcl_original_library_layout_inventory_receipt_hash": layout_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert complete_replay.status_code == 200, complete_replay.text
        assert complete_replay.json()["receipt"]["receipt_id"] == complete_receipt["receipt_id"]

        complete_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-library-layout-completeness",
            json={
                "expected_nlcl_original_library_layout_inventory_receipt_hash": "0" * 64
            },
        )
        assert complete_stale.status_code == 409, complete_stale.text
        assert complete_stale.json()["error"]["code"] == "change_set_conflict"

        empty_activation = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-bounded-library-activation"
        )
        assert empty_activation.status_code == 200, empty_activation.text
        assert empty_activation.json()["receipt"] is None

        activation_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-bounded-library-activation",
            json={
                "expected_nlcl_original_library_layout_completeness_receipt_hash": complete_receipt[
                    "receipt_hash"
                ],
                "max_sources": 8,
            },
        )
        assert activation_response.status_code == 200, activation_response.text
        activation_receipt = activation_response.json()["receipt"]
        assert activation_receipt["bounded_library_activated"] is True
        assert activation_receipt["whole_library_load"] is False
        assert activation_receipt["whole_library_load_forbidden"] is True
        assert activation_receipt["formal_draft_allowed"] is False
        assert activation_receipt["max_sources"] == 8
        assert activation_receipt["activated_count"] == len(
            activation_receipt["activated_source_ids"]
        )
        assert activation_receipt["activated_count"] <= 8
        assert (
            project
            / "chapter_context"
            / f"{activation_receipt['chapter_id']}_bounded_library_activation_latest.json"
        ).is_file()

        activation_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-bounded-library-activation",
            json={
                "expected_nlcl_original_library_layout_completeness_receipt_hash": complete_receipt[
                    "receipt_hash"
                ],
                "max_sources": 8,
            },
        )
        assert activation_replay.status_code == 200, activation_replay.text
        assert (
            activation_replay.json()["receipt"]["receipt_id"]
            == activation_receipt["receipt_id"]
        )

        activation_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-bounded-library-activation",
            json={
                "expected_nlcl_original_library_layout_completeness_receipt_hash": "0" * 64,
                "max_sources": 8,
            },
        )
        assert activation_stale.status_code == 409, activation_stale.text
        assert activation_stale.json()["error"]["code"] == "change_set_conflict"

        empty_prewrite = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-prewrite-context-report"
        )
        assert empty_prewrite.status_code == 200, empty_prewrite.text
        assert empty_prewrite.json()["schema"] == (
            "zhimeng.nlcl-original-prewrite-context-report-response.v1"
        )
        assert empty_prewrite.json()["receipt"] is None

        prewrite_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-prewrite-context-report",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert prewrite_response.status_code == 200, prewrite_response.text
        prewrite_body = prewrite_response.json()
        prewrite_receipt = prewrite_body["receipt"]
        assert prewrite_receipt["schema"] == (
            "zhimeng.nlcl-original-prewrite-context-report-receipt.v1"
        )
        assert prewrite_receipt["prewrite_context_report_synced"] is True
        assert prewrite_receipt["original_nlcl_truth_matrix_synced"] is True
        assert prewrite_receipt["formal_draft_allowed"] is False
        assert prewrite_receipt["quality_assessed"] is False
        assert prewrite_receipt["bounded_library_activated"] is True
        assert prewrite_receipt["activated_source_count"] == activation_receipt["activated_count"]
        assert (
            prewrite_receipt["nlcl_original_bounded_library_activation_receipt_hash"]
            == activation_receipt["receipt_hash"]
        )
        assert prewrite_receipt["whole_library_load_forbidden"] is True
        assert len(prewrite_receipt["written_files"]) == 4
        report_json = (
            project
            / "chapter_context"
            / f"{prewrite_receipt['chapter_id']}_prewrite_context_report_latest.json"
        )
        report_payload = __import__("json").loads(report_json.read_text(encoding="utf-8"))
        library_activation = report_payload.get("library_activation") or report_payload.get(
            "prewrite_context_report", {}
        ).get("library_activation")
        assert library_activation is not None
        assert library_activation["bounded_library_activated"] is True
        assert library_activation["whole_library_load"] is False
        for item in prewrite_receipt["written_files"]:
            path = project.joinpath(*item["relative_path"].split("/"))
            assert path.is_file()
            assert item["source_hash"] == hashlib.sha256(path.read_bytes()).hexdigest()
        latest_json = project / "chapter_context" / f"{prewrite_receipt['chapter_id']}_prewrite_context_report_latest.json"
        latest_payload = json.loads(latest_json.read_text(encoding="utf-8"))
        assert "prewrite_context_report" in latest_payload
        assert latest_payload["prewrite_context_report"]["chapter_id"] == prewrite_receipt["chapter_id"]
        latest_md = project / "chapter_context" / f"{prewrite_receipt['chapter_id']}_prewrite_context_report_latest.md"
        assert "前情写作上下文报告" in latest_md.read_text(encoding="utf-8")

        prewrite_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-prewrite-context-report",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert prewrite_replay.status_code == 200, prewrite_replay.text
        assert prewrite_replay.json()["receipt"]["receipt_id"] == prewrite_receipt["receipt_id"]

        prewrite_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/nlcl-original-prewrite-context-report",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": "0" * 64
            },
        )
        assert prewrite_stale.status_code == 409, prewrite_stale.text
        assert prewrite_stale.json()["error"]["code"] == "change_set_conflict"

        empty_reader = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review"
        )
        assert empty_reader.status_code == 200, empty_reader.text
        assert empty_reader.json()["schema"] == "zhimeng.reader-review-response.v1"
        assert empty_reader.json()["receipt"] is None

        reader_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review",
            json={
                "expected_result_fingerprint": rolled["writeback"]["result_fingerprint"]
            },
        )
        assert reader_response.status_code == 200, reader_response.text
        reviewed = reader_response.json()
        assert reviewed["schema"] == "zhimeng.reader-review-response.v1"
        reader_receipt = reviewed["receipt"]
        assert reader_receipt["schema"] == "zhimeng.reader-review-receipt.v1"
        assert reader_receipt["evaluator_kind"] == "deterministic_cold_read_v1"
        assert reader_receipt["evaluator_id"] == "zhimeng.reader.deterministic-cold-read"
        assert reader_receipt["model_id"] == "none"
        assert reader_receipt["prompt_fingerprint"]
        assert reader_receipt["context_fingerprint"]
        assert reader_receipt["evaluator_fingerprint"]
        assert reader_receipt["outcome"] == "rolled_back"
        assert reader_receipt["review_mode"] == "deterministic_cold_read_v1"
        assert reader_receipt["reader_opinion_recorded"] is True
        assert reader_receipt["overrides_gate"] is False
        assert reader_receipt["formal_draft_allowed"] is False
        assert reader_receipt["quality_assessed"] is False
        assert reader_receipt["novel_state_synced"] is False
        assert "planner_context" in reader_receipt["excluded_input_classes"]
        assert "prior_published_chapter_text" in reader_receipt["allowed_input_classes"]
        assert len(reader_receipt["findings"]) >= 1
        assert all(item["overrides_gate"] is False for item in reader_receipt["findings"])
        assert len(reviewed["history"]) == 1

        reader_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review",
            json={
                "expected_result_fingerprint": rolled["writeback"]["result_fingerprint"]
            },
        )
        assert reader_replay.status_code == 200, reader_replay.text
        assert reader_replay.json()["receipt"]["receipt_id"] == reader_receipt[
            "receipt_id"
        ]
        assert reader_replay.json()["receipt"]["receipt_hash"] == reader_receipt[
            "receipt_hash"
        ]

        reader_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review",
            json={"expected_result_fingerprint": "0" * 64},
        )
        assert reader_stale.status_code == 409, reader_stale.text
        assert reader_stale.json()["error"]["code"] == "change_set_conflict"

        empty_decision = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review-decision"
        )
        assert empty_decision.status_code == 200, empty_decision.text
        assert empty_decision.json()["schema"] == (
            "zhimeng.reader-review-decision-response.v1"
        )
        assert empty_decision.json()["receipt"] is None

        decision_payload = {
            "expected_reader_review_receipt_hash": reader_receipt["receipt_hash"],
            "finalize": True,
            "decisions": [
                {
                    "finding_id": item["finding_id"],
                    "decision": ("adopted" if index == 0 else "rejected"),
                }
                for index, item in enumerate(reader_receipt["findings"])
            ],
        }
        decision_response = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review-decision",
            json=decision_payload,
        )
        assert decision_response.status_code == 200, decision_response.text
        decided = decision_response.json()
        assert decided["schema"] == "zhimeng.reader-review-decision-response.v1"
        decision_receipt = decided["receipt"]
        assert decision_receipt["schema"] == (
            "zhimeng.reader-review-decision-receipt.v1"
        )
        assert decision_receipt["finalized"] is True
        assert decision_receipt["parent_reader_valid_for_new_candidate"] is False
        assert decision_receipt["formal_draft_allowed"] is False
        assert decision_receipt["quality_assessed"] is False
        assert decision_receipt["reader_review_receipt_hash"] == reader_receipt[
            "receipt_hash"
        ]
        assert len(decision_receipt["adopted_finding_ids"]) >= 1
        assert decision_receipt["follow_up_change_set_id"]
        follow_up = decided["follow_up_change_set"]
        assert follow_up is not None
        assert follow_up["change_set_id"] == decision_receipt["follow_up_change_set_id"]
        assert follow_up["status"] == "reviewing"
        assert follow_up["change_set_id"] != rolled["change_set_id"]
        assert any(
            change["relative_path"].endswith("reader_revision_notes.md")
            for change in follow_up["changes"]
        )

        parent_reader = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review"
        )
        assert parent_reader.status_code == 200, parent_reader.text
        assert parent_reader.json()["receipt"]["receipt_id"] == reader_receipt[
            "receipt_id"
        ]
        follow_up_reader = client.get(
            f"/api/v1/change-sets/{follow_up['change_set_id']}/reader-review"
        )
        assert follow_up_reader.status_code == 200, follow_up_reader.text
        assert follow_up_reader.json()["receipt"] is None

        decision_replay = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review-decision",
            json=decision_payload,
        )
        assert decision_replay.status_code == 200, decision_replay.text
        assert decision_replay.json()["receipt"]["receipt_id"] == decision_receipt[
            "receipt_id"
        ]
        assert decision_replay.json()["receipt"]["follow_up_change_set_id"] == (
            decision_receipt["follow_up_change_set_id"]
        )

        decision_stale = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/reader-review-decision",
            json={
                "expected_reader_review_receipt_hash": "0" * 64,
                "finalize": True,
                "decisions": decision_payload["decisions"],
            },
        )
        assert decision_stale.status_code == 409, decision_stale.text
        assert decision_stale.json()["error"]["code"] == "change_set_conflict"

        feedback = client.get(
            f"/api/v1/change-sets/{rolled['change_set_id']}/feedback-candidates"
        )
        assert feedback.status_code == 200, feedback.text
        feedback_body = feedback.json()
        assert feedback_body["schema"] == "zhimeng.feedback-candidate-response.v1"
        bundle = feedback_body["bundle"]
        assert bundle is not None
        assert bundle["schema"] == "zhimeng.feedback-candidate-bundle.v1"
        assert bundle["reader_review_decision_receipt_hash"] == decision_receipt[
            "receipt_hash"
        ]
        assert bundle["follow_up_change_set_id"] == decision_receipt[
            "follow_up_change_set_id"
        ]
        assert len(bundle["candidates"]) == len(reader_receipt["findings"])
        assert all(item["status"] == "candidate" for item in bundle["candidates"])
        assert all(
            item["promotion_status"] == "not_promoted" for item in bundle["candidates"]
        )
        assert all(
            item["formal_knowledge_contaminated"] is False
            for item in bundle["candidates"]
        )
        assert bundle["formal_draft_allowed"] is False
        assert len(feedback_body["history"]) == 1

        # Promote the first adopted candidate through FR-EVO controlled steps.
        target = next(
            item
            for item in bundle["candidates"]
            if item["candidate_id"]
            and item["source_decision"] == "adopted"
        )
        steps = [
            "verify_source",
            "check_conflicts",
            "evaluate_regression",
            "approve",
            "record_experience",
        ]
        current_hash = bundle["receipt_hash"]
        for action in steps:
            promo = client.post(
                f"/api/v1/change-sets/{rolled['change_set_id']}/feedback-candidate-promotion",
                json={
                    "expected_feedback_candidate_bundle_hash": current_hash,
                    "decisions": [
                        {"candidate_id": target["candidate_id"], "action": action}
                    ],
                },
            )
            assert promo.status_code == 200, promo.text
            promo_body = promo.json()
            assert promo_body["schema"] == (
                "zhimeng.feedback-candidate-promotion-response.v1"
            )
            assert promo_body["receipt"]["formal_knowledge_contaminated"] is False
            assert promo_body["receipt"]["formal_draft_allowed"] is False
            assert promo_body["bundle"] is not None
            current_hash = promo_body["bundle"]["receipt_hash"]
            updated = next(
                item
                for item in promo_body["bundle"]["candidates"]
                if item["candidate_id"] == target["candidate_id"]
            )
            if action == "verify_source":
                assert updated["promotion_status"] == "source_verified"
            elif action == "check_conflicts":
                assert updated["promotion_status"] == "conflict_checked"
            elif action == "evaluate_regression":
                assert updated["promotion_status"] == "regression_evaluated"
                assert updated["regression_result"] == "pass_with_watch"
            elif action == "approve":
                assert updated["promotion_status"] == "author_approved"
            elif action == "record_experience":
                assert updated["promotion_status"] == "experience_recorded"
                assert updated["status"] == "experience"
                assert updated["experience_relative_path"] == (
                    "07_运营数据/nlcl_feedback/experience_candidates.jsonl"
                )
                assert promo_body["receipt"]["experience_rows_appended"] == 1

        experience_file = (
            project
            / "07_运营数据"
            / "nlcl_feedback"
            / "experience_candidates.jsonl"
        )
        assert experience_file.is_file()
        experience_lines = [
            line
            for line in experience_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(experience_lines) == 1
        experience_row = json.loads(experience_lines[0])
        assert experience_row["bridge_schema"] == "zhimeng.feedback-experience-row.v1"
        assert experience_row["formal_knowledge_contaminated"] is False
        assert experience_row["formal_draft_allowed"] is False

        # Reject another candidate directly.
        other = next(
            item
            for item in promo_body["bundle"]["candidates"]
            if item["candidate_id"] != target["candidate_id"]
        )
        reject = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/feedback-candidate-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": current_hash,
                "decisions": [
                    {"candidate_id": other["candidate_id"], "action": "reject"}
                ],
            },
        )
        assert reject.status_code == 200, reject.text
        rejected = next(
            item
            for item in reject.json()["bundle"]["candidates"]
            if item["candidate_id"] == other["candidate_id"]
        )
        assert rejected["promotion_status"] == "rejected"
        assert rejected["status"] == "rejected"

        stale_promo = client.post(
            f"/api/v1/change-sets/{rolled['change_set_id']}/feedback-candidate-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": "0" * 64,
                "decisions": [
                    {"candidate_id": target["candidate_id"], "action": "verify_source"}
                ],
            },
        )
        assert stale_promo.status_code == 409, stale_promo.text
        assert stale_promo.json()["error"]["code"] == "change_set_conflict"

    artifact_root = tmp_path / "change-sets" / change_set["change_set_id"]
    assert (artifact_root / "writeback-manifest.json").is_file()
    assert (artifact_root / "rollback-manifest.json").is_file()
    assert (artifact_root / "writeback-evidence-receipt.json").is_file()
    assert (artifact_root / "writeback-evidence-history.json").is_file()
    assert (artifact_root / "nlcl-writeback-sync-receipt.json").is_file()
    assert (artifact_root / "nlcl-writeback-sync-history.json").is_file()
    assert (artifact_root / "nlcl-call-usage-continuity-receipt.json").is_file()
    assert (artifact_root / "nlcl-call-usage-continuity-history.json").is_file()
    assert (artifact_root / "nlcl-original-call-usage-export-receipt.json").is_file()
    assert (artifact_root / "nlcl-original-call-usage-export-history.json").is_file()
    assert (
        artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json"
    ).is_file()
    assert (
        artifact_root / "nlcl-original-chapter-ops-usage-export-history.json"
    ).is_file()
    assert (artifact_root / "reader-review-receipt.json").is_file()
    assert (artifact_root / "reader-review-history.json").is_file()
    assert (artifact_root / "reader-review-decision-receipt.json").is_file()
    assert (artifact_root / "reader-review-decision-history.json").is_file()
    assert (artifact_root / "feedback-candidate-bundle.json").is_file()
    assert (artifact_root / "feedback-candidate-history.json").is_file()
    assert (artifact_root / "feedback-candidate-promotion-receipt.json").is_file()
    assert (artifact_root / "feedback-candidate-promotion-history.json").is_file()

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
    assert paths["outline"].read_text(encoding="utf-8") == "old outline\n"


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
    assert paths["outline"].read_text(encoding="utf-8") == "old outline\n"


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
    assert paths["outline"].read_text(encoding="utf-8") == "old outline\n"

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
    assert paths["outline"].read_text(encoding="utf-8") == "old outline\n"


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
