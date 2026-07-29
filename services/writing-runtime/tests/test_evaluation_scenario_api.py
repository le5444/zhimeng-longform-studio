from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def test_evaluation_scenario_pack_list_get_and_bind(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        listed = client.get("/api/v1/evaluation-scenario-packs")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["schema"] == "zhimeng.evaluation-scenario-pack-list.v1"
        assert body["packs"], "expected local-v1 scenario pack from repo evaluation/"
        assert "quality_assessed" in body["not_claiming"]
        pack_id = body["packs"][0]["pack_id"]
        assert pack_id == "local-v1-longform-quality-scenarios"
        assert body["packs"][0]["quality_assessed"] is False

        got = client.get(f"/api/v1/evaluation-scenario-packs/{pack_id}")
        assert got.status_code == 200, got.text
        pack = got.json()["pack"]
        assert pack["quality_assessed"] is False
        assert pack["training_ready"] is False
        assert pack["formal_draft_allowed"] is False
        assert len(pack["scenarios"]) >= 4
        assert "LFQ-001" in {item["scenario_id"] for item in pack["scenarios"]}

        incomplete = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/suite-bindings",
            json={
                "suite_id": "eval-suite-" + ("a" * 32),
                "suite_receipt_hash": "b" * 64,
                "member_count": 2,
                "has_provider_fingerprint": False,
                "blind_review_finalized": False,
            },
        )
        assert incomplete.status_code == 201, incomplete.text
        binding = incomplete.json()["binding"]
        assert binding["all_ready"] is False
        assert binding["quality_assessed"] is False
        assert binding["formal_draft_allowed"] is False
        assert "quality_assessed" in incomplete.json()["not_claiming"]

        complete = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/suite-bindings",
            json={
                "suite_id": "eval-suite-" + ("c" * 32),
                "suite_receipt_hash": "d" * 64,
                "member_count": 2,
                "has_provider_fingerprint": True,
                "blind_review_finalized": True,
            },
        )
        assert complete.status_code == 201, complete.text
        complete_binding = complete.json()["binding"]
        # LFQ-004 does not require fingerprint, others do; with both true all should ready
        assert complete_binding["all_ready"] is True
        assert complete_binding["ready_count"] == len(pack["scenarios"])
        assert complete_binding["quality_assessed"] is False


def test_evaluation_scenario_longform_execution_matrix(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        pack_id = "local-v1-longform-quality-scenarios"
        incomplete = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions",
            json={
                "project_id": "project-" + ("a" * 24),
                "project_revision": 1,
                "project_fingerprint": "fp-longform-1",
                "available_chapters": ["CH001", "CH002"],
                "available_artifacts": ["gate_pipeline"],
                "member_count": 1,
                "has_provider_fingerprint": False,
                "blind_review_finalized": False,
                "external_blind_review_present": False,
            },
        )
        assert incomplete.status_code == 201, incomplete.text
        body = incomplete.json()
        assert body["schema"] == "zhimeng.evaluation-scenario-execution-response.v1"
        execution = body["execution"]
        assert execution["quality_assessed"] is False
        assert execution["formal_draft_allowed"] is False
        assert execution["all_executed"] is False
        assert execution["matrix_complete"] is False
        assert "literary_quality_score" in body["not_claiming"]
        lfq001 = next(item for item in execution["results"] if item["scenario_id"] == "LFQ-001")
        assert lfq001["evidence_complete"] is False
        assert "CH003" in lfq001["missing_chapters"]

        complete_inputs = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions",
            json={
                "project_id": "project-" + ("a" * 24),
                "project_revision": 2,
                "project_fingerprint": "fp-longform-2",
                "available_chapters": [
                    "CH001",
                    "CH002",
                    "CH003",
                    "CH010",
                    "CH011",
                    "CH012",
                ],
                "available_artifacts": [
                    "gate_pipeline",
                    "reader_review",
                    "writeback_evidence",
                    "literary_quality_gate",
                    "hard_fact_gate",
                    "reader_review_decision",
                    "feedback_candidate_bundle",
                    "eval_corpus_export",
                    "eval_corpus_baseline",
                    "eval_corpus_regression",
                    "skill_rule_evolution",
                ],
                "suite_id": "eval-suite-" + ("e" * 32),
                "suite_receipt_hash": "f" * 64,
                "member_count": 2,
                "has_provider_fingerprint": True,
                "blind_review_finalized": True,
                "external_blind_review_present": False,
            },
        )
        assert complete_inputs.status_code == 201, complete_inputs.text
        full = complete_inputs.json()["execution"]
        assert full["all_executed"] is True
        assert full["matrix_complete"] is False  # external blind matrix still missing
        assert full["quality_assessed"] is False
        assert full["executed_count"] == full["scenario_count"]
        assert all(item["scored"] is False for item in full["results"])
        assert all(item["quality_assessed"] is False for item in full["results"])

        latest = client.get(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions/latest",
            params={"project_id": "project-" + ("a" * 24)},
        )
        assert latest.status_code == 200, latest.text
        assert latest.json()["execution"]["execution_hash"] == full["execution_hash"]

