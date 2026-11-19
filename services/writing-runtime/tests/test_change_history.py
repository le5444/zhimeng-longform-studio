import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_task_review import setup, create, complete
from test_agent_store import start_run
from zhimeng_runtime.change_history import ChangeHistoryReader, ChangeHistoryPage, ChangeHistoryDetail
from zhimeng_runtime.change_history_api import create_change_history_router
from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.file_changes_api import create_file_changes_router
from zhimeng_runtime.file_journal import PlannedWrite


@pytest.fixture
def history(setup):
    service, store, session, binding, folder = setup
    reader = ChangeHistoryReader(store, service.tool_executor.file_journal, service.tool_executor.change_sets)
    app = FastAPI(); install_exception_handlers(app)
    app.include_router(create_change_history_router(reader))
    app.include_router(create_file_changes_router(reader.journal, store))
    with TestClient(app) as client:
        yield reader, client, setup
    asyncio.run(service.shutdown())


def write_project(history, path='report.txt', content='Total: 49.50'):
    reader, _, setup = history
    service, store, _, _, _ = setup
    run, _, _ = create(setup, writer_mode='author_review_change_proposal')
    start_run(store, run.run_id)
    result = asyncio.run(service._execute_tool_call(run, provider_tool_call_id='history-write', tool_name='write_file',
        arguments={'path':path, 'content':content}, cancel_event=asyncio.Event()))
    assert result.tool_call.status=='succeeded'
    store.complete_run(run.run_id, content='saved', message_id='message-'+run.run_id, finish_reason='stop', usage=None)
    return run, result.tool_call.output['change_set_id']


def journal_write(history, *, session=None, run=None, name='external.csv'):
    reader, _, setup = history
    _, store, current, _, folder = setup
    if run is None:
        run, _, _ = create(setup); complete(store, run)
    return reader.journal.apply(session_id=(session or current).session_id, run_id=run.run_id, tool_call_id=name,
        title='Export '+name, operations=[PlannedWrite(folder.parent/name,b'item,total\nPaper,49.50\n')])


def route(history): return '/api/v1/agent/sessions/'+history[2][2].session_id+'/changes'


def test_scope_keeps_current_chat_separate_and_project_includes_other_chats(history, monkeypatch):
    reader, client, setup = history
    _, store, session, binding, folder = setup
    run, project_id = write_project(history)
    external = journal_write(history, run=run)
    other = store.create_session(project_id=binding.project_id,title='Another task',executor=session.executor,capabilities=session.capabilities)
    other_run,_,_ = store.create_run(session_id=other.session_id,message='other',idempotency_key='other',
        expected_project_revision=store.database.get_project_binding(binding.project_id).revision,
        expected_project_fingerprint=store.database.get_project_binding(binding.project_id).fingerprint,active_chapter_id=None)
    complete(store,other_run)
    other_change=journal_write(history,session=other,run=other_run,name='other.csv')
    # Listing metadata must not hash every artifact on disk.
    monkeypatch.setattr('zhimeng_runtime.change_history.observe_file',lambda *a,**k:pytest.fail('list read disk'))
    current=client.get(route(history)).raise_for_status().json()
    assert {c['id'] for c in current['changes']}=={project_id,external.change_id}
    assert {c['source'] for c in current['changes']}=={'change_set','file_change'}
    whole=client.get(route(history),params={'scope':'project'}).raise_for_status().json()
    assert {c['id'] for c in whole['changes']}=={project_id,external.change_id,other_change.change_id}
    assert client.get(route(history)+'/'+other_change.change_id).status_code==404


def test_project_and_external_details_use_actual_files_and_original_undo(history):
    reader,client,setup=history
    _,_,session,_,folder=setup
    run,project_id=write_project(history)
    external=journal_write(history,run=run)
    for change_id in [project_id,external.change_id]:
        detail=client.get(route(history)+'/'+change_id).raise_for_status().json()
        assert detail['undo_available'] is True
        assert detail['previews'][0]['current_state']=='file'
        assert Path(detail['previews'][0]['open_path']).exists()
        assert detail['previews'][0]['hunks']
    reader.projects.rollback(project_id,ChangeSetRollbackRequest(expected_review_revision=reader.projects.get(project_id).review_revision))
    undone=client.get(route(history)+'/'+project_id).raise_for_status().json()
    assert undone['status']=='rolled_back' and undone['undo_available'] is False
    assert undone['previews'][0]['open_path'] is None and not (folder/'report.txt').exists()
    response=client.post(f'/api/v1/agent/sessions/{session.session_id}/file-changes/{external.change_id}/undo')
    assert response.status_code==200
    gone=client.get(route(history)+'/'+external.change_id).raise_for_status().json()
    assert gone['status']=='undone' and gone['previews'][0]['current_state']=='missing'


def test_external_edits_disable_undo_without_destroying_saved_diff(history):
    _,client,setup=history
    _,_,_,_,folder=setup
    run,project_id=write_project(history)
    (folder/'report.txt').write_text('human edit',encoding='utf-8')
    detail=client.get(route(history)+'/'+project_id).raise_for_status().json()
    assert detail['undo_available'] is False and detail['undo_reason']
    assert detail['previews'][0]['hunks'][0]['after']=='Total: 49.50'
    assert (folder/'report.txt').read_text()=='human edit'


def test_pagination_invalidates_on_undo_and_closed_or_unknown_sessions(history):
    reader,client,setup=history
    first=journal_write(history,name='first.csv')
    journal_write(history,name='second.csv')
    reader.list(setup[2].session_id,limit=1)
    page=client.get(route(history),params={'limit':1}).raise_for_status().json()
    assert page['total']==2 and page['next_offset']==1
    tail=client.get(route(history),params={'offset':1,'limit':1,'revision':page['revision']}).raise_for_status().json()
    assert tail['next_offset'] is None and tail['changes'][0]['id']!=page['changes'][0]['id']
    reader.journal.undo(first.change_id)
    assert client.get(route(history),params={'offset':1,'revision':page['revision']}).status_code==409
    assert client.get(route(history).replace(setup[2].session_id,'missing')).status_code==404
    with setup[1].database.connection() as db: db.execute("UPDATE execution_session SET status='closed' WHERE id=?",(setup[2].session_id,))
    assert client.get(route(history)).status_code==404


def test_rebound_session_retains_original_project_changes(history):
    reader,client,setup=history
    _,store,session,_,_=setup
    _,project_id=write_project(history)
    store.rebind_session(session.session_id,project_id=None)
    page=client.get(route(history)).raise_for_status().json()
    assert page['project_id'] is None and page['changes'][0]['id']==project_id
    assert client.get(route(history)+'/'+project_id).raise_for_status().json()['undo_available'] is True
    assert client.get(route(history),params={'scope':'project'}).status_code==409


def test_history_contracts():
    root=Path(__file__).resolve().parents[3]
    for name,model in [('change-history-page',ChangeHistoryPage),('change-history-detail',ChangeHistoryDetail)]:
        schema=json.loads((root/'packages/contracts'/f'{name}.schema.json').read_text(encoding='utf-8'))
        schema.pop('$id');schema.pop('$schema')
        assert schema==model.model_json_schema()
