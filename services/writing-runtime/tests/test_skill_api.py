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



def test_managed_project_skills_endpoint_is_gone_but_catalog_and_user_skills_survive(
    tmp_path: Path,
) -> None:
    """2b-i 的回归：受管项目技能端点删了，但正常技能目录和用户技能接口都还在。"""
    with TestClient(create_app(_settings(tmp_path))) as client:
        # 正常技能目录照常
        catalog = client.get("/api/v1/skills")
        assert catalog.status_code == 200

        # 受管端点：不再注册 -> 404
        managed = client.get("/api/v1/projects/project-managed-api/skills")
        assert managed.status_code == 404

        # 设置页用的用户技能接口（settings 技能库读这份）仍在
        user_skills = client.get("/api/v1/user-skills")
        assert user_skills.status_code == 200
