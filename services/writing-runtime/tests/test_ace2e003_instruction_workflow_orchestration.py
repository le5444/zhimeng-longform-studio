from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


FAMILIES = (
    ("source_ingest", "raw-source-ingest"),
    ("distillation", "raw-distillation"),
    ("knowledge_context", "raw-knowledge-context"),
    ("creation_suite", "raw-creation-suite"),
)


def _create_longform_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    (volume / "CH001.md").write_text("第一章。青崖找到钥匙。\n", encoding="utf-8")
    (volume / "CH002.md").write_text("第二章。旧港只剩一盏白灯。\n", encoding="utf-8")
    (volume / "CH003.md").write_text("第三章。他仍未开门。\n", encoding="utf-8")


def _write_raw_skill(root: Path, *, name: str, description: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Instruction-only raw skill body for adaptation.\n",
        encoding="utf-8",
    )
    return root


def test_ace2e003_longform_instruction_workflow_orchestration(tmp_path: Path) -> None:
    """AC-E2E-003 residual slice:

    long-form project
    -> four raw skills plan/materialize
    -> generic instruction workflow binding
    -> runnable generic instruction workflow orchestration
    -> instruction chain handoff prepared
    -> generic execution probe denies instruction_source packages
    -> family-mapped generic managed execution runs four deterministic tools
    -> project-bound instruction chain handoff
    -> continuous O2 recognizes handoff without completing alone
    -> formal draft remains denied

    Claims managed_skill_execution and four_skills_generic_exactly_once only
    for the family-mapped managed execution receipt. Still does not claim
    raw SKILL.md execution or formal draft unlock.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-book"
    _create_longform_project(project)
    trusted = tmp_path / "trusted-skills"
    members = []
    for family, name in FAMILIES:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        members.append({"family": family, "source_path": str(path)})

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            trusted_skill_roots=(trusted,),
            managed_skill_root=Path(__file__).resolve().parents[3] / "adapters" / "builtin",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]
        revision = inspected.json()["project"]["revision"]
        fingerprint = inspected.json()["project"]["fingerprint"]
        prose = [
            item
            for item in inspected.json()["files"]
            if "05_正文内容" in item["relative_path"] and item["relative_path"].endswith(".md")
        ]
        assert len(prose) >= 3

        planned = client.post(
            "/api/v1/four-skills-raw-adaptations/plan",
            json={"members": members},
        )
        assert planned.status_code == 201, planned.text
        batch_id = planned.json()["receipt"]["batch_id"]
        batch_hash = planned.json()["receipt"]["batch_hash"]

        materialized = client.post(
            "/api/v1/four-skills-raw-adaptations/materialize",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": batch_hash,
                "import_after_materialize": True,
            },
        )
        assert materialized.status_code == 200, materialized.text
        mat = materialized.json()["receipt"]
        assert mat["authorization_input_ready"] is True

        bound = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
            },
        )
        assert bound.status_code == 201, bound.text
        binding = bound.json()["receipt"]
        assert binding["workflow_ready"] is True
        assert binding["executable"] is False

        orchestrated = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-orchestrations",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_binding_id": binding["binding_id"],
                "expected_binding_hash": binding["binding_hash"],
            },
        )
        assert orchestrated.status_code == 201, orchestrated.text
        receipt = orchestrated.json()["receipt"]
        assert receipt["status"] == "completed"
        assert receipt["runnable"] is True
        assert receipt["orchestration_ready"] is True
        assert receipt["managed_skill_execution"] is False
        assert receipt["raw_skill_md_execution"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["formal_draft_allowed"] is False
        assert all(step["runner"] == "instruction_orchestrator" for step in receipt["steps"])

        handed = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_binding_id": binding["binding_id"],
                "expected_binding_hash": binding["binding_hash"],
                "expected_orchestration_id": receipt["orchestration_id"],
                "expected_orchestration_hash": receipt["orchestration_hash"],
            },
        )
        assert handed.status_code == 201, handed.text
        handoff = handed.json()["receipt"]
        assert handoff["status"] == "ready"
        assert handoff["instruction_chain_ready"] is True
        assert handoff["prepared_step_count"] == 4
        assert handoff["managed_skill_execution"] is False
        assert handoff["raw_skill_md_execution"] is False
        assert handoff["four_skills_generic_exactly_once"] is False
        assert handoff["formal_draft_allowed"] is False
        assert handoff["writes_project_truth"] is False
        assert all(step["runner"] == "instruction_chain_handoff" for step in handoff["steps"])
        assert all(step["status"] == "prepared" for step in handoff["steps"])

        probed = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/generic-execution-probes",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_handoff_id": handoff["handoff_id"],
                "expected_handoff_hash": handoff["handoff_hash"],
            },
        )
        assert probed.status_code == 201, probed.text
        probe = probed.json()["receipt"]
        assert probe["status"] == "complete"
        assert probe["probe_complete"] is True
        assert probe["generic_execution_ready"] is False
        assert probe["managed_skill_execution"] is False
        assert probe["raw_skill_md_execution"] is False
        assert probe["four_skills_generic_exactly_once"] is False
        assert probe["formal_draft_allowed"] is False
        assert probe["assessed_step_count"] == 4
        assert probe["denied_step_count"] == 4
        assert probe["would_accept_step_count"] == 0
        assert all(step["package_kind"] == "instruction_source" for step in probe["steps"])
        assert all(step["executor_would_accept"] is False for step in probe["steps"])
        assert all(step["denial_code"] == "skill_not_executable" for step in probe["steps"])
        assert all(step["status"] == "denied" for step in probe["steps"])
        assert all(step["managed_skill_execution"] is False for step in probe["steps"])
        assert "generic_execution_ready" in probed.json()["not_claiming"]

        executed = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/generic-managed-executions",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_probe_id": probe["probe_id"],
                "expected_probe_hash": probe["probe_hash"],
                "project_id": project_id,
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert executed.status_code == 201, executed.text
        managed_exec = executed.json()["receipt"]
        assert managed_exec["status"] == "complete"
        assert managed_exec["managed_skill_execution"] is True
        assert managed_exec["raw_skill_md_execution"] is False
        assert managed_exec["four_skills_generic_exactly_once"] is True
        assert managed_exec["formal_draft_allowed"] is False
        assert managed_exec["writes_project_truth"] is False
        assert managed_exec["succeeded_step_count"] == 4
        assert managed_exec["failed_step_count"] == 0
        assert all(step["status"] == "succeeded" for step in managed_exec["steps"])
        assert all(step["managed_skill_execution"] is True for step in managed_exec["steps"])
        assert all(step["raw_skill_md_execution"] is False for step in managed_exec["steps"])
        assert all(
            step["skill_receipt_id"] and step["skill_receipt_id"].startswith("skill-receipt-")
            for step in managed_exec["steps"]
        )
        assert "raw_skill_md_execution" in executed.json()["not_claiming"]
        assert "local_v1_exe" in executed.json()["not_claiming"]


        project_bound = client.post(
            f"/api/v1/projects/{project_id}/instruction-chain-handoff-bindings",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
                "batch_id": batch_id,
                "batch_hash": mat["batch_hash"],
                "handoff_id": handoff["handoff_id"],
                "handoff_hash": handoff["handoff_hash"],
            },
        )
        assert project_bound.status_code == 201, project_bound.text
        project_binding = project_bound.json()["receipt"]
        assert project_binding["status"] == "bound"
        assert project_binding["instruction_chain_ready"] is True
        assert project_binding["formal_draft_allowed"] is False
        assert project_binding["managed_skill_execution"] is False
        assert project_binding["raw_skill_md_execution"] is False

        o2 = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/continuous-o2/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert o2.status_code == 200, o2.text
        o2_receipt = o2.json()["receipt"]
        assert o2_receipt["formal_draft_allowed"] is False
        assert o2_receipt["four_skills_generic_exactly_once"] is False
        assert any(
            item["code"] == "instruction_chain_handoff_bound" and item["present"] is True
            for item in o2_receipt["checks"]
        )
        # Handoff alone does not complete continuous O2; other required stages still missing.
        assert o2_receipt["status"] == "incomplete"
        assert o2_receipt["authorization_input_ready"] is False

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": revision,
                "expected_project_fingerprint": fingerprint,
            },
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["formal_draft_allowed"] is False
