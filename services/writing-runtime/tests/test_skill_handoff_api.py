from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SqliteStore

from test_knowledge_api import (  # type: ignore[import-not-found]
    _reviewed_validator_card,
    _reviewed_voice_card,
    _reviewed_work_card,
    _seed_source_import,
    _settings,
    _write_project,
)


def _formalize_three_card_batch(
    tmp_path: Path,
    *,
    source_import_id: str = "source-import-" + "1" * 32,
) -> tuple[TestClient, dict, dict, dict]:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / "o2d-handoff-novel")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    formal_cards = {
        "work": _reviewed_work_card("work-o2d-handoff"),
        "voice": _reviewed_voice_card("voice-o2d-handoff"),
        "validator": _reviewed_validator_card("validator-o2d-handoff"),
    }
    draft_cards = []
    for card in formal_cards.values():
        draft = deepcopy(card)
        draft["draft_candidate"] = True
        draft["requires_human_distillation"] = True
        draft_cards.append(draft)
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=source_import_id,
        card_id="unused-o2d",
        cards_payload=draft_cards,
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
    assert inspected.status_code == 200, inspected.text
    project_record = inspected.json()["project"]
    project_id = project_record["project_id"]
    knowledge_path = f"/api/v1/projects/{project_id}/knowledge"
    imported = client.post(f"{knowledge_path}/source-imports/{source_import_id}/candidates")
    assert imported.status_code == 201, imported.text
    assert {item["card_type"] for item in imported.json()["candidates"]} == {"work", "voice", "validator"}

    for candidate in imported.json()["candidates"]:
        reviewed = client.put(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
                "card": formal_cards[candidate["card_type"]],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        promoted = client.post(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}/promote",
            json={
                "expected_revision": reviewed.json()["revision"],
                "expected_content_hash": reviewed.json()["content_hash"],
                "replace_active": False,
            },
        )
        assert promoted.status_code == 200, promoted.text

    terminal = client.get(f"{knowledge_path}/candidates", params={"source_import_id": source_import_id})
    assert terminal.status_code == 200, terminal.text
    finalized = client.post(
        f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
        json={
            "expected_source_receipt_hash": receipt.receipt_hash,
            "expected_draft_hash": receipt.cards_draft_hash,
            "expected_candidate_set_hash": terminal.json()["candidate_set_hash"],
        },
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["schema"] == "zhimeng.knowledge-formalization-receipt.v2"
    return client, project_record, finalized.json(), terminal.json()


def _prepare(client: TestClient, project: dict, source_import_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/prepare",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "source_import_id": source_import_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["record"]


def _approve(client: TestClient, project_id: str, handoff_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/skill-handoffs/novel-distillation-kb/approve",
        json={"expected_handoff_id": handoff_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["record"]


def test_managed_skill_handoff_runs_and_recovers_from_runtime_state(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "2" * 32
    client, project, finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    try:
        initial = client.get(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb"
        )
        assert initial.status_code == 200, initial.text
        prepared = _prepare(client, project, source_import_id)
        assert prepared["status"] == "prepared"
        assert prepared["upstream"]["formalization_receipt_id"] == finalized["formalization_receipt_id"]
        assert prepared["upstream"]["formalization_receipt_hash"] == finalized["receipt_hash"]
        assert prepared["activation"]["managed_skill_id"] == "novel-distillation.kb-handoff"
        assert prepared["permissions"] == {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        }
        assert prepared["formal_draft_allowed"] is False
        assert [card["card_type"] for card in prepared["upstream"]["cards"]] == ["work", "voice", "validator"]

        approved = _approve(client, project["project_id"], prepared["handoff_id"])
        assert approved["status"] == "approved"
        ran = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert ran.status_code == 200, ran.text
        record = ran.json()["record"]
        assert record["status"] == "succeeded"
        assert record["skill_receipt_id"].startswith("skill-receipt-")
        assert record["skill_receipt_hash"] and len(record["skill_receipt_hash"]) == 64
        assert record["result"]["status"] == "ready_for_downstream"
        assert record["result"]["downstream_target"] == "novel-kb-manager.import-distillation-index"
        assert record["result"]["counts"] == {"work": 1, "voice": 1, "validator": 1}
        assert [item["card_type"] for item in record["result"]["entries"]] == ["work", "voice", "validator"]
        artifact_path = Path(record["output_artifact_path"])
        assert artifact_path.is_file()
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["target_skill_id"] == "novel-kb-manager"
        assert artifact["target_capability"] == "import-distillation-index"
        assert artifact["no_full_card_text_copied"] is True
        assert all("summary" not in item for item in artifact["entries"])

        recovered = client.get(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb"
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["record"]["handoff_id"] == record["handoff_id"]
        assert recovered.json()["record"]["skill_receipt_hash"] == record["skill_receipt_hash"]
    finally:
        client.__exit__(None, None, None)


def test_handoff_fails_closed_without_author_approval(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "3" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(tmp_path, source_import_id=source_import_id)
    try:
        prepared = _prepare(client, project, source_import_id)
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_handoff_fails_closed_when_disabled(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "4" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(tmp_path, source_import_id=source_import_id)
    try:
        prepared = _prepare(client, project, source_import_id)
        disabled = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/disable",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert disabled.status_code == 200, disabled.text
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_handoff_fails_closed_on_stale_project_identity(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "5" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(tmp_path, source_import_id=source_import_id)
    try:
        prepared = _prepare(client, project, source_import_id)
        _approve(client, project["project_id"], prepared["handoff_id"])
        project_root = Path(project["root_path"])
        (project_root / "01_项目定位" / "新增.md").write_text("触发 revision 变化。", encoding="utf-8")
        refreshed = client.post("/api/v1/projects/inspect", json={"root_path": str(project_root)})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["project"]["fingerprint"] != project["fingerprint"]
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_handoff_fails_closed_when_upstream_receipt_is_tampered(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "6" * 32
    client, project, finalized, _terminal = _formalize_three_card_batch(tmp_path, source_import_id=source_import_id)
    try:
        prepared = _prepare(client, project, source_import_id)
        _approve(client, project["project_id"], prepared["handoff_id"])
        store = client.app.state.runtime.store
        with store.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM knowledge_formalization_receipt WHERE id = ?",
                (finalized["formalization_receipt_id"],),
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["receipt_hash"] = "0" * 64
            connection.execute(
                "UPDATE knowledge_formalization_receipt SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), finalized["formalization_receipt_id"]),
            )
            connection.commit()
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
            json={"expected_handoff_id": prepared["handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "skill_handoff_conflict"
        latest = client.get(
            f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb"
        ).json()["record"]
        assert latest["status"] == "failed"
        assert latest["error_code"] in {"upstream_stale", "upstream_invalid"}
    finally:
        client.__exit__(None, None, None)
