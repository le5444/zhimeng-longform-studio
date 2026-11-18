"""Draft authentication must behave like the saved connection, without real upstream calls."""
from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.provider_settings_api import create_provider_settings_router
from zhimeng_runtime.provider_settings_service import ProviderSettingsService, resolve_model_connection
from test_provider_routes_20261005 import (
    BASE, KEY, _chat_text, _chat_tool_call, _has_tool_result, _mock_transport,
    _responses_text, _responses_tool_call,
)


def _save(service, *, headers=None, auth="bearer", key=KEY):
    return service.save_profile(
        provider_id="relay", name="Local probe", base_url=BASE, api_key=key,
        model="plain-model", selected_models=["plain-model"], reasoning_effort="auto",
        wire_format="auto", auth_type=auth, extra_headers=headers, make_active=True,
    )


def _client(service):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_provider_settings_router(service))
    return TestClient(app)


def test_service_draft_overrides_saved_model_connection_for_all_probe_paths(tmp_path, monkeypatch):
    """服务表单的明确草稿就是测试对象；已保存的模型例外不能暗中改回去。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay", name="Local probe", base_url=BASE, api_key=KEY,
        model="plain-model", selected_models=["plain-model"], reasoning_effort="auto",
        wire_format="responses", model_overrides={"plain-model": {
            "base_url": "https://old-model.example/v1", "api_key": "old-model-key",
            "wire_format": "responses",
        }}, make_active=True,
    )
    seen = []

    async def handler(request):
        seen.append((request.url.path, request.headers.get("authorization"), request.headers.get("x-draft")))
        return _chat_tool_call() if json.loads(request.content).get("tools") and not _has_tool_result(json.loads(request.content)) else _chat_text("draft")

    _mock_transport(monkeypatch, handler)
    for action in ("text", "tools"):
        if action == "text":
            result = service.test_model(
                provider_id="relay", base_url=BASE, api_key="draft-key", model="plain-model",
                prompt="Describe a task.", wire_format="chat", extra_headers={"X-Draft": "service"},
                test_scope="service_draft",
            )
            assert result["wire_format"] == "chat"
        else:
            checked = service.test_models(
                provider_id="relay", base_url=BASE, api_key="draft-key", models=["plain-model"],
                wire_format="chat", extra_headers={"X-Draft": "service"}, test_scope="service_draft",
            )
            assert checked["results"][0]["tool_roundtrip"] == "ok"
    assert seen
    assert all(path.endswith("/chat/completions") for path, _key, _header in seen)
    assert all(key == "Bearer draft-key" for _path, key, _header in seen)
    assert all(header == "service" for _path, _key, header in seen)

    # 正式保存后的连接仍保留模型例外，草稿测试没有修改用户配置。
    profile, _ = service.resolve_profile("relay")
    effective = resolve_model_connection(profile, "plain-model")
    assert effective.base_url == "https://old-model.example/v1"
    assert effective.api_key == "old-model-key"
    assert effective.wire_format == "responses"


@pytest.mark.parametrize("stage", ["models", "text", "tools"])
def test_blank_saved_header_values_are_reused_by_all_probe_paths(tmp_path, monkeypatch, stage):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "saved-account", "X-Tag": "old-tag"})
    seen = []

    async def handler(request):
        seen.append(dict(request.headers))
        body = json.loads(request.content)
        if not body.get("tools") or _has_tool_result(body):
            return _chat_text("The tool result is available.")
        return _chat_tool_call()

    def models(request, **kwargs):
        seen.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(b'{"data":[{"id":"plain-model"}]}')

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr("urllib.request.urlopen", models)
    draft = {"provider_id": "relay", "base_url": BASE, "api_key": None,
             "extra_headers": {"x-account": "", "X-Tag": "new-tag"}}
    if stage == "models":
        assert service.list_models(**draft) == ["plain-model"]
    elif stage == "text":
        assert service.test_model(**draft, model="plain-model", prompt="Describe a file task.", wire_format="chat")["ok"]
    else:
        result = service.test_models(**draft, models=["plain-model"], wire_format="chat")
        assert result["results"][0]["tool_roundtrip"] == "ok"
    assert seen and all(h.get("x-account") == "saved-account" for h in seen)
    assert all(h.get("x-tag") == "new-tag" for h in seen)
    before, _ = service.resolve_profile("relay")
    assert before.extra_headers == {"X-Account": "saved-account", "X-Tag": "old-tag"}
    _save(service, headers=draft["extra_headers"])
    saved, _ = service.resolve_profile("relay")
    assert {k.lower(): v for k, v in saved.extra_headers.items()} == {"x-account": "saved-account", "x-tag": "new-tag"}


@pytest.mark.parametrize("change", ["auth", "header"])
def test_verified_protocol_does_not_follow_changed_authentication(tmp_path, monkeypatch, change):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "account-a"})

    async def handler(request):
        if not request.url.path.endswith("responses"):
            return httpx.Response(404)
        body = json.loads(request.content)
        return _responses_tool_call() if body.get("tools") and not _has_tool_result(body) else _responses_text()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")
    assert result["results"][0]["remembered"] is True
    profile, _ = service.resolve_profile("relay")
    assert service._verified_routes_view(profile)["plain-model"]["wire_format"] == "responses"
    changed = replace(profile, auth_type="x-api-key") if change == "auth" else replace(profile, extra_headers={"X-Account": "account-b"})
    assert service._verified_routes_view(changed) == {}
    assert resolve_model_connection(changed, "plain-model").wire_format == "chat"
    # A header name's casing does not define another account.
    assert service._verified_routes_view(replace(profile, extra_headers={"x-account": "account-a"}))
    assert "account-a" not in service._routes_path().read_text(encoding="utf8")
    with _client(service) as client:
        response = client.request("DELETE", "/api/v1/settings/provider/routes", json={"provider_id": "relay", "base_url": BASE})
    assert response.status_code == 200 and response.json()["cleared"] == 1


def test_reply_test_preserves_upstream_http_status(tmp_path, monkeypatch):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service)

    async def handler(request):
        return httpx.Response(403, json={"error": {"message": "Account has no permission"}})

    _mock_transport(monkeypatch, handler)
    with _client(service) as client:
        response = client.post("/api/v1/settings/provider/test", json={"provider_id": "relay", "base_url": BASE, "model": "plain-model", "wire_format": "chat", "prompt": "Describe a file task."})
    assert response.status_code == 403
    assert KEY not in response.text


@pytest.mark.parametrize('wire', ['chat', 'responses', 'anthropic_messages'])
@pytest.mark.parametrize("shape", ["nested", "string", "top-level"])
def test_permission_error_preserves_structured_reason_without_guessing_key_expiry(tmp_path, monkeypatch, wire, shape):
    service = ProviderSettingsService(tmp_path / 'provider-settings.json')
    _save(service)
    message = 'Identity verification is required. Complete verification at https://account.example/security?action=realname.'

    async def handler(request):
        reason = message + ' Key: ' + KEY
        payload = {'request_id': 'x' * 300}
        payload.update({'error': {'message': reason}} if shape == 'nested' else
                       {'error': reason} if shape == 'string' else {'message': reason})
        return httpx.Response(403, json=payload)

    _mock_transport(monkeypatch, handler)
    with _client(service) as client:
        response = client.post('/api/v1/settings/provider/test', json={'provider_id': 'relay', 'base_url': BASE,
            'model': 'plain-model', 'wire_format': wire, 'prompt': 'Say hello.'})
    assert response.status_code == 403
    reason = response.json()['error']['message']
    assert message in reason
    assert '过期' not in reason
    assert 'request_id' not in reason
    assert KEY not in reason


def test_empty_reply_is_an_error_even_when_upstream_http_succeeded(tmp_path, monkeypatch):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service)

    async def handler(request):
        return _chat_text("")

    _mock_transport(monkeypatch, handler)
    with _client(service) as client:
        response = client.post("/api/v1/settings/provider/test", json={"provider_id": "relay", "base_url": BASE, "model": "plain-model", "wire_format": "chat", "prompt": "Describe a file task."})
    assert response.status_code == 502


@pytest.mark.parametrize("auth", ["custom", "none"])
def test_keyless_service_can_be_configured_probed_and_retested(tmp_path, monkeypatch, auth):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, key=None, auth=auth, headers={"X-Session": "local-session"} if auth == "custom" else {})
    assert service.public_view()["configured"] is True
    assert service.catalog_view()["providers"][0]["configured"] is True

    async def handler(request):
        assert "authorization" not in request.headers
        if auth == "custom":
            assert request.headers["x-session"] == "local-session"
        body = json.loads(request.content)
        return _chat_tool_call() if body.get("tools") and not _has_tool_result(body) else _chat_text()

    _mock_transport(monkeypatch, handler)
    with _client(service) as client:
        result = client.post("/api/v1/settings/provider/models/liveness", json={"provider_id": "relay", "base_url": BASE, "models": ["plain-model"], "wire_format": "auto"})
        assert result.status_code == 200
        assert result.json()["results"][0]["tool_roundtrip"] == "ok"
        cleared = client.request("DELETE", "/api/v1/settings/provider/routes", json={"provider_id": "relay", "base_url": BASE})
        assert cleared.status_code == 200 and cleared.json()["cleared"] == 1


def test_default_bearer_route_records_keep_working(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service)
    service._record_routes(base_url=BASE, api_key=KEY, records={"plain-model": {"wire_format": "responses", "tools": True}})
    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "responses"


def test_clearing_model_override_routes_needs_no_service_key(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, key=None)
    service.save_profile(provider_id="relay", name="Local probe", base_url=None, model=None,
                         api_key=None, selected_models=None, reasoning_effort=None, make_active=False,
                         model_overrides={"plain-model": {"api_key": KEY}})
    service._record_routes(base_url=BASE, api_key=KEY, records={"plain-model": {"wire_format": "responses", "tools": True}})
    with _client(service) as client:
        response = client.request("DELETE", "/api/v1/settings/provider/routes", json={"provider_id": "relay", "base_url": BASE})
    assert response.status_code == 200 and response.json()["cleared"] == 1


def test_clearing_routes_reports_invalid_address_as_input_error(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service)
    with _client(service) as client:
        response = client.request("DELETE", "/api/v1/settings/provider/routes", json={"provider_id": "relay", "base_url": "not-a-url"})
    assert response.status_code == 400


def test_old_duplicate_header_casing_is_collapsed_when_testing_and_saving(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "account-a"})
    data = json.loads(service._path.read_text(encoding="utf8"))
    data["providers"][0]["extra_headers"]["x-account"] = "account-b"
    service._path.write_text(json.dumps(data), encoding="utf8")
    _save(service, headers={"X-ACCOUNT": "account-c"})
    profile, _ = service.resolve_profile("relay")
    assert [(k, v) for k, v in profile.extra_headers.items() if k.lower() == "x-account"] == [("X-ACCOUNT", "account-c")]


@pytest.mark.parametrize("stage", ["models", "text", "tools"])
def test_removed_saved_headers_are_absent_in_draft_probes_without_saving(tmp_path, monkeypatch, stage):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "saved-account", "X-Obsolete": "old-secret"})
    seen = []

    async def handler(request):
        seen.append(dict(request.headers))
        body = json.loads(request.content)
        return _chat_tool_call() if body.get("tools") and not _has_tool_result(body) else _chat_text("The tool result is available.")

    def models(request, **kwargs):
        seen.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(b'{"data":[{"id":"plain-model"}]}')

    _mock_transport(monkeypatch, handler)
    monkeypatch.setattr("urllib.request.urlopen", models)
    payload = {"provider_id": "relay", "base_url": BASE, "remove_header_names": ["x-obsolete"]}
    with _client(service) as client:
        if stage == "models":
            response = client.post("/api/v1/settings/provider/models", json=payload)
        elif stage == "text":
            response = client.post("/api/v1/settings/provider/test", json={**payload, "model": "plain-model", "prompt": "Describe a task.", "wire_format": "chat"})
        else:
            response = client.post("/api/v1/settings/provider/models/liveness", json={**payload, "models": ["plain-model"], "wire_format": "chat"})
    assert response.status_code == 200
    assert seen and all("x-obsolete" not in headers and headers.get("x-account") == "saved-account" for headers in seen)
    saved, _ = service.resolve_profile("relay")
    assert saved.extra_headers["X-Obsolete"] == "old-secret"


def test_explicit_header_removal_persists_but_blanks_still_preserve_secrets(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "saved-account", "X-Obsolete": "old-secret"})
    with _client(service) as client:
        response = client.put("/api/v1/settings/providers", json={
            "provider_id": "relay", "name": "Local probe", "extra_headers": {"x-account": ""},
            "remove_header_names": ["X-OBSOLETE"], "make_active": False,
        })
        assert response.status_code == 200
        assert response.json()["profile"]["extra_header_names"] == ["X-Account"]
        assert "saved-account" not in response.text and "old-secret" not in response.text
    reloaded = ProviderSettingsService(service._path)
    saved, _ = reloaded.resolve_profile("relay")
    assert saved.extra_headers == {"X-Account": "saved-account"}


def test_remove_then_set_header_is_a_replacement_and_rejects_invalid_names(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "saved-account"})
    with _client(service) as client:
        payload = {"provider_id": "relay", "name": "Local probe", "remove_header_names": ["x-account"], "extra_headers": {"X-Account": "replacement"}}
        assert client.put("/api/v1/settings/providers", json=payload).status_code == 200
        invalid = client.put("/api/v1/settings/providers", json={**payload, "remove_header_names": ["Bad Header"]})
        assert invalid.status_code == 400
    assert service.resolve_profile("relay")[0].extra_headers == {"X-Account": "replacement"}


def test_deleting_non_default_service_does_not_resolve_old_session_to_default(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service)
    service.save_profile(provider_id="old", name="Old", base_url=BASE, api_key=KEY,
                         model="old-model", selected_models=["old-model"], reasoning_effort="auto", make_active=False)
    with _client(service) as client:
        assert client.delete("/api/v1/settings/providers/old").status_code == 200
    missing, credential = service.resolve_agent_profile("old")
    assert not missing.configured and credential is None
    assert service.resolve_agent_profile(None)[0].configured


def test_removal_fields_are_available_in_generated_openapi(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    with _client(service) as client:
        schema = client.get("/openapi.json").json()
    for name in ["ProviderProfileUpdate", "ProviderModelsRequest", "ProviderTestRequest", "ProviderLivenessRequest", "ProviderRoutesDeleteRequest"]:
        assert schema["components"]["schemas"][name]["properties"]["remove_header_names"]["type"] == "array"


def test_clear_routes_uses_removed_header_draft_identity_without_saving(tmp_path):
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save(service, headers={"X-Account": "saved-account", "X-Obsolete": "old-secret"})
    service._record_routes(base_url=BASE, api_key=KEY, extra_headers={"X-Account": "saved-account"},
                           records={"plain-model": {"wire_format": "responses", "tools": True}})
    with _client(service) as client:
        response = client.request("DELETE", "/api/v1/settings/provider/routes", json={
            "provider_id": "relay", "base_url": BASE, "remove_header_names": ["x-obsolete"],
        })
    assert response.status_code == 200 and response.json()["cleared"] == 1
    assert service.resolve_profile("relay")[0].extra_headers["X-Obsolete"] == "old-secret"
