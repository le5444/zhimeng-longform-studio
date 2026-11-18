from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import httpx

from zhimeng_runtime.provider_settings_service import ProviderSettingsService


def _mock_probe_transport(monkeypatch, handler, captured_kwargs: dict) -> None:
    """让探测走真实 Provider 链路，但把网络换成 MockTransport。

    2026-09-12 收编之后，「测试模型 / 一键测活」不再自己拼 `/chat/completions`，
    而是复用 `build_agent_provider_from_connection` + `stream_chat`（与正式对话、
    写章同一条链）。所以不能再打桩 urllib，要在 httpx 这层打——
    这样 URL 解析、认证头、协议选择、请求体依然全部由产品代码生成。
    """
    from zhimeng_runtime import agent_provider as ap

    real_factory = ap.build_agent_provider_from_connection

    def fake_factory(settings, **kwargs):
        captured_kwargs.update(kwargs)
        provider = real_factory(settings, **kwargs)
        if provider is not None:
            provider._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                timeout=httpx.Timeout(30.0),
            )
        return provider

    monkeypatch.setattr(ap, "build_agent_provider_from_connection", fake_factory)


def _sse_http(pieces: list[str]) -> httpx.Response:
    body = "".join(
        "data: "
        + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
        + "\n\n"
        for piece in pieces
    )
    body += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


class _JsonResponse(io.BytesIO):
    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _sse(pieces: list[str]) -> bytes:
    """拼一段 OpenAI 风格的流式响应，供假 urlopen 返回。"""
    lines = [
        "data: "
        + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
        for piece in pieces
    ]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def test_multiple_provider_catalog_hides_keys_and_custom_test_uses_saved_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="primary",
        name="主力线路",
        base_url="https://primary.example/v1",
        model="author-model",
        api_key="secret-primary-key",
        selected_models=["author-model", "review-model"],
        reasoning_effort="high",
        make_active=True,
    )
    service.save_profile(
        provider_id="backup",
        name="备用线路",
        base_url="https://backup.example/v1",
        model="backup-model",
        api_key="secret-backup-key",
        selected_models=["backup-model"],
        reasoning_effort="auto",
        make_active=False,
    )

    public_catalog = service.catalog_view()
    assert public_catalog["active_provider_id"] == "primary"
    assert [item["provider_id"] for item in public_catalog["providers"]] == [
        "primary",
        "backup",
    ]
    assert all(item["api_key_set"] for item in public_catalog["providers"])
    assert "secret-primary-key" not in json.dumps(public_catalog, ensure_ascii=False)
    assert "secret-backup-key" not in json.dumps(public_catalog, ensure_ascii=False)

    captured: dict[str, object] = {}
    factory_kwargs: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return _sse_http(["备用线路", "连接成功"])

    _mock_probe_transport(monkeypatch, handler, factory_kwargs)
    result = service.test_model(
        provider_id="backup",
        base_url="https://backup.example/v1",
        api_key=None,
        model="backup-model",
        prompt="请回答这句自定义测试",
    )

    # 请求没带密钥时要用这个渠道保存的那把
    assert factory_kwargs["api_key"] == "secret-backup-key"
    # 协议/认证按该渠道的配置走，不再硬编码（这个渠道没配，就是默认 chat+bearer）
    assert factory_kwargs["wire_format"] == "chat"
    assert factory_kwargs["auth_type"] == "bearer"
    # 测活只发十几个 token：绝不能带作者配的思考强度，否则推理模型会把预算
    # 全花在思考上、一个字不吐，能用的模型会被误判成「没返回文字」。
    assert factory_kwargs["reasoning_effort"] == "auto"
    # 探测如实反映这一次：不重试（重试会掩盖真实延迟，一批模型也太贵）
    assert factory_kwargs["max_retries"] == 0

    assert captured["url"] == "https://backup.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret-backup-key"
    body = captured["body"]
    assert body["model"] == "backup-model"
    assert body["messages"] == [{"role": "user", "content": "请回答这句自定义测试"}]
    assert body["max_tokens"] == 80
    # 必须流式：作者的中转非流式会缓冲到超时，把能用的模型误判成坏的
    assert body["stream"] is True
    # reasoning_effort=auto 的含义就是不发这个参数
    assert "reasoning_effort" not in body
    assert result["ok"] is True
    assert result["model"] == "backup-model"
    assert result["reply"] == "备用线路连接成功"
    assert "secret-backup-key" not in json.dumps(result, ensure_ascii=False)


def test_reasoning_effort_accepts_max_for_glm_style_models(tmp_path) -> None:
    """GLM 等模型最高档叫 max：保存与服务都要接受，不许在系统里被改成别的档。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    saved = service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="glm-4.6",
        api_key="sk-test",
        selected_models=["glm-4.6"],
        reasoning_effort="max",
        make_active=True,
    )
    assert saved["profile"]["reasoning_effort"] == "max"
    effective = service.effective()
    assert effective.reasoning_effort == "max"


def test_accepted_reported_model_aliases_round_trip_through_settings(tmp_path) -> None:
    """中转把模型名报成别的写法时，作者要能在界面里加接受名，而不是去改启动脚本。

    身份核对本身不放宽（AGENTS.md 3.49 的边界）；这里只验证名单能在设置里
    存下来、读回来、并跟着连接配置一起交给 provider。
    """
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    saved = service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="[opencode]deepseek-v4-pro",
        api_key="sk-test",
        selected_models=["[opencode]deepseek-v4-pro"],
        reasoning_effort="high",
        # 同一个模型被这条中转报过三种写法，都要能填进来
        accepted_reported_models=[
            "deepseek-v4-pro",
            "deepseek/deepseek-v4-pro",
            "deepseek-v4-pro-260425-fy",
            "deepseek-v4-pro",  # 重复的要去重
            "  ",  # 空白的要丢掉
        ],
        make_active=True,
    )
    assert saved["profile"]["accepted_reported_models"] == [
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-pro-260425-fy",
    ]

    # 重新构造一个 service 读同一份文件：名单要能从磁盘读回来
    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    assert reloaded.effective().accepted_reported_models == (
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-pro-260425-fy",
    )
    assert reloaded.catalog_view()["providers"][0]["accepted_reported_models"] == [
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-pro-260425-fy",
    ]
    # 不传就保持原样，不会被清空
    unchanged = reloaded.save_profile(
        provider_id="relay",
        name="中转站",
        base_url=None,
        model=None,
        api_key=None,
        selected_models=None,
        reasoning_effort=None,
        make_active=True,
    )
    assert len(unchanged["profile"]["accepted_reported_models"]) == 3


def test_reported_model_alias_with_whitespace_is_rejected(tmp_path) -> None:
    """名字里带空格多半是作者粘错了，当场说清楚，别等到对话失败才发现。"""
    import pytest

    from zhimeng_runtime.provider_settings_service import ProviderSettingsError

    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    with pytest.raises(ProviderSettingsError):
        service.save_profile(
            provider_id="relay",
            name="中转站",
            base_url="https://relay.example/v1",
            model="m",
            api_key="sk-test",
            selected_models=["m"],
            reasoning_effort="auto",
            accepted_reported_models=["deep seek v4"],
            make_active=True,
        )


def test_model_liveness_separates_working_models_from_broken_channels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """一键测活：列表里列出来的不等于能用，要按实测把能用的挑出来并排在前面。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="good-fast",
        api_key="secret-relay-key",
        selected_models=["good-fast"],
        reasoning_effort="auto",
        make_active=True,
    )

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen.append(body)
        model = body["model"]
        if model == "broken-upstream":
            return httpx.Response(502, content=b"")
        if model == "dead-timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if model == "silent":
            return _sse_http([])
        return _sse_http(["ok"])

    _mock_probe_transport(monkeypatch, handler, {})
    report = service.test_models(
        provider_id="relay",
        base_url="https://relay.example/v1",
        api_key=None,
        models=["broken-upstream", "good-fast", "dead-timeout", "silent"],
        max_workers=1,
        # 这条只管「能用 / 坏了」分得清；「自动」换协议另有专门的测试
        wire_format="chat",
    )

    assert report["tested"] == 4
    assert report["usable_models"] == ["good-fast"]
    assert report["fastest_model"] == "good-fast"
    # 能用的排最前面，坏的沉底
    assert report["results"][0]["model"] == "good-fast"
    by_model = {item["model"]: item for item in report["results"]}
    assert by_model["broken-upstream"]["status"] == "upstream"
    assert by_model["dead-timeout"]["status"] == "timeout"
    assert by_model["silent"]["status"] == "empty"
    assert all(not item["usable"] for item in report["results"][1:])
    # 每条都要有作者看得懂的说法，且不能泄漏密钥
    assert all(item["label"] for item in report["results"])
    assert "secret-relay-key" not in json.dumps(report, ensure_ascii=False)
    # 测活必须走流式；试出字用最小 token，能出字的再带读文件工具问一句会不会调
    assert all(item["stream"] is True for item in seen)
    assert all(item["max_tokens"] == 16 for item in seen if "tools" not in item)
    tool_probes = [item for item in seen if "tools" in item]
    assert [item["model"] for item in tool_probes] == ["good-fast"]
    assert tool_probes[0]["tools"][0]["function"]["name"] == "read_file"



def test_model_liveness_counts_a_model_that_only_thinks_as_usable(tmp_path: Path, monkeypatch) -> None:
    """先想后答的模型：十几个 token 全花在思考上、正文还没开口，也是活的（2026-09-26 真机：kimi-k3 被误判「没出字」）。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="thinker",
        api_key="secret-relay-key",
        selected_models=["thinker"],
        reasoning_effort="auto",
        make_active=True,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode("utf-8"))["model"]
        if model == "silent":
            return _sse_http([])
        body = "".join(
            "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": piece}}]}, ensure_ascii=False) + "\n\n"
            for piece in ("先想", "一想")
        )
        body += 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))

    _mock_probe_transport(monkeypatch, handler, {})
    report = service.test_models(provider_id="relay", base_url="https://relay.example/v1", api_key=None,
                                 models=["thinker", "silent"], max_workers=1)

    by_model = {item["model"]: item for item in report["results"]}
    assert by_model["thinker"]["usable"] is True
    assert "先想后答" in by_model["thinker"]["note"]
    assert "会正常回答" not in by_model["thinker"]["note"]
    assert by_model["thinker"]["first_byte_ms"] is not None
    # 什么都没回的照旧算「没出字」
    assert by_model["silent"]["status"] == "empty"
    assert report["usable_models"] == ["thinker"]


def test_accept_reported_model_on_active_appends_alias_and_is_idempotent(tmp_path) -> None:
    """失败卡「认可这个名字」：名字加进当前在用那套的接受名单，其他配置不动；
    重复认可幂等；空名字与没有在用服务都给人话错误。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="glm-4.6",
        api_key="sk-test",
        selected_models=["glm-4.6"],
        reasoning_effort="high",
        make_active=True,
    )

    accepted = service.accept_reported_model_on_active("glm-4.6-0915")
    assert accepted["already_accepted"] is False
    assert accepted["reported_model"] == "glm-4.6-0915"
    assert accepted["active_settings"]["accepted_reported_models"] == ["glm-4.6-0915"]

    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    effective = reloaded.effective()
    assert effective.model == "glm-4.6"
    assert effective.api_key == "sk-test"
    assert effective.accepted_reported_models == ("glm-4.6-0915",)

    again = reloaded.accept_reported_model_on_active("glm-4.6-0915")
    assert again["already_accepted"] is True
    assert again["active_settings"]["accepted_reported_models"] == ["glm-4.6-0915"]

    import pytest

    from zhimeng_runtime.provider_settings_service import ProviderSettingsError

    with pytest.raises(ProviderSettingsError):
        reloaded.accept_reported_model_on_active("   ")

    empty = ProviderSettingsService(tmp_path / "other-settings.json")
    with pytest.raises(ProviderSettingsError):
        empty.accept_reported_model_on_active("glm-4.6-0915")


def test_context_window_tier_saves_and_reads_back(tmp_path: Path) -> None:
    """上下文窗口档位：保存→读回→catalog/public 视图都带；不写时默认 256k。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="primary",
        name="主力线路",
        base_url="https://primary.example/v1",
        model="author-model",
        api_key="secret-primary-key",
        selected_models=["author-model"],
        reasoning_effort="high",
        context_window="1m",
        make_active=True,
    )
    catalog = service.catalog_view()
    assert catalog["providers"][0]["context_window"] == "1m"
    public = service.public_view()
    assert public["context_window"] == "1m"

    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    assert reloaded.catalog_view()["providers"][0]["context_window"] == "1m"
    # 落到连接上，provider 构建能拿到
    connection, _ = reloaded.resolve_agent_profile(None)
    assert connection.context_window == "1m"

    # 默认值与脏值都回落到 256k
    service.save_profile(
        provider_id="backup",
        name="备用线路",
        base_url="https://backup.example/v1",
        model="backup-model",
        api_key="secret-backup-key",
        selected_models=["backup-model"],
        reasoning_effort="auto",
        make_active=False,
    )
    assert service.catalog_view()["providers"][1]["context_window"] == "256k"


def test_model_liveness_explains_a_missing_model_or_a_rate_limit_in_terms_of_that_model(tmp_path: Path, monkeypatch) -> None:
    """9-26 真机：deepseek-v4-pro 回 404 时提示成「地址能连上，但没有 /models 接口」（那是拉模型列表的说法）；
    一口气测几十个模型时中转站回 429「请求过于频繁」。测活说的应该是这个模型怎么了，并附上中转站原话。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url="https://relay.example/v1",
        model="gone",
        api_key="secret-relay-key",
        selected_models=["gone"],
        reasoning_effort="auto",
        make_active=True,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content.decode("utf-8"))["model"]
        if model == "gone":
            return httpx.Response(404, json={"error": {"message": "model gone not found", "type": "invalid_request_error"}})
        return httpx.Response(429, json={"error": {"message": "请求过于频繁,请稍后再试", "type": "new_api_error"}})

    _mock_probe_transport(monkeypatch, handler, {})
    report = service.test_models(provider_id="relay", base_url="https://relay.example/v1", api_key=None,
                                 models=["gone", "busy"], max_workers=1)
    by_model = {item["model"]: item for item in report["results"]}
    assert by_model["gone"]["status"] == "not_found"
    assert "没有这个模型" in by_model["gone"]["note"]
    assert "/models 接口" not in by_model["gone"]["note"]
    assert "model gone not found" in by_model["gone"]["note"]
    assert "429" in by_model["busy"]["note"] and "请求过于频繁" in by_model["busy"]["note"]
    assert "secret-relay-key" not in json.dumps(report, ensure_ascii=False)
