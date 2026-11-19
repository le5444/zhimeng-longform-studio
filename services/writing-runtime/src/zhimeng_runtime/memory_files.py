"""跨对话的两种记忆（纠偏指令 3.7）：用户记忆（全局，关于这个人）和项目记忆（跟着打开的文件夹走）。

- 用户记忆：本机状态目录里的《用户记忆.md》。每个对话（没打开文件夹的也一样）开头带上一条 role="user"
  消息；设置页「记忆」直接编辑。9-26 以前这份叫《作者偏好.md》，第一次用到时原样改名过来，内容一个字不动。
- 项目说明 / 项目记忆：打开的文件夹根目录里的 AGENTS.md（Codex 等工具通用的项目说明文件），也认 CLAUDE.md；
  老书根里的《写作笔记.md》是以前的项目记忆，照样带上。打开了文件夹的对话开头带上它们的内容。
  memory_write(scope=project) 往 AGENTS.md 末尾追加一行，走修改记录，能撤回。

项目里的每份文件最多带 32 KiB（Codex 读 AGENTS.md 的默认上限 project_doc_max_bytes 也是 32 KiB），多的只带前面。

用户记忆开关（作者：「agent根据我对话直接记忆，可以开关和编辑」）：状态目录里的 memory-settings.json，默认开着。
关着时不注入或自动提取用户记忆，memory_write(scope=user) 也停用。
项目说明照旧读取，memory_write(scope=project) 按当前文件写权限保存，和此开关分开。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.project_instruction_context import (
    LEGACY_PROJECT_NOTES_FILE_NAME,
    MAX_PROJECT_INSTRUCTION_BYTES,
    PROJECT_INSTRUCTION_FILE_NAMES,
    PROJECT_MEMORY_FILE_NAME,
    read_project_instructions,
)

USER_MEMORY_FILE_NAME = "用户记忆.md"
LEGACY_USER_MEMORY_FILE_NAME = "作者偏好.md"
MAX_USER_MEMORY_BYTES = 128 * 1024
MEMORY_SETTINGS_FILE_NAME = "memory-settings.json"

_USER_MEMORY_LOCKS: dict[str, threading.RLock] = {}
_USER_MEMORY_LOCKS_GUARD = threading.Lock()


@contextmanager
def user_memory_transaction(state_root: Path):
    """同一份记忆的读取、比较、写入一起完成，设置页和后台追加共用。"""
    key = os.path.normcase(str(Path(state_root).resolve()))
    with _USER_MEMORY_LOCKS_GUARD:
        lock = _USER_MEMORY_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def user_memory_path(state_root: Path) -> Path:
    """《用户记忆.md》的位置；旧的《作者偏好.md》还在而新文件没有时，先原样改名过来。"""

    root = Path(state_root)
    path = root / USER_MEMORY_FILE_NAME
    legacy = root / LEGACY_USER_MEMORY_FILE_NAME
    if not path.exists() and legacy.is_file():
        try:
            os.replace(legacy, path)
        except OSError:
            return legacy
    return path


def _read_memory_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def read_user_memory(state_root: Path, *, strict: bool = False) -> str:
    """上下文可降级读取；编辑与保存必须完整读取，不能用空值或截断内容代替旧版本。"""

    try:
        raw = _read_memory_bytes(user_memory_path(state_root))
    except OSError:
        if strict:
            raise
        return ""
    if not strict and len(raw) > MAX_USER_MEMORY_BYTES:
        raw = raw[:MAX_USER_MEMORY_BYTES]
    return raw.decode("utf-8-sig", errors="strict" if strict else "replace")


def _write_atomic(path: Path, encoded: bytes) -> None:
    """同目录临时文件 + fsync + os.replace：写一半出错时原文件不动。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(Path(temp_name), path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_user_memory(state_root: Path, content: str) -> Path:
    """整份改写《用户记忆.md》，拒绝写坏。"""

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_USER_MEMORY_BYTES:
        raise ValueError("user memory exceeds the byte limit")
    with user_memory_transaction(state_root):
        path = user_memory_path(state_root)
        original = _read_memory_bytes(path)
        existing = original.decode("utf-8-sig")
        if existing == content and path.exists():
            return path
        if original.startswith(b"\xef\xbb\xbf"):
            encoded = b"\xef\xbb\xbf" + encoded
        # 先保留修改前版本；设置页和 Agent 的自动追加走同一条写入路径。
        version = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        _write_atomic(
            Path(state_root) / "memory-history" / f"{version}.json",
            json.dumps({
                "version": version,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "content": existing,
            }, ensure_ascii=False).encode("utf-8"),
        )
        _write_atomic(path, encoded)
        return path


def memory_enabled(state_root: Path, *, strict: bool = False) -> bool:
    """首次未设置时开启；已有设置不可读时暂停，不能丢失用户的关闭选择。"""

    try:
        settings = json.loads((Path(state_root) / MEMORY_SETTINGS_FILE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        if strict:
            raise
        return False
    if not isinstance(settings, dict) or type(settings.get("enabled")) is not bool:
        if strict:
            raise ValueError("Memory settings must contain a boolean enabled value")
        return False
    return settings["enabled"]


def set_memory_enabled(state_root: Path, enabled: bool) -> None:
    payload = {"schema": "zhimeng.memory-settings.v1", "enabled": bool(enabled)}
    with user_memory_transaction(state_root):
        _write_atomic(
            Path(state_root) / MEMORY_SETTINGS_FILE_NAME,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )


def last_memory_line(existing: str) -> str:
    """现有内容的最后一行非空行（去掉开头的「- 」）；没有就是「」。"""

    for line in reversed(existing.splitlines()):
        if line.strip():
            return line.strip().removeprefix("- ").strip()
    return ""


def append_user_memory(state_root: Path, text: str) -> tuple[Path, bool]:
    """把一句话追加进《用户记忆.md》，一行一条；空句子不写。

    返回（路径，是否真的写了）：新句子与最后一行一模一样时不重复记（模型偶尔会把同一句连着记两次）。
    """

    line = text.strip()
    if not line:
        raise ValueError("memory text must not be blank")
    with user_memory_transaction(state_root):
        existing = read_user_memory(state_root, strict=True)
        if last_memory_line(existing) == line:
            return user_memory_path(state_root), False
        newline = "\r\n" if "\r\n" in existing else "\n"
        content = f"{existing.rstrip()}{newline}{line}{newline}" if existing.strip() else f"{line}{newline}"
        return write_user_memory(state_root, content), True


def append_project_memory_line(existing: str | None, text: str) -> str:
    """AGENTS.md 追加一行「- 这句话」；文件没有就建一个只有「## 记住的事」的；和最后一行一样就原样返回。"""

    line = text.strip()
    if existing is None or not existing.strip():
        return f"## 记住的事\n\n- {line}\n"
    if last_memory_line(existing) == line:
        return existing
    return f"{existing.rstrip()}\n- {line}\n"
