from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


CHAIN_PREFIX = "/api/v1/projects/{project_id}/managed-skill-chains"
AUDIT_SKILL = "novel-creation-suite.project-audit"
INSTRUCTION_SKILL = "novel-creation-suite.writer-instruction"
REPO_BUILTIN = Path(__file__).resolve().parents[3] / "adapters" / "builtin"


def _write_project(root: Path) -> Path:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    (root / "01_项目定位" / "定位.md").write_text("玄幻长篇升级故事。", encoding="utf-8")
    return root


def _write_healthy_audit_package(managed_root: Path) -> None:
    package = managed_root / "creation-suite-project-audit"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    (package / "PROVENANCE.md").write_text(
        "Test fixture managed project-audit package.\n",
        encoding="utf-8",
    )
    (package / "run_project_audit.py").write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path

request = json.load(open(0, encoding="utf-8"))
assert isinstance(request, dict)
report = {
    "project": "bound_project_snapshot",
    "health": "healthy",
    "counts": {"P0": 0, "P1": 0, "P2": 0},
    "expected_files": 1,
    "chapter_files": 0,
    "issue_cards": 0,
    "continuation_gate_results": [],
    "ai_smell_reports": [],
    "ai_smell_bridge_proofs": [],
    "ai_smell_gate_results": [],
    "findings": [],
    "next_actions": ["continue"],
}
Path("project-audit.json").write_text(
    json.dumps(report, ensure_ascii=False),
    encoding="utf-8",
)
print(json.dumps({
    "health": "healthy",
    "summary": "Fixture project audit completed.",
    "result": report,
    "artifacts": [{
        "artifact_type": "project_audit",
        "relative_path": "project-audit.json",
        "payload": report,
    }],
}, ensure_ascii=False, separators=(",", ":")))
""",
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.skill-manifest.v1",
                "id": AUDIT_SKILL,
                "display_name": "Creation Suite Project Audit",
                "description": "Fixture audit that always returns healthy.",
                "kind": "deterministic_tool",
                "version": "2026.07.26-m3w-test.1",
                "source": {
                    "type": "builtin_managed",
                    "reference": "PROVENANCE.md",
                    "license": None,
                    "source_url": None,
                    "source_version": None,
                    "modified": True,
                },
                "entrypoint": "run_project_audit.py",
                "approval": "none",
                "permissions": {
                    "read": ["bound_project_snapshot"],
                    "write": ["run_staging"],
                    "network": False,
                    "secrets": [],
                },
                "timeout_seconds": 60,
                "exit_codes": {"0": "success", "1": "domain_blocked"},
                "artifacts": ["project_audit"],
                "input_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-project-audit-request.schema.json"
                ),
                "output_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-project-audit-result.schema.json"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _client_and_project(tmp_path: Path) -> tuple[TestClient, dict]:
    projects_root = tmp_path / "projects"
    project_root = _write_project(projects_root / "managed-chain-novel")
    managed_root = tmp_path / "managed-skills"
    shutil.copytree(REPO_BUILTIN, managed_root)
    # Keep all builtin packages so bootstrap can finish, but replace audit with a
    # healthy fixture so N-step execution does not depend on a full novel scaffold.
    _write_healthy_audit_package(managed_root)
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        trusted_project_roots=(projects_root,),
        managed_skill_root=managed_root,
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    inspected = client.post(
        "/api/v1/projects/inspect",
        json={"root_path": str(project_root)},
    )
    assert inspected.status_code == 200, inspected.text
    return client, inspected.json()["project"]


def _prepare_body(project: dict) -> dict:
    return {
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "chain_key": "dual-project-audit",
        "steps": [
            {
                "step_id": "audit-a",
                "title": "First project audit",
                "skill_id": AUDIT_SKILL,
                "capability": "project-audit",
                "input_payload": {
                    "schema": "zhimeng.creation-project-audit-request.v1",
                },
            },
            {
                "step_id": "audit-b",
                "title": "Second project audit",
                "skill_id": AUDIT_SKILL,
                "capability": "project-audit",
                "input_payload": {
                    "schema": "zhimeng.creation-project-audit-request.v1",
                },
            },
        ],
    }


def test_managed_skill_chain_two_step_audit_with_risk_approval(tmp_path: Path) -> None:
    client, project = _client_and_project(tmp_path)
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        prepared = client.post(f"{base}/prepare", json=_prepare_body(project))
        assert prepared.status_code == 200, prepared.text
        prepared_record = prepared.json()["record"]
        assert prepared_record["status"] == "prepared"
        assert prepared_record["formal_draft_allowed"] is False
        assert prepared.json()["not_claiming"] == [
            "four_skills_generic_exactly_once",
            "raw_skill_md_execution",
            "creation_suite_drafting",
            "reader_gate",
            "local_v1_exe",
            "formal_prose_writeback",
        ]
        assert [step["skill_id"] for step in prepared_record["steps"]] == [
            AUDIT_SKILL,
            AUDIT_SKILL,
        ]
        assert prepared_record["risk_dossier"] is not None
        assert len(prepared_record["risk_dossier"]["skills"]) == 2
        assert prepared_record["risk_approval_token"] is None

        blocked = client.post(
            f"{base}/{prepared_record['chain_id']}/run",
            json={"expected_chain_id": prepared_record["chain_id"]},
        )
        assert blocked.status_code == 409, blocked.text
        assert "批准" in blocked.json()["error"]["message"]

        approved = client.post(
            f"{base}/{prepared_record['chain_id']}/approve",
            json={"expected_chain_id": prepared_record["chain_id"]},
        )
        assert approved.status_code == 200, approved.text
        approved_record = approved.json()["record"]
        assert approved_record["status"] == "approved"
        assert approved_record["approval"]["risk_acknowledged"] is True
        token = approved_record["risk_approval_token"]
        assert token is not None
        assert token["consumed"] is False
        plan = approved_record["workflow_plan"]
        assert plan is not None
        assert plan["workflow_plan_id"].startswith("managed-skill-chain-plan-")
        assert len(plan["steps"]) == 2
        assert all(step["runner"] == "managed_skill" for step in plan["steps"])
        assert all(
            step["input_builder_id"] == "author-supplied-input"
            for step in plan["steps"]
        )

        ran = client.post(
            f"{base}/{prepared_record['chain_id']}/run",
            json={"expected_chain_id": prepared_record["chain_id"]},
        )
        assert ran.status_code == 200, ran.text
        record = ran.json()["record"]
        assert record["status"] == "succeeded"
        assert record["risk_approval_token"]["consumed"] is True
        assert record["current_step_ordinal"] == 2
        assert [step["status"] for step in record["steps"]] == ["succeeded", "succeeded"]
        assert all(
            step["skill_receipt_id"].startswith("skill-receipt-")
            for step in record["steps"]
        )
        assert len(record["workflow_checkpoints"]) == 2
        assert (
            record["workflow_checkpoints"][1]["previous_checkpoint_id"]
            == record["workflow_checkpoints"][0]["checkpoint_id"]
        )
        assert record["formal_draft_allowed"] is False

        recovered = client.get(f"{base}/{record['chain_id']}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["record"]["status"] == "succeeded"
        assert (
            recovered.json()["record"]["workflow_checkpoints"][1]["checkpoint_hash"]
            == record["workflow_checkpoints"][1]["checkpoint_hash"]
        )

        rerun = client.post(
            f"{base}/{record['chain_id']}/run",
            json={"expected_chain_id": record["chain_id"]},
        )
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()["record"]["status"] == "succeeded"
    finally:
        client.__exit__(None, None, None)


def test_managed_skill_chain_rejects_instruction_source(tmp_path: Path) -> None:
    client, project = _client_and_project(tmp_path)
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        catalog = client.get(f"/api/v1/projects/{project_id}/managed-skills")
        assert catalog.status_code == 200, catalog.text
        skill_ids = {
            item.get("skill_id") or item.get("id")
            for item in catalog.json().get("skills", catalog.json().get("items", []))
        }
        # If catalog shape differs, still attempt prepare against the known instruction id.
        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "chain_key": "instruction-reject",
                "steps": [
                    {
                        "step_id": "writer-instruction",
                        "title": "Instruction source must fail closed",
                        "skill_id": INSTRUCTION_SKILL,
                        "capability": "writer.instruction-source",
                        "input_payload": {},
                    }
                ],
            },
        )
        assert prepared.status_code == 422, prepared.text
        body = prepared.json()
        assert body["error"]["code"] == "managed_skill_chain_invalid"
        assert "deterministic_tool" in body["error"]["message"]
        assert INSTRUCTION_SKILL  # keep constant referenced
        assert skill_ids is not None
    finally:
        client.__exit__(None, None, None)
