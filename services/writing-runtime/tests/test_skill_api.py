from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _manifest(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: api-fixture-skill\n"
        "description: Skill exposed by the read-only registry API.\n"
        "---\n\n"
        "# API fixture\n",
        encoding="utf-8",
    )


def _settings(
    tmp_path: Path,
    *,
    skill_roots: tuple[Path, ...] = (),
    project_roots: tuple[Path, ...] = (),
    managed_skill_root: Path | None = None,
) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=project_roots,
        trusted_skill_roots=skill_roots,
        managed_skill_root=managed_skill_root,
        allowed_origins=(),
    )


def _managed_creation_audit_package(managed_root: Path) -> Path:
    package = managed_root / "creation-suite-project-audit"
    package.mkdir(parents=True)
    (package / "run_project_audit.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (package / "PROVENANCE.md").write_text(
        "Application-owned managed test fixture.\n",
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.skill-manifest.v1",
                "id": "novel-creation-suite.project-audit",
                "display_name": "Creation Suite Project Audit",
                "description": "Read-only audit of a bound project snapshot.",
                "kind": "deterministic_tool",
                "version": "2026.07.20-test.1",
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
                "timeout_seconds": 120,
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
    return package


def _managed_creation_prewrite_package(managed_root: Path) -> Path:
    package = managed_root / "creation-suite-prewrite-gate"
    package.mkdir(parents=True)
    (package / "run_prewrite_gate.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (package / "PROVENANCE.md").write_text(
        "Application-owned managed prewrite test fixture.\n",
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.skill-manifest.v1",
                "id": "novel-creation-suite.prewrite-gate",
                "display_name": "Creation Suite Prewrite Gate",
                "description": "Read-only prewrite gate over a Runtime package.",
                "kind": "deterministic_tool",
                "version": "2026.07.20-test.1",
                "source": {
                    "type": "builtin_managed",
                    "reference": "PROVENANCE.md",
                    "license": None,
                    "source_url": None,
                    "source_version": None,
                    "modified": True,
                },
                "entrypoint": "run_prewrite_gate.py",
                "approval": "none",
                "permissions": {
                    "read": ["bound_project_snapshot"],
                    "write": ["run_staging"],
                    "network": False,
                    "secrets": [],
                },
                "timeout_seconds": 60,
                "exit_codes": {"0": "success", "1": "domain_blocked"},
                "artifacts": ["prewrite_gate"],
                "input_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-prewrite-gate-request.schema.json"
                ),
                "output_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-prewrite-gate-result.schema.json"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package


def _managed_creation_handoff_package(managed_root: Path) -> Path:
    package = managed_root / "creation-suite-handoff"
    package.mkdir(parents=True)
    (package / "run_creation_handoff.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (package / "PROVENANCE.md").write_text(
        "Application-owned managed handoff test fixture.\n",
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.skill-manifest.v1",
                "id": "novel-creation-suite.creation-handoff",
                "display_name": "Creation Suite Handoff",
                "description": "Read-only creation handoff over prior workflow receipts.",
                "kind": "deterministic_tool",
                "version": "2026.07.21-test.1",
                "source": {
                    "type": "builtin_managed",
                    "reference": "PROVENANCE.md",
                    "license": None,
                    "source_url": None,
                    "source_version": None,
                    "modified": True,
                },
                "entrypoint": "run_creation_handoff.py",
                "approval": "none",
                "permissions": {
                    "read": ["bound_project_snapshot"],
                    "write": ["run_staging"],
                    "network": False,
                    "secrets": [],
                },
                "timeout_seconds": 60,
                "exit_codes": {"0": "success", "1": "domain_blocked"},
                "artifacts": ["creation_handoff"],
                "input_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-handoff-request.schema.json"
                ),
                "output_schema": (
                    "https://zhimeng.local/contracts/"
                    "creation-handoff-result.schema.json"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package


def test_skill_api_defaults_to_an_empty_non_scanning_registry(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/api/v1/skills")

    assert response.status_code == 200
    assert response.json()["schema"] == "zhimeng.skill-registry.v1"
    assert response.json()["configured"] is False
    assert response.json()["roots"] == []
    assert response.json()["skills"] == []
    assert response.json()["diagnostics"][0]["code"] == "registry_not_configured"


def test_skill_api_lists_only_explicit_trusted_roots(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    _manifest(trusted)
    _manifest(outside)

    with TestClient(
        create_app(_settings(tmp_path, skill_roots=(trusted,)))
    ) as client:
        response = client.get("/api/v1/skills")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert [item["name"] for item in payload["skills"]] == ["api-fixture-skill"]
    assert payload["roots"][0]["root_path"] == str(trusted)
    assert payload["skills"][0]["source"]["root_path"] == str(trusted)
    assert payload["skills"][0]["package_fingerprint"] is None
    assert payload["skills"][0]["package_fingerprint_status"] == "not_scanned"


def test_invalid_skill_root_returns_typed_error_without_killing_health(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    app = create_app(_settings(tmp_path, skill_roots=(missing,)))

    with TestClient(app) as client:
        live = client.get("/health/live")
        response = client.get("/api/v1/skills")

    assert live.status_code == 200
    assert response.status_code == 503
    assert response.json()["schema"] == "zhimeng.error.v1"
    assert response.json()["error"]["code"] == "skill_registry_unavailable"
    assert response.json()["error"]["retryable"] is False


def test_root_removed_after_startup_does_not_lose_healthy_roots(
    tmp_path: Path,
) -> None:
    healthy = tmp_path / "healthy"
    removed = tmp_path / "removed"
    _manifest(healthy)
    _manifest(removed)
    app = create_app(_settings(tmp_path, skill_roots=(healthy, removed)))
    shutil.rmtree(removed)

    with TestClient(app) as client:
        response = client.get("/api/v1/skills")

    assert response.status_code == 200
    payload = response.json()
    assert [skill["source"]["root_path"] for skill in payload["skills"]] == [
        str(healthy)
    ]
    assert [root["scan_status"] for root in payload["roots"]] == [
        "complete",
        "blocked",
    ]
    assert payload["roots"][1]["diagnostics"][0]["code"] == "skill_scan_failed"


def test_app_registers_builtin_managed_skill_after_database_startup(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    managed_root = tmp_path / "managed-skills"
    _managed_creation_audit_package(managed_root)
    _managed_creation_prewrite_package(managed_root)
    _managed_creation_handoff_package(managed_root)
    app = create_app(
        _settings(
            tmp_path,
            project_roots=(project,),
            managed_skill_root=managed_root,
        )
    )

    with TestClient(app) as client:
        app.state.runtime.store.sync_project_binding(
            project_id="project-managed-api",
            root_path=str(project.resolve()),
            display_name="Managed API fixture",
            fingerprint="a" * 64,
        )
        response = client.get("/api/v1/projects/project-managed-api/skills")
        ready = client.get("/health/ready")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert [item["manifest"]["id"] for item in payload["skills"]] == [
            "novel-creation-suite.creation-handoff",
            "novel-creation-suite.prewrite-gate",
            "novel-creation-suite.project-audit",
        ]
        assert ready.status_code == 200, ready.text
        assert app.state.managed_skill_service_error is None
        assert app.state.skill_executor is not None
        assert app.state.agent_service.tool_executor is not None
        assert "skill_execute" in {
            definition.name
            for definition in app.state.agent_service.tool_executor.definitions
        }


def test_managed_skill_registration_failure_degrades_only_that_capability(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    managed_root = tmp_path / "managed-skills"
    app = create_app(
        _settings(
            tmp_path,
            project_roots=(project,),
            managed_skill_root=managed_root,
        )
    )

    with TestClient(app) as client:
        managed = client.get("/api/v1/projects/missing-project/skills")
        ready = client.get("/health/ready")

        assert managed.status_code == 503, managed.text
        assert managed.json()["error"]["code"] == "managed_skill_unavailable"
        assert managed.json()["error"]["details"]["startup_error"]
        assert ready.status_code == 200, ready.text
        assert app.state.managed_skill_service_error
        assert app.state.skill_executor is None
        assert app.state.agent_service.tool_executor is not None
        assert "skill_execute" not in {
            definition.name
            for definition in app.state.agent_service.tool_executor.definitions
        }


def test_managed_skill_lifecycle_api_imports_inert_project_snapshot_and_uses_cas(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    managed_root = tmp_path / "bundled-skills"
    _managed_creation_audit_package(managed_root)
    _managed_creation_prewrite_package(managed_root)
    _managed_creation_handoff_package(managed_root)
    trusted = tmp_path / "trusted-imports"
    imported_package = _managed_creation_audit_package(trusted)
    manifest_path = imported_package / "skill.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "2026.07.22-test.2"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (imported_package / "run_project_audit.py").write_text(
        "print({'version': 2})\n",
        encoding="utf-8",
    )
    app = create_app(
        _settings(
            tmp_path,
            skill_roots=(trusted,),
            project_roots=(project,),
            managed_skill_root=managed_root,
        )
    )

    with TestClient(app) as client:
        app.state.runtime.store.sync_project_binding(
            project_id="project-lifecycle-api",
            root_path=str(project.resolve()),
            display_name="Lifecycle API fixture",
            fingerprint="a" * 64,
        )
        application = client.get("/api/v1/managed-skills")
        imported = client.post(
            "/api/v1/projects/project-lifecycle-api/managed-skills/imports",
            json={"source_path": str(imported_package)},
        )

        assert application.status_code == 200, application.text
        assert application.json()["scope"] == {
            "scope_type": "application",
            "project_id": None,
        }
        assert imported.status_code == 200, imported.text
        imported_payload = imported.json()
        assert imported_payload["imported"] is True
        assert imported_payload["activation"]["configured_state"] == "inherited"
        version_id = imported_payload["version"]["skill_version_id"]
        skill_id = imported_payload["version"]["manifest"]["id"]

        lifecycle = client.get(
            "/api/v1/projects/project-lifecycle-api/managed-skills"
        )
        assert lifecycle.status_code == 200, lifecycle.text
        matching_versions = [
            item
            for item in lifecycle.json()["versions"]
            if item["manifest"]["id"] == skill_id
        ]
        assert len(matching_versions) == 2
        assert matching_versions[1]["validation"]["permissions"] == {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        }

        enabled = client.put(
            f"/api/v1/projects/project-lifecycle-api/managed-skills/{skill_id}/activation",
            json={
                "action": "enable",
                "skill_version_id": version_id,
                "expected_state_revision": 0,
                "expected_version_id": None,
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["activation"]["effective_scope"] == "project"

        stale = client.put(
            f"/api/v1/projects/project-lifecycle-api/managed-skills/{skill_id}/activation",
            json={
                "action": "disable",
                "skill_version_id": None,
                "expected_state_revision": 0,
                "expected_version_id": None,
            },
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "managed_skill_activation_conflict"

        disabled = client.put(
            f"/api/v1/projects/project-lifecycle-api/managed-skills/{skill_id}/activation",
            json={
                "action": "disable",
                "skill_version_id": None,
                "expected_state_revision": 1,
                "expected_version_id": version_id,
            },
        )
        assert disabled.status_code == 200, disabled.text
        effective = client.get("/api/v1/projects/project-lifecycle-api/skills")
        assert skill_id not in {
            item["manifest"]["id"] for item in effective.json()["skills"]
        }

        inherited = client.put(
            f"/api/v1/projects/project-lifecycle-api/managed-skills/{skill_id}/activation",
            json={
                "action": "inherit",
                "skill_version_id": None,
                "expected_state_revision": 2,
                "expected_version_id": None,
            },
        )
        assert inherited.status_code == 200, inherited.text
        assert inherited.json()["activation"]["effective_scope"] == "application"


def test_managed_skill_import_api_rejects_untrusted_and_unadapted_sources(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    managed_root = tmp_path / "bundled-skills"
    _managed_creation_audit_package(managed_root)
    _managed_creation_prewrite_package(managed_root)
    _managed_creation_handoff_package(managed_root)
    trusted = tmp_path / "trusted-imports"
    trusted.mkdir()
    raw = trusted / "raw"
    raw.mkdir()
    (raw / "SKILL.md").write_text("# Requires an adapter\n", encoding="utf-8")
    untrusted = tmp_path / "untrusted"
    _managed_creation_audit_package(untrusted)
    app = create_app(
        _settings(
            tmp_path,
            skill_roots=(trusted,),
            project_roots=(project,),
            managed_skill_root=managed_root,
        )
    )

    with TestClient(app) as client:
        untrusted_response = client.post(
            "/api/v1/managed-skills/imports",
            json={"source_path": str(untrusted / "creation-suite-project-audit")},
        )
        raw_response = client.post(
            "/api/v1/managed-skills/imports",
            json={"source_path": str(raw)},
        )

    assert untrusted_response.status_code == 403, untrusted_response.text
    assert untrusted_response.json()["error"]["code"] == (
        "managed_skill_import_not_allowed"
    )
    assert raw_response.status_code == 422, raw_response.text
    assert raw_response.json()["error"]["code"] == "managed_skill_adaptation_required"
