from pathlib import Path
import asyncio
import json
import os
import sys
import time

import pytest

from zhimeng_runtime.command_files import CommandFiles
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.file_journal import FileChangeJournal, FileJournalError
from zhimeng_runtime.command_sessions import CommandSessionManager


@pytest.fixture
def tracker(tmp_path):
    store = SqliteStore(tmp_path/'runtime.sqlite3')
    store.initialize()
    return CommandFiles(FileChangeJournal(store, tmp_path/'state'))


def prepare(tracker, folder):
    return tracker.prepare(session_id='session-files', run_id='run-files', tool_call_id='call-files',
                           workdir=folder, paths=['.'])


def test_actual_changes_get_receipts_without_rewriting_then_restore_original_bytes(tracker, tmp_path):
    folder = tmp_path/'work'; folder.mkdir()
    original = '原始报告\r\n'.encode('utf-16')
    (folder/'report.txt').write_bytes(original)
    (folder/'delete.bin').write_bytes(bytes(range(256)))
    (folder/'.ordinary').write_bytes(b'hidden is still data')
    receipt = prepare(tracker, folder)
    (folder/'report.txt').write_bytes(b'new report\n')
    (folder/'delete.bin').unlink()
    (folder/'new.csv').write_bytes(b'a,b\n1,2\n')
    modified = (folder/'report.txt').stat().st_mtime_ns
    result = tracker.finish(receipt)
    assert result['status']=='recorded' and result['file_count']==3
    assert (folder/'report.txt').stat().st_mtime_ns==modified
    reloaded=CommandFiles(tracker.journal)
    assert reloaded.finish(receipt)==result
    tracker.journal.undo(result['change_id'])
    assert (folder/'report.txt').read_bytes()==original
    assert (folder/'delete.bin').read_bytes()==bytes(range(256))
    assert not (folder/'new.csv').exists()
    assert (folder/'.ordinary').read_bytes()==b'hidden is still data'


def test_later_external_edit_is_preserved_on_undo(tracker,tmp_path):
    folder=tmp_path/'work';folder.mkdir();path=folder/'document.md';path.write_bytes(b'before')
    receipt=prepare(tracker,folder);path.write_bytes(b'command result')
    result=tracker.finish(receipt);path.write_bytes(b'later human edit')
    with pytest.raises(FileJournalError,match='后来又被改过'):
        tracker.journal.undo(result['change_id'])
    assert path.read_bytes()==b'later human edit'


@pytest.mark.parametrize('when',['before','after'])
def test_unreadable_directory_never_invents_creations_or_deletions(tracker,tmp_path,monkeypatch,when):
    folder=tmp_path/'work';folder.mkdir();path=folder/'existing.txt';path.write_bytes(b'before')
    linked=tracker._linked
    def unavailable(value):
        if value==folder: raise PermissionError('fixture blocked')
        return linked(value)
    if when=='before': monkeypatch.setattr(tracker,'_linked',unavailable)
    receipt=prepare(tracker,folder)
    if when=='before': monkeypatch.setattr(tracker,'_linked',linked)
    else: monkeypatch.setattr(tracker,'_linked',unavailable)
    path.write_bytes(b'after')
    result=tracker.finish(receipt)
    assert result['status']=='partial' and result['file_count']==0 and result['change_id'] is None
    assert result['problems']


def test_only_selected_paths_are_recorded(tracker,tmp_path):
    (tmp_path/'selected.txt').write_bytes(b'before')
    receipt=tracker.prepare(session_id='s',run_id='r',tool_call_id='t',workdir=tmp_path,
                            paths=['selected.txt','new.txt'])
    (tmp_path/'selected.txt').write_bytes(b'after')
    (tmp_path/'new.txt').write_bytes(b'new')
    (tmp_path/'outside.txt').write_bytes(b'outside')
    result=tracker.finish(receipt)
    assert result['file_count']==2
    tracker.journal.undo(result['change_id'])
    assert (tmp_path/'outside.txt').read_bytes()==b'outside'


def test_record_retry_keeps_original_end_version_after_later_human_edit(tracker,tmp_path,monkeypatch):
    folder=tmp_path/'work';folder.mkdir();target=folder/'report.txt';target.write_bytes(b'before')
    receipt=prepare(tracker,folder);target.write_bytes(b'command')
    record=tracker.journal.record_observed
    def fail(**kwargs): raise OSError('journal unavailable')
    monkeypatch.setattr(tracker.journal,'record_observed',fail)
    assert tracker.finish_safely(receipt)['status']=='unverified'
    target.write_bytes(b'later human edit')
    monkeypatch.setattr(tracker.journal,'record_observed',record)
    result=CommandFiles(tracker.journal).finish(receipt)
    with pytest.raises(FileJournalError,match='后来又被改过'):
        tracker.journal.undo(result['change_id'])
    assert target.read_bytes()==b'later human edit'


@pytest.mark.parametrize('tty',[False,True])
def test_background_stop_finalizes_real_file_changes_before_terminal_status(tracker,tmp_path,tty):
    if tty and os.name!='nt': pytest.skip('Windows ConPTY')
    folder=tmp_path/'work';folder.mkdir()
    script=folder/'produce.py'
    script.write_text("from pathlib import Path\nimport time\nPath('result.txt').write_bytes(b'result')\nprint('SAVED',flush=True)\ntime.sleep(60)\n",encoding='utf-8')
    receipt=prepare(tracker,folder)
    manager=CommandSessionManager(tmp_path/'commands',finalize_files=tracker.finish_safely)
    try:
        value=manager.start(owner_session_id='session-files',owner_run_id='run-files',
            command=f'"{sys.executable}" -u "{script}"',cwd=folder,yield_time_ms=100,tty=tty,file_tracking=receipt)
        deadline=time.monotonic()+15
        while 'SAVED' not in value['stdout']:
            assert time.monotonic()<deadline
            value=manager.read('session-files',value['session_id'],wait_ms=100)
        done=manager.stop('session-files',value['session_id'])
        assert done['status']=='cancelled' and done['file_tracking']['file_count']==1
        tracker.journal.undo(done['change_id'])
        assert not (folder/'result.txt').exists() and script.exists()
    finally: manager.close()


def test_recovered_running_observation_is_not_falsely_completed(tracker,tmp_path):
    folder=tmp_path/'work';folder.mkdir();receipt=prepare(tracker,folder)
    command=tmp_path/'commands'/'cmd-old';command.mkdir(parents=True)
    (command/'session.json').write_text(json.dumps({'schema':'zhimeng.command-session.v1',
        'session_id':'cmd-old','status':'running','file_tracking':receipt}),encoding='utf-8')
    manager=CommandSessionManager(tmp_path/'commands',finalize_files=tracker.finish_safely)
    try:
        saved=json.loads((command/'session.json').read_bytes())
        assert saved['file_tracking']['status']=='interrupted'
        assert not tracker.journal.session_changes('session-files')
    finally: manager.close()


def test_receipt_memory_failure_does_not_hide_actual_process_exit(tmp_path):
    def fail(_): raise MemoryError('fixture memory failure')
    manager=CommandSessionManager(tmp_path/'commands',finalize_files=fail)
    try:
        value=manager.start(owner_session_id='chat',owner_run_id='run',command='echo finished',cwd=tmp_path,
            yield_time_ms=100,file_tracking={'id':'observation','status':'observing','paths':[],'problems':[]})
        deadline=time.monotonic()+15
        while value['status']=='running':
            assert time.monotonic()<deadline
            value=manager.read('chat',value['session_id'],wait_ms=100)
        assert value['exit_code']==0 and value['file_tracking']['status']=='unverified'
        assert 'MemoryError' in value['file_tracking']['problems'][0]['reason']
    finally: manager.close()


@pytest.mark.parametrize('transport',['sync','pipes','pty'])
def test_command_tool_records_real_artifact_and_existing_undo_tool_restores_it(tmp_path,transport):
    if transport=='pty' and os.name!='nt': pytest.skip('Windows ConPTY')
    from test_memory_permissions import _harness
    from test_agent_direct_write_tools import _call
    from zhimeng_runtime.execution_protocol import RunStatus
    async def scenario():
        service,_,_,folder=_harness(tmp_path)
        target=folder/'result.txt';target.write_bytes(b'before\r\n')
        script=folder/'write.py';script.write_text("from pathlib import Path\nPath('result.txt').write_bytes(b'after\\n')\n",encoding='utf-8')
        try:
            session=await service.create_session(project_id=None,title='Command file recovery')
            run,_,_=service.store.create_run(session_id=session.session_id,message='Update the report',
                expected_project_revision=None,expected_project_fingerprint=None,active_chapter_id=None,
                writer_mode='author_review_change_proposal',idempotency_key='command-files')
            service.store.transition_run(run.run_id,RunStatus.STARTING,detail='start')
            run=service.store.transition_run(run.run_id,RunStatus.RUNNING,detail='run')
            arguments={'command':f'"{sys.executable}" "{script}"','workdir':str(folder),'record_paths':['result.txt']}
            if transport!='sync': arguments.update(yield_time_ms=100,tty=transport=='pty')
            value=(await _call(service.tool_executor,run,'computer_run_command',arguments)).output
            deadline=time.monotonic()+15
            while value.get('status')=='running':
                assert time.monotonic()<deadline
                value=await asyncio.to_thread(service.tool_executor._command_session_manager().read,
                    session.session_id,value['session_id'],wait_ms=100)
            assert value['exit_code']==0 and value['file_tracking']['file_count']==1
            assert target.read_bytes()==b'after\n'
            change=service.tool_executor.file_journal.get(value['change_id'])
            assert change.run_id==run.run_id and change.tool_call_id=='call-0001'
            await _call(service.tool_executor,run,'undo_change',{'change_set_id':value['change_id']},call_id='undo-command')
            assert target.read_bytes()==b'before\r\n'
        finally: await service.shutdown()
    asyncio.run(scenario())
