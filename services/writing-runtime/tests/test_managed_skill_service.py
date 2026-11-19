from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

import zhimeng_runtime.managed_skill_service as managed_skill_module
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillActivationConflictError,
    ManagedSkillActivationInvalidError,
    ManagedSkillImportNotAllowedError,
    ManagedSkillLimits,
    ManagedSkillNotFoundError,
    ManagedSkillPackageDriftError,
    ManagedSkillPackageError,
    ManagedSkillPackageLimitError,
    ManagedSkillProjectNotFoundError,
    ManagedSkillService,
    ManagedSkillVersionConflictError,
)
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest, SkillManifest


CONTRACTS_ROOT = Path(__file__).resolve().parents[3] / "packages" / "contracts"
INPUT_CONTRACT_NAME = "creation-project-audit-request.schema.json"
OUTPUT_CONTRACT_NAME = "creation-project-audit-result.schema.json"


def manifest_payload(
    *,
    version: str = "2026.07.20-m3a.1",
    entrypoint: str = "scripts/project_audit.py",
    capabilities: list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
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
        "entrypoint": entrypoint,
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
    if capabilities is not None:
        payload["capabilities"] = capabilities
    return payload


def write_package(
    managed_root: Path,
    name: str = "project-audit-v1",
    *,
    version: str = "2026.07.20-m3a.1",
    script: str = "print('{}')\n",
    capabilities: list[str] | None = None,
) -> Path:
    package = managed_root / name
    entrypoint = package / "scripts" / "project_audit.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(script, encoding="utf-8")
    (package / "PROVENANCE.md").write_text("Builtin managed fixture.\n", encoding="utf-8")
    (package / "skill.manifest.json").write_text(
        json.dumps(
            manifest_payload(version=version, capabilities=capabilities),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package


def instruction_source_manifest_payload(
    *,
    resources: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema": "zhimeng.skill-manifest.v1",
        "id": "novel-creation-suite.writer-instruction",
        "display_name": "Creation writer instruction",
        "description": "Immutable managed instructions for Writer materialization.",
        "kind": "instruction_source",
        "version": "2026.07.23-o2.2",
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": True,
        },
        "approval": "none",
        "permissions": {
            "read": [],
            "write": [],
            "network": False,
            "secrets": [],
        },
        "artifacts": [],
        "capabilities": ["creation_instruction_materialization.v1"],
        "resources": (
            ["instruction-map.json"] if resources is None else resources
        ),
    }


def write_instruction_source_package(root: Path, name: str = "writer-source") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "PROVENANCE.md").write_text("Builtin instruction fixture.\n", encoding="utf-8")
    (package / "instruction-map.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.instruction-map.v1",
                "instructions": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(instruction_source_manifest_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return package


def setup_service(
    tmp_path: Path,
    *,
    trusted_import_roots: tuple[Path, ...] = (),
    contracts_root: Path | None = None,
) -> tuple[SqliteStore, ManagedSkillService, Path]:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    managed_root = tmp_path / "managed-skills"
    service = ManagedSkillService(
        store,
        managed_root,
        trusted_import_roots=trusted_import_roots,
        contracts_root=contracts_root,
    )
    project = tmp_path / "project"
    project.mkdir()
    store.sync_project_binding(
        project_id="project-fixture",
        root_path=str(project.resolve()),
        display_name="Fixture",
        fingerprint="a" * 64,
    )
    return store, service, managed_root


def test_manifest_is_strict_and_accepts_the_builtin_package_contract() -> None:
    manifest = SkillManifest.model_validate(manifest_payload(), strict=True)

    assert manifest.id == "novel-creation-suite.project-audit"
    assert manifest.version == "2026.07.20-m3a.1"
    assert manifest.permissions.read == ["bound_project_snapshot"]
    assert manifest.capabilities == []
    assert manifest.exit_codes.model_dump(by_alias=True) == {
        "0": "success",
        "1": "domain_blocked",
    }

    with pytest.raises(ValidationError):
        SkillManifest.model_validate({**manifest_payload(), "unknown": True}, strict=True)
    unsafe = manifest_payload(entrypoint="../outside.py")
    with pytest.raises(ValidationError, match="package-relative"):
        SkillManifest.model_validate(unsafe, strict=True)
    networked = manifest_payload()
    networked["permissions"] = {
        "read": ["bound_project_snapshot"],
        "write": ["run_staging"],
        "network": True,
        "secrets": [],
    }
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(networked, strict=True)


def test_instruction_source_manifest_has_no_execution_contract() -> None:
    manifest = SkillManifest.model_validate(
        instruction_source_manifest_payload(),
        strict=True,
    )

    assert manifest.kind == "instruction_source"
    assert manifest.entrypoint is None
    assert manifest.input_schema is None
    assert manifest.output_schema is None
    assert manifest.permissions.read == []
    assert manifest.permissions.write == []
    assert manifest.resources == ["instruction-map.json"]

    with_reference = SkillManifest.model_validate(
        instruction_source_manifest_payload(
            resources=[
                "instruction-map.json",
                "references/novel-volume-folder/SKILL.md",
            ]
        ),
        strict=True,
    )
    assert with_reference.resources[-1] == "references/novel-volume-folder/SKILL.md"

    for invalid in (
        {**instruction_source_manifest_payload(), "entrypoint": "run.py"},
        {
            **instruction_source_manifest_payload(),
            "input_schema": (
                "https://zhimeng.local/contracts/creation-handoff-request.schema.json"
            ),
        },
        instruction_source_manifest_payload(resources=[]),
        instruction_source_manifest_payload(resources=["../instruction-map.json"]),
    ):
        with pytest.raises(ValidationError):
            SkillManifest.model_validate(invalid, strict=True)


def test_instruction_source_import_validation_and_activation_use_managed_snapshot(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _, service, _ = setup_service(
        tmp_path,
        trusted_import_roots=(trusted,),
    )
    source = write_instruction_source_package(trusted)

    imported = service.import_package(source, project_id="project-fixture")

    assert imported.imported is True
    assert imported.version.manifest.kind == "instruction_source"
    assert imported.version.validation.contract_status == "not_applicable"
    assert imported.version.validation.input_contract_fingerprint is None
    assert imported.version.validation.output_contract_fingerprint is None
    assert imported.version.validation.contract_bundle_fingerprint is None
    assert imported.activation.effective_state == "disabled"

    enabled = service.activate(
        imported.version.manifest.id,
        ManagedSkillActivationRequest(
            action="enable",
            skill_version_id=imported.version.skill_version_id,
            expected_state_revision=0,
            expected_version_id=None,
        ),
        project_id="project-fixture",
    )
    active = service.get_active(
        "project-fixture",
        imported.version.manifest.id,
    )

    assert enabled.activation.effective_state == "enabled"
    assert active.entrypoint_path is None
    assert active.instruction_map_path is not None
    assert active.instruction_map_path.is_file()
    assert active.instruction_map_path.is_relative_to(service.managed_root / "snapshots")
    assert not active.instruction_map_path.is_relative_to(source)

    active.instruction_map_path.write_text(
        json.dumps({"schema": "tampered"}),
        encoding="utf-8",
    )
    with pytest.raises(ManagedSkillPackageDriftError):
        service.verify_active(active)


def test_instruction_source_rejects_missing_or_invalid_instruction_map(
    tmp_path: Path,
) -> None:
    _, service, managed_root = setup_service(tmp_path)
    missing = write_instruction_source_package(managed_root, "missing-map")
    (missing / "instruction-map.json").unlink()
    with pytest.raises(ManagedSkillPackageError, match="resources"):
        service.register_builtin(missing)

    invalid = write_instruction_source_package(managed_root, "invalid-map")
    (invalid / "instruction-map.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ManagedSkillPackageError, match="JSON object"):
        service.register_builtin(invalid)


def test_manifest_capabilities_are_bounded_canonical_and_content_addressed(
    tmp_path: Path,
) -> None:
    declared = SkillManifest.model_validate(
        manifest_payload(capabilities=["knowledge_validator_bundle.v1"]),
        strict=True,
    )
    assert declared.capabilities == ["knowledge_validator_bundle.v1"]

    for invalid in (
        ["z_capability", "a_capability"],
        ["duplicate", "duplicate"],
        ["invalid-ending."],
    ):
        with pytest.raises(ValidationError, match="capability declarations"):
            SkillManifest.model_validate(
                manifest_payload(capabilities=invalid),
                strict=True,
            )

    without_root = tmp_path / "without-capability"
    without_root.mkdir()
    _, without_service, without_managed = setup_service(without_root)
    without = without_service.register_builtin(write_package(without_managed))

    with_root = tmp_path / "with-capability"
    with_root.mkdir()
    _, with_service, with_managed = setup_service(with_root)
    with_capability = with_service.register_builtin(
        write_package(
            with_managed,
            capabilities=["knowledge_validator_bundle.v1"],
        )
    )

    assert without.manifest_fingerprint != with_capability.manifest_fingerprint
    assert without.package_fingerprint != with_capability.package_fingerprint
    assert without.skill_version_id != with_capability.skill_version_id


def test_registers_full_package_fingerprint_and_lists_project_safe_catalog(
    tmp_path: Path,
) -> None:
    store, service, managed_root = setup_service(tmp_path)
    package = write_package(managed_root)

    registered = service.register_builtin(package)
    catalog = service.list_for_project("project-fixture")

    assert catalog.skills == [registered.public_entry()]
    payload = catalog.model_dump(mode="json", by_alias=True)
    rendered = json.dumps(payload, ensure_ascii=False)
    assert payload["skills"][0]["manifest"]["source"]["type"] == "builtin_managed"
    assert "source_path" not in rendered
    assert str(managed_root) not in rendered
    assert ".codex" not in rendered.lower()

    with store.connection() as connection:
        row = connection.execute("SELECT * FROM skill_version").fetchone()
    assert row is not None
    source_path = Path(str(row["source_path"]))
    assert source_path == registered.manifest_path
    assert source_path.parent == managed_root / "snapshots" / registered.skill_version_id
    assert not source_path.is_relative_to(package)
    assert row["fingerprint"] == registered.package_fingerprint
    assert row["status"] == "active"
    assert row["snapshot_status"] == "managed"

    (package / "PROVENANCE.md").write_text("Changed after registration.\n", encoding="utf-8")
    assert service.verify_active(registered) == registered.package_fingerprint

    (registered.package_root / "PROVENANCE.md").write_text(
        "Tampered Runtime snapshot.\n",
        encoding="utf-8",
    )
    with pytest.raises(ManagedSkillPackageDriftError):
        service.verify_active(registered)


def test_snapshot_publish_retries_transient_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, managed_root = setup_service(tmp_path)
    package = write_package(managed_root)
    original_rename = Path.rename
    attempts = 0
    delays: list[float] = []

    def flaky_rename(source: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "snapshot fixture is temporarily locked")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    monkeypatch.setattr(managed_skill_module.time, "sleep", delays.append)

    registered = service.register_builtin(package)

    assert attempts == 3
    assert delays == list(managed_skill_module.SNAPSHOT_PUBLISH_RETRY_DELAYS[:2])
    assert registered.package_root.is_dir()
    assert list((managed_root / ".staging").iterdir()) == []


def test_snapshot_publish_exhausts_permission_retry_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, managed_root = setup_service(tmp_path)
    package = write_package(managed_root)
    attempts = 0
    delays: list[float] = []

    def locked_rename(_source: Path, _target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        raise PermissionError(5, "snapshot fixture remains locked")

    monkeypatch.setattr(Path, "rename", locked_rename)
    monkeypatch.setattr(managed_skill_module.time, "sleep", delays.append)

    with pytest.raises(PermissionError, match="remains locked"):
        service.register_builtin(package)

    assert attempts == len(managed_skill_module.SNAPSHOT_PUBLISH_RETRY_DELAYS) + 1
    assert delays == list(managed_skill_module.SNAPSHOT_PUBLISH_RETRY_DELAYS)
    assert list((managed_root / ".staging").iterdir()) == []
    assert list((managed_root / "snapshots").iterdir()) == []


def test_versions_are_immutable_and_activation_upgrade_and_rollback_use_cas(
    tmp_path: Path,
) -> None:
    store, service, managed_root = setup_service(tmp_path)
    first_package = write_package(managed_root, "v1")
    first = service.register_builtin(first_package)

    changed_same_version = write_package(
        managed_root,
        "same-version-different-content",
        script="print('changed')\n",
    )
    with pytest.raises(ManagedSkillVersionConflictError):
        service.register_builtin(changed_same_version)
    assert {path.name for path in (managed_root / "snapshots").iterdir()} == {
        first.skill_version_id
    }

    second_package = write_package(
        managed_root,
        "v2",
        version="2026.07.20-m3a.2",
        script="print('v2')\n",
    )
    second = service.register_builtin(second_package)

    with store.connection() as connection:
        rows = connection.execute(
            "SELECT id, status FROM skill_version ORDER BY registered_at, id"
        ).fetchall()
    statuses = {str(row["id"]): str(row["status"]) for row in rows}
    assert statuses[first.skill_version_id] == "active"
    assert statuses[second.skill_version_id] == "active"
    assert service.get_active("project-fixture", second.manifest.id) == second

    author_rollback = service.activate(
        second.manifest.id,
        ManagedSkillActivationRequest(
            action="rollback",
            skill_version_id=first.skill_version_id,
            expected_state_revision=2,
            expected_version_id=second.skill_version_id,
        ),
    )
    assert author_rollback.activation.effective_version_id == first.skill_version_id
    assert author_rollback.receipt.action == "rollback"

    upgraded = service.activate(
        second.manifest.id,
        ManagedSkillActivationRequest(
            action="upgrade",
            skill_version_id=second.skill_version_id,
            expected_state_revision=3,
            expected_version_id=first.skill_version_id,
        ),
    )
    assert upgraded.activation.effective_version_id == second.skill_version_id
    assert upgraded.receipt.action == "upgrade"
    assert service.get_active("project-fixture", second.manifest.id) == second

    rolled_back = service.activate(
        second.manifest.id,
        ManagedSkillActivationRequest(
            action="rollback",
            skill_version_id=first.skill_version_id,
            expected_state_revision=4,
            expected_version_id=second.skill_version_id,
        ),
    )
    assert rolled_back.activation.effective_version_id == first.skill_version_id
    assert rolled_back.receipt.action == "rollback"


def test_builtin_registration_upgrades_an_unmodified_builtin_default(
    tmp_path: Path,
) -> None:
    store, service, managed_root = setup_service(tmp_path)
    first = service.register_builtin(write_package(managed_root, "v1"))

    second = service.register_builtin(
        write_package(
            managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )

    active = service.get_active("project-fixture", second.manifest.id)
    assert active.skill_version_id == second.skill_version_id
    with store.connection() as connection:
        activation = connection.execute(
            """
            SELECT active_version_id, state_revision
            FROM skill_activation
            WHERE scope_key = 'application' AND skill_id = ?
            """,
            (second.manifest.id,),
        ).fetchone()
        recovery = connection.execute(
            """
            SELECT action, previous_version_id, next_version_id
            FROM skill_activation_receipt
            WHERE scope_key = 'application' AND skill_id = ? AND state_revision = 2
            """,
            (second.manifest.id,),
        ).fetchone()
    assert dict(activation) == {
        "active_version_id": second.skill_version_id,
        "state_revision": 2,
    }
    assert dict(recovery) == {
        "action": "builtin_bootstrap",
        "previous_version_id": first.skill_version_id,
        "next_version_id": second.skill_version_id,
    }


def test_builtin_registration_preserves_an_explicit_author_rollback(
    tmp_path: Path,
) -> None:
    _, service, managed_root = setup_service(tmp_path)
    first = service.register_builtin(write_package(managed_root, "v1"))
    second = service.register_builtin(
        write_package(
            managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )
    service.activate(
        first.manifest.id,
        ManagedSkillActivationRequest(
            action="rollback",
            skill_version_id=first.skill_version_id,
            expected_state_revision=2,
            expected_version_id=second.skill_version_id,
        ),
    )

    third = service.register_builtin(
        write_package(
            managed_root,
            "v3",
            version="2026.07.20-m3a.3",
            script="print('v3')\n",
        )
    )

    assert service.get_active("project-fixture", third.manifest.id) == first


def test_lifecycle_reports_missing_history_and_refuses_to_reactivate_it(
    tmp_path: Path,
) -> None:
    _, service, managed_root = setup_service(tmp_path)
    first = service.register_builtin(write_package(managed_root, "v1"))
    shutil.rmtree(first.package_root)
    second = service.register_builtin(
        write_package(
            managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )

    lifecycle = service.list_lifecycle()

    assert [item.skill_version_id for item in lifecycle.versions] == [
        second.skill_version_id
    ]
    assert len(lifecycle.unavailable_versions) == 1
    unavailable = lifecycle.unavailable_versions[0]
    assert unavailable.skill_version_id == first.skill_version_id
    assert unavailable.skill_id == first.manifest.id
    assert unavailable.version == first.manifest.version
    assert unavailable.package_fingerprint == first.package_fingerprint
    assert unavailable.reason == "snapshot_missing"
    assert service.get_active("project-fixture", second.manifest.id) == second

    with pytest.raises(ManagedSkillPackageError, match="not accessible"):
        service.activate(
            second.manifest.id,
            ManagedSkillActivationRequest(
                action="rollback",
                skill_version_id=first.skill_version_id,
                expected_state_revision=2,
                expected_version_id=second.skill_version_id,
            ),
        )


def test_rejects_untrusted_imports_links_hardlinks_and_budgets(tmp_path: Path) -> None:
    _, service, managed_root = setup_service(tmp_path)
    outside = write_package(tmp_path / "outside-root")
    with pytest.raises(ManagedSkillImportNotAllowedError, match="trusted roots"):
        service.import_package(outside)

    oversized_service = ManagedSkillService(
        service._store,
        tmp_path / "small-managed",
        limits=ManagedSkillLimits(max_files=2, max_total_bytes=4096, max_file_bytes=4096),
    )
    oversized = write_package(oversized_service.managed_root)
    with pytest.raises(ManagedSkillPackageLimitError, match="file-count"):
        oversized_service.register_builtin(oversized)

    linked = write_package(managed_root, "linked")
    target = linked / "PROVENANCE.md"
    link = linked / "alias.md"
    try:
        link.symlink_to(target)
    except OSError:
        pass
    else:
        with pytest.raises(ManagedSkillPackageError, match="links"):
            service.register_builtin(linked)

    hardlinked = write_package(managed_root, "hardlinked")
    try:
        os.link(hardlinked / "PROVENANCE.md", hardlinked / "alias.md")
    except OSError:
        pass
    else:
        with pytest.raises(ManagedSkillPackageError, match="hardlinked"):
            service.register_builtin(hardlinked)


def test_catalog_is_project_scoped(tmp_path: Path) -> None:
    _, service, managed_root = setup_service(tmp_path)
    service.register_builtin(write_package(managed_root))

    with pytest.raises(ManagedSkillProjectNotFoundError):
        service.list_for_project("unknown-project")


def test_trusted_project_import_is_inert_until_enabled_and_never_crosses_projects(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    store, service, managed_root = setup_service(
        tmp_path,
        trusted_import_roots=(trusted,),
    )
    project_two = tmp_path / "project-two"
    project_two.mkdir()
    store.sync_project_binding(
        project_id="project-two",
        root_path=str(project_two.resolve()),
        display_name="Second fixture",
        fingerprint="b" * 64,
    )
    builtin = service.register_builtin(write_package(managed_root, "builtin-v1"))
    project_package = write_package(
        trusted,
        "project-v2",
        version="2026.07.20-m3a.2",
        script="print('project v2')\n",
    )

    imported = service.import_package(project_package, project_id="project-fixture")

    assert imported.imported is True
    assert imported.activation.configured_state == "inherited"
    assert imported.activation.effective_version_id == builtin.skill_version_id
    assert service.get_active("project-two", builtin.manifest.id) == builtin

    enabled = service.activate(
        builtin.manifest.id,
        ManagedSkillActivationRequest(
            action="enable",
            skill_version_id=imported.version.skill_version_id,
            expected_state_revision=0,
            expected_version_id=None,
        ),
        project_id="project-fixture",
    )
    assert enabled.activation.effective_scope == "project"
    assert service.get_active(
        "project-fixture",
        builtin.manifest.id,
    ).skill_version_id == imported.version.skill_version_id
    assert service.get_active("project-two", builtin.manifest.id) == builtin

    disabled = service.activate(
        builtin.manifest.id,
        ManagedSkillActivationRequest(
            action="disable",
            skill_version_id=None,
            expected_state_revision=1,
            expected_version_id=imported.version.skill_version_id,
        ),
        project_id="project-fixture",
    )
    assert disabled.activation.effective_state == "disabled"
    with pytest.raises(ManagedSkillNotFoundError):
        service.get_active("project-fixture", builtin.manifest.id)
    assert service.get_active("project-two", builtin.manifest.id) == builtin

    inherited = service.activate(
        builtin.manifest.id,
        ManagedSkillActivationRequest(
            action="inherit",
            skill_version_id=None,
            expected_state_revision=2,
            expected_version_id=None,
        ),
        project_id="project-fixture",
    )
    assert inherited.activation.effective_scope == "application"
    assert service.get_active("project-fixture", builtin.manifest.id) == builtin

    with pytest.raises(ManagedSkillNotFoundError):
        service.activate(
            builtin.manifest.id,
            ManagedSkillActivationRequest(
                action="upgrade",
                skill_version_id=imported.version.skill_version_id,
                expected_state_revision=1,
                expected_version_id=builtin.skill_version_id,
            ),
        )


def test_import_is_content_addressed_idempotent_and_survives_source_removal(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    store, service, managed_root = setup_service(
        tmp_path,
        trusted_import_roots=(trusted,),
    )
    source = write_package(trusted)

    first = service.import_package(source)
    repeated = service.import_package(
        source,
        expected_package_fingerprint=first.version.package_fingerprint,
    )

    assert first.imported is True
    assert repeated.imported is False
    assert repeated.receipt == first.receipt
    assert first.version.skill_version_id == repeated.version.skill_version_id
    snapshot_root = service.managed_root / "snapshots" / first.version.skill_version_id
    assert snapshot_root.is_dir()
    shutil.rmtree(trusted)

    restarted = ManagedSkillService(
        store,
        managed_root,
        trusted_import_roots=(trusted,),
    )
    lifecycle = restarted.list_lifecycle()
    assert lifecycle.versions[0].skill_version_id == first.version.skill_version_id
    assert lifecycle.versions[0].package_fingerprint == first.version.package_fingerprint


def test_registered_contract_fingerprints_fail_closed_after_registry_drift(
    tmp_path: Path,
) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    for file_name in (INPUT_CONTRACT_NAME, OUTPUT_CONTRACT_NAME):
        shutil.copy2(CONTRACTS_ROOT / file_name, contracts / file_name)
    store, service, managed_root = setup_service(
        tmp_path,
        contracts_root=contracts,
    )
    service.register_builtin(write_package(managed_root))

    contract_path = contracts / INPUT_CONTRACT_NAME
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["description"] = "Changed after the managed Skill was imported."
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    restarted = ManagedSkillService(
        store,
        managed_root,
        contracts_root=contracts,
    )
    with pytest.raises(ManagedSkillPackageDriftError, match="metadata"):
        restarted.list_lifecycle()


def test_failed_import_and_restart_recovery_leave_no_partial_snapshot(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _, service, managed_root = setup_service(
        tmp_path,
        trusted_import_roots=(trusted,),
    )
    raw_skill = trusted / "raw-skill"
    raw_skill.mkdir()
    (raw_skill / "SKILL.md").write_text("# Not adapted\n", encoding="utf-8")

    with pytest.raises(ManagedSkillPackageError, match="managed-package adapter"):
        service.import_package(raw_skill)
    assert list((managed_root / ".staging").iterdir()) == []
    assert list((managed_root / "snapshots").iterdir()) == []

    interrupted = managed_root / ".staging" / "interrupted"
    interrupted.mkdir()
    (interrupted / "partial").write_text("partial", encoding="utf-8")
    orphan = managed_root / "snapshots" / f"skill-version-{'f' * 32}"
    orphan.mkdir()
    (orphan / "partial").write_text("orphan", encoding="utf-8")

    foreign_store = SqliteStore(tmp_path / "foreign-runtime.sqlite3")
    foreign_store.initialize()
    foreign_service = ManagedSkillService(foreign_store, managed_root)
    foreign_snapshot = foreign_service.register_builtin(
        write_package(
            tmp_path / "foreign-source",
            version="2026.07.20-m3a.2",
            script="print('foreign')\n",
        )
    ).package_root

    recovered = service.recover_imports()
    assert recovered == {"removed_staging": 1, "removed_orphans": 1}
    assert not interrupted.exists()
    assert not orphan.exists()
    assert foreign_snapshot.is_dir()


def test_activation_cas_allows_only_one_competing_author_decision(tmp_path: Path) -> None:
    _, service, managed_root = setup_service(tmp_path)
    registered = service.register_builtin(write_package(managed_root))
    request = ManagedSkillActivationRequest(
        action="disable",
        skill_version_id=None,
        expected_state_revision=1,
        expected_version_id=registered.skill_version_id,
    )

    def decide() -> str:
        try:
            service.activate(registered.manifest.id, request)
        except ManagedSkillActivationConflictError:
            return "conflict"
        return "disabled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: decide(), range(2)))

    assert results == ["conflict", "disabled"]
    with service._store.connection() as connection:
        receipts = connection.execute(
            "SELECT COUNT(*) FROM skill_activation_receipt WHERE action = 'disable'"
        ).fetchone()[0]
    assert receipts == 1


def test_activation_rejects_stale_cas_and_false_rollback_label(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    _, service, managed_root = setup_service(
        tmp_path,
        trusted_import_roots=(trusted,),
    )
    first = service.register_builtin(write_package(managed_root, "v1"))
    second = service.register_builtin(
        write_package(
            managed_root,
            "v2",
            version="2026.07.20-m3a.2",
            script="print('v2')\n",
        )
    )
    never_active = service.import_package(
        write_package(
            trusted,
            "v3",
            version="2026.07.20-m3a.3",
            script="print('v3')\n",
        )
    )

    with pytest.raises(ManagedSkillActivationConflictError):
        service.activate(
            first.manifest.id,
            ManagedSkillActivationRequest(
                action="disable",
                expected_state_revision=0,
                expected_version_id=None,
            ),
        )
    with pytest.raises(ManagedSkillActivationInvalidError, match="never active"):
        service.activate(
            first.manifest.id,
            ManagedSkillActivationRequest(
                action="rollback",
                skill_version_id=never_active.version.skill_version_id,
                expected_state_revision=2,
                expected_version_id=second.skill_version_id,
            ),
        )


def test_import_and_activation_receipt_tamper_fail_closed(tmp_path: Path) -> None:
    _, service, managed_root = setup_service(tmp_path)
    registered = service.register_builtin(write_package(managed_root))

    with service._store.connection() as connection:
        connection.execute(
            """
            UPDATE skill_import_receipt
            SET source_reference = 'tampered'
            WHERE skill_version_id = ?
            """,
            (registered.skill_version_id,),
        )
    with pytest.raises(ManagedSkillPackageDriftError, match="import receipt"):
        service.list_lifecycle()

    with service._store.connection() as connection:
        payload = json.loads(
            connection.execute(
                """
                SELECT payload_json FROM skill_import_receipt
                WHERE skill_version_id = ?
                """,
                (registered.skill_version_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE skill_import_receipt
            SET source_reference = ?
            WHERE skill_version_id = ?
            """,
            (payload["source"]["reference"], registered.skill_version_id),
        )
        connection.execute(
            """
            UPDATE skill_activation_receipt
            SET payload_json = '{"tampered":true}'
            WHERE skill_id = ?
            """,
            (registered.manifest.id,),
        )
    with pytest.raises(ManagedSkillPackageDriftError, match="activation receipt"):
        service.get_active("project-fixture", registered.manifest.id)
