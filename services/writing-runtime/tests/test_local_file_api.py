from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime import local_file_api as api
from zhimeng_runtime.errors import install_exception_handlers


@pytest.fixture
def client():
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(api.create_local_file_router())
    with TestClient(app) as value:
        yield value


def test_local_preview_keeps_encoding_bytes_and_literal_fragment_filename(client, tmp_path):
    file = tmp_path / 'report #L4.md'
    raw = '# 核对\r\n总额：49.50\r\n'.encode('utf-16')
    file.write_bytes(raw)
    other = tmp_path / 'report '
    other.write_text('不能误开', encoding='utf-8')
    response = client.get('/api/v1/fs/file', params=[('path', str(file)), ('path', str(other))])
    assert response.status_code == 200
    result = response.json()
    assert result['content'] == '# 核对\r\n总额：49.50\r\n'
    assert result['path'] == str(file)
    assert result['encoding'] == 'utf-16-le'
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert file.read_bytes() == raw


def test_missing_location_path_falls_back_but_io_failure_does_not(client, tmp_path, monkeypatch):
    file = tmp_path / 'report.csv'
    file.write_bytes(b'amount\n49.50\n')
    response = client.get('/api/v1/fs/file', params=[('path', str(file) + '#L2'), ('path', str(file))])
    assert response.json()['path'] == str(file)
    assert response.json()['content'].endswith('49.50\n')
    original = Path.stat
    def inaccessible(path, *args, **kwargs):
        if str(path).endswith('#L2'):
            raise PermissionError('unreadable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', inaccessible)
    assert client.get('/api/v1/fs/file', params=[('path', str(file) + '#L2'), ('path', str(file))]).status_code == 403


def test_office_is_extracted_from_same_bytes_and_binary_is_not_garbled(client, tmp_path):
    file = tmp_path / '资料.docx'
    with zipfile.ZipFile(file, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>核对49.50</w:t></w:r></w:p></w:body></w:document>')
    raw = file.read_bytes()
    result = client.get('/api/v1/fs/file', params={'path': str(file)}).json()
    assert result['kind'] == 'office' and result['format'] == 'docx'
    assert result['content'] == '核对49.50' and result['warnings']
    assert result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert file.read_bytes() == raw
    image = tmp_path / 'image.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00')
    result = client.get('/api/v1/fs/file', params={'path': str(image)}).json()
    assert result['kind'] == 'unavailable' and result['content'] is None
    assert result['sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_missing_directory_relative_and_large_file_states(client, tmp_path, monkeypatch):
    assert client.get('/api/v1/fs/file', params={'path': str(tmp_path / 'missing.md')}).status_code == 404
    assert client.get('/api/v1/fs/file', params={'path': 'notes.txt'}).status_code == 422
    assert client.get('/api/v1/fs/file').status_code == 422
    assert client.get('/api/v1/fs/file', params={'path': str(tmp_path)}).json()['kind'] == 'directory'
    file = tmp_path / 'large.txt'
    file.write_text('hello', encoding='utf-8')
    monkeypatch.setattr(api, 'MAX_TEXT_FILE_BYTES', 2)
    result = client.get('/api/v1/fs/file', params={'path': str(file)}).json()
    assert result['kind'] == 'unavailable' and result['sha256'] is None and result['warnings']


def test_changed_during_read_returns_conflict(client, tmp_path, monkeypatch):
    file = tmp_path / 'changing.txt'
    file.write_text('before', encoding='utf-8')
    def changed(*_args):
        raise api.ProjectChangedDuringReadError('changed')
    monkeypatch.setattr(api, '_read_stable_file_limited', changed)
    assert client.get('/api/v1/fs/file', params={'path': str(file)}).status_code == 409


def test_truncation_is_explicit_and_hash_describes_whole_source(client, tmp_path):
    file = tmp_path / 'long.txt'
    raw = ('字' * 512_001).encode('utf-8')
    file.write_bytes(raw)
    result = client.get('/api/v1/fs/file', params={'path': str(file)}).json()
    assert len(result['content']) == 512_000 and result['truncated']
    assert result['sha256'] == hashlib.sha256(raw).hexdigest() and result['size_bytes'] == len(raw)


def test_reveal_uses_containing_folder_even_after_undo(client, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr('zhimeng_runtime.project_api.reveal_folder', opened.append)
    result = client.post('/api/v1/fs/reveal', json={'path': str(tmp_path / 'undone.md')})
    assert result.status_code == 200 and opened == [tmp_path.resolve()]
    assert client.post('/api/v1/fs/reveal', json={'path': 'relative.md'}).status_code == 422


@pytest.mark.parametrize(('name', 'model'), [
    ('local-file-preview.schema.json', api.LocalFilePreview),
    ('local-file-reveal-request.schema.json', api.RevealLocalFileRequest),
    ('local-file-reveal-response.schema.json', api.RevealLocalFileResponse),
])
def test_local_file_contract(name, model):
    stored = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts' / name).read_bytes())
    assert stored.pop('$schema') == 'https://json-schema.org/draft/2020-12/schema'
    assert stored.pop('$id') == f'https://zhimeng.local/contracts/{name}'
    assert stored == model.model_json_schema()
