from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider, wait_for_success
from test_change_set_api import _client, _inspect, _file
from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall


def test_undo_delete_preserves_a_new_same_path_file(tmp_path):
    library = tmp_path / 'workspace'
    library.mkdir()
    project = library / 'documents'
    project.mkdir()
    notes = project / 'notes.md'
    notes.write_bytes(b'Original meeting notes.\n')
    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        file = _file(snapshot, 'notes.md')
        created = client.post(f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets", json={
            'base_revision': snapshot['project']['revision'], 'base_fingerprint': snapshot['project']['fingerprint'],
            'title': 'Remove superseded notes', 'summary': 'Test ordinary file deletion.', 'origin': 'manual',
            'changes': [{'operation': 'delete', 'file_id': file['file_id'], 'rationale': 'Remove old notes.'}],
        })
        assert created.status_code == 201, created.text
        cid = created.json()['change_set_id']
        applied = client.post(f'/api/v1/change-sets/{cid}/apply-direct', json={'expected_review_revision':created.json()['review_revision']})
        assert applied.status_code == 200, applied.text
        assert not notes.exists()
        replacement = b'The author wrote a NEW meeting note after the deletion.\n'
        notes.write_bytes(replacement)
        response = client.post(f'/api/v1/change-sets/{cid}/rollback', json={'expected_review_revision':applied.json()['review_revision']})
        assert response.status_code == 409, response.text
        assert notes.read_bytes() == replacement


@pytest.mark.parametrize(('name', 'encoding', 'bom'), [
    ('calc.py', 'utf-8', b''), ('settings.ini', 'utf-8', b''), ('Dockerfile', 'utf-8', b''),
    ('notes16.txt', 'utf-16-le', b'\xff\xfe'), ('notes16be.txt', 'utf-16-be', b'\xfe\xff'),
    ('notes32.txt', 'utf-32-le', b'\xff\xfe\x00\x00'), ('notes32be.txt', 'utf-32-be', b'\x00\x00\xfe\xff'),
])
def test_edit_and_undo_generic_text_preserve_original_encoding_and_newlines(tmp_path, name, encoding, bom):
    library = tmp_path / 'workspace'
    library.mkdir()
    project = library / 'documents'
    project.mkdir()
    path = project / name
    original = bom + 'title="记录"\r\nvalue=1\r\n'.encode(encoding)
    path.write_bytes(original)
    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        file = _file(snapshot, name)
        created = client.post(f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets", json={
            'base_revision':snapshot['project']['revision'], 'base_fingerprint':snapshot['project']['fingerprint'],
            'title':'Edit ordinary text', 'summary':'Preserve the file format.', 'origin':'manual',
            'changes':[{'operation':'update','file_id':file['file_id'],'after_content':'title="更新"\nvalue=2\n','rationale':'Update the value.'}],
        })
        assert created.status_code == 201, created.text
        cid = created.json()['change_set_id']
        applied = client.post(f'/api/v1/change-sets/{cid}/apply-direct',json={'expected_review_revision':created.json()['review_revision']})
        assert applied.status_code == 200, applied.text
        assert path.read_bytes() == bom + 'title="更新"\r\nvalue=2\r\n'.encode(encoding)
        undone = client.post(f'/api/v1/change-sets/{cid}/rollback',json={'expected_review_revision':applied.json()['review_revision']})
        assert undone.status_code == 200, undone.text
        assert path.read_bytes() == original


class CommandProvider(ApiProvider):
    def __init__(self, command, cwd, timeout=10):
        self.command, self.cwd, self.timeout = command, cwd, timeout
        self.calls = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.calls.append(copy.deepcopy(messages))
        if len(self.calls) == 1:
            yield ProviderToolCall(0, 'ordinary-command', 'computer_run_command', {
                'command': self.command, 'workdir': str(self.cwd), 'timeout_seconds': self.timeout,
            })
            yield ProviderCompleted('tool_calls', None)
        else:
            yield ProviderTextDelta('The command result has been checked.')
            yield ProviderCompleted('stop', None)


class EmptyFileProvider(ApiProvider):
    def __init__(self, path, content):
        self.path, self.content = path, content
        self.result = None

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        receipt = next((part for part in messages if part.get('role') == 'tool'), None)
        if receipt is None:
            yield ProviderToolCall(0, 'empty-file', 'write_file', {'path': str(self.path), 'content': self.content})
            yield ProviderCompleted('tool_calls', None)
        else:
            self.result = json.loads(receipt['content'])
            yield ProviderTextDelta('The file result is available.')
            yield ProviderCompleted('stop', None)


@pytest.mark.parametrize('content', ['', ' \n\t'])
def test_single_file_write_preserves_explicit_empty_or_whitespace_content(settings, tmp_path, content):
    path = tmp_path / 'placeholder.txt'
    provider = EmptyFileProvider(path, content)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = client.post('/api/v1/agent/sessions', json={'title':'Create a placeholder file'}).json()['session_id']
        started = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key':'empty-file'},
            json={'message':'Create the placeholder file with the specified content.', 'writer_mode':'author_review_change_proposal'})
        assert started.status_code == 202, started.text
        assert wait_for_success(client, started.json()['run']['run_id'])['status'] == 'succeeded'
        assert provider.result['status'] == 'succeeded', provider.result
        assert path.read_bytes() == content.encode('utf-8')


@pytest.mark.parametrize(('program', 'timeout', 'expected_status', 'expected_code'), [
    ("print('diagnostic output'); raise SystemExit(3)", 10, 'failed', 3),
    ("print('words like error do not change a zero exit code')", 10, 'succeeded', 0),
    ("import time; print('working', flush=True); time.sleep(10)", 1, 'failed', None),
])
def test_command_outcome_matches_process_and_is_returned_to_model(settings, tmp_path, program, timeout, expected_status, expected_code):
    command = f'"{sys.executable}" -c "{program}"'
    provider = CommandProvider(command, tmp_path, timeout)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = client.post('/api/v1/agent/sessions', json={'title':'Check a small script'}).json()['session_id']
        created = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key':'generic-command-result'},
            json={'message':'Run the ordinary script and check its result.', 'writer_mode':'author_review_change_proposal'})
        assert created.status_code == 202, created.text
        rid = created.json()['run']['run_id']
        assert wait_for_success(client, rid)['status'] == 'succeeded'
        items = client.get(f'/api/v1/agent/runs/{rid}/activities').json()['activities']
        result = next(item['payload'] for item in items if item['kind'] == 'tool_result')
        assert result['status'] == expected_status
        output = result['output']
        assert output['exit_code'] == expected_code if expected_code is not None else output['timed_out'] is True
        assert output['stdout']
        assert len(provider.calls) == 2
        returned = next(json.loads(message['content']) for message in reversed(provider.calls[-1]) if message.get('role') == 'tool')
        assert returned['status'] == expected_status
        assert returned['output']['stdout'] == output['stdout']
