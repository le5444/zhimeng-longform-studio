"""Durable command facts, delivered atomically with an Agent transcript or run."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import sqlite3
from typing import Any, Iterator

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.secret_mask import mask_secrets


class CommandCompletionStore:
    def __init__(self, database: SqliteStore):
        self.database = database

    @contextmanager
    def _connection(self, connection: sqlite3.Connection | None, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        with self.database.connection() as opened:
            if write:
                opened.execute("BEGIN IMMEDIATE")
            try:
                yield opened
                if write:
                    opened.commit()
            except BaseException:
                if write:
                    opened.rollback()
                raise

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def current_generation(self, session_id: str, *, connection: sqlite3.Connection | None = None) -> int:
        with self._connection(connection) as opened:
            row = opened.execute("SELECT stop_generation FROM command_completion_scope WHERE session_id = ?", (session_id,)).fetchone()
            return int(row[0]) if row else 0

    def run_generation(self, session_id: str, run_id: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute("SELECT stop_generation FROM run WHERE id = ? AND session_id = ?", (run_id, session_id)).fetchone()
            if row is None:
                raise ValueError("命令所属任务不存在。")
            return int(row[0])

    def record(self, payload: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        # Historical commands have no delivery contract. Installing this feature
        # does not wake every conversation that ever ran a command.
        if payload.get("completion_version") != 1 or payload.get("status") not in {"exited", "cancelled", "interrupted"}:
            return None
        command_id = str(payload.get("session_id") or payload.get("command_session_id") or "")
        session_id = str(payload.get("owner_session_id") or "")
        run_id = str(payload.get("owner_run_id") or "")
        if not command_id or not session_id or not run_id:
            raise ValueError("命令完成回执缺少所属对话或任务。")
        # The callback carries metadata and log references. Tool reads may carry
        # text as well; retain one small fact row, with the full logs on disk.
        saved = {key: value for key, value in payload.items() if key not in {"stdout", "stderr"}}
        saved["command"] = mask_secrets(str(saved.get("command", "")))[0]
        with self._connection(connection, write=True) as opened:
            existing = opened.execute("SELECT * FROM command_completion WHERE command_session_id = ?", (command_id,)).fetchone()
            if existing:
                if existing["owner_session_id"] != session_id or existing["owner_run_id"] != run_id:
                    raise ValueError("命令完成回执的对话归属不一致。")
                return self._event(existing)
            owner = opened.execute("SELECT status, stop_generation FROM run WHERE id = ? AND session_id = ?", (run_id, session_id)).fetchone()
            if owner is None:
                # Removed conversations leave old command logs behind; never
                # recreate their runs, or let one bad old record block startup.
                return None
            generation = int(payload.get("stop_generation", owner["stop_generation"]))
            eligible = (payload.get("completion_eligible") is True and payload["status"] == "exited"
                        and owner["status"] not in {"cancelling", "cancelled", "interrupted"}
                        and generation == int(owner["stop_generation"])
                        and generation == self.current_generation(session_id, connection=opened))
            opened.execute("""INSERT INTO command_completion(event_id, command_session_id, owner_session_id,
                owner_run_id,status,exit_code,ended_at,payload_json,delivery_state,stop_generation,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                "command-completion:" + command_id, command_id, session_id, run_id, payload["status"],
                payload.get("exit_code"), payload.get("ended_at"), json.dumps(saved, ensure_ascii=False),
                "pending" if eligible else "suppressed", generation, datetime.now(UTC).isoformat(),
            ))
            return self._event(opened.execute("SELECT * FROM command_completion WHERE command_session_id = ?", (command_id,)).fetchone())

    def pending(self, session_id: str, *, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._connection(connection) as opened:
            rows = opened.execute("""SELECT c.* FROM command_completion c JOIN run r ON r.id=c.owner_run_id
                WHERE c.owner_session_id=? AND c.delivery_state='pending'
                AND c.stop_generation=COALESCE((SELECT stop_generation FROM command_completion_scope WHERE session_id=?),0)
                AND r.status NOT IN ('cancelling','cancelled','interrupted') ORDER BY c.sequence""", (session_id, session_id)).fetchall()
            return [self._event(row) for row in rows]

    def pending_session_ids(self) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute("""SELECT DISTINCT c.owner_session_id FROM command_completion c JOIN run r ON r.id=c.owner_run_id
                WHERE c.delivery_state='pending' AND r.status NOT IN ('cancelling','cancelled','interrupted')
                AND json_extract(c.payload_json,'$.automatic_delivery_error') IS NULL
                AND c.stop_generation=COALESCE((SELECT stop_generation FROM command_completion_scope WHERE session_id=c.owner_session_id),0)
                ORDER BY c.owner_session_id""").fetchall()
            return [str(row[0]) for row in rows]

    def list_events(self, session_id: str, *, after: int = 0, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._connection(connection) as opened:
            return [self._event(row) for row in opened.execute(
                "SELECT * FROM command_completion WHERE owner_session_id=? AND sequence>? ORDER BY sequence", (session_id, after)).fetchall()]

    def mark_delivered(self, event_ids: list[str], run_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        with self._connection(connection, write=True) as opened:
            run = opened.execute("SELECT session_id FROM run WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("接收命令结果的任务不存在。")
            for event_id in event_ids:
                event = opened.execute("SELECT owner_session_id FROM command_completion WHERE event_id=?", (event_id,)).fetchone()
                if event is None or event[0] != run[0]:
                    raise ValueError("命令结果只能交给所属对话。")
                opened.execute("UPDATE command_completion SET delivery_state='delivered',delivered_run_id=?,payload_json=json_remove(payload_json,'$.automatic_delivery_error') WHERE event_id=? AND delivery_state='pending'", (run_id, event_id))

    def defer_automatic(self, event_ids: list[str], reason: str, *, connection: sqlite3.Connection | None = None) -> None:
        """Keep facts for the next user turn after automatic admission failed."""
        safe_reason = mask_secrets(reason)[0]
        with self._connection(connection, write=True) as opened:
            for event_id in event_ids:
                opened.execute("""UPDATE command_completion
                    SET payload_json=json_set(payload_json,'$.automatic_delivery_error',?)
                    WHERE event_id=? AND delivery_state='pending'""", (safe_reason,event_id))

    def mark_observed(self, payload: dict[str, Any], run_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        with self._connection(connection, write=True) as opened:
            event = self.record(payload, connection=opened)
            if event:
                self.mark_delivered([event["event_id"]], run_id, connection=opened)

    def suppress_run(self, owner_run_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        with self._connection(connection, write=True) as opened:
            opened.execute("UPDATE command_completion SET delivery_state='suppressed' WHERE owner_run_id=? AND delivery_state='pending'", (owner_run_id,))

    def bump_stop_generation(self, session_id: str, *, connection: sqlite3.Connection | None = None) -> int:
        with self._connection(connection, write=True) as opened:
            opened.execute("""INSERT INTO command_completion_scope(session_id,stop_generation) VALUES(?,1)
                ON CONFLICT(session_id) DO UPDATE SET stop_generation=stop_generation+1""", (session_id,))
            opened.execute("UPDATE command_completion SET delivery_state='suppressed' WHERE owner_session_id=? AND delivery_state='pending'", (session_id,))
            return self.current_generation(session_id, connection=opened)
