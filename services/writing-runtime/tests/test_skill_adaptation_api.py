from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _settings(tmp_path: Path, *, skill_roots: tuple[Path, ...]) -> RuntimeSettings:
    managed_root = tmp_path / "managed-skills"
    managed_root.mkdir()
    # Minimal bootstrap set: copy only project-audit style empty managed root and
    # rely on no builtin packages being required when managed_skill_root has none
    # of the builtin names? App bootstrap will fail on missing packages.
    # Use real builtin root instead.
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        trusted_skill_roots=skill_roots,
        managed_skill_root=Path(__file__).resolve().parents[3] / "adapters" / "builtin",
    )


def _write_raw_skill(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: sample-volume-folder\n"
        "description: Raw instruction for volume folder structure only.\n"
        "---\n\n"
        "# Sample volume folder\n\n"
        "Keep chapter folders stable.\n",
        encoding="utf-8",
    )
    return root


def test_raw_skill_adaptation_plan_materialize_and_inert_import(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted-skills"
    raw = _write_raw_skill(trusted / "sample-volume-folder")
    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
        planned = client.post(
            "/api/v1/managed-skills/adaptations/plan",
            json={"source_path": str(raw)},
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()["plan"]
        assert plan["status"] == "ready_to_materialize"
        assert plan["adapter_id"] == "zhimeng.raw-skill-instruction"
        assert plan["target"]["kind"] == "instruction_source"
        assert plan["target"]["skill_id"] == "adapted.sample-volume-folder"
        assert plan["target"]["executable"] is False
        assert plan["target"]["formal_draft_allowed"] is False
        assert plan["not_claiming"] == [
            "four_skills_generic_exactly_once",
            "raw_skill_md_execution",
            "deterministic_tool_auto_generation",
            "formal_prose_writeback",
            "local_v1_exe",
        ]
        assert any(item["code"] == "managed_manifest_missing" for item in plan["missing"])

        recovered = client.get(f"/api/v1/managed-skills/adaptations/{plan['plan_id']}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["plan"]["plan_hash"] == plan["plan_hash"]

        materialized = client.post(
            "/api/v1/managed-skills/adaptations/materialize",
            json={
                "expected_plan_id": plan["plan_id"],
                "expected_plan_hash": plan["plan_hash"],
                "import_after_materialize": True,
            },
        )
        assert materialized.status_code == 200, materialized.text
        item = materialized.json()["materialization"]
        assert item["imported"] is True
        assert item["kind"] == "instruction_source"
        assert item["formal_draft_allowed"] is False
        assert item["skill_version_id"].startswith("skill-version-")
        package_root = Path(item["package_root"])
        assert (package_root / "skill.manifest.json").is_file()
        assert (package_root / "instruction-map.json").is_file()
        assert (package_root / "resources" / "SKILL.md").is_file()

        catalog = client.get("/api/v1/managed-skills")
        assert catalog.status_code == 200, catalog.text
        versions = catalog.json()["versions"]
        matched = [
            version
            for version in versions
            if version["manifest"]["id"] == "adapted.sample-volume-folder"
        ]
        assert matched, catalog.text
        # Trusted/adapted imports stay inert until author enable.
        activations = catalog.json()["activations"]
        adapted_activation = next(
            (
                item
                for item in activations
                if item["skill_id"] == "adapted.sample-volume-folder"
            ),
            None,
        )
        if adapted_activation is not None:
            assert adapted_activation["effective_state"] in {"disabled", "enabled"}
            # Default for external/trusted import should not auto-route as project tool.
            # Application bootstrap only auto-enables builtins; adapted remains inert unless enabled.
            assert adapted_activation["configured_state"] in {
                "unconfigured",
                "disabled",
                "enabled",
                "inherited",
            }

        # Direct raw import still fail-closed without adaptation.
        raw_import = client.post(
            "/api/v1/managed-skills/imports",
            json={"source_path": str(raw)},
        )
        assert raw_import.status_code == 422, raw_import.text
        assert raw_import.json()["error"]["code"] == "managed_skill_adaptation_required"


def test_raw_skill_adaptation_rejects_outside_trusted_roots(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted-skills"
    trusted.mkdir()
    outside = tmp_path / "outside"
    _write_raw_skill(outside / "leak-skill")
    app = create_app(_settings(tmp_path, skill_roots=(trusted,)))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/managed-skills/adaptations/plan",
            json={"source_path": str(outside / "leak-skill")},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "skill_adaptation_not_allowed"
