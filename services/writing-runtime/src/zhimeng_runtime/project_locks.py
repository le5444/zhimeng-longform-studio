from __future__ import annotations

import asyncio
import os
from pathlib import Path


class ProjectLockRegistry:
    """Process-local single-writer locks shared by project API surfaces."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def for_path(self, root: str | Path) -> asyncio.Lock:
        key = os.path.normcase(str(Path(root)))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock
