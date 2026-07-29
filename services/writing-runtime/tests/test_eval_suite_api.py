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


def _applied_with_baseline(
    client: TestClient,
    project: Path,
    *,
    db_parent: Path,
    after_content: str,
    gate_suffix: str,
) -> tuple[str, str, dict]:
    inspected = client.post(
        "/api/v1/projects/inspect",
        json={"root_path": str(project)},
    )
    assert inspected.status_code == 200, inspected.text
    snapshot = inspected.json()
    project_id = snapshot["project"]["project_id"]
    chapter = next(
        item for item in snapshot["files"] if item["relative_path"].endswith("CH001.md")
    )
    created = client.post(
        f"/api/v1/projects/{project_id}/change-sets",
        json={
            "title": "eval suite member",
            "summary": "member for cross-project suite",
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "origin": "manual",
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": after_content,
                    "rationale": "Create applied member for eval suite.",
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
    change_set_id = ready_body["change_set_id"]

    artifact_root = db_parent / "change-sets" / change_set_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "hard-fact-gate-receipt.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.hard-fact-gate-receipt.v1",
                "receipt_id": "hard-fact-gate-" + (gate_suffix * 32),
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
                "receipt_id": "literary-quality-" + (gate_suffix * 32),
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
    frozen = client.post(
        f"/api/v1/change-sets/{change_set_id}/eval-corpus-baseline",
        json={
            "expected_export_file_sha256": export_receipt["export_file_sha256"],
            "label": f"suite-member-{gate_suffix}",
        },
    )
    assert frozen.status_code == 200, frozen.text
    return project_id, change_set_id, frozen.json()["receipt"]


def test_eval_suite_cross_project_compare_and_blind_review(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project_a = library / "book-a"
    project_b = library / "book-b"
    _create_project(project_a)
    _create_project(project_b)
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
        project_id_a, change_set_a, baseline_a = _applied_with_baseline(
            client,
            project_a,
            db_parent=db_path.parent,
            after_content="第一章正文完成。\n",
            gate_suffix="a",
        )
        project_id_b, change_set_b, baseline_b = _applied_with_baseline(
            client,
            project_b,
            db_parent=db_path.parent,
            after_content="第一章正文也完成。\n",
            gate_suffix="b",
        )

        prepared = client.post(
            "/api/v1/eval-suites/prepare",
            json={
                "title": "local-v1 cross-project min suite",
                "summary": "Orchestrate two project baselines and author blind review.",
                "members": [
                    {
                        "project_id": project_id_a,
                        "change_set_id": change_set_a,
                        "expected_baseline_receipt_id": baseline_a["receipt_id"],
                        "expected_baseline_receipt_hash": baseline_a["receipt_hash"],
                        "label": "book-a",
                    },
                    {
                        "project_id": project_id_b,
                        "change_set_id": change_set_b,
                        "expected_baseline_receipt_id": baseline_b["receipt_id"],
                        "expected_baseline_receipt_hash": baseline_b["receipt_hash"],
                        "label": "book-b",
                    },
                ],
            },
        )
        assert prepared.status_code == 201, prepared.text
        body = prepared.json()
        assert body["schema"] == "zhimeng.eval-suite-response.v1"
        receipt = body["receipt"]
        assert receipt["status"] == "prepared"
        assert receipt["member_count"] == 2
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["training_ready"] is False
        assert receipt["auto_upgrade"] is False
        suite_id = receipt["suite_id"]

        frozen = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={
                "action": "freeze",
                "expected_status": "prepared",
                "expected_state_revision": receipt["state_revision"],
            },
        )
        assert frozen.status_code == 200, frozen.text
        frozen_receipt = frozen.json()["receipt"]
        assert frozen_receipt["status"] == "frozen"
        assert frozen_receipt["frozen"] is True

        # Second suite with same members acts as non-degraded compare target.
        prepared_b = client.post(
            "/api/v1/eval-suites/prepare",
            json={
                "title": "local-v1 cross-project min suite b",
                "summary": "Second frozen suite for non-degraded compare.",
                "members": [
                    {
                        "project_id": project_id_a,
                        "change_set_id": change_set_a,
                        "expected_baseline_receipt_id": baseline_a["receipt_id"],
                    },
                    {
                        "project_id": project_id_b,
                        "change_set_id": change_set_b,
                        "expected_baseline_receipt_id": baseline_b["receipt_id"],
                    },
                ],
            },
        )
        assert prepared_b.status_code == 201, prepared_b.text
        suite_b = prepared_b.json()["receipt"]["suite_id"]
        frozen_b = client.post(
            f"/api/v1/eval-suites/{suite_b}/actions",
            json={"action": "freeze"},
        )
        assert frozen_b.status_code == 200, frozen_b.text

        compared = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={
                "action": "compare",
                "expected_status": "frozen",
                "compare_against_suite_id": suite_b,
            },
        )
        assert compared.status_code == 200, compared.text
        compared_receipt = compared.json()["receipt"]
        assert compared_receipt["status"] == "compare_ready"
        assert compared_receipt["compare_status"] == "non_degraded"
        assert all(item["present"] for item in compared_receipt["compare_checks"])

        opened = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={"action": "blind_review_open"},
        )
        assert opened.status_code == 200, opened.text
        open_body = opened.json()
        open_receipt = open_body["receipt"]
        assert open_receipt["status"] == "blind_review_open"
        assert open_receipt["blind_review_open"] is True
        assert open_receipt["reveal_member_identities"] is False
        assert len(open_body["blind_member_views"]) == 2
        tokens = [item["member_token"] for item in open_body["blind_member_views"]]
        assert all(token.startswith("eval-member-") for token in tokens)
        # Blind view must not expose project identities.
        assert "project_id" not in open_body["blind_member_views"][0]

        submitted = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={
                "action": "blind_review_submit",
                "blind_review_items": [
                    {"member_token": tokens[0], "score": 4, "notes": "continuous pressure holds"},
                    {"member_token": tokens[1], "score": 5, "notes": "voice stays concrete"},
                ],
            },
        )
        assert submitted.status_code == 200, submitted.text
        assert len(submitted.json()["receipt"]["blind_scores"]) == 2
        assert submitted.json()["receipt"]["reveal_member_identities"] is False

        finalized = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={"action": "blind_review_finalize"},
        )
        assert finalized.status_code == 200, finalized.text
        final_receipt = finalized.json()["receipt"]
        assert final_receipt["status"] == "blind_review_finalized"
        assert final_receipt["blind_review_finalized"] is True
        assert final_receipt["reveal_member_identities"] is True
        assert final_receipt["formal_draft_allowed"] is False
        assert final_receipt["quality_assessed"] is False
        assert {item["project_id"] for item in final_receipt["members"]} == {
            project_id_a,
            project_id_b,
        }
        assert "formal_draft_authorization_unlock" in finalized.json()["not_claiming"]

        listed = client.get("/api/v1/eval-suites")
        assert listed.status_code == 200, listed.text
        listed_body = listed.json()
        assert listed_body["schema"] == "zhimeng.eval-suite-list-response.v1"
        assert any(item["suite_id"] == suite_id for item in listed_body["suites"])
        assert all(item["formal_draft_allowed"] is False for item in listed_body["suites"])

        fetched = client.get(f"/api/v1/eval-suites/{suite_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["receipt_id" if False else "suite_id"] == suite_id
        assert fetched.json()["receipt"]["status"] == "blind_review_finalized"


def test_eval_suite_provider_fingerprint_report(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project_a = library / "book-a"
    project_b = library / "book-b"
    _create_project(project_a)
    _create_project(project_b)
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
        project_id_a, change_set_a, baseline_a = _applied_with_baseline(
            client,
            project_a,
            db_parent=db_path.parent,
            after_content="第一章正文完成。\n",
            gate_suffix="a",
        )
        project_id_b, change_set_b, baseline_b = _applied_with_baseline(
            client,
            project_b,
            db_parent=db_path.parent,
            after_content="第一章正文也完成。\n",
            gate_suffix="b",
        )

        prepared = client.post(
            "/api/v1/eval-suites/prepare",
            json={
                "title": "provider fingerprint suite",
                "summary": "Build provider fingerprint report for suite members.",
                "members": [
                    {
                        "project_id": project_id_a,
                        "change_set_id": change_set_a,
                        "expected_baseline_receipt_id": baseline_a["receipt_id"],
                        "expected_baseline_receipt_hash": baseline_a["receipt_hash"],
                        "label": "member-a",
                    },
                    {
                        "project_id": project_id_b,
                        "change_set_id": change_set_b,
                        "expected_baseline_receipt_id": baseline_b["receipt_id"],
                        "expected_baseline_receipt_hash": baseline_b["receipt_hash"],
                        "label": "member-b",
                    },
                ],
            },
        )
        assert prepared.status_code == 201, prepared.text
        suite_id = prepared.json()["receipt"]["suite_id"]

        # Unfrozen suite cannot build fingerprint report.
        blocked = client.post(
            f"/api/v1/eval-suites/{suite_id}/provider-fingerprint-report"
        )
        assert blocked.status_code == 422, blocked.text

        frozen = client.post(
            f"/api/v1/eval-suites/{suite_id}/actions",
            json={"action": "freeze"},
        )
        assert frozen.status_code == 200, frozen.text

        incomplete = client.post(
            f"/api/v1/eval-suites/{suite_id}/provider-fingerprint-report"
        )
        assert incomplete.status_code == 200, incomplete.text
        body = incomplete.json()
        report = body["report"]
        assert report["status"] == "incomplete"
        assert report["all_present"] is False
        assert report["present_count"] == 0
        assert report["quality_assessed"] is False
        assert report["formal_draft_allowed"] is False
        assert report["training_ready"] is False
        assert "quality_assessed" in body["not_claiming"]
        assert "local_v1_exe" in body["not_claiming"]
        assert len(report["missing_member_tokens"]) == 2

        # Attach sidecars for both members and rebuild to complete.
        for change_set_id, model_name in (
            (change_set_a, "demo-model-a"),
            (change_set_b, "demo-model-b"),
        ):
            sidecar_dir = db_path.parent / "change-sets" / change_set_id
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            (sidecar_dir / "provider-fingerprint.json").write_text(
                json.dumps(
                    {
                        "requested_models": [model_name],
                        "reported_models": [model_name],
                        "model_identity_statuses": ["exact_match"],
                        "provider_identity_status": "matched",
                        "turn_count": 1,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        complete = client.post(
            f"/api/v1/eval-suites/{suite_id}/provider-fingerprint-report"
        )
        assert complete.status_code == 200, complete.text
        complete_report = complete.json()["report"]
        assert complete_report["status"] == "complete"
        assert complete_report["all_present"] is True
        assert complete_report["present_count"] == 2
        assert complete_report["quality_assessed"] is False
        assert all(item["present"] for item in complete_report["members"])
        assert all(item["fingerprint"] for item in complete_report["members"])
        assert all(
            item["evidence_kind"] == "artifact_sidecar"
            for item in complete_report["members"]
        )

        fetched = client.get(
            f"/api/v1/eval-suites/{suite_id}/provider-fingerprint-report"
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["report"]["report_hash"] == complete_report["report_hash"]

