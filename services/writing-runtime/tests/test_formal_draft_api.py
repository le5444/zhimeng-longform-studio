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
    (volume / "CH001.md").write_text("第一章正文", encoding="utf-8")


def test_formal_draft_authorization_is_locked_denied(tmp_path: Path) -> None:
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
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]
        revision = inspected.json()["project"]["revision"]
        fingerprint = inspected.json()["project"]["fingerprint"]

        evaluated = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        assert payload["schema"] == "zhimeng.formal-draft-authorization-response.v1"
        receipt = payload["receipt"]
        assert receipt["decision"] == "denied"
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False
        assert receipt["policy_version"] == "local-v1-locked-1"
        assert any(item["code"] == "local_v1_policy_locked" for item in receipt["denials"])
        assert any(
            item["code"] == "four_skills_continuous_o2" and item["satisfied"] is False
            for item in receipt["prerequisites"]
        )
        assert "formal_prose_writeback" in payload["not_claiming"]

        stale = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision + 1,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert stale.status_code == 409, stale.text


def test_formal_draft_authorization_binds_on_writeback(tmp_path: Path) -> None:
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
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
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
                "title": "formal-draft bind",
                "summary": "bind formal draft authorization on apply",
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "origin": "manual",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter["file_id"],
                        "after_content": "第一章正文-改\n",
                        "rationale": "Bind formal-draft denial into writeback evidence.",
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
        evidence = client.get(
            f"/api/v1/change-sets/{ready_body['change_set_id']}/writeback-evidence"
        )
        assert evidence.status_code == 200, evidence.text
        receipt = evidence.json()["receipt"]
        assert receipt["formal_draft_allowed"] is False
        auth_id = receipt["formal_draft_authorization_receipt_id"]
        auth_hash = receipt["formal_draft_authorization_receipt_hash"]
        assert auth_id.startswith("formal-draft-auth-")
        assert len(auth_hash) == 64

        evaluated = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={"change_set_id": ready_body["change_set_id"]},
        )
        assert evaluated.status_code == 200, evaluated.text
        auth_receipt = evaluated.json()["receipt"]
        assert auth_receipt["decision"] == "denied"
        assert auth_receipt["formal_draft_allowed"] is False
        assert auth_receipt["writes_project_truth"] is False


def test_writeback_fails_closed_when_formal_draft_authorization_is_not_denied(
    tmp_path: Path,
) -> None:
    from zhimeng_runtime.change_set_service import (
        ChangeSetService,
        ChangeSetWritebackError,
    )
    from zhimeng_runtime.formal_draft_models import FormalDraftDecision
    from zhimeng_runtime.project_reader import ProjectReader

    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    store_path = tmp_path / "runtime.sqlite3"
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=store_path,
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
        project_id = inspected.json()["project"]["project_id"]

    class _GrantingService:
        def evaluate(self, project_id: str, request=None):
            class _Receipt:
                decision = FormalDraftDecision.DENIED
                formal_draft_allowed = True
                writes_project_truth = False
                change_set_id = None
                receipt_id = "formal-draft-auth-" + ("b" * 32)
                receipt_hash = "c" * 64

            receipt = _Receipt()
            receipt.project_id = project_id

            class _Response:
                pass

            response = _Response()
            response.receipt = receipt
            return response

    from zhimeng_runtime.database import SqliteStore

    store = SqliteStore(store_path)
    store.initialize()
    service = ChangeSetService(
        ProjectReader(trusted_roots=(library,)),
        store,
        tmp_path / "change-sets",
        formal_draft_authorization_service=_GrantingService(),  # type: ignore[arg-type]
    )
    try:
        service._require_denied_formal_draft_authorization(project_id=project_id)
        raised = False
    except ChangeSetWritebackError as exc:
        raised = True
        assert "did not deny" in str(exc)
    assert raised

