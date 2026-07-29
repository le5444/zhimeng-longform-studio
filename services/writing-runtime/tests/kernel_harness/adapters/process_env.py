from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


_PASSTHROUGH = {
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "WINDIR",
}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_pinned_binary(
    source: Path,
    isolation_root: Path,
    *,
    expected_sha256: str,
) -> tuple[Path, str]:
    """Copy then verify an executable before any subprocess can invoke it."""

    source = source.resolve(strict=True)
    destination_root = isolation_root / "pinned-bin"
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / source.name
    if destination.exists():
        raise RuntimeError("pinned binary destination already exists")
    temporary = destination.with_suffix(f"{destination.suffix}.copying")
    try:
        shutil.copy2(source, temporary)
        observed = _file_hash(temporary)
        if observed != expected_sha256:
            raise RuntimeError("kernel binary SHA-256 does not match the pinned version")
        temporary.replace(destination)
        return destination, observed
    finally:
        temporary.unlink(missing_ok=True)


def isolated_process_environment(
    isolation_root: Path,
    *,
    extras: dict[str, str] | None = None,
) -> dict[str, str]:
    isolation_root.mkdir(parents=True, exist_ok=True)
    environment: dict[str, str] = {}
    source = {key.upper(): value for key, value in os.environ.items()}
    for key in sorted(_PASSTHROUGH):
        if key in source:
            environment[key] = source[key]

    home = isolation_root / "home"
    appdata = isolation_root / "appdata"
    localappdata = isolation_root / "localappdata"
    temp = isolation_root / "temp"
    for path in (home, appdata, localappdata, temp):
        path.mkdir(parents=True, exist_ok=True)

    drive, tail = os.path.splitdrive(str(home.resolve()))
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HOMEDRIVE": drive,
            "HOMEPATH": tail,
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "TEMP": str(temp),
            "TMP": str(temp),
            "XDG_CONFIG_HOME": str(isolation_root / "xdg-config"),
            "XDG_DATA_HOME": str(isolation_root / "xdg-data"),
            "XDG_CACHE_HOME": str(isolation_root / "xdg-cache"),
            "XDG_STATE_HOME": str(isolation_root / "xdg-state"),
        }
    )
    if extras:
        environment.update(extras)
    return environment
