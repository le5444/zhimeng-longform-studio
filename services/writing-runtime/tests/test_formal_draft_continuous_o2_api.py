from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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


def _client(tmp_path: Path) -> tuple[TestClient, dict, Path]:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    db_path = tmp_path / "runtime.sqlite3"
    client = TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=db_path,
                trusted_project_roots=(library,),
                allowed_origins=(),
            )
        )
    )
    client.__enter__()
    inspected = client.post(
        "/api/v1/projects/inspect",
        json={"root_path": str(project)},
    )
    assert inspected.status_code == 200, inspected.text
    return client, inspected.json()["project"], db_path.parent


def _write_adjacent_success(
    runtime_root: Path,
    *,
    project_id: str,
    revision: int,
    fingerprint: str,
) -> str:
    chain_id = "adjacent-chain-" + ("a" * 32)
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema": "zhimeng.adjacent-skill-chain-run.v1",
        "chain_id": chain_id,
        "chain_key": "novel-distillation-kb-to-context",
        "status": "succeeded",
        "project": {
            "project_id": project_id,
            "revision": revision,
            "fingerprint": fingerprint,
        },
        "route": {
            "profile": "chapter",
            "stage": "draft",
            "chapter_id": "CH001",
            "task_intent": "continuous O2 readiness fixture",
        },
        "steps": [
            {
                "step_id": "o2d-kb-handoff",
                "ordinal": 1,
                "title": "O2-D",
                "skill_id": "novel-distillation.kb-handoff",
                "capability": "kb-handoff",
                "status": "succeeded",
                "child_id": "skill-handoff-" + ("b" * 32),
                "result_hash": "c" * 64,
                "error_code": None,
                "error_message": None,
            },
            {
                "step_id": "o2e-context-handoff",
                "ordinal": 2,
                "title": "O2-E",
                "skill_id": "novel-kb.context-handoff",
                "capability": "context-handoff",
                "status": "succeeded",
                "child_id": "context-handoff-" + ("d" * 32),
                "result_hash": "e" * 64,
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
            "read": ["bound_project_snapshot", "run_staging"],
            "write": ["run_staging", "runtime_context_store"],
            "network": False,
            "secrets": [],
        },
        "risk_dossier": None,
        "risk_approval_token": None,
        "workflow_plan": None,
        "workflow_checkpoints": [],
        "formal_draft_allowed": False,
        "source_import_id": None,
        "skill_handoff_id": "skill-handoff-" + ("b" * 32),
        "context_handoff_id": "context-handoff-" + ("d" * 32),
        "context_pack_id": "context-pack-o2e-fixture",
        "context_pack_hash": "f" * 64,
        "route_receipt_id": "nlcl-route-" + ("1" * 32),
        "route_receipt_hash": "2" * 64,
        "current_step_ordinal": 2,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    root = runtime_root / "adjacent-chains"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{chain_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
    (root / f"project-{digest}.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "latest_chain_id": chain_id,
                "updated_at": now,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return chain_id


def _write_managed_creation_success(
    runtime_root: Path,
    *,
    project_id: str,
    revision: int,
    fingerprint: str,
) -> str:
    chain_id = "managed-skill-chain-" + ("9" * 32)
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




def _seed_ready_instruction_chain_handoff(runtime_root: Path) -> dict:
    from zhimeng_runtime.four_skills_raw_adaptation_models import (
        FourSkillsInstructionChainHandoffReceipt,
        FourSkillsInstructionChainHandoffStep,
        FourSkillsRawFamily,
        four_skills_instruction_chain_handoff_receipt_hash,
    )

    families = (
        FourSkillsRawFamily.SOURCE_INGEST,
        FourSkillsRawFamily.DISTILLATION,
        FourSkillsRawFamily.KNOWLEDGE_CONTEXT,
        FourSkillsRawFamily.CREATION_SUITE,
    )
    package_fp = "a" * 64
    batch_hash = "b" * 64
    binding_hash = "c" * 64
    orchestration_hash = "d" * 64
    batch_id = "four-skills-raw-adaptation-" + ("1" * 32)
    binding_id = "four-skills-workflow-binding-" + ("2" * 32)
    orchestration_id = "four-skills-workflow-orchestration-" + ("3" * 32)
    handoff_id = "four-skills-instruction-chain-handoff-" + ("4" * 32)
    steps = [
        FourSkillsInstructionChainHandoffStep(
            family=family,
            ordinal=index,
            skill_id=f"adapted.{family.value}",
            skill_version_id=f"skill-version-{index:02d}" + ("0" * 30),
            package_fingerprint=package_fp,
            runner="instruction_chain_handoff",
            status="prepared",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            detail="seeded prepared instruction step",
        )
        for index, family in enumerate(families, start=1)
    ]
    handed_off_at = datetime.now(UTC)
    temp = FourSkillsInstructionChainHandoffReceipt.model_construct(
        schema_id="zhimeng.four-skills-instruction-chain-handoff-receipt.v1",
        handoff_id=handoff_id,
        batch_id=batch_id,
        batch_hash=batch_hash,
        binding_id=binding_id,
        binding_hash=binding_hash,
        orchestration_id=orchestration_id,
        orchestration_hash=orchestration_hash,
        status="ready",
        workflow_surface="generic_instruction_workflow",
        steps=steps,
        step_count=4,
        prepared_step_count=4,
        instruction_chain_ready=True,
        managed_skill_execution=False,
        raw_skill_md_execution=False,
        four_skills_generic_exactly_once=False,
        formal_draft_allowed=False,
        writes_project_truth=False,
        policy_version="local-v1-four-skills-instruction-chain-handoff-1",
        detail="seeded ready instruction chain handoff",
        handed_off_at=handed_off_at,
        handoff_hash="0" * 64,
    )
    handoff_hash = four_skills_instruction_chain_handoff_receipt_hash(temp)
    receipt = FourSkillsInstructionChainHandoffReceipt(
        handoff_id=handoff_id,
        batch_id=batch_id,
        batch_hash=batch_hash,
        binding_id=binding_id,
        binding_hash=binding_hash,
        orchestration_id=orchestration_id,
        orchestration_hash=orchestration_hash,
        status="ready",
        workflow_surface="generic_instruction_workflow",
        steps=steps,
        step_count=4,
        prepared_step_count=4,
        instruction_chain_ready=True,
        managed_skill_execution=False,
        raw_skill_md_execution=False,
        four_skills_generic_exactly_once=False,
        formal_draft_allowed=False,
        writes_project_truth=False,
        policy_version="local-v1-four-skills-instruction-chain-handoff-1",
        detail="seeded ready instruction chain handoff",
        handed_off_at=handed_off_at,
        handoff_hash=handoff_hash,
    )
    target = runtime_root / "four-skills-raw-adaptations" / batch_id / "instruction-chain-handoff.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "handoff": receipt.model_dump(mode="json", by_alias=True),
    }


def _bind_instruction_chain_handoff(client, project: dict, prepared: dict) -> dict:
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/instruction-chain-handoff-bindings",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "batch_id": prepared["batch_id"],
            "batch_hash": prepared["batch_hash"],
            "handoff_id": prepared["handoff"]["handoff_id"],
            "handoff_hash": prepared["handoff"]["handoff_hash"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["receipt"]

def test_continuous_o2_incomplete_without_chain_evidence(tmp_path: Path) -> None:
    client, project, _runtime = _client(tmp_path)
    project_id = project["project_id"]
    try:
        evaluated = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/continuous-o2/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        payload = evaluated.json()
        assert payload["schema"] == "zhimeng.formal-draft-continuous-o2-response.v1"
        receipt = payload["receipt"]
        assert receipt["status"] == "incomplete"
        assert receipt["authorization_input_ready"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["authorization_unlock"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert "four_skills_continuous_linked" in receipt["missing_required"]
        assert "adjacent_o2_chain_succeeded" in receipt["missing_required"]
        assert "four_skills_generic_exactly_once" in payload["not_claiming"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
            },
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "four_skills_continuous_o2"
        )
        assert prereq["satisfied"] is False
        assert any(
            item["code"] == "four_skills_chain_incomplete"
            for item in formal_receipt["denials"]
        )
        assert formal_receipt["formal_draft_allowed"] is False
    finally:
        client.__exit__(None, None, None)


def test_continuous_o2_ready_as_authorization_input_without_unlock(
    tmp_path: Path,
) -> None:
    from test_skill_handoff_api import _formalize_three_card_batch  # type: ignore[import-not-found]

    source_import_id = "source-import-" + ("d" * 32)
    client, project, finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    chain_prefix = (
        f"/api/v1/projects/{project_id}/adjacent-skill-chains/"
        "novel-distillation-kb-to-context"
    )
    try:
        prepared = client.post(
            f"{chain_prefix}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "profile": "chapter",
                "stage": "draft",
                "chapter_id": "CH001",
                "task_intent": "continuous O2 requires linked four-skills evidence",
            },
        )
        assert prepared.status_code == 200, prepared.text
        chain_id = prepared.json()["record"]["chain_id"]
        approved = client.post(
            f"{chain_prefix}/approve",
            json={"expected_chain_id": chain_id},
        )
        assert approved.status_code == 200, approved.text
        ran = client.post(
            f"{chain_prefix}/run",
            json={"expected_chain_id": chain_id},
        )
        assert ran.status_code == 200, ran.text
        assert ran.json()["record"]["status"] == "succeeded"

        runtime_root = Path(client.app.state.runtime.store.path).parent
        managed_id = _write_managed_creation_success(
            runtime_root,
            project_id=project_id,
            revision=project["revision"],
            fingerprint=project["fingerprint"],
        )
        prepared_handoff = _seed_ready_instruction_chain_handoff(runtime_root)
        handoff_binding = _bind_instruction_chain_handoff(client, project, prepared_handoff)

        evaluated = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/continuous-o2/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "adjacent_chain_id": chain_id,
                "managed_skill_chain_id": managed_id,
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        receipt = evaluated.json()["receipt"]
        assert receipt["status"] == "complete"
        assert receipt["authorization_input_ready"] is True
        assert receipt["authorization_unlock"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["missing_required"] == []
        assert receipt["policy_version"] == "local-v1-continuous-o2-3"
        assert receipt["source_import_id"] == source_import_id
        assert receipt["formalization_receipt_id"] == finalized["formalization_receipt_id"]
        assert receipt["adjacent_chain_id"] == chain_id
        assert receipt["managed_skill_chain_id"] == managed_id
        assert receipt["creation_suite_chain_id"] == managed_id
        assert receipt["four_skills_continuous_receipt_id"]
        assert receipt["instruction_chain_handoff_binding_id"] == handoff_binding["binding_id"]
        assert receipt["instruction_chain_handoff_id"] == prepared_handoff["handoff"]["handoff_id"]
        assert all(item["present"] for item in receipt["checks"])
        assert any(
            item["code"] == "four_skills_continuous_linked" and item["present"] is True
            for item in receipt["checks"]
        )
        assert any(
            item["code"] == "instruction_chain_handoff_bound" and item["present"] is True
            for item in receipt["checks"]
        )
        assert "formal_draft_authorization_unlock" in evaluated.json()["not_claiming"]
        assert "four_skills_generic_exactly_once" in evaluated.json()["not_claiming"]

        persisted = (
            runtime_root
            / "formal-draft-authorization"
            / project_id
            / "continuous-o2-receipt.json"
        )
        assert persisted.is_file()

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
            },
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["decision"] == "denied"
        assert formal_receipt["formal_draft_allowed"] is False
        prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "four_skills_continuous_o2"
        )
        assert prereq["satisfied"] is True
        assert "authorization input" in prereq["detail"]
        assert all(
            item["code"] != "four_skills_chain_incomplete"
            for item in formal_receipt["denials"]
        )
        assert any(
            item["code"] == "local_v1_policy_locked"
            for item in formal_receipt["denials"]
        )
    finally:
        client.__exit__(None, None, None)
