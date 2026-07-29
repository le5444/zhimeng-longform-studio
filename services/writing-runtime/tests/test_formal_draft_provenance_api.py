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


def test_formal_draft_provenance_complete_after_required_artifacts(
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
        revision = snapshot["project"]["revision"]
        fingerprint = snapshot["project"]["fingerprint"]
        chapter = next(
            item for item in snapshot["files"] if item["relative_path"].endswith("CH001.md")
        )

        limited = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/provenance/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert limited.status_code == 200, limited.text
        limited_receipt = limited.json()["receipt"]
        assert limited_receipt["status"] == "scope_limited"
        assert limited_receipt["authorization_input_ready"] is False
        assert limited_receipt["formal_draft_allowed"] is False

        created = client.post(
            f"/api/v1/projects/{project_id}/change-sets",
            json={
                "title": "provenance",
                "summary": "formal draft provenance chain",
                "base_revision": revision,
                "base_fingerprint": fingerprint,
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文完成。\n",
                        "rationale": "Provenance fixture content.",
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
        change_set_id = ready_body["change_set_id"]

        incomplete = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/provenance/evaluate",
            json={"change_set_id": change_set_id},
        )
        assert incomplete.status_code == 200, incomplete.text
        incomplete_receipt = incomplete.json()["receipt"]
        assert incomplete_receipt["status"] == "incomplete"
        assert incomplete_receipt["authorization_input_ready"] is False
        assert "hard_fact_gate_receipt" in [
            item if isinstance(item, str) else item
            for item in incomplete_receipt["missing_required"]
        ]

        author = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/author-decision",
            json={
                "intent": "request_authorization",
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
                "change_set_id": change_set_id,
            },
        )
        assert author.status_code == 200, author.text

        hard = client.post(
            f"/api/v1/change-sets/{change_set_id}/hard-fact-gate/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert hard.status_code == 200, hard.text
        assert hard.json()["receipt"]["status"] == "passed"

        literary = client.post(
            f"/api/v1/change-sets/{change_set_id}/literary-quality-gate/evaluate",
            json={"expected_review_revision": ready_body["review_revision"]},
        )
        assert literary.status_code == 200, literary.text

        complete = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/provenance/evaluate",
            json={"change_set_id": change_set_id},
        )
        assert complete.status_code == 200, complete.text
        complete_receipt = complete.json()["receipt"]
        assert complete_receipt["status"] == "complete"
        assert complete_receipt["authorization_input_ready"] is True
        assert complete_receipt["authorization_unlock"] is False
        assert complete_receipt["missing_required"] == []
        assert "formal_draft_authorization_unlock" in complete.json()["not_claiming"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": change_set_id},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
        provenance_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "provenance_complete"
        )
        assert provenance_prereq["satisfied"] is True
        assert "authorization input" in provenance_prereq["detail"]
        assert all(
            item["code"] != "provenance_incomplete"
            for item in formal_receipt["denials"]
        )
        assert any(
            item["code"] == "local_v1_policy_locked"
            for item in formal_receipt["denials"]
        )
