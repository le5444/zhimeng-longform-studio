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


def test_author_decision_satisfies_formal_draft_prerequisite_without_grant(
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
        project_id = inspected.json()["project"]["project_id"]
        revision = inspected.json()["project"]["revision"]
        fingerprint = inspected.json()["project"]["fingerprint"]

        empty = client.get(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/author-decision"
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["receipt"] is None

        before = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert before.status_code == 200, before.text
        before_receipt = before.json()["receipt"]
        author_before = next(
            item
            for item in before_receipt["prerequisites"]
            if item["code"] == "author_explicit_authorization"
        )
        assert author_before["satisfied"] is False
        assert any(
            item["code"] == "author_authorization_missing"
            for item in before_receipt["denials"]
        )

        recorded = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/author-decision",
            json={
                "intent": "request_authorization",
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
                "note": "作者希望进入正式起稿授权评估",
            },
        )
        assert recorded.status_code == 200, recorded.text
        decision = recorded.json()["receipt"]
        assert decision["intent"] == "request_authorization"
        assert decision["authorization_effective"] is False
        assert decision["formal_draft_allowed"] is False
        assert decision["writes_project_truth"] is False
        assert "formal_draft_authorization_grant" in recorded.json()["not_claiming"]

        after = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert after.status_code == 200, after.text
        after_receipt = after.json()["receipt"]
        assert after_receipt["decision"] == "denied"
        assert after_receipt["formal_draft_allowed"] is False
        author_after = next(
            item
            for item in after_receipt["prerequisites"]
            if item["code"] == "author_explicit_authorization"
        )
        assert author_after["satisfied"] is True
        assert "requested formal-draft authorization" in author_after["detail"]
        assert all(
            item["code"] != "author_authorization_missing"
            for item in after_receipt["denials"]
        )
        assert any(
            item["code"] == "local_v1_policy_locked"
            for item in after_receipt["denials"]
        )

        withheld = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/author-decision",
            json={
                "intent": "withhold_authorization",
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert withheld.status_code == 200, withheld.text
        after_withhold = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert after_withhold.status_code == 200, after_withhold.text
        author_withheld = next(
            item
            for item in after_withhold.json()["receipt"]["prerequisites"]
            if item["code"] == "author_explicit_authorization"
        )
        assert author_withheld["satisfied"] is False
        assert any(
            item["code"] == "author_authorization_missing"
            for item in after_withhold.json()["receipt"]["denials"]
        )
