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


def test_eval_corpus_export_redacts_and_writes_package(tmp_path: Path) -> None:
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
                "title": "eval corpus",
                "summary": "export desensitized eval corpus",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文完成。\n",
                        "rationale": "Create applied change set for eval corpus export.",
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

        # Plant a desensitizable gate receipt with an absolute path field.
        artifact_root = db_path.parent / "change-sets" / change_set_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "hard-fact-gate-receipt.json").write_text(
            json.dumps(
                {
                    "schema": "zhimeng.hard-fact-gate-receipt.v1",
                    "receipt_id": "hard-fact-gate-" + ("1" * 32),
                    "status": "passed",
                    "absolute_path": str(project / "secret.txt"),
                    "root_path": str(project),
                    "api_key": "should-not-leak",
                    "relative_path": "05_正文内容/第一卷/CH001.md",
                    "formal_draft_allowed": False,
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
        body = exported.json()
        assert body["schema"] == "zhimeng.eval-corpus-export-response.v1"
        receipt = body["receipt"]
        assert receipt["formal_draft_allowed"] is False
        assert receipt["training_ready"] is False
        assert receipt["formal_knowledge_contaminated"] is False
        assert receipt["artifact_count"] >= 1
        assert any(item["kind"] == "hard_fact_gate" and item["present"] for item in receipt["artifacts"])
        assert "training_dataset_ready" in body["not_claiming"]

        package = body["package"]
        assert package is not None
        hard = package["artifacts"]["hard_fact_gate"]
        assert hard["api_key"] == "[redacted]"
        assert hard["absolute_path"] == "[redacted]"
        assert hard["root_path"] == "[redacted]"
        assert hard["relative_path"] == "05_正文内容/第一卷/CH001.md"

        export_file = project / "07_运营数据" / "eval_corpus" / f"{change_set_id}.json"
        assert export_file.is_file()
        on_disk = json.loads(export_file.read_text(encoding="utf-8"))
        assert on_disk["training_ready"] is False
        assert on_disk["artifacts"]["hard_fact_gate"]["api_key"] == "[redacted]"

        fetched = client.get(f"/api/v1/change-sets/{change_set_id}/eval-corpus-export")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["receipt_id"] == receipt["receipt_id"]
