from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


def _write_longform_project(root: Path) -> dict[str, Path]:
    """Multi-chapter long-form fixture for AC-E2E-001 black-box evidence."""
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()

    core_files = {
        "01_项目定位/定位.md": "悬疑长篇。主承诺：证人还活着，但每章都要付可见代价。",
        "02_世界观设定/旧港.md": "旧港夜间封港；雨夜灯塔只亮一盏。",
        "03_人物设定/人物.json": (
            '{"主角":"林澈","证人":"阿宁","底线":"不拿证人换自己的安全"}'
        ),
        "04_剧情大纲/主线.md": "主线：林澈从匿名信追到旧港，逐步逼近证人下落。",
        "04_剧情大纲/CH003_控制卡.md": (
            "目标：确认钥匙归属。\n"
            "压力：封港前只剩两小时。\n"
            "禁止：提前揭穿幕后主使。\n"
        ),
        "06_写作规范/风格.md": "事实先于抒情；对话短；代价要落到具体物件。",
        "07_运营数据/project_status.md": "连载中；上一完成章 CH002；活动章 CH003。",
    }
    for relative_path, content in core_files.items():
        target = root / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    chapters = {
        "CH001_正文.md": (
            "第一章。\n"
            "雨落在旧站铁皮顶上。林澈拆开匿名信，信纸边角烧出一截焦痕。\n"
            "信里只有一句：证人还活着，但钥匙不在你手里。\n"
        ),
        "CH002_正文.md": (
            "第二章。\n"
            "旧港封港前一小时，灯塔只剩一盏冷白灯。\n"
            "林澈把半截钥匙塞进内袋，听见身后铁链拖地。\n"
        ),
        "CH003_正文.md": (
            "第三章。\n"
            "带血钥匙还在抽屉里。林澈还没决定要不要推开那扇门。\n"
        ),
    }
    paths: dict[str, Path] = {}
    for name, content in chapters.items():
        path = volume / name
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    return paths


def test_ace2e001_longform_bind_gate_reader_apply_rollback(tmp_path: Path) -> None:
    """AC-E2E-001 residual black-box:

    long-form multi-chapter project
    -> bind/inspect/read
    -> multi-file ChangeSet (continuous CH002/CH003)
    -> ordered gate-pipeline (literary + hard-fact + post-draft status)
    -> formal-draft authorization stays denied
    -> post-draft required apply
    -> Reader cold-read + author decision + feedback candidates
    -> rollback restores project truth

    Product note: deterministic Reader is bound to writeback result fingerprint,
    so it runs after apply; Gate pipeline is evaluated before and on apply.
    Does not claim four_skills_generic_exactly_once or formal_draft unlock.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-book"
    chapter_paths = _write_longform_project(project)
    original_ch2 = chapter_paths["CH002_正文.md"].read_bytes()
    original_ch3 = chapter_paths["CH003_正文.md"].read_bytes()

    ch2_after = (
        "第二章。\n"
        "旧港封港前一小时，灯塔只剩一盏冷白灯。\n"
        "林澈把半截钥匙塞进内袋，听见身后铁链拖地。\n"
        "他把焦痕信纸按在掌心，确认纸边温度还没散尽。\n"
    )
    ch3_after = (
        "第三章。\n"
        "带血钥匙还在抽屉里。林澈推开半扇门，雨气先一步撞进来。\n"
        "门后只有空椅和一枚被掐灭的烟头，证人刚离开不久。\n"
        "他把钥匙收进掌心，知道下一程必须自己付代价。\n"
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
        project_meta = snapshot["project"]
        project_id = project_meta["project_id"]
        chapter_ids = {
            chapter_id
            for volume in snapshot["volumes"]
            for chapter_id in volume.get("chapter_ids", [])
        }
        if not chapter_ids:
            chapter_ids = {
                item["chapter_id"]
                for item in snapshot.get("chapters", [])
                if item.get("chapter_id")
            }
        assert "VOL001-CH001" in chapter_ids
        assert "VOL001-CH002" in chapter_ids
        assert "VOL001-CH003" in chapter_ids

        files_by_name = {
            Path(item["relative_path"]).name: item for item in snapshot["files"]
        }
        ch2 = files_by_name["CH002_正文.md"]
        ch3 = files_by_name["CH003_正文.md"]

        read_ch2 = client.get(f"/api/v1/projects/{project_id}/files/{ch2['file_id']}")
        assert read_ch2.status_code == 200, read_ch2.text
        assert read_ch2.json()["content"].encode("utf-8") == original_ch2

        created = client.post(
            f"/api/v1/projects/{project_id}/change-sets",
            json={
                "title": "AC-E2E-001 long-form continuous rewrite",
                "summary": (
                    "Continuous long-form ChangeSet covering CH002 pressure carry "
                    "and CH003 cost reveal under ordered Gate/Reader evidence."
                ),
                "base_revision": project_meta["revision"],
                "base_fingerprint": project_meta["fingerprint"],
                "origin": "manual",
                "gate_required": True,
                "changes": [
                    {
                        "operation": "update",
                        "file_id": ch2["file_id"],
                        "after_content": ch2_after,
                        "rationale": "Carry CH002 pressure object into the continuous loop.",
                    },
                    {
                        "operation": "update",
                        "file_id": ch3["file_id"],
                        "after_content": ch3_after,
                        "rationale": "Advance CH003 with a concrete cost and no formal unlock.",
                    },
                ],
            },
        )
        assert created.status_code == 201, created.text
        change_set = created.json()
        change_set_id = change_set["change_set_id"]
        assert change_set["status"] == "reviewing"
        assert len(change_set["changes"]) == 2

        decisions = [
            {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
            for change in change_set["changes"]
            for hunk in change["hunks"]
        ]
        ready = client.patch(
            f"/api/v1/change-sets/{change_set_id}/decisions",
            json={
                "expected_review_revision": change_set["review_revision"],
                "decisions": decisions,
            },
        )
        assert ready.status_code == 200, ready.text
        ready_body = ready.json()
        assert ready_body["status"] == "ready"

        pipeline = client.post(
            f"/api/v1/change-sets/{change_set_id}/gate-pipeline/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert pipeline.status_code == 200, pipeline.text
        pipeline_receipt = pipeline.json()["receipt"]
        assert pipeline_receipt["status"] == "complete"
        assert pipeline_receipt["authorization_input_ready"] is True
        assert pipeline_receipt["formal_draft_allowed"] is False
        assert pipeline_receipt["quality_assessed"] is False
        assert pipeline_receipt["authorization_unlock"] is False
        step_codes = [item["code"] for item in pipeline_receipt["steps"]]
        assert "literary_quality_gate" in step_codes
        assert "hard_fact_gate" in step_codes
        assert pipeline_receipt["literary_quality_receipt_id"]
        assert pipeline_receipt["hard_fact_receipt_id"]
        literary_step = next(
            item
            for item in pipeline_receipt["steps"]
            if item["code"] == "literary_quality_gate"
        )
        assert literary_step["present"] is True

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": change_set_id},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
        assert formal_receipt["writes_project_truth"] is False
        pipeline_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "gate_pipeline_complete"
        )
        assert pipeline_prereq["satisfied"] is True
        literary_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "literary_quality_gate"
        )
        assert literary_prereq["satisfied"] is False

        gated = client.post(
            f"/api/v1/change-sets/{change_set_id}/post-draft-gate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "passed"

        applied = client.post(
            f"/api/v1/change-sets/{change_set_id}/apply",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        assert applied_body["status"] == "applied"
        assert applied_body["writeback"]["result_fingerprint"]
        assert chapter_paths["CH002_正文.md"].read_text(encoding="utf-8") == ch2_after
        assert chapter_paths["CH003_正文.md"].read_text(encoding="utf-8") == ch3_after

        evidence = client.get(f"/api/v1/change-sets/{change_set_id}/writeback-evidence")
        assert evidence.status_code == 200, evidence.text
        evidence_receipt = evidence.json()["receipt"]
        assert evidence_receipt["formal_draft_allowed"] is False
        assert evidence_receipt["formal_draft_authorization_receipt_id"]
        assert evidence_receipt["formal_draft_authorization_receipt_hash"]

        pipeline_after_apply = client.get(
            f"/api/v1/change-sets/{change_set_id}/gate-pipeline"
        )
        assert pipeline_after_apply.status_code == 200, pipeline_after_apply.text
        assert pipeline_after_apply.json()["receipt"]["status"] == "complete"
        assert pipeline_after_apply.json()["receipt"]["formal_draft_allowed"] is False

        reader_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/reader-review",
            json={
                "expected_result_fingerprint": applied_body["writeback"][
                    "result_fingerprint"
                ]
            },
        )
        assert reader_response.status_code == 200, reader_response.text
        reader_body = reader_response.json()
        assert reader_body["schema"] == "zhimeng.reader-review-response.v1"
        reader_receipt = reader_body["receipt"]
        assert reader_receipt["outcome"] == "applied"
        assert reader_receipt["evaluator_kind"] == "deterministic_cold_read_v1"
        assert reader_receipt["overrides_gate"] is False
        assert reader_receipt["formal_draft_allowed"] is False
        assert reader_receipt["quality_assessed"] is False
        assert reader_receipt["result_fingerprint"] == applied_body["writeback"][
            "result_fingerprint"
        ]
        assert len(reader_receipt["findings"]) >= 1
        assert set(reader_receipt["candidate_relative_paths"]) >= {
            "05_正文内容/第一卷/CH002_正文.md",
            "05_正文内容/第一卷/CH003_正文.md",
        }

        decision_payload = {
            "expected_reader_review_receipt_hash": reader_receipt["receipt_hash"],
            "finalize": True,
            "decisions": [
                {
                    "finding_id": item["finding_id"],
                    "decision": "adopted" if index == 0 else "rejected",
                }
                for index, item in enumerate(reader_receipt["findings"])
            ],
        }
        decision_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/reader-review-decision",
            json=decision_payload,
        )
        assert decision_response.status_code == 200, decision_response.text
        decision_body = decision_response.json()
        decision_receipt = decision_body["receipt"]
        assert decision_receipt["finalized"] is True
        assert decision_receipt["formal_draft_allowed"] is False
        assert decision_receipt["quality_assessed"] is False
        assert decision_receipt["reader_review_receipt_hash"] == reader_receipt[
            "receipt_hash"
        ]
        assert decision_receipt["follow_up_change_set_id"]
        follow_up = decision_body["follow_up_change_set"]
        assert follow_up is not None
        assert follow_up["change_set_id"] != change_set_id
        assert follow_up["status"] == "reviewing"

        feedback = client.get(f"/api/v1/change-sets/{change_set_id}/feedback-candidates")
        assert feedback.status_code == 200, feedback.text
        feedback_bundle = feedback.json()["bundle"]
        assert feedback_bundle is not None
        assert feedback_bundle["reader_review_decision_receipt_hash"] == decision_receipt[
            "receipt_hash"
        ]
        assert feedback_bundle["follow_up_change_set_id"] == decision_receipt[
            "follow_up_change_set_id"
        ]
        assert len(feedback_bundle["candidates"]) == len(reader_receipt["findings"])

        rolled = client.post(
            f"/api/v1/change-sets/{change_set_id}/rollback",
            json={"expected_review_revision": applied_body["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        rolled_body = rolled.json()
        assert rolled_body["status"] == "rolled_back"
        assert chapter_paths["CH002_正文.md"].read_bytes() == original_ch2
        assert chapter_paths["CH003_正文.md"].read_bytes() == original_ch3

        rolled_evidence = client.get(
            f"/api/v1/change-sets/{change_set_id}/writeback-evidence"
        )
        assert rolled_evidence.status_code == 200, rolled_evidence.text
        assert rolled_evidence.json()["receipt"]["formal_draft_allowed"] is False
        assert rolled_evidence.json()["receipt"][
            "formal_draft_authorization_receipt_id"
        ]

        recovered_reader = client.get(
            f"/api/v1/change-sets/{change_set_id}/reader-review"
        )
        assert recovered_reader.status_code == 200, recovered_reader.text
        assert recovered_reader.json()["receipt"]["receipt_id"] == reader_receipt[
            "receipt_id"
        ]
        recovered_decision = client.get(
            f"/api/v1/change-sets/{change_set_id}/reader-review-decision"
        )
        assert recovered_decision.status_code == 200, recovered_decision.text
        assert recovered_decision.json()["receipt"]["receipt_id"] == decision_receipt[
            "receipt_id"
        ]
