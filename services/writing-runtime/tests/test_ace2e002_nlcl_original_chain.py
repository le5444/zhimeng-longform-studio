from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


def _write_longform_project(root: Path) -> Path:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    (root / "02_世界观设定" / "规则.md").write_text(
        "# 信息边界\n读者知道旧港白灯，角色尚未确认。\n",
        encoding="utf-8",
    )
    (root / "03_人物设定" / "主角.md").write_text(
        "# 林澈\n从匿名信追到旧港，当前仍不掌握白灯真相。\n\n- 持有焦痕信纸\n",
        encoding="utf-8",
    )
    (root / "03_人物设定" / "人物.json").write_text(
        '{"主角":"林澈","证人":"阿宁"}',
        encoding="utf-8",
    )
    (root / "04_剧情大纲" / "主线.md").write_text(
        "# 主线\n林澈从匿名信追到旧港。\n",
        encoding="utf-8",
    )
    (root / "04_剧情大纲" / "伏笔清单.md").write_text(
        "# 伏笔：旧港白灯\n旧港白灯尚未点破，不能提前揭穿。\n",
        encoding="utf-8",
    )
    (root / "07_运营数据" / "project_status.md").write_text(
        "连载中；活动章 CH001。\n",
        encoding="utf-8",
    )
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    chapter = volume / "CH001_正文.md"
    chapter.write_text(
        "第一章。\n雨落在旧站铁皮顶上。林澈拆开匿名信。\n",
        encoding="utf-8",
    )
    return chapter


def test_ace2e002_nlcl_original_chain_after_apply(tmp_path: Path) -> None:
    """AC-E2E-002 residual: long-form apply -> NLCL original-compatible chain.

    Covers product bridges for writeback evidence, call_usage continuity,
    original call_usage export, chapter/ops usage export, and novel-state
    inventory bridge, original state sync, truth-content extract, mandatory usage plan/closure, operation_package_like, library layout inventory/completeness, bounded activation, and prewrite
    context report. Formal draft remains locked; quality_assessed stays false.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-nlcl"
    chapter_path = _write_longform_project(project)
    after = (
        "第一章。\n"
        "雨落在旧站铁皮顶上。林澈拆开匿名信。\n"
        "他把焦痕信纸按在掌心，确认纸边温度还没散尽。\n"
    )

    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(library,),
                allowed_origins=(),
            )
        )
    ) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        snapshot = inspected.json()
        project_id = snapshot["project"]["project_id"]
        chapter = next(
            item
            for item in snapshot["files"]
            if item["relative_path"].endswith("CH001_正文.md")
        )
        created = client.post(
            f"/api/v1/projects/{project_id}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Longform NLCL chain",
                "summary": "Exercise original-compatible NLCL bridges after apply.",
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": after,
                        "rationale": "Advance chapter one with visible cost.",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        change_set = created.json()
        hunk_count = len(change_set["changes"][0]["hunks"])
        ready = client.patch(
            f"/api/v1/change-sets/{change_set['change_set_id']}/decisions",
            json={
                "expected_review_revision": change_set["review_revision"],
                "decisions": [
                    {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                    for hunk in change_set["changes"][0]["hunks"]
                ],
            },
        )
        assert ready.status_code == 200, ready.text
        ready_body = ready.json()
        applied = client.post(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/apply",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        change_set_id = applied_body["change_set_id"]
        assert chapter_path.read_text(encoding="utf-8") == after

        evidence = client.get(f"/api/v1/change-sets/{change_set_id}/writeback-evidence")
        assert evidence.status_code == 200, evidence.text
        evidence_receipt = evidence.json()["receipt"]
        assert evidence_receipt["outcome"] == "applied"
        assert evidence_receipt["novel_state_synced"] is False

        sync = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-writeback-sync",
            json={
                "expected_writeback_evidence_receipt_hash": evidence_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert sync.status_code == 200, sync.text
        sync_receipt = sync.json()["receipt"]
        assert sync_receipt["original_nlcl_chapter_ops_usage_synced"] is False

        usage = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-call-usage-continuity",
            json={
                "expected_nlcl_writeback_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert usage.status_code == 200, usage.text
        usage_receipt = usage.json()["receipt"]
        assert usage_receipt["original_nlcl_call_usage_recorded"] is False

        call_usage = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-call-usage-export",
            json={
                "expected_nlcl_call_usage_continuity_receipt_hash": usage_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert call_usage.status_code == 200, call_usage.text
        call_usage_receipt = call_usage.json()["receipt"]
        assert call_usage_receipt["original_nlcl_call_usage_recorded"] is True
        assert call_usage_receipt["original_nlcl_chapter_ops_usage_synced"] is False

        chapter_ops = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-chapter-ops-usage-export",
            json={
                "expected_nlcl_original_call_usage_export_receipt_hash": call_usage_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert chapter_ops.status_code == 200, chapter_ops.text
        chapter_ops_receipt = chapter_ops.json()["receipt"]
        assert chapter_ops_receipt["original_nlcl_chapter_ops_usage_synced"] is True
        assert chapter_ops_receipt["novel_state_synced"] is False

        novel_state = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-bridge",
            json={
                "expected_nlcl_original_chapter_ops_usage_export_receipt_hash": chapter_ops_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert novel_state.status_code == 200, novel_state.text
        novel_state_receipt = novel_state.json()["receipt"]
        assert novel_state_receipt["original_nlcl_novel_state_bridge_recorded"] is True
        assert novel_state_receipt["novel_state_synced"] is False
        assert novel_state_receipt["formal_draft_allowed"] is False
        assert novel_state_receipt["quality_assessed"] is False
        total_refs = (
            len(novel_state_receipt["character_state_refs"])
            + len(novel_state_receipt["plot_state_refs"])
            + len(novel_state_receipt["foreshadowing_state_refs"])
            + len(novel_state_receipt["project_status_refs"])
        )
        assert total_refs >= 3
        assert hunk_count >= 1

        bridge_file = (
            project / "07_运营数据" / "nlcl_feedback" / "novel_state_bridge.jsonl"
        )
        assert bridge_file.is_file()
        lines = [line for line in bridge_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 1

        state_sync = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-novel-state-sync",
            json={
                "expected_nlcl_original_novel_state_bridge_receipt_hash": novel_state_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert state_sync.status_code == 200, state_sync.text
        sync_receipt = state_sync.json()["receipt"]
        assert sync_receipt["novel_state_synced"] is True
        assert sync_receipt["original_nlcl_novel_state_synced"] is True
        assert sync_receipt["original_nlcl_truth_matrix_synced"] is True
        assert sync_receipt["formal_draft_allowed"] is False
        assert len(sync_receipt["written_files"]) == 7
        for rel in (
            "state/project_status.json",
            "state/character_state.json",
            "state/plot_state.json",
            "state/foreshadowing_state.json",
            "state/truth_character_matrix.json",
            "state/truth_foreshadow_lifecycle.json",
            "state/truth_info_boundary.json",
        ):
            assert (project / Path(rel)).is_file()

        prewrite = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-prewrite-context-report",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert prewrite.status_code == 200, prewrite.text
        prewrite_receipt = prewrite.json()["receipt"]
        assert prewrite_receipt["prewrite_context_report_synced"] is True
        assert prewrite_receipt["formal_draft_allowed"] is False
        assert (
            project
            / "chapter_context"
            / f"{prewrite_receipt['chapter_id']}_prewrite_context_report_latest.json"
        ).is_file()
        assert (
            project
            / "chapter_context"
            / f"{prewrite_receipt['chapter_id']}_prewrite_context_report_latest.md"
        ).is_file()

        extract = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-truth-content-extract",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert extract.status_code == 200, extract.text
        extract_receipt = extract.json()["receipt"]
        assert extract_receipt["truth_content_extracted"] is True
        assert extract_receipt["formal_draft_allowed"] is False
        assert extract_receipt["quality_assessed"] is False
        assert extract_receipt["extracted_character_count"] >= 1
        assert extract_receipt["extracted_foreshadow_count"] >= 1
        assert len(extract_receipt["written_files"]) == 6
        character_state = json.loads(
            (project / "state" / "character_state.json").read_text(encoding="utf-8")
        )
        assert any(
            "林澈" in str(item.get("name", ""))
            for item in character_state.get("characters", {}).values()
        )
        foreshadow_state = json.loads(
            (project / "state" / "truth_foreshadow_lifecycle.json").read_text(
                encoding="utf-8"
            )
        )
        assert any(
            "伏笔" in str(item.get("title", ""))
            for item in foreshadow_state.get("open_debts", [])
        )

        plan = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-plan",
            json={
                "expected_nlcl_original_truth_content_extract_receipt_hash": extract_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert plan.status_code == 200, plan.text
        plan_receipt = plan.json()["receipt"]
        assert plan_receipt["mandatory_usage_plan_synced"] is True
        assert plan_receipt["mandatory_usage_closure_closed"] is False
        assert plan_receipt["formal_draft_allowed"] is False
        assert len(plan_receipt["items"]) >= 1
        assert (
            project
            / "chapter_context"
            / f"{plan_receipt['chapter_id']}_mandatory_usage_plan_latest.json"
        ).is_file()

        closure = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-mandatory-usage-closure",
            json={
                "expected_nlcl_original_mandatory_usage_plan_receipt_hash": plan_receipt[
                    "receipt_hash"
                ],
                "decisions": [
                    {
                        "item_id": item["item_id"],
                        "status": "used",
                        "note": "longform ACE2E author decision",
                    }
                    for item in plan_receipt["items"]
                ],
                "finalize": True,
            },
        )
        assert closure.status_code == 200, closure.text
        closure_receipt = closure.json()["receipt"]
        assert closure_receipt["mandatory_usage_closure_closed"] is True
        assert closure_receipt["route_validated"] is False
        assert closure_receipt["formal_draft_allowed"] is False
        assert (
            project
            / "chapter_context"
            / f"{closure_receipt['chapter_id']}_mandatory_usage_closure_latest.json"
        ).is_file()

        package = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-operation-package-like",
            json={
                "expected_nlcl_original_mandatory_usage_closure_receipt_hash": closure_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert package.status_code == 200, package.text
        package_receipt = package.json()["receipt"]
        assert package_receipt["operation_package_like_synced"] is True
        assert package_receipt["readiness"] == "risk_draft_only"
        assert package_receipt["formal_draft_allowed"] is False
        assert (
            project
            / "chapter_context"
            / f"{package_receipt['chapter_id']}_operation_package_like_latest.json"
        ).is_file()

        layout = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-inventory",
            json={
                "expected_nlcl_original_operation_package_like_receipt_hash": package_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert layout.status_code == 200, layout.text
        layout_receipt = layout.json()["receipt"]
        assert layout_receipt["library_layout_inventoried"] is True
        assert layout_receipt["library_layout_complete"] is False
        assert layout_receipt["whole_library_load_forbidden"] is True
        assert (
            project
            / "chapter_context"
            / f"{layout_receipt['chapter_id']}_library_layout_inventory_latest.json"
        ).is_file()

        completeness = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-library-layout-completeness",
            json={
                "expected_nlcl_original_library_layout_inventory_receipt_hash": layout_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert completeness.status_code == 200, completeness.text
        completeness_receipt = completeness.json()["receipt"]
        assert completeness_receipt["library_layout_assessed"] is True
        assert completeness_receipt["whole_library_load_forbidden"] is True
        assert completeness_receipt["formal_draft_allowed"] is False
        assert completeness_receipt["library_layout_complete"] is (
            completeness_receipt["missing_required_slot_count"] == 0
        )
        assert (
            project
            / "chapter_context"
            / f"{completeness_receipt['chapter_id']}_library_layout_completeness_latest.json"
        ).is_file()

        activation = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-bounded-library-activation",
            json={
                "expected_nlcl_original_library_layout_completeness_receipt_hash": completeness_receipt[
                    "receipt_hash"
                ],
                "max_sources": 8,
            },
        )
        assert activation.status_code == 200, activation.text
        activation_receipt = activation.json()["receipt"]
        assert activation_receipt["bounded_library_activated"] is True
        assert activation_receipt["whole_library_load"] is False
        assert activation_receipt["whole_library_load_forbidden"] is True
        assert activation_receipt["formal_draft_allowed"] is False
        assert (
            project
            / "chapter_context"
            / f"{activation_receipt['chapter_id']}_bounded_library_activation_latest.json"
        ).is_file()

        prewrite_bound = client.post(
            f"/api/v1/change-sets/{change_set_id}/nlcl-original-prewrite-context-report",
            json={
                "expected_nlcl_original_novel_state_sync_receipt_hash": sync_receipt[
                    "receipt_hash"
                ]
            },
        )
        assert prewrite_bound.status_code == 200, prewrite_bound.text
        bound_receipt = prewrite_bound.json()["receipt"]
        assert bound_receipt["bounded_library_activated"] is True
        assert bound_receipt["activated_source_count"] == activation_receipt["activated_count"]
        assert (
            bound_receipt["nlcl_original_bounded_library_activation_receipt_hash"]
            == activation_receipt["receipt_hash"]
        )
