"""Resource revisions and shared in-process locks for settings files.

Revisions describe configuration content, not connection health. A credential
change invalidates an editor too; only the opaque digest leaves the service.
The locks coordinate writers in this process. Arbitrary external editors do
not participate, so stores also compare their source before atomic replace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading


_registry_lock = threading.Lock()
_file_locks: dict[str, threading.RLock] = {}


def settings_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _registry_lock:
        return _file_locks.setdefault(key, threading.RLock())


def settings_revision(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SettingsConflictError(RuntimeError):
    """An editor's base or a writer's source no longer matches the saved file."""

    code = "settings_conflict"

    def __init__(self, message: str = "设置已在其他地方修改。你的草稿仍保留，请核对最新配置后再保存。"):
        super().__init__(message)


def check_settings_revision(expected: str | None, actual: str | None) -> None:
    # Older API clients may omit the precondition. New editors always send the
    # revision captured with their draft, never the newest background response.
    if expected is not None and expected != actual:
        raise SettingsConflictError()
