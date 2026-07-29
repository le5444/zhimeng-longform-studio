"""SQLite state for the novel runtime. Project files remain the source of truth."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


SCHEMA_VERSION = 2
PREVIEW_LEASE_SLOT = 1


class PreviewLeaseBusyError(RuntimeError):
    """Raised when a preview lease or its captured binding cannot be honored."""

    def __init__(
        self,
        message: str,
        *,
        holder: Dict[str, Any] | None = None,
        code: str = "WRITING_PREVIEW_LEASE_BUSY",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.holder = dict(holder or {})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def stable_project_id(root: Path) -> str:
    normalized = str(root.resolve()).replace("\\", "/").casefold()
    return f"novel-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]}"


class RuntimeStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_bindings (
                    project_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL UNIQUE,
                    kb_root TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL,
                    project_fingerprint TEXT NOT NULL,
                    adapters_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project_bindings(project_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_project_sequence
                    ON runtime_events(project_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_run_sequence
                    ON runtime_events(run_id, sequence DESC);
                CREATE TABLE IF NOT EXISTS preview_leases (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1),
                    project_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    binding_revision INTEGER NOT NULL,
                    project_fingerprint TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    expires_at_epoch REAL NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES project_bindings(project_id)
                );
                """
            )
            binding_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(project_bindings)").fetchall()
            }
            if "kb_root" not in binding_columns:
                connection.execute("ALTER TABLE project_bindings ADD COLUMN kb_root TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "INSERT OR REPLACE INTO runtime_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _binding_row(row: sqlite3.Row | None) -> Dict[str, Any] | None:
        if row is None:
            return None
        try:
            adapters = json.loads(row["adapters_json"])
        except json.JSONDecodeError:
            adapters = []
        return {
            "project_id": row["project_id"],
            "root_path": row["root_path"],
            "kb_root": row["kb_root"],
            "display_name": row["display_name"],
            "project_fingerprint": row["project_fingerprint"],
            "adapters": adapters if isinstance(adapters, list) else [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": int(row["revision"]),
        }

    @staticmethod
    def _lease_row(row: sqlite3.Row | None) -> Dict[str, Any] | None:
        if row is None:
            return None
        return {
            "project_id": row["project_id"],
            "run_id": row["run_id"],
            "lease_id": row["lease_id"],
            "binding_revision": int(row["binding_revision"]),
            "project_fingerprint": row["project_fingerprint"],
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "expires_at_epoch": float(row["expires_at_epoch"]),
            "ttl_seconds": int(row["ttl_seconds"]),
        }

    def _active_preview_lease(
        self,
        connection: sqlite3.Connection,
        *,
        now_epoch: float | None = None,
    ) -> Dict[str, Any] | None:
        current_epoch = time.time() if now_epoch is None else now_epoch
        row = connection.execute(
            "SELECT * FROM preview_leases WHERE slot = ?",
            (PREVIEW_LEASE_SLOT,),
        ).fetchone()
        lease = self._lease_row(row)
        if lease is None:
            return None
        if float(lease["expires_at_epoch"]) <= current_epoch:
            connection.execute("DELETE FROM preview_leases WHERE slot = ?", (PREVIEW_LEASE_SLOT,))
            return None
        return lease

    def _reject_binding_change_if_leased(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        holder = self._active_preview_lease(connection)
        if holder is None or holder["project_id"] != project_id:
            return
        raise PreviewLeaseBusyError(
            f"Writing project binding is frozen by preview run {holder['run_id']}",
            holder=holder,
            code="WRITING_PREVIEW_BINDING_LEASED",
        )

    def bind_project(
        self,
        root: Path,
        *,
        display_name: str,
        project_fingerprint: str,
        kb_root: Path | None = None,
        adapters: List[str] | None = None,
    ) -> Dict[str, Any]:
        resolved = root.resolve()
        project_id = stable_project_id(resolved)
        created_at = now_iso()
        adapters_json = json.dumps(sorted(set(adapters or [])), ensure_ascii=False, separators=(",", ":"))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO project_bindings(
                        project_id, root_path, kb_root, display_name, project_fingerprint,
                        adapters_json, created_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        project_id,
                        str(resolved),
                        str(kb_root.resolve()) if kb_root else "",
                        display_name,
                        project_fingerprint,
                        adapters_json,
                        created_at,
                        created_at,
                    ),
                )
            else:
                changed = any((
                    existing["root_path"] != str(resolved),
                    existing["kb_root"] != (str(kb_root.resolve()) if kb_root else ""),
                    existing["display_name"] != display_name,
                    existing["project_fingerprint"] != project_fingerprint,
                    existing["adapters_json"] != adapters_json,
                ))
                if changed:
                    self._reject_binding_change_if_leased(connection, project_id)
                connection.execute(
                    """
                    UPDATE project_bindings
                    SET root_path = ?, kb_root = ?, display_name = ?, project_fingerprint = ?,
                        adapters_json = ?, updated_at = ?,
                        revision = revision + ?
                    WHERE project_id = ?
                    """,
                    (
                        str(resolved),
                        str(kb_root.resolve()) if kb_root else "",
                        display_name,
                        project_fingerprint,
                        adapters_json,
                        created_at,
                        1 if changed else 0,
                        project_id,
                    ),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            binding = self._binding_row(row)
            if binding is None:
                raise RuntimeError(f"binding disappeared after commit: {project_id}")
            return binding
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_binding(self, project_id: str) -> Dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return self._binding_row(row)
        finally:
            connection.close()

    def refresh_project_fingerprint(self, project_id: str, project_fingerprint: str) -> Dict[str, Any]:
        refreshed_at = now_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"writing project binding not found: {project_id}")
            if existing["project_fingerprint"] != project_fingerprint:
                self._reject_binding_change_if_leased(connection, project_id)
            connection.execute(
                """
                UPDATE project_bindings
                SET project_fingerprint = ?, updated_at = ?, revision = revision + 1
                WHERE project_id = ? AND project_fingerprint != ?
                """,
                (project_fingerprint, refreshed_at, project_id, project_fingerprint),
            )
            row = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"writing project binding not found: {project_id}")
            connection.commit()
            binding = self._binding_row(row)
            if binding is None:
                raise RuntimeError(f"binding disappeared during fingerprint refresh: {project_id}")
            return binding
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acquire_preview_lease(
        self,
        project_id: str,
        run_id: str,
        ttl_seconds: int = 1800,
    ) -> Dict[str, Any]:
        if not str(project_id).strip() or not str(run_id).strip():
            raise ValueError("project_id and run_id are required for a preview lease")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

        acquired_epoch = time.time()
        expires_epoch = acquired_epoch + ttl_seconds
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            binding_row = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            binding = self._binding_row(binding_row)
            if binding is None:
                raise KeyError(f"writing project binding not found: {project_id}")

            holder = self._active_preview_lease(connection, now_epoch=acquired_epoch)
            if holder is not None and (holder["project_id"], holder["run_id"]) != (project_id, run_id):
                raise PreviewLeaseBusyError(
                    f"Writing preview runtime is held by run {holder['run_id']}",
                    holder=holder,
                )

            renewed = holder is not None
            if holder is not None:
                if (
                    int(holder["binding_revision"]) != int(binding["revision"])
                    or str(holder["project_fingerprint"]) != str(binding["project_fingerprint"])
                ):
                    raise PreviewLeaseBusyError(
                        "Preview lease binding changed before renewal",
                        holder=holder,
                        code="WRITING_PREVIEW_BINDING_CHANGED",
                    )
                connection.execute(
                    """
                    UPDATE preview_leases
                    SET expires_at = ?, expires_at_epoch = ?, ttl_seconds = ?
                    WHERE slot = ?
                    """,
                    (_epoch_iso(expires_epoch), expires_epoch, ttl_seconds, PREVIEW_LEASE_SLOT),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO preview_leases(
                        slot, project_id, run_id, lease_id, binding_revision,
                        project_fingerprint, acquired_at, expires_at, expires_at_epoch, ttl_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        PREVIEW_LEASE_SLOT,
                        project_id,
                        run_id,
                        uuid.uuid4().hex,
                        int(binding["revision"]),
                        str(binding["project_fingerprint"]),
                        _epoch_iso(acquired_epoch),
                        _epoch_iso(expires_epoch),
                        expires_epoch,
                        ttl_seconds,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM preview_leases WHERE slot = ?",
                (PREVIEW_LEASE_SLOT,),
            ).fetchone()
            lease = self._lease_row(row)
            if lease is None:
                raise RuntimeError("preview lease disappeared during acquisition")
            connection.commit()
            return {"acquired": True, "renewed": renewed, **lease}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_preview_lease(self, project_id: str, run_id: str) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM preview_leases WHERE slot = ? AND project_id = ? AND run_id = ?",
                (PREVIEW_LEASE_SLOT, project_id, run_id),
            )
            connection.commit()
            return int(cursor.rowcount) == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def assert_preview_publish(
        self,
        project_id: str,
        run_id: str,
        expected_revision: int,
        expected_fingerprint: str,
    ) -> Dict[str, Any]:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("expected_revision must be an integer")
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
            raise ValueError("expected_fingerprint must be a non-empty string")

        now_epoch = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            raw_lease = connection.execute(
                "SELECT * FROM preview_leases WHERE slot = ?",
                (PREVIEW_LEASE_SLOT,),
            ).fetchone()
            holder = self._lease_row(raw_lease)
            if holder is None:
                raise PreviewLeaseBusyError(
                    "No active preview lease exists for publication",
                    code="WRITING_PREVIEW_LEASE_MISSING",
                )
            if float(holder["expires_at_epoch"]) <= now_epoch:
                connection.execute("DELETE FROM preview_leases WHERE slot = ?", (PREVIEW_LEASE_SLOT,))
                connection.commit()
                raise PreviewLeaseBusyError(
                    "Preview lease expired before publication",
                    holder=holder,
                    code="WRITING_PREVIEW_LEASE_EXPIRED",
                )
            if (holder["project_id"], holder["run_id"]) != (project_id, run_id):
                raise PreviewLeaseBusyError(
                    f"Writing preview runtime is held by run {holder['run_id']}",
                    holder=holder,
                )

            binding_row = connection.execute(
                "SELECT * FROM project_bindings WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            binding = self._binding_row(binding_row)
            if binding is None:
                raise PreviewLeaseBusyError(
                    "Writing project binding disappeared before publication",
                    holder=holder,
                    code="WRITING_PREVIEW_BINDING_CHANGED",
                )
            binding_matches = (
                int(binding["revision"]) == expected_revision
                and str(binding["project_fingerprint"]) == expected_fingerprint
                and int(holder["binding_revision"]) == expected_revision
                and str(holder["project_fingerprint"]) == expected_fingerprint
            )
            if not binding_matches:
                raise PreviewLeaseBusyError(
                    "Writing project binding changed before preview publication",
                    holder=holder,
                    code="WRITING_PREVIEW_BINDING_CHANGED",
                )
            renewed_expires_epoch = now_epoch + int(holder["ttl_seconds"])
            connection.execute(
                """
                UPDATE preview_leases
                SET expires_at = ?, expires_at_epoch = ?
                WHERE slot = ? AND project_id = ? AND run_id = ?
                """,
                (
                    _epoch_iso(renewed_expires_epoch),
                    renewed_expires_epoch,
                    PREVIEW_LEASE_SLOT,
                    project_id,
                    run_id,
                ),
            )
            holder["expires_at"] = _epoch_iso(renewed_expires_epoch)
            holder["expires_at_epoch"] = renewed_expires_epoch
            connection.commit()
            return {"binding": binding, "lease": holder}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_bindings(self) -> List[Dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM project_bindings ORDER BY updated_at DESC",
            ).fetchall()
            return [binding for row in rows if (binding := self._binding_row(row)) is not None]
        finally:
            connection.close()

    def append_event(
        self,
        project_id: str,
        event_type: str,
        status: str,
        payload: Dict[str, Any],
        *,
        run_id: str = "",
    ) -> Dict[str, Any]:
        created_at = now_iso()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                INSERT INTO runtime_events(project_id, run_id, event_type, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, run_id, event_type, status, payload_json, created_at),
            )
            connection.commit()
            return {
                "sequence": int(cursor.lastrowid),
                "project_id": project_id,
                "run_id": run_id,
                "type": event_type,
                "status": status,
                "payload": payload,
                "created_at": created_at,
            }
        finally:
            connection.close()

    def recent_events(self, project_id: str, *, after_sequence: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE project_id = ? AND sequence > ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (project_id, max(0, int(after_sequence)), safe_limit),
            ).fetchall()
        finally:
            connection.close()
        events: List[Dict[str, Any]] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {"unreadable": True}
            events.append({
                "sequence": int(row["sequence"]),
                "project_id": row["project_id"],
                "run_id": row["run_id"],
                "type": row["event_type"],
                "status": row["status"],
                "payload": payload,
                "created_at": row["created_at"],
            })
        return events
