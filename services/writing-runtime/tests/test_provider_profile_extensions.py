"""Provider 接入层分层第一刀：渠道级新字段 + 模型级覆盖的配置层测试。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from zhimeng_runtime.provider_settings_service import (
    ProviderSettingsError,
    ProviderSettingsService,
    resolve_model_connection,
)


class _JsonResponse(io.BytesIO):
    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _save_extended_profile(service: ProviderSettingsService) -> dict:
    return service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="main-model",
        api_key="sk-channel-key",
        selected_models=["main-model", "fast-model"],
        reasoning_effort="auto",
        auth_type="x-api-key",
        extra_headers={"X-Project-ID": "novel-project", "anthropic-version": "2023-06-01"},
        models_url="https://relay.example/internal/models",
        request_timeout_seconds=120.0,
        stream_idle_timeout_seconds=30.0,
        max_retries=3,
        model_overrides={
            "fast-model": {
                "wire_format": "responses",
                "base_url": "https://fast.example/v1",
                "api_key": "sk-fast-key",
                "reasoning_effort": "high",
                "max_output_tokens": 8192,
                "extra_headers": {"X-Accel": "yes"},
            }
        },
        make_active=True,
    )


def test_extended_profile_round_trip_and_old_json_still_reads(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    saved = _save_extended_profile(service)
    assert saved["profile"]["auth_type"] == "x-api-key"

    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    profile, _reference = reloaded.resolve_profile("relay")
    assert profile is not None
    assert profile.auth_type == "x-api-key"
    assert profile.extra_headers == {
        "X-Project-ID": "novel-project",
        "anthropic-version": "2023-06-01",
    }
    assert profile.models_url == "https://relay.example/internal/models"
    assert profile.request_timeout_seconds == 120.0
    assert profile.stream_idle_timeout_seconds == 30.0
    assert profile.max_retries == 3
    override = profile.model_overrides["fast-model"]
    assert override.wire_format == "responses"
    assert override.base_url == "https://fast.example/v1"
    assert override.api_key == "sk-fast-key"
    assert override.reasoning_effort == "high"
    assert override.max_output_tokens == 8192
    assert override.extra_headers == {"X-Accel": "yes"}

    # 旧 JSON（没有任何新键）原样能读，全部落到默认值
    legacy_path = tmp_path / "legacy" / "provider-settings.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "schema": "zhimeng.provider-catalog.v1",
                "active_provider_id": "old",
                "providers": [
                    {
                        "provider_id": "old",
                        "name": "旧渠道",
                        "base_url": "https://old.example/v1",
                        "model": "old-model",
                        "api_key": "sk-old",
                        "selected_models": ["old-model"],
                        "reasoning_effort": "auto",
                        "wire_format": "chat",
                        "accepted_reported_models": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    legacy = ProviderSettingsService(legacy_path)
    legacy_profile, _ = legacy.resolve_profile("old")
    assert legacy_profile is not None
    assert legacy_profile.auth_type == "bearer"
    assert legacy_profile.extra_headers == {}
    assert legacy_profile.models_url is None
    assert legacy_profile.request_timeout_seconds is None
    assert legacy_profile.model_overrides == {}
    assert legacy.effective().auth_type == "bearer"


def test_deselect_and_reselect_model_preserves_its_saved_connection(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / 'provider-settings.json')
    _save_extended_profile(service)
    previous, _ = service.resolve_profile('relay')
    arguments = dict(provider_id='relay', name='Updated service', base_url=None, model='main-model',
        api_key=None, reasoning_effort='auto', make_active=False)
    service.save_profile(**arguments, selected_models=['main-model'])
    service = ProviderSettingsService(tmp_path / 'provider-settings.json')
    deselected, _ = service.resolve_profile('relay')
    assert deselected.selected_models == ('main-model',)
    assert deselected.model_overrides == previous.model_overrides
    service.save_profile(**arguments, selected_models=['main-model', 'fast-model'])
    restored, _ = service.resolve_profile('relay')
    assert resolve_model_connection(restored, 'fast-model') == resolve_model_connection(previous, 'fast-model')


def test_views_never_leak_header_values_or_override_keys(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save_extended_profile(service)

    for view in (service.catalog_view(), service.public_view()):
        serialized = json.dumps(view, ensure_ascii=False)
        assert "sk-channel-key" not in serialized
        assert "sk-fast-key" not in serialized
        assert "novel-project" not in serialized
        assert "2023-06-01" not in serialized
        assert '"yes"' not in serialized

    catalog_profile = service.catalog_view()["providers"][0]
    assert catalog_profile["auth_type"] == "x-api-key"
    assert catalog_profile["extra_header_names"] == ["X-Project-ID", "anthropic-version"]
    assert catalog_profile["models_url"] == "https://relay.example/internal/models"
    assert catalog_profile["request_timeout_seconds"] == 120.0
    assert catalog_profile["max_retries"] == 3
    override_view = catalog_profile["model_overrides"]["fast-model"]
    assert override_view["api_key_set"] is True
    assert override_view["extra_header_names"] == ["X-Accel"]
    assert sorted(override_view["overridden_fields"]) == [
        "api_key",
        "base_url",
        "extra_headers",
        "max_output_tokens",
        "reasoning_effort",
        "wire_format",
    ]

    public = service.public_view()
    assert public["auth_type"] == "x-api-key"
    assert public["extra_header_names"] == ["X-Project-ID", "anthropic-version"]
    assert public["model_overrides"]["fast-model"]["api_key_set"] is True


def test_resolve_model_connection_applies_overrides_with_priority(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save_extended_profile(service)
    profile, _ = service.resolve_profile("relay")
    assert profile is not None

    # 被覆盖的模型：模型级字段全生效，extra_headers 与渠道级合并（模型级优先）
    fast = resolve_model_connection(profile, "fast-model")
    assert fast.base_url == "https://fast.example/v1"
    assert fast.api_key == "sk-fast-key"
    assert fast.wire_format == "responses"
    assert fast.reasoning_effort == "high"
    assert fast.max_output_tokens == 8192
    assert fast.extra_headers == {
        "X-Project-ID": "novel-project",
        "anthropic-version": "2023-06-01",
        "X-Accel": "yes",
    }
    # 渠道级字段原样继承
    assert fast.auth_type == "x-api-key"
    assert fast.models_url == "https://relay.example/internal/models"
    assert fast.provider_id == "relay"

    # 没配覆盖的模型：渠道级连接原样，只有模型名换成目标
    main = resolve_model_connection(profile, "main-model")
    assert main.model == "main-model"
    assert main.base_url == "https://relay.example/v1"
    assert main.api_key == "sk-channel-key"
    assert main.wire_format == "chat"
    assert main.max_output_tokens is None

    # 不点名模型：用渠道的默认模型
    default = resolve_model_connection(profile, None)
    assert default.model == "main-model"


def test_explicit_chat_survives_capability_card_suggestion(tmp_path: Path) -> None:
    """渠道写明 wire_format='chat'，就算能力卡对某个家族（grok/deepseek）建议 responses，
    用户的明确选择也不能被改（作者 10-04 钉的：能力卡只是自动档建议）。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="grok-4.6",
        api_key="sk-channel-key",
        selected_models=["grok-4.6", "deepseek-v3.2"],
        reasoning_effort="auto",
        wire_format="chat",  # 用户明确要求 chat
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    assert profile is not None
    assert profile.wire_format_explicit is True

    # 渠道默认模型
    default_conn = resolve_model_connection(profile, None)
    assert default_conn.wire_format == "chat"

    # 家族规则会建议 deepseek 走 responses 的模型，用户的 chat 也不许被改
    deepseek_conn = resolve_model_connection(profile, "deepseek-v3.2")
    assert deepseek_conn.wire_format == "chat"


def test_unwritten_wire_allows_capability_card_suggestion(tmp_path: Path) -> None:
    """配置文件里没写 wire_format（v1 / 环境变量来源），允许能力卡给一次建议。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="grok-4.6",
        api_key="sk-channel-key",
        selected_models=["grok-4.6"],
        reasoning_effort="auto",
        wire_format=None,  # 不写：保存时落到 chat，但 wire_format_explicit=False
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    assert profile is not None
    assert profile.wire_format_explicit is False

    conn = resolve_model_connection(profile, None)
    assert conn.wire_format == "responses"  # grok 家族规则给的建议
    # 「自动」不把按模型名猜的档位发给上游，保留给 Provider 省略参数。
    assert conn.reasoning_effort == "auto"
    # max_output_tokens 不从能力卡补（它是会话预算，不是模型声明）——保持 None
    assert conn.max_output_tokens is None


def test_model_override_wire_format_is_respected_and_marks_explicit(tmp_path: Path) -> None:
    """模型级 override 里写了 wire_format（如 deepseek 改 anthropic），
    它就生效，且能力卡不能反过来用 chat 盖它，也不能再改这条连接的 wire。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="main-model",
        api_key="sk-channel-key",
        selected_models=["main-model", "special-model"],
        reasoning_effort="auto",
        wire_format="chat",
        model_overrides={"special-model": {"wire_format": "anthropic_messages"}},
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    conn = resolve_model_connection(profile, "special-model")
    assert conn.wire_format == "anthropic_messages"
    assert conn.wire_format_explicit is True


def test_model_override_max_output_tokens_respected_even_if_card_wants_other(
    tmp_path: Path,
) -> None:
    """模型级写了 max_output_tokens=8192：能力卡想给 32768 或 65536 都不许盖。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="deepseek-v4-pro",
        api_key="sk-channel-key",
        selected_models=["deepseek-v4-pro"],
        reasoning_effort="auto",
        model_overrides={"deepseek-v4-pro": {"max_output_tokens": 8192}},
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    conn = resolve_model_connection(profile, "deepseek-v4-pro")
    assert conn.max_output_tokens == 8192


def test_max_output_tokens_not_filled_from_capability_card(tmp_path: Path) -> None:
    """能力卡的 max_output_tokens 不再生效——它是会话预算，不是模型声明。
    作者不写就是 None，交给 build_agent_provider_from_connection 的会话级默认。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="grok-4.6",
        api_key="sk-channel-key",
        selected_models=["grok-4.6"],
        reasoning_effort="auto",
        wire_format=None,  # 自动档
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    conn = resolve_model_connection(profile, "grok-4.6")
    assert conn.max_output_tokens is None


def test_credential_reference_uses_effective_model_key(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    base = dict(
        provider_id="relay", name="中转站", base_url="https://relay.example/v1",
        model="m", selected_models=["m"], reasoning_effort="auto", make_active=True,
    )
    service.save_profile(**base, api_key=None, model_overrides={"m": {"api_key": "model-key"}})
    profile, reference = service.resolve_profile("relay")
    assert profile is not None
    assert resolve_model_connection(profile, "m").api_key == "model-key"
    assert reference and reference.startswith("settings:provider-key:")
    service.save_profile(**base, api_key=None, model_overrides={"m": {"api_key": "changed-key"}})
    _profile, changed = service.resolve_profile("relay")
    assert changed != reference


@pytest.mark.parametrize("url", [
    "https://user:secret@relay.example/v1",
    "https://relay.example/v1?account=private",
    "https://relay.example/v1#private",
])
def test_provider_base_url_rejects_embedded_secrets_and_extras(tmp_path: Path, url: str) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    with pytest.raises(ProviderSettingsError, match="接口地址"):
        service.save_profile(
            provider_id="relay", name="中转站", base_url=url, model="m", api_key="key",
            selected_models=["m"], reasoning_effort="auto", make_active=True,
        )
    """与 api_key「留空不修改」同语义：头值/覆盖密钥给空值 = 保留旧值。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save_extended_profile(service)

    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url=None,
        model=None,
        api_key=None,
        selected_models=None,
        reasoning_effort=None,
        extra_headers={"X-Project-ID": "", "X-New": "added"},
        model_overrides={"fast-model": {"api_key": "", "max_output_tokens": 4096}},
        make_active=True,
    )
    profile, _ = service.resolve_profile("relay")
    assert profile is not None
    # 空值保留旧值；新名字正常进来
    assert profile.extra_headers == {
        "X-Project-ID": "novel-project",
        "anthropic-version": "2023-06-01",
        "X-New": "added",
    }
    override = profile.model_overrides["fast-model"]
    assert override.api_key == "sk-fast-key"
    assert override.max_output_tokens == 4096
    # 没在这次提交里提的覆盖字段回退到该模型原有值
    assert override.base_url == "https://fast.example/v1"


def test_invalid_auth_type_and_header_names_are_rejected(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    base = dict(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="m",
        api_key="sk-test",
        selected_models=["m"],
        reasoning_effort="auto",
        make_active=True,
    )
    with pytest.raises(ProviderSettingsError, match="认证方式"):
        service.save_profile(**base, auth_type="oauth2")
    with pytest.raises(ProviderSettingsError, match="不合法"):
        service.save_profile(**base, extra_headers={"Bad Header": "x"})
    with pytest.raises(ProviderSettingsError, match="控制字符"):
        service.save_profile(**base, extra_headers={"X-Test": "line1\nline2"})
    with pytest.raises(ProviderSettingsError, match="接口协议"):
        service.save_profile(
            **base, model_overrides={"m": {"wire_format": "grpc"}}
        )
    with pytest.raises(ProviderSettingsError, match="正整数"):
        service.save_profile(
            **base, model_overrides={"m": {"max_output_tokens": -1}}
        )


def test_model_override_for_unselected_model_is_rejected(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    with pytest.raises(ProviderSettingsError, match="没有选中的模型"):
        service.save_profile(
            provider_id="relay",
            name="中转站",
            base_url="https://relay.example/v1",
            model="m",
            api_key="sk-test",
            selected_models=["m"],
            reasoning_effort="auto",
            model_overrides={"ghost-model": {"max_output_tokens": 100}},
            make_active=True,
        )


def test_resilience_fields_round_trip_and_validate(tmp_path: Path) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="m",
        api_key="sk-test",
        selected_models=["m"],
        reasoning_effort="auto",
        max_concurrent_requests=8,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown_seconds=120.0,
        make_active=True,
    )
    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    profile, _ = reloaded.resolve_profile("relay")
    assert profile is not None
    assert profile.max_concurrent_requests == 8
    assert profile.circuit_breaker_threshold == 3
    assert profile.circuit_breaker_cooldown_seconds == 120.0
    # 连接层原样带出（None 才回退内置默认）
    connection = reloaded.resolve_agent_profile("relay")[0]
    assert connection.max_concurrent_requests == 8
    assert connection.circuit_breaker_threshold == 3
    # 视图可见（非密钥字段）
    catalog_profile = reloaded.catalog_view()["providers"][0]
    assert catalog_profile["max_concurrent_requests"] == 8
    assert catalog_profile["circuit_breaker_threshold"] == 3

    base = dict(
        provider_id="relay",
        name="中转站",
        base_url=None,
        model=None,
        api_key=None,
        selected_models=None,
        reasoning_effort=None,
        make_active=True,
    )
    with pytest.raises(ProviderSettingsError, match="并发上限"):
        service.save_profile(**base, max_concurrent_requests=99)
    with pytest.raises(ProviderSettingsError, match="熔断次数"):
        service.save_profile(**base, circuit_breaker_threshold=0)
    with pytest.raises(ProviderSettingsError, match="熔断冷却"):
        service.save_profile(**base, circuit_breaker_cooldown_seconds=99999.0)


def test_explicit_models_url_is_used_for_model_listing(
    tmp_path: Path, monkeypatch
) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    _save_extended_profile(service)

    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, *, timeout: int):
        captured["url"] = request.full_url
        return _JsonResponse(
            json.dumps({"data": [{"id": "main-model"}]}).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    models = service.list_models(
        base_url="https://relay.example/v1", api_key=None, provider_id="relay"
    )
    assert models == ["main-model"]
    # 显式模型列表地址优先于 base_url + /models
    assert captured["url"] == "https://relay.example/internal/models"
