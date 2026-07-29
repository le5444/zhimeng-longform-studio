"""End-to-end portable package-test: real bundle -> install -> Runtime ready -> backup/restore.

This is still not a signed EXE/MSI black-box matrix. It freezes the Local v1
portable chain that package-tests can already own before shell/bundler ADR.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from desktop_packaging.portable import (
    PortableLayout,
    assemble_portable_bundle,
    default_repo_paths,
    evaluate_signed_package_readiness,
    install_clean,
    reinstall_restore,
    runtime_settings_kwargs_from_layout,
    uninstall_preserve,
    upgrade,
    verify_portable_bundle,
    write_signed_package_readiness_receipt,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _require_repo_bundle_inputs(repo: Path) -> dict[str, Path]:
    paths = default_repo_paths(repo)
    if not (paths["workbench_dist"] / "index.html").is_file():
        pytest.skip("apps/workbench/dist not built")
    if not (paths["runtime_src_root"] / "zhimeng_runtime" / "__init__.py").is_file():
        pytest.skip("runtime package source missing")
    managed = paths.get("managed_skills_source")
    if managed is None or not managed.is_dir():
        pytest.skip("managed skills source missing")
    return paths


def _assemble_real_bundle(
    *,
    output_dir: Path,
    product_version: str,
    paths: dict[str, Path],
) -> dict:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    return assemble_portable_bundle(
        output_dir=output_dir,
        product_version=product_version,
        workbench_dist=paths["workbench_dist"],
        runtime_src_root=paths["runtime_src_root"],
        managed_skills_source=paths["managed_skills_source"],
        git_commit=None,
    )


def _runtime_settings(layout: PortableLayout) -> RuntimeSettings:
    kwargs = runtime_settings_kwargs_from_layout(layout)
    return RuntimeSettings(
        **kwargs,
        agent_provider_base_url="https://example.invalid/v1",
        agent_provider_api_key="super-secret-key-must-not-leak",
        agent_provider_credential_reference="vault://portable-e2e/provider",
        agent_provider_model="demo-model",
    )


def _upsert_marker(db_path: Path, marker: str) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS portable_e2e_marker "
            "(id TEXT PRIMARY KEY, note TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO portable_e2e_marker (id, note) VALUES (?, ?)",
            ("marker", marker),
        )
        connection.commit()
    finally:
        connection.close()


def _read_marker(db_path: Path) -> str | None:
    connection = sqlite3.connect(str(db_path))
    try:
        try:
            row = connection.execute(
                "SELECT note FROM portable_e2e_marker WHERE id = ?",
                ("marker",),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else str(row[0])
    finally:
        connection.close()


def test_real_bundle_install_runtime_ready_backup_and_restore(tmp_path: Path) -> None:
    repo = Path.cwd()
    paths = _require_repo_bundle_inputs(repo)

    bundle_dir = tmp_path / "portable-bundle-e2e"
    manifest = _assemble_real_bundle(
        output_dir=bundle_dir,
        product_version="0.1.0-e2e",
        paths=paths,
    )
    assert manifest["formal_draft_allowed"] is False
    assert manifest["includes_secrets"] is False
    assert manifest["includes_novel_body"] is False
    assert manifest["includes_project_files"] is False
    assert "local_v1_exe" in manifest["not_claiming"]
    assert (bundle_dir / "workbench" / "index.html").is_file()
    assert (bundle_dir / "runtime" / "src" / "zhimeng_runtime" / "app.py").is_file()
    managed_names = list(manifest["components"]["managed_skills"]["package_names"])
    assert managed_names, "expected managed skills packaged from adapters/builtin"
    verified = verify_portable_bundle(bundle_dir)
    assert verified["manifest_hash"] == manifest["manifest_hash"]

    signed_readiness = evaluate_signed_package_readiness(bundle_dir)
    signed_receipt = signed_readiness["receipt"]
    assert signed_receipt["package_kind"] == "portable_bundle"
    assert signed_receipt["signature_ready"] is False
    assert signed_receipt["local_v1_exe_claim_allowed"] is False
    assert signed_receipt["formal_draft_allowed"] is False
    assert "local_v1_exe" in signed_readiness["not_claiming"]
    assert "authenticode_signature_present" in signed_receipt["missing_required"]
    readiness_path = write_signed_package_readiness_receipt(
        signed_readiness,
        tmp_path / "signed-package-readiness.json",
    )
    assert readiness_path.is_file()

    layout = PortableLayout(tmp_path / "Zhimeng")
    installed = install_clean(
        layout,
        product_version="0.1.0-e2e",
        bundle_source=bundle_dir,
    )
    assert installed["outcome"] == "clean_install"
    assert installed["formal_draft_allowed"] is False
    assert installed["includes_secrets"] is False
    assert installed["runtime_database_path"] == str(layout.runtime_database_path)
    assert installed["runtime_backups_root"] == str(layout.runtime_backups_root)
    assert (layout.app_version_dir("0.1.0-e2e") / "MANIFEST.json").is_file()
    assert (layout.app_version_dir("0.1.0-e2e") / "workbench" / "index.html").is_file()
    assert (
        layout.app_version_dir("0.1.0-e2e")
        / "runtime"
        / "src"
        / "zhimeng_runtime"
        / "app.py"
    ).is_file()

    for name in managed_names:
        assert (layout.skills_root / name).is_dir(), name
        assert (
            (layout.skills_root / name / "SKILL.md").is_file()
            or (layout.skills_root / name / "skill.json").is_file()
            or any((layout.skills_root / name).glob("**/SKILL.md"))
        ), name
    seeded_marker = layout.skills_root / ".seeded-from-bundle.txt"
    assert seeded_marker.is_file()
    seeded_text = seeded_marker.read_text(encoding="utf-8")
    for name in managed_names:
        assert name in seeded_text

    env_text = layout.runtime_env_path.read_text(encoding="utf-8")
    assert f"ZHIMENG_DATABASE_PATH={layout.runtime_database_path}" in env_text
    assert f"ZHIMENG_BACKUP_ROOT={layout.runtime_backups_root}" in env_text
    assert f"ZHIMENG_DIAGNOSTICS_ROOT={layout.diagnostics_root}" in env_text
    assert f"ZHIMENG_MANAGED_SKILL_ROOT={layout.skills_root}" in env_text

    settings = _runtime_settings(layout)
    assert settings.database_path == layout.runtime_database_path
    assert settings.backup_root == layout.runtime_backups_root
    assert settings.diagnostics_root == layout.diagnostics_root
    assert settings.managed_skill_root == layout.skills_root

    with TestClient(create_app(settings)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        ready_body = ready.json()
        assert ready_body["status"] == "ready"
        assert "super-secret-key-must-not-leak" not in ready.text

        diagnostics = client.post("/api/v1/runtime/diagnostics-package")
        assert diagnostics.status_code == 200, diagnostics.text
        diagnostics_body = diagnostics.json()
        diagnostics_receipt = diagnostics_body["receipt"]
        assert diagnostics_receipt["formal_draft_allowed"] is False
        assert diagnostics_receipt["writes_project_truth"] is False
        assert diagnostics_receipt["contains_secrets"] is False
        assert diagnostics_receipt["contains_novel_body"] is False
        assert "super-secret-key-must-not-leak" not in diagnostics.text
        diagnostics_name = Path(diagnostics_receipt["export_relative_path"]).name
        diagnostics_path = layout.diagnostics_root / diagnostics_name
        assert diagnostics_path.is_file()
        diagnostics_text = diagnostics_path.read_text(encoding="utf-8")
        assert "super-secret-key-must-not-leak" not in diagnostics_text
        assert not (layout.runtime_data_root / "diagnostics" / diagnostics_name).exists()

        _upsert_marker(layout.runtime_database_path, "portable-before")
        assert _read_marker(layout.runtime_database_path) == "portable-before"

        created = client.post("/api/v1/runtime/backups")
        assert created.status_code == 200, created.text
        body = created.json()
        backup_id = body["receipt"]["backup_id"]
        manifest_hash = body["receipt"]["manifest"]["manifest_hash"]
        assert body["receipt"]["formal_draft_allowed"] is False
        assert body["receipt"]["writes_project_truth"] is False
        assert "super-secret-key-must-not-leak" not in created.text

        package_dir = layout.runtime_backups_root / backup_id
        assert package_dir.is_dir()
        assert (package_dir / "manifest.json").is_file()
        assert (package_dir / "writing-runtime.sqlite3").is_file()
        package_manifest_text = (package_dir / "manifest.json").read_text(encoding="utf-8")
        assert "super-secret-key-must-not-leak" not in package_manifest_text
        assert '"formal_draft_allowed": false' in package_manifest_text.replace(
            "False", "false"
        )

        listed = client.get("/api/v1/runtime/backups")
        assert listed.status_code == 200, listed.text
        assert any(item["backup_id"] == backup_id for item in listed.json()["backups"])
        assert "super-secret-key-must-not-leak" not in listed.text

        _upsert_marker(layout.runtime_database_path, "portable-after")
        assert _read_marker(layout.runtime_database_path) == "portable-after"

        restored = client.post(
            "/api/v1/runtime/backups/restore",
            json={
                "schema": "zhimeng.runtime-restore-request.v1",
                "backup_id": backup_id,
                "expected_manifest_hash": manifest_hash,
            },
        )
        assert restored.status_code == 200, restored.text
        restore_body = restored.json()
        restore_receipt = restore_body["receipt"]
        assert restore_receipt["outcome"] == "restored"
        assert restore_receipt["formal_draft_allowed"] is False
        assert restore_receipt["writes_project_truth"] is False
        assert restore_receipt["includes_project_files"] is False
        assert restore_receipt["integrity_check"] == "ok"
        assert restore_receipt["pre_restore_backup_id"] is not None
        assert "project_file_restore" in restore_body["not_claiming"]
        assert "secret_vault_import" in restore_body["not_claiming"]
        assert "local_v1_exe" in restore_body["not_claiming"]
        assert "super-secret-key-must-not-leak" not in restored.text
        assert _read_marker(layout.runtime_database_path) == "portable-before"

        pre_restore_id = restore_receipt["pre_restore_backup_id"]
        assert (layout.runtime_backups_root / pre_restore_id / "manifest.json").is_file()

        ready_after = client.get("/health/ready")
        assert ready_after.status_code == 200, ready_after.text
        assert ready_after.json()["status"] == "ready"

        after_list = client.get("/api/v1/runtime/backups")
        assert after_list.status_code == 200, after_list.text
        listed_ids = {item["backup_id"] for item in after_list.json()["backups"]}
        assert backup_id in listed_ids
        assert pre_restore_id in listed_ids

    assert layout.runtime_database_path.is_file()
    assert (layout.runtime_backups_root / backup_id / "manifest.json").is_file()
    assert _read_marker(layout.runtime_database_path) == "portable-before"


def test_real_bundle_upgrade_uninstall_preserve_and_reinstall_restore(tmp_path: Path) -> None:
    """Real app payload survives upgrade backup + uninstall-preserve + reinstall-restore.

    This is not a signed OS installer matrix. It proves the portable package-test
    path can keep Runtime DB marker data and previously created runtime backups.
    """
    repo = Path.cwd()
    paths = _require_repo_bundle_inputs(repo)

    v1_bundle = tmp_path / "portable-bundle-0.1.0"
    v2_bundle = tmp_path / "portable-bundle-0.2.0"
    _assemble_real_bundle(output_dir=v1_bundle, product_version="0.1.0", paths=paths)
    _assemble_real_bundle(output_dir=v2_bundle, product_version="0.2.0", paths=paths)
    verify_portable_bundle(v1_bundle)
    verify_portable_bundle(v2_bundle)

    layout = PortableLayout(tmp_path / "Zhimeng-reinstall")
    install_clean(layout, product_version="0.1.0", bundle_source=v1_bundle)

    settings = _runtime_settings(layout)
    with TestClient(create_app(settings)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        _upsert_marker(layout.runtime_database_path, "keep-across-reinstall")
        created = client.post("/api/v1/runtime/backups")
        assert created.status_code == 200, created.text
        runtime_backup_id = created.json()["receipt"]["backup_id"]
        assert (layout.runtime_backups_root / runtime_backup_id / "manifest.json").is_file()

    upgraded = upgrade(layout, product_version="0.2.0", bundle_source=v2_bundle)
    assert upgraded["outcome"] == "upgrade"
    assert upgraded["previous_version"] == "0.1.0"
    portable_backup_id = upgraded["backup_id"]
    assert portable_backup_id
    assert (layout.backups_root / portable_backup_id / "manifest.json").is_file()
    assert (
        layout.backups_root / portable_backup_id / "runtime-backups" / runtime_backup_id / "manifest.json"
    ).is_file()
    assert (layout.app_version_dir("0.2.0") / "MANIFEST.json").is_file()
    assert layout.app_version_dir("0.1.0").exists()
    assert _read_marker(layout.runtime_database_path) == "keep-across-reinstall"

    preserved = uninstall_preserve(layout)
    assert preserved["outcome"] == "uninstall_preserve"
    assert not layout.app_root.exists()
    assert layout.runtime_database_path.is_file()
    assert layout.backups_root.exists()

    restored = reinstall_restore(
        layout,
        product_version="0.1.0",
        bundle_source=v1_bundle,
        backup_id=portable_backup_id,
    )
    assert restored["outcome"] == "reinstall_restore"
    assert layout.app_version_dir("0.1.0").is_dir()
    assert (layout.app_version_dir("0.1.0") / "workbench" / "index.html").is_file()
    assert (
        layout.app_version_dir("0.1.0")
        / "runtime"
        / "src"
        / "zhimeng_runtime"
        / "app.py"
    ).is_file()
    assert _read_marker(layout.runtime_database_path) == "keep-across-reinstall"
    assert (layout.runtime_backups_root / runtime_backup_id / "manifest.json").is_file()

    settings_after = _runtime_settings(layout)
    with TestClient(create_app(settings_after)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "ready"
        listed = client.get("/api/v1/runtime/backups")
        assert listed.status_code == 200, listed.text
        assert any(
            item["backup_id"] == runtime_backup_id for item in listed.json()["backups"]
        )
        assert "super-secret-key-must-not-leak" not in listed.text
        assert _read_marker(layout.runtime_database_path) == "keep-across-reinstall"
