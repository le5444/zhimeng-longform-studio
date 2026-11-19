"""打开的文件夹以外的写入记录：没打开文件夹、或给了文件夹以外的绝对路径时，写文件工具走这里。

打开的项目文件夹里的写入照旧走 ChangeSet（修改卡、撤回按钮都在那边）；这里管其余位置
（桌面、用户主目录、别的盘……），做法照 Claude Code 的 file-history：写之前把原内容备份到
状态目录 file-history/<change_id>/，数据库 file_change 表一文件一行。撤回时先核对每个文件
还是写完时的样子（哈希对得上）才还原；后来被别人又改过的，原样留着、如实说，一个都不动。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.file_commit import FileCommitError, commit_delete, commit_move, commit_write

FileChangeOperation = Literal["create", "update", "delete", "move", "mkdir"]
CHANGE_ID_PREFIX = "filechange-"


class _ConnectionSource(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


class FileJournalError(RuntimeError):
    """code：not_found / already_undone / conflict / invalid / rollback_failed。"""

    def __init__(self, message: str, *, code: str, untouched: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.untouched = untouched


@dataclass(frozen=True, slots=True)
class PlannedWrite:
    path: Path
    content: bytes
    # None leaves legacy callers unconditional; "" expects a missing file.
    expected_before_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedDelete:
    path: Path
    expected_before_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedMove:
    path: Path
    target: Path


@dataclass(frozen=True, slots=True)
class PlannedMkdir:
    path: Path


PlannedOperation = PlannedWrite | PlannedDelete | PlannedMove | PlannedMkdir


@dataclass(frozen=True, slots=True)
class FileChangeEntry:
    operation: FileChangeOperation
    path: str
    target_path: str | None
    before_sha256: str | None
    after_sha256: str | None
    backup_path: str | None
    created_dirs: tuple[str, ...]
    sequence: int = 0
    undone_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FileChange:
    change_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    title: str
    created_at: datetime
    undone_at: datetime | None
    entries: tuple[FileChangeEntry, ...]
    apply_phase: Literal["legacy", "pending", "applied", "failed"] = "legacy"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(64 * 1024):
                digest.update(block)
        return digest.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError, NotADirectoryError):
        return None


def _missing_dirs(folder: Path) -> list[Path]:
    """folder 及其还不存在的上级，最外层在前。"""

    missing: list[Path] = []
    cursor = folder
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    missing.reverse()
    return missing


def _write_atomic(target: Path, content: bytes) -> None:
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(Path(temp_name), target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _remove_empty_dirs(created: tuple[str, ...]) -> None:
    for raw in reversed(created):
        try:
            Path(raw).rmdir()
        except OSError:
            return  # 里面有东西了（或已经没了），留着


class FileChangeJournal:
    def __init__(self, store: _ConnectionSource, state_root: Path) -> None:
        self.store = store
        self.backup_root = state_root / "file-history"
        self._mutation_lock = threading.RLock()

    @staticmethod
    def change_id_for(run_id: str, tool_call_id: str) -> str:
        digest = hashlib.sha256(f"{run_id}\n{tool_call_id}".encode("utf-8")).hexdigest()
        return f"{CHANGE_ID_PREFIX}{digest[:32]}"

    # ------------------------------------------------------------------
    # 读

    def get(self, change_id: str) -> FileChange | None:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM file_change WHERE change_id = ? ORDER BY seq",
                (change_id,),
            ).fetchall()
        return self._change_from_rows(rows) if rows else None

    def open_changes(self, session_id: str) -> list[FileChange]:
        """这场对话里还没撤回的写入，最近的在前。"""

        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM file_change
                WHERE session_id = ? AND undone_at IS NULL
                ORDER BY created_at DESC, change_id, seq
                """,
                (session_id,),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["change_id"]), []).append(row)
        return [self._change_from_rows(items) for items in grouped.values()]

    def session_changes(self, session_id: str) -> list[FileChange]:
        """保留已撤回的记录，供对话的修改记录查看。"""
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM file_change WHERE session_id = ? ORDER BY created_at DESC, change_id, seq",
                (session_id,),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["change_id"]), []).append(row)
        return [self._change_from_rows(items) for items in grouped.values()]

    # ------------------------------------------------------------------
    # 写

    def record_observed(self, *, session_id: str, run_id: str, tool_call_id: str,
                        title: str, entries: list[FileChangeEntry]) -> FileChange | None:
        """Record already-observed bytes, without writing the destinations again.

        Callers retain a verified before-image. This is an observation interval,
        not proof that a particular process exclusively caused every change.
        Undo uses the same after-hash and backup checks as ordinary file tools.
        """
        if not entries:
            return None
        change_id = self.change_id_for(run_id, tool_call_id)
        with self._mutation_lock:
            existing = self.get(change_id)
            if existing is not None:
                return existing
            for entry in entries:
                if entry.before_sha256 is not None and (
                    not entry.backup_path or _file_sha256(Path(entry.backup_path)) != entry.before_sha256
                ):
                    raise FileJournalError("观察前的文件备份无法核对，未生成可撤回声明。", code="invalid")
            created_at = datetime.now(UTC)
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for seq, entry in enumerate(entries):
                    connection.execute("""INSERT INTO file_change(
                        change_id, seq, session_id, run_id, tool_call_id, title, operation, path,
                        target_path, before_sha256, after_sha256, backup_path, created_dirs_json,
                        created_at, apply_phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'applied')""",
                        (change_id, seq, session_id, run_id, tool_call_id, title, entry.operation,
                         entry.path, entry.target_path, entry.before_sha256, entry.after_sha256,
                         entry.backup_path, json.dumps(list(entry.created_dirs)), created_at.isoformat()))
                connection.commit()
            return self.get(change_id)

    def apply(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        title: str,
        operations: list[PlannedOperation],
    ) -> FileChange:
        # 工具和界面的撤回共用这一把锁，核对与写入不会彼此穿插。
        with self._mutation_lock:
            return self._apply(session_id=session_id, run_id=run_id, tool_call_id=tool_call_id,
                               title=title, operations=operations)

    def _apply(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        title: str,
        operations: list[PlannedOperation],
    ) -> FileChange:
        if not operations:
            raise FileJournalError("没有要写的内容。", code="invalid")
        change_id = self.change_id_for(run_id, tool_call_id)
        existing = self.get(change_id)
        if existing is not None:
            if len(existing.entries) != len(operations):
                raise FileJournalError(
                    f"上次写入未完整完成，尚未还原文件的备份和撤回记录已保留（修改编号 {change_id}）。",
                    code="rollback_failed",
                )
            if existing.undone_at is None and existing.apply_phase in {"pending", "failed"}:
                raise FileJournalError(
                    f"这次写入没有完整成功的记录（修改编号 {change_id}）；现有文件与备份已保留，请核对或撤回。",
                    code="rollback_failed",
                )
            if existing.undone_at is None and any(
                entry.undone_at is not None or not self._matches_after(entry)
                for entry in existing.entries
            ):
                raise FileJournalError(
                    f"上次写入未能确认完整落盘，或文件后来有了变化（修改编号 {change_id}）。"
                    "现有文件、原内容备份和撤回记录已保留，可核对后撤回。",
                    code="conflict",
                )
            return existing  # 同一次工具调用重放：已经写过，不再写第二遍
        backup_dir = self.backup_root / change_id
        entries: list[FileChangeEntry] = []
        backups: list[tuple[Path, bytes]] = []
        for seq, operation in enumerate(operations):
            entry, backup = self._plan(operation, backup_dir / f"{seq}.before")
            entry = replace(entry, sequence=seq)
            entries.append(entry)
            if backup is not None:
                backups.append(backup)
        for path, data in backups:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, data)
        created_at = datetime.now(UTC)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for seq, entry in enumerate(entries):
                    connection.execute(
                        """
                        INSERT INTO file_change(
                            change_id, seq, session_id, run_id, tool_call_id, title,
                            operation, path, target_path, before_sha256, after_sha256,
                            backup_path, created_dirs_json, created_at, apply_phase
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            change_id, seq, session_id, run_id, tool_call_id, title,
                            entry.operation, entry.path, entry.target_path,
                            entry.before_sha256, entry.after_sha256, entry.backup_path,
                            json.dumps(list(entry.created_dirs), ensure_ascii=False),
                            created_at.isoformat(),
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        attempted: list[tuple[int, FileChangeEntry]] = []
        try:
            for seq, (entry, operation) in enumerate(zip(entries, operations, strict=True)):
                # A failed move/copy can already have changed disk before raising.
                attempted.append((seq, entry))
                self._perform(entry, operation)
            with self.store.connection() as connection:
                connection.execute("UPDATE file_change SET apply_phase = 'applied' WHERE change_id = ?", (change_id,))
        except BaseException as write_error:
            if isinstance(write_error, FileJournalError) and write_error.untouched:
                # A rejected commit never owns the current destination bytes,
                # even if an outside edit happens to equal the proposed content.
                attempted.pop()
            # 已还原和未执行的条目可以清掉；还原失败的保留记录和原文备份，供后续撤回。
            unreverted: list[tuple[int, FileChangeEntry]] = []
            for seq, entry in reversed(attempted):
                try:
                    if self._matches_before(entry):
                        _remove_empty_dirs(entry.created_dirs)
                        continue
                    # Compensation follows the same version checks as explicit undo.
                    if self._undo_problem(entry) is not None:
                        unreverted.append((seq, entry))
                        continue
                    self._reverse(entry)
                except OSError:
                    unreverted.append((seq, entry))
            if unreverted:
                remaining = {seq for seq, _ in unreverted}
                with self.store.connection() as connection:
                    connection.execute("UPDATE file_change SET apply_phase = 'failed' WHERE change_id = ?", (change_id,))
                    connection.executemany(
                        "DELETE FROM file_change WHERE change_id = ? AND seq = ?",
                        [(change_id, seq) for seq in range(len(entries)) if seq not in remaining],
                    )
                paths = "、".join(f"「{entry.path}」" for _, entry in unreverted)
                raise FileJournalError(
                    f"写入失败，{paths}尚未还原。现有文件、可用备份和修改记录已保留"
                    f"（修改编号 {change_id}），可核对后撤回。",
                    code="rollback_failed",
                ) from write_error
            with self.store.connection() as connection:
                connection.execute("DELETE FROM file_change WHERE change_id = ?", (change_id,))
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise
        return FileChange(
            change_id=change_id,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            title=title,
            created_at=created_at,
            undone_at=None,
            entries=tuple(entries),
            apply_phase="applied",
        )

    def undo(self, change_id: str) -> FileChange:
        with self._mutation_lock:
            return self._undo(change_id)

    def _undo(self, change_id: str) -> FileChange:
        change = self.get(change_id)
        if change is None:
            raise FileJournalError(f"没有这条写入记录：{change_id}", code="not_found")
        if change.undone_at is not None:
            raise FileJournalError("这次写入已经撤回过了。", code="already_undone")
        # 先全部核对，再动手：有一个文件后来被改过，就一个都不动。
        pending = [entry for entry in change.entries if entry.undone_at is None]
        for entry in pending:
            if self._matches_before(entry):
                continue
            problem = self._undo_problem(entry, restore_missing=change.apply_phase in {"pending", "failed"})
            if problem is not None:
                raise FileJournalError(problem, code="conflict")
        completed = len(change.entries) - len(pending)
        for entry in reversed(pending):
            try:
                # 落盘前中断，或撤回落盘后记账前中断，都可能已经是原内容。
                if not self._matches_before(entry):
                    restore_missing = (
                        entry.operation == "update" and change.apply_phase in {"pending", "failed"}
                        and not os.path.lexists(Path(entry.path))
                    )
                    self._reverse(replace(entry, operation="delete") if restore_missing else entry)
            except FileCommitError as exc:
                raise FileJournalError(
                    f"这次修改已有 {completed}/{len(change.entries)} 个文件撤回；{exc}",
                    code="conflict" if exc.conflict else "undo_failed",
                ) from exc
            except OSError as exc:
                raise FileJournalError(
                    f"这次修改已有 {completed}/{len(change.entries)} 个文件撤回；「{entry.path}」暂时无法还原。"
                    "原内容备份和未完成记录已保留，解决文件占用或权限问题后可以继续撤回。",
                    code="undo_failed",
                ) from exc
            # 一笔可能含多个文件；逐个记下结果，失败后重试只处理剩余文件。
            with self.store.connection() as connection:
                connection.execute(
                    "UPDATE file_change SET undone_at = ? WHERE change_id = ? AND seq = ?",
                    (datetime.now(UTC).isoformat(), change_id, entry.sequence),
                )
            completed += 1
        updated = self.get(change_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------

    @staticmethod
    def _plan(operation: PlannedOperation, backup_path: Path) -> tuple[FileChangeEntry, tuple[Path, bytes] | None]:
        if isinstance(operation, PlannedWrite):
            target = operation.path
            if target.is_dir():
                raise FileJournalError(f"「{target}」是一个文件夹，不能当文件写。", code="invalid")
            before: bytes | None = target.read_bytes() if target.exists() else None
            if operation.expected_before_sha256 is not None:
                actual = "" if before is None else _sha256(before)
                if actual != operation.expected_before_sha256:
                    raise FileJournalError(
                        f"读取后「{target}」已被修改或撤回，本次没有写入。重新读取当前文件后可以继续修改。",
                        code="write_conflict",
                    )
            created = _missing_dirs(target.parent)
            entry = FileChangeEntry(
                operation="update" if before is not None else "create",
                path=str(target),
                target_path=None,
                before_sha256=None if before is None else _sha256(before),
                after_sha256=_sha256(operation.content),
                backup_path=None if before is None else str(backup_path),
                created_dirs=tuple(str(item) for item in created),
            )
            return entry, None if before is None else (backup_path, before)
        if isinstance(operation, PlannedDelete):
            target = operation.path
            if not target.is_file():
                raise FileJournalError(f"没有找到这个文件：{target}", code="not_found")
            before = target.read_bytes()
            if operation.expected_before_sha256 is not None and _sha256(before) != operation.expected_before_sha256:
                raise FileJournalError("核对后文件又被修改，本次没有移除。请重新核对。", code="write_conflict")
            entry = FileChangeEntry(
                operation="delete",
                path=str(target),
                target_path=None,
                before_sha256=_sha256(before),
                after_sha256=None,
                backup_path=str(backup_path),
                created_dirs=(),
            )
            return entry, (backup_path, before)
        if isinstance(operation, PlannedMove):
            source, target = operation.path, operation.target
            if not source.is_file():
                raise FileJournalError(f"没有找到这个文件：{source}", code="not_found")
            if os.path.lexists(target):
                raise FileJournalError(f"目标位置已经有东西了：{target}", code="invalid")
            digest = _sha256(source.read_bytes())
            created = _missing_dirs(target.parent)
            entry = FileChangeEntry(
                operation="move",
                path=str(source),
                target_path=str(target),
                before_sha256=digest,
                after_sha256=digest,
                backup_path=None,
                created_dirs=tuple(str(item) for item in created),
            )
            return entry, None
        target = operation.path
        if os.path.lexists(target):
            raise FileJournalError(f"这个位置已经有东西了：{target}", code="invalid")
        entry = FileChangeEntry(
            operation="mkdir",
            path=str(target),
            target_path=None,
            before_sha256=None,
            after_sha256=None,
            backup_path=None,
            created_dirs=tuple(str(item) for item in _missing_dirs(target)),
        )
        return entry, None

    @staticmethod
    def _perform(entry: FileChangeEntry, operation: PlannedOperation) -> None:
        if entry.backup_path and _file_sha256(Path(entry.backup_path)) != entry.before_sha256:
            raise FileJournalError("原内容备份暂时无法核对，本次没有提交；现有文件已保留。",
                                   code="write_busy", untouched=True)
        try:
            if isinstance(operation, PlannedWrite):
                try:
                    operation.path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise FileCommitError(operation.path, conflict=False) from exc
                commit_write(operation.path, operation.content, entry.before_sha256)
            elif isinstance(operation, PlannedDelete):
                assert entry.before_sha256 is not None
                commit_delete(operation.path, entry.before_sha256)
            elif isinstance(operation, PlannedMove):
                assert entry.before_sha256 is not None
                try:
                    operation.target.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise FileCommitError(operation.target, conflict=False) from exc
                commit_move(operation.path, operation.target, entry.before_sha256)
            else:
                try:
                    operation.path.mkdir(parents=True)
                except FileExistsError as exc:
                    raise FileCommitError(operation.path) from exc
        except FileCommitError as exc:
            raise FileJournalError(str(exc), code="write_conflict" if exc.conflict else "write_busy",
                                   untouched=exc.untouched) from exc

    @staticmethod
    def _matches_before(entry: FileChangeEntry) -> bool:
        path = Path(entry.path)
        if entry.operation in {"create", "mkdir"}:
            return not os.path.lexists(path)
        if entry.operation == "move":
            return (
                _file_sha256(path) == entry.before_sha256
                and not os.path.lexists(Path(entry.target_path or ""))
            )
        return _file_sha256(path) == entry.before_sha256

    @staticmethod
    def _matches_after(entry: FileChangeEntry) -> bool:
        path = Path(entry.path)
        if entry.operation == "delete":
            return not os.path.lexists(path)
        if entry.operation == "mkdir":
            return path.is_dir()
        if entry.operation == "move":
            return (
                not os.path.lexists(path)
                and _file_sha256(Path(entry.target_path or "")) == entry.after_sha256
            )
        return _file_sha256(path) == entry.after_sha256

    @staticmethod
    def _undo_problem(entry: FileChangeEntry, *, restore_missing: bool = False) -> str | None:
        if FileChangeJournal._matches_before(entry):
            return None
        path = Path(entry.path)
        if entry.operation in {"create", "update"}:
            current = _file_sha256(path)
            if current is None and entry.operation == "create" and not os.path.lexists(path):
                return None  # 已经没了：撤回就是让它不在
            interrupted_missing = (restore_missing and entry.operation == "update" and not os.path.lexists(path))
            if current != entry.after_sha256 and not interrupted_missing:
                return f"「{path}」后来又被改过，已保留现有内容，没有撤回。"
            if entry.operation == "update" and (entry.backup_path is None or not Path(entry.backup_path).is_file()):
                return f"「{path}」写之前的备份找不到了，没有撤回。"
            if entry.operation == "update" and _file_sha256(Path(entry.backup_path)) != entry.before_sha256:
                return f"「{path}」写之前的备份已变化，没有撤回。"
            return None
        if entry.operation == "delete":
            if os.path.lexists(path):
                return f"「{path}」原位置又有了文件，已保留现有内容，没有撤回。"
            if entry.backup_path is None or not Path(entry.backup_path).is_file():
                return f"「{path}」删之前的备份找不到了，没有撤回。"
            if _file_sha256(Path(entry.backup_path)) != entry.before_sha256:
                return f"「{path}」删之前的备份已变化，没有撤回。"
            return None
        if entry.operation == "move":
            target = Path(entry.target_path or "")
            if os.path.lexists(path):
                return f"「{path}」原位置又有了文件，没有挪回去。"
            if _file_sha256(target) != entry.after_sha256:
                return f"「{target}」后来又被改过或挪走了，没有挪回去。"
            return None
        if path.is_dir() and any(path.iterdir()):
            return f"「{path}」里已经有东西了，文件夹留着没删。"
        if os.path.lexists(path) and not path.is_dir():
            return f"「{path}」现在已不是原来的空文件夹，没有撤回。"
        return None

    @staticmethod
    def _reverse(entry: FileChangeEntry) -> None:
        path = Path(entry.path)
        if entry.operation == "create":
            if path.is_file():
                assert entry.after_sha256 is not None
                commit_delete(path, entry.after_sha256)
            _remove_empty_dirs(entry.created_dirs)
        elif entry.operation in {"update", "delete"}:
            assert entry.backup_path is not None
            before = Path(entry.backup_path).read_bytes()
            if _sha256(before) != entry.before_sha256:
                raise FileCommitError(Path(entry.backup_path))
            path.parent.mkdir(parents=True, exist_ok=True)
            commit_write(path, before,
                         entry.after_sha256 if entry.operation == "update" else None)
        elif entry.operation == "move":
            target = Path(entry.target_path or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            assert entry.after_sha256 is not None
            commit_move(target, path, entry.after_sha256)
            _remove_empty_dirs(entry.created_dirs)
        else:
            if path.is_dir():
                path.rmdir()
            _remove_empty_dirs(tuple(raw for raw in entry.created_dirs if Path(raw) != path))

    @staticmethod
    def _change_from_rows(rows: list[sqlite3.Row]) -> FileChange:
        first = rows[0]
        undone = max(str(row["undone_at"]) for row in rows) if all(row["undone_at"] for row in rows) else None
        return FileChange(
            change_id=str(first["change_id"]),
            session_id=str(first["session_id"]),
            run_id=str(first["run_id"]),
            tool_call_id=str(first["tool_call_id"]),
            title=str(first["title"]),
            created_at=datetime.fromisoformat(str(first["created_at"])),
            undone_at=None if undone is None else datetime.fromisoformat(str(undone)),
            apply_phase=first["apply_phase"],
            entries=tuple(
                FileChangeEntry(
                    operation=row["operation"],
                    path=str(row["path"]),
                    target_path=None if row["target_path"] is None else str(row["target_path"]),
                    before_sha256=row["before_sha256"],
                    after_sha256=row["after_sha256"],
                    backup_path=row["backup_path"],
                    created_dirs=tuple(json.loads(str(row["created_dirs_json"]))),
                    sequence=int(row["seq"]),
                    undone_at=datetime.fromisoformat(str(row["undone_at"])) if row["undone_at"] else None,
                )
                for row in rows
            ),
        )
