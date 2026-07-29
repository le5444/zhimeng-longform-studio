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


def _ready_or_applied(
    client: TestClient,
    project: Path,
    after_content: str,
    *,
    apply: bool,
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
            "title": "gate pipeline",
            "summary": "evaluate ordered gate pipeline",
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "origin": "manual",
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": after_content,
                    "rationale": "Gate pipeline fixture.",
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
    body = ready.json()
    if apply:
        applied = client.post(
            f"/api/v1/change-sets/{body['change_set_id']}/apply",
            json={"expected_review_revision": body["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
    return body["change_set_id"], body


def test_gate_pipeline_runs_literary_then_hard_fact_and_stays_locked(
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
        change_set_id, body = _ready_or_applied(
            client,
            project,
            "第一章正文完成。\n",
            apply=True,
        )
        evaluated = client.post(
            f"/api/v1/change-sets/{change_set_id}/gate-pipeline/evaluate",
            json={
                "expected_review_revision": body["review_revision"],
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        assert payload["schema"] == "zhimeng.gate-pipeline-response.v1"
        receipt = payload["receipt"]
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        assert receipt["authorization_unlock"] is False
        assert receipt["status"] == "complete"
        assert receipt["authorization_input_ready"] is True
        codes = [item["code"] for item in receipt["steps"]]
        assert codes == [
            "post_draft_gate",
            "literary_quality_gate",
            "llm_literary_score",
            "external_blind_review_matrix",
            "hard_fact_gate",
        ]
        assert receipt["hard_fact_receipt_id"]
        assert receipt["literary_quality_receipt_id"]
        assert receipt["llm_literary_score_receipt_id"]
        assert receipt["external_blind_review_matrix_ready"] is False
        assert receipt["policy_version"] == "local-v1-gate-pipeline-2"
        assert "literary_quality_score" in payload["not_claiming"]
        assert "llm_literary_score_quality_unlock" in payload["not_claiming"]
        assert "external_blind_review_quality_unlock" in payload["not_claiming"]
        llm_step = next(item for item in receipt["steps"] if item["code"] == "llm_literary_score")
        assert llm_step["status"] == "offline_stub"
        assert llm_step["required"] is False
        blind_step = next(
            item for item in receipt["steps"] if item["code"] == "external_blind_review_matrix"
        )
        assert blind_step["status"] == "missing_evidence"
        assert blind_step["required"] is False

        fetched = client.get(f"/api/v1/change-sets/{change_set_id}/gate-pipeline")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["receipt_id"] == receipt["receipt_id"]


def test_gate_pipeline_blocks_on_hard_fact_failure(tmp_path: Path) -> None:
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
        change_set_id, body = _ready_or_applied(
            client,
            project,
            "第一章正文 TODO 待补事实\n",
            apply=False,
        )
        evaluated = client.post(
            f"/api/v1/change-sets/{change_set_id}/gate-pipeline/evaluate",
            json={"expected_review_revision": body["review_revision"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        receipt = evaluated.json()["receipt"]
        assert receipt["status"] == "blocked"
        assert receipt["authorization_input_ready"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["quality_assessed"] is False
        hard = next(item for item in receipt["steps"] if item["code"] == "hard_fact_gate")
        assert hard["status"] in {"failed", "missing_inputs"}
