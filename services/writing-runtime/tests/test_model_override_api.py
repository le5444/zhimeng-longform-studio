"""Single-model editing preserves other connections and secret values."""
from concurrent.futures import ThreadPoolExecutor

from test_provider_probe_drafts import _client
from test_provider_profile_extensions import _save_extended_profile
from zhimeng_runtime.provider_settings_service import ProviderSettingsService, resolve_model_connection

URL = '/api/v1/settings/providers/relay/model-overrides/fast-model'


def test_partial_update_readback_inheritance_and_reset(tmp_path):
    path = tmp_path / 'provider-settings.json'
    service = ProviderSettingsService(path)
    _save_extended_profile(service)
    before, _ = service.resolve_profile('relay')
    with _client(service) as client:
        response = client.patch(URL, json={'wire_format': 'chat', 'base_url': 'https://updated.example/v1'})
        assert response.status_code == 200, response.text
        view = response.json()['providers'][0]['model_overrides']['fast-model']
        assert view['base_url'] == 'https://updated.example/v1'
        assert view['wire_format'] == 'chat'
        assert view['reasoning_effort'] == 'high'
        assert view['max_output_tokens'] == 8192
        assert view['api_key_set'] is True
        assert 'sk-fast-key' not in response.text and 'sk-channel-key' not in response.text
        assert '"yes"' not in response.text
        response = client.patch(URL, json={'base_url': None, 'wire_format': None, 'api_key': '   ',
            'reasoning_effort': None, 'max_output_tokens': None, 'remove_header_names': ['x-accel']})
        assert response.status_code == 200
        after, _ = ProviderSettingsService(path).resolve_profile('relay')
        connection = resolve_model_connection(after, 'fast-model')
        assert connection.base_url == before.base_url
        assert connection.api_key == 'sk-fast-key'
        assert after.model_overrides['fast-model'].extra_headers == {}
        response = client.patch(URL, json={'inherit_api_key': True})
        assert response.status_code == 200
        assert response.json()['providers'][0]['model_overrides'] == {}
        assert client.patch(URL, json={'api_key': 'sk-new-secret', 'extra_headers': {'X-One': 'private-header'}}).status_code == 200
        assert client.patch(URL, json={'reset': True}).status_code == 200
    after, _ = service.resolve_profile('relay')
    assert after.model_overrides == {}
    assert after.connection() == before.connection()


def test_single_model_patch_preserves_deselected_overrides_and_parallel_updates(tmp_path):
    service = ProviderSettingsService(tmp_path / 'provider-settings.json')
    _save_extended_profile(service)
    service.save_profile(provider_id='relay', name='Same service', base_url=None, model='main-model',
        api_key=None, selected_models=['main-model'], reasoning_effort='auto', make_active=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: service.patch_model_override('relay', pair[0], pair[1]), [
            ('fast-model', {'reasoning_effort': 'low'}), ('main-model', {'wire_format': 'responses'}),
        ]))
    assert len(results) == 2
    profile, _ = service.resolve_profile('relay')
    assert profile.model_overrides['fast-model'].reasoning_effort == 'low'
    assert profile.model_overrides['fast-model'].api_key == 'sk-fast-key'
    assert profile.model_overrides['main-model'].wire_format == 'responses'
    assert profile.selected_models == ('main-model',)


def test_invalid_patch_does_not_mutate_config(tmp_path):
    path = tmp_path / 'provider-settings.json'
    service = ProviderSettingsService(path)
    _save_extended_profile(service)
    before = path.read_bytes()
    with _client(service) as client:
        assert client.patch(URL, json={'max_output_tokens': 0}).status_code == 422
        assert client.patch(URL, json={'wire_format': 'invented'}).status_code == 422
        assert client.patch(URL, json={'base_url': 'not a URL'}).status_code == 400
        assert client.patch(URL.replace('relay', 'missing'), json={}).status_code == 404
        assert client.patch(URL.replace('fast-model', 'unknown'), json={}).status_code == 400
    assert path.read_bytes() == before
