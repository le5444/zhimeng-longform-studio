from __future__ import annotations

import sqlite3
import threading
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.secret_mask import mask_secrets


class SessionSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(default="", max_length=500, description="Literal terms separated by spaces; all must match. Empty browses recent messages.")
    scope: Literal["project", "all"] = Field(default="project", description="project searches the current folder, or unbound chats when no folder is open. all searches all conversations.")
    session_id: str | None = Field(default=None, min_length=1, max_length=200, description="Search only this conversation; overrides scope. 'current' selects this conversation.")
    role: Literal["all", "user", "assistant"] = "all"
    limit: int = Field(default=10, ge=1, le=50)
    before_message_id: str | None = Field(default=None, min_length=1, max_length=200, description="Use next_before_message_id from the previous result to continue with the same query and scope.")


class SessionReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1, max_length=200, description="Conversation from session_search, or 'current'.")
    message_id: str | None = Field(default=None, min_length=1, max_length=200, description="Read this exact message in chunks. Without it, read a chronological message page.")
    after_ordinal: int = Field(default=0, ge=0, description="Read messages after this ordinal. Use next_after_ordinal to continue.")
    limit: int = Field(default=10, ge=1, le=30)
    start_offset: int = Field(default=0, ge=0, description="For a single message, continue at its next_start_offset. Offsets refer to masked text.")

    @model_validator(mode="after")
    def check_mode(self) -> SessionReadArguments:
        if self.message_id is not None and self.after_ordinal:
            raise ValueError("Use message_id or after_ordinal, not both")
        if self.message_id is None and self.start_offset:
            raise ValueError("start_offset requires message_id")
        return self


# Keep the small canonical-message table outermost; activity includes every stream delta.
_SELECT = """
    SELECT sm.session_id, s.title AS session_title, r.project_binding_id AS project_id,
           p.display_name AS project_name, sm.message_id, sm.ordinal, sm.role,
           sm.created_at, sm.run_id, r.status AS run_status, r.origin_kind,
           json_extract(a.payload_json, '$.payload.content') AS content
    FROM session_message sm
    CROSS JOIN activity a ON a.id = sm.activity_id
    JOIN execution_session s ON s.id = sm.session_id
    JOIN run r ON r.id = sm.run_id
    LEFT JOIN project_binding p ON p.id = r.project_binding_id
"""
_VISIBLE = "sm.role IN ('user', 'assistant') AND a.kind = 'message' AND s.status != 'closed'"


class SessionRecall:
    """Read canonical messages directly; no second copy to expire or resurrect."""

    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    @contextmanager
    def _snapshot(self, cancelled: threading.Event) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(Path(self.database.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
            connection.create_function("recall_fold", 1, lambda text: str(text or "").casefold(), deterministic=True)
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()
        finally:
            connection.close()

    @staticmethod
    def _session(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id, project_binding_id FROM execution_session WHERE id = ? AND status != 'closed'", (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Conversation no longer exists or was not found")
        return row

    @staticmethod
    def _message(row: sqlite3.Row, *, start: int = 0, size: int = 1000, terms: list[str] | None = None) -> dict:
        if not isinstance(row["content"], str):
            raise ValueError("Stored message content is unavailable")
        content, masked = mask_secrets(row["content"])
        if terms:
            # Find an original-text line, so Unicode casefold expansion cannot shift offsets.
            offset = 0
            for line in content.splitlines(keepends=True):
                if any(term in line.casefold() for term in terms):
                    start = offset
                    # Long unbroken lines need a window around the match as well.
                    for index in range(0, len(line), size // 2):
                        if any(term in line[index:index + size].casefold() for term in terms):
                            start += index
                            break
                    break
                offset += len(line)
        if start > len(content):
            raise ValueError("start_offset is beyond this message; read it again from offset 0")
        end = min(start + size, len(content))
        result = {key: row[key] for key in row.keys() if key != "content"}
        result["session_title"] = mask_secrets(result["session_title"])[0]
        if result["project_name"] is not None:
            result["project_name"] = mask_secrets(result["project_name"])[0]
        return {
            **result, "content": content[start:end], "start_offset": start, "end_offset": end,
            "total_characters": len(content), "next_start_offset": end if end < len(content) else None,
            "masked_secrets": masked, "offset_basis": "masked_text",
        }

    def search(self, current_session_id: str, current_run_id: str, args: SessionSearchArguments, cancelled: threading.Event) -> dict:
        with self._snapshot(cancelled) as connection:
            current = self._session(connection, current_session_id)
            session_id = current_session_id if args.session_id == "current" else args.session_id
            if session_id is not None:
                self._session(connection, session_id)
            return self._search(connection, args, current_run_id=current_run_id, session_id=session_id,
                                project_id=current["project_binding_id"] if args.scope == "project" and session_id is None else None)

    def search_messages(self, args: SessionSearchArguments, cancelled: threading.Event, *, project_id: str | None = None) -> dict:
        """Human history search needs neither an active session nor a model run."""
        if not args.query.strip():
            raise ValueError("请输入要搜索的文字。")
        with self._snapshot(cancelled) as connection:
            return self._search(connection, args, project_id=project_id if args.scope == "project" else None)

    def _search(self, connection: sqlite3.Connection, args: SessionSearchArguments, *,
                current_run_id: str | None = None, session_id: str | None = None, project_id: str | None = None) -> dict:
        terms = list(dict.fromkeys(args.query.casefold().split()))
        conditions = [_VISIBLE]
        values: list = []
        if current_run_id is not None:
            conditions.append("sm.run_id != ?")
            values.append(current_run_id)
        scope = "session" if session_id is not None else args.scope
        if session_id is not None:
            conditions.append("sm.session_id = ?")
            values.append(session_id)
        elif scope == "project":
            conditions.append("r.project_binding_id IS ?")
            values.append(project_id)
        if args.role != "all":
            conditions.append("sm.role = ?")
            values.append(args.role)
        if args.before_message_id is not None:
            cursor = connection.execute(
                "SELECT sm.created_at, sm.message_id FROM session_message sm JOIN execution_session s ON s.id = sm.session_id WHERE sm.message_id = ? AND s.status != 'closed'",
                (args.before_message_id,),
            ).fetchone()
            if cursor is None:
                raise SearchCursorError("Search cursor no longer exists; restart the search")
            conditions.append("(sm.created_at, sm.message_id) < (?, ?)")
            values.extend(cursor)
        for term in terms:
            conditions.append("instr(recall_fold(json_extract(a.payload_json, '$.payload.content')), ?) > 0")
            values.append(term)
        rows = connection.execute(
            _SELECT + " WHERE " + " AND ".join(conditions)
            + " ORDER BY sm.created_at DESC, sm.message_id DESC LIMIT ?",
            (*values, args.limit + 1),
        ).fetchall()
        selected = rows[:args.limit]
        return {
            "schema": "zhimeng.tool-result.session-search.v1", "query": args.query,
            "scope": scope, "project_id": project_id, "session_id": session_id,
            "search_method": "literal_terms", "order": "newest_first",
            "matches": [self._message(row, terms=terms) for row in selected],
            "has_more": len(rows) > args.limit,
            "next_before_message_id": selected[-1]["message_id"] if len(rows) > args.limit else None,
        }

    def read(self, current_session_id: str, current_run_id: str, args: SessionReadArguments, cancelled: threading.Event) -> dict:
        session_id = current_session_id if args.session_id == "current" else args.session_id
        with self._snapshot(cancelled) as connection:
            self._session(connection, session_id)
            conditions = [_VISIBLE, "sm.session_id = ?", "sm.run_id != ?"]
            values: list = [session_id, current_run_id]
            if args.message_id is not None:
                conditions.append("sm.message_id = ?")
                values.append(args.message_id)
            else:
                conditions.append("sm.ordinal > ?")
                values.append(args.after_ordinal)
            rows = connection.execute(
                _SELECT + " WHERE " + " AND ".join(conditions) + " ORDER BY sm.ordinal LIMIT ?",
                (*values, 1 if args.message_id is not None else args.limit + 1),
            ).fetchall()
            if args.message_id is not None and not rows:
                raise ValueError("Message was not found in this conversation's earlier history")
            selected = rows[:args.limit]
            return {
                "schema": "zhimeng.tool-result.session-read.v1", "session_id": session_id,
                "messages": [self._message(row, start=args.start_offset, size=12000 if args.message_id else 1000) for row in selected],
                "has_more": len(rows) > args.limit,
                "next_after_ordinal": selected[-1]["ordinal"] if len(rows) > args.limit else None,
            }

    def source(self, session_id: str, message_id: str, cancelled: threading.Event, *, start_offset: int = 0, expected_hash: str | None = None) -> dict:
        with self._snapshot(cancelled) as connection:
            row = connection.execute(
                _SELECT + " WHERE " + _VISIBLE + " AND sm.session_id=? AND sm.message_id=?",
                (session_id, message_id),
            ).fetchone()
            if row is None:
                raise LookupError("原消息已删除或不属于这条对话。")
            if not isinstance(row["content"], str):
                raise ValueError("Stored message content is unavailable")
            digest = hashlib.sha256(mask_secrets(row["content"])[0].encode("utf-8")).hexdigest()
            if expected_hash is not None and digest != expected_hash:
                raise SourceChangedError("原消息已更新，请重新读取。")
            message = self._message(row, start=start_offset, size=12000)
            neighbors = []
            for operator, order in (("<", "DESC"), (">", "ASC")):
                neighbor = connection.execute(
                    _SELECT + " WHERE " + _VISIBLE + f" AND sm.session_id=? AND sm.ordinal {operator} ? ORDER BY sm.ordinal {order} LIMIT 1",
                    (session_id, row["ordinal"]),
                ).fetchone()
                neighbors.append(neighbor["message_id"] if neighbor is not None else None)
            return {"message": message, "content_hash": digest, "previous_message_id": neighbors[0], "next_message_id": neighbors[1]}


class SourceChangedError(ValueError):
    pass


class SearchCursorError(ValueError):
    pass
