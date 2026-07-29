from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from test_skill_handoff_api import _formalize_three_card_batch  # type: ignore[import-not-found]


CHAIN_PREFIX = "/api/v1/projects/{project_id}/adjacent-skill-chains/novel-distillation-kb-to-context"


def _write_creation_success(
    runtime_root: Path,
    *,
    project_id: str,
    revision: int,
    fingerprint: str,
) -> str:
    chain_id = "managed-skill-chain-" + ("c" * 32)
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema": "zhimeng.managed-skill-chain-run.v1",
        "chain_id": chain_id,
        "chain_key": "creation-suite-continuous",
        "status": "succeeded",
        "project": {
            "project_id": project_id,
            "revision": revision,
            "fingerprint": fingerprint,
            "root_path": "C:/tmp/book",
        },
        "steps": [
            {
                "step_id": "audit",
                "ordinal": 1,
                "title": "Project audit",
                "skill_id": "novel-creation-suite.project-audit",
                "capability": "project-audit",
                "status": "succeeded",
                "input_payload": {},
                "input_hash": "a" * 64,
                "skill_receipt_id": "skill-receipt-" + ("a" * 32),
                "skill_receipt_hash": "b" * 64,
                "result_hash": "c" * 64,
                "domain_status": "healthy",
                "error_code": None,
                "error_message": None,
            },
            {
                "step_id": "prewrite",
                "ordinal": 2,
                "title": "Prewrite gate",
                "skill_id": "novel-creation-suite.prewrite-gate",
                "capability": "prewrite-gate",
                "status": "succeeded",
                "input_payload": {},
                "input_hash": "d" * 64,
                "skill_receipt_id": "skill-receipt-" + ("b" * 32),
                "skill_receipt_hash": "e" * 64,
                "result_hash": "f" * 64,
                "domain_status": "healthy",
                "error_code": None,
                "error_message": None,
            },
        ],
        "approval": {
            "approved": True,
            "approved_by": "author",
            "approved_at": now,
            "risk_acknowledged": True,
        },
        "permissions": {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        },
        "risk_dossier": None,
        "risk_approval_token": None,
        "workflow_plan": None,
        "workflow_checkpoints": [],
        "formal_draft_allowed": False,
        "current_step_ordinal": 2,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    root = runtime_root / "managed-skill-chains"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{chain_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return chain_id


def test_four_skills_continuous_incomplete_without_linked_stages(tmp_path: Path) -> None:
    source_import_id = "source-import-" + ("f" * 32)
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    try:
        evaluated = client.post(
            f"/api/v1/projects/{project_id}/four-skills-continuous/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        receipt = evaluated.json()["receipt"]
        assert receipt["status"] == "incomplete"
        assert receipt["authorization_input_ready"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert "adjacent_o2" in receipt["missing_required"]
        assert "creation_suite" in receipt["missing_required"]
        assert "four_skills_generic_exactly_once" in evaluated.json()["not_claiming"]
    finally:
        client.__exit__(None, None, None)


def test_four_skills_continuous_complete_with_linked_real_adjacent_and_creation(
    tmp_path: Path,
) -> None:
    source_import_id = "source-import-" + ("e" * 32)
    client, project, finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "profile": "chapter",
                "stage": "draft",
                "chapter_id": "CH001",
                "task_intent": "四 Skills 连续证据：真实 adjacent O2 链。",
            },
        )
        assert prepared.status_code == 200, prepared.text
        chain_id = prepared.json()["record"]["chain_id"]
        approved = client.post(
            f"{base}/approve",
            json={"expected_chain_id": chain_id},
        )
        assert approved.status_code == 200, approved.text
        ran = client.post(
            f"{base}/run",
            json={"expected_chain_id": chain_id},
        )
        assert ran.status_code == 200, ran.text
        assert ran.json()["record"]["status"] == "succeeded"
        assert ran.json()["record"]["source_import_id"] == source_import_id

        runtime_root = Path(client.app.state.runtime.store.path).parent
        creation_id = _write_creation_success(
            runtime_root,
            project_id=project_id,
            revision=project["revision"],
            fingerprint=project["fingerprint"],
        )

        evaluated = client.post(
            f"/api/v1/projects/{project_id}/four-skills-continuous/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "adjacent_chain_id": chain_id,
                "managed_skill_chain_id": creation_id,
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        receipt = payload["receipt"]
        assert payload["schema"] == "zhimeng.four-skills-continuous-response.v1"
        assert receipt["status"] == "complete"
        assert receipt["authorization_input_ready"] is True
        assert receipt["authorization_unlock"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["missing_required"] == []
        assert receipt["source_import_id"] == source_import_id
        assert receipt["formalization_receipt_id"] == finalized["formalization_receipt_id"]
        assert receipt["adjacent_chain_id"] == chain_id
        assert receipt["creation_suite_chain_id"] == creation_id
        codes = [item["code"] for item in receipt["stages"]]
        assert codes == [
            "source_ingest",
            "distillation_formalization",
            "adjacent_o2",
            "creation_suite",
        ]
        assert all(item["present"] and item["linked"] for item in receipt["stages"])
        assert "four_skills_generic_exactly_once" in payload["not_claiming"]
        assert "formal_draft_authorization_unlock" in payload["not_claiming"]

        persisted = (
            runtime_root / "four-skills-continuous" / project_id / "receipt.json"
        )
        assert persisted.is_file()
    finally:
        client.__exit__(None, None, None)
