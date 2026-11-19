import json
from pathlib import Path

import pytest

from test_command_sessions import start, until
from zhimeng_runtime.command_sessions import CommandSessionError, CommandSessionManager


@pytest.mark.parametrize('failure', ['missing', 'permission'])
def test_bad_history_does_not_block_other_commands_or_completion(tmp_path, monkeypatch, failure):
    root = tmp_path / 'commands'
    for identifier in ('damaged', 'healthy'):
        folder = root / identifier
        folder.mkdir(parents=True)
        metadata = {
            'schema': 'zhimeng.command-session.v1', 'session_id': identifier,
            'owner_session_id': 'chat-a', 'owner_run_id': 'run-a',
            'command': 'previous command', 'cwd': str(tmp_path), 'created_at': 1.0,
            'ended_at': 2.0, 'status': 'exited', 'exit_code': 0,
            'completion_version': 1, 'completion_ready': True, 'completion_eligible': True,
        }
        (folder / 'session.json').write_text(json.dumps(metadata), encoding='utf-8')
        (folder / 'stdout.log').write_bytes(b'previous output')
        (folder / 'stderr.log').write_bytes(b'')
    damaged_log = root / 'damaged' / 'stdout.log'
    if failure == 'missing':
        damaged_log.unlink()
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == damaged_log and failure == 'permission':
            raise PermissionError('locked log')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded_open)
    completions = []
    manager = CommandSessionManager(root, on_completion=completions.append)
    try:
        summaries = {item['command_session_id']: item for item in manager.summaries('chat-a')}
        assert summaries['damaged']['stdout_bytes'] is None
        assert 'stdout.log' in summaries['damaged']['output_error']
        assert summaries['healthy']['stdout_bytes'] == len(b'previous output')
        assert len(manager.list('chat-a')) == 2
        damaged = next(item for item in completions if item['session_id'] == 'damaged')
        assert damaged['completion_eligible'] is False
        assert 'stdout' not in damaged['cursor']
        assert next(item for item in completions if item['session_id'] == 'healthy')['completion_eligible'] is True
        manager.reconcile_completions()
        assert len(completions) == 2
        for read in (manager.read, manager.read_page):
            with pytest.raises(CommandSessionError) as error:
                read('chat-a', 'damaged')
            assert error.value.code == 'command_output_unavailable'
        new = start(manager, tmp_path, "print('new command still works')\n")
        result = until(manager, new, lambda value: value['status'] != 'running')
        assert result['exit_code'] == 0
        assert 'new command still works' in result['stdout']
        monkeypatch.setattr(Path, 'open', original_open)
        damaged_log.write_bytes(b'recovered output')
        assert manager.read_page('chat-a', 'damaged')['stdout']['text'] == 'recovered output'
        assert not next(item for item in manager.summaries('chat-a') if item['command_session_id'] == 'damaged')['output_error']
    finally:
        manager.close()
