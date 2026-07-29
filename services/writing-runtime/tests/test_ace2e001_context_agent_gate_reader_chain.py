from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS

from test_context_agent_e2e import (
    StableKnowledgeAdapter,
    ToolCallingProvider,
    _start_tool_run,
    _wait_for_terminal,
)


def _write_longform_context_project(root: Path) -> Path:
    """Long-form multi-chapter project with an active CH001 rewrite surface."""
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    fixtures = {
        "01_项目定位/position.md": "A long mystery about a missing witness.",
        "02_世界观设定/world.md": "The old port closes after midnight.",
        "03_人物设定/characters.md": "Lin refuses to sacrifice a witness.",
        "04_剧情大纲/plot.md": "Find the witness and expose the old fire across continuous chapters.",
        "04_剧情大纲/CH001_控制卡.md": (
            "Goal: recover the witness. Cost: expose the investigator."
        ),
        "04_剧情大纲/CH002_控制卡.md": (
            "Goal: keep the key hidden while entering the sealed port."
        ),
        "04_剧情大纲/CH003_控制卡.md": (
            "Goal: confirm the witness left a live trail without revealing the mastermind."
        ),
        "06_写作规范/style.md": "Keep dialogue restrained and concrete.",
        "07_运营数据/status.md": "Serialization is active; last complete CH002; active CH003.",
    }
    for relative_path, text in fixtures.items():
        target = root / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    chapter_path = volume / "CH001_正文.md"
    chapter_path.write_text(
        "Lin finds a blood-stained key at the old port.",
        encoding="utf-8",
    )
    (volume / "CH002_正文.md").write_text(
        "Chapter two. The sealed port only keeps one cold white light.\n"
        "Lin hides half the key and hears chains drag behind him.\n",
        encoding="utf-8",
    )
    (volume / "CH003_正文.md").write_text(
        "Chapter three. The bloody key remains in the drawer.\n"
        "Lin has not decided whether to open the door.\n",
        encoding="utf-8",
    )
    return chapter_path


def test_ace2e001_context_agent_gate_reader_apply_rollback(tmp_path: Path) -> None:
    """AC-E2E-001 same-chain black box:

    long-form multi-chapter bind
    -> Context Pack
    -> Agent WriterTask/ChangeSet (author_review)
    -> ordered gate-pipeline
    -> formal-draft denied
    -> post-draft apply
    -> Reader cold-read/decision/feedback
    -> rollback

    Keeps formal_draft_allowed=false and does not claim four_skills_generic_exactly_once.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-novel"
    chapter_path = _write_longform_context_project(project)
    original = chapter_path.read_text(encoding="utf-8")
    provider = ToolCallingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        project_id = snapshot["project"]["project_id"]
        chapter_ids = {
            chapter_id
            for volume in snapshot["volumes"]
            for chapter_id in volume.get("chapter_ids", [])
        }
        assert {"VOL001-CH001", "VOL001-CH002", "VOL001-CH003"} <= chapter_ids

        objective = "Tighten the opening without losing the blood-stained key."
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective=objective,
            key_prefix="ace2e001-context-agent",
            writer_mode="author_review_change_proposal",
        )
        runtime_propose_tool_id = app.state.agent_service._runtime_tool_call_id(
            run_id,
            "call_propose_change_0003",
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal
        assert pack["context_pack_id"]
        assert pack["pack_hash"]
        assert pack["active_chapter_id"] == "VOL001-CH001"

        writer_task = client.get(f"/api/v1/agent/runs/{run_id}/writer-task").json()
        assert writer_task["mode"] == "author_review_change_proposal"
        assert writer_task["formal_draft_allowed"] is False
        assert writer_task["writes_project_truth"] is False
        assert writer_task["context_pack_id"] == pack["context_pack_id"]

        writer_result = client.get(
            f"/api/v1/agent/runs/{run_id}/writer-candidate-result"
        ).json()
        assert writer_result["writer_task_id"] == writer_task["writer_task_id"]
        assert writer_result["candidate_kind"] == "change_proposal"
        assert writer_result["formal_draft_allowed"] is False
        assert writer_result["writes_project_truth"] is False

        activities = client.get(f"/api/v1/agent/runs/{run_id}/activities").json()[
            "activities"
        ]
        proposals = [item for item in activities if item["kind"] == "change_proposal"]
        assert len(proposals) == 1
        change_set_id = proposals[0]["payload"]["proposal_id"]
        assert chapter_path.read_text(encoding="utf-8") == original

        change_set = client.get(f"/api/v1/change-sets/{change_set_id}").json()
        assert change_set["origin"] == "agent"
        assert change_set["source_run_id"] == run_id
        assert change_set["source_tool_call_id"] == runtime_propose_tool_id
        assert change_set["gate_required"] is True
        assert change_set["status"] == "reviewing"

        hunks = [
            hunk for change in change_set["changes"] for hunk in change["hunks"]
        ]
        ready = client.patch(
            f"/api/v1/change-sets/{change_set_id}/decisions",
            json={
                "expected_review_revision": change_set["review_revision"],
                "decisions": [
                    {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                    for hunk in hunks
                ],
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
        rewritten = chapter_path.read_text(encoding="utf-8")
        assert "before the old port closes" in rewritten
        assert rewritten != original

        evidence = client.get(f"/api/v1/change-sets/{change_set_id}/writeback-evidence")
        assert evidence.status_code == 200, evidence.text
        evidence_receipt = evidence.json()["receipt"]
        assert evidence_receipt["formal_draft_allowed"] is False
        assert evidence_receipt["formal_draft_authorization_receipt_id"]
        assert evidence_receipt["source_run_id"] == run_id

        reader_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/reader-review",
            json={
                "expected_result_fingerprint": applied_body["writeback"][
                    "result_fingerprint"
                ]
            },
        )
        assert reader_response.status_code == 200, reader_response.text
        reader_receipt = reader_response.json()["receipt"]
        assert reader_receipt["outcome"] == "applied"
        assert reader_receipt["evaluator_kind"] == "deterministic_cold_read_v1"
        assert reader_receipt["overrides_gate"] is False
        assert reader_receipt["formal_draft_allowed"] is False
        assert reader_receipt["quality_assessed"] is False
        assert len(reader_receipt["findings"]) >= 1

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
        decision_receipt = decision_response.json()["receipt"]
        assert decision_receipt["finalized"] is True
        assert decision_receipt["formal_draft_allowed"] is False
        assert decision_receipt["follow_up_change_set_id"]

        feedback = client.get(f"/api/v1/change-sets/{change_set_id}/feedback-candidates")
        assert feedback.status_code == 200, feedback.text
        feedback_bundle = feedback.json()["bundle"]
        assert feedback_bundle is not None
        assert feedback_bundle["reader_review_decision_receipt_hash"] == decision_receipt[
            "receipt_hash"
        ]

        rolled = client.post(
            f"/api/v1/change-sets/{change_set_id}/rollback",
            json={"expected_review_revision": applied_body["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["status"] == "rolled_back"
        assert chapter_path.read_text(encoding="utf-8") == original

        recovered_reader = client.get(
            f"/api/v1/change-sets/{change_set_id}/reader-review"
        )
        assert recovered_reader.status_code == 200, recovered_reader.text
        assert recovered_reader.json()["receipt"]["receipt_id"] == reader_receipt[
            "receipt_id"
        ]

    with app.state.runtime.store.connection() as connection:
        provenance = connection.execute(
            "SELECT * FROM change_set_provenance WHERE change_set_id = ?",
            (change_set_id,),
        ).fetchone()
    assert provenance is not None
    assert str(provenance["run_id"]) == run_id
    assert str(provenance["tool_call_id"]) == runtime_propose_tool_id
    assert str(provenance["context_pack_id"]) == pack["context_pack_id"]
    assert str(provenance["project_fingerprint"]) == pack["project_fingerprint"]
