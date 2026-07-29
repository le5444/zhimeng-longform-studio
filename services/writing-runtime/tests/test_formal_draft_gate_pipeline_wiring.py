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


def _ready_change_set(
    client: TestClient,
    project: Path,
    after_content: str,
    *,
    gate_required: bool = False,
) -> tuple[str, dict]:
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
            "title": "formal draft pipeline",
            "summary": "wire gate pipeline into formal draft and apply",
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "origin": "manual",
            "gate_required": gate_required,
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": after_content,
                    "rationale": "Pipeline formal-draft fixture.",
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


def test_formal_draft_prefers_gate_pipeline_when_present(tmp_path: Path) -> None:
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
            "第一章正文完成。\n",
        )
        pipeline = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/gate-pipeline/evaluate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert pipeline.status_code == 200, pipeline.text
        pipeline_receipt = pipeline.json()["receipt"]
        assert pipeline_receipt["status"] == "complete"
        assert pipeline_receipt["formal_draft_allowed"] is False
        assert pipeline_receipt["quality_assessed"] is False

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        receipt = formal.json()["receipt"]
        assert receipt["decision"] == "denied"
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False

        hard = next(
            item for item in receipt["prerequisites"] if item["code"] == "hard_fact_gate_pass"
        )
        assert hard["satisfied"] is True
        assert "ordered gate pipeline" in hard["detail"]

        pipeline_prereq = next(
            item
            for item in receipt["prerequisites"]
            if item["code"] == "gate_pipeline_complete"
        )
        assert pipeline_prereq["satisfied"] is True
        assert all(
            item["code"] != "gate_pipeline_incomplete" for item in receipt["denials"]
        )
        assert all(
            item["code"] != "hard_fact_gate_incomplete" for item in receipt["denials"]
        )
        literary = next(
            item
            for item in receipt["prerequisites"]
            if item["code"] == "literary_quality_gate"
        )
        assert literary["satisfied"] is False


def test_formal_draft_defers_gate_pipeline_when_absent(tmp_path: Path) -> None:
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
            "第一章正文完成。\n",
        )
        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready["change_set_id"]},
        )
        assert formal.status_code == 200, formal.text
        receipt = formal.json()["receipt"]
        pipeline_prereq = next(
            item
            for item in receipt["prerequisites"]
            if item["code"] == "gate_pipeline_complete"
        )
        assert pipeline_prereq["satisfied"] is False
        assert "deferred" in pipeline_prereq["detail"]
        assert all(
            item["code"] != "gate_pipeline_incomplete" for item in receipt["denials"]
        )


def test_apply_with_post_draft_required_fails_closed_on_blocked_pipeline(
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
        _, ready = _ready_change_set(
            client,
            project,
            "第一章正文 TODO 待补事实\n",
            gate_required=True,
        )
        # Clean post-draft gate content shape still needs a passing gate before apply.
        # For blocked hard-fact content, post-draft may pass while pipeline blocks.
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "passed"

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "gate_pipeline_blocked"


def test_apply_with_post_draft_required_still_applies_when_pipeline_complete(
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
        _, ready = _ready_change_set(
            client,
            project,
            "第一章正文完成，夜雨落在旧站。\n",
            gate_required=True,
        )
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "passed"

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "applied"

        evidence = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/writeback-evidence"
        )
        assert evidence.status_code == 200, evidence.text
        assert evidence.json()["receipt"]["formal_draft_allowed"] is False

        pipeline = client.get(f"/api/v1/change-sets/{ready['change_set_id']}/gate-pipeline")
        assert pipeline.status_code == 200, pipeline.text
        receipt = pipeline.json()["receipt"]
        assert receipt is not None
        assert receipt["status"] == "complete"
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
