"""Windows cmd /c must not silently discard everything after a newline."""
import os
import sys
import time

import pytest

from zhimeng_runtime.command_capture import capture_command
from zhimeng_runtime.command_sessions import CommandSessionManager

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows CMD transport')


def capture(command, folder):
    return capture_command(command, folder, timeout_seconds=10, stop_command=None, folder=folder/'logs',
                           limit=10000, prune=lambda _: None)


def test_multiline_executes_all_lines_and_returns_last_exit_code(tmp_path):
    result = capture('echo first\necho 中文结果> "result.txt"\necho last\nexit /b 17', tmp_path)
    assert result['exit_code'] == 17
    assert 'first' in result['stdout'] and 'last' in result['stdout']
    assert (tmp_path/'result.txt').read_text(encoding='utf-8').strip() == '中文结果'


def test_invalid_quoted_multiline_python_is_a_real_command_failure(tmp_path):
    result = capture(f'"{sys.executable}" -c "\nprint(\'should execute\')\n"', tmp_path)
    assert result['exit_code'] != 0
    assert result['stderr'].strip()


def test_multiline_persistent_command_and_script_cleanup(tmp_path, monkeypatch):
    import tempfile
    real_mkstemp = tempfile.mkstemp
    scripts = []
    def tracked(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        scripts.append(result[1])
        return result
    monkeypatch.setattr(tempfile, 'mkstemp', tracked)
    manager = CommandSessionManager(tmp_path/'sessions')
    try:
        result = manager.start(owner_session_id='chat-a', owner_run_id='run-a', command='echo first\necho second\nexit /b 19', cwd=tmp_path, yield_time_ms=1000)
        until = time.monotonic()+10
        while result['status']=='running' and time.monotonic()<until:
            result = manager.read('chat-a', result['session_id'], wait_ms=100)
        assert result['exit_code']==19
        assert 'first' in result['stdout'] and 'second' in result['stdout']
        assert scripts and all(not os.path.exists(path) for path in scripts)
    finally:
        manager.close()
