"""Portable install/upgrade/uninstall helpers for Local v1 package-tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from desktop_packaging.portable.layout import (
    INSTALL_RECEIPT_NAME,
    LAYOUT_POLICY,
    PortableLayout,
)
from desktop_packaging.portable.runtime_bridge import (
    portable_config_dict,
    write_runtime_env,
)


class PortablePackagingError(RuntimeError):
    pass


class PortablePackagingConflict(PortablePackagingError):
    pass


class PortablePackagingStateError(PortablePackagingError):
    pass


Outcome = Literal[
    "clean_install",
    "upgrade",
    "repair",
    "reinstall_restore",
    "rollback",
    "uninstall_preserve",
    "uninstall_delete_confirmed",
]


def _now() -> datetime:
    return datetime.now(UTC)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise PortablePackagingConflict(f"destination already exists: {destination}")
    shutil.copytree(source, destination)




def _seed_managed_skills(layout: PortableLayout, *, bundle_dir: Path) -> list[str]:
    """Copy bundle managed-skills into data/skills without overwriting existing packages."""
    source = bundle_dir / "managed-skills"
    if not source.is_dir():
        return []
    layout.skills_root.mkdir(parents=True, exist_ok=True)
    seeded: list[str] = []
    for child in sorted(source.iterdir()):
        if not child.is_dir():
            continue
        target = layout.skills_root / child.name
        if target.exists():
            continue
        shutil.copytree(child, target)
        seeded.append(child.name)
    return seeded


def _write_portable_config(
    layout: PortableLayout,
    *,
    product_version: str,
    previous_version: str | None = None,
) -> None:
    _atomic_write_json(
        layout.config_path,
        portable_config_dict(
            layout,
            product_version=product_version,
            previous_version=previous_version,
        ),
    )
    write_runtime_env(layout)


def _receipt(
    *,
    outcome: Outcome,
    layout: PortableLayout,
    product_version: str,
    previous_version: str | None = None,
    preserved_paths: list[str] | None = None,
    removed_paths: list[str] | None = None,
    backup_id: str | None = None,
) -> dict[str, Any]:
    created_at = _now()
    material = {
        "schema": "zhimeng.portable-install-receipt.v1",
        "outcome": outcome,
        "product_version": product_version,
        "previous_version": previous_version,
        "layout_policy": LAYOUT_POLICY,
        "root": str(layout.root),
        "app_version_dir": (
            str(layout.app_version_dir(product_version))
            if outcome
            in {
                "clean_install",
                "upgrade",
                "repair",
                "reinstall_restore",
                "rollback",
            }
            else None
        ),
        "data_root": str(layout.data_root),
        "backups_root": str(layout.backups_root),
        "runtime_database_path": str(layout.runtime_database_path),
        "runtime_backups_root": str(layout.runtime_backups_root),
        "preserved_paths": preserved_paths or [],
        "removed_paths": removed_paths or [],
        "backup_id": backup_id,
        "includes_secrets": False,
        "includes_novel_body": False,
        "includes_project_files": False,
        "formal_draft_allowed": False,
        "created_at": created_at.isoformat(),
    }
    receipt_id = (
        "portable-install-"
        + hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
    )
    material["receipt_id"] = receipt_id
    return material


def read_install_receipt(layout: PortableLayout) -> dict[str, Any] | None:
    path = layout.install_receipt_path
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def current_installed_version(layout: PortableLayout) -> str | None:
    receipt = read_install_receipt(layout)
    if receipt is None:
        return None
    version = receipt.get("product_version")
    return str(version) if version else None


def install_clean(
    layout: PortableLayout,
    *,
    product_version: str,
    bundle_source: Path,
) -> dict[str, Any]:
    if not bundle_source.is_dir():
        raise PortablePackagingStateError("bundle source must be a directory")
    if layout.install_receipt_path.exists():
        raise PortablePackagingConflict("portable root already has an install receipt")
    version_dir = layout.app_version_dir(product_version)
    if version_dir.exists():
        raise PortablePackagingConflict(f"app version already exists: {product_version}")

    layout.ensure_user_roots()
    _copy_tree(bundle_source, version_dir)
    seeded = _seed_managed_skills(layout, bundle_dir=version_dir)
    _write_portable_config(layout, product_version=product_version)
    _write_text(
        layout.runtime_data_root / "runtime-marker.txt",
        f"installed={product_version}\n",
    )
    if seeded:
        _write_text(
            layout.skills_root / ".seeded-from-bundle.txt",
            "\n".join(seeded) + "\n",
        )
    receipt = _receipt(
        outcome="clean_install",
        layout=layout,
        product_version=product_version,
        preserved_paths=[],
        removed_paths=[],
    )
    _atomic_write_json(layout.install_receipt_path, receipt)
    return receipt


def _create_upgrade_backup(layout: PortableLayout, *, product_version: str) -> str:
    created_at = _now()
    backup_id = (
        "portable-backup-"
        + hashlib.sha256(
            f"{layout.root}\0{product_version}\0{created_at.isoformat()}".encode("utf-8")
        ).hexdigest()[:32]
    )
    backup_dir = layout.backups_root / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    if layout.data_root.exists():
        shutil.copytree(layout.data_root, backup_dir / "data")
    if layout.config_path.exists():
        shutil.copy2(layout.config_path, backup_dir / "config.v1.json")
    # Runtime SQLite backups live under backups/runtime and are included via data? 
    # No: runtime_backups_root is under backups/, not data/. Copy that too.
    if layout.runtime_backups_root.exists():
        shutil.copytree(layout.runtime_backups_root, backup_dir / "runtime-backups")
    manifest = {
        "schema": "zhimeng.portable-backup-manifest.v1",
        "backup_id": backup_id,
        "product_version": product_version,
        "created_at": created_at.isoformat(),
        "includes_secrets": False,
        "includes_novel_body": False,
        "includes_project_files": False,
        "formal_draft_allowed": False,
        "paths": ["data", "config.v1.json", "runtime-backups"],
    }
    _atomic_write_json(backup_dir / "manifest.json", manifest)
    return backup_id


def upgrade(
    layout: PortableLayout,
    *,
    product_version: str,
    bundle_source: Path,
) -> dict[str, Any]:
    if not bundle_source.is_dir():
        raise PortablePackagingStateError("bundle source must be a directory")
    previous = current_installed_version(layout)
    if previous is None:
        raise PortablePackagingStateError("upgrade requires an existing install receipt")
    if previous == product_version:
        raise PortablePackagingConflict("target version is already installed")
    version_dir = layout.app_version_dir(product_version)
    if version_dir.exists():
        raise PortablePackagingConflict(f"app version already exists: {product_version}")

    backup_id = _create_upgrade_backup(layout, product_version=previous)
    layout.ensure_user_roots()
    _copy_tree(bundle_source, version_dir)
    _seed_managed_skills(layout, bundle_dir=version_dir)
    _write_portable_config(
        layout,
        product_version=product_version,
        previous_version=previous,
    )
    _write_text(
        layout.runtime_data_root / "runtime-marker.txt",
        f"installed={product_version}\nprevious={previous}\n",
    )
    receipt = _receipt(
        outcome="upgrade",
        layout=layout,
        product_version=product_version,
        previous_version=previous,
        preserved_paths=[
            str(layout.data_root),
            str(layout.backups_root),
            str(layout.app_version_dir(previous)),
        ],
        removed_paths=[],
        backup_id=backup_id,
    )
    _atomic_write_json(layout.install_receipt_path, receipt)
    return receipt


def repair(layout: PortableLayout) -> dict[str, Any]:
    existing_receipt = read_install_receipt(layout)
    previous = current_installed_version(layout)
    if previous is None or existing_receipt is None:
        raise PortablePackagingStateError("repair requires an existing install receipt")
    version_dir = layout.app_version_dir(previous)
    if not version_dir.is_dir():
        raise PortablePackagingStateError(
            f"repair cannot find installed app version directory: {previous}"
        )
    layout.ensure_user_roots()
    config_previous = None
    if layout.config_path.is_file():
        try:
            existing = json.loads(layout.config_path.read_text(encoding="utf-8"))
            value = existing.get("previous_version")
            config_previous = str(value) if value else None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            config_previous = None
    if config_previous is None:
        value = existing_receipt.get("previous_version")
        config_previous = str(value) if value else None
    backup_id = existing_receipt.get("backup_id")
    backup_id_text = str(backup_id) if backup_id else None
    _write_portable_config(
        layout,
        product_version=previous,
        previous_version=config_previous,
    )
    receipt = _receipt(
        outcome="repair",
        layout=layout,
        product_version=previous,
        previous_version=config_previous,
        preserved_paths=[
            str(layout.data_root),
            str(layout.backups_root),
            str(version_dir),
        ],
        removed_paths=[],
        backup_id=backup_id_text,
    )
    _atomic_write_json(layout.install_receipt_path, receipt)
    return receipt


def reinstall_restore(
    layout: PortableLayout,
    *,
    product_version: str,
    bundle_source: Path,
    backup_id: str,
) -> dict[str, Any]:
    if not bundle_source.is_dir():
        raise PortablePackagingStateError("bundle source must be a directory")
    backup_dir = layout.backups_root / backup_id
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise PortablePackagingStateError(f"portable backup not found: {backup_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortablePackagingStateError("portable backup manifest is unreadable") from exc
    if str(manifest.get("backup_id")) != backup_id:
        raise PortablePackagingConflict("portable backup identity mismatch")

    if layout.app_root.exists():
        shutil.rmtree(layout.app_root)
    version_dir = layout.app_version_dir(product_version)
    layout.ensure_user_roots()
    _copy_tree(bundle_source, version_dir)

    data_backup = backup_dir / "data"
    if data_backup.is_dir():
        if layout.data_root.exists():
            shutil.rmtree(layout.data_root)
        shutil.copytree(data_backup, layout.data_root)
    runtime_backup_src = backup_dir / "runtime-backups"
    if runtime_backup_src.is_dir():
        if layout.runtime_backups_root.exists():
            shutil.rmtree(layout.runtime_backups_root)
        shutil.copytree(runtime_backup_src, layout.runtime_backups_root)
    config_backup = backup_dir / "config.v1.json"
    if config_backup.is_file():
        shutil.copy2(config_backup, layout.config_path)
    else:
        _write_portable_config(layout, product_version=product_version)
    write_runtime_env(layout)
    layout.ensure_user_roots()

    receipt = _receipt(
        outcome="reinstall_restore",
        layout=layout,
        product_version=product_version,
        previous_version=str(manifest.get("product_version") or "") or None,
        preserved_paths=[str(backup_dir)],
        removed_paths=[],
        backup_id=backup_id,
    )
    _atomic_write_json(layout.install_receipt_path, receipt)
    return receipt


def rollback(layout: PortableLayout) -> dict[str, Any]:
    receipt = read_install_receipt(layout)
    if receipt is None:
        raise PortablePackagingStateError("rollback requires an existing install receipt")
    current = str(receipt.get("product_version") or "")
    previous = str(receipt.get("previous_version") or "")
    if not current or not previous:
        raise PortablePackagingStateError("rollback requires current and previous versions")
    # Repair may rewrite the latest outcome; previous_version + kept app dir still authorize rollback.
    if str(receipt.get("outcome")) not in {"upgrade", "repair"}:
        raise PortablePackagingStateError(
            "rollback requires an upgrade (or repaired post-upgrade) install state"
        )
    previous_dir = layout.app_version_dir(previous)
    if not previous_dir.is_dir():
        raise PortablePackagingStateError(
            f"rollback target app version is missing: {previous}"
        )
    current_dir = layout.app_version_dir(current)
    backup_id = str(receipt.get("backup_id") or "") or None
    if backup_id:
        backup_dir = layout.backups_root / backup_id
        data_backup = backup_dir / "data"
        if data_backup.is_dir():
            if layout.data_root.exists():
                shutil.rmtree(layout.data_root)
            shutil.copytree(data_backup, layout.data_root)
        runtime_backup_src = backup_dir / "runtime-backups"
        if runtime_backup_src.is_dir():
            if layout.runtime_backups_root.exists():
                shutil.rmtree(layout.runtime_backups_root)
            shutil.copytree(runtime_backup_src, layout.runtime_backups_root)
        config_backup = backup_dir / "config.v1.json"
        if config_backup.is_file():
            shutil.copy2(config_backup, layout.config_path)
    _write_portable_config(layout, product_version=previous)
    write_runtime_env(layout)
    layout.ensure_user_roots()
    removed: list[str] = []
    if current_dir.exists():
        shutil.rmtree(current_dir)
        removed.append(str(current_dir))
    result = _receipt(
        outcome="rollback",
        layout=layout,
        product_version=previous,
        previous_version=current,
        preserved_paths=[
            str(previous_dir),
            str(layout.data_root),
            str(layout.backups_root),
        ],
        removed_paths=removed,
        backup_id=backup_id,
    )
    _atomic_write_json(layout.install_receipt_path, result)
    return result


def uninstall_preserve(layout: PortableLayout) -> dict[str, Any]:
    previous = current_installed_version(layout)
    if previous is None:
        raise PortablePackagingStateError("uninstall requires an existing install receipt")
    removed: list[str] = []
    if layout.app_root.exists():
        shutil.rmtree(layout.app_root)
        removed.append(str(layout.app_root))
    preserved = [
        str(layout.data_root),
        str(layout.backups_root),
        str(layout.logs_root),
        str(layout.config_path),
        str(layout.runtime_env_path),
    ]
    receipt = _receipt(
        outcome="uninstall_preserve",
        layout=layout,
        product_version=previous,
        previous_version=previous,
        preserved_paths=preserved,
        removed_paths=removed,
    )
    _atomic_write_json(layout.install_receipt_path, receipt)
    return receipt


def uninstall_delete_confirmed(layout: PortableLayout, *, confirm_root: str) -> dict[str, Any]:
    if Path(confirm_root).resolve() != layout.root.resolve():
        raise PortablePackagingConflict(
            "delete confirmation root does not match portable install root"
        )
    previous = current_installed_version(layout) or "unknown"
    removed: list[str] = []
    for path in (
        layout.app_root,
        layout.data_root,
        layout.logs_root,
        layout.backups_root,
        layout.config_path,
        layout.runtime_env_path,
        layout.install_receipt_path,
    ):
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.is_file():
            path.unlink()
            removed.append(str(path))
    receipt = _receipt(
        outcome="uninstall_delete_confirmed",
        layout=layout,
        product_version=previous,
        previous_version=previous,
        preserved_paths=[],
        removed_paths=removed,
    )
    layout.root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(layout.root / INSTALL_RECEIPT_NAME, receipt)
    return receipt


__all__ = [
    "PortablePackagingConflict",
    "PortablePackagingError",
    "PortablePackagingStateError",
    "current_installed_version",
    "install_clean",
    "read_install_receipt",
    "reinstall_restore",
    "repair",
    "rollback",
    "uninstall_delete_confirmed",
    "uninstall_preserve",
    "upgrade",
]
