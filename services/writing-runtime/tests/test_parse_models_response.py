"""模型发现的容错解析 + 发现失败不判死。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.provider_settings_api import create_provider_settings_router
from zhimeng_runtime.provider_settings_service import (
    ProviderProbeError,
    ProviderSettingsService,
    parse_models_response,
)


def test_openai_standard_shape() -> None:
    assert parse_models_response(
        {"data": [{"id": "m-a"}, {"id": "m-b"}, {"no-id": 1}]}
    ) == ["m-a", "m-b"]


def test_data_as_string_array() -> None:
    assert parse_models_response({"data": ["m-a", "m-b"]}) == ["m-a", "m-b"]


def test_models_and_items_shapes() -> None:
    assert parse_models_response({"models": ["m-a", {"id": "m-b"}]}) == ["m-a", "m-b"]
    assert parse_models_response(
        {"models": [{"model_id": "m-x"}, {"id": "m-y"}]}
    ) == ["m-x", "m-y"]
    assert parse_models_response({"items": [{"id": "m-i"}]}) == ["m-i"]
    assert parse_models_response({"items": ["m-j"]}) == ["m-j"]


def test_top_level_string_array() -> None:
    assert parse_models_response(["m-a", "m-b"]) == ["m-a", "m-b"]


def test_unrecognized_shape_raises_with_key_summary_only() -> None:
    with pytest.raises(ProviderProbeError) as caught:
        parse_models_response(
            {"data": [{"name": "secret-model-name"}], "extra": 1}
        )
    message = str(caught.value)
    assert "形状不认识" in message
    # 摘要只带键名，不带值
    assert "data" in message and "extra" in message
    assert "secret-model-name" not in message


def test_empty_list_is_not_a_shape_error() -> None:
    assert parse_models_response({"data": []}) == []


class _JsonResponse(io.BytesIO):
    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_list_models_uses_tolerant_parser_and_15s_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, *, timeout: int):
        captured["timeout"] = timeout
        return _JsonResponse(
            json.dumps({"models": [{"model_id": "m-b"}, "m-a"]}).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    models = service.list_models(
        base_url="https://relay.example/v1", api_key="sk-test"
    )
    assert models == ["m-a", "m-b"]
    assert captured["timeout"] == 15


def test_list_models_failure_is_not_a_death_sentence(
    tmp_path: Path, monkeypatch
) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")

    def fake_urlopen(request: urllib.request.Request, *, timeout: int):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProviderProbeError) as caught:
        service.list_models(base_url="https://relay.example/v1", api_key="sk-test")
    message = str(caught.value)
    assert "模型列表拉不到" in message
    assert "不代表模型不能用" in message
    assert "手动填模型名" in message
    assert "测试" in message


def test_list_models_sends_auth_headers_matching_channel_config(
    tmp_path: Path, monkeypatch
) -> None:
    """获取模型的认证头按渠道配置走：x-api-key / 自定义头不再写死 Bearer。

    2026-09-12 缝补：作者配了非 Bearer 认证的渠道，正式对话是好的，
    「获取模型」却 401——因为这里写死了 Bearer。
    """
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="m-a",
        api_key="sk-test",
        selected_models=["m-a"],
        reasoning_effort="auto",
        auth_type="x-api-key",
        extra_headers={"X-Relay-Tag": "primary"},
        make_active=True,
    )
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, *, timeout: int):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _JsonResponse(json.dumps({"data": [{"id": "m-a"}]}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    models = service.list_models(
        base_url="https://relay.example/v1", api_key="sk-test", provider_id="relay"
    )
    assert models == ["m-a"]
    headers = captured["headers"]
    assert "authorization" not in headers
    assert headers["x-api-key"] == "sk-test"
    assert headers["x-relay-tag"] == "primary"


def test_list_models_without_profile_keeps_bearer_default(
    tmp_path: Path, monkeypatch
) -> None:
    """没有保存渠道时维持朴素默认：Bearer（与 _probe_provider_for 同一口径）。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, *, timeout: int):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _JsonResponse(json.dumps({"data": [{"id": "m-a"}]}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    models = service.list_models(
        base_url="https://relay.example/v1", api_key="sk-test"
    )
    assert models == ["m-a"]
    assert captured["headers"]["authorization"] == "Bearer sk-test"


def test_list_models_uses_unsaved_form_connection(tmp_path: Path, monkeypatch) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay", name="relay", base_url="https://relay.example/v1",
        model="m-a", api_key="sk-old", auth_type="bearer",
        extra_headers={"X-Client": "old"}, models_url="https://relay.example/old/models",
        make_active=True, selected_models=["m-a"], reasoning_effort="auto",
    )
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _JsonResponse(b'{"data":[{"id":"m-draft"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_provider_settings_router(service))
    response = TestClient(app).post("/api/v1/settings/provider/models", json={
        "provider_id": "relay", "base_url": "https://relay.example/v1",
        "api_key": "sk-draft", "auth_type": "x-api-key",
        "extra_headers": {"X-Client": "draft", "User-Agent": "custom-client"},
        "models_url": "https://relay.example/custom/models",
    })
    assert response.status_code == 200, response.text
    assert response.json()["models"] == ["m-draft"]
    assert captured["url"] == "https://relay.example/custom/models"
    assert captured["headers"]["x-api-key"] == "sk-draft"
    assert captured["headers"]["x-client"] == "draft"
    assert captured["headers"]["user-agent"] == "custom-client"
    assert "authorization" not in captured["headers"]
    profile = service.resolve_profile("relay")[0]
    assert profile.api_key == "sk-old"
    assert profile.auth_type == "bearer"
    assert profile.extra_headers == {"X-Client": "old"}
    assert profile.models_url == "https://relay.example/old/models"
    assert "sk-draft" not in response.text


def test_new_service_does_not_borrow_active_service_key_or_headers(tmp_path: Path, monkeypatch) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="existing", name="existing", base_url="https://existing.example/v1",
        model="m-a", api_key="sk-existing", auth_type="x-api-key",
        extra_headers={"X-Private": "existing-token"}, make_active=True,
        selected_models=["m-a"], reasoning_effort="auto",
    )
    captured = []

    def fake_urlopen(request, *, timeout):
        captured.append({k.lower(): v for k, v in request.header_items()})
        return _JsonResponse(b'{"data":[{"id":"m-a"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProviderProbeError, match="还没有填写密钥"):
        service.list_models(base_url="https://new.example/v1", api_key=None)
    assert captured == []
    assert service.list_models(base_url="https://new.example/v1", api_key="sk-new") == ["m-a"]
    assert captured[0]["authorization"] == "Bearer sk-new"
    assert "x-api-key" not in captured[0]
    assert "x-private" not in captured[0]
    assert "user-agent" in captured[0]


@pytest.mark.parametrize("auth_type", ["none", "custom"])
def test_model_listing_allows_keyless_auth_modes(tmp_path: Path, monkeypatch, auth_type) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")

    def fake_urlopen(request, *, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        assert "authorization" not in headers
        assert headers["x-session"] == "session-value"
        return _JsonResponse(b'{"data":[{"id":"m-a"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert service.list_models(
        base_url="https://relay.example/v1", api_key=None,
        auth_type=auth_type, extra_headers={"X-Session": "session-value"},
    ) == ["m-a"]


def test_blank_draft_models_url_clears_saved_override_for_this_request(tmp_path: Path, monkeypatch) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay", name="relay", base_url="https://relay.example/v1",
        model="m-a", api_key="sk-test", models_url="https://relay.example/custom/models",
        selected_models=["m-a"], reasoning_effort="auto", make_active=True,
    )

    def fake_urlopen(request, *, timeout):
        assert request.full_url == "https://relay.example/v1/models"
        return _JsonResponse(b'{"data":[{"id":"m-a"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert service.list_models(
        provider_id="relay", base_url="https://relay.example/v1", api_key=None, models_url="",
    ) == ["m-a"]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_models_endpoint_preserves_upstream_status_without_echoing_key(tmp_path: Path, monkeypatch, status) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    key = "test-key-never-display"

    def fake_urlopen(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url, status, "failed", {}, io.BytesIO(f'{{"error":"rejected {key}"}}'.encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_provider_settings_router(service))
    response = TestClient(app).post("/api/v1/settings/provider/models", json={
        "base_url": "https://relay.example/v1", "api_key": key,
    })
    assert response.status_code == status
    assert key not in response.text
    assert "https://relay.example/v1/models" in response.json()["error"]["message"]
