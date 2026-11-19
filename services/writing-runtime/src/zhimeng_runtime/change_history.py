"""One read model for project and journal changes; writes retain their owners."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.file_changes_api import _detail, _summary
from zhimeng_runtime.file_journal import FileChangeJournal
from zhimeng_runtime.secret_mask import mask_secrets
from zhimeng_runtime.task_review_evidence import observe_file


class HistoryFile(BaseModel):
    path: str
    target_path: str | None = None
    operation: str


class HistoryChange(BaseModel):
    id: str
    source: Literal['change_set', 'file_change']
    session_id: str | None
    project_id: str | None
    run_id: str | None
    title: str
    status: str
    created_at: str
    files: list[HistoryFile]


class ChangeHistoryPage(BaseModel):
    session_id: str
    scope: Literal['session', 'project']
    project_id: str | None
    revision: str
    total: int
    changes: list[HistoryChange]
    next_offset: int | None


class HistoryHunk(BaseModel):
    before: str
    after: str


class HistoryPreview(HistoryFile):
    current_state: Literal['file', 'directory', 'missing', 'unverified']
    open_path: str | None = None
    hunks: list[HistoryHunk] = Field(default_factory=list)
    note: str | None = None
    before_characters: int | None = None
    after_characters: int | None = None


class ChangeHistoryDetail(HistoryChange):
    previews: list[HistoryPreview]
    undo_available: bool
    undo_reason: str | None
    review_revision: int | None = None


class HistoryChanged(ValueError):
    pass


class ChangeHistoryReader:
    def __init__(self, sessions: AgentStore, journal: FileChangeJournal, projects: ChangeSetService | None):
        self.sessions, self.journal, self.projects = sessions, journal, projects

    def _rows(self, connection, session_id: str, scope: str, change_id: str | None = None):
        session = connection.execute("SELECT project_binding_id FROM execution_session WHERE id=? AND status!='closed'", (session_id,)).fetchone()
        if session is None:
            raise LookupError('没有找到这场对话。')
        project_id = session['project_binding_id']
        if scope == 'project' and not project_id:
            raise ValueError('这场对话尚未关联项目。')
        project_filter = 'c.project_binding_id=?' if scope == 'project' else 'r.session_id=?'
        journal_filter = 'r.project_binding_id=?' if scope == 'project' else 'f.session_id=?'
        key = project_id if scope == 'project' else session_id
        if change_id is not None:
            project_filter += ' AND c.id=?'
            journal_filter += ' AND f.change_id=?'
        parameters = (key, change_id, key, change_id) if change_id is not None else (key, key)
        rows = connection.execute(
            "SELECT c.id,'change_set' AS source,c.created_at,c.updated_at AS changed_at,c.status "
            "FROM change_set c LEFT JOIN run r ON r.id=json_extract(c.payload_json,'$.source_run_id') "
            f"WHERE {project_filter} UNION ALL "
            "SELECT f.change_id,'file_change',MIN(f.created_at),MAX(COALESCE(f.undone_at,f.created_at)),"
            "GROUP_CONCAT(f.apply_phase || ':' || COALESCE(f.undone_at,'')) "
            "FROM file_change f LEFT JOIN run r ON r.id=f.run_id "
            f"WHERE {journal_filter} GROUP BY f.change_id", parameters,
        ).fetchall()
        # Compare actual timestamps; old records can use either Z or offsets.
        rows = sorted(rows, key=lambda row: (datetime.fromisoformat(row['created_at']), row['id']), reverse=True)
        return project_id, rows

    def _summary(self, connection, row) -> HistoryChange:
        if row['source'] == 'file_change':
            entries = connection.execute('SELECT * FROM file_change WHERE change_id=? ORDER BY seq', (row['id'],)).fetchall()
            if not entries:
                raise HistoryChanged('修改记录已变化，请刷新。')
            change = self.journal._change_from_rows(entries)
            summary = _summary(change)
            run = connection.execute('SELECT project_binding_id FROM run WHERE id=?', (change.run_id,)).fetchone()
            return HistoryChange(id=change.change_id, source='file_change', session_id=change.session_id,
                project_id=run['project_binding_id'] if run else None, run_id=change.run_id or None,
                title=summary.title, status=summary.status, created_at=change.created_at.isoformat(),
                files=[HistoryFile(path=f.path, target_path=f.target_path, operation=f.operation) for f in summary.files])
        saved = connection.execute(
            "SELECT c.payload_json,p.root_path,r.session_id FROM change_set c "
            "JOIN project_binding p ON p.id=c.project_binding_id "
            "LEFT JOIN run r ON r.id=json_extract(c.payload_json,'$.source_run_id') WHERE c.id=?", (row['id'],),
        ).fetchone()
        if saved is None:
            raise HistoryChanged('修改记录已变化，请刷新。')
        data = json.loads(saved['payload_json'])
        root = Path(saved['root_path'])
        for file in data['changes']:
            for raw in (file['relative_path'], file.get('target_relative_path')):
                if raw and (Path(raw).is_absolute() or Path(raw).drive or '..' in Path(raw).parts):
                    raise ValueError('修改记录中的相对路径无效。')
        return HistoryChange(id=row['id'], source='change_set', session_id=saved['session_id'],
            project_id=data['project_id'], run_id=data.get('source_run_id'), title=mask_secrets(data['title'])[0],
            status=data['status'], created_at=data['created_at'], files=[HistoryFile(
                path=str(root / f['relative_path']),
                target_path=str(root / f['target_relative_path']) if f.get('target_relative_path') else None,
                operation=f['operation']) for f in data['changes']])

    def list(self, session_id: str, *, scope: str = 'session', offset: int = 0, limit: int = 40,
             revision: str | None = None) -> ChangeHistoryPage:
        with self.sessions.database.connection() as connection:
            connection.execute('BEGIN')
            project_id, rows = self._rows(connection, session_id, scope)
            current = hashlib.sha256(json.dumps([session_id, scope, project_id, [tuple(row) for row in rows]], ensure_ascii=False).encode()).hexdigest()
            if offset and not revision or revision is not None and revision != current:
                raise HistoryChanged('修改记录已变化，请刷新后继续。')
            changes = [self._summary(connection, row) for row in rows[offset:offset + limit]]
            end = offset + len(changes)
            return ChangeHistoryPage(session_id=session_id, scope=scope, project_id=project_id, revision=current,
                total=len(rows), changes=changes, next_offset=end if end < len(rows) else None)

    def detail(self, session_id: str, change_id: str, *, scope: str = 'session') -> ChangeHistoryDetail:
        with self.sessions.database.connection() as connection:
            _, rows = self._rows(connection, session_id, scope, change_id)
            row = next((row for row in rows if row['id'] == change_id), None)
            if row is None:
                raise LookupError('当前范围内没有这条修改记录。')
            summary = self._summary(connection, row)
        if summary.source == 'file_change':
            record = self.journal.get(change_id)
            if record is None:
                raise HistoryChanged('修改记录已变化，请刷新。')
            detail = _detail(self.journal, record)
            previews = []
            for entry, file in zip(record.entries, detail.previews, strict=True):
                raw_path = entry.target_path if entry.operation == 'move' and not entry.undone_at else entry.path
                state = observe_file(Path(raw_path), include_text=False)['state']
                previews.append(HistoryPreview(**HistoryFile(path=file.path, target_path=file.target_path, operation=file.operation).model_dump(),
                    current_state=state, open_path=raw_path if state == 'file' else None, note=file.preview_note,
                    before_characters=file.before_characters, after_characters=file.after_characters,
                    hunks=[HistoryHunk(before=file.before_text, after=file.after_text)] if file.before_text is not None and file.after_text is not None else []))
            return ChangeHistoryDetail(**summary.model_dump(), previews=previews,
                undo_available=detail.undo_available, undo_reason=detail.undo_reason)
        if self.projects is None:
            raise RuntimeError('项目修改服务暂不可用。')
        record = self.projects.get(change_id)
        summary = summary.model_copy(update={'status': record.status.value, 'title': mask_secrets(record.title)[0]})
        previews, reason = [], None
        manifest = record.writeback
        for file, changed in zip(summary.files, record.changes, strict=True):
            path = file.target_path if changed.operation == 'move' and record.status == 'applied' else file.path
            current = observe_file(Path(path), include_text=False)
            saved = next((f for f in manifest.files if f.change_id == changed.change_id), None) if manifest else None
            if record.status == 'applied':
                expected = 'missing' if changed.operation == 'delete' else 'file'
                if current['state'] != expected or (expected == 'file' and (not saved or current.get('sha256') != saved.after_sha256)):
                    reason = '文件在保存后发生变化或暂时无法读取，请刷新核对后再撤回。'
                if changed.operation == 'move' and observe_file(Path(file.path), include_text=False)['state'] != 'missing':
                    reason = '原位置已有文件，暂时无法撤回移动。'
            previews.append(HistoryPreview(**file.model_dump(), current_state=current['state'],
                open_path=path if current['state'] == 'file' else None,
                hunks=[HistoryHunk(before=mask_secrets(h.before_text)[0], after=mask_secrets(h.after_text)[0]) for h in changed.hunks
                       if record.status not in ('applied', 'rolled_back') or saved and h.hunk_id in saved.accepted_hunk_ids],
                note='显示这次修改保存的差异。' if record.status == 'rolled_back' else None))
        return ChangeHistoryDetail(**summary.model_dump(), previews=previews, review_revision=record.review_revision,
            undo_available=record.status == 'applied' and reason is None, undo_reason=reason)
