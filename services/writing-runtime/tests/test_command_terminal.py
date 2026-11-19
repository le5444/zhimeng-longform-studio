"""Real Windows ConPTY behavior, independent of a browser or model."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from zhimeng_runtime.command_sessions import CommandSessionManager, CommandSessionError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY")


def until(manager, cid, predicate):
    deadline = time.monotonic()+15
    while True:
        value = manager.read('chat', cid)
        if predicate(value):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(.03)


def launch(manager, tmp_path, source):
    path = tmp_path/'interactive.py'
    path.write_text(source, encoding='utf-8')
    return manager.start(owner_session_id='chat', owner_run_id='run',
                         command=f'"{sys.executable}" -X utf8 -u "{path}"',
                         cwd=tmp_path, tty=True, yield_time_ms=100)


def test_real_terminal_unicode_resize_exit_and_durable_output(tmp_path):
    manager = CommandSessionManager(tmp_path/'logs')
    try:
        started = launch(manager, tmp_path,
            "import os,sys,json\nfrom pathlib import Path\n"
            "print('TTY',sys.stdin.isatty(),sys.stdout.isatty(),flush=True)\n"
            "value=input('姓名:')\nsize=os.get_terminal_size()\n"
            "Path('result.json').write_text(json.dumps({'value':value,'columns':size.columns,'rows':size.lines},ensure_ascii=False),encoding='utf-8')\n"
            "print('SAVED',value,flush=True)\nsys.exit(17)\n")
        cid = started['session_id']
        first = until(manager,cid,lambda v:'TTY True True' in v['stdout'] and '姓名:' in v['stdout'])
        assert first['transport']=='pty'
        with pytest.raises(CommandSessionError): manager.write('other',cid,text='wrong')
        manager.resize('chat',cid,rows=31,columns=99)
        manager.write('chat',cid,text='中文测试\n')
        done = until(manager,cid,lambda v:v['status']!='running')
        assert done['exit_code']==17
        assert 'SAVED' in done['stdout'] and '中文测试' in done['stdout']
        assert '\x1b[8;24;80t' in done['stdout'] and '\x1b[8;31;99t' in done['stdout']
        assert json.loads((tmp_path/'result.json').read_text(encoding='utf-8'))=={'value':'中文测试','columns':99,'rows':31}
        assert manager.summaries('chat')[0]['transport']=='pty'
        fresh = CommandSessionManager(tmp_path/'logs')
        try: assert fresh.read('chat',cid)['stdout']==done['stdout']
        finally: fresh.close()
    finally: manager.close()


def test_terminal_ctrl_c_and_stop_own_child_tree(tmp_path):
    manager = CommandSessionManager(tmp_path/'logs')
    try:
        started=launch(manager,tmp_path,
            "import time,sys\nprint('WAITING',flush=True)\n"
            "try:\n while True: time.sleep(.05)\n"
            "except KeyboardInterrupt:\n print('INTERRUPTED',flush=True)\n sys.exit(23)\n")
        cid=started['session_id']
        until(manager,cid,lambda v:'WAITING' in v['stdout'])
        manager.write('chat',cid,text='\x03')
        # CMD may ask whether to terminate the batch; answer this prompt.
        value=until(manager,cid,lambda v:'INTERRUPTED' in v['stdout'])
        manager.stop('chat',cid)
        assert manager.read('chat',cid)['status']!='running'
    finally: manager.close()


def test_terminal_stop_waits_for_descendant_and_masks_saved_output(tmp_path):
    import ctypes
    from ctypes import wintypes
    secret = 'sk-' + 'abcdefghijklmnopqrstuvwx012345'
    manager = CommandSessionManager(tmp_path/'logs')
    try:
        item = launch(manager,tmp_path,
            "import subprocess,sys\nfrom pathlib import Path\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "Path('child.pid').write_text(str(child.pid))\n"
            f"print({secret!r},flush=True)\nprint('CHILD_READY',flush=True)\ninput()\n")
        cid=item['session_id']
        ready=until(manager,cid,lambda v:'CHILD_READY' in v['stdout'])
        assert secret not in ready['stdout']
        assert secret not in (Path(ready['stdout_file']).read_text(encoding='utf-8'))
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x100000,False,int((tmp_path/'child.pid').read_text()))
        assert handle
        try:
            assert api.WaitForSingleObject(handle,0)==258
            manager.stop('chat',cid)
            assert api.WaitForSingleObject(handle,3000)==0
        finally: api.CloseHandle(handle)
    finally: manager.close()


def test_terminal_input_resize_api_and_session_isolation(settings,tmp_path):
    from fastapi.testclient import TestClient
    from test_agent_api import ApiProvider
    from test_agent_queue import session
    from test_command_api import manager_of, read_until
    from zhimeng_runtime.app import create_app
    with TestClient(create_app(settings,agent_provider=ApiProvider())) as client:
        sid=session(client)
        manager=manager_of(client)
        script=tmp_path/'api.py'
        script.write_text("print('READY',flush=True)\nprint(input(),flush=True)\n",encoding='utf-8')
        item=manager.start(owner_session_id=sid,owner_run_id='api-test',command=f'"{sys.executable}" -X utf8 -u "{script}"',cwd=tmp_path,tty=True,yield_time_ms=100)
        url=f'/api/v1/agent/sessions/{sid}/commands/{item["session_id"]}'
        read_until(client,url,lambda v:'READY' in v['stdout']['text'])
        sized=client.post(url+'/resize',json={'rows':30,'columns':100})
        assert sized.status_code==200 and sized.json()['terminal_columns']==100
        other=session(client)
        denied=client.post(url.replace(sid,other)+'/input',json={'input':'wrong'})
        assert denied.status_code==404
        assert client.post(url+'/input',json={'input':'中文\r'}).status_code==200
        done=read_until(client,url,lambda v:v['status']=='exited')
        assert '中文' in done['stdout']['text'] and done['transport']=='pty'
        assert client.post(url+'/input',json={'input':'late'}).status_code==422


def test_terminal_mask_understands_split_control_sequences():
    from zhimeng_runtime.command_sessions import _TerminalMask
    secret='sk-'+'abcdefghijklmnopqrstuvwx012345'
    for source in ['\x1b8'+secret+'\r\n', 'sk-\x1b[31m'+secret[3:]+'\x1b[0m\n', 'sk-\x1b[3;1H'+secret[3:]+'\n']:
        for split in range(len(source)+1):
            masker=_TerminalMask()
            output=masker.feed(source[:split])+masker.feed(source[split:],final=True)
            assert secret not in output
            assert secret[3:] not in output
            assert masker.masked_count>=1


def test_hard_runtime_exit_stops_terminal_and_recovers_log_without_replay(tmp_path):
    import subprocess
    import textwrap
    owner=tmp_path/'owner.py'
    child=tmp_path/'child.py'
    child.write_text("import time,os\nfrom pathlib import Path\nPath('terminal.pid').write_text(str(os.getpid()))\nprint('DURABLE READY',flush=True)\ntime.sleep(60)\n",encoding='utf-8')
    owner.write_text(textwrap.dedent('''
        import os,sys,time
        from pathlib import Path
        from zhimeng_runtime.command_sessions import CommandSessionManager
        root=Path(sys.argv[1]);m=CommandSessionManager(root/'logs')
        r=m.start(owner_session_id='chat',owner_run_id='run',command=f'"{sys.executable}" -u "{root / "child.py"}"',cwd=root,tty=True,yield_time_ms=100)
        deadline=time.monotonic()+10
        while 'DURABLE READY' not in m.read('chat',r['session_id'])['stdout']:
            assert time.monotonic()<deadline
            time.sleep(.03)
        (root/'command.id').write_text(r['session_id'])
        os._exit(23)
    '''),encoding='utf-8')
    result=subprocess.run([sys.executable,str(owner),str(tmp_path)],timeout=30,capture_output=True)
    assert result.returncode==23,result.stderr
    manager=CommandSessionManager(tmp_path/'logs')
    try:
        restored=manager.read('chat',(tmp_path/'command.id').read_text())
        assert restored['status']=='interrupted' and 'DURABLE READY' in restored['stdout']
        assert not restored['completion_eligible']
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x100000,False,int((tmp_path/'terminal.pid').read_text()))
        if handle:
            try: assert api.WaitForSingleObject(handle,3000)==0
            finally: api.CloseHandle(handle)
    finally: manager.close()
