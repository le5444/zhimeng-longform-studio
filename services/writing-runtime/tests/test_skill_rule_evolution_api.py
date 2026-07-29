from __future__ import annotations

import json
from pathlib import Path

import pytest

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.managed_skill_service import ManagedSkillService
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest
from zhimeng_runtime.skill_rule_evolution_models import (
    SkillRuleEvolutionAction,
    SkillRuleEvolutionActionRequest,
    SkillRuleEvolutionPrepareRequest,
)
from zhimeng_runtime.skill_rule_evolution_service import (
    SkillRuleEvolutionBlockedError,
    SkillRuleEvolutionService,
)


CONTRACTS_ROOT = Path(__file__).resolve().parents[3] / "packages" / "contracts"
INPUT_CONTRACT_NAME = "creation-project-audit-request.schema.json"
OUTPUT_CONTRACT_NAME = "creation-project-audit-result.schema.json"


def manifest_payload(*, version: str = "2026.07.20-m3a.1") -> dict[str, object]:
    return {
        "schema": "zhimeng.skill-manifest.v1",
        "id": "novel-creation-suite.project-audit",
        "display_name": "Project audit",
        "description": "Audit a bound project snapshot without writing project truth.",
        "kind": "deterministic_tool",
        "version": version,
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": False,
        },
        "entrypoint": "scripts/project_audit.py",
        "approval": "none",
        "permissions": {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        },
        "timeout_seconds": 120,
        "exit_codes": {"0": "success", "1": "domain_blocked"},
        "artifacts": ["project_audit"],
        "input_schema": f"https://zhimeng.local/contracts/{INPUT_CONTRACT_NAME}",
        "output_schema": f"https://zhimeng.local/contracts/{OUTPUT_CONTRACT_NAME}",
    }


def write_package(
    managed_root: Path,
    name: str,
    *,
    version: str,
    script: str = "print('{}')\n",
) -> Path:
    package = managed_root / name
    entrypoint = package / "scripts" / "project_audit.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(script, encoding="utf-8")
    (package / "PROVENANCE.md").write_text("Builtin managed fixture.\n", encoding="utf-8")
    (package / "skill.manifest.json").write_text(
        json.dumps(manifest_payload(version=version), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return package


def setup(tmp_path: Path) -> tuple[ManagedSkillService, SkillRuleEvolutionService]:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project = tmp_path / "project"
    project.mkdir()
    store.sync_project_binding(
        project_id="project-fixture",
        root_path=str(project.resolve()),
        display_name="Fixture",
        fingerprint="a" * 64,
    )
    managed_root = tmp_path / "managed-skills"
    managed = ManagedSkillService(store, managed_root, contracts_root=CONTRACTS_ROOT)
    evolution = SkillRuleEvolutionService(
        managed,
        state_root=tmp_path / "skill-rule-evolutions",
        change_set_service=None,
    )
    return managed, evolution


def test_skill_rule_evolution_freeze_compare_apply_rollback(tmp_path: Path) -> None:
    managed, evolution = setup(tmp_path)
    first = managed.register_builtin(
        write_package(managed.managed_root, "v1", version="2026.07.20-m3a.1")
    )
    second = managed.register_builtin(
        write_package(
            managed.managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )

    prepared = evolution.prepare(
        SkillRuleEvolutionPrepareRequest(
            skill_id=first.manifest.id,
            target_version_id=second.skill_version_id,
            expected_current_version_id=first.skill_version_id,
        )
    )
    receipt = prepared.receipt
    assert receipt.status == "prepared"
    assert receipt.freeze_recorded is True
    assert receipt.auto_upgrade is False
    assert receipt.formal_draft_allowed is False
    assert receipt.quality_assessed is False
    assert receipt.from_version_id == first.skill_version_id
    assert receipt.to_version_id == second.skill_version_id
    assert "automatic_skill_rule_upgrade" in prepared.not_claiming

    compared = evolution.act(
        receipt.evolution_id,
        SkillRuleEvolutionActionRequest(action=SkillRuleEvolutionAction.COMPARE),
    )
    assert compared.receipt.status == "compare_ready"
    assert compared.receipt.compare_status == "deferred"

    applied = evolution.act(
        receipt.evolution_id,
        SkillRuleEvolutionActionRequest(action=SkillRuleEvolutionAction.APPLY),
    )
    assert applied.receipt.status == "applied"
    assert applied.receipt.activation_receipt_id
    assert managed.get_active("project-fixture", first.manifest.id).skill_version_id == (
        second.skill_version_id
    )

    rolled = evolution.act(
        receipt.evolution_id,
        SkillRuleEvolutionActionRequest(action=SkillRuleEvolutionAction.ROLLBACK),
    )
    assert rolled.receipt.status == "rolled_back"
    assert managed.get_active("project-fixture", first.manifest.id).skill_version_id == (
        first.skill_version_id
    )


def test_skill_rule_evolution_blocks_apply_when_eval_compare_degraded(
    tmp_path: Path,
) -> None:
    managed, _ = setup(tmp_path)
    first = managed.register_builtin(
        write_package(managed.managed_root, "v1", version="2026.07.20-m3a.1")
    )
    second = managed.register_builtin(
        write_package(
            managed.managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )

    class _Baseline:
        receipt_id = "eval-corpus-baseline-" + ("1" * 32)

    class _Regression:
        receipt_id = "eval-corpus-regression-" + ("2" * 32)
        status = "degraded"

    class _FakeChangeSetService:
        def get_eval_corpus_baseline(self, change_set_id: str):
            class _Resp:
                receipt = _Baseline()

            return _Resp()

        def evaluate_eval_corpus_regression(self, change_set_id: str, request=None):
            class _Resp:
                receipt = _Regression()

            return _Resp()

    evolution = SkillRuleEvolutionService(
        managed,
        state_root=tmp_path / "skill-rule-evolutions-blocked",
        change_set_service=_FakeChangeSetService(),
    )
    prepared = evolution.prepare(
        SkillRuleEvolutionPrepareRequest(
            skill_id=first.manifest.id,
            target_version_id=second.skill_version_id,
            change_set_id="changeset-" + ("a" * 32),
            expected_current_version_id=first.skill_version_id,
        )
    )
    compared = evolution.act(
        prepared.receipt.evolution_id,
        SkillRuleEvolutionActionRequest(action=SkillRuleEvolutionAction.COMPARE),
    )
    assert compared.receipt.status == "compare_blocked"
    assert compared.receipt.compare_status == "degraded"
    with pytest.raises(SkillRuleEvolutionBlockedError):
        evolution.act(
            prepared.receipt.evolution_id,
            SkillRuleEvolutionActionRequest(action=SkillRuleEvolutionAction.APPLY),
        )
    # still on first version
    assert managed.get_active("project-fixture", first.manifest.id).skill_version_id == (
        first.skill_version_id
    )


def test_skill_rule_evolution_api_prepare_and_apply(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from zhimeng_runtime.app import create_app
    from zhimeng_runtime.config import RuntimeSettings

    managed_root = tmp_path / "managed-skills"
    first_pkg = write_package(managed_root, "v1", version="2026.07.20-m3a.1")
    second_pkg = write_package(
        managed_root, "v2", version="2026.07.20-m3a.2", script="print('v2')\n"
    )
    # App bootstrap only auto-registers packages under configured managed root
    # when startup scans known package names. Register via service after boot.
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                managed_skill_root=managed_root,
                allowed_origins=(),
            )
        )
    ) as client:
        managed = client.app.state.managed_skill_service
        assert managed is not None
        first = managed.register_builtin(first_pkg)
        second = managed.register_builtin(second_pkg)

        prepared = client.post(
            "/api/v1/skill-rule-evolutions/prepare",
            json={
                "skill_id": first.manifest.id,
                "target_version_id": second.skill_version_id,
                "expected_current_version_id": first.skill_version_id,
            },
        )
        assert prepared.status_code == 201, prepared.text
        body = prepared.json()
        assert body["schema"] == "zhimeng.skill-rule-evolution-response.v1"
        assert body["receipt"]["formal_draft_allowed"] is False
        assert body["receipt"]["auto_upgrade"] is False
        evolution_id = body["receipt"]["evolution_id"]

        compared = client.post(
            f"/api/v1/skill-rule-evolutions/{evolution_id}/actions",
            json={"action": "compare"},
        )
        assert compared.status_code == 200, compared.text
        assert compared.json()["receipt"]["status"] == "compare_ready"

        applied = client.post(
            f"/api/v1/skill-rule-evolutions/{evolution_id}/actions",
            json={"action": "apply"},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["receipt"]["status"] == "applied"

        fetched = client.get(f"/api/v1/skill-rule-evolutions/{evolution_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["receipt"]["status"] == "applied"
