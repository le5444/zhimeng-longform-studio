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


def test_eval_corpus_export_includes_gate_pipeline_when_present(tmp_path: Path) -> None:
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
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200, inspected.text
        snapshot = inspected.json()
        project_id = snapshot["project"]["project_id"]
        chapter = next(
            item for item in snapshot["files"] if item["relative_path"].endswith("CH001.md")
        )
        created = client.post(
            f"/api/v1/projects/{project_id}/change-sets",
            json={
                "title": "export pipeline",
                "summary": "include gate pipeline in eval corpus",
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

        pipeline = client.post(
            f"/api/v1/change-sets/{change_set_id}/gate-pipeline/evaluate",
            json={"expected_review_revision": ready.json()["review_revision"]},
        )
        assert pipeline.status_code == 200, pipeline.text
        assert pipeline.json()["receipt"]["formal_draft_allowed"] is False

        exported = client.post(
            f"/api/v1/change-sets/{change_set_id}/eval-corpus-export",
            json={"include_gates": True},
        )
        assert exported.status_code == 200, exported.text
        receipt = exported.json()["receipt"]
        assert any(
            item["kind"] == "gate_pipeline" and item["present"] for item in receipt["artifacts"]
        )
        package = exported.json()["package"]
        assert package["artifacts"]["gate_pipeline"]["quality_assessed"] is False
        assert package["artifacts"]["gate_pipeline"]["formal_draft_allowed"] is False
