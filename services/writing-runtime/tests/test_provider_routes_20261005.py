"""接口协议「自动」= 测活时真试、试通的记住；测活顺带看会不会调工具（2026-10-05）。

背景：10-04 真机测 220 中转，5 个模型走 Chat 通 4 个、走 Responses 只通 grok 一个，
有两个 Responses 不通时还是「回一个空答案」而不是报错。所以「自动」不能按模型名猜，
要在作者的线路上真试一遍，而且得真出字、真调出工具才算通。

这里的网络全部换成 MockTransport，请求体、地址、协议都还是产品代码生成的。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.provider_settings_api import create_provider_settings_router
from zhimeng_runtime.provider_settings_service import (
    ModelOverride,
    ProviderSettingsService,
    resolve_model_connection,
)

BASE = "https://relay.example/v1"
KEY = "secret-relay-key"


def _mock_transport(monkeypatch, handler) -> None:
    from zhimeng_runtime import agent_provider as ap

    real_factory = ap.build_agent_provider_from_connection

    def fake_factory(settings, **kwargs):
        provider = real_factory(settings, **kwargs)
        if provider is not None:
            provider._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=httpx.Timeout(30.0)
            )
        return provider

    monkeypatch.setattr(ap, "build_agent_provider_from_connection", fake_factory)


def _sse(*payloads: object) -> httpx.Response:
    body = "".join(
        "data: " + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n\n"
        for item in payloads
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))


def _chat_text(text: str = "ok") -> httpx.Response:
    return _sse(
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    )


def _chat_tool_call() -> httpx.Response:
    return _sse(
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_probe1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
        }]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    )


def _responses_text(text: str = "ok") -> httpx.Response:
    return _sse(
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    )


def _responses_tool_call() -> httpx.Response:
    return _sse(
        {"type": "response.output_item.added", "output_index": 0, "item": {
            "id": "fc_1", "type": "function_call", "call_id": "call_probe1",
            "name": "read_file", "arguments": "",
        }},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path": "README.md"}'},
        {"type": "response.completed", "response": {"status": "completed", "output": [{
            "type": "function_call", "call_id": "call_probe1", "name": "read_file",
            "arguments": '{"path": "README.md"}',
        }]}},
    )


def _has_tool_result(body: dict) -> bool:
    """第二轮回传的合成工具结果，Chat/Responses 两种历史形状都认。"""
    if any(item.get("role") == "tool" for item in body.get("messages", [])):
        return True
    return any(item.get("type") == "function_call_output" for item in body.get("input", []))


def _service(tmp_path: Path, *, wire_format: str | None = None, model: str = "plain-model") -> ProviderSettingsService:
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    service.save_profile(
        provider_id="relay",
        name="中转站",
        base_url=BASE,
        model=model,
        api_key=KEY,
        selected_models=[model],
        reasoning_effort="auto",
        wire_format=wire_format,
        make_active=True,
    )
    return service


def _routes_file(tmp_path: Path) -> Path:
    return tmp_path / "provider-routes.json"


def _attempts(result: dict) -> list[dict]:
    """按顺序试过的协议和结果（不比原话）。"""
    return [{key: value for key, value in item.items() if key != "note"} for item in result["tried"]]


def test_liveness_also_checks_that_the_model_can_call_a_tool(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, wire_format="chat")
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if not body.get("tools"):
            return _chat_text()
        return _chat_text("工具结果已收到") if _has_tool_result(body) else _chat_tool_call()

    _mock_transport(monkeypatch, handler)
    report = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")

    result = report["results"][0]
    assert result["usable"] is True
    assert result["tools"] == "ok"
    assert result["tool_call_emitted"] is True
    assert result["tool_roundtrip"] == "ok"
    assert result["wire_format"] == "chat"
    assert result["auto"] is False
    assert _attempts(result) == [{"wire_format": "chat", "status": "ok", "tools": "ok"}]
    # 工具探测像平常干活：先发读文件调用，再回传合成结果让模型继续答。
    assert seen[1]["tools"][0]["function"]["name"] == "read_file"
    assert seen[2]["messages"][-1]["role"] == "tool"
    # 明确选的协议测完不记
    assert not _routes_file(tmp_path).exists()


def test_a_model_that_talks_but_never_calls_tools_is_flagged(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, wire_format="chat")

    async def handler(request: httpx.Request) -> httpx.Response:
        return _chat_text("我是一个回答")

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]

    assert result["usable"] is True  # 能出字照样算能用
    assert result["tools"] == "no"
    assert "没有调工具" in result["tools_note"]


def test_a_model_that_only_thinks_during_the_tool_check_is_unsure_not_unable(tmp_path: Path, monkeypatch) -> None:
    """10-05 真测：kimi-k3 测工具给 512 额度，一直在想、没来得及调就到头了。那不算「不会调工具」。"""
    service = _service(tmp_path, wire_format="chat")
    budgets: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            return _chat_text()
        budgets.append(body["max_tokens"])
        return _sse(
            {"choices": [{"delta": {"reasoning_content": "先想想这个文件在哪"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            "[DONE]",
        )

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]

    assert result["usable"] is True
    assert result["tools"] == "unsure"
    assert "一直在想" in result["tools_note"]
    # 先给 2048；只顾着想、额度用完的再给 8192 问一次
    assert budgets == [2048, 8192]


def test_a_slow_thinker_gets_a_second_try_with_more_room(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, wire_format="chat")
    budgets: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            return _chat_text()
        budgets.append(body["max_tokens"])
        if body["max_tokens"] < 8192:
            return _sse(
                {"choices": [{"delta": {"reasoning_content": "先想想"}}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
                "[DONE]",
            )
        return _chat_text("工具结果已收到") if _has_tool_result(body) else _chat_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]

    assert result["tools"] == "ok"
    assert budgets == [2048, 8192, 8192]


def test_auto_moves_on_when_one_wire_is_missing_and_remembers_the_one_that_worked(tmp_path: Path, monkeypatch) -> None:
    """Chat 那条在这个中转上没对接（404），Responses 出字又会调工具：记 Responses，以后正式对话就走它。"""
    service = _service(tmp_path)  # 不写协议 = 自动
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "该模型当前没有已对接的可用供应商"}})
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]

    assert result["usable"] is True
    assert result["auto"] is True
    assert result["wire_format"] == "responses"
    assert result["tools"] == "ok"
    assert "没有已对接的可用供应商" in result["tried"][0]["note"]
    assert _attempts(result) == [
        {"wire_format": "chat", "status": "not_found"},
        {"wire_format": "responses", "status": "ok", "tools": "ok"},
    ]
    assert paths == [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/responses",
        "/v1/responses",
    ]

    # 记下来了；文件里只有线路摘要，没有钥匙
    stored = _routes_file(tmp_path).read_text(encoding="utf-8")
    assert KEY not in stored
    reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
    profile, _ = reloaded.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "responses"
    view = reloaded.catalog_view()["providers"][0]["verified_routes"]["plain-model"]
    assert view["wire_format"] == "responses" and view["tools"] is True and view["verified_at"]
    with _client(reloaded) as client:
        public = client.get('/api/v1/settings/providers').json()['providers'][0]
        assert public['effective_routes']['plain-model'] == {'wire_format': 'responses', 'source': 'verified'}
        assert KEY not in json.dumps(public)


def test_auto_prefers_a_wire_that_can_call_tools_over_one_that_only_talks(tmp_path: Path, monkeypatch) -> None:
    """Chat 能出字但工具被吃掉，Responses 两样都行：选 Responses。"""
    service = _service(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return _chat_text()
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]

    assert result["wire_format"] == "responses"
    assert result["tools"] == "ok"
    assert [item["wire_format"] for item in result["tried"]] == ["chat", "responses"]


def test_auto_keeps_a_talk_only_wire_when_nothing_can_call_tools(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return _chat_text()
        return httpx.Response(404, json={"error": {"message": "not here"}})

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]

    assert result["usable"] is True
    assert result["wire_format"] == "chat"
    assert result["tools"] == "no"
    stored = json.loads(_routes_file(tmp_path).read_text(encoding="utf-8"))
    (bucket,) = stored["routes"].values()
    assert bucket["plain-model"]["wire_format"] == "chat"
    assert bucket["plain-model"]["tools"] is False


def test_auto_stops_trying_other_wires_when_the_key_is_rejected(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "Invalid token"}})

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]

    assert result["status"] == "auth"
    assert paths == ["/v1/chat/completions"]
    assert not _routes_file(tmp_path).exists()


@pytest.mark.parametrize("failure", [401, 403, 429, "timeout", "network"])
def test_auto_stops_on_non_protocol_errors_in_the_tool_result_round(
    tmp_path: Path, monkeypatch, failure: int | str
) -> None:
    service = _service(tmp_path)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        if not body.get("tools"):
            return _chat_text()
        if not _has_tool_result(body):
            return _chat_tool_call()
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "network":
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(failure, json={"error": {"message": "upstream rejected request"}})

    _mock_transport(monkeypatch, handler)
    result = service.test_models(
        provider_id="relay", base_url=BASE, api_key=None,
        models=["plain-model"], wire_format="auto",
    )["results"][0]

    assert result["tools"] == "failed"
    assert result["tool_call_emitted"] is True
    assert result["tool_roundtrip"] == "failed"
    assert result["remembered"] is False
    assert paths == ["/v1/chat/completions"] * 3
    assert not _routes_file(tmp_path).exists()


def test_tool_roundtrip_without_a_final_answer_is_not_remembered(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "not here"}})
        if not body.get("tools"):
            return _chat_text()
        return _chat_text("") if _has_tool_result(body) else _chat_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(
        provider_id="relay", base_url=BASE, api_key=None,
        models=["plain-model"], wire_format="auto",
    )["results"][0]

    assert result["tools"] == "failed"
    assert result["tool_roundtrip"] == "failed"
    assert result["remembered"] is False
    assert not _routes_file(tmp_path).exists()


def test_auto_stops_on_rate_limit_instead_of_settling_for_a_worse_wire(tmp_path: Path, monkeypatch) -> None:
    """限流（429）跟协议无关：换协议再试只会把一个更差的结果记下来。"""
    service = _service(tmp_path)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(429, json={"error": {"message": "Concurrency limit exceeded"}})
        return _responses_text()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]

    assert result["usable"] is False
    assert paths == ["/v1/chat/completions"]
    assert "Concurrency limit exceeded" in result["tried"][0]["note"]
    assert result["remembered"] is False
    assert not _routes_file(tmp_path).exists()


def test_after_an_unclear_error_only_a_wire_that_calls_tools_is_remembered(tmp_path: Path, monkeypatch) -> None:
    """10-05 真测：kimi 走 Chat 碰巧报错一次，自动换成只会出字的 Anthropic 还记了下来。
    说不准的报错（400、5xx）之后，只会出字的结果照样显示，但不记；出字又会调工具的才记。"""
    service = _service(tmp_path)

    async def talk_only(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(500, json={"error": {"message": "upstream hiccup"}})
        return _responses_text()

    _mock_transport(monkeypatch, talk_only)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]
    assert result["usable"] is True and result["wire_format"] == "responses" and result["tools"] == "no"
    assert result["remembered"] is False
    assert not _routes_file(tmp_path).exists()

    async def full(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(500, json={"error": {"message": "upstream hiccup"}})
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, full)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")["results"][0]
    assert result["wire_format"] == "responses" and result["tools"] == "ok"
    assert result["remembered"] is True
    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "responses"


def test_a_chosen_wire_is_tested_alone_even_when_it_fails(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path, wire_format="chat")
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "no"}})
        return _responses_text()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]

    assert result["status"] == "not_found"
    assert paths == ["/v1/chat/completions"]
    assert not _routes_file(tmp_path).exists()
    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "chat"


def test_suggested_wire_goes_first_in_auto(tmp_path: Path, monkeypatch) -> None:
    """能力卡给 grok 的建议是 Responses：自动时先试它，通了就不再试别的。"""
    service = _service(tmp_path, model="grok-4.6")
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["grok-4.6"], wire_format="auto")["results"][0]

    assert result["wire_format"] == "responses"
    assert paths == ["/v1/responses", "/v1/responses", "/v1/responses"]


def test_verified_route_only_applies_to_the_same_address_and_key(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "no"}})
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, handler)
    service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")

    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "responses"
    # 换了钥匙（sub2api 按钥匙分组，背后可能是另一批上游）：测过的结果不算数，回到建议
    other_key = ProviderSettingsService(tmp_path / "provider-settings.json")
    other_key.save_profile(provider_id="relay", name="中转站", base_url=None, model=None, api_key="another-key",
                           selected_models=None, reasoning_effort=None, make_active=True)
    profile, _ = other_key.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "chat"
    assert other_key.catalog_view()["providers"][0]["verified_routes"] == {}


def test_explicit_choice_and_model_override_beat_a_verified_route(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": "no"}})
        if not body.get("tools"):
            return _responses_text()
        return _responses_text("工具结果已收到") if _has_tool_result(body) else _responses_tool_call()

    _mock_transport(monkeypatch, handler)
    service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="auto")

    service.save_profile(provider_id="relay", name="中转站", base_url=None, model=None, api_key=None,
                         selected_models=None, reasoning_effort=None, wire_format="chat", make_active=True)
    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "chat"

    service.save_profile(provider_id="relay", name="中转站", base_url=None, model=None, api_key=None,
                         selected_models=None, reasoning_effort=None, wire_format="auto", make_active=True)
    profile, _ = service.resolve_profile("relay")
    assert resolve_model_connection(profile, "plain-model").wire_format == "responses"
    overridden = profile.__class__(**{
        **{name: getattr(profile, name) for name in profile.__slots__},
        "model_overrides": {"plain-model": ModelOverride(wire_format="anthropic_messages")},
    })
    assert resolve_model_connection(overridden, "plain-model").wire_format == "anthropic_messages"


def test_saving_auto_undoes_an_earlier_explicit_choice(tmp_path: Path) -> None:
    service = _service(tmp_path, wire_format="responses")
    view = service.catalog_view()["providers"][0]
    assert view["wire_format_explicit"] is True and view["wire_format"] == "responses"

    keep = service.save_profile(provider_id="relay", name="中转站", base_url=None, model=None, api_key=None,
                                selected_models=None, reasoning_effort=None, make_active=True)
    assert keep["profile"]["wire_format_explicit"] is True  # 不给就保持原样

    back = service.save_profile(provider_id="relay", name="中转站", base_url=None, model=None, api_key=None,
                                selected_models=None, reasoning_effort=None, wire_format="auto", make_active=True)
    assert back["profile"]["wire_format_explicit"] is False
    stored = json.loads((tmp_path / "provider-settings.json").read_text(encoding="utf-8"))
    assert "wire_format" not in stored["providers"][0]


def _client(service: ProviderSettingsService) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_provider_settings_router(service))
    return TestClient(app)


@pytest.mark.parametrize('wire, expected_source', [(None, 'default'), ('responses', 'provider')])
def test_catalog_reports_the_same_route_used_for_dispatch(tmp_path, wire, expected_source):
    service = _service(tmp_path, wire_format=wire)
    with _client(service) as client:
        public = client.get('/api/v1/settings/providers').json()['providers'][0]
        profile, _ = service.resolve_profile('relay')
        assert public['effective_routes']['plain-model'] == {
            'wire_format': resolve_model_connection(profile, 'plain-model').wire_format,
            'source': expected_source,
        }


def test_http_clear_routes_accepts_a_json_body_and_preserves_other_accounts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    record = {"plain-model": {"wire_format": "responses", "tools": True, "verified_at": "2026-10-07"}}
    service._record_routes(base_url=BASE, api_key=KEY, records=record)
    service._record_routes(base_url=BASE, api_key="other-account", records=record)
    client = _client(service)

    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    operation = spec.json()["paths"]["/api/v1/settings/provider/routes"]["delete"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("ProviderRoutesDeleteRequest")

    response = client.request("DELETE", "/api/v1/settings/provider/routes", json={"provider_id": "relay", "base_url": BASE})
    assert response.status_code == 200, response.text
    assert response.json() == {"cleared": 1}
    assert service.catalog_view()["providers"][0]["verified_routes"] == {}
    stored = json.loads(_routes_file(tmp_path).read_text(encoding="utf-8"))
    assert len(stored["routes"]) == 1
    assert next(iter(stored["routes"].values()))["plain-model"]["wire_format"] == "responses"


def test_http_save_without_a_wire_keeps_the_service_on_auto(tmp_path: Path) -> None:
    """10-05 实测：设置页选「自动」不发这个字段，接口却默认填 chat，存成了「明确选了 Chat」。"""
    service = ProviderSettingsService(tmp_path / "provider-settings.json")
    client = _client(service)
    payload = {"provider_id": "relay", "name": "中转站", "base_url": BASE, "model": "plain-model",
               "selected_models": ["plain-model"], "api_key": KEY, "make_active": True}

    created = client.put("/api/v1/settings/providers", json=payload)
    assert created.status_code == 200, created.text
    assert created.json()["profile"]["wire_format_explicit"] is False

    chosen = client.put("/api/v1/settings/providers", json={**payload, "wire_format": "responses"})
    assert chosen.json()["profile"]["wire_format"] == "responses"
    assert chosen.json()["profile"]["wire_format_explicit"] is True

    back = client.put("/api/v1/settings/providers", json={**payload, "wire_format": "auto"})
    assert back.json()["profile"]["wire_format_explicit"] is False
    assert KEY not in back.text


def test_http_liveness_and_single_test_pass_the_form_wire_through(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    client = _client(service)
    calls: list[dict] = []

    def fake_test_models(**kwargs):
        calls.append(kwargs)
        return {"schema": "zhimeng.provider-model-liveness.v1", "tested": 0, "usable_count": 0,
                "usable_models": [], "fastest_model": None, "results": []}

    def fake_test_model(**kwargs):
        calls.append(kwargs)
        return {"schema": "zhimeng.provider-test.v1", "ok": True, "model": kwargs["model"], "reply": "ok",
                "latency_ms": 1, "first_byte_ms": 1, "wire_format": "chat"}

    monkeypatch.setattr(service, "test_models", fake_test_models)
    monkeypatch.setattr(service, "test_model", fake_test_model)

    live = client.post("/api/v1/settings/provider/models/liveness",
                       json={"provider_id": "relay", "base_url": BASE, "models": ["plain-model"], "wire_format": "auto"})
    assert live.status_code == 200, live.text
    single = client.post("/api/v1/settings/provider/test",
                         json={"provider_id": "relay", "base_url": BASE, "model": "plain-model", "prompt": "你好",
                               "wire_format": "responses"})
    assert single.status_code == 200, single.text
    assert [call["wire_format"] for call in calls] == ["auto", "responses"]


def test_hitting_the_probe_token_cap_silently_is_alive_not_a_gateway_error(tmp_path: Path, monkeypatch) -> None:
    """上游不把思考单独报出来、还没开口就撞了测活自己给的输出上限：这是活的线路，不是
    「502 上游网关错误」（作者 2026-10-12 截图：grok-4.7 明明能用，单列测试和测活都误报）。
    """
    service = _service(tmp_path, wire_format="chat")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            return _sse(
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
                "[DONE]",
            )
        return _chat_text("工具结果已收到") if _has_tool_result(body) else _chat_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]
    assert result["usable"] is True
    assert result["wire_format"] == "chat"
    assert result["tools"] == "ok"

    single = service.test_model(
        provider_id="relay", base_url=BASE, api_key=None, model="plain-model",
        prompt="你好", wire_format="chat",
    )
    assert single["ok"] is True


def test_truncated_but_audible_replies_count_as_alive(tmp_path: Path, monkeypatch) -> None:
    """顶满之前已经开口：算能用，注明话没说完，不往「网关错误」上扣。"""
    service = _service(tmp_path, wire_format="chat")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            return _sse(
                {"choices": [{"delta": {"content": "杭州有三样"}}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}]},
                "[DONE]",
            )
        return _chat_text("工具结果已收到") if _has_tool_result(body) else _chat_tool_call()

    _mock_transport(monkeypatch, handler)
    result = service.test_models(provider_id="relay", base_url=BASE, api_key=None, models=["plain-model"], wire_format="chat")["results"][0]
    assert result["usable"] is True
    assert result["wire_format"] == "chat"
    assert result["tools"] == "ok"
    # 关键的回归点：任何一条路都不能再被错标成 http_error（前端据此显示「502 上游网关错误」）
    assert all(item["status"] != "http_error" for item in result["tried"])


def test_reveal_key_returns_the_saved_secret_only_on_explicit_ask(tmp_path: Path) -> None:
    """公开视图永远只给 api_key_set；点「显示已存的密钥」才回明文（作者 2026-10-12）。"""
    service = _service(tmp_path)
    client = _client(service)

    public = client.get("/api/v1/settings/providers")
    assert public.status_code == 200
    assert "api_key\"" not in public.text.replace("api_key_set", "")
    assert KEY not in public.text

    revealed = client.post("/api/v1/settings/providers/relay/reveal-key")
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["api_key"] == KEY

    missing = client.post("/api/v1/settings/providers/nope/reveal-key")
    assert missing.status_code == 404
