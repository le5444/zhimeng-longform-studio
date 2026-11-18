from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading

from fastapi.testclient import TestClient

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session, enqueue
from test_agent_service import TurnScriptedProvider
from test_memory_permissions import _harness
from test_session_goal import _request, _answer
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SqliteStore, SCHEMA_VERSION
from zhimeng_runtime.session_goal import GoalStore, GoalAdmission


def test_goal_hard_exit_preserves_stage_and_file_and_resumes_without_replay(tmp_path):
    child = textwrap.dedent('''
        import asyncio, json, os, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[2])
        from test_memory_permissions import _harness
        from test_agent_service import ScriptedProvider
        from test_session_goal import _request
        from zhimeng_runtime.agent_service import AgentService
        from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
        from zhimeng_runtime.session_goal import GoalAdmission
        async def main():
            root = Path(sys.argv[1])
            base, _, _, _ = _harness(root)
            class Provider(ScriptedProvider):
                calls = 0
                async def stream_chat(self, messages, *, cancel_event, tools=()):
                    self.calls += 1
                    if self.calls == 1:
                        yield ProviderToolCall(0, 'write-once', 'write_file', {'path':str(root/'report.md'), 'content':'Saved before exit'})
                        yield ProviderCompleted('tool_calls', None)
                    elif self.calls == 2:
                        yield ProviderTextDelta('File saved, verification remains.')
                        yield ProviderCompleted('stop', None)
                    else:
                        os._exit(23)
            service = AgentService(base.store, Provider([]), tool_executor=base.tool_executor, auto_memory=False)
            session = await service.create_session(project_id=None, title='Hard exit goal')
            started = await service.start_run(**_request(session, goal=GoalAdmission(action='start')))
            (root/'identity.json').write_text(json.dumps({'run':started.run.run_id,'session':session.session_id}))
            await service._tasks[started.run.run_id]
        asyncio.run(main())
    ''')
    result = subprocess.run([sys.executable, '-c', child, str(tmp_path), str(Path(__file__).parent)], capture_output=True,
                            text=True, timeout=45, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert result.returncode == 23, result.stderr
    identity = json.loads((tmp_path/'identity.json').read_text())
    store = AgentStore(SqliteStore(tmp_path/'runtime.sqlite3'))
    assert store.recover_interrupted() == 1
    assert store.recover_interrupted() == 0
    goal = GoalStore(store.database).read(identity['session']).goal
    assert goal.status == 'paused' and goal.stage_count == 1
    before = (tmp_path/'report.md').read_bytes()
    class Resume(TurnScriptedProvider):
        async def stream_chat(self, messages, **kwargs):
            assert any('Saved before exit' in str(m) for m in messages)
            assert any(goal.objective in str(m) for m in messages)
            async for event in super().stream_chat(messages, **kwargs):
                yield event
    async def resume():
        from zhimeng_runtime.agent_tools import AgentToolExecutor
        from zhimeng_runtime.project_reader import ProjectReader
        from zhimeng_runtime.project_locks import ProjectLockRegistry
        from zhimeng_runtime.change_set_service import ChangeSetService
        reader = ProjectReader((tmp_path,))
        executor = AgentToolExecutor(reader, store.database, ProjectLockRegistry(), ChangeSetService(reader, store.database, tmp_path/'changes'))
        provider = Resume([
            [ProviderToolCall(0,'read-back','read_file',{'path':str(tmp_path/'report.md')}),ProviderCompleted('tool_calls',None)],
            [ProviderToolCall(0,'mark-done','goal_update',{'status':'completed','progress':'Recovered and checked','evidence':'Read saved report'}),ProviderCompleted('tool_calls',None)],
            _answer('Verified after restart.'),
        ])
        service = AgentService(store, provider, tool_executor=executor, auto_memory=False)
        try:
            session_record = store.get_session(identity['session'])
            started = await service.start_run(**_request(session_record, 'resume-after-exit', goal=GoalAdmission(action='resume', goal_id=goal.goal_id, expected_revision=goal.revision)))
            await asyncio.wait_for(service._tasks[started.run.run_id], 30)
            assert store.get_run(started.run.run_id).status.value == 'succeeded'
            assert GoalStore(store.database).read(identity['session']).goal.status == 'completed'
            assert [call.tool_name for call in store.list_tool_calls(started.run.run_id)] == ['read_file','goal_update']
            assert (tmp_path/'report.md').read_bytes() == before
        finally:
            await service.shutdown()
    asyncio.run(resume())


def test_new_user_input_invalidates_pending_completion_and_goal_api_persists(settings):
    class Provider(ApiProvider):
        calls = 0
        entered = threading.Event()
        release = threading.Event()
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.calls += 1
            if self.calls in {1, 4}:
                yield ProviderToolCall(0, f'goal-done-{self.calls}', 'goal_update', {'status':'completed','progress':'Checked requirements','evidence':'Verified the requested answer'})
                yield ProviderCompleted('tool_calls',None)
            else:
                if self.calls == 2:
                    self.entered.set()
                    while not self.release.is_set():
                        await asyncio.sleep(0.01)
                yield ProviderTextDelta(f'Answer stage {self.calls}')
                yield ProviderCompleted('stop',None)
    provider = Provider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        response = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key':'goal-api-start'},
                              json={'message':'Explain the report','goal':{'action':'start'}})
        assert response.status_code == 202, response.text
        rid = response.json()['run']['run_id']
        assert provider.entered.wait(15)
        pending = client.get(f'/api/v1/agent/sessions/{sid}/goal').json()['goal']
        assert pending['status'] == 'active' and pending['requested_status'] == 'completed'
        enqueue(client, sid, message='Also explain the verification method.')
        accepted = client.post(f'/api/v1/agent/sessions/{sid}/queue/message-0001/steer', json={'expected_run_id':rid})
        assert accepted.status_code == 200
        provider.release.set()
        assert wait_for_success(client,rid)['status'] == 'succeeded'
        assert provider.calls == 5
        finished = client.get(f'/api/v1/agent/sessions/{sid}/goal').json()['goal']
        assert finished['status'] == 'completed'
        stale = client.post(f'/api/v1/agent/sessions/{sid}/goal/end', json={'goal_id':finished['goal_id'],'expected_revision':1})
        assert stale.status_code == 409
        ended = client.post(f'/api/v1/agent/sessions/{sid}/goal/end', json={'goal_id':finished['goal_id'],'expected_revision':finished['revision']})
        assert ended.status_code == 200 and ended.json()['goal']['status'] == 'ended'
        with client.app.state.agent_service.store.database.connection() as connection:
            kinds = [r[0] for r in connection.execute('SELECT kind FROM goal_event ORDER BY revision')]
        assert 'new_input' in kinds and kinds[-1] == 'end'


def test_v32_migration_keeps_existing_sessions_without_inventing_goals(tmp_path):
    async def prepare():
        base, _, _, _ = _harness(tmp_path)
        session = await base.create_session(project_id=None, title='Existing chat')
        with base.store.database.connection() as connection:
            connection.executescript('DROP TABLE goal_event; DROP TABLE session_goal; PRAGMA user_version=32;')
        assert base.store.database.initialize().ready
        assert GoalStore(base.store.database).read(session.session_id).goal is None
        assert base.store.get_session(session.session_id).title == 'Existing chat'
        with base.store.database.connection() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    asyncio.run(prepare())
