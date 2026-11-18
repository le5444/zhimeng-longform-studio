from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.provider_settings_service import (
    ProviderProbeError,
    ProviderSettingsError,
    ProviderSettingsService,
)


class ProviderRevisionRequest(BaseModel):
    expected_revision: str | None = Field(default=None, pattern=r"^(?:[a-f0-9]{64}|missing)$")


class ProviderSettingsUpdate(ProviderRevisionRequest):
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    wire_format: Literal["chat", "responses", "anthropic_messages"] | None = None
    context_window: Literal["256k", "1m"] | None = None
    # 不传 = 不修改；传空串 = 清除已保存的密钥
    api_key: str | None = Field(default=None, max_length=500)


class ProviderModelsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    provider_id: str | None = Field(default=None, max_length=80)
    # 不传就用已保存的密钥，作者不必每次重新粘贴
    api_key: str | None = Field(default=None, max_length=500)
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] | None = None
    extra_headers: dict[str, str] | None = None
    remove_header_names: list[str] = Field(default_factory=list)
    models_url: str | None = Field(default=None, max_length=500)


class ProviderRoutesDeleteRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    provider_id: str | None = Field(default=None, max_length=80)
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] | None = None
    extra_headers: dict[str, str] | None = None
    remove_header_names: list[str] = Field(default_factory=list)


class ModelOverrideUpdate(BaseModel):
    """单个模型对渠道级配置的覆盖。api_key / extra_headers 的值只进不出。"""

    wire_format: Literal["chat", "responses", "anthropic_messages"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    reasoning_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=1000000)
    extra_headers: dict[str, str] | None = None


class ProviderProfileUpdate(ProviderRevisionRequest):
    provider_id: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    selected_models: list[str] = Field(default_factory=list, max_length=200)
    reasoning_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"] = "auto"
    # 不给 = 保持原样（新服务就是「自动」）；给 "auto" = 取消明确选择、回到自动。
    # 以前这里默认填 "chat"：前端选「自动」不发这个字段，结果被存成「明确选了 Chat」。
    wire_format: Literal["auto", "chat", "responses", "anthropic_messages"] | None = None
    accepted_reported_models: list[str] = Field(default_factory=list, max_length=64)
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] | None = None
    extra_headers: dict[str, str] | None = None
    remove_header_names: list[str] = Field(default_factory=list)
    models_url: str | None = Field(default=None, max_length=500)
    request_timeout_seconds: float | None = Field(default=None, ge=1, le=3600)
    stream_idle_timeout_seconds: float | None = Field(default=None, ge=1, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    max_concurrent_requests: int | None = Field(default=None, ge=1, le=32)
    circuit_breaker_threshold: int | None = Field(default=None, ge=1, le=20)
    circuit_breaker_cooldown_seconds: float | None = Field(default=None, ge=1, le=3600)
    model_overrides: dict[str, ModelOverrideUpdate] | None = None
    context_window: Literal["256k", "1m"] | None = None
    api_key: str | None = Field(default=None, max_length=500)
    make_active: bool = False


class ProviderModelOverridePatch(ModelOverrideUpdate, ProviderRevisionRequest):
    reset: bool = False
    inherit_api_key: bool = False
    remove_header_names: list[str] = Field(default_factory=list)


class ProviderTestRequest(BaseModel):
    provider_id: str | None = Field(default=None, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=2000)
    wire_format: Literal["auto", "chat", "responses", "anthropic_messages"] | None = None
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] | None = None
    extra_headers: dict[str, str] | None = None
    remove_header_names: list[str] = Field(default_factory=list)
    accepted_reported_models: list[str] | None = Field(default=None, max_length=64)
    # 旧调用默认保留模型例外；新界面显式传 service_draft 测试当前服务草稿。
    test_scope: Literal["service_draft", "saved_model"] = "saved_model"


class ProviderLivenessRequest(BaseModel):
    """一键测活：一次测一批模型，看哪些真能出字。"""

    provider_id: str | None = Field(default=None, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    models: list[str] = Field(min_length=1, max_length=60)
    prompt: str | None = Field(default=None, max_length=2000)
    # 表单里选的接口协议：具体的一种就只测它；"auto" 就按顺序试、测通的记下来。
    wire_format: Literal["auto", "chat", "responses", "anthropic_messages"] | None = None
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] | None = None
    extra_headers: dict[str, str] | None = None
    remove_header_names: list[str] = Field(default_factory=list)
    accepted_reported_models: list[str] | None = Field(default=None, max_length=64)
    # 旧调用默认保留模型例外；新界面显式传 service_draft 测试当前服务草稿。
    test_scope: Literal["service_draft", "saved_model"] = "saved_model"


class AcceptReportedModelRequest(BaseModel):
    """失败卡上的「认可这个名字」：把中转实际上报的模型名加进接受名单。"""

    reported_model: str = Field(min_length=1, max_length=200)


def create_provider_settings_router(service: ProviderSettingsService | None) -> APIRouter:
    router = APIRouter(tags=["provider-settings"])

    def require() -> ProviderSettingsService:
        if service is None:
            raise ApiError(
                "写作模型设置不可用",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="provider_settings_unavailable",
                retryable=False,
            )
        return service

    async def settings_call(method, *args, **kwargs):
        try:
            return await run_in_threadpool(method, *args, **kwargs)
        except ProviderSettingsError as exc:
            raise ApiError(str(exc), status_code=exc.status_code, code=exc.code, retryable=False) from None
        except OSError:
            raise ApiError("模型配置未能保存，请检查文件占用、权限或磁盘空间后重试。",
                           status_code=503, code="provider_settings_write_failed", retryable=True) from None

    @router.get("/api/v1/settings/provider")
    async def read_settings() -> dict:
        return await settings_call(require().public_view)

    @router.get("/api/v1/settings/model-capabilities/{model_id}")
    async def read_model_capability(model_id: str) -> dict:
        """按模型 id 返回能力卡（设置页选模型时旁边显示）。"""
        from zhimeng_runtime.model_capabilities import resolve_model_capability

        card = resolve_model_capability(model_id)
        return {
            "model_id": card.model_id,
            "context_window_tier": card.context_window_tier,
            "default_wire": card.default_wire,
            "default_effort": card.default_effort,
            "effort_levels": list(card.effort_levels),
            "max_output_tokens": card.max_output_tokens,
            "vision": card.vision,
            "image_generation": card.image_generation,
            "supports_tools": card.supports_tools,
            "supports_json_schema": card.supports_json_schema,
            "supports_reasoning_content": card.supports_reasoning_content,
        }

    @router.put("/api/v1/settings/provider")
    async def update_settings(request: ProviderSettingsUpdate) -> dict:
        active = require()
        return await settings_call(
            active.save,
            expected_revision=request.expected_revision,
            base_url=request.base_url,
            model=request.model,
            api_key=request.api_key,
            reasoning_effort=request.reasoning_effort,
            wire_format=request.wire_format,
            context_window=request.context_window,
        )

    @router.get("/api/v1/settings/providers")
    async def read_providers() -> dict:
        return await settings_call(require().catalog_view)

    @router.put("/api/v1/settings/providers")
    async def save_provider(request: ProviderProfileUpdate) -> dict:
        active = require()
        return await settings_call(
            active.save_profile,
            expected_revision=request.expected_revision,
            provider_id=request.provider_id,
            name=request.name,
            base_url=request.base_url,
            model=request.model,
            api_key=request.api_key,
            selected_models=request.selected_models,
            reasoning_effort=request.reasoning_effort,
            wire_format=request.wire_format,
            accepted_reported_models=request.accepted_reported_models,
            auth_type=request.auth_type,
            extra_headers=request.extra_headers,
            remove_header_names=request.remove_header_names,
            models_url=request.models_url,
            request_timeout_seconds=request.request_timeout_seconds,
            stream_idle_timeout_seconds=request.stream_idle_timeout_seconds,
            max_retries=request.max_retries,
            max_concurrent_requests=request.max_concurrent_requests,
            circuit_breaker_threshold=request.circuit_breaker_threshold,
            circuit_breaker_cooldown_seconds=request.circuit_breaker_cooldown_seconds,
            model_overrides=(
                {
                    model: override.model_dump(exclude_unset=True)
                    for model, override in request.model_overrides.items()
                }
                if request.model_overrides is not None
                else None
            ),
            context_window=request.context_window,
            make_active=request.make_active,
        )

    @router.patch("/api/v1/settings/providers/{provider_id}/model-overrides/{model:path}")
    async def patch_model_override(provider_id: str, model: str, request: ProviderModelOverridePatch) -> dict:
        return await settings_call(
            require().patch_model_override, provider_id, model,
            request.model_dump(exclude_unset=True),
        )

    @router.post("/api/v1/settings/providers/{provider_id}/activate")
    async def activate_provider(provider_id: str, expected_revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
                                expected_catalog_revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$")) -> dict:
        return await settings_call(require().activate_profile, provider_id, expected_revision=expected_revision,
                                   expected_catalog_revision=expected_catalog_revision)

    @router.delete("/api/v1/settings/providers/{provider_id}")
    async def delete_provider(provider_id: str, expected_revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$")) -> dict:
        return await settings_call(require().delete_profile, provider_id, expected_revision=expected_revision)

    @router.post("/api/v1/settings/providers/{provider_id}/reveal-key")
    async def reveal_provider_key(provider_id: str) -> dict:
        """作者本人查看已保存的密钥：公开视图只给布尔值，只在这个显式动作里回明文。"""
        key = await settings_call(require().reveal_api_key, provider_id)
        return {"schema": "zhimeng.provider-key-reveal.v1", "api_key": key}

    @router.post("/api/v1/settings/provider/accept-reported-model")
    async def accept_reported_model(request: AcceptReportedModelRequest) -> dict:
        """模型身份核对失败后的作者自救：认可中转实际上报的名字。

        不悄悄放行——名字是作者亲手点的；点完写进当前那套模型服务的
        接受名单，重发那句话就能过核对。
        """
        try:
            return await run_in_threadpool(
                require().accept_reported_model_on_active,
                request.reported_model,
            )
        except ProviderSettingsError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
                retryable=False,
            ) from exc

    @router.post("/api/v1/settings/provider/models")
    async def list_models(request: ProviderModelsRequest) -> dict:
        """一键获取模型：只探测 /models，不调用模型生成。"""
        active = require()
        try:
            models = await run_in_threadpool(
                active.list_models,
                base_url=request.base_url,
                api_key=request.api_key,
                provider_id=request.provider_id,
                auth_type=request.auth_type,
                extra_headers=request.extra_headers,
                remove_header_names=request.remove_header_names,
                models_url=request.models_url,
            )
        except ProviderProbeError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.http_status or status.HTTP_502_BAD_GATEWAY,
                code=exc.code,
                retryable=True,
            ) from exc
        except ProviderSettingsError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
                retryable=False,
            ) from exc
        return {"schema": "zhimeng.provider-models.v1", "models": models}

    @router.post("/api/v1/settings/provider/test")
    async def test_provider(request: ProviderTestRequest) -> dict:
        active = require()
        try:
            return await run_in_threadpool(
                active.test_model,
                provider_id=request.provider_id,
                base_url=request.base_url,
                api_key=request.api_key,
                model=request.model,
                prompt=request.prompt,
                wire_format=request.wire_format,
                auth_type=request.auth_type,
                extra_headers=request.extra_headers,
                remove_header_names=request.remove_header_names,
                accepted_reported_models=request.accepted_reported_models,
                test_scope=request.test_scope,
            )
        except ProviderProbeError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.http_status or status.HTTP_502_BAD_GATEWAY,
                code=exc.code,
                retryable=True,
            ) from exc
        except ProviderSettingsError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
                retryable=False,
            ) from exc

    @router.post("/api/v1/settings/provider/models/liveness")
    async def test_models_liveness(request: ProviderLivenessRequest) -> dict:
        """一键测活：逐个实测这批模型，返回哪些能用、出字多快。"""
        active = require()
        try:
            return await run_in_threadpool(
                active.test_models,
                provider_id=request.provider_id,
                base_url=request.base_url,
                api_key=request.api_key,
                models=request.models,
                prompt=request.prompt,
                wire_format=request.wire_format,
                auth_type=request.auth_type,
                extra_headers=request.extra_headers,
                remove_header_names=request.remove_header_names,
                accepted_reported_models=request.accepted_reported_models,
                test_scope=request.test_scope,
            )
        except ProviderProbeError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_502_BAD_GATEWAY,
                code=exc.code,
                retryable=True,
            ) from exc
        except ProviderSettingsError as exc:
            raise ApiError(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
                retryable=False,
            ) from exc

    @router.delete("/api/v1/settings/provider/routes")
    async def clear_verified_routes(request: ProviderRoutesDeleteRequest) -> dict:
        """清空这条线路的测活记忆（前端「重测全部」时用）。"""
        active = require()
        try:
            key = (request.api_key or "").strip() or (
                active._profile_key(request.provider_id) if request.provider_id else ""
            )
            count = await run_in_threadpool(
                active.clear_verified_routes,
                base_url=request.base_url,
                api_key=key or "",
                provider_id=request.provider_id,
                auth_type=request.auth_type,
                extra_headers=request.extra_headers,
                remove_header_names=request.remove_header_names,
            )
        except ProviderSettingsError as exc:
            raise ApiError(str(exc), status_code=exc.status_code,
                           code=exc.code, retryable=False) from exc
        return {"cleared": count}

    return router
