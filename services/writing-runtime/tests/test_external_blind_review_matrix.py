from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.external_blind_review_matrix import REQUIRED_CELLS


def _full_matrix_evidence(suite_id: str = "suite-ace2e004-demo") -> dict:
    return {
        "schema": "zhimeng.external-blind-review-matrix-evidence.v1",
        "suite_id": suite_id,
        "evidence_id": "evidence-external-blind-demo-1",
        "cells": [
            {
                "reviewer_id": reviewer,
                "sample_id": sample,
                "status": "passed",
                "score": 78,
                "notes": "external blind review cell",
                "evidence_ref": f"ref:{reviewer}:{sample}",
            }
            for reviewer, sample in REQUIRED_CELLS
        ],
    }


def test_external_blind_review_matrix_fail_closed_and_ready(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(tmp_path,),
                allowed_origins=(),
            )
        )
    ) as client:
        empty = client.post("/api/v1/external-blind-review-matrix/evaluate", json={})
        assert empty.status_code == 200, empty.text
        empty_body = empty.json()
        assert empty_body["schema"] == "zhimeng.external-blind-review-matrix-response.v1"
        assert empty_body["receipt"]["matrix_ready"] is False
        assert empty_body["receipt"]["quality_assessed"] is False
        assert empty_body["receipt"]["formal_draft_allowed"] is False
        assert empty_body["receipt"]["llm_literary_score_claim_allowed"] is False
        assert empty_body["receipt"]["local_v1_exe_claim_allowed"] is False
        assert empty_body["receipt"]["passed_cell_count"] == 0
        assert len(empty_body["receipt"]["missing_required"]) == 6
        assert "external_blind_review_matrix" in empty_body["not_claiming"]
        assert "quality_assessed" in empty_body["not_claiming"]

        partial = client.post(
            "/api/v1/external-blind-review-matrix/evaluate",
            json={
                "suite_id": "suite-ace2e004-demo",
                "evidence": {
                    "schema": "zhimeng.external-blind-review-matrix-evidence.v1",
                    "suite_id": "suite-ace2e004-demo",
                    "cells": [
                        {
                            "reviewer_id": "external_reviewer_1",
                            "sample_id": "chapter_sample_a",
                            "status": "passed",
                        }
                    ],
                },
            },
        )
        assert partial.status_code == 200, partial.text
        partial_body = partial.json()
        assert partial_body["receipt"]["matrix_ready"] is False
        assert partial_body["receipt"]["passed_cell_count"] == 1
        assert "external_blind_review_matrix" in partial_body["not_claiming"]

        receipt_path = tmp_path / "external-blind-review-matrix-receipt.json"
        full = client.post(
            "/api/v1/external-blind-review-matrix/evaluate",
            json={
                "suite_id": "suite-ace2e004-demo",
                "evidence": _full_matrix_evidence(),
                "write_receipt_to": str(receipt_path),
            },
        )
        assert full.status_code == 200, full.text
        full_body = full.json()
        assert full_body["receipt"]["matrix_ready"] is True
        assert full_body["receipt"]["passed_cell_count"] == 6
        assert full_body["receipt"]["missing_required"] == []
        assert full_body["receipt"]["quality_assessed"] is False
        assert full_body["receipt"]["formal_draft_allowed"] is False
        assert "external_blind_review_matrix" not in full_body["not_claiming"]
        assert "quality_assessed" in full_body["not_claiming"]
        assert "llm_literary_score" in full_body["not_claiming"]
        assert "local_v1_exe" in full_body["not_claiming"]
        assert receipt_path.is_file()

        mismatch = client.post(
            "/api/v1/external-blind-review-matrix/evaluate",
            json={
                "suite_id": "suite-other",
                "evidence": _full_matrix_evidence("suite-ace2e004-demo"),
            },
        )
        assert mismatch.status_code == 200, mismatch.text
        mismatch_body = mismatch.json()
        assert mismatch_body["receipt"]["matrix_ready"] is False
        assert "suite_id mismatch" in mismatch_body["receipt"]["detail"]
