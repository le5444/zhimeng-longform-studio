"""Bridge portable layout paths into Runtime process settings.

Portable packaging owns durable paths. Runtime still owns SQLite backup/restore
semantics; this module only points Runtime at the portable roots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from desktop_packaging.portable.layout import LAYOUT_POLICY, PortableLayout


def portable_config_dict(
    layout: PortableLayout,
    *,
    product_version: str,
    previous_version: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "zhimeng.portable-config.v1",
        "product_version": product_version,
        "layout_policy": LAYOUT_POLICY,
        "runtime_data_root": str(layout.runtime_data_root),
        "runtime_database_path": str(layout.runtime_database_path),
        "runtime_backups_root": str(layout.runtime_backups_root),
        "diagnostics_root": str(layout.diagnostics_root),
        "backups_root": str(layout.backups_root),
        "logs_root": str(layout.logs_root),
        "skills_root": str(layout.skills_root),
        "knowledge_root": str(layout.knowledge_root),
        "formal_draft_allowed": False,
        "includes_secrets": False,
        "includes_novel_body": False,
        "includes_project_files": False,
    }
    if previous_version is not None:
        payload["previous_version"] = previous_version
    return payload


def write_runtime_env(layout: PortableLayout) -> Path:
    """Write non-secret Runtime path overrides for portable launches."""
    lines = [
        f"ZHIMENG_DATABASE_PATH={layout.runtime_database_path}",
        f"ZHIMENG_BACKUP_ROOT={layout.runtime_backups_root}",
        f"ZHIMENG_DIAGNOSTICS_ROOT={layout.diagnostics_root}",
        f"ZHIMENG_MANAGED_SKILL_ROOT={layout.skills_root}",
        "ZHIMENG_ENVIRONMENT=portable",
    ]
    path = layout.runtime_env_path
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_portable_config(layout: PortableLayout) -> dict[str, Any]:
    if not layout.config_path.is_file():
        raise FileNotFoundError(f"portable config missing: {layout.config_path}")
    return json.loads(layout.config_path.read_text(encoding="utf-8"))


def runtime_settings_kwargs_from_layout(
    layout: PortableLayout,
    *,
    environment: str = "portable",
) -> dict[str, Any]:
    """Return kwargs suitable for zhimeng_runtime.config.RuntimeSettings."""
    return {
        "environment": environment,
        "database_path": layout.runtime_database_path,
        "backup_root": layout.runtime_backups_root,
        "diagnostics_root": layout.diagnostics_root,
        "managed_skill_root": layout.skills_root,
        "trusted_skill_roots": (layout.skills_root,),
        "allowed_origins": (),
    }


__all__ = [
    "load_portable_config",
    "portable_config_dict",
    "runtime_settings_kwargs_from_layout",
    "write_runtime_env",
]
