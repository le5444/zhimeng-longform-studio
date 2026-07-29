from __future__ import annotations

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


def test_literary_quality_gate_is_advisory_only_and_feeds_formal_draft(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

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
            if item["relative_path"].endswith("CH001.md")
        )
        created = client.post(
            f"/api/v1/projects/{project_id}/change-sets",
            json={
                "title": "literary quality",
                "summary": "evaluate literary quality gate",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": (
                            "夜风带着血腥味扑面而来。\n\n"
                            "「谁在那里？」他低声问，掌心全是冷汗。\n\n"
                            "黑暗里忽然传来一声轻响，值得注意的是门缝下渗出淡红光影。TODO\n"
                        ),
                        "rationale": "Inject structural markers for literary quality Gate.",
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

        empty = client.get(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/literary-quality-gate"
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["receipt"] is None

        evaluated = client.post(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/literary-quality-gate/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        assert payload["schema"] == "zhimeng.literary-quality-gate-response.v1"
        receipt = payload["receipt"]
        assert receipt["decision"] == "advisory_only"
        assert receipt["authorization_unlock"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["structural_checks_run"] is True
        assert receipt["deterministic_rubric_scored"] is True
        assert receipt["outcome"] == "ready_candidate"
        assert receipt["gate_mode"] == "deterministic_structural_rubric_v2"
        assert receipt["policy_version"] == "local-v1-advisory-2"
        assert isinstance(receipt["rubric_total_score"], int)
        assert 0 <= receipt["rubric_total_score"] <= 100
        assert len(receipt["rubric_scores"]) == 5
        assert {item["code"] for item in receipt["rubric_scores"]} == {
            "dialogue_balance",
            "sensory_density",
            "pacing_variation",
            "hook_opening",
            "repetition_risk",
        }
        assert any(
            item["code"] == "deterministic_rubric"
            for item in receipt["dimensions"]
        )
        assert any(
            item["code"] == "subjective_literary_score"
            and item["status"] == "not_scored"
            for item in receipt["dimensions"]
        )
        assert any(
            item["code"] == "authorization_unlock" and item["status"] == "fail"
            for item in receipt["dimensions"]
        )
        assert "literary_quality_score" in payload["not_claiming"]
        assert "llm_literary_score" in payload["not_claiming"]
        assert "quality_assessed" in payload["not_claiming"]
        assert receipt["finding_counts"].get("structural", 0) >= 1

        replay = client.post(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/literary-quality-gate/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert replay.json()["receipt"]["receipt_hash"] == receipt["receipt_hash"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready_body["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
        literary_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "literary_quality_gate"
        )
        assert literary_prereq["satisfied"] is False
        assert "receipt exists" in literary_prereq["detail"]
        assert any(
            item["code"] == "literary_quality_gate_missing"
            and "advisory only" in item["message"]
            for item in formal_receipt["denials"]
        )
