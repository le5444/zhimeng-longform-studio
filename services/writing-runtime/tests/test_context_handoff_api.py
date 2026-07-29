from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app

from test_knowledge_api import _settings, _write_project  # type: ignore[import-not-found]
from test_skill_handoff_api import (  # type: ignore[import-not-found]
    _approve,
    _formalize_three_card_batch,
    _prepare,
)


def _run_o2d_handoff(
    client: TestClient,
    project: dict,
    source_import_id: str,
) -> dict:
    prepared = _prepare(client, project, source_import_id)
    _approve(client, project["project_id"], prepared["handoff_id"])
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/skill-handoffs/novel-distillation-kb/run",
        json={"expected_handoff_id": prepared["handoff_id"]},
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["status"] == "succeeded"
    assert record["result"]["downstream_target"] == "novel-kb-manager.import-distillation-index"
    return record


def _prepare_context_handoff(client: TestClient, project: dict) -> dict:
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/prepare",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "profile": "chapter",
            "stage": "draft",
            "chapter_id": "CH001",
            "task_intent": "把 O2-D 受控 distillation_index 入口装配成写前 Context Pack；不写正文。",
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["status"] == "prepared"
    assert record["downstream"]["capability"] == "assemble-context"
    assert record["activation"]["managed_skill_id"] == "novel-kb.context-handoff"
    assert record["permissions"] == {
        "read": ["bound_project_snapshot", "run_staging"],
        "write": ["run_staging", "runtime_context_store"],
        "network": False,
        "secrets": [],
    }
    assert record["formal_draft_allowed"] is False
    assert record["constraints"]["no_full_card_text_copied"] is True
    assert [entry["card_type"] for entry in record["upstream"]["entries"]] == [
        "work",
        "voice",
        "validator",
    ]
    return record


def _approve_context_handoff(client: TestClient, project_id: str, context_handoff_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/context-handoffs/novel-kb-assemble/approve",
        json={"expected_context_handoff_id": context_handoff_id},
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["status"] == "approved"
    assert record["approval"]["approved_by"] == "author"
    return record


def _run_context_handoff(client: TestClient, project_id: str, context_handoff_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/context-handoffs/novel-kb-assemble/run",
        json={"expected_context_handoff_id": context_handoff_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["record"]


def test_context_handoff_consumes_o2d_index_and_persists_context_pack(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "7" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    restart_settings = _settings(
        tmp_path,
        trusted_project_roots=(tmp_path / "projects",),
    )
    try:
        o2d = _run_o2d_handoff(client, project, source_import_id)
        prepared = _prepare_context_handoff(client, project)
        assert prepared["upstream"]["source_handoff_id"] == o2d["handoff_id"]
        assert prepared["upstream"]["distillation_index_hash"] == o2d["result"]["distillation_index_hash"]
        assert prepared["upstream"]["no_full_card_text_copied"] is True

        _approve_context_handoff(client, project["project_id"], prepared["context_handoff_id"])
        record = _run_context_handoff(client, project["project_id"], prepared["context_handoff_id"])
        assert record["status"] == "succeeded"
        assert record["context_pack_id"].startswith("context-pack-o2e-")
        assert record["context_pack_hash"] and len(record["context_pack_hash"]) == 64
        assert record["route_receipt_id"].startswith("nlcl-route-")
        assert record["route_receipt_hash"] and len(record["route_receipt_hash"]) == 64
        assert record["result"]["downstream_target"] == "novel-kb-manager.assemble-context"
        assert record["result"]["route_health"] == "ready"
        assert record["result"]["route_evidence_status"] == "not_validated"
        assert record["result"]["constraints"] == {
            "no_full_card_text_copied": True,
            "formal_draft_allowed": False,
            "project_truth_not_written": True,
            "route_health_ready_is_not_validated": True,
            "writes_formal_prose": False,
        }
        assert [item["card_type"] for item in record["result"]["selected_entries"]] == [
            "work",
            "voice",
            "validator",
        ]
        artifact_path = Path(record["output_artifact_path"])
        assert artifact_path.is_file()
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["schema"] == "zhimeng.o2e-context-pack-intent.v1"

        pack_response = client.get(f"/api/v1/agent/context-packs/{record['context_pack_id']}")
        assert pack_response.status_code == 200, pack_response.text
        pack = pack_response.json()["pack"]
        assert pack["pack_hash"] == record["context_pack_hash"]
        assert pack["project_id"] == project["project_id"]
        assert pack["project_revision"] == project["revision"]
        assert pack["project_fingerprint"] == project["fingerprint"]
        assert pack["knowledge_fingerprint"] == o2d["result"]["distillation_index_hash"]
        assert len(pack["included"]) == 3
        assert all(item["source_id"].startswith("knowledge-card:") for item in pack["included"])

        route_response = client.get(
            f"/api/v1/agent/context-packs/{record['context_pack_id']}/nlcl-route"
        )
        assert route_response.status_code == 200, route_response.text
        route = route_response.json()
        assert route["route_receipt_id"] == record["route_receipt_id"]
        assert route["receipt_hash"] == record["route_receipt_hash"]
        assert route["source"] == "external_distillation"
        assert route["status"] == "ready"
        assert route["formal_draft_allowed"] is False
        assert route["project_truth_precedence"] is True
        assert route["whole_library_load_forbidden"] is True
        assert {item["card_type"] for item in route["selected_cards"]} == {
            "work",
            "voice",
            "validator",
        }
        sessions_response = client.get(
            f"/api/v1/projects/{project['project_id']}/agent/sessions"
        )
        assert sessions_response.status_code == 200, sessions_response.text
        context_session = next(
            item
            for item in sessions_response.json()["sessions"]
            if item["title"] == "O2-E assemble-context"
        )
        assert context_session["executor"] == {
            "executor_id": "executor.context-handoff-runtime",
            "kind": "embedded",
            "implementation": "runtime-context-handoff-compiler",
            "version": "0.1.0",
            "protocol_version": "1",
            "instance_id": "runtime.context-handoff",
        }
        assert context_session["capabilities"] == {
            "protocol_version": "1",
            "names": ["checkpoints", "skills", "artifacts"],
            "extensions": {},
        }

        recovered = client.get(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble"
        )
        assert recovered.status_code == 200, recovered.text
        recovered_record = recovered.json()["record"]
        assert recovered_record["context_handoff_id"] == record["context_handoff_id"]
        assert recovered_record["context_pack_hash"] == record["context_pack_hash"]
        assert recovered_record["route_receipt_hash"] == record["route_receipt_hash"]
        client.__exit__(None, None, None)
        client = None
        with TestClient(create_app(restart_settings)) as restarted:
            restarted_handoff = restarted.get(
                f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble"
            )
            assert restarted_handoff.status_code == 200, restarted_handoff.text
            restarted_record = restarted_handoff.json()["record"]
            assert restarted_record["context_handoff_id"] == record["context_handoff_id"]
            assert restarted_record["context_pack_hash"] == record["context_pack_hash"]
            assert restarted_record["route_receipt_hash"] == record["route_receipt_hash"]

            restarted_pack = restarted.get(
                f"/api/v1/agent/context-packs/{record['context_pack_id']}"
            )
            assert restarted_pack.status_code == 200, restarted_pack.text
            assert restarted_pack.json()["pack"]["pack_hash"] == record["context_pack_hash"]

            restarted_route = restarted.get(
                f"/api/v1/agent/context-packs/{record['context_pack_id']}/nlcl-route"
            )
            assert restarted_route.status_code == 200, restarted_route.text
            assert restarted_route.json()["receipt_hash"] == record["route_receipt_hash"]
    finally:
        if client is not None:
            client.__exit__(None, None, None)


def test_context_handoff_fails_closed_without_author_approval(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "8" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    try:
        _run_o2d_handoff(client, project, source_import_id)
        prepared = _prepare_context_handoff(client, project)
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/run",
            json={"expected_context_handoff_id": prepared["context_handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "context_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_context_handoff_fails_closed_when_disabled(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "9" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    try:
        _run_o2d_handoff(client, project, source_import_id)
        prepared = _prepare_context_handoff(client, project)
        disabled = client.post(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/disable",
            json={"expected_context_handoff_id": prepared["context_handoff_id"]},
        )
        assert disabled.status_code == 200, disabled.text
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/run",
            json={"expected_context_handoff_id": prepared["context_handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "context_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_context_handoff_fails_closed_on_stale_project_identity(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "a" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    try:
        _run_o2d_handoff(client, project, source_import_id)
        prepared = _prepare_context_handoff(client, project)
        _approve_context_handoff(client, project["project_id"], prepared["context_handoff_id"])
        project_root = Path(project["root_path"])
        (project_root / "01_项目定位" / "O2E新增.md").write_text(
            "触发 O2-E project revision/fingerprint 变化。",
            encoding="utf-8",
        )
        refreshed = client.post("/api/v1/projects/inspect", json={"root_path": str(project_root)})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["project"]["fingerprint"] != project["fingerprint"]
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/run",
            json={"expected_context_handoff_id": prepared["context_handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "context_handoff_conflict"
    finally:
        client.__exit__(None, None, None)


def test_context_handoff_fails_closed_when_o2d_artifact_is_tampered(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "b" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    try:
        o2d = _run_o2d_handoff(client, project, source_import_id)
        prepared = _prepare_context_handoff(client, project)
        _approve_context_handoff(client, project["project_id"], prepared["context_handoff_id"])
        artifact_path = Path(o2d["output_artifact_path"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["entries"][0]["route_terms"].append("tampered-after-approval")
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        response = client.post(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble/run",
            json={"expected_context_handoff_id": prepared["context_handoff_id"]},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "context_handoff_conflict"
        latest = client.get(
            f"/api/v1/projects/{project['project_id']}/context-handoffs/novel-kb-assemble"
        ).json()["record"]
        assert latest["status"] == "failed"
        assert latest["error_code"] in {"upstream_stale", "upstream_invalid"}
    finally:
        client.__exit__(None, None, None)


def test_context_handoff_prepare_fails_until_o2d_index_exists(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / "o2e-no-upstream")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    with TestClient(create_app(settings)) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200, inspected.text
        project_record = inspected.json()["project"]
        response = client.post(
            f"/api/v1/projects/{project_record['project_id']}/context-handoffs/novel-kb-assemble/prepare",
            json={
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "context_handoff_invalid"
