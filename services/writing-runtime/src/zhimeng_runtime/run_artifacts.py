"""Read-only, on-demand file observations for one run's durable receipts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.task_review_evidence import _receipts, artifact_targets, observe_artifact


class ArtifactFileState(BaseModel):
    state: Literal['file', 'directory', 'missing', 'unverified']
    bytes: int | None = None
    sha256: str | None = None
    reason: str | None = None


class RunArtifact(BaseModel):
    receipt_id: str
    source: Literal['file_change', 'change_set']
    record_state: str
    operation: str
    path: str
    expected_state: Literal['file', 'directory', 'missing', 'unknown']
    expected_sha256: str | None
    current: ArtifactFileState
    matches_recorded_state: bool | None


class RunArtifactsResponse(BaseModel):
    session_id: str
    run_id: str
    run_status: RunStatus
    revision: str
    observed_at: str
    total_paths: int
    observations: list[RunArtifact]
    next_offset: int | None
    scope: Literal['run_receipt_paths_only'] = 'run_receipt_paths_only'


class ArtifactRevisionChanged(ValueError):
    pass


def _snapshot(database: SqliteStore, session_id: str, run_id: str):
    with closing(sqlite3.connect(Path(database.path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        row = connection.execute(
            "SELECT r.status FROM run r JOIN execution_session s ON s.id=r.session_id "
            "WHERE r.id=? AND r.session_id=? AND s.status != 'closed'", (run_id, session_id),
        ).fetchone()
        if row is None:
            raise LookupError('这轮任务不存在或对话已经关闭。')
        records = _receipts(connection, [run_id])
        revision = hashlib.sha256(json.dumps([session_id, run_id, row['status'], records],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return row['status'], records, revision


def read_run_artifacts(database: SqliteStore, session_id: str, run_id: str, *, offset: int = 0,
                       limit: int = 20, revision: str | None = None,
                       cancelled: threading.Event | None = None) -> RunArtifactsResponse:
    if offset < 0 or not 1 <= limit <= 50:
        raise ValueError('文件记录分页参数无效。')
    status, records, current_revision = _snapshot(database, session_id, run_id)
    if (offset and not revision) or (revision is not None and revision != current_revision):
        raise ArtifactRevisionChanged('修改记录已变化，请重新读取。')
    targets = artifact_targets(records)
    observations = []
    for target in targets[offset:offset + limit]:
        if cancelled is not None and cancelled.is_set():
            break
        observations.append(observe_artifact(target, include_text=False, cancelled=cancelled))
    # No database transaction is held while reading disk. Do not combine a
    # pre-rollback receipt with a post-rollback file, including within page 1.
    if _snapshot(database, session_id, run_id)[2] != current_revision:
        raise ArtifactRevisionChanged('核对期间修改记录发生变化，请重新读取。')
    end = offset + len(observations)
    return RunArtifactsResponse(session_id=session_id, run_id=run_id, run_status=status,
        revision=current_revision, observed_at=datetime.now(UTC).isoformat(), total_paths=len(targets),
        observations=observations, next_offset=end if end < len(targets) else None)
