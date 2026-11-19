from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from zhimeng_runtime.project_instruction_context import MAX_PROJECT_INSTRUCTION_BYTES, read_project_instruction_snapshot

from test_agent_service import wait_terminal
from test_opening_context_freeze_20260927 import _AllRequests
from test_project_memory_scope import build_project


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing mode")
def test_windows_exclusive_handle_reports_unreadable_then_recovers(tmp_path: Path):
    import ctypes
    from ctypes import wintypes

    path = tmp_path / "AGENTS.md"
    original = "表格金额保留两位小数。\r\n".encode("utf-16")
    path.write_bytes(original)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ, no sharing, OPEN_EXISTING: Python cannot read this handle's file.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        snapshot = read_project_instruction_snapshot(tmp_path)
        assert dict(snapshot.states)["AGENTS.md"] == "unreadable"
        assert "不能认定已删除" in snapshot.message()["content"]
        assert snapshot.files == ()
    finally:
        assert kernel.CloseHandle(handle)
    recovered = read_project_instruction_snapshot(tmp_path)
    assert not recovered.unreliable
    assert recovered.files == (("AGENTS.md", "表格金额保留两位小数。"),)
    assert path.read_bytes() == original


def test_project_disappearing_during_read_does_not_confirm_deletions(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    original_open = Path.open

    def opened(candidate, *args, **kwargs):
        if candidate == project / "AGENTS.md":
            project.rmdir()
        return original_open(candidate, *args, **kwargs)

    with patch.object(Path, "open", opened):
        snapshot = read_project_instruction_snapshot(project)
    assert not snapshot.available
    message = snapshot.message(current=True, missing_names=("AGENTS.md",))["content"]
    assert "无法核对" in message
    assert "不存在" not in message


def test_locked_instruction_is_not_reported_as_deleted_and_recovers(tmp_path: Path):
    async def scenario():
        provider = _AllRequests()
        service, binding, _changes, project = build_project(tmp_path, provider)
        path = project / 'AGENTS.md'
        original = path.read_bytes()
        try:
            session = await service.create_session(project_id=binding.project_id, title='普通表格约定')
            accepted = await service.start_run(session_id=session.session_id, message='查看表格列名约定。',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, idempotency_key='instruction-lock-baseline')
            run = await wait_terminal(service, accepted.run.run_id)
            original_open = Path.open

            def opened(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError('temporary sharing violation')
                return original_open(candidate, *args, **kwargs)

            with patch.object(Path, 'open', opened):
                opening, update = await service._opening_context_messages(run)
            assert update is not None
            assert '都没有了' not in update['content']
            assert '无法读取' in update['content'] and 'AGENTS.md' in update['content']
            assert '写作笔记.md' not in update['content']
            assert '表格列名保留原始名称' in str(opening)
            _opening, recovered = await service._opening_context_messages(run, replayed=[update])
            assert recovered is not None
            assert '表格列名保留原始名称' in recovered['content']
            assert '无法读取' not in recovered['content']
            assert path.read_bytes() == original
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_partial_read_keeps_other_instructions_and_empty_is_not_missing(tmp_path: Path):
    agents = tmp_path / 'AGENTS.md'
    agents.write_bytes(b'original\r\n')
    (tmp_path / 'CLAUDE.md').write_bytes('保留表格列名。\r\n'.encode())
    (tmp_path / '写作笔记.md').write_bytes(b' \r\n')
    original_open = Path.open

    def opened(candidate, *args, **kwargs):
        if candidate == agents:
            raise PermissionError('temporarily locked')
        return original_open(candidate, *args, **kwargs)

    with patch.object(Path, 'open', opened):
        snapshot = read_project_instruction_snapshot(tmp_path)
    assert snapshot.files == (('CLAUDE.md', '保留表格列名。'),)
    assert dict(snapshot.states) == {'AGENTS.md': 'unreadable', 'CLAUDE.md': 'read', '写作笔记.md': 'empty'}
    assert snapshot.unreliable
    message = snapshot.message(current=True)['content']
    assert 'CLAUDE.md' in message and '保留表格列名' in message
    assert '文件存在，内容为空' in message
    assert '不存在' not in message
    assert agents.read_bytes() == b'original\r\n'


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-32'])
def test_instruction_decoding_and_multibyte_prefix_preserve_source(tmp_path: Path, encoding: str):
    path = tmp_path / 'AGENTS.md'
    content = '列名与金额😀\r\n'
    original = content.encode(encoding)
    path.write_bytes(original)
    snapshot = read_project_instruction_snapshot(tmp_path)
    assert snapshot.files == (('AGENTS.md', content.strip()),)
    assert path.read_bytes() == original
    original = ('金额😀' * MAX_PROJECT_INSTRUCTION_BYTES).encode(encoding)
    path.write_bytes(original)
    snapshot = read_project_instruction_snapshot(tmp_path)
    assert not snapshot.unreliable
    text = dict(snapshot.files)['AGENTS.md']
    assert '\ufffd' not in text
    assert text.endswith('要看全文就读这个文件。）')
    assert path.read_bytes() == original


@pytest.mark.parametrize('raw', [b'Rule\xff', b'Rule\x00', b'\xff\xfe\x01'])
def test_invalid_instruction_text_has_no_replacement_content(tmp_path: Path, raw: bytes):
    path = tmp_path / 'AGENTS.md'
    path.write_bytes(raw)
    snapshot = read_project_instruction_snapshot(tmp_path)
    assert dict(snapshot.states)['AGENTS.md'] == 'invalid_text'
    assert snapshot.files == ()
    assert '无法读取' in snapshot.message()['content']
    assert path.read_bytes() == raw


def test_unavailable_project_directory_does_not_confirm_file_deletions(tmp_path: Path):
    snapshot = read_project_instruction_snapshot(tmp_path / 'missing-project')
    assert not snapshot.available
    assert '无法核对' in snapshot.message()['content']
    assert '不存在' not in snapshot.message(current=True)['content']


def test_first_unreadable_snapshot_survives_restart_then_recovers(tmp_path: Path):
    async def scenario():
        provider = _AllRequests()
        service, binding, _changes, project = build_project(tmp_path, provider)
        # Invalid text still has a stable file identity: exercise the whole Run,
        # original transcript, persisted opening artifact and a fresh service.
        path = project / 'AGENTS.md'
        path.write_bytes(b'Rule\xff')
        inspection = service.tool_executor.reader.inspect(project)['project']
        binding = service.store.database.sync_project_binding(project_id=binding.project_id,
            root_path=str(project), display_name='table project', fingerprint=inspection['fingerprint'])
        try:
            session = await service.create_session(project_id=binding.project_id, title='说明恢复')
            accepted = await service.start_run(session_id=session.session_id, message='读取项目说明。',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, idempotency_key='first-unreadable')
            run = await wait_terminal(service, accepted.run.run_id)
            assert run.status.value == 'succeeded'
            assert '无法读取' in str(provider.requests[-1])
            opening = service.store.session_opening_context(session.session_id)
            assert opening['project_instructions'] == []
            assert opening['project_instruction_state']['states']['AGENTS.md'] == 'invalid_text'
            await service.shutdown()
            from zhimeng_runtime.agent_service import AgentService
            restarted = AgentService(service.store, _AllRequests(), tool_executor=service.tool_executor)
            try:
                original = '表格金额保留两位小数。\r\n'.encode('utf-16')
                path.write_bytes(original)
                messages, update = await restarted._opening_context_messages(run)
                assert '无法读取' in str(messages)  # The immutable opening is historical.
                assert '表格金额保留两位小数' in update['content']
                assert '无法读取' not in update['content']
                assert '写作笔记.md' not in update['content']
                _, repeated = await restarted._opening_context_messages(run, replayed=[update])
                assert repeated is None
                assert restarted.store.session_opening_context(session.session_id) == opening
                assert path.read_bytes() == original
            finally:
                await restarted.shutdown()
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_empty_deleted_and_detached_project_have_different_updates(tmp_path: Path):
    async def scenario():
        service, binding, _changes, project = build_project(tmp_path, _AllRequests())
        try:
            session = await service.create_session(project_id=binding.project_id, title='说明状态')
            accepted = await service.start_run(session_id=session.session_id, message='读取项目说明。',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, idempotency_key='instruction-state')
            run = await wait_terminal(service, accepted.run.run_id)
            path = project / 'AGENTS.md'
            path.write_bytes(b' \r\n')
            _, empty = await service._opening_context_messages(run)
            assert 'AGENTS.md：文件存在，内容为空' in empty['content']
            path.unlink()
            _, deleted = await service._opening_context_messages(run, replayed=[empty])
            assert 'AGENTS.md：本轮检查时不存在' in deleted['content']
            detached = run.model_copy(update={'project_revision': None})
            _, unrelated = await service._opening_context_messages(detached, replayed=[deleted])
            assert '未关联文件夹' in unrelated['content']
            assert '不存在' not in unrelated['content']
            # Pre-metadata artifacts are still readable, but never prove that
            # a newly inaccessible file has been removed.
            old_opening = service.store.session_opening_context(session.session_id)
            old_opening.pop('project_instruction_state')
            path.write_bytes(b'Rule\xff')
            with patch.object(service.store, 'session_opening_context', return_value=old_opening):
                _, legacy_update = await service._opening_context_messages(run)
            assert '无法读取' in legacy_update['content']
            assert 'AGENTS.md：本轮检查时不存在' not in legacy_update['content']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_binding_error_is_unavailable_and_identical_other_project_is_refreshed(tmp_path: Path):
    async def scenario():
        service, binding, _changes, project = build_project(tmp_path, _AllRequests())
        try:
            session = await service.create_session(project_id=binding.project_id, title='切换项目说明')
            accepted = await service.start_run(session_id=session.session_id, message='核对项目约定。',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, idempotency_key='instruction-binding')
            run = await wait_terminal(service, accepted.run.run_id)
            with patch.object(service.store.database, 'get_project_binding', side_effect=OSError('database read unavailable')):
                _, unavailable = await service._opening_context_messages(run)
            assert '无法核对' in unavailable['content']
            assert '未关联' not in unavailable['content']
            other = tmp_path / 'other-table-project'
            other.mkdir()
            (other / 'AGENTS.md').write_bytes((project / 'AGENTS.md').read_bytes())
            identity = service.tool_executor.reader.inspect(other)['project']
            other_binding = service.store.database.sync_project_binding(project_id=identity['project_id'],
                root_path=str(other), display_name='other table', fingerprint=identity['fingerprint'])
            other_run = run.model_copy(update={'project_revision': run.project_revision.model_copy(update={'project_id': other_binding.project_id})})
            _, changed = await service._opening_context_messages(other_run)
            assert changed is not None
            assert '表格列名保留原始名称' in changed['content']
            snapshot = await service._project_instruction_snapshot(other_run)
            assert snapshot.project_id == other_binding.project_id
            _, repeated = await service._opening_context_messages(other_run, replayed=[changed])
            assert repeated is None
        finally:
            await service.shutdown()
    asyncio.run(scenario())
