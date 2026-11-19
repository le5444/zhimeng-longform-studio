"""A real process exit and an immediate follow-up must retain completed work."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading

import pytest

from test_agent_interrupted_transcript import assert_protocol_pairs
from test_agent_service import ScriptedProvider
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.database import SqliteStore


CHILD = textwrap.dedent('''
    import asyncio, json, os, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[3])
    from test_memory_permissions import _harness
    from test_agent_service import ScriptedProvider
    from zhimeng_runtime.agent_service import AgentService
    from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall

    async def main():
        root, mode = Path(sys.argv[1]), sys.argv[2]
        base, _, _, _ = _harness(root)
        class Provider(ScriptedProvider):
            requests = 0
            async def stream_chat(self, messages, *, cancel_event, tools=()):
                self.requests += 1
                if self.requests > 1:
                    os._exit(23)
                yield ProviderTextDelta('Saving the first receipt.')
                yield ProviderToolCall(0, 'first', 'write_file', {'path': str(root/'receipt.txt'), 'content': 'saved once'})
                yield ProviderToolCall(1, 'second', 'write_file', {'path': str(root/'later.txt'), 'content': 'later'})
                yield ProviderCompleted('tool_calls', None)
        service = AgentService(base.store, Provider([]), tool_executor=base.tool_executor, auto_memory=False)
        execute = base.tool_executor.execute
        async def crash_after_write(run, **kwargs):
            result = await execute(run, **kwargs)
            if mode == 'after_write':
                os._exit(23)
            return result
        base.tool_executor.execute = crash_after_write
        prepare = service._prepare_tool_call
        async def crash_between_tools(run, **kwargs):
            if mode == 'before_first' or (mode == 'before_second' and kwargs['provider_tool_call_id'] == 'second'):
                os._exit(23)
            return await prepare(run, **kwargs)
        service._prepare_tool_call = crash_between_tools
        session = await service.create_session(project_id=None, title='Durable handoff')
        started = await service.start_run(session_id=session.session_id, message='Save two receipts.',
            expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
            writer_mode='author_review_change_proposal', idempotency_key='first')
        (root/'identity.json').write_text(json.dumps({'run': started.run.run_id, 'session': session.session_id}))
        await service._tasks[started.run.run_id]
    asyncio.run(main())
''')


@pytest.mark.parametrize('mode,completed', [('before_first', 0), ('before_second', 1), ('after_write', 1), ('next_request', 2), ('legacy', 2)])
def test_hard_exit_recovers_batch_before_ordinary_followup(tmp_path, mode, completed):
    child = subprocess.run([sys.executable, '-c', CHILD, str(tmp_path), mode, str(Path(__file__).parent)],
                           capture_output=True, text=True, timeout=45, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert child.returncode == 23, child.stdout + child.stderr
    identity = json.loads((tmp_path / 'identity.json').read_text())
    store = AgentStore(SqliteStore(tmp_path / 'runtime.sqlite3'))
    if mode == 'legacy':
        with store.database.connection() as connection:
            connection.execute('DELETE FROM run_transcript WHERE run_id=?', (identity['run'],))
    assert store.recover_interrupted() == 1
    assert store.recover_interrupted() == 0
    assert store.get_run(identity['run']).status.value == 'interrupted'
    assert len(store.list_tool_calls(identity['run'])) == completed
    for path in ['receipt.txt', 'later.txt'][:completed]:
        assert (tmp_path / path).exists()
    for path in ['receipt.txt', 'later.txt'][completed:]:
        assert not (tmp_path / path).exists()

    class Followup(ScriptedProvider):
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            assert_protocol_pairs(messages)
            results = [json.loads(m['content']) for m in messages if m.get('role') == 'tool']
            assert [r['status'] for r in results] == ['succeeded'] * completed + ['not_started'] * (2 - completed)
            assert [m.get('content') for m in messages].count('Saving the first receipt.') == 1
            yield ProviderTextDelta('Previous receipts accounted for.')
            yield ProviderCompleted('stop', None)

    async def followup():
        service = AgentService(store, Followup([]), auto_memory=False)
        try:
            run = await service.start_run(session_id=identity['session'], message='What finished before the restart?',
                expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
                idempotency_key='ordinary-followup')
            await service._tasks[run.run.run_id]
            assert store.get_run(run.run.run_id).status.value == 'succeeded'
            assert len(store.list_tool_calls(identity['run'])) == completed
        finally:
            await service.shutdown()
    asyncio.run(followup())


@pytest.mark.parametrize('fail_commit', [False, True])
def test_completion_and_transcript_are_one_commit(tmp_path, monkeypatch, fail_commit):
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        entered, release = threading.Event(), threading.Event()
        save = base.store._save_run_transcript_in_connection

        def held_commit(connection, run_id, messages):
            if messages and messages[-1].get('content') == 'Receipt saved.':
                entered.set()
                assert release.wait(15)
                if fail_commit:
                    raise RuntimeError('Injected transcript commit failure')
            return save(connection, run_id, messages)
        monkeypatch.setattr(base.store, '_save_run_transcript_in_connection', held_commit)

        class Provider(ScriptedProvider):
            requests = 0
            async def stream_chat(self, messages, *, cancel_event, tools=()):
                self.requests += 1
                if self.requests == 1:
                    yield ProviderToolCall(0, 'receipt', 'write_file', {'path': str(tmp_path/'receipt.txt'), 'content': 'saved'})
                    yield ProviderCompleted('tool_calls', None)
                else:
                    if self.requests > 2:
                        assert_protocol_pairs(messages)
                        assert len([m for m in messages if m.get('role') == 'tool']) == 1
                        assert any(m.get('content') == 'Receipt saved.' for m in messages)
                    yield ProviderTextDelta('Receipt saved.' if self.requests == 2 else 'Follow-up complete.')
                    yield ProviderCompleted('stop', None)
        service = AgentService(base.store, Provider([]), tool_executor=base.tool_executor, auto_memory=False)
        common = dict(expected_project_revision=None, expected_project_fingerprint=None,
                      active_chapter_id=None, writer_mode='author_review_change_proposal')
        try:
            session = await service.create_session(project_id=None, title='Atomic completion')
            first = await service.start_run(session_id=session.session_id, message='Save receipt.', idempotency_key='first', **common)
            task = service._tasks[first.run.run_id]
            assert await asyncio.to_thread(entered.wait, 15)
            assert service.store.get_run(first.run.run_id).status.value == 'running'
            release.set()
            await task
            status = service.store.get_run(first.run.run_id).status.value
            assert status == ('failed' if fail_commit else 'succeeded')
            if fail_commit:
                with service.store.database.connection() as connection:
                    assert not connection.execute("SELECT 1 FROM activity WHERE run_id=? AND json_extract(payload_json, '$.status')='succeeded'", (first.run.run_id,)).fetchone()
            else:
                second = await service.start_run(session_id=session.session_id, message='What did you do?', idempotency_key='second', **common)
                await service._tasks[second.run.run_id]
                assert service.store.get_run(second.run.run_id).status.value == 'succeeded'
            assert (tmp_path/'receipt.txt').read_bytes() == b'saved'
        finally:
            release.set()
            await service.shutdown()
            await base.shutdown()
    asyncio.run(scenario())
