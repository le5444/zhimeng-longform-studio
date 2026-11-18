from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from zhimeng_runtime.provider_settings_service import ProviderSettingsService


def save_profile(service, provider_id):
    return service.save_profile(
        provider_id=provider_id, name=provider_id, base_url='https://provider.test/v1',
        model='test-model', api_key='test-secret', selected_models=None,
        reasoning_effort='auto', make_active=False,
    )


def test_concurrent_catalog_updates_keep_both_profiles(tmp_path, monkeypatch):
    service = ProviderSettingsService(tmp_path / 'providers.json')
    entered, release, second_started, second_finished = (threading.Event() for _ in range(4))
    original_write = service._write_file

    def delayed_write(payload):
        if any(item['provider_id'] == 'first' for item in payload['providers']) and not entered.is_set():
            entered.set()
            assert release.wait(5)
        original_write(payload)

    def second():
        second_started.set()
        try:
            return save_profile(service, 'second')
        finally:
            second_finished.set()

    monkeypatch.setattr(service, '_write_file', delayed_write)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(save_profile, service, 'first')
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
            assert not second_finished.wait(0.1)
        finally:
            release.set()
        first.result(5)
        other.result(5)
    saved = json.loads(service._path.read_text(encoding='utf-8'))
    assert {item['provider_id'] for item in saved['providers']} == {'first', 'second'}
    assert all(item['api_key'] == 'test-secret' for item in saved['providers'])


def test_failed_catalog_replace_preserves_previous_file_and_removes_temporary(tmp_path, monkeypatch):
    service = ProviderSettingsService(tmp_path / 'providers.json')
    save_profile(service, 'first')
    before = service._path.read_bytes()

    def locked(*_args):
        raise PermissionError('file occupied')

    monkeypatch.setattr('zhimeng_runtime.provider_settings_service.replace_with_retry', locked)
    with pytest.raises(PermissionError):
        save_profile(service, 'second')
    assert service._path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [service._path]
