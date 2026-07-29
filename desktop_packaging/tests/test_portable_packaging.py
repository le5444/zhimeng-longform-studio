"""Package-test coverage for portable install/upgrade/uninstall/runtime-backup paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop_packaging.portable import (
    PortableLayout,
    PortablePackagingConflict,
    install_clean,
    read_install_receipt,
    reinstall_restore,
    repair,
    rollback,
    runtime_settings_kwargs_from_layout,
    uninstall_delete_confirmed,
    uninstall_preserve,
    upgrade,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _bundle(tmp_path: Path, version: str) -> Path:
    root = tmp_path / f"bundle-{version}"
    (root / "runtime").mkdir(parents=True)
    (root / "workbench").mkdir(parents=True)
    (root / "runtime" / "ENTRY.txt").write_text(f"runtime {version}\n", encoding="utf-8")
    (root / "workbench" / "index.html").write_text(
        f"<html><body>workbench {version}</body></html>\n",
        encoding="utf-8",
    )
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def test_clean_install_upgrade_uninstall_preserve_and_delete(tmp_path: Path) -> None:
    root = tmp_path / "Zhimeng"
    layout = PortableLayout(root)
    v1 = _bundle(tmp_path, "0.1.0")
    v2 = _bundle(tmp_path, "0.2.0")

    installed = install_clean(layout, product_version="0.1.0", bundle_source=v1)
    assert installed["outcome"] == "clean_install"
    assert installed["formal_draft_allowed"] is False
    assert installed["includes_secrets"] is False
    assert installed["includes_project_files"] is False
    assert installed["runtime_database_path"] == str(layout.runtime_database_path)
    assert installed["runtime_backups_root"] == str(layout.runtime_backups_root)
    assert (layout.app_version_dir("0.1.0") / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert layout.runtime_data_root.joinpath("runtime-marker.txt").is_file()
    assert layout.config_path.is_file()
    assert layout.runtime_env_path.is_file()
    env_text = layout.runtime_env_path.read_text(encoding="utf-8")
    assert "ZHIMENG_DATABASE_PATH=" in env_text
    assert "ZHIMENG_BACKUP_ROOT=" in env_text
    assert "ZHIMENG_DIAGNOSTICS_ROOT=" in env_text
    assert read_install_receipt(layout) is not None

    marker = layout.data_root / "author-state.txt"
    marker.write_text("keep-me\n", encoding="utf-8")

    upgraded = upgrade(layout, product_version="0.2.0", bundle_source=v2)
    assert upgraded["outcome"] == "upgrade"
    assert upgraded["previous_version"] == "0.1.0"
    assert upgraded["backup_id"]
    assert (layout.app_version_dir("0.2.0") / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert layout.app_version_dir("0.1.0").exists()
    assert marker.read_text(encoding="utf-8") == "keep-me\n"
    backup_dir = layout.backups_root / upgraded["backup_id"]
    assert (backup_dir / "manifest.json").is_file()
    assert (backup_dir / "data" / "author-state.txt").read_text(encoding="utf-8") == "keep-me\n"

    preserved = uninstall_preserve(layout)
    assert preserved["outcome"] == "uninstall_preserve"
    assert not layout.app_root.exists()
    assert marker.is_file()
    assert layout.backups_root.exists()
    assert layout.config_path.is_file()
    receipt = read_install_receipt(layout)
    assert receipt is not None
    assert receipt["outcome"] == "uninstall_preserve"

    deleted = uninstall_delete_confirmed(layout, confirm_root=str(root))
    assert deleted["outcome"] == "uninstall_delete_confirmed"
    assert not marker.exists()
    assert not layout.data_root.exists()
    assert not layout.backups_root.exists()


def test_install_conflict_and_delete_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "Zhimeng"
    layout = PortableLayout(root)
    bundle = _bundle(tmp_path, "0.1.0")
    install_clean(layout, product_version="0.1.0", bundle_source=bundle)
    with pytest.raises(PortablePackagingConflict):
        install_clean(layout, product_version="0.1.0", bundle_source=bundle)
    with pytest.raises(PortablePackagingConflict):
        uninstall_delete_confirmed(layout, confirm_root=str(tmp_path / "other"))


def test_repair_rollback_and_reinstall_restore(tmp_path: Path) -> None:
    root = tmp_path / "Zhimeng"
    layout = PortableLayout(root)
    v1 = _bundle(tmp_path, "0.1.0")
    v2 = _bundle(tmp_path, "0.2.0")
    install_clean(layout, product_version="0.1.0", bundle_source=v1)
    marker = layout.data_root / "author-state.txt"
    marker.write_text("v1-data\n", encoding="utf-8")

    upgraded = upgrade(layout, product_version="0.2.0", bundle_source=v2)
    marker.write_text("v2-data\n", encoding="utf-8")
    assert upgraded["backup_id"]

    layout.config_path.write_text("{bad", encoding="utf-8")
    repaired = repair(layout)
    assert repaired["outcome"] == "repair"
    config_text = layout.config_path.read_text(encoding="utf-8")
    assert "runtime_database_path" in config_text
    assert "runtime_backups_root" in config_text
    assert marker.read_text(encoding="utf-8") == "v2-data\n"

    rolled = rollback(layout)
    assert rolled["outcome"] == "rollback"
    assert rolled["product_version"] == "0.1.0"
    assert not layout.app_version_dir("0.2.0").exists()
    assert layout.app_version_dir("0.1.0").exists()
    assert marker.read_text(encoding="utf-8") == "v1-data\n"

    upgraded2 = upgrade(layout, product_version="0.2.0", bundle_source=v2)
    marker.write_text("post-upgrade\n", encoding="utf-8")
    uninstall_preserve(layout)
    assert not layout.app_root.exists()
    assert marker.is_file()

    restored = reinstall_restore(
        layout,
        product_version="0.1.0",
        bundle_source=v1,
        backup_id=upgraded2["backup_id"],
    )
    assert restored["outcome"] == "reinstall_restore"
    assert layout.app_version_dir("0.1.0").exists()
    assert marker.read_text(encoding="utf-8") == "v1-data\n"


def test_runtime_backup_uses_portable_backup_root(tmp_path: Path) -> None:
    root = tmp_path / "Zhimeng"
    layout = PortableLayout(root)
    bundle = _bundle(tmp_path, "0.1.0")
    install_clean(layout, product_version="0.1.0", bundle_source=bundle)

    kwargs = runtime_settings_kwargs_from_layout(layout)
    settings = RuntimeSettings(
        **kwargs,
        agent_provider_base_url="https://example.invalid/v1",
        agent_provider_api_key="super-secret-key",
        agent_provider_credential_reference="vault://test/provider",
        agent_provider_model="demo-model",
    )
    assert settings.database_path == layout.runtime_database_path
    assert settings.backup_root == layout.runtime_backups_root

    with TestClient(create_app(settings)) as client:
        created = client.post("/api/v1/runtime/backups")
        assert created.status_code == 200, created.text
        body = created.json()
        backup_id = body["receipt"]["backup_id"]
        assert body["receipt"]["formal_draft_allowed"] is False
        assert body["receipt"]["writes_project_truth"] is False
        assert "super-secret-key" not in created.text
        package_dir = layout.runtime_backups_root / backup_id
        assert package_dir.is_dir()
        assert (package_dir / "manifest.json").is_file()
        assert (package_dir / "writing-runtime.sqlite3").is_file()

        listed = client.get("/api/v1/runtime/backups")
        assert listed.status_code == 200, listed.text
        assert any(item["backup_id"] == backup_id for item in listed.json()["backups"])

    uninstall_preserve(layout)
    assert (layout.runtime_backups_root / backup_id / "manifest.json").is_file()


def test_forced_schema_migration_failure_keeps_backup_and_restores(tmp_path: Path) -> None:
    """Simulate a future-schema DB and prove package-test recovery path.

    This is not a real Windows installer black-box, but it covers the Local v1
    forced migration failure + restore invariant for portable packaging.
    """
    import sqlite3

    from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore

    root = tmp_path / "Zhimeng"
    layout = PortableLayout(root)
    v1 = _bundle(tmp_path, "0.1.0")
    v2 = _bundle(tmp_path, "0.2.0")
    install_clean(layout, product_version="0.1.0", bundle_source=v1)

    store = SqliteStore(layout.runtime_database_path)
    health = store.initialize()
    assert health.ready is True
    layout.runtime_data_root.joinpath("author-marker.txt").write_text(
        "before-bad-migration\n",
        encoding="utf-8",
    )

    upgraded = upgrade(layout, product_version="0.2.0", bundle_source=v2)
    backup_id = upgraded["backup_id"]
    assert backup_id

    connection = sqlite3.connect(str(layout.runtime_database_path))
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 99}")
        connection.commit()
    finally:
        connection.close()

    kwargs = runtime_settings_kwargs_from_layout(layout)
    settings = RuntimeSettings(
        **kwargs,
        agent_provider_base_url="https://example.invalid/v1",
        agent_provider_api_key="super-secret-key",
        agent_provider_credential_reference="vault://test/provider",
        agent_provider_model="demo-model",
    )
    with TestClient(create_app(settings)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code != 200 or ready.json().get("ready") is False

    restored = reinstall_restore(
        layout,
        product_version="0.1.0",
        bundle_source=v1,
        backup_id=backup_id,
    )
    assert restored["outcome"] == "reinstall_restore"
    assert layout.runtime_data_root.joinpath("author-marker.txt").read_text(
        encoding="utf-8"
    ) == "before-bad-migration\n"

    recovered = SqliteStore(layout.runtime_database_path).initialize()
    assert recovered.ready is True
    assert recovered.schema_version == SCHEMA_VERSION
