"""Explicit recovery against a checked current version; never resume commands."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zhimeng_runtime.command_files import CommandFiles
from zhimeng_runtime.file_journal import FileJournalError, PlannedDelete, PlannedWrite, _file_sha256, _write_atomic
from zhimeng_runtime.file_preview import verified_text
from zhimeng_runtime.secret_mask import mask_secrets


class RecoveryEntry(BaseModel):
    path: str
    revision: str
    action: Literal['restore', 'remove']
    status: Literal['available', 'unchanged', 'conflict', 'unavailable']
    before_sha256: str | None
    current_sha256: str | None
    before_text: str | None = None
    current_text: str | None = None
    note: str | None = None


class RecoveryReceipt(BaseModel):
    request_id: str
    path: str
    change_id: str
    status: Literal['applied', 'undone', 'unconfirmed']


class CommandRecoveryResponse(BaseModel):
    observation_id: str
    source: Literal['command_end', 'current_check']
    entries: list[RecoveryEntry]
    problems: list[str]
    restorations: list[RecoveryReceipt]
    previewed_path: str | None = None


class CommandRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(min_length=1)
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,128}$')


class CommandFileRecovery:
    def __init__(self, tracker: CommandFiles):
        self.tracker = tracker
        self.journal = tracker.journal

    def _load(self, observation_id: str, session_id: str, run_id: str):
        if not re.fullmatch(r'filechange-[a-f0-9]{32}', observation_id):
            raise FileJournalError('找不到这条文件备份记录。', code='not_found')
        folder = self.tracker.root / observation_id
        state = json.loads((folder / 'observation.json').read_bytes())
        if (state.get('id'), state.get('session_id'), state.get('run_id')) != (observation_id, session_id, run_id):
            raise FileJournalError('这条备份不属于当前命令。', code='not_found')
        return folder, state

    def _backup(self, folder: Path, key: str, before: dict) -> Path:
        expected = folder / (hashlib.sha256(key.encode()).hexdigest() + '.before')
        if before['backup'] != str(expected) or self.tracker._linked(expected) or _file_sha256(expected) != before['sha256']:
            raise FileJournalError('执行前备份缺失或已变化，无法恢复。', code='invalid')
        return expected

    def _receipt(self, request_id: str, saved: dict) -> RecoveryReceipt:
        change = self.journal.get(saved['change_id'])
        status = ('undone' if change.undone_at else 'applied' if change.apply_phase == 'applied' else 'unconfirmed') if change else 'unconfirmed'
        return RecoveryReceipt(request_id=request_id, path=saved['path'], change_id=saved['change_id'], status=status)

    def inspect(self, observation_id: str, session_id: str, run_id: str, *, preview_path: str | None = None) -> CommandRecoveryResponse:
        with self.tracker._lock:
            folder, state = self._load(observation_id, session_id, run_id)
            return self._inspect(folder, state, preview_path=preview_path, preview=True)

    def _inspect(self, folder: Path, state: dict, *, preview_path: str | None = None, preview: bool = False) -> CommandRecoveryResponse:
        after, problems = self.tracker._scan(state['paths'], folder, backup=False)
        problems = [*state.get('end_problems', state['problems']), *problems]
        frozen = {os.path.normcase(entry['path']): entry for entry in state.get('observed_entries', [])}
        has_end = 'observed_entries' in state
        entries = []
        for key in sorted(state['before'].keys() | after.keys() | frozen.keys()):
            before, current = state['before'].get(key), after.get(key)
            path = (before or current or frozen[key])['path']
            before_hash, current_hash = (before['sha256'] if before else None), (current['sha256'] if current else None)
            entry = RecoveryEntry(path=path, revision=hashlib.sha256(json.dumps(
                [state['id'], path, before_hash, current_hash, has_end, frozen.get(key)], sort_keys=True).encode()).hexdigest(),
                action='restore' if before else 'remove', status='available',
                before_sha256=before_hash, current_sha256=current_hash)
            problem = next((p['reason'] for p in problems if p['path'] and Path(path).is_relative_to(Path(p['path']))), None)
            if not problem and not current and os.path.lexists(path):
                problem = '当前位置不是可核对的普通文件，已保留。'
            try:
                backup = self._backup(folder, key, before) if before else None
            except (OSError, FileJournalError) as exc:
                backup = None
                problem = str(exc)
            if problem:
                entry.status, entry.note = 'unavailable', mask_secrets(problem)[0]
            elif before_hash == current_hash:
                entry.status, entry.note = 'unchanged', '当前文件与执行前一致，无需恢复。'
            elif has_end and current_hash != frozen.get(key, {}).get('after_sha256', before_hash):
                entry.status, entry.note = 'conflict', '命令结束快照之后文件又有变化，已保留当前版本。'
            # A completed observation remains authoritative even after a crash.
            # Without it, this is an explicitly labelled present-day comparison.
            entries.append(entry)
        selected = next((e for e in entries if e.path == preview_path), None) if preview_path else next(
            (e for e in entries if e.status != 'unchanged'), entries[0] if entries else None)
        if preview and selected and selected.status != 'unavailable':
            # One selected preview bounds response memory independently of directory size.
            try:
                before = state['before'].get(os.path.normcase(selected.path))
                backup = self._backup(folder, os.path.normcase(selected.path), before) if before else None
                selected.before_text, bmask = mask_secrets(verified_text(backup, selected.before_sha256)) if backup else ('', 0)
                selected.current_text, cmask = mask_secrets(verified_text(Path(selected.path), selected.current_sha256)) if selected.current_sha256 else ('', 0)
                if bmask + cmask:
                    selected.note = '；'.join(filter(None, [selected.note, '预览中的密钥已遮住，恢复使用原始文件字节。']))
            except (OSError, ValueError, FileJournalError) as exc:
                selected.note = '；'.join(filter(None, [selected.note, mask_secrets(str(exc))[0]]))
        return CommandRecoveryResponse(observation_id=state['id'], source='command_end' if has_end else 'current_check',
            entries=entries, problems=[mask_secrets(f"{p['path']}：{p['reason']}")[0] for p in problems],
            restorations=[self._receipt(key, value) for key, value in state.get('recovery_requests', {}).items()],
            previewed_path=selected.path if preview and selected else None)

    def restore(self, observation_id: str, session_id: str, run_id: str, request: CommandRecoveryRequest) -> RecoveryReceipt:
        with self.tracker._lock:
            folder, state = self._load(observation_id, session_id, run_id)
            requests = state.setdefault('recovery_requests', {})
            old = requests.get(request.request_id)
            if old and (old['path'], old['revision']) != (request.path, request.revision):
                raise FileJournalError('恢复请求编号已用于另一份核对结果。', code='conflict')
            if old and self.journal.get(old['change_id']) is not None:
                return self._receipt(request.request_id, old)
            checked = self._inspect(folder, state)
            entry = next((e for e in checked.entries if e.path == request.path), None)
            if entry is None or entry.status != 'available' or entry.revision != request.revision:
                raise FileJournalError('文件或备份在核对后有变化，未执行恢复；请重新核对。', code='conflict')
            key = os.path.normcase(entry.path)
            before = state['before'].get(key)
            if before:
                contents = self._backup(folder, key, before).read_bytes()
                if hashlib.sha256(contents).hexdigest() != entry.before_sha256:
                    raise FileJournalError('备份读取期间变化，未执行恢复。', code='conflict')
                operation = PlannedWrite(Path(entry.path), contents, expected_before_sha256=entry.current_sha256 or '')
            else:
                operation = PlannedDelete(Path(entry.path), expected_before_sha256=entry.current_sha256)
            tool_id = f"command-recovery:{observation_id}:{request.request_id}"
            saved = {'path': entry.path, 'revision': entry.revision, 'change_id': self.journal.change_id_for(run_id, tool_id)}
            requests[request.request_id] = saved
            # Persist intent before the existing journal owns the disk operation.
            _write_atomic(folder / 'observation.json', json.dumps(state, ensure_ascii=False).encode())
            self.journal.apply(session_id=session_id, run_id=run_id, tool_call_id=tool_id,
                title='从中断命令的备份恢复文件', operations=[operation])
            return self._receipt(request.request_id, saved)
