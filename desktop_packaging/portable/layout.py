"""Portable Local v1 layout helpers for Zhimeng desktop packaging package-tests.

This is not a signed Windows EXE installer. It freezes the Local AppData-style
directory roles so install/upgrade/uninstall-preserve paths can be tested before
shell/bundler ADR selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PRODUCT_NAME = "Zhimeng"
CONFIG_NAME = "config.v1.json"
INSTALL_RECEIPT_NAME = "install-receipt.json"
LAYOUT_POLICY = "local-v1-portable-layout-1"


@dataclass(frozen=True)
class PortableLayout:
    root: Path

    @property
    def app_root(self) -> Path:
        return self.root / "app"

    @property
    def data_root(self) -> Path:
        return self.root / "data"

    @property
    def logs_root(self) -> Path:
        return self.root / "logs"

    @property
    def backups_root(self) -> Path:
        return self.root / "backups"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def install_receipt_path(self) -> Path:
        return self.root / INSTALL_RECEIPT_NAME

    @property
    def runtime_data_root(self) -> Path:
        return self.data_root / "runtime"

    @property
    def runtime_database_path(self) -> Path:
        return self.runtime_data_root / "writing-runtime.sqlite3"

    @property
    def runtime_backups_root(self) -> Path:
        # Keep Runtime SQLite packages under the product backups root, not app/.
        return self.backups_root / "runtime"

    @property
    def diagnostics_root(self) -> Path:
        return self.data_root / "diagnostics"

    @property
    def skills_root(self) -> Path:
        return self.data_root / "skills"

    @property
    def knowledge_root(self) -> Path:
        return self.data_root / "knowledge"

    @property
    def runtime_env_path(self) -> Path:
        return self.root / "runtime.env"

    def app_version_dir(self, version: str) -> Path:
        if not version or any(sep in version for sep in ("/", "\\", "..")):
            raise ValueError(f"invalid portable app version: {version!r}")
        return self.app_root / version

    def ensure_user_roots(self) -> None:
        for path in (
            self.app_root,
            self.data_root,
            self.logs_root,
            self.backups_root,
            self.runtime_data_root,
            self.runtime_backups_root,
            self.diagnostics_root,
            self.skills_root,
            self.knowledge_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def default_localappdata_root(local_app_data: Path) -> Path:
    return local_app_data / PRODUCT_NAME
