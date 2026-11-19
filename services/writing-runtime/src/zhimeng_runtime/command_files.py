"""Before/after receipts for explicitly selected command files and directories.

Observation does not claim ownership of external concurrent edits or effects
outside the selected paths. File contents are copied and hashed in chunks.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import threading

from zhimeng_runtime.file_journal import FileChangeEntry, FileChangeJournal, _write_atomic


class CommandFiles:
    def __init__(self, journal: FileChangeJournal):
        self.journal = journal
        self.root = journal.backup_root.parent / "command-file-observations"
        self._lock = threading.RLock()

    @staticmethod
    def _linked(path: Path) -> bool:
        value = path.lstat()
        return stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & 0x400)

    def _scan(self, roots: list[str], folder: Path, *, backup: bool) -> tuple[dict, list]:
        files, problems = {}, []
        seen = set()
        def visit(path: Path):
            key = os.path.normcase(str(path))
            if key in seen:
                return
            seen.add(key)
            try:
                if any(p == self.root or p == self.journal.backup_root for p in (path, *path.parents)):
                    raise ValueError("运行时备份目录不纳入自身观察")
                # Check parents too, so explicit paths through junctions cannot
                # silently expand the requested observation scope.
                if any(self._linked(p) for p in (path, *path.parents) if os.path.lexists(p)):
                    raise ValueError("链接或重解析路径未跟随")
                if not os.path.lexists(path):
                    return
                info = path.stat()
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        for item in entries:
                            visit(Path(item.path))
                    return
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("不是普通文件")
                name = hashlib.sha256(key.encode()).hexdigest() + ".before"
                target = folder / name
                digest = hashlib.sha256()
                with path.open("rb") as source, (target.open("wb") if backup else nullcontext()) as saved:
                    while block := source.read(64 * 1024):
                        digest.update(block)
                        if saved is not None:
                            saved.write(block)
                    if saved is not None:
                        saved.flush()
                        os.fsync(saved.fileno())
                    end = os.fstat(source.fileno())
                current = path.stat()
                identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                if identity(info) != identity(end) or identity(end) != identity(current):
                    raise ValueError("读取期间文件变化，无法核对完整版本")
                files[key] = {"path": str(path), "sha256": digest.hexdigest(),
                              "backup": str(target) if backup else None}
            except (OSError, ValueError, MemoryError) as exc:
                problems.append({"path": str(path), "reason": str(exc)})
        for root in roots:
            visit(Path(root))
        return files, problems

    def prepare(self, *, session_id: str, run_id: str, tool_call_id: str,
                workdir: Path, paths: list[str]) -> dict:
        identity = self.journal.change_id_for(run_id, tool_call_id)
        folder = self.root / identity
        folder.mkdir(parents=True, exist_ok=False)
        roots = list(dict.fromkeys(os.path.abspath(Path(raw).expanduser() if Path(raw).expanduser().is_absolute()
            else workdir / Path(raw).expanduser()) for raw in paths))
        before, problems = self._scan(roots, folder, backup=True)
        state = {"id": identity, "session_id": session_id, "run_id": run_id,
                 "tool_call_id": tool_call_id, "paths": roots, "before": before,
                 "problems": problems, "status": "observing"}
        _write_atomic(folder / "observation.json", json.dumps(state, ensure_ascii=False).encode())
        return self._summary(state)

    @staticmethod
    def _summary(state: dict) -> dict:
        return {key: state[key] for key in ("id", "paths", "status", "problems", "change_id", "file_count") if key in state} | {
            "note": "记录所选路径在命令期间的文件变化；范围外、空目录、网络副作用未覆盖，并发外部改动不能归因给命令。"}

    def finish(self, observation: dict) -> dict:
        with self._lock:
            folder = self.root / observation["id"]
            state = json.loads((folder / "observation.json").read_bytes())
            if state["status"] != "observing":
                return self._summary(state)
            if "observed_entries" not in state:
                after, problems = self._scan(state["paths"], folder, backup=False)
                all_problems = state["problems"] + problems
                def uncertain(path):
                    return any(Path(path).is_relative_to(Path(p["path"])) for p in all_problems)
                changes = []
                for key in sorted(state["before"].keys() | after.keys()):
                    before, current = state["before"].get(key), after.get(key)
                    path = (current or before)["path"]
                    if uncertain(path) or (before and current and before["sha256"] == current["sha256"]):
                        continue
                    changes.append(FileChangeEntry(operation="create" if not before else "delete" if not current else "update",
                        path=path, target_path=None, before_sha256=before["sha256"] if before else None,
                        after_sha256=current["sha256"] if current else None, backup_path=before["backup"] if before else None,
                        created_dirs=()))
                # Freeze the end observation before crossing the SQLite/disk
                # boundary. Retrying persistence cannot claim later human edits.
                state.update(observed_entries=[asdict(entry) for entry in changes], end_problems=all_problems)
                _write_atomic(folder / "observation.json", json.dumps(state, ensure_ascii=False).encode())
            else:
                changes = [FileChangeEntry(**entry) for entry in state["observed_entries"]]
                all_problems = state["end_problems"]
            change = self.journal.record_observed(session_id=state["session_id"], run_id=state["run_id"],
                tool_call_id=state["tool_call_id"], title="命令期间观察到的文件变化", entries=changes)
            state.update(status="partial" if all_problems else "recorded", problems=all_problems,
                         change_id=change.change_id if change else None, file_count=len(change.entries) if change else 0)
            _write_atomic(folder / "observation.json", json.dumps(state, ensure_ascii=False).encode())
            return self._summary(state)

    def finish_safely(self, observation: dict) -> dict:
        try:
            return self.finish(observation)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, MemoryError) as exc:
            return {**observation, "status": "unverified", "problems": [*observation.get("problems", []),
                {"path": "", "reason": f"文件变化记录未完成：{type(exc).__name__}"}]}
