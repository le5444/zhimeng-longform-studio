from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


FAMILIES = (
    ("source_ingest", "raw-source-ingest"),
    ("distillation", "raw-distillation"),
    ("knowledge_context", "raw-knowledge-context"),
    ("creation_suite", "raw-creation-suite"),
)


def _settings(tmp_path: Path, *, skill_roots: tuple[Path, ...]) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        trusted_skill_roots=skill_roots,
        managed_skill_root=Path(__file__).resolve().parents[3] / "adapters" / "builtin",
    )


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


def test_four_skills_raw_adaptation_plan_materialize_linked_inert(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted-skills"
    members = []
    for family, name in FAMILIES:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        members.append({"family": family, "source_path": str(path)})

    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
        planned = client.post(
            "/api/v1/four-skills-raw-adaptations/plan",
            json={"members": members},
        )
        assert planned.status_code == 201, planned.text
        body = planned.json()
        assert body["schema"] == "zhimeng.four-skills-raw-adaptation-response.v1"
        receipt = body["receipt"]
        assert receipt["status"] == "planned"
        assert receipt["member_count"] == 4
        assert receipt["all_present"] is True
        assert receipt["all_linked"] is True
        assert receipt["authorization_input_ready"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["raw_skill_md_execution"] is False
        families = [item["family"] for item in receipt["members"]]
        assert families == [
            "source_ingest",
            "distillation",
            "knowledge_context",
            "creation_suite",
        ]
        assert all(item["plan_id"] for item in receipt["members"])
        assert all(item["executable"] is False for item in receipt["members"])
        assert "raw_skill_md_execution" in body["not_claiming"]
        batch_id = receipt["batch_id"]
        batch_hash = receipt["batch_hash"]

        recovered = client.get(f"/api/v1/four-skills-raw-adaptations/{batch_id}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["receipt"]["batch_hash"] == batch_hash

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
        assert mat["status"] == "materialized"
        assert mat["authorization_input_ready"] is True
        assert mat["all_present"] is True
        assert mat["all_linked"] is True
        assert mat["formal_draft_allowed"] is False
        assert mat["raw_skill_md_execution"] is False
        assert mat["four_skills_generic_exactly_once"] is False
        assert all(item["materialization_id"] for item in mat["members"])
        assert all(item["package_fingerprint"] for item in mat["members"])
        assert all(item["skill_version_id"] for item in mat["members"])

        catalog = client.get("/api/v1/managed-skills")
        assert catalog.status_code == 200, catalog.text
        skill_ids = {
            version["manifest"]["id"] for version in catalog.json()["versions"]
        }
        for item in mat["members"]:
            assert item["skill_id"] in skill_ids

        # Bind materialized inert packages into generic instruction workflow surface.
        bound = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
            },
        )
        assert bound.status_code == 201, bound.text
        binding = bound.json()["receipt"]
        assert binding["status"] == "bound"
        assert binding["workflow_ready"] is True
        assert binding["all_bound"] is True
        assert binding["executable"] is False
        assert binding["raw_skill_md_execution"] is False
        assert binding["four_skills_generic_exactly_once"] is False
        assert binding["formal_draft_allowed"] is False
        assert binding["workflow_surface"] == "generic_instruction_workflow"
        assert binding["step_count"] == 4
        assert all(item["runner"] == "instruction_source" for item in binding["steps"])
        assert all(item["bound"] is True for item in binding["steps"])
        assert all(item["executable"] is False for item in binding["steps"])
        assert "managed_skill_execution" in bound.json()["not_claiming"]
        assert "raw_skill_md_execution" in bound.json()["not_claiming"]

        fetched_binding = client.get(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings"
        )
        assert fetched_binding.status_code == 200, fetched_binding.text
        assert fetched_binding.json()["receipt"]["binding_hash"] == binding["binding_hash"]

        replay_binding = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
            },
        )
        assert replay_binding.status_code == 201, replay_binding.text
        assert replay_binding.json()["receipt"]["binding_id"] == binding["binding_id"]

        replay = client.post(
            "/api/v1/four-skills-raw-adaptations/materialize",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "import_after_materialize": True,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["batch_id"] == batch_id
        assert replay.json()["receipt"]["status"] == "materialized"


def test_four_skills_raw_adaptation_rejects_incomplete_families(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted-skills"
    only_three = []
    for family, name in FAMILIES[:3]:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        only_three.append({"family": family, "source_path": str(path)})
    # duplicate one family to keep length 4 but not distinct
    only_three.append(
        {
            "family": "source_ingest",
            "source_path": only_three[0]["source_path"],
        }
    )
    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/four-skills-raw-adaptations/plan",
            json={"members": only_three},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "four_skills_raw_adaptation_invalid"


def test_four_skills_workflow_orchestration_runs_instruction_bound_surface(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted-skills"
    members = []
    for family, name in FAMILIES:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        members.append({"family": family, "source_path": str(path)})

    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
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
        payload = orchestrated.json()
        assert payload["schema"] == "zhimeng.four-skills-workflow-orchestration-response.v1"
        receipt = payload["receipt"]
        assert receipt["status"] == "completed"
        assert receipt["orchestration_ready"] is True
        assert receipt["runnable"] is True
        assert receipt["completed_step_count"] == 4
        assert receipt["managed_skill_execution"] is False
        assert receipt["raw_skill_md_execution"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["formal_draft_allowed"] is False
        assert all(step["runner"] == "instruction_orchestrator" for step in receipt["steps"])
        assert all(step["status"] == "completed" for step in receipt["steps"])
        assert "managed_skill_execution" in payload["not_claiming"]
        assert "raw_skill_md_execution" in payload["not_claiming"]

        recovered = client.get(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-orchestrations"
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["receipt"]["orchestration_hash"] == receipt["orchestration_hash"]

        # idempotent
        again = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-orchestrations",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_binding_id": binding["binding_id"],
                "expected_binding_hash": binding["binding_hash"],
            },
        )
        assert again.status_code == 201, again.text
        assert again.json()["receipt"]["orchestration_id"] == receipt["orchestration_id"]




def test_four_skills_instruction_chain_handoff_after_orchestration(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted-skills"
    members = []
    for family, name in FAMILIES:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        members.append({"family": family, "source_path": str(path)})

    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
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

        bound = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
            },
        )
        assert bound.status_code == 201, bound.text
        binding = bound.json()["receipt"]

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
        orchestration = orchestrated.json()["receipt"]

        handed = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_binding_id": binding["binding_id"],
                "expected_binding_hash": binding["binding_hash"],
                "expected_orchestration_id": orchestration["orchestration_id"],
                "expected_orchestration_hash": orchestration["orchestration_hash"],
            },
        )
        assert handed.status_code == 201, handed.text
        payload = handed.json()
        assert payload["schema"] == "zhimeng.four-skills-instruction-chain-handoff-response.v1"
        receipt = payload["receipt"]
        assert receipt["status"] == "ready"
        assert receipt["instruction_chain_ready"] is True
        assert receipt["prepared_step_count"] == 4
        assert receipt["managed_skill_execution"] is False
        assert receipt["raw_skill_md_execution"] is False
        assert receipt["four_skills_generic_exactly_once"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["writes_project_truth"] is False
        assert all(step["runner"] == "instruction_chain_handoff" for step in receipt["steps"])
        assert all(step["status"] == "prepared" for step in receipt["steps"])
        assert "managed_skill_execution" in payload["not_claiming"]
        assert "raw_skill_md_execution" in payload["not_claiming"]

        recovered = client.get(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs"
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["receipt"]["handoff_hash"] == receipt["handoff_hash"]

        again = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/instruction-chain-handoffs",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
                "expected_binding_id": binding["binding_id"],
                "expected_binding_hash": binding["binding_hash"],
                "expected_orchestration_id": orchestration["orchestration_id"],
                "expected_orchestration_hash": orchestration["orchestration_hash"],
            },
        )
        assert again.status_code == 201, again.text
        assert again.json()["receipt"]["handoff_id"] == receipt["handoff_id"]
