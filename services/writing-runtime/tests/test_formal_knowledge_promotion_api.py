from __future__ import annotations

import json
from datetime import UTC, datetime
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


def _plant_experience_bundle(
    runtime_root: Path,
    *,
    change_set_id: str,
    project_id: str,
) -> tuple[str, str, str]:
    candidate_id = "feedback-candidate-" + ("a" * 32)
    bundle_id = "feedback-candidate-bundle-" + ("b" * 32)
    now = datetime.now(UTC)
    now_text = now.isoformat().replace("+00:00", "Z")
    material = {
        "schema": "zhimeng.feedback-candidate-bundle.v1",
        "bundle_id": bundle_id,
        "change_set_id": change_set_id,
        "project_id": project_id,
        "reader_review_decision_receipt_id": "reader-review-decision-" + ("c" * 32),
        "reader_review_decision_receipt_hash": "d" * 64,
        "follow_up_change_set_id": None,
        "candidates": [
            {
                "candidate_id": candidate_id,
                "source_kind": "reader_review_decision",
                "source_decision": "adopted",
                "finding_id": "fact-1",
                "finding_kind": "fact_risk",
                "summary": "关键人物名前后不一致",
                "evidence_quote": "前文称林海，后文称林河",
                "relative_path": "05_正文内容/第一卷/CH001.md",
                "status": "experience",
                "promotion_status": "experience_recorded",
                "confidence": "author_explicit",
                "regression_result": "pass_with_watch",
                "experience_relative_path": "07_运营数据/nlcl_feedback/experience_candidates.jsonl",
                "formal_knowledge_contaminated": False,
                "formal_draft_allowed": False,
            }
        ],
        "formal_knowledge_contaminated": False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "novel_state_synced": False,
        "created_at": now_text,
    }
    # compute receipt_hash using runtime helper
    from zhimeng_runtime.context_models import canonical_json_hash

    receipt_hash = canonical_json_hash(material)
    material["receipt_hash"] = receipt_hash
    material["created_at"] = now.isoformat()
    root = runtime_root / "change-sets" / change_set_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "feedback-candidate-bundle.json").write_text(
        json.dumps(material, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return candidate_id, bundle_id, receipt_hash


def test_formal_knowledge_promotion_stage_for_review_without_contamination(
    tmp_path: Path,
) -> None:
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
                "title": "formal knowledge promotion",
                "summary": "stage experience into formal knowledge review queue",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文完成。\n",
                        "rationale": "Create applied change set for formal knowledge promotion.",
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

        candidate_id, _bundle_id, bundle_hash = _plant_experience_bundle(
            db_path.parent,
            change_set_id=change_set_id,
            project_id=project_id,
        )

        incomplete = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion/evaluate",
            json={},
        )
        # Before planting? already planted. Should be ready.
        assert incomplete.status_code == 200, incomplete.text
        evaluated = incomplete.json()
        assert evaluated["schema"] == "zhimeng.formal-knowledge-promotion-response.v1"
        receipt = evaluated["receipt"]
        assert receipt["status"] == "ready"
        assert receipt["authorization_input_ready"] is True
        assert receipt["formal_knowledge_contaminated"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_formal_knowledge"] is False
        assert receipt["missing_required"] == []
        assert any(item["candidate_id"] == candidate_id for item in receipt["items"])
        assert "formal_knowledge_auto_activation" in evaluated["not_claiming"]

        staged = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "stage_for_review",
                    }
                ],
            },
        )
        assert staged.status_code == 200, staged.text
        staged_body = staged.json()
        staged_receipt = staged_body["receipt"]
        assert staged_receipt["status"] == "staged"
        assert staged_receipt["authorization_input_ready"] is True
        assert staged_receipt["writes_formal_knowledge"] is False
        assert staged_receipt["formal_knowledge_contaminated"] is False
        assert staged_receipt["queue_rows_appended"] == 1
        assert staged_receipt["queue_relative_path"] == (
            "07_运营数据/nlcl_feedback/formal_knowledge_promotion_queue.jsonl"
        )
        assert len(staged_receipt["packages"]) == 1
        package = staged_receipt["packages"][0]
        assert package["candidate_id"] == candidate_id
        assert package["proposed_card_type"] == "validator"
        assert package["writes_formal_knowledge"] is False
        assert package["source_kind"] == "feedback_experience"

        queue_file = (
            project
            / "07_运营数据"
            / "nlcl_feedback"
            / "formal_knowledge_promotion_queue.jsonl"
        )
        assert queue_file.is_file()
        rows = [
            json.loads(line)
            for line in queue_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["candidate_id"] == candidate_id
        assert rows[0]["writes_formal_knowledge"] is False
        assert rows[0]["formal_knowledge_contaminated"] is False

        fetched = client.get(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion"
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["receipt_id"] == staged_receipt["receipt_id"]

        materialized = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "materialize_for_review",
                    }
                ],
            },
        )
        assert materialized.status_code == 200, materialized.text
        material_receipt = materialized.json()["receipt"]
        assert material_receipt["status"] == "materialized"
        assert material_receipt["writes_formal_knowledge"] is False
        assert material_receipt["formal_knowledge_contaminated"] is False
        assert material_receipt["formal_draft_allowed"] is False
        assert len(material_receipt["packages"]) == 1
        material_package = material_receipt["packages"][0]
        assert material_package["materialized_source_import_id"]
        assert material_package["materialized_knowledge_candidate_id"]
        assert material_package["writes_formal_knowledge"] is False
        assert any(
            item["promotion_status"] == "formal_promotion_materialized"
            for item in material_receipt["items"]
        )

        listed = client.get(
            f"/api/v1/projects/{project_id}/knowledge/candidates"
        )
        assert listed.status_code == 200, listed.text
        candidates = listed.json()["candidates"]
        assert any(
            item["candidate_id"]
            == material_package["materialized_knowledge_candidate_id"]
            and item["status"] == "pending"
            and item["card_type"] == "validator"
            and item["lineage_status"] == "legacy_unverified"
            for item in candidates
        )

        author_request = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "author_request_formalization",
                    }
                ],
            },
        )
        assert author_request.status_code == 200, author_request.text
        author_body = author_request.json()["receipt"]
        assert author_body["status"] == "author_formalization_requested"
        assert author_body["author_formalization_intent"] == "request"
        assert author_body["author_formalization_effective"] is False
        assert author_body["writes_formal_knowledge"] is False
        assert author_body["formal_draft_allowed"] is False
        assert author_body["formal_knowledge_contaminated"] is False
        assert author_body["authorization_input_ready"] is True
        assert "formal_knowledge_auto_activation" in author_request.json()["not_claiming"]
        assert "formal_draft_authorization_unlock" in author_request.json()["not_claiming"]
        assert any(
            item["promotion_status"] == "formal_promotion_author_requested"
            for item in author_body["items"]
        )
        # Author request does not formalize/activate knowledge candidates.
        listed_after_request = client.get(
            f"/api/v1/projects/{project_id}/knowledge/candidates"
        )
        assert listed_after_request.status_code == 200, listed_after_request.text
        assert any(
            item["candidate_id"]
            == material_package["materialized_knowledge_candidate_id"]
            and item["status"] == "pending"
            and item["lineage_status"] == "legacy_unverified"
            for item in listed_after_request.json()["candidates"]
        )

        author_commit = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "commit_author_attested_formal_knowledge",
                    }
                ],
            },
        )
        assert author_commit.status_code == 200, author_commit.text
        commit_body = author_commit.json()["receipt"]
        assert commit_body["status"] == "author_formalization_committed"
        assert commit_body["author_formalization_intent"] == "request"
        assert commit_body["author_formalization_effective"] is True
        assert commit_body["writes_formal_knowledge"] is True
        assert commit_body["formal_draft_allowed"] is False
        assert commit_body["formal_knowledge_contaminated"] is False
        assert commit_body["authorization_input_ready"] is True
        assert "formal_knowledge_auto_activation" in author_commit.json()["not_claiming"]
        assert "formal_draft_authorization_unlock" in author_commit.json()["not_claiming"]
        assert "formal_knowledge_write_unlock" not in author_commit.json()["not_claiming"]
        commit_item = next(
            item
            for item in commit_body["items"]
            if item["candidate_id"] == candidate_id
        )
        assert commit_item["promotion_status"] == "formal_promotion_author_committed"
        assert commit_item["writes_formal_knowledge"] is True
        assert commit_item["committed_knowledge_card_id"]
        assert commit_item["committed_knowledge_version_id"]

        listed_after_commit = client.get(
            f"/api/v1/projects/{project_id}/knowledge/candidates"
        )
        assert listed_after_commit.status_code == 200, listed_after_commit.text
        assert any(
            item["candidate_id"]
            == material_package["materialized_knowledge_candidate_id"]
            and item["status"] == "promoted"
            for item in listed_after_commit.json()["candidates"]
        )

        cards = client.get(f"/api/v1/projects/{project_id}/knowledge/cards")
        assert cards.status_code == 200, cards.text
        card = next(
            item
            for item in cards.json()["cards"]
            if item["card_id"] == commit_item["committed_knowledge_card_id"]
        )
        assert card["status"] == "active"
        assert card["active_version_id"] == commit_item["committed_knowledge_version_id"]
        assert card["active_version"]["lineage_status"] == "legacy_unverified"
        assert card["active_version"]["formalization_status"] == "legacy_unverified"
        assert card["active_version"]["formalization"] is None
        # No verified formalization => not Context-injectable / no auto-activation.

        # Idempotent recommit keeps the same formal knowledge write.
        recommit = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "commit_author_attested_formal_knowledge",
                    }
                ],
            },
        )
        assert recommit.status_code == 200, recommit.text
        recommit_item = next(
            item
            for item in recommit.json()["receipt"]["items"]
            if item["candidate_id"] == candidate_id
        )
        assert recommit_item["committed_knowledge_card_id"] == commit_item["committed_knowledge_card_id"]
        assert recommit_item["committed_knowledge_version_id"] == commit_item["committed_knowledge_version_id"]

        rolled = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": bundle_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "rollback_author_attested_formal_knowledge",
                    }
                ],
            },
        )
        assert rolled.status_code == 200, rolled.text
        rolled_body = rolled.json()["receipt"]
        assert rolled_body["status"] == "rolled_back"
        assert rolled_body["writes_formal_knowledge"] is False
        assert rolled_body["author_formalization_effective"] is False
        assert rolled_body["formal_draft_allowed"] is False
        assert any(
            item["promotion_status"] == "formal_promotion_rolled_back"
            and item["writes_formal_knowledge"] is False
            for item in rolled_body["items"]
            if item["candidate_id"] == candidate_id
        )
        cards_after_rollback = client.get(f"/api/v1/projects/{project_id}/knowledge/cards")
        assert cards_after_rollback.status_code == 200, cards_after_rollback.text
        rolled_card = next(
            item
            for item in cards_after_rollback.json()["cards"]
            if item["card_id"] == commit_item["committed_knowledge_card_id"]
        )
        assert rolled_card["status"] == "disabled"



def _plant_eval_corpus_baseline_and_regression(
    runtime_root: Path,
    *,
    change_set_id: str,
    project_id: str,
    status: str = "non_degraded",
) -> None:
    """Plant a frozen baseline + regression receipt for formal-knowledge gate tests."""
    artifact_root = runtime_root / "change-sets" / change_set_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    package = {
        "schema": "zhimeng.eval-corpus-package.v1",
        "change_set_id": change_set_id,
        "project_id": project_id,
        "outcome": "applied",
        "redaction_policy": "local-v1-eval-corpus-redact-1",
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "formal_knowledge_contaminated": False,
        "training_ready": False,
        "artifacts": {
            "hard_fact_gate": {
                "schema": "zhimeng.hard-fact-gate-receipt.v1",
                "status": "passed",
                "signals": [],
            }
        },
        "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    package_text = json.dumps(package, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    export_sha = __import__("hashlib").sha256((package_text + "\n").encode("utf-8")).hexdigest()
    (artifact_root / "eval-corpus-package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (artifact_root / "eval-corpus-baseline-package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    baseline_id = "eval-corpus-baseline-" + ("e" * 32)
    export_id = "eval-corpus-export-" + ("f" * 32)
    regression_id = "eval-corpus-regression-" + ("1" * 32)
    now = datetime.now(UTC)
    # Build validated receipts via runtime models so hash/validators stay honest.
    from zhimeng_runtime.eval_corpus_export_models import (
        EvalCorpusArtifactRef,
        EvalCorpusExportReceipt,
        eval_corpus_export_receipt_hash,
    )
    from zhimeng_runtime.eval_corpus_regression_models import (
        EvalCorpusBaselineReceipt,
        EvalCorpusRegressionCheck,
        EvalCorpusRegressionCheckCode,
        EvalCorpusRegressionReceipt,
        EvalCorpusRegressionStatus,
        eval_corpus_baseline_receipt_hash,
        eval_corpus_regression_receipt_hash,
    )

    export_provisional = EvalCorpusExportReceipt.model_construct(
        schema_id="zhimeng.eval-corpus-export-receipt.v1",
        receipt_id=export_id,
        change_set_id=change_set_id,
        project_id=project_id,
        outcome="applied",
        export_relative_path=f"07_运营数据/eval_corpus/{change_set_id}.json",
        export_file_sha256=export_sha,
        artifact_count=1,
        artifacts=[
            EvalCorpusArtifactRef(
                kind="hard_fact_gate",
                present=True,
                relative_artifact="hard-fact-gate-receipt.json",
                content_sha256="a" * 64,
                desensitized=True,
            )
        ],
        redaction_policy="local-v1-eval-corpus-redact-1",
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        created_at=now,
        receipt_hash="0" * 64,
    )
    export_receipt = EvalCorpusExportReceipt(
        receipt_id=export_id,
        change_set_id=change_set_id,
        project_id=project_id,
        outcome="applied",
        export_relative_path=f"07_运营数据/eval_corpus/{change_set_id}.json",
        export_file_sha256=export_sha,
        artifact_count=1,
        artifacts=export_provisional.artifacts,
        redaction_policy="local-v1-eval-corpus-redact-1",
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        created_at=now,
        receipt_hash=eval_corpus_export_receipt_hash(export_provisional),
    )
    (artifact_root / "eval-corpus-export-receipt.json").write_text(
        json.dumps(export_receipt.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )

    baseline_provisional = EvalCorpusBaselineReceipt.model_construct(
        schema_id="zhimeng.eval-corpus-baseline-receipt.v1",
        receipt_id=baseline_id,
        change_set_id=change_set_id,
        project_id=project_id,
        baseline_version=1,
        source_export_receipt_id=export_id,
        source_export_file_sha256=export_sha,
        package_schema="zhimeng.eval-corpus-package.v1",
        present_artifact_kinds=["hard_fact_gate"],
        artifact_count=1,
        label="fixture",
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        policy_version="local-v1-eval-corpus-baseline-1",
        frozen_at=now,
        receipt_hash="0" * 64,
    )
    baseline = EvalCorpusBaselineReceipt(
        receipt_id=baseline_id,
        change_set_id=change_set_id,
        project_id=project_id,
        baseline_version=1,
        source_export_receipt_id=export_id,
        source_export_file_sha256=export_sha,
        package_schema="zhimeng.eval-corpus-package.v1",
        present_artifact_kinds=["hard_fact_gate"],
        artifact_count=1,
        label="fixture",
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        policy_version="local-v1-eval-corpus-baseline-1",
        frozen_at=now,
        receipt_hash=eval_corpus_baseline_receipt_hash(baseline_provisional),
    )
    (artifact_root / "eval-corpus-baseline-receipt.json").write_text(
        json.dumps(baseline.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )

    status_enum = EvalCorpusRegressionStatus(status)
    checks = [
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.BASELINE_PRESENT,
            required=True,
            present=True,
            detail="baseline present",
        ),
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.CURRENT_EXPORT_PRESENT,
            required=True,
            present=True,
            detail="current export present",
        ),
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.PACKAGE_SCHEMA_COMPATIBLE,
            required=True,
            present=True,
            detail="schema compatible",
        ),
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.PRESENT_KINDS_SUPERSET,
            required=True,
            present=status_enum is EvalCorpusRegressionStatus.NON_DEGRADED,
            detail="kinds",
        ),
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.LOCKS_INTACT,
            required=True,
            present=True,
            detail="locks intact",
        ),
        EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.MIN_SAMPLE_ARTIFACTS,
            required=True,
            present=True,
            detail="min sample",
        ),
    ]
    if status_enum is EvalCorpusRegressionStatus.DEGRADED:
        # force a required miss for degraded status validator
        checks[3] = EvalCorpusRegressionCheck(
            code=EvalCorpusRegressionCheckCode.PRESENT_KINDS_SUPERSET,
            required=True,
            present=False,
            detail="kinds missing",
        )
    missing = [item.code for item in checks if item.required and not item.present]
    provisional = EvalCorpusRegressionReceipt.model_construct(
        schema_id="zhimeng.eval-corpus-regression-receipt.v1",
        receipt_id=regression_id,
        change_set_id=change_set_id,
        project_id=project_id,
        status=status_enum,
        baseline_receipt_id=baseline_id,
        baseline_version=1,
        baseline_export_file_sha256=export_sha,
        current_export_receipt_id=export_id,
        current_export_file_sha256=export_sha,
        checks=checks,
        missing_required=missing,
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        policy_version="local-v1-eval-corpus-regression-1",
        evaluated_at=now,
        receipt_hash="0" * 64,
    )
    regression = EvalCorpusRegressionReceipt(
        receipt_id=regression_id,
        change_set_id=change_set_id,
        project_id=project_id,
        status=status_enum,
        baseline_receipt_id=baseline_id,
        baseline_version=1,
        baseline_export_file_sha256=export_sha,
        current_export_receipt_id=export_id,
        current_export_file_sha256=export_sha,
        checks=checks,
        missing_required=missing,
        formal_draft_allowed=False,
        quality_assessed=False,
        formal_knowledge_contaminated=False,
        training_ready=False,
        policy_version="local-v1-eval-corpus-regression-1",
        evaluated_at=now,
        receipt_hash=eval_corpus_regression_receipt_hash(provisional),
    )
    (artifact_root / "eval-corpus-regression-receipt.json").write_text(
        json.dumps(regression.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def test_formal_knowledge_promotion_defers_eval_corpus_gate_without_baseline(
    tmp_path: Path,
) -> None:
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
                "title": "eval gate deferred",
                "summary": "no baseline means deferred gate",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文完成。\n",
                        "rationale": "fixture",
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
        applied = client.post(
            f"/api/v1/change-sets/{ready.json()['change_set_id']}/apply",
            json={"expected_review_revision": ready.json()["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        change_set_id = ready.json()["change_set_id"]
        candidate_id, _bundle_id, receipt_hash = _plant_experience_bundle(
            db_path.parent,
            change_set_id=change_set_id,
            project_id=project_id,
        )
        evaluated = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion/evaluate",
            json={"expected_feedback_candidate_bundle_hash": receipt_hash},
        )
        assert evaluated.status_code == 200, evaluated.text
        checks = {
            item["code"]: item for item in evaluated.json()["receipt"]["checks"]
        }
        assert checks["eval_corpus_baseline_gate"]["required"] is False
        assert checks["eval_corpus_baseline_gate"]["present"] is True
        assert "deferred" in checks["eval_corpus_baseline_gate"]["detail"]


def test_formal_knowledge_stage_requires_non_degraded_when_baseline_frozen(
    tmp_path: Path,
) -> None:
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
                "title": "eval gate required",
                "summary": "baseline forces non-degradation before stage",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文完成。\n",
                        "rationale": "fixture",
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
        applied = client.post(
            f"/api/v1/change-sets/{ready.json()['change_set_id']}/apply",
            json={"expected_review_revision": ready.json()["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        change_set_id = ready.json()["change_set_id"]
        candidate_id, _bundle_id, receipt_hash = _plant_experience_bundle(
            db_path.parent,
            change_set_id=change_set_id,
            project_id=project_id,
        )
        _plant_eval_corpus_baseline_and_regression(
            db_path.parent,
            change_set_id=change_set_id,
            project_id=project_id,
            status="degraded",
        )
        evaluated = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion/evaluate",
            json={"expected_feedback_candidate_bundle_hash": receipt_hash},
        )
        assert evaluated.status_code == 200, evaluated.text
        receipt = evaluated.json()["receipt"]
        checks = {item["code"]: item for item in receipt["checks"]}
        assert checks["eval_corpus_baseline_gate"]["required"] is True
        assert checks["eval_corpus_baseline_gate"]["present"] is False
        assert "eval_corpus_baseline_gate" in receipt["missing_required"]

        blocked = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": receipt_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "stage_for_review",
                    }
                ],
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert "non_degraded" in blocked.text

        _plant_eval_corpus_baseline_and_regression(
            db_path.parent,
            change_set_id=change_set_id,
            project_id=project_id,
            status="non_degraded",
        )
        staged = client.post(
            f"/api/v1/change-sets/{change_set_id}/formal-knowledge-promotion",
            json={
                "expected_feedback_candidate_bundle_hash": receipt_hash,
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "action": "stage_for_review",
                    }
                ],
            },
        )
        assert staged.status_code == 200, staged.text
        staged_receipt = staged.json()["receipt"]
        assert staged_receipt["status"] == "staged"
        checks = {item["code"]: item for item in staged_receipt["checks"]}
        assert checks["eval_corpus_baseline_gate"]["present"] is True
        assert staged_receipt["formal_draft_allowed"] is False
