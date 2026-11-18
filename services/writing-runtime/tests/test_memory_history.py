from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.memory_files import append_user_memory, read_user_memory
from zhimeng_runtime.user_memory_api import create_user_memory_router


def test_memory_versions_cover_manual_auto_and_restore(tmp_path: Path):
    app = FastAPI()
    app.include_router(create_user_memory_router(tmp_path))
    with TestClient(app) as client:
        initial = client.get('/api/v1/memory/user').json()
        saved = client.put('/api/v1/memory/user', json={'content': 'Keep answers concise\n', 'expected_sha256': initial['sha256']})
        assert saved.status_code == 200
        assert saved.json()['updated_at']
        append_user_memory(tmp_path, 'Prefer Python')
        history = client.get('/api/v1/memory/user/history?limit=1').json()
        assert len(history['items']) == 1
        assert history['next_offset'] == 1
        previous = client.get('/api/v1/memory/user/history/' + history['items'][0]['version']).json()
        assert previous['content'] == 'Keep answers concise\n'
        current = client.get('/api/v1/memory/user').json()
        restored = client.put('/api/v1/memory/user', json={'content': previous['content'], 'expected_sha256': current['sha256']})
        assert restored.status_code == 200
        assert read_user_memory(tmp_path) == previous['content']
        undo = client.get('/api/v1/memory/user/history').json()['items'][0]
        assert 'Prefer Python' in client.get('/api/v1/memory/user/history/' + undo['version']).json()['content']
        client.put('/api/v1/memory/user', json={'content': previous['content'], 'expected_sha256': restored.json()['sha256']})
        assert len(client.get('/api/v1/memory/user/history').json()['items']) == 3


def test_history_api_rejects_invalid_pagination(tmp_path: Path):
    app = FastAPI()
    app.include_router(create_user_memory_router(tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/v1/memory/user/history?offset=-1').status_code == 422
        assert client.get('/api/v1/memory/user/history?limit=0').status_code == 422


def test_history_write_failure_preserves_current_memory(tmp_path: Path, monkeypatch):
    from zhimeng_runtime import memory_files
    memory_files.write_user_memory(tmp_path, 'Original')
    original_write = memory_files._write_atomic

    def fail_archive(path, content):
        if path.parent.name == 'memory-history':
            raise OSError('Disk write failed')
        original_write(path, content)

    monkeypatch.setattr(memory_files, '_write_atomic', fail_archive)
    import pytest
    with pytest.raises(OSError):
        memory_files.write_user_memory(tmp_path, 'Replacement')
    assert memory_files.read_user_memory(tmp_path) == 'Original'


def test_context_shortcut_preserves_provider_configuration(tmp_path: Path):
    from zhimeng_runtime.provider_settings_api import create_provider_settings_router
    from zhimeng_runtime.provider_settings_service import ProviderSettingsService

    service = ProviderSettingsService(tmp_path / 'providers.json')
    service.save_profile(provider_id='local', name='Local', base_url='https://example.test/v1',
                         model='test', api_key='test-key', selected_models=['test'], reasoning_effort='high',
                         wire_format='responses', make_active=True)
    app = FastAPI()
    app.include_router(create_provider_settings_router(service))
    with TestClient(app) as client:
        response = client.put('/api/v1/settings/provider', json={'context_window': '1m'})
        assert response.status_code == 200
        assert response.json()['context_window'] == '1m'
        profile = service.catalog_view()['providers'][0]
        assert profile['wire_format'] == 'responses'
        assert profile['model'] == 'test'
        assert profile['api_key_set'] is True
        assert profile['reasoning_effort'] == 'high'
        assert client.put('/api/v1/settings/provider', json={'context_window': 'invalid'}).status_code == 422
