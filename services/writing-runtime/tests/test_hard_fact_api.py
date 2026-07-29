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


def _ready_change_set(client: TestClient, project: Path, after_content: str) -> tuple[str, dict]:
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
            "title": "hard fact",
            "summary": "evaluate hard-fact gate",
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "origin": "manual",
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": after_content,
                    "rationale": "Hard-fact Gate evaluation fixture.",
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
    return project_id, ready.json()


def test_hard_fact_gate_pass_becomes_formal_draft_authorization_input(
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
        project_id, ready = _ready_change_set(
            client,
            project,
            "第一章正文，林见川推开门。\n",
        )
        empty = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/hard-fact-gate"
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["receipt"] is None

        evaluated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/hard-fact-gate/evaluate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        receipt = payload["receipt"]
        assert payload["schema"] == "zhimeng.hard-fact-gate-response.v1"
        assert receipt["status"] == "passed"
        assert receipt["authorization_input_ready"] is True
        assert receipt["authorization_unlock"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert "formal_draft_authorization_unlock" in payload["not_claiming"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
        hard_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "hard_fact_gate_pass"
        )
        assert hard_prereq["satisfied"] is True
        assert "authorization input" in hard_prereq["detail"]
        assert all(
            item["code"] != "hard_fact_gate_incomplete"
            for item in formal_receipt["denials"]
        )


def test_hard_fact_gate_fail_blocks_authorization_input(tmp_path: Path) -> None:
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
        project_id, ready = _ready_change_set(
            client,
            project,
            "第一章正文 TODO 【待定】\n",
        )
        evaluated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/hard-fact-gate/evaluate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        receipt = evaluated.json()["receipt"]
        assert receipt["status"] == "failed"
        assert receipt["authorization_input_ready"] is False
        assert receipt["signal_counts"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        hard_prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "hard_fact_gate_pass"
        )
        assert hard_prereq["satisfied"] is False
        assert any(
            item["code"] == "hard_fact_gate_incomplete"
            for item in formal_receipt["denials"]
        )
