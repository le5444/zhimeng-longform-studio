import codecs
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.memory_files import (
    MAX_USER_MEMORY_BYTES, MEMORY_SETTINGS_FILE_NAME, USER_MEMORY_FILE_NAME, append_user_memory,
    memory_enabled, read_user_memory, set_memory_enabled, write_user_memory,
)
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.user_memory_api import create_user_memory_router


@pytest.mark.parametrize('operation', ['write', 'append'])
@pytest.mark.parametrize('failure', ['permission', 'encoding'])
def test_unreadable_memory_cannot_be_replaced(tmp_path, monkeypatch, operation, failure):
    path = tmp_path / USER_MEMORY_FILE_NAME
    original = b'Original preference\n' if failure == 'permission' else b'Original\xff\n'
    path.write_bytes(original)
    read_bytes = Path.read_bytes

    def read(candidate):
        if candidate == path and failure == 'permission':
            raise PermissionError('temporarily locked')
        return read_bytes(candidate)

    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises((OSError, UnicodeError)):
        (write_user_memory if operation == 'write' else append_user_memory)(tmp_path, 'New preference')
    assert read_bytes(path) == original
    assert not list((tmp_path / 'memory-history').glob('*.json'))


def test_large_existing_memory_is_archived_in_full(tmp_path):
    original = 'x' * (MAX_USER_MEMORY_BYTES + 20) + '\nLast preference\n'
    path = tmp_path / USER_MEMORY_FILE_NAME
    path.write_bytes(original.encode())
    assert len(read_user_memory(tmp_path)) == MAX_USER_MEMORY_BYTES
    assert read_user_memory(tmp_path, strict=True) == original
    with pytest.raises(ValueError):
        append_user_memory(tmp_path, 'New preference')
    assert path.read_bytes() == original.encode()
    write_user_memory(tmp_path, 'Shorter replacement\n')
    history = list((tmp_path / 'memory-history').glob('*.json'))
    assert len(history) == 1
    assert json.loads(history[0].read_text(encoding='utf-8'))['content'] == original


def test_append_preserves_utf8_bom_and_crlf(tmp_path):
    path = tmp_path / USER_MEMORY_FILE_NAME
    path.write_bytes(codecs.BOM_UTF8 + b'Original\r\n')
    append_user_memory(tmp_path, 'New preference')
    assert path.read_bytes() == codecs.BOM_UTF8 + b'Original\r\nNew preference\r\n'


def test_api_rejects_unreadable_memory_instead_of_exposing_empty_content(tmp_path, monkeypatch):
    path = tmp_path / USER_MEMORY_FILE_NAME
    path.write_bytes(b'Original\n')
    read_bytes = Path.read_bytes

    def read(candidate):
        if candidate == path:
            raise PermissionError('temporarily locked')
        return read_bytes(candidate)

    monkeypatch.setattr(Path, 'read_bytes', read)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_user_memory_router(tmp_path))
    with TestClient(app) as client:
        for response in (client.get('/api/v1/memory/user'), client.put('/api/v1/memory/user', json={'content': 'Replacement'})):
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'user_memory_unreadable'
    assert read_bytes(path) == b'Original\n'


def test_invalid_utf8_memory_is_not_a_trustworthy_agent_snapshot(tmp_path):
    path = tmp_path / USER_MEMORY_FILE_NAME
    original = b'Original\xff preference\n'
    path.write_bytes(original)
    service = AgentService.__new__(AgentService)
    service.store = SimpleNamespace(database=SimpleNamespace(path=tmp_path / 'runtime.sqlite3'))
    assert service._user_memory_snapshot() == ('', False)
    assert service._user_memory_provider_message() is None
    assert path.read_bytes() == original


@pytest.mark.parametrize('settings', ['{broken', '[]', '{}', '{"enabled":"false"}', '{"enabled":null}'])
def test_invalid_settings_pause_memory_and_api_allows_explicit_recovery(tmp_path, settings):
    memory = tmp_path / USER_MEMORY_FILE_NAME
    memory.write_bytes(b'Original\r\n')
    set_memory_enabled(tmp_path, False)
    (tmp_path / MEMORY_SETTINGS_FILE_NAME).write_text(settings, encoding='utf-8')
    assert memory_enabled(tmp_path) is False
    with pytest.raises(ValueError):
        memory_enabled(tmp_path, strict=True)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_user_memory_router(tmp_path))
    with TestClient(app) as client:
        view = client.get('/api/v1/memory/user').json()
        assert view['enabled'] is False
        assert view['settings_error']
        assert view['content'] == 'Original\r\n'
        restored = client.put('/api/v1/memory/user/settings', json={'enabled': True})
        assert restored.status_code == 200
        assert restored.json()['enabled'] is True
        assert restored.json()['settings_error'] is None
    assert memory.read_bytes() == b'Original\r\n'
    assert not list((tmp_path / 'memory-history').glob('*.json'))


def test_unreadable_settings_do_not_enable_agent_memory(tmp_path, monkeypatch):
    set_memory_enabled(tmp_path, False)
    path = tmp_path / MEMORY_SETTINGS_FILE_NAME
    original_read = Path.read_text

    def read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError('temporarily locked settings')
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    service = AgentService.__new__(AgentService)
    service.store = SimpleNamespace(database=SimpleNamespace(path=tmp_path / 'runtime.sqlite3'))
    assert not memory_enabled(tmp_path)
    assert not service._memory_enabled()
    assert service._user_memory_snapshot() == ('', False)
