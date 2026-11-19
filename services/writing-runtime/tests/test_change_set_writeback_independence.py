from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_change_set_api import _client, _file, _inspect
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_reader import ProjectReader


@pytest.mark.parametrize("legacy_evidence", [False, True])
def test_ordinary_save_reload_and_undo_without_formal_draft_evaluation(
    tmp_path: Path, legacy_evidence: bool,
) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    target = project / "README.md"
    before = b"# Project\r\nvalue=1\r\n"
    target.write_bytes(before)
    settings = project / "settings.csv"
    settings.write_bytes(b"key,value\r\nactive,true\r\n")

    with _client(tmp_path, project) as client:
        snapshot = _inspect(client, project)
        created_response = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
            json={
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": "Update project notes", "summary": "Save an ordinary file",
                "origin": "manual",
                "changes": [{
                    "operation": "update", "file_id": _file(snapshot, "README.md")["file_id"],
                    "after_content": "# Project\nvalue=2\n", "rationale": "Update the value",
                }],
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        route = f"/api/v1/change-sets/{created['change_set_id']}"
        applied_response = client.post(
            route + "/apply-direct", json={"expected_review_revision": created["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert applied["status"] == "applied"
        assert target.read_bytes() == before.replace(b"value=1", b"value=2")
        retried = client.post(
            route + "/apply-direct", json={"expected_review_revision": created["review_revision"]},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["writeback"] == applied["writeback"]

    artifacts = tmp_path / "change-sets" / created["change_set_id"]
    evidence_path = artifacts / "writeback-evidence-receipt.json"
    history_path = artifacts / "writeback-evidence-history.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["formal_draft_authorization_receipt_id"] is None
    assert evidence["formal_draft_authorization_receipt_hash"] is None
    assert not (artifacts / "formal-draft-authorization-receipt.json").exists()
    assert evidence["formal_draft_allowed"] is False
    assert evidence["quality_assessed"] is False
    assert evidence["novel_state_synced"] is False
    if legacy_evidence:
        # An older release attached a prose-denial receipt to every file save.
        evidence["formal_draft_authorization_receipt_id"] = "formal-draft-auth-" + "a" * 32
        evidence["formal_draft_authorization_receipt_hash"] = "b" * 64
        evidence["receipt_hash"] = canonical_json_hash({
            key: value for key, value in evidence.items() if key != "receipt_hash"
        })
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        history_path.write_text(json.dumps([evidence]), encoding="utf-8")

    store = SqliteStore(tmp_path / "runtime.sqlite3")
    service = ChangeSetService(ProjectReader(trusted_roots=(project,)), store, tmp_path / "change-sets")
    old = service.get_writeback_evidence(created["change_set_id"])
    assert old.receipt is not None
    assert old.receipt.model_dump(mode="json", by_alias=True) == evidence
    settings.write_bytes(b"key,value\r\nactive,false\r\n")
    with _client(tmp_path, project) as client:
        reloaded = client.get(route)
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["status"] == "applied"
        rolled = client.post(
            route + "/rollback", json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["status"] == "rolled_back"
        assert target.read_bytes() == before
        assert settings.read_bytes() == b"key,value\r\nactive,false\r\n"
    restored = service.get_writeback_evidence(created["change_set_id"])
    assert len(restored.history) == 2
    assert restored.history[0] == old.receipt
    assert restored.receipt is not None
    assert restored.receipt.outcome == "rolled_back"
    assert restored.receipt.formal_draft_authorization_receipt_id is None
