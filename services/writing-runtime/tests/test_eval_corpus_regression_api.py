from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


def _create_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    (volume / "CH001.md").write_text("第一章正文\n", encoding="utf-8")


def _applied_change_set(client: TestClient, project: Path) -> str:
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
        if item["relative_path"].endswith("CH001.md")
    )
    created = client.post(
        f"/api/v1/projects/{project_id}/change-sets",
        json={
            "title": "eval corpus regression",
            "summary": "freeze baseline and compare non-degradation",
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "origin": "manual",
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": "第一章正文完成。\n",
                    "rationale": "Create applied change set for eval corpus regression.",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    change_set = created.json()
    decisions = [
        {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
        for change in change_set["changes"]
        for hunk in change["hunks"]
    ]
    ready = client.patch(
        f"/api/v1/change-sets/{change_set['change_set_id']}/decisions",
        json={
            "expected_review_revision": change_set["review_revision"],
            "decisions": decisions,
        },
    )
    assert ready.status_code == 200, ready.text
    ready_body = ready.json()
    applied = client.post(
        f"/api/v1/change-sets/{ready_body['change_set_id']}/apply",
        json={"expected_review_revision": ready_body["review_revision"]},
    )
    assert applied.status_code == 200, applied.text
    return ready_body["change_set_id"]


def test_eval_corpus_baseline_freeze_and_non_degraded_compare(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    db_path = tmp_path / "runtime.sqlite3"

    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=db_path,
                trusted_project_roots=(library,),
                allowed_origins=(),
            )
        )
    ) as client:
        change_set_id = _applied_change_set(client, project)
        artifact_root = db_path.parent / "change-sets" / change_set_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "hard-fact-gate-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "zhimeng.hard-fact-gate-receipt.v1",
                    "receipt_id": "hard-fact-gate-" + ("2" * 32),
                    "status": "passed",
                    "signals": [],
                    "formal_draft_allowed": False,
                    "quality_assessed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (artifact_root / "literary-quality-gate-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "zhimeng.literary-quality-gate-receipt.v1",
                    "receipt_id": "literary-quality-" + ("3" * 32),
                    "finding_counts": {"hard_fact_signal": 0},
                    "findings": [],
                    "formal_draft_allowed": False,
                    "quality_assessed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        exported = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-export",
            json={
                "include_feedback_experience": True,
                "include_formal_knowledge_promotion": True,
                "include_gates": True,
                "include_writeback": True,
            },
        )
        assert exported.status_code == 200, exported.text
        export_receipt = exported.json()["receipt"]

        missing = client.get(f"/api/v1/change-sets/{change_set_id}/eval-corpus-baseline")
        assert missing.status_code == 200, missing.text
        assert missing.json()["receipt"] is None

        frozen = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-baseline",
            json={
                "expected_export_file_sha256": export_receipt["export_file_sha256"],
                "label": "local-v1-min-sample",
            },
        )
        assert frozen.status_code == 200, frozen.text
        baseline = frozen.json()["receipt"]
        assert baseline["baseline_version"] == 1
        assert baseline["formal_draft_allowed"] is False
        assert baseline["quality_assessed"] is False
        assert baseline["training_ready"] is False
        assert "hard_fact_gate" in baseline["present_artifact_kinds"]
        assert (artifact_root / "eval-corpus-baseline-package.json").is_file()

        compared = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-regression",
            json={
                "expected_baseline_receipt_id": baseline["receipt_id"],
                "expected_export_file_sha256": export_receipt["export_file_sha256"],
            },
        )
        assert compared.status_code == 200, compared.text
        body = compared.json()
        assert body["schema"] == "zhimeng.eval-corpus-regression-response.v1"
        receipt = body["receipt"]
        assert receipt["status"] == "non_degraded"
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["missing_required"] == []
        codes = {item["code"]: item["present"] for item in receipt["checks"]}
        assert codes["baseline_present"] is True
        assert codes["present_kinds_superset"] is True
        assert codes["locks_intact"] is True
        assert codes["min_sample_artifacts"] is True
        assert "literary_quality_score" in body["not_claiming"]

        fetched = client.get(f"/api/v1/change-sets/{change_set_id}/eval-corpus-regression")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["receipt_id"] == receipt["receipt_id"]


def test_eval_corpus_regression_detects_hard_fact_degradation(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    db_path = tmp_path / "runtime.sqlite3"

    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=db_path,
                trusted_project_roots=(library,),
                allowed_origins=(),
            )
        )
    ) as client:
        change_set_id = _applied_change_set(client, project)
        artifact_root = db_path.parent / "change-sets" / change_set_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "hard-fact-gate-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "zhimeng.hard-fact-gate-receipt.v1",
                    "receipt_id": "hard-fact-gate-" + ("4" * 32),
                    "status": "passed",
                    "signals": [],
                    "formal_draft_allowed": False,
                    "quality_assessed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        exported = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-export",
            json={"include_gates": True},
        )
        assert exported.status_code == 200, exported.text
        frozen = client.post(f"/api/v1/change-sets/{change_set_id}/eval-corpus-baseline")
        assert frozen.status_code == 200, frozen.text

        # Degrade current package by introducing hard-fact block signals.
        (artifact_root / "hard-fact-gate-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "zhimeng.hard-fact-gate-receipt.v1",
                    "receipt_id": "hard-fact-gate-" + ("5" * 32),
                    "status": "blocked",
                    "signals": [
                        {
                            "code": "placeholder_token",
                            "severity": "block",
                            "summary": "TODO leaked",
                        }
                    ],
                    "formal_draft_allowed": False,
                    "quality_assessed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        reexported = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-export",
            json={"include_gates": True},
        )
        assert reexported.status_code == 200, reexported.text

        compared = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-regression"
        )
        assert compared.status_code == 200, compared.text
        receipt = compared.json()["receipt"]
        assert receipt["status"] == "degraded"
        assert "hard_fact_block_non_increase" in receipt["missing_required"]
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
