"""作者在界面里配置写作模型：接口地址 + 密钥 + 模型。

设计要点：

* 作者不该被要求去改环境变量或 .env。配置存成本地 JSON，界面里填。
* 密钥**只进不出**：保存后任何读取接口都不回传明文，只回传"已设置/未设置"。
* 「一键获取模型」只探测 `/models` 端点，**不调用模型生成**，不产生费用。
* 环境变量仍然有效，作为回退（方便自动化和验收脚本）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from zhimeng_runtime.model_context import normalize_context_window_tier
from zhimeng_runtime.provider_error_messages import upstream_message as _upstream_message
from zhimeng_runtime.provider_url_resolver import (
    resolve_models_url,
    strip_known_suffixes,
)
from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.settings_versions import SettingsConflictError, check_settings_revision, settings_lock, settings_revision

SETTINGS_SCHEMA = "zhimeng.provider-settings.v1"
PROVIDER_CATALOG_SCHEMA = "zhimeng.provider-catalog.v1"
ReasoningEffort = Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"]
REASONING_EFFORTS = frozenset({"auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"})
# 对接口说哪种话：responses 是现代主协议，chat 是老渠道的兼容回退，
# anthropic_messages 是 Anthropic 系渠道的原生协议。
# 缺省保持 chat——作者已在用的渠道不能因为升级就换协议。
WireFormat = Literal["chat", "responses", "anthropic_messages"]
WIRE_FORMATS = frozenset({"chat", "responses", "anthropic_messages"})
# 「自动」不是第四种协议，是「不写死、测活时真试一遍」：试通的那种记进
# provider-routes.json（按 地址+密钥 分开记），以后这条线路上的这个模型就用它。
WIRE_AUTO = "auto"
PROVIDER_ROUTES_SCHEMA = "zhimeng.provider-routes.v1"
# 「自动」挨个试协议时怎么看失败：
# - 这条线路上这个模型没有这种协议（404/405，或连上了却一个字不回）：换下一种试；
# - 跟说哪种协议无关（钥匙不对、网络不通、模型太慢、限流 429）：换了也白等，停下，不记；
# - 别的报错（400、5xx）说不准是协议不对还是碰巧出错：接着试，但这次的结果只有「出字又会
#   调工具」才记——10-05 真测 220，kimi 走 Chat 碰巧报了一次错，自动就换成了 Anthropic，
#   还把这个调不了工具的结果记了下来，而上一轮 Chat 明明出字、调工具都通。
_WIRE_MISSING = frozenset({"not_found", "empty"})
_FAILURES_NOT_ABOUT_WIRE = frozenset({"auth", "timeout", "unreachable"})


def _failure_not_about_wire(outcome: Mapping[str, object]) -> bool:
    return (
        outcome.get("status") in _FAILURES_NOT_ABOUT_WIRE
        or outcome.get("failure_status") in _FAILURES_NOT_ABOUT_WIRE
        or outcome.get("status") == "thinking_only"
        or outcome.get("http_status") == 429
    )


# 认证头怎么带：bearer 是 OpenAI 兼容渠道的现状；x-api-key 是 Anthropic 系习惯；
# custom 表示认证头完全由作者在 extra_headers 里自己写；none 表示不带认证。
AuthType = Literal["bearer", "x-api-key", "custom", "none"]
AUTH_TYPES = frozenset({"bearer", "x-api-key", "custom", "none"})
# RFC 7230 token：HTTP 头名的合法字符集。
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,100}$")


class ProviderSettingsError(RuntimeError):
    code = "provider_settings_failed"
    status_code = 400


class ProviderSettingsReadError(ProviderSettingsError):
    code = "provider_settings_unreadable"
    status_code = 503


class ProviderSettingsConflictError(ProviderSettingsError):
    code = "settings_conflict"
    status_code = 409


class ProviderProbeError(ProviderSettingsError):
    code = "provider_probe_failed"

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class ProviderNotFoundError(ProviderSettingsError):
    code = "provider_not_found"
    status_code = 404


def _normalize_reasoning_effort(raw: object) -> ReasoningEffort:
    value = str(raw or "auto").strip().lower()
    if value not in REASONING_EFFORTS:
        raise ProviderSettingsError("思考强度必须是自动、关闭、低、中、高或极高")
    return value  # type: ignore[return-value]


def _normalize_wire_format(raw: object) -> WireFormat:
    value = str(raw or "chat").strip().lower()
    if value not in WIRE_FORMATS:
        raise ProviderSettingsError("接口协议只能是 chat、responses 或 anthropic_messages")
    return value  # type: ignore[return-value]


# 10-07 作者：中转站对 UA 有黑名单，不能让 Python 默认的 python-httpx 指出去；
# 伪装成 codex-cli（new-api 源码里对 codex 上游就是这么干的，见 codex_models.go:140）。
DEFAULT_PROVIDER_USER_AGENT = "codex-cli/0.55.0"


def _normalize_auth_type(raw: object) -> AuthType:
    value = str(raw or "bearer").strip().lower()
    if value not in AUTH_TYPES:
        raise ProviderSettingsError("认证方式只能是 bearer、x-api-key、custom 或 none")
    return value  # type: ignore[return-value]


def _clean_extra_headers(raw: object) -> dict[str, str]:
    """自定义请求头。值视同密钥，这里只做合法性校验，不做任何展示。"""
    values = raw if isinstance(raw, Mapping) else {}
    headers: dict[str, str] = {}
    for name, value in values.items():
        header_name = str(name or "").strip()
        if not header_name:
            continue
        if not _HEADER_NAME_PATTERN.fullmatch(header_name):
            raise ProviderSettingsError(
                f"请求头名「{header_name}」不合法：只能用字母、数字和 - _ . 等符号，不能有空格"
            )
        header_value = str(value if value is not None else "")
        if any(ord(character) < 32 or ord(character) == 127 for character in header_value):
            raise ProviderSettingsError(f"请求头「{header_name}」的值里有换行或控制字符")
        if len(header_value) > 2000:
            raise ProviderSettingsError(f"请求头「{header_name}」的值太长了")
        headers[header_name] = header_value
        if len(headers) > 32:
            raise ProviderSettingsError("自定义请求头最多 32 个")
    return headers


def _merge_extra_headers(previous: Mapping[str, str], draft: object, remove_names: Sequence[str] | None = None) -> dict[str, str]:
    """Blank secret values retain saved values; HTTP header names ignore casing."""
    merged: dict[str, str] = {}
    names: dict[str, str] = {}
    removed = {name.lower() for name in _clean_extra_headers({name: "" for name in remove_names or ()})}
    for name, value in previous.items():
        if name.lower() in removed:
            continue
        old_name = names.get(name.lower())
        if old_name is not None:
            merged.pop(old_name, None)
        merged[name] = value
        names[name.lower()] = name
    for name, value in _clean_extra_headers(draft).items():
        if not value:
            continue
        old_name = names.get(name.lower())
        if old_name is not None:
            merged.pop(old_name, None)
        merged[name] = value
        names[name.lower()] = name
    return merged


def _clean_timeout_seconds(raw: object, label: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ProviderSettingsError(f"{label}必须是数字（秒）") from None
    if not 1.0 <= value <= 3600.0:
        raise ProviderSettingsError(f"{label}要在 1 到 3600 秒之间")
    return value


def _clean_max_retries(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderSettingsError("重试次数必须是整数")
    if not 0 <= raw <= 10:
        raise ProviderSettingsError("重试次数要在 0 到 10 之间")
    return raw


def _clean_max_concurrent_requests(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderSettingsError("并发上限必须是整数")
    if not 1 <= raw <= 32:
        raise ProviderSettingsError("并发上限要在 1 到 32 之间")
    return raw


def _clean_circuit_breaker_threshold(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderSettingsError("熔断次数必须是整数")
    if not 1 <= raw <= 20:
        raise ProviderSettingsError("熔断次数要在 1 到 20 之间")
    return raw


def _clean_models_url(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        raise ProviderSettingsError("模型列表地址必须以 http:// 或 https:// 开头")
    if len(value) > 500:
        raise ProviderSettingsError("模型列表地址太长了")
    return value


def _models_from_entries(entries: object) -> list[str] | None:
    """从一组条目里提取模型名：字符串，或带 id / model_id 的对象。

    全部条目都不认识时返回 None（让调用方按「形状不符」处理）。
    """
    if not isinstance(entries, list):
        return None
    models: list[str] = []
    recognized = 0
    for entry in entries:
        name: str | None = None
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            for key in ("id", "model_id"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    name = value
                    break
        if name is None:
            continue
        recognized += 1
        cleaned = name.strip()
        if cleaned and len(cleaned) <= 200 and cleaned not in models:
            models.append(cleaned)
    if not recognized and entries:
        return None
    return models


def _shape_summary(payload: object, *, depth: int = 0) -> str:
    """返回形状的键名摘要（绝不含值），给「形状不符」的报错用。"""
    if isinstance(payload, dict):
        keys = "、".join(sorted(str(key) for key in payload)[:8])
        summary = f"对象（键：{keys}）" if keys else "空对象"
        if depth == 0:
            for key in ("data", "models", "items"):
                nested = payload.get(key)
                if nested is not None:
                    summary += f"；{key} 是{_shape_summary(nested, depth=1)}"
        return summary
    if isinstance(payload, list):
        if not payload:
            return "空数组"
        first = payload[0]
        kind = _shape_summary(first, depth=1) if isinstance(first, (dict, list)) else (
            "字符串" if isinstance(first, str) else type(first).__name__
        )
        return f"数组（{len(payload)} 项，首项是{kind}）"
    return type(payload).__name__


def parse_models_response(payload: object) -> list[str]:
    """容错解析模型列表响应。支持的形状：

    1. ``{"data": [{"id": ...}]}``（OpenAI 标准）
    2. ``{"data": ["model-a", ...]}``（字符串数组）
    3. ``{"models": [...]}``（字符串，或带 id / model_id 的对象）
    4. ``{"items": [...]}``（同 models）
    5. 顶层直接是字符串数组

    都不匹配时抛带形状摘要（只有键名，不含值）的错误。
    """
    candidates: list[object] = []
    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if key in payload:
                candidates.append(payload[key])
    for candidate in candidates:
        models = _models_from_entries(candidate)
        if models is not None and (models or not candidate):
            return models
    raise ProviderProbeError(
        "返回的 JSON 形状不认识：" + _shape_summary(payload)
    )


def _clean_reported_model_aliases(raw: object) -> tuple[str, ...]:
    """作者在设置里填的「接受的上报名」。

    身份校验本身不放宽（AGENTS.md 3.49 作者定的边界：中转把模型换成别的，
    作者有权知道）；这里只是把原来只能改启动脚本的接受名单挪到界面里，
    让作者自己能加，不用去改 PowerShell。
    """
    values = raw if isinstance(raw, list) else []
    aliases: list[str] = []
    for item in values:
        alias = str(item or "").strip()
        if not alias:
            continue
        if len(alias) > 512:
            raise ProviderSettingsError("接受的上报名太长了")
        if any(character.isspace() for character in alias):
            raise ProviderSettingsError("接受的上报名里不能有空格")
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases[:64])


@dataclass(frozen=True, slots=True)
class ModelOverride:
    """单个模型对渠道级配置的覆盖。字段全部可选；给了的覆盖渠道级。

    api_key 与 extra_headers 的值同样只进不出：任何视图都不回传明文。
    """

    wire_format: WireFormat | None = None
    base_url: str | None = None
    api_key: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    base_url: str | None
    model: str | None
    api_key: str | None
    reasoning_effort: ReasoningEffort = "auto"
    provider_id: str | None = None
    wire_format: WireFormat = "chat"
    accepted_reported_models: tuple[str, ...] = ()
    auth_type: AuthType = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    models_url: str | None = None
    # None = 用全局 RuntimeSettings 的现状值，不覆盖。
    request_timeout_seconds: float | None = None
    stream_idle_timeout_seconds: float | None = None
    max_retries: int | None = None
    max_output_tokens: int | None = None
    # transport 韧性：None = 用内置默认（并发 4、熔断 5 次 / 冷却 60 秒）。
    max_concurrent_requests: int | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_seconds: float | None = None
    # 上下文窗口档位：256K（默认）/ 1M（最大）。
    context_window: str = "256k"
    # 用户有没有在这条连接上「明确选过」接口协议：False = 还在自动档（允许
    # 能力卡给建议），True = 用户写明了（能力卡不得覆盖）。这个标记只存在内存，
    # 从「配置文件里写没写 wire_format」推出来，不落到 JSON。
    wire_format_explicit: bool = False
    # 10-07 作者：中转站对 UA 有黑名单，默认伪装成 codex-cli；
    # author 显式配的 extra_headers 里带的 User-Agent 优先。
    default_user_agent: str = DEFAULT_PROVIDER_USER_AGENT

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and (self.api_key or self.auth_type in {"custom", "none"}))


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider_id: str
    name: str
    base_url: str | None
    model: str | None
    api_key: str | None
    selected_models: tuple[str, ...] = ()
    reasoning_effort: ReasoningEffort = "auto"
    wire_format: WireFormat = "chat"
    accepted_reported_models: tuple[str, ...] = ()
    auth_type: AuthType = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    models_url: str | None = None
    request_timeout_seconds: float | None = None
    stream_idle_timeout_seconds: float | None = None
    max_retries: int | None = None
    model_overrides: dict[str, ModelOverride] = field(default_factory=dict)
    max_concurrent_requests: int | None = None
    circuit_breaker_threshold: int | None = None
    circuit_breaker_cooldown_seconds: float | None = None
    # 上下文窗口档位：256K（默认）/ 1M（最大）。一个设置决定所有预算。
    context_window: str = "256k"
    # 同 ProviderSettings.wire_format_explicit：配置文件里写没写 wire_format。
    wire_format_explicit: bool = False
    # 10-07 作者：中转站对 UA 有黑名单，默认伪装成 codex-cli；
    # author 显式配的 extra_headers 里带的 User-Agent 优先。
    default_user_agent: str = DEFAULT_PROVIDER_USER_AGENT
    # 测活在「自动」下真测通过的协议：{线路摘要: {模型: {"wire_format", "tools", "verified_at"}}}。
    # 读配置时从 provider-routes.json 带进来，不写回 provider-settings.json。
    verified_routes: Mapping[str, Mapping[str, Mapping[str, object]]] = field(
        default_factory=dict
    )

    @property
    def configured(self) -> bool:
        return resolve_model_connection(self, self.model).configured

    def connection(self) -> ProviderSettings:
        return ProviderSettings(
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            reasoning_effort=self.reasoning_effort,
            provider_id=self.provider_id,
            wire_format=self.wire_format,
            accepted_reported_models=self.accepted_reported_models,
            auth_type=self.auth_type,
            extra_headers=dict(self.extra_headers),
            models_url=self.models_url,
            request_timeout_seconds=self.request_timeout_seconds,
            stream_idle_timeout_seconds=self.stream_idle_timeout_seconds,
            max_retries=self.max_retries,
            max_concurrent_requests=self.max_concurrent_requests,
            circuit_breaker_threshold=self.circuit_breaker_threshold,
            circuit_breaker_cooldown_seconds=self.circuit_breaker_cooldown_seconds,
            context_window=self.context_window,
            wire_format_explicit=self.wire_format_explicit,
            default_user_agent=self.default_user_agent,
        )


def _route_key(
    base_url: str | None, api_key: str | None, *,
    auth_type: str = "bearer", extra_headers: Mapping[str, str] | None = None,
) -> str:
    """同一个地址 + 同一把钥匙算一条线路；只留摘要，不存钥匙本身。

    钥匙也算进来：sub2api 这类中转按钥匙分组，不同钥匙背后的上游可能说不同的协议。
    """
    material = f"{(base_url or '').strip()}\0{(api_key or '').strip()}"
    headers = {
        name.lower(): value for name, value in (extra_headers or {}).items()
        if name.lower() not in {"accept", "content-type"}
        and not (name.lower() == "user-agent" and value == DEFAULT_PROVIDER_USER_AGENT)
    }
    # Preserve legacy bearer/no-custom-header routes. Other credentials form distinct routes.
    if auth_type != "bearer" or headers:
        material += "\0" + json.dumps([auth_type, headers], sort_keys=True, separators=(",", ":"))
    return sha256(material.encode("utf-8")).hexdigest()[:24]


def _connection_route_key(connection: ProviderSettings) -> str:
    return _route_key(connection.base_url, connection.api_key,
                      auth_type=connection.auth_type, extra_headers=connection.extra_headers)


def _verified_wire(
    routes: Mapping[str, Mapping[str, Mapping[str, object]]],
    connection: ProviderSettings,
    model: str | None,
) -> WireFormat | None:
    """这条线路上这个模型测活时真测通过的协议；没测过返回 None。"""
    if not model or not routes:
        return None
    record = (routes.get(_connection_route_key(connection)) or {}).get(model)
    if not isinstance(record, Mapping):
        return None
    wire = str(record.get("wire_format") or "")
    return wire if wire in WIRE_FORMATS else None  # type: ignore[return-value]


def resolve_model_connection(
    profile: ProviderProfile,
    model: str | None,
) -> ProviderSettings:
    """应用模型级覆盖后得到的有效连接。

    优先级（从高到低），同一份能力卡只在「自动档」里给建议、不许覆盖用户明确选择：
      1. 模型级 `model_overrides` 里明确写的字段（wire_format / reasoning_effort /
         max_output_tokens / base_url / api_key / extra_headers）
      2. 渠道级：配置里写明了 `wire_format`（`wire_format_explicit=True`）时的接口协议；
         `reasoning_effort != "auto"` 时的思考强度
      3. 自动档的接口协议：测活时在这条线路（地址+密钥）上真测通过的那种
      4. 能力卡（`model_capabilities.resolve_model_capability`）：只在上面都没有时补
         ——默认 wire / clamp 后的思考强度；max_output_tokens 只有作者明确在渠道或
         模型上写过才使用，不写就留给 `build_agent_provider_from_connection` 的会话级默认。
      5. 兜底：chat / auto / None（不补）
    """
    selected = (model or "").strip() or profile.model
    connection = replace(profile.connection(), model=selected)
    # UA 合进 extra_headers：author 显式带的 User-Agent 优先（在 override.extra_headers 里）；
    # 渠道 override 里没有 User-Agent 就用 default_user_agent（默认 codex-cli）。
    if not any(name.lower() == "user-agent" for name in connection.extra_headers):
        connection = replace(connection, extra_headers={
            "User-Agent": connection.default_user_agent,
            **connection.extra_headers,
        })
    override = profile.model_overrides.get(selected) if selected else None
    if override is not None:
        headers = _merge_extra_headers(profile.extra_headers, override.extra_headers)
        connection = replace(
            connection,
            base_url=override.base_url or connection.base_url,
            api_key=override.api_key or connection.api_key,
            reasoning_effort=override.reasoning_effort or connection.reasoning_effort,
            wire_format=override.wire_format or connection.wire_format,
            extra_headers=headers,
            max_output_tokens=override.max_output_tokens,
            # 模型级对 wire_format 是明确的覆盖，此时整个连接的 wire 也算明确：
            # 它不再是「渠道的 wire」，能力卡不许再改。
            wire_format_explicit=True
            if override.wire_format is not None
            else connection.wire_format_explicit,
        )
    # 渠道级没写明 wire（自动档）：测活真测通过的优先；没测过，能力卡才给个建议。
    verified = (
        None
        if connection.wire_format_explicit
        else _verified_wire(profile.verified_routes, connection, selected)
    )
    if verified is not None:
        connection = replace(connection, wire_format=verified)
    elif not connection.wire_format_explicit:
        try:
            from zhimeng_runtime.model_capabilities import resolve_model_capability

            card = resolve_model_capability(selected)
            if connection.wire_format != card.default_wire:
                connection = replace(connection, wire_format=card.default_wire)
        except Exception:
            pass  # 能力卡查不到就照旧，不挡道。
    # 「自动」就是不向模型指定思考档，交给上游默认；能力卡只建议接口和探测顺序。
    return connection


def _model_override_from(raw: object) -> ModelOverride:
    data = raw if isinstance(raw, Mapping) else {}
    base_url = str(data.get("base_url") or "").strip() or None
    max_tokens = data.get("max_output_tokens")
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ProviderSettingsError("模型的输出上限必须是正整数")
    return ModelOverride(
        wire_format=(
            _normalize_wire_format(data["wire_format"]) if data.get("wire_format") else None
        ),
        base_url=normalize_base_url(base_url) if base_url else None,
        api_key=str(data.get("api_key") or "").strip() or None,
        reasoning_effort=(
            _normalize_reasoning_effort(data["reasoning_effort"])
            if data.get("reasoning_effort")
            else None
        ),
        max_output_tokens=max_tokens,
        extra_headers=_clean_extra_headers(data.get("extra_headers")),
    )


def _model_override_payload(override: ModelOverride) -> dict:
    payload: dict = {}
    if override.wire_format:
        payload["wire_format"] = override.wire_format
    if override.base_url:
        payload["base_url"] = override.base_url
    if override.api_key:
        payload["api_key"] = override.api_key
    if override.reasoning_effort:
        payload["reasoning_effort"] = override.reasoning_effort
    if override.max_output_tokens is not None:
        payload["max_output_tokens"] = override.max_output_tokens
    if override.extra_headers:
        payload["extra_headers"] = dict(override.extra_headers)
    return payload


def _model_overrides_view(overrides: Mapping[str, ModelOverride]) -> dict:
    """连接参数可回显；密钥和请求头的值只进不出。"""
    view: dict = {}
    for model in sorted(overrides):
        override = overrides[model]
        fields: list[str] = []
        if override.wire_format:
            fields.append("wire_format")
        if override.base_url:
            fields.append("base_url")
        if override.api_key:
            fields.append("api_key")
        if override.reasoning_effort:
            fields.append("reasoning_effort")
        if override.max_output_tokens is not None:
            fields.append("max_output_tokens")
        if override.extra_headers:
            fields.append("extra_headers")
        view[model] = {
            "overridden_fields": fields,
            "api_key_set": bool(override.api_key),
            "extra_header_names": sorted(override.extra_headers),
            "wire_format": override.wire_format,
            "base_url": override.base_url,
            "reasoning_effort": override.reasoning_effort,
            "max_output_tokens": override.max_output_tokens,
        }
    return view


def normalize_base_url(raw: str) -> str:
    """把作者可能填错的地址收敛成可用的 base。

    允许作者直接粘贴 `https://x.com`、`https://x.com/v1`、
    甚至 `https://x.com/v1/chat/completions`。
    """
    value = strip_known_suffixes(raw)
    if not value:
        raise ProviderSettingsError("接口地址不能为空")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderSettingsError("接口地址必须以 http:// 或 https:// 开头")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderSettingsError("接口地址不能包含账号密码、查询参数或 # 片段")
    return value


def _friendly_probe_error(status_code: int | None, detail: str) -> str:
    if status_code in (401, 403):
        return _friendly_model_probe_error(status_code, detail)
    if status_code in (404, 405):
        return (
            f"地址能连上，但模型列表接口不存在或不接受这个请求（{status_code}）。"
            "多数服务要把地址填到 /v1 结尾，例如 https://xxx.com/v1。"
        )
    if status_code is not None and status_code >= 500:
        return f"对方服务器出错（{status_code}），稍后再试。"
    return (detail or "获取模型列表失败。")[:260]


def _friendly_model_probe_error(status_code: int, excerpt: str) -> str:
    """测活时某个模型的请求被拒：说的是「这个模型」，不是「拉模型列表」那一步（9-26：404 被说成「地址没有 /models 接口」）。"""
    said = _upstream_message(excerpt)
    tail = f"服务返回：{said}" if said else ""
    if status_code == 401:
        return "认证未通过（401）。请核对密钥与认证方式。" + tail
    if status_code == 403:
        return "服务拒绝访问（403）。请按返回原因检查账号状态或访问权限。" + tail
    if status_code in (404, 405):
        return f"中转站说没有这个模型（{status_code}）：可能名字不对，或者这把密钥不让用它。" + tail
    if status_code == 429:
        return "请求太频繁或额度用完了（429），过一会儿再测。" + tail
    if status_code >= 500:
        return f"中转站后面的模型服务出错（{status_code}），稍后再试。" + tail
    return (tail or f"服务返回了错误（{status_code}）。")[:260]


def _discovery_not_fatal(reason: str) -> str:
    """模型发现失败不判死：列表拉不到 ≠ 模型不能用。"""
    return (
        f"模型列表拉不到（{reason}）。"
        "这不代表模型不能用——可以手动填模型名，然后点「测试」直接验证。"
    )


def _catalog_update(method):
    @wraps(method)
    def update(self, *args, **kwargs):
        # 从读取旧值到落盘共用锁，save() 转调 save_profile() 时可重入。
        with self._catalog_lock:
            outer = self._write_depth == 0
            if outer:
                self._write_snapshot = self._read_file()
            self._write_depth += 1
            try:
                return method(self, *args, **kwargs)
            except SettingsConflictError as exc:
                raise ProviderSettingsConflictError(str(exc)) from None
            finally:
                self._write_depth -= 1
                if outer:
                    self._write_snapshot = None
    return update


class ProviderSettingsService:
    def __init__(
        self,
        settings_path: Path,
        *,
        fallback: ProviderSettings | None = None,
        fallback_credential_reference: str | None = None,
    ) -> None:
        self._path = settings_path
        self._fallback = fallback
        self._fallback_credential_reference = fallback_credential_reference
        self._routes_lock = threading.Lock()
        self._catalog_lock = settings_lock(settings_path)
        self._write_depth = 0
        self._write_snapshot: dict | None = None

    # ---------------------------------------------------------------- 存取
    def _read_file(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ProviderSettingsReadError("模型配置暂时无法读取，原文件已保留。请检查文件内容、占用或权限后重试。") from None
        if not isinstance(data, dict):
            raise ProviderSettingsReadError("模型配置格式不正确，原文件已保留；请修复后重试。")
        if "providers" in data:
            profiles = data["providers"]
            if not isinstance(profiles, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("provider_id"), str)
                or not item["provider_id"].strip() for item in profiles
            ):
                raise ProviderSettingsReadError("模型服务目录格式不正确，原文件已保留；请修复后重试。")
            # Persisted identities must stay stable across reads. The legacy ID
            # normalizer generates random IDs for invalid input and must never
            # silently repair a saved resource or collapse whitespace aliases.
            if any(item["provider_id"] != item["provider_id"].strip()
                   or len(item["provider_id"]) > 80
                   or not all(char.isalnum() or char in "-_" for char in item["provider_id"])
                   for item in profiles):
                raise ProviderSettingsReadError("模型服务编号格式不正确，原文件已保留；请修复后重试。")
            if len({item["provider_id"] for item in profiles}) != len(profiles):
                raise ProviderSettingsReadError("模型服务目录含重复编号，原文件已保留；请修复后重试。")
        return data

    def _profile_revision(self, profile: ProviderProfile, stored: dict) -> str:
        raw = next((item for item in stored.get("providers", []) if item.get("provider_id") == profile.provider_id), None)
        return settings_revision(raw if raw is not None else {"source": "settings" if stored else "environment", "profile": self._profile_payload(profile)})

    def _check_profile_revision(self, expected: str | None, profile: ProviderProfile | None, stored: dict) -> None:
        check_settings_revision(expected, self._profile_revision(profile, stored) if profile else "missing")

    # 测活在「自动」下测通的协议单独放一个文件：它是测出来的事实，不是作者填的配置，
    # 和 provider-settings.json 分开，保存配置时不会把它冲掉，也不会把它当成「作者选的」。
    def _routes_path(self) -> Path:
        return self._path.with_name("provider-routes.json")

    def _read_routes(self) -> dict[str, dict[str, dict[str, object]]]:
        path = self._routes_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("routes") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        routes: dict[str, dict[str, dict[str, object]]] = {}
        for key, models in raw.items():
            if not isinstance(key, str) or not isinstance(models, dict):
                continue
            routes[key] = {
                str(model): dict(record)
                for model, record in models.items()
                if isinstance(record, dict)
                and str(record.get("wire_format") or "") in WIRE_FORMATS
            }
        return routes

    def _record_routes(
        self,
        *,
        base_url: str,
        api_key: str,
        records: Mapping[str, Mapping[str, object]],
        auth_type: str = "bearer",
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        if not records:
            return
        with self._routes_lock:
            routes = self._read_routes()
            bucket = routes.setdefault(_route_key(base_url, api_key, auth_type=auth_type, extra_headers=extra_headers), {})
            for model, record in records.items():
                bucket[model] = dict(record)
            path = self._routes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schema": PROVIDER_ROUTES_SCHEMA, "routes": routes},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            replace_with_retry(temporary, path)

    def clear_verified_routes(
        self,
        *,
        base_url: str,
        api_key: str,
        provider_id: str | None = None,
        auth_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
    ) -> int:
        """清空这条线路（地址+密钥摘要）的测活记忆。返回清掉的模型数。

        用于「重测全部」：用户改了 key 或想从头重新测时，忘掉以前测通过的协议。
        只清这一条线路的，不动别的服务/账号。
        """
        profile = self._saved_profile(provider_id)
        base = normalize_base_url(base_url)
        connection = self._test_connection(profile, base=base, key=api_key, model="",
                                           auth_type=auth_type, extra_headers=extra_headers, remove_header_names=remove_header_names)
        keys = {_connection_route_key(connection)}
        if profile is not None:
            for model in profile.selected_models:
                connection = self._test_connection(profile, base=base, key=api_key, model=model,
                                                   auth_type=auth_type, extra_headers=extra_headers, remove_header_names=remove_header_names,
                                                   test_scope="saved_model")
                keys.add(_connection_route_key(connection))
        with self._routes_lock:
            routes = self._read_routes()
            count = sum(len(routes.pop(key, {})) for key in keys)
            path = self._routes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schema": PROVIDER_ROUTES_SCHEMA, "routes": routes},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            replace_with_retry(temporary, path)
            return count

    def _fallback_settings(self) -> ProviderSettings:
        return self._fallback or ProviderSettings(
            base_url=os.environ.get("ZHIMENG_AGENT_PROVIDER_BASE_URL") or None,
            model=os.environ.get("ZHIMENG_AGENT_PROVIDER_MODEL") or None,
            api_key=os.environ.get("ZHIMENG_AGENT_PROVIDER_API_KEY") or None,
            reasoning_effort=_normalize_reasoning_effort(
                os.environ.get("ZHIMENG_AGENT_PROVIDER_REASONING_EFFORT") or "auto"
            ),
        )

    @staticmethod
    def _clean_models(raw: object, *, default_model: str | None = None) -> tuple[str, ...]:
        values = raw if isinstance(raw, list) else []
        models: list[str] = []
        for item in values:
            model = str(item or "").strip()
            if model and len(model) <= 200 and model not in models:
                models.append(model)
        selected = str(default_model or "").strip()
        if selected and selected not in models:
            models.insert(0, selected)
        return tuple(models[:200])

    @staticmethod
    def _profile_id(raw: object) -> str:
        value = str(raw or "").strip()
        if value and len(value) <= 80 and all(
            character.isalnum() or character in "-_" for character in value
        ):
            return value
        return f"provider-{uuid4().hex[:12]}"

    def _profiles_from(self, stored: dict) -> tuple[list[ProviderProfile], str | None]:
        try:
            return self._parse_profiles(stored)
        except (ProviderSettingsError, ValueError, TypeError):
            # Validation failures in saved input must leave the runtime usable
            # for history and repair; they are not invalid incoming requests.
            raise ProviderSettingsReadError("模型配置中的字段格式不正确，原文件已保留；请修复后重试。") from None

    def _parse_profiles(self, stored: dict) -> tuple[list[ProviderProfile], str | None]:
        profiles: list[ProviderProfile] = []
        routes = self._read_routes()
        raw_profiles = stored.get("providers")
        if isinstance(raw_profiles, list):
            seen: set[str] = set()
            for item in raw_profiles:
                if not isinstance(item, dict):
                    continue
                provider_id = self._profile_id(item.get("provider_id"))
                if provider_id in seen:
                    continue
                seen.add(provider_id)
                model = str(item.get("model") or "").strip() or None
                base_url = str(item.get("base_url") or "").strip() or None
                selected_models = self._clean_models(
                    item.get("selected_models"), default_model=model
                )
                overrides: dict[str, ModelOverride] = {}
                raw_overrides = item.get("model_overrides")
                if isinstance(raw_overrides, Mapping):
                    for override_model, raw_override in raw_overrides.items():
                        override_model = str(override_model or "").strip()
                        # 未勾选模型的连接配置仍保留，重新选回和旧会话都可能用到。
                        if override_model:
                            overrides[override_model] = _model_override_from(raw_override)
                profiles.append(
                    ProviderProfile(
                        provider_id=provider_id,
                        name=str(item.get("name") or "模型服务").strip()[:80]
                        or "模型服务",
                        base_url=base_url,
                        model=model,
                        api_key=str(item.get("api_key") or "").strip() or None,
                        selected_models=selected_models,
                        reasoning_effort=_normalize_reasoning_effort(
                            item.get("reasoning_effort") or "auto"
                        ),
                        wire_format=_normalize_wire_format(
                            item.get("wire_format") or "chat"
                        ),
                        wire_format_explicit=bool(item.get("wire_format")),
                        accepted_reported_models=_clean_reported_model_aliases(
                            item.get("accepted_reported_models")
                        ),
                        auth_type=_normalize_auth_type(item.get("auth_type")),
                        extra_headers=_clean_extra_headers(item.get("extra_headers")),
                        models_url=_clean_models_url(item.get("models_url")),
                        request_timeout_seconds=_clean_timeout_seconds(
                            item.get("request_timeout_seconds"), "请求超时"
                        ),
                        stream_idle_timeout_seconds=_clean_timeout_seconds(
                            item.get("stream_idle_timeout_seconds"), "流式空闲超时"
                        ),
                        max_retries=_clean_max_retries(item.get("max_retries")),
                        model_overrides=overrides,
                        max_concurrent_requests=_clean_max_concurrent_requests(
                            item.get("max_concurrent_requests")
                        ),
                        context_window=normalize_context_window_tier(
                            item.get("context_window")
                        ),
                        circuit_breaker_threshold=_clean_circuit_breaker_threshold(
                            item.get("circuit_breaker_threshold")
                        ),
                        circuit_breaker_cooldown_seconds=_clean_timeout_seconds(
                            item.get("circuit_breaker_cooldown_seconds"), "熔断冷却"
                        ),
                        verified_routes=routes,
                    )
                )
            active_id = str(stored.get("active_provider_id") or "").strip() or None
            if active_id not in {item.provider_id for item in profiles}:
                active_id = profiles[0].provider_id if profiles else None
            return profiles, active_id

        # v1 自动迁移视图：旧配置仍然原样可用，第一次保存多上游时再写成 catalog。
        effective = self._effective_from(stored)
        if effective.base_url or effective.model or effective.api_key:
            source_name = "原有模型服务" if stored else "环境变量模型服务"
            profiles.append(
                ProviderProfile(
                    provider_id="default",
                    name=source_name,
                    base_url=effective.base_url,
                    model=effective.model,
                    api_key=effective.api_key,
                    selected_models=self._clean_models([], default_model=effective.model),
                    reasoning_effort=effective.reasoning_effort,
                    wire_format=effective.wire_format,
                    accepted_reported_models=effective.accepted_reported_models,
                    verified_routes=routes,
                )
            )
            return profiles, "default"
        return [], None

    @staticmethod
    def _profile_payload(profile: ProviderProfile) -> dict:
        payload: dict = {
            "provider_id": profile.provider_id,
            "name": profile.name,
            "base_url": profile.base_url or "",
            "model": profile.model or "",
            "api_key": profile.api_key or "",
            "selected_models": list(profile.selected_models),
            "reasoning_effort": profile.reasoning_effort,
            "accepted_reported_models": list(profile.accepted_reported_models),
        }
        # wire_format 只在用户明确写过时才落盘。没写过就是不写——下次读
        # 配置文件时 wire_format_explicit 才是 False，自动档（能力卡）才有权给建议。
        # 这同时避免「保存一次默认 chat 也覆盖」把所有人锁死在 chat。
        if profile.wire_format_explicit:
            payload["wire_format"] = profile.wire_format
        # 新字段只在非默认时落盘：旧版本读这份文件时不受干扰，文件也保持干净。
        if profile.auth_type != "bearer":
            payload["auth_type"] = profile.auth_type
        if profile.extra_headers:
            payload["extra_headers"] = dict(profile.extra_headers)
        if profile.models_url:
            payload["models_url"] = profile.models_url
        if profile.request_timeout_seconds is not None:
            payload["request_timeout_seconds"] = profile.request_timeout_seconds
        if profile.stream_idle_timeout_seconds is not None:
            payload["stream_idle_timeout_seconds"] = profile.stream_idle_timeout_seconds
        if profile.max_retries is not None:
            payload["max_retries"] = profile.max_retries
        if profile.max_concurrent_requests is not None:
            payload["max_concurrent_requests"] = profile.max_concurrent_requests
        if profile.context_window != "256k":
            payload["context_window"] = profile.context_window
        if profile.circuit_breaker_threshold is not None:
            payload["circuit_breaker_threshold"] = profile.circuit_breaker_threshold
        if profile.circuit_breaker_cooldown_seconds is not None:
            payload["circuit_breaker_cooldown_seconds"] = (
                profile.circuit_breaker_cooldown_seconds
            )
        if profile.model_overrides:
            payload["model_overrides"] = {
                model: _model_override_payload(profile.model_overrides[model])
                for model in sorted(profile.model_overrides)
            }
        return payload

    def _write_catalog(
        self,
        profiles: list[ProviderProfile],
        active_provider_id: str | None,
    ) -> None:
        original = self._write_snapshot if self._write_snapshot is not None else self._read_file()
        raw_profiles = {item["provider_id"]: item for item in original.get("providers", [])}
        known_fields = {item.name for item in fields(ProviderProfile)}
        # Keep future/extension fields while replacing every known field, so
        # resets (e.g. explicit wire format -> auto) really remove the old value.
        payload = {
            **original,
            "schema": PROVIDER_CATALOG_SCHEMA,
            "active_provider_id": active_provider_id,
            "providers": [
                {**{key: value for key, value in raw_profiles.get(profile.provider_id, {}).items() if key not in known_fields},
                 **self._profile_payload(profile)} for profile in profiles
            ],
        }
        # Legacy singleton fields must not resurrect a deleted catalog through
        # the environment/legacy fallback. Unknown root extensions stay intact.
        for name in known_fields - {"schema", "providers", "active_provider_id"}:
            payload.pop(name, None)
        self._write_file(payload)

    def _write_file(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self._write_snapshot is not None and self._read_file() != self._write_snapshot:
                raise SettingsConflictError()
            replace_with_retry(temporary, self._path)
            if self._write_depth:
                self._write_snapshot = payload
        finally:
            temporary.unlink(missing_ok=True)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def _effective_from(self, stored: dict) -> ProviderSettings:
        fallback = self._fallback_settings()
        return ProviderSettings(
            base_url=stored.get("base_url")
            or fallback.base_url
            or None,
            model=stored.get("model")
            or fallback.model
            or None,
            api_key=stored.get("api_key")
            or fallback.api_key
            or None,
            reasoning_effort=_normalize_reasoning_effort(
                stored.get("reasoning_effort") or fallback.reasoning_effort
            ),
            wire_format=_normalize_wire_format(
                stored.get("wire_format") or fallback.wire_format
            ),
            accepted_reported_models=_clean_reported_model_aliases(
                stored.get("accepted_reported_models")
            )
            or fallback.accepted_reported_models,
        )

    def _credential_reference_from(
        self,
        stored: dict,
        effective: ProviderSettings,
    ) -> str | None:
        if not effective.api_key:
            return None
        material = (
            f"{self._path.resolve(strict=False)}\0{effective.api_key}"
        ).encode("utf-8")
        if stored.get("api_key") or self._profiles_from(self._read_file())[0]:
            return f"settings:provider-key:{sha256(material).hexdigest()}"
        if self._fallback_credential_reference:
            return self._fallback_credential_reference
        environment_reference = os.environ.get(
            "ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE"
        )
        if environment_reference:
            return environment_reference
        material = f"environment\0{effective.api_key}".encode("utf-8")
        return f"environment:provider-key:{sha256(material).hexdigest()}"

    def effective(self) -> ProviderSettings:
        """界面里配的优先；没配就回退到环境变量。"""
        stored = self._read_file()
        profiles, active_id = self._profiles_from(stored)
        if profiles:
            active = next(
                (profile for profile in profiles if profile.provider_id == active_id),
                profiles[0],
            )
            return active.connection()
        return self._effective_from(stored)

    def credential_reference(self) -> str | None:
        """返回不含密钥明文、但能识别密钥变更的本地引用。"""
        return self.resolve_agent_connection()[1]

    def resolve_agent_connection(self) -> tuple[ProviderSettings, str | None]:
        """一次读取内同时解析连接与密钥身份，避免保存时产生混合快照。"""
        return self.resolve_agent_profile(None)

    def resolve_agent_profile(
        self,
        provider_id: str | None,
    ) -> tuple[ProviderSettings, str | None]:
        """按上游 ID 解析连接；未指定时使用默认上游。"""
        profile, credential_reference = self.resolve_profile(provider_id)
        if profile is not None:
            return profile.connection(), credential_reference
        if provider_id not in (None, "default"):
            return ProviderSettings(None, None, None), None
        stored = self._read_file()
        effective = self._effective_from(stored)
        return effective, self._credential_reference_from(stored, effective)

    def resolve_profile(
        self,
        provider_id: str | None,
    ) -> tuple[ProviderProfile | None, str | None]:
        """按上游 ID 解析完整渠道配置（含模型级覆盖）；未指定时使用默认上游。

        返回 (None, None) 表示没有界面配置的渠道——调用方应回退到
        resolve_agent_profile 走 v1/环境变量路径。
        """
        stored = self._read_file()
        profiles, active_id = self._profiles_from(stored)
        if not profiles:
            return None, None
        wanted = provider_id or active_id
        profile = next(
            (item for item in profiles if item.provider_id == wanted),
            None,
        )
        if profile is None:
            return None, None
        if profile.api_key:
            return profile, self._credential_reference_from({"api_key": profile.api_key}, profile.connection())
        return profile, self._credential_reference_from(
            {"api_key": profile.api_key}, resolve_model_connection(profile, profile.model)
        )

    def catalog_view(self, stored: dict | None = None) -> dict:
        """全部模型服务的公开清单。密钥只显示是否存在。"""
        stored = self._read_file() if stored is None else stored
        profiles, active_id = self._profiles_from(stored)
        return {
            "schema": PROVIDER_CATALOG_SCHEMA,
            "revision": settings_revision(stored),
            "active_settings": self.public_view(stored),
            "active_provider_id": active_id,
            "providers": [
                {
                    "provider_id": profile.provider_id,
                    "revision": self._profile_revision(profile, stored),
                    "source": "settings" if stored else "environment",
                    "applies_to": "next_request",
                    "name": profile.name,
                    "base_url": profile.base_url or "",
                    "model": profile.model or "",
                    "selected_models": list(profile.selected_models),
                    "reasoning_effort": profile.reasoning_effort,
                    "wire_format": profile.wire_format,
                    "wire_format_explicit": profile.wire_format_explicit,
                    "accepted_reported_models": list(profile.accepted_reported_models),
                    "auth_type": profile.auth_type,
                    # 自定义头的值视同密钥：只回头名列表，不回值
                    "extra_header_names": sorted(profile.extra_headers),
                    "models_url": profile.models_url or "",
                    "request_timeout_seconds": profile.request_timeout_seconds,
                    "stream_idle_timeout_seconds": profile.stream_idle_timeout_seconds,
                    "max_retries": profile.max_retries,
                    "max_concurrent_requests": profile.max_concurrent_requests,
                    "circuit_breaker_threshold": profile.circuit_breaker_threshold,
                    "circuit_breaker_cooldown_seconds": (
                        profile.circuit_breaker_cooldown_seconds
                    ),
                    "model_overrides": _model_overrides_view(profile.model_overrides),
                    "verified_routes": self._verified_routes_view(profile),
                    "effective_routes": self._effective_routes_view(profile),
                    "context_window": profile.context_window,
                    "api_key_set": bool(profile.api_key),
                    "configured": profile.configured,
                    "active": profile.provider_id == active_id,
                }
                for profile in profiles
            ],
        }

    @staticmethod
    def _effective_routes_view(profile: ProviderProfile) -> dict:
        result = {}
        for model in dict.fromkeys((profile.model, *profile.selected_models)):
            if not model:
                continue
            connection = resolve_model_connection(profile, model)
            override = profile.model_overrides.get(model)
            verified = _verified_wire(profile.verified_routes, connection, model)
            source = (
                "model_override" if override and override.wire_format
                else "provider" if profile.wire_format_explicit
                else "verified" if verified else "default"
            )
            result[model] = {"wire_format": connection.wire_format, "source": source}
        return result

    @staticmethod
    def _verified_routes_view(profile: ProviderProfile) -> dict:
        """这条线路上已选模型测活测出来的协议，给设置页显示「自动：走哪种」。"""
        result = {}
        for model in profile.selected_models:
            connection = resolve_model_connection(profile, model)
            bucket = profile.verified_routes.get(_connection_route_key(connection)) or {}
            record = bucket.get(model)
            if record is None:
                continue
            result[model] = {
                "wire_format": record.get("wire_format"),
                "tools": bool(record.get("tools")),
                "verified_at": record.get("verified_at"),
            }
        return result

    def public_view(self, stored: dict | None = None) -> dict:
        """给界面看的状态。**绝不回传密钥明文。**"""
        stored = self._read_file() if stored is None else stored
        profiles, active_id = self._profiles_from(stored)
        active = next(
            (profile for profile in profiles if profile.provider_id == active_id),
            None,
        )
        effective = active.connection() if active else self._effective_from(stored)
        return {
            "schema": SETTINGS_SCHEMA,
            "revision": settings_revision(stored),
            "applies_to": "new_sessions",
            "provider_id": active.provider_id if active else None,
            "provider_name": active.name if active else "",
            "base_url": effective.base_url or "",
            "model": effective.model or "",
            "selected_models": list(active.selected_models) if active else (
                [effective.model] if effective.model else []
            ),
            "reasoning_effort": effective.reasoning_effort,
            "wire_format": active.wire_format if active else effective.wire_format,
            "accepted_reported_models": list(
                active.accepted_reported_models
                if active
                else effective.accepted_reported_models
            ),
            "auth_type": active.auth_type if active else effective.auth_type,
            "extra_header_names": sorted(
                active.extra_headers if active else effective.extra_headers
            ),
            "models_url": (active.models_url if active else effective.models_url) or "",
            "request_timeout_seconds": (
                active.request_timeout_seconds if active else None
            ),
            "stream_idle_timeout_seconds": (
                active.stream_idle_timeout_seconds if active else None
            ),
            "max_retries": active.max_retries if active else None,
            "max_concurrent_requests": (
                active.max_concurrent_requests if active else None
            ),
            "circuit_breaker_threshold": (
                active.circuit_breaker_threshold if active else None
            ),
            "circuit_breaker_cooldown_seconds": (
                active.circuit_breaker_cooldown_seconds if active else None
            ),
            "model_overrides": _model_overrides_view(
                active.model_overrides if active else {}
            ),
            "context_window": (
                active.context_window if active else effective.context_window
            ),
            "api_key_set": bool(effective.api_key),
            # 让作者知道当前这份配置是界面里存的还是环境变量来的
            "source": "settings" if (profiles and stored) or stored.get("base_url") else ("environment" if effective.base_url else "none"),
            "configured": effective.configured,
        }

    @_catalog_update
    def save(
        self,
        *,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
        reasoning_effort: str | None = None,
        wire_format: str | None = None,
        accepted_reported_models: list[str] | None = None,
        context_window: str | None = None,
        expected_revision: str | None = None,
    ) -> dict:
        stored = self._read_file()
        check_settings_revision(expected_revision, settings_revision(stored))
        profiles, active_id = self._profiles_from(stored)
        if isinstance(stored.get("providers"), list) or profiles:
            current = next(
                (profile for profile in profiles if profile.provider_id == active_id),
                profiles[0] if profiles else None,
            )
            return self.save_profile(
                provider_id=current.provider_id if current else None,
                name=current.name if current else "默认模型服务",
                base_url=base_url,
                model=model,
                api_key=api_key,
                selected_models=None,
                reasoning_effort=reasoning_effort,
                wire_format=wire_format,
                accepted_reported_models=accepted_reported_models,
                context_window=context_window,
                make_active=True,
            )["active_settings"]
        if base_url is not None:
            stored["base_url"] = normalize_base_url(base_url)
        if model is not None:
            stored["model"] = model.strip()
        if reasoning_effort is not None:
            stored["reasoning_effort"] = _normalize_reasoning_effort(reasoning_effort)
        if wire_format is not None:
            stored["wire_format"] = _normalize_wire_format(wire_format)
        if context_window is not None:
            stored["context_window"] = normalize_context_window_tier(context_window)
        if accepted_reported_models is not None:
            stored["accepted_reported_models"] = list(
                _clean_reported_model_aliases(accepted_reported_models)
            )
        # api_key 传 None 表示"不修改"，传空串表示"清除"
        if api_key is not None:
            cleaned = api_key.strip()
            if cleaned:
                stored["api_key"] = cleaned
            else:
                stored.pop("api_key", None)
        stored["schema"] = SETTINGS_SCHEMA

        self._write_file(stored)
        return self.public_view()

    @_catalog_update
    def save_profile(
        self,
        *,
        provider_id: str | None,
        name: str,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
        selected_models: list[str] | None,
        reasoning_effort: str | None,
        wire_format: str | None = None,
        accepted_reported_models: list[str] | None = None,
        auth_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
        models_url: str | None = None,
        request_timeout_seconds: float | None = None,
        stream_idle_timeout_seconds: float | None = None,
        max_retries: int | None = None,
        max_concurrent_requests: int | None = None,
        circuit_breaker_threshold: int | None = None,
        circuit_breaker_cooldown_seconds: float | None = None,
        model_overrides: Mapping[str, Mapping[str, object]] | None = None,
        context_window: str | None = None,
        make_active: bool,
        expected_revision: str | None = None,
    ) -> dict:
        stored = self._read_file()
        profiles, active_id = self._profiles_from(stored)
        normalized_id = self._profile_id(provider_id)
        index = next(
            (offset for offset, item in enumerate(profiles) if item.provider_id == normalized_id),
            None,
        )
        current = profiles[index] if index is not None else None
        self._check_profile_revision(expected_revision, current, stored)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ProviderSettingsError("请给这个模型服务起个名字")
        next_base_url = (
            normalize_base_url(base_url)
            if base_url is not None
            else (current.base_url if current else None)
        )
        next_model = (
            str(model).strip()
            if model is not None
            else (current.model if current else None)
        ) or None
        if api_key is None:
            next_key = current.api_key if current else None
        else:
            next_key = api_key.strip() or None
        models = self._clean_models(
            selected_models if selected_models is not None else (
                list(current.selected_models) if current else []
            ),
            default_model=next_model,
        )
        if next_model and next_model not in models:
            models = (next_model, *models)
        # 自定义请求头：不传 = 保持原样；某名字给非空值 = 设置/更新；
        # 某名字给空值 = 保留旧值（与 api_key「留空不修改」同语义）。
        next_headers = _merge_extra_headers(current.extra_headers if current else {}, extra_headers, remove_header_names)
        # 本次提供的覆盖须对应已选模型；未提供时保留原配置，便于取消勾选后再选回。
        # 字段级合并——
        # 这次没给的字段保留该模型原有覆盖，给了的才改；
        # api_key 与自定义头的值遵循同样的「留空保留旧值」语义。
        if model_overrides is None:
            next_overrides = dict(current.model_overrides) if current else {}
        else:
            previous_overrides = dict(current.model_overrides) if current else {}
            next_overrides = {}
            for override_model, raw_override in model_overrides.items():
                override_model = str(override_model or "").strip()
                if not override_model:
                    continue
                previous = previous_overrides.get(override_model)
                parsed = _model_override_from(raw_override)
                raw_headers = (
                    raw_override.get("extra_headers")
                    if isinstance(raw_override, Mapping)
                    else None
                )
                merged_headers = _merge_extra_headers(previous.extra_headers if previous else {}, raw_headers)
                next_overrides[override_model] = ModelOverride(
                    wire_format=parsed.wire_format or (
                        previous.wire_format if previous else None
                    ),
                    base_url=parsed.base_url or (
                        previous.base_url if previous else None
                    ),
                    api_key=parsed.api_key or (
                        previous.api_key if previous else None
                    ),
                    reasoning_effort=parsed.reasoning_effort or (
                        previous.reasoning_effort if previous else None
                    ),
                    max_output_tokens=(
                        parsed.max_output_tokens
                        if parsed.max_output_tokens is not None
                        else (previous.max_output_tokens if previous else None)
                    ),
                    extra_headers=merged_headers,
                )
        unknown_overrides = sorted(set(next_overrides) - set(models)) if model_overrides is not None else []
        if unknown_overrides:
            raise ProviderSettingsError(
                "模型级覆盖里有没有选中的模型："
                + "、".join(unknown_overrides)
                + "。先把模型加进列表，再给它配覆盖。"
            )
        # 接口协议：给了具体的一种 = 作者明确选的；给「auto」= 回到自动（以前明确选过也取消）；
        # 不给 = 保持原样（新服务就是自动）。
        wire_choice = str(wire_format).strip().lower() if wire_format is not None else ""
        if wire_choice == WIRE_AUTO:
            next_wire, next_wire_explicit = "chat", False
        elif wire_choice:
            next_wire, next_wire_explicit = _normalize_wire_format(wire_choice), True
        else:
            next_wire = current.wire_format if current else "chat"
            next_wire_explicit = current.wire_format_explicit if current else False
        profile = ProviderProfile(
            provider_id=normalized_id,
            name=clean_name[:80],
            base_url=next_base_url,
            model=next_model,
            api_key=next_key,
            selected_models=models,
            reasoning_effort=_normalize_reasoning_effort(
                reasoning_effort or (current.reasoning_effort if current else "auto")
            ),
            wire_format=next_wire,
            wire_format_explicit=next_wire_explicit,
            accepted_reported_models=(
                _clean_reported_model_aliases(accepted_reported_models)
                if accepted_reported_models is not None
                else (current.accepted_reported_models if current else ())
            ),
            auth_type=_normalize_auth_type(
                auth_type or (current.auth_type if current else "bearer")
            ),
            context_window=(
                normalize_context_window_tier(context_window)
                if context_window is not None
                else (current.context_window if current else "256k")
            ),
            extra_headers=next_headers,
            models_url=(
                _clean_models_url(models_url)
                if models_url is not None
                else (current.models_url if current else None)
            ),
            request_timeout_seconds=(
                _clean_timeout_seconds(request_timeout_seconds, "请求超时")
                if request_timeout_seconds is not None
                else (current.request_timeout_seconds if current else None)
            ),
            stream_idle_timeout_seconds=(
                _clean_timeout_seconds(stream_idle_timeout_seconds, "流式空闲超时")
                if stream_idle_timeout_seconds is not None
                else (current.stream_idle_timeout_seconds if current else None)
            ),
            max_retries=(
                _clean_max_retries(max_retries)
                if max_retries is not None
                else (current.max_retries if current else None)
            ),
            max_concurrent_requests=(
                _clean_max_concurrent_requests(max_concurrent_requests)
                if max_concurrent_requests is not None
                else (current.max_concurrent_requests if current else None)
            ),
            circuit_breaker_threshold=(
                _clean_circuit_breaker_threshold(circuit_breaker_threshold)
                if circuit_breaker_threshold is not None
                else (current.circuit_breaker_threshold if current else None)
            ),
            circuit_breaker_cooldown_seconds=(
                _clean_timeout_seconds(
                    circuit_breaker_cooldown_seconds, "熔断冷却"
                )
                if circuit_breaker_cooldown_seconds is not None
                else (
                    current.circuit_breaker_cooldown_seconds if current else None
                )
            ),
            model_overrides=next_overrides,
        )
        if index is None:
            profiles.append(profile)
        else:
            profiles[index] = profile
        if make_active or active_id is None:
            active_id = normalized_id
        self._write_catalog(profiles, active_id)
        catalog = self.catalog_view()
        return {
            "catalog": catalog,
            "profile": next(
                item
                for item in catalog["providers"]
                if item["provider_id"] == normalized_id
            ),
            "active_settings": catalog["active_settings"],
        }

    @_catalog_update
    def patch_model_override(
        self, provider_id: str, model: str, patch: Mapping[str, object],
    ) -> dict:
        stored = self._read_file()
        profiles, active_id = self._profiles_from(stored)
        index = next((i for i, item in enumerate(profiles) if item.provider_id == provider_id), None)
        self._check_profile_revision(patch.get("expected_revision"), profiles[index] if index is not None else None, stored)
        if index is None:
            raise ProviderNotFoundError("没有找到这个模型服务")
        current = profiles[index]
        if model not in current.selected_models and model not in current.model_overrides:
            raise ProviderSettingsError("先保存模型列表，再修改这个模型的连接设置。")
        overrides = dict(current.model_overrides)
        if patch.get("reset"):
            overrides.pop(model, None)
        else:
            previous = overrides.get(model, ModelOverride())
            payload = _model_override_payload(previous)
            for field_name in ("wire_format", "base_url", "reasoning_effort", "max_output_tokens"):
                if field_name in patch:
                    payload[field_name] = patch[field_name]
            new_key = str(patch.get("api_key") or "").strip()
            if new_key:
                payload["api_key"] = new_key
            if patch.get("inherit_api_key"):
                payload.pop("api_key", None)
            payload["extra_headers"] = _merge_extra_headers(
                previous.extra_headers, patch.get("extra_headers"), patch.get("remove_header_names"),
            )
            updated = _model_override_from(payload)
            if _model_override_payload(updated):
                overrides[model] = updated
            else:
                overrides.pop(model, None)
        profiles[index] = replace(current, model_overrides=overrides)
        self._write_catalog(profiles, active_id)
        return self.catalog_view()

    @_catalog_update
    def activate_profile(self, provider_id: str, *, expected_revision: str | None = None, expected_catalog_revision: str | None = None) -> dict:
        stored = self._read_file()
        profiles, _active_id = self._profiles_from(stored)
        check_settings_revision(expected_catalog_revision, settings_revision(stored))
        self._check_profile_revision(expected_revision, next((item for item in profiles if item.provider_id == provider_id), None), stored)
        if provider_id not in {profile.provider_id for profile in profiles}:
            raise ProviderNotFoundError("没有找到这个模型服务")
        self._write_catalog(profiles, provider_id)
        return self.public_view()

    @_catalog_update
    def delete_profile(self, provider_id: str, *, expected_revision: str | None = None) -> dict:
        stored = self._read_file()
        profiles, active_id = self._profiles_from(stored)
        self._check_profile_revision(expected_revision, next((item for item in profiles if item.provider_id == provider_id), None), stored)
        remaining = [profile for profile in profiles if profile.provider_id != provider_id]
        if len(remaining) == len(profiles):
            raise ProviderNotFoundError("没有找到这个模型服务")
        if active_id == provider_id:
            active_id = remaining[0].provider_id if remaining else None
        self._write_catalog(remaining, active_id)
        return self.catalog_view()

    @_catalog_update
    def accept_reported_model_on_active(self, reported_model: str) -> dict:
        """失败卡上的「认可这个名字」：把中转实际上报的模型名加进当前在用
        那套模型服务的接受名单。只加这一个名字，其余配置一律不动；
        名字已经在名单里（或就是在用模型本身）时幂等返回。"""
        alias = str(reported_model or "").strip()
        if not alias:
            raise ProviderSettingsError("上报的模型名是空的，没什么可认可的。")
        profiles, active_id = self._profiles_from(self._read_file())
        if active_id is None:
            raise ProviderSettingsError("还没有在用的模型服务，先去设置页配一套。")
        index = next(
            (
                offset
                for offset, item in enumerate(profiles)
                if item.provider_id == active_id
            ),
            None,
        )
        if index is None:
            raise ProviderNotFoundError("没有找到这个模型服务")
        current = profiles[index]
        already = alias == current.model or alias in current.accepted_reported_models
        if not already:
            profiles[index] = replace(
                current,
                accepted_reported_models=_clean_reported_model_aliases(
                    [*current.accepted_reported_models, alias]
                ),
            )
            self._write_catalog(profiles, active_id)
        return {
            "already_accepted": already,
            "reported_model": alias,
            "catalog": self.catalog_view(),
            "active_settings": self.public_view(),
        }

    # ---------------------------------------------------------------- 探测
    def _profile_key(self, provider_id: str | None) -> str | None:
        if provider_id is None:
            return self.effective().api_key
        profiles, _active_id = self._profiles_from(self._read_file())
        profile = next(
            (item for item in profiles if item.provider_id == provider_id),
            None,
        )
        return profile.api_key if profile else None

    def _profile_models_url(self, provider_id: str | None) -> str | None:
        if provider_id is None:
            return None
        profiles, _active_id = self._profiles_from(self._read_file())
        profile = next(
            (item for item in profiles if item.provider_id == provider_id),
            None,
        )
        return profile.models_url if profile else None

    def list_models(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_id: str | None = None,
        auth_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
        models_url: str | None = None,
    ) -> list[str]:
        """只探测模型列表端点，不调用模型生成，不产生 token 费用。

        拉不到列表不代表模型不能用（很多服务根本没有 /models），
        所以失败话术要把「手动填模型名 + 测试」这条活路指给作者。
        """
        base = normalize_base_url(base_url)
        profile = self._saved_profile(provider_id)
        key = (api_key or "").strip() or (profile.api_key if profile else "") or ""

        # 认证头按该渠道的配置来（2026-09-12 缝补）：以前这里写死 Bearer，
        # 作者配了 x-api-key / custom 认证的渠道，「获取模型」会 401——
        # 而正式对话是好的，看起来像「模型坏了」。与 agent_provider 的认证分支同口径。
        auth_type = _normalize_auth_type(
            auth_type if auth_type is not None else profile.auth_type if profile else "bearer"
        )
        if auth_type in ("bearer", "x-api-key") and not key:
            raise ProviderProbeError("还没有填写密钥。")
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif auth_type == "x-api-key":
            headers["x-api-key"] = key
        # custom / none：认证头由作者在自定义头里写，或干脆不带。
        configured_headers = _merge_extra_headers(profile.extra_headers if profile else {}, extra_headers, remove_header_names)
        for header_name, header_value in configured_headers.items():
            if header_name.lower() in ("content-type", "accept"):
                continue
            headers[header_name] = header_value
        if not any(name.lower() == "user-agent" for name in headers):
            headers["User-Agent"] = profile.default_user_agent if profile else DEFAULT_PROVIDER_USER_AGENT

        url = resolve_models_url(
            base,
            _clean_models_url(models_url)
            if models_url is not None else profile.models_url if profile else None,
        )
        request = urllib.request.Request(
            url,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if key:
                detail = detail.replace(key, "[密钥已隐藏]")
            from zhimeng_runtime.secret_mask import mask_secrets
            detail, _masked = mask_secrets(detail)
            detail = _upstream_message(detail)
            raise ProviderProbeError(
                _discovery_not_fatal(f"请求 {url}：{_friendly_probe_error(exc.code, detail)}"),
                http_status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderProbeError(
                _discovery_not_fatal(f"连不上 {url}：{exc}")
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderProbeError(
                _discovery_not_fatal(f"请求 {url}：对方返回的不是合法 JSON，可能地址填错了。")
            ) from exc

        try:
            models = parse_models_response(payload)
        except ProviderProbeError as exc:
            raise ProviderProbeError(_discovery_not_fatal(str(exc))) from exc
        if not models:
            raise ProviderProbeError(
                _discovery_not_fatal("模型列表是空的。")
            )
        return sorted(models)

    # ------------------------------------------------------- 测活（流式）
    # 这个中转在非流式下会把整段回答缓冲完才吐，后端稍慢就变成「零字节超时」，
    # 看起来像模型坏了，其实它正常干活（见 中转站配置说明_给AI看.md 坑 1）。
    # 所以测活一律走 stream，并用「第一个字什么时候到」判断死活。
    # 10-07 作者：「测活别发 hi/你好这种伤号的请求，要像正常干活的句子」
    PROBE_PROMPT = "请用三句话说明如何把一项复杂任务拆成可执行步骤。"
    PROBE_STATUS_LABELS = {
        "ok": ("可用", "正常出字"),
        "empty": ("没出字", "连上了，但一个字也没返回；这个模型时好时坏，可以重试一次"),
        "auth": ("认证或权限", "服务未通过认证或拒绝访问，原因见返回信息"),
        "not_found": ("没有这个模型", "服务里找不到这个模型名"),
        "upstream": ("上游故障", "中转连得上，但它后面的模型服务坏了"),
        "http_error": ("接口报错", "服务返回了错误"),
        "timeout": ("超时", "等到最后一个字都没来"),
        "unreachable": ("连不上", "地址不通或网络有问题"),
    }

    @staticmethod
    def _stream_delta_text(event: dict) -> str:
        """从一个 SSE 事件里取出这一小段正文（兼容 delta 和整条 message）。"""
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        for field in ("delta", "message"):
            payload = first.get(field)
            if not isinstance(payload, dict):
                continue
            content = payload.get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                joined = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
                if joined:
                    return joined
        return ""

    @staticmethod
    def _probe_status_for_http(status_code: int) -> str:
        if status_code in (401, 403):
            return "auth"
        if status_code in (404, 405):
            return "not_found"
        if status_code >= 500:
            return "upstream"
        return "http_error"

    def _saved_profile(self, provider_id: str | None) -> ProviderProfile | None:
        """测试按表单来：没给 provider_id 就是还没保存的新服务，不借用当前在用那套的配置。"""
        return self.resolve_profile(provider_id)[0] if provider_id else None

    def reveal_api_key(self, provider_id: str) -> str:
        """作者本人点「显示已存的密钥」时回传本机保存的明文。

        公开视图永远只给 api_key_set 布尔值，只有这一个显式动作才回明文——
        本机单用户，作家要和中转站后台核对自己存的到底是哪把（作者 2026-10-12：
        「这个现在就是我自己，你把它隐藏了也没用」）。
        """
        profile = self._saved_profile(provider_id)
        if profile is None:
            raise ProviderNotFoundError("找不到这条线路。")
        if not profile.api_key:
            raise ProviderNotFoundError("这条线路还没存密钥。")
        return profile.api_key

    @staticmethod
    def _test_connection(
        profile: ProviderProfile | None, *, base: str, key: str, model: str,
        auth_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
        accepted_reported_models: Sequence[str] | None = None,
        test_scope: str = "service_draft",
    ) -> ProviderSettings:
        """明确区分服务表单草稿与已保存模型例外，不能用隐式优先级猜测测试对象。"""
        draft = (
            replace(profile, base_url=base, api_key=key or None)
            if profile is not None
            else ProviderProfile(provider_id="", name="", base_url=base, api_key=key or None, model=model)
        )
        draft = replace(
            draft,
            auth_type=_normalize_auth_type(auth_type) if auth_type is not None else draft.auth_type,
            extra_headers=_merge_extra_headers(draft.extra_headers, extra_headers, remove_header_names),
            accepted_reported_models=(
                tuple(str(item).strip() for item in accepted_reported_models if str(item).strip())
                if accepted_reported_models is not None else draft.accepted_reported_models
            ),
        )
        if test_scope == "saved_model":
            return resolve_model_connection(draft, model)
        if test_scope != "service_draft":
            raise ProviderSettingsError("未知的模型测试范围")
        return replace(draft.connection(), model=model)

    def _probe_provider_for(
        self,
        *,
        provider_id: str | None,
        base: str,
        key: str,
        model: str,
        timeout_seconds: float,
        max_tokens: int,
        wire_format: str | None = None,
        connection: ProviderSettings | None = None,
    ):
        """复用正式连接的路由与认证；仅探测预算、重试和候选协议独立。"""
        from zhimeng_runtime.agent_provider import build_agent_provider_from_connection
        from zhimeng_runtime.config import RuntimeSettings

        if connection is None:
            profile = self._saved_profile(provider_id)
            connection = self._test_connection(profile, base=base, key=key, model=model)
        if wire_format not in WIRE_FORMATS:
            wire_format = connection.wire_format
        # UA 兜底：author 在渠道上配的 User-Agent 优先，没配就带 codex-cli 默认值。
        # 测活不能漏掉这个——中转站认 UA，测活不带 UA 用的就是错误的身份。
        base_headers = dict(connection.extra_headers)
        if not any(k.lower() == "user-agent" for k in base_headers):
            base_headers["User-Agent"] = connection.default_user_agent
        return build_agent_provider_from_connection(
            RuntimeSettings.from_environment(),
            base_url=connection.base_url,
            model=model,
            provider_id=provider_id,
            api_key=connection.api_key,
            # 探测不写收据，但 Provider 要求密钥与凭证引用成对出现。
            credential_reference=(
                f"probe:provider-key:{sha256(connection.api_key.encode('utf-8')).hexdigest()}"
                if connection.api_key else None
            ),
            # 测活只发十几个 token：不能带作者配的思考强度，否则推理模型会
            # 把预算全花在思考上、一个字不吐，能用的模型会被误判成「没返回文字」。
            # "auto" 的含义就是不发这个参数，交给模型自己默认。
            reasoning_effort="auto",
            wire_format=wire_format,
            auth_type=connection.auth_type,
            extra_headers=base_headers or None,
            accepted_reported_models=connection.accepted_reported_models,
            stream_idle_timeout_seconds=connection.stream_idle_timeout_seconds,
            # 探测就是要如实看到这一次的结果：不重试（重试会掩盖真实延迟，
            # 60 个模型各重试 4 次也太贵），也不让断路器把后面的模型一起拖黑
            # （测活本来就预期一批模型会失败）。
            max_retries=0,
            is_probe=True,
            request_timeout_seconds=timeout_seconds,
            max_output_tokens=max_tokens,
            circuit_breaker_threshold=10**6,
        )

    def _probe_model_stream(
        self,
        *,
        base: str,
        key: str,
        model: str,
        prompt: str,
        timeout_seconds: float,
        max_tokens: int,
        enough_characters: int,
        provider_id: str | None = None,
        wire_format: str | None = None,
        connection: ProviderSettings | None = None,
    ) -> dict:
        """对一个模型发一次最小请求，返回分类结果（不抛异常）。

        走的是和正式对话、写章同一条 Provider 链路（2026-09-12 收编）——
        以前这里自己拼 `/chat/completions` + Bearer，作者配了 Responses 也还是
        用 Chat 测，于是「测过 ≠ 真能用」。作者 2026-09-11 认可过的原则是
        「测试链和正式链要复用同一套解析」，这里把它落实。
        """
        from zhimeng_runtime.agent_provider import (
            AgentProviderCancelled,
            AgentProviderError,
            ProviderReasoningDelta,
            ProviderTextDelta,
            run_provider_blocking,
        )

        started = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, round((time.monotonic() - started) * 1000))

        try:
            provider = self._probe_provider_for(
                provider_id=provider_id,
                base=base,
                key=key,
                model=model,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
                wire_format=wire_format,
                connection=connection,
            )
        except (ValueError, TypeError) as exc:
            return {
                "status": "http_error",
                "http_status": None,
                "detail": f"这个渠道的配置有问题：{exc}"[:260],
                "reply": "",
                "first_byte_ms": None,
                "elapsed_ms": elapsed_ms(),
            }
        if provider is None:
            return {
                "status": "http_error",
                "http_status": None,
                "detail": "地址或模型没填全。",
                "reply": "",
                "first_byte_ms": None,
                "elapsed_ms": elapsed_ms(),
            }

        pieces: list[str] = []
        first_byte: list[int] = []
        thought = [0]

        async def collect() -> None:
            cancel_event = asyncio.Event()
            stream = provider.stream_chat(
                [{"role": "user", "content": prompt}],
                cancel_event=cancel_event,
                tools=(),
            )
            async for event in stream:
                if isinstance(event, ProviderReasoningDelta) and event.text:
                    if not first_byte:
                        first_byte.append(elapsed_ms())
                    thought[0] += len(event.text)
                    continue
                if not isinstance(event, ProviderTextDelta) or not event.text:
                    continue
                if not first_byte:
                    first_byte.append(elapsed_ms())
                pieces.append(event.text)
                # 够判断活着就收工，不必把整段读完，省时间也省 token
                if sum(len(item) for item in pieces) >= enough_characters:
                    cancel_event.set()
                    await stream.aclose()
                    return

        def thinking_only() -> dict:
            # 先想后答的模型：测活只给十几个 token，全花在思考上，正文还没开口就到头了。
            # 在思考就说明线路和模型都是活的（2026-09-26 真机：kimi-k3 被判成「没出字」，
            # 按正常额度问一次 8 秒就答了，也会调工具）。
            return {
                "status": "ok",
                "http_status": 200,
                "detail": "先想后答：本次只收到思考，尚未收到正文；实际回复能力需进一步核验。",
                "reply": "",
                "first_byte_ms": first_byte[0] if first_byte else None,
                "elapsed_ms": elapsed_ms(),
            }

        try:
            run_provider_blocking(collect())
        except AgentProviderCancelled:
            pass  # 是我们自己喊停的（已经收够字），算成功
        except AgentProviderError as exc:
            code = getattr(exc, "code", "") or ""
            if code == "agent_provider_output_truncated":
                reply_so_far = "".join(pieces).strip()
                if thought[0] > 0 and not reply_so_far:
                    return thinking_only()
                if reply_so_far:
                    # 顶满之前已经开口：能用，注明话没说完。
                    return {
                        "status": "ok",
                        "http_status": 200,
                        "detail": "测活给的输出额度被顶满了、话没说完，但它确实开口答了——线路是活的，正式对话额度大得多。",
                        "reply": reply_so_far,
                        "first_byte_ms": first_byte[0] if first_byte else None,
                        "elapsed_ms": elapsed_ms(),
                    }
                # 这条线路上游不把思考单独报出来：顶满的是我们测活自己给的十几格
                # 额度，不是网关坏了——以前这条落到 502「上游网关错误」（作者 2026-10-12
                # 截图，grok-4.7 明明能用却报 Bad Gateway）。
                return {
                    "status": "ok",
                    "http_status": 200,
                    "detail": "本次达到测活输出额度，尚未收到正文；上游未返回可核对的思考内容，实际回复能力需进一步核验。",
                    "reply": "",
                    "first_byte_ms": first_byte[0] if first_byte else None,
                    "elapsed_ms": elapsed_ms(),
                }
            if thought[0] > 0 and not "".join(pieces).strip() and code == "agent_provider_empty_response":
                return thinking_only()
            status, http_status, detail = self._probe_outcome_for_provider_error(
                exc, timeout_seconds=timeout_seconds
            )
            return {
                "status": status,
                "http_status": http_status,
                "detail": detail,
                "reply": "".join(pieces).strip(),
                "first_byte_ms": first_byte[0] if first_byte else None,
                "elapsed_ms": elapsed_ms(),
            }
        except Exception as exc:  # 探测绝不把异常抛给调用方
            return {
                "status": "unreachable",
                "http_status": None,
                "detail": f"连不上：{exc}"[:260],
                "reply": "",
                "first_byte_ms": None,
                "elapsed_ms": elapsed_ms(),
            }

        reply = "".join(pieces).strip()
        if not reply and thought[0] > 0:
            return thinking_only()
        return {
            "status": "ok" if reply else "empty",
            "http_status": 200,
            "detail": "" if reply else "连上了，但没有返回文字。",
            "reply": reply,
            "first_byte_ms": first_byte[0] if first_byte else None,
            "elapsed_ms": elapsed_ms(),
        }

    def _probe_outcome_for_provider_error(
        self,
        exc,
        *,
        timeout_seconds: float,
    ) -> tuple[str, int | None, str]:
        """把 Provider 的错误码翻回测活那套分类，前端徽章的语义保持不变。"""
        code = getattr(exc, "code", "") or ""
        details = getattr(exc, "details", None)
        details = details if isinstance(details, dict) else {}
        http_status = details.get("upstream_status")
        http_status = http_status if isinstance(http_status, int) else None
        if http_status is not None:
            excerpt = str(details.get("upstream_message") or details.get("upstream_excerpt") or exc)
            return (
                self._probe_status_for_http(http_status),
                http_status,
                _friendly_model_probe_error(http_status, excerpt),
            )
        if code in {
            "agent_provider_timeout",
            "agent_provider_stream_idle_timeout",
        }:
            return "timeout", None, f"等了 {int(timeout_seconds)} 秒没有回应。"
        if code == "agent_provider_empty_response":
            return "empty", 200, "连上了，但没有返回文字。"
        if code == "agent_provider_request_failed":
            return "unreachable", None, f"连不上：{exc}"[:260]
        return "http_error", None, str(exc)[:260]

    # ------------------------------------------------- 测活：会不会调工具
    # Agent 干活靠调工具，光能出字不够。像平常干活那样带一个读文件工具问一句，
    # 看模型能不能真调出来，并在回传合成结果后继续回答——中转站换协议转换时最容易
    # 丢的就是工具调用或工具结果回合。
    # 10-05 真测 220：第一版用「这是连通性检查，请调用 probe_echo」，grok-4.6 三种协议都把它
    # 当成一句话复述回来；换成正常的读文件请求，Chat 和 Responses 都马上调了 read_file。
    PROBE_TOOL_NAME = "read_file"
    PROBE_TOOL_PROMPT = "读一下 README.md 这个文件，告诉我第一行写的什么。"
    PROBE_TOOL_STATUS_NOTES = {
        "no_tool_call": "能回话，但没有调工具；在这条线路上它可能干不了需要读写文件的活",
        "thinking_only": "一直在想，测的额度用完了还没调工具；正式对话额度大得多，可以再测一次",
        "tool_roundtrip_failed": "模型发出了工具调用，但收到模拟工具结果后没有继续完成回答",
    }
    # 先想后答的模型想的那段也算在额度里：10-05 真测 kimi-k3 给 512 只想不调，4096 三次里两次只想不调，
    # 2048 / 8192 单次都调了。可额度要大了，有的上游按「要的额度」算每分钟用量：gpt-oss-120b 要 8192
    # 直接回 429「Tokens per minute」，要 4096 是通的。所以先给 2048，只顾着想、额度用完还没调的，
    # 再给 8192 问一次。第一轮开口说够 400 字仍没调工具时停止；调出工具则回传合成结果，
    # 等第二轮回答完成，才能证明工具结果也能通过这条协议。
    PROBE_TOOL_BUDGETS = (2048, 8192)
    PROBE_TOOL_ENOUGH_TEXT = 400

    def _probe_tool_call(
        self,
        *,
        base: str,
        key: str,
        model: str,
        timeout_seconds: float,
        provider_id: str | None = None,
        wire_format: str | None = None,
        connection: ProviderSettings | None = None,
    ) -> dict:
        """带一个只读探针工具问一句，并验证工具结果回传后的第二轮回答。

        ``ok`` 只在完整闭环成立时使用：模型先发出工具调用，探针再回传一份合成的只读
        结果，模型随后继续输出。探针不会访问作者文件，也不会把真实文件内容发给上游。
        """
        outcome: dict = {}
        for budget in self.PROBE_TOOL_BUDGETS:
            outcome = self._probe_tool_call_once(
                base=base,
                key=key,
                model=model,
                timeout_seconds=timeout_seconds,
                provider_id=provider_id,
                wire_format=wire_format,
                max_tokens=budget,
                connection=connection,
            )
            if outcome["status"] != "thinking_only":
                break
        return outcome

    def _probe_tool_call_once(
        self,
        *,
        base: str,
        key: str,
        model: str,
        timeout_seconds: float,
        provider_id: str | None,
        wire_format: str | None,
        max_tokens: int,
        connection: ProviderSettings | None = None,
    ) -> dict:
        from zhimeng_runtime.agent_provider import (
            AgentProviderCancelled,
            AgentProviderError,
            ProviderCompleted,
            ProviderReasoningDelta,
            ProviderTextDelta,
            ProviderToolCall,
            ProviderToolDefinition,
            run_provider_blocking,
        )

        started = time.monotonic()
        first_call: list[ProviderToolCall] = []
        thought = [0]
        spoken = [0]

        def not_called() -> str:
            return "thinking_only" if thought[0] and not spoken[0] else "no_tool_call"

        def outcome(
            status: str,
            detail: str = "",
            http_status: int | None = 200,
            *,
            failure_status: str | None = None,
        ) -> dict:
            if status == "ok":
                roundtrip = "ok"
            elif first_call or status in {"tool_roundtrip_failed", "unsure"}:
                roundtrip = "failed"
            elif status in {"no_tool_call", "thinking_only"}:
                roundtrip = "not_attempted"
            else:
                roundtrip = "untested"
            return {
                "status": status,
                "http_status": http_status,
                "detail": detail or self.PROBE_TOOL_STATUS_NOTES.get(status, ""),
                "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
                "tool_call_emitted": bool(first_call),
                "tool_roundtrip": roundtrip,
                "failure_status": failure_status,
            }

        try:
            provider = self._probe_provider_for(
                provider_id=provider_id,
                base=base,
                key=key,
                model=model,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
                wire_format=wire_format,
                connection=connection,
            )
        except (ValueError, TypeError) as exc:
            return outcome("http_error", f"这个渠道的配置有问题：{exc}"[:260], None)
        if provider is None:
            return outcome("http_error", "地址或模型没填全。", None)
        tool = ProviderToolDefinition(
            name=self.PROBE_TOOL_NAME,
            version="1",
            description="Read a text file and return its content.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path of the file to read."}},
                "required": ["path"],
            },
        )

        async def collect_first() -> None:
            cancel_event = asyncio.Event()
            stream = provider.stream_chat(
                [{"role": "user", "content": self.PROBE_TOOL_PROMPT}],
                cancel_event=cancel_event,
                tools=(tool,),
            )
            async for event in stream:
                if isinstance(event, ProviderReasoningDelta):
                    thought[0] += len(event.text or "")
                    continue
                if isinstance(event, ProviderTextDelta):
                    spoken[0] += len(event.text or "")
                    # 开口说了一大段还没调，也看得出它这回不打算调。
                    if spoken[0] >= self.PROBE_TOOL_ENOUGH_TEXT:
                        cancel_event.set()
                        await stream.aclose()
                        return
                if isinstance(event, ProviderToolCall):
                    first_call.append(event)
                    # 工具调用之后要等本轮终止事件，才能安全构造下一轮历史；不在这里
                    # 关闭流，否则只能证明“发过调用”，无法证明上游接受工具结果。
                    continue
                if isinstance(event, ProviderCompleted):
                    return

        async def collect_second(call: ProviderToolCall) -> tuple[str, int, bool, bool]:
            cancel_event = asyncio.Event()
            assistant_call = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            }
            tool_result = {
                "role": "tool",
                "tool_call_id": call.tool_call_id,
                "content": "README.md 第一行：这是工具探针返回的模拟内容。",
            }
            stream = provider.stream_chat(
                [
                    {"role": "user", "content": self.PROBE_TOOL_PROMPT},
                    assistant_call,
                    tool_result,
                ],
                cancel_event=cancel_event,
                tools=(tool,),
            )
            pieces: list[str] = []
            second_thought = 0
            completed = False
            nested_tool_call = False
            async for event in stream:
                if isinstance(event, ProviderReasoningDelta):
                    second_thought += len(event.text or "")
                    continue
                if isinstance(event, ProviderTextDelta):
                    pieces.append(event.text or "")
                    continue
                if isinstance(event, ProviderToolCall):
                    nested_tool_call = True
                    continue
                if isinstance(event, ProviderCompleted):
                    completed = True
            return "".join(pieces).strip(), second_thought, completed, nested_tool_call

        try:
            run_provider_blocking(collect_first())
        except AgentProviderCancelled:
            pass
        except AgentProviderError as exc:
            if not first_call and getattr(exc, "code", "") in {
                "agent_provider_empty_response",
                "agent_provider_output_truncated",
            }:
                return outcome(not_called())
            status, http_status, detail = self._probe_outcome_for_provider_error(
                exc, timeout_seconds=timeout_seconds
            )
            return outcome(status, detail, http_status)
        except Exception as exc:  # 探测绝不把异常抛给调用方
            return outcome("unreachable", f"连不上：{exc}"[:260], None)

        if not first_call:
            return outcome(not_called())
        call = first_call[0]
        if call.arguments_error:
            return outcome(
                "unsure",
                "模型发出了工具调用，但参数没有完整解析出来。",
                200,
            )

        try:
            final_text, second_thought, completed, nested_tool_call = run_provider_blocking(
                collect_second(call)
            )
        except AgentProviderCancelled:
            # 第二轮由上游主动取消时不把第一轮误报成成功；本探针不主动取消第二轮，
            # 因而这里表示上游确实没有完成回合。
            final_text, second_thought, completed, nested_tool_call = "", 0, False, False
        except AgentProviderError as exc:
            status, http_status, detail = self._probe_outcome_for_provider_error(
                exc, timeout_seconds=timeout_seconds
            )
            return outcome(
                "tool_roundtrip_failed",
                f"工具调用已发出，但回传结果后的第二轮失败：{detail}",
                http_status,
                failure_status=status,
            )
        except Exception as exc:  # 探测绝不把异常抛给调用方
            return outcome(
                "tool_roundtrip_failed",
                f"工具调用已发出，但第二轮连不上：{exc}"[:260],
                None,
                failure_status="unreachable",
            )
        if completed and final_text and not nested_tool_call:
            return outcome("ok")
        if second_thought:
            return outcome(
                "tool_roundtrip_failed",
                "工具结果已回传，但测活额度用完前还没有继续回答。",
                200,
            )
        return outcome(
            "tool_roundtrip_failed",
            self.PROBE_TOOL_STATUS_NOTES["tool_roundtrip_failed"],
            200,
        )

    def _wire_plan(
        self,
        profile: ProviderProfile | None,
        model: str,
        wire_choice: str | None,
        test_scope: str = "service_draft",
    ) -> tuple[list[str], bool]:
        """测这个模型要试哪几种协议、算不算「自动」。

        这个模型单独写过协议、或作者在表单里选了具体的一种：只测那一种，测完不记。
        选「自动」：能力卡的建议排第一，另外两种跟在后面，测通的记下来。
        表单没说（旧前端）：照已保存的配置——写明过就是明确，没写过就是自动。
        """
        override = profile.model_overrides.get(model) if profile is not None else None
        choice = str(wire_choice or "").strip().lower()
        if choice in WIRE_FORMATS:
            return [choice], False
        if override is not None and override.wire_format and test_scope == "saved_model":
            return [override.wire_format], False
        if choice != WIRE_AUTO and profile is not None and profile.wire_format_explicit:
            return [profile.wire_format], False
        suggested = "chat"
        try:
            from zhimeng_runtime.model_capabilities import resolve_model_capability

            suggested = resolve_model_capability(model).default_wire
        except Exception:
            pass  # 能力卡查不到就按 chat 打头
        if suggested not in WIRE_FORMATS:
            suggested = "chat"
        return [suggested, *(w for w in ("chat", "responses", "anthropic_messages") if w != suggested)], True

    def test_model(
        self,
        *,
        provider_id: str | None,
        base_url: str,
        api_key: str | None,
        model: str,
        prompt: str,
        wire_format: str | None = None,
        auth_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
        accepted_reported_models: Sequence[str] | None = None,
        test_scope: str = "saved_model",
    ) -> dict:
        """用作者给的一句话做一次短测试，确认模型不只是出现在列表里。

        用的协议和正式对话一样：明确选的就用选的；自动的话，这条线路上测活测通过
        的优先，没测过用能力卡的建议。
        """
        base = normalize_base_url(base_url)
        profile = self._saved_profile(provider_id)
        key = (api_key or "").strip() or (profile.api_key if profile else "") or ""
        selected_model = str(model or "").strip()
        test_prompt = str(prompt or "").strip()
        if not selected_model:
            raise ProviderSettingsError("请选择一个要测试的模型")
        if not test_prompt:
            raise ProviderSettingsError("请输入测试句子")
        connection = self._test_connection(
            profile, base=base, key=key, model=selected_model,
            auth_type=auth_type, extra_headers=extra_headers,
            remove_header_names=remove_header_names,
            accepted_reported_models=accepted_reported_models,
            test_scope=test_scope,
        )
        if connection.auth_type in ("bearer", "x-api-key") and not connection.api_key:
            raise ProviderProbeError("还没有填写密钥。")
        wires, auto = self._wire_plan(profile, selected_model, wire_format, test_scope)
        if test_scope == "service_draft":
            auto = str(wire_format or "").strip().lower() == WIRE_AUTO
        wire = wires[0]
        if auto:
            wire = (
                _verified_wire(
                    self._read_routes(),
                    connection,
                    selected_model,
                )
                or wire
            )
        outcome = self._probe_model_stream(
            provider_id=provider_id,
            base=base,
            key=key,
            model=selected_model,
            prompt=test_prompt,
            timeout_seconds=120.0,
            max_tokens=80,
            enough_characters=800,
            wire_format=wire,
            connection=connection,
        )
        if outcome["status"] != "ok":
            upstream_status = outcome.get("http_status")
            raise ProviderProbeError(
                outcome["detail"] or "这个模型没有正常返回内容。",
                http_status=upstream_status if isinstance(upstream_status, int) and upstream_status >= 400 else None,
            )
        return {
            "schema": "zhimeng.provider-test.v1",
            "ok": True,
            "model": selected_model,
            "reply": outcome["reply"][:1000],
            "latency_ms": outcome["elapsed_ms"],
            "first_byte_ms": outcome["first_byte_ms"],
            "wire_format": wire,
        }

    def test_models(
        self,
        *,
        provider_id: str | None,
        base_url: str,
        api_key: str | None,
        models: Sequence[str],
        prompt: str | None = None,
        # 10-05 真测 220：grok 第一个字要等 24–29 秒，25 秒就判「超时」会把能用的判死。
        timeout_seconds: float = 90.0,
        max_workers: int = 4,
        wire_format: str | None = None,
        check_tools: bool = True,
        auth_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        remove_header_names: Sequence[str] | None = None,
        accepted_reported_models: Sequence[str] | None = None,
        test_scope: str = "saved_model",
    ) -> dict:
        """一键测活：对一批模型各发一次最小流式请求，分出哪些真能用。

        中转站列出的模型只是清单，不代表可用：上游通道坏掉时同样在列表里。
        这里逐个实测，按「先能用、再按出字快慢」排序返回。
        出字只要 16 个 token；能出字的再验证工具调用和合成结果回传后的第二轮回答。

        wire_format = 表单里选的接口协议。选了具体的一种就只测那一种；选「自动」
        就按顺序试（能力卡建议的打头），出字又会调工具的那种记进 provider-routes.json，
        以后正式对话在这条线路上用它。
        """
        base = normalize_base_url(base_url)
        profile = self._saved_profile(provider_id)
        key = (api_key or "").strip() or (profile.api_key if profile else "") or ""
        wanted: list[str] = []
        for item in models or ():
            name = str(item or "").strip()
            if name and name not in wanted:
                wanted.append(name)
        if not wanted:
            raise ProviderSettingsError("请先选择要测试的模型")
        if len(wanted) > 60:
            raise ProviderSettingsError("一次最多测 60 个模型")
        test_prompt = str(prompt or "").strip() or self.PROBE_PROMPT
        connections = {
            name: self._test_connection(
                profile, base=base, key=key, model=name,
                auth_type=auth_type, extra_headers=extra_headers,
                remove_header_names=remove_header_names,
                accepted_reported_models=accepted_reported_models,
                test_scope=test_scope,
            )
            for name in wanted
        }
        if any(connection.auth_type in ("bearer", "x-api-key") and not connection.api_key for connection in connections.values()):
            raise ProviderProbeError("还没有填写密钥。")
        verified_routes = self._read_routes()

        def probe(name: str) -> dict:
            wires, auto = self._wire_plan(profile, name, wire_format, test_scope)
            if test_scope == "service_draft":
                auto = str(wire_format or "").strip().lower() == WIRE_AUTO
            if auto:
                verified = _verified_wire(verified_routes, connections[name], name)
                if verified:
                    wires = [verified, *(wire for wire in wires if wire != verified)]
            tried: list[dict] = []
            picked: tuple[str, dict, dict | None] | None = None
            speaks_only: tuple[str, dict, dict | None] | None = None
            first_failure: tuple[str, dict] | None = None
            unsure = False  # 前面有没有「说不准」的失败
            for wire in wires:
                text = self._probe_model_stream(
                    provider_id=provider_id,
                    base=base,
                    key=key,
                    model=name,
                    prompt=test_prompt,
                    timeout_seconds=timeout_seconds,
                    max_tokens=16,
                    enough_characters=24,
                    wire_format=wire,
                    connection=connections[name],
                )
                if text["status"] != "ok":
                    tried.append({"wire_format": wire, "status": text["status"], "note": text["detail"][:160]})
                    first_failure = first_failure or (wire, text)
                    if _failure_not_about_wire(text):
                        break
                    unsure = unsure or text["status"] not in _WIRE_MISSING
                    continue
                tool = (
                    self._probe_tool_call(
                        provider_id=provider_id,
                        base=base,
                        key=key,
                        model=name,
                        timeout_seconds=timeout_seconds,
                        wire_format=wire,
                        connection=connections[name],
                    )
                    if check_tools
                    else None
                )
                tried.append(
                    {
                        "wire_format": wire,
                        "status": "ok",
                        "tools": tool["status"] if tool is not None else "untested",
                        "note": "" if tool is None or tool["status"] == "ok" else tool["detail"][:160],
                    }
                )
                if tool is None or tool["status"] == "ok":
                    picked = (wire, text, tool)
                    break
                # 能出字但调不出工具：先记着，换下一种协议看看会不会调
                speaks_only = speaks_only or (wire, text, tool)
                if _failure_not_about_wire(tool):
                    unsure = True  # 没问出个结果（超时、限流），不算「不会调」
                    break
            picked = picked or speaks_only
            if picked is not None:
                wire, outcome, tool = picked
            else:
                wire, outcome = first_failure or (wires[0], {
                    "status": "http_error", "detail": "", "first_byte_ms": None,
                    "elapsed_ms": 0, "reply": "",
                })
                tool = None
            label, hint = self.PROBE_STATUS_LABELS.get(
                outcome["status"], ("测不出来", "")
            )
            tools = (
                "untested" if tool is None
                else "ok" if tool["status"] == "ok"
                else "no" if tool["status"] == "no_tool_call"
                else "failed" if tool["status"] == "tool_roundtrip_failed"
                else "unsure"  # 超时、限流、只顾着想：没问出结果，不算「不会调」
            )
            tool_call_emitted = bool(tool and tool.get("tool_call_emitted"))
            tool_roundtrip = (
                "untested"
                if tool is None
                else str(tool.get("tool_roundtrip") or ("ok" if tool["status"] == "ok" else "failed"))
            )
            # 记不记：出字又会调工具就记；只会出字的，前面没有说不准的失败才记
            remembered = auto and outcome["status"] == "ok" and (
                tools == "ok" or (tools == "no" and not unsure)
            )
            return {
                "model": name,
                "status": outcome["status"],
                "usable": outcome["status"] == "ok",
                "label": label,
                "note": outcome["detail"] or hint,
                "first_byte_ms": outcome["first_byte_ms"],
                "elapsed_ms": outcome["elapsed_ms"],
                "reply": outcome["reply"][:120],
                "wire_format": wire,
                "auto": auto,
                "tools": tools,
                "tool_call_emitted": tool_call_emitted,
                "tool_roundtrip": tool_roundtrip,
                "tools_note": "" if tool is None or tools == "ok" else (tool["detail"] or ""),
                "tried": tried,
                "remembered": remembered,
            }

        workers = max(1, min(int(max_workers), 8, len(wanted)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(probe, wanted))
        verified_at = datetime.now().astimezone().isoformat(timespec="seconds")
        route_records: dict[str, dict[str, dict]] = {}
        route_connections: dict[str, ProviderSettings] = {}
        for item in results:
            if item["remembered"]:
                connection = connections[item["model"]]
                route_id = _connection_route_key(connection)
                route_connections[route_id] = connection
                bucket = route_records.setdefault(route_id, {})
                bucket[item["model"]] = {
                    "wire_format": item["wire_format"],
                    "tools": item["tools"] == "ok",
                    "verified_at": verified_at,
                }
        for route_id, records in route_records.items():
            connection = route_connections[route_id]
            self._record_routes(base_url=connection.base_url or "", api_key=connection.api_key or "",
                                auth_type=connection.auth_type, extra_headers=connection.extra_headers,
                                records=records)
        results.sort(
            key=lambda item: (
                not item["usable"],
                item["first_byte_ms"] if item["first_byte_ms"] is not None else 10**9,
                item["model"],
            )
        )
        usable = [item["model"] for item in results if item["usable"]]
        return {
            "schema": "zhimeng.provider-model-liveness.v1",
            "tested": len(results),
            "usable_count": len(usable),
            "usable_models": usable,
            "fastest_model": usable[0] if usable else None,
            "results": results,
        }
