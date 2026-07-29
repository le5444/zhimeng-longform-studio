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


def test_llm_literary_score_is_advisory_offline_stub_only(tmp_path: Path) -> None:
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
                "title": "llm literary score",
                "summary": "evaluate llm-shaped literary score",
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
                            "黑暗里忽然传来一声轻响，门缝下渗出淡红光影。\n"
                        ),
                        "rationale": "Inject prose markers for offline LLM score stub.",
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
            f"/api/v1/change-sets/{ready_body['change_set_id']}/llm-literary-scores"
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["receipt"] is None

        evaluated = client.post(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/llm-literary-scores/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        assert payload["schema"] == "zhimeng.llm-literary-score-response.v1"
        receipt = payload["receipt"]
        assert receipt["scorer_mode"] == "offline_stub"
        assert receipt["llm_score_recorded"] is True
        assert receipt["advisory_only"] is True
        assert receipt["quality_assessed"] is False
        assert receipt["subjective_quality_unlocked"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["authorization_unlock"] is False
        assert receipt["provider_model"] is None
        assert receipt["outcome"] == "ready_candidate"
        assert receipt["policy_version"] == "local-v1-llm-literary-score-1"
        assert isinstance(receipt["total_score"], int)
        assert 0 <= receipt["total_score"] <= 100
        assert len(receipt["axes"]) == 5
        assert {item["code"] for item in receipt["axes"]} == {
            "immersion",
            "character_voice",
            "emotional_arc",
            "prose_craft",
            "suspense_pull",
        }
        assert "quality_assessed" in payload["not_claiming"]
        assert "literary_quality_score" in payload["not_claiming"]
        assert "external_blind_review_matrix" in payload["not_claiming"]
        assert "local_v1_exe" in payload["not_claiming"]

        prefer = client.post(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/llm-literary-scores/evaluate",
            json={
                "expected_review_revision": ready_body["review_revision"],
                "prefer_provider": True,
            },
        )
        # same revision/outcome should replay existing offline receipt
        assert prefer.status_code == 200, prefer.text
        assert prefer.json()["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert prefer.json()["receipt"]["scorer_mode"] == "offline_stub"
        assert prefer.json()["receipt"]["quality_assessed"] is False

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready_body["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
