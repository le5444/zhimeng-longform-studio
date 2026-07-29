from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.provider_settings_service import (
    ProviderProbeError,
    ProviderSettingsError,
    ProviderSettingsService,
)


class ProviderSettingsUpdate(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    # 不传 = 不修改；传空串 = 清除已保存的密钥
    api_key: str | None = Field(default=None, max_length=500)


class ProviderModelsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    # 不传就用已保存的密钥，作者不必每次重新粘贴
    api_key: str | None = Field(default=None, max_length=500)


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

    @router.get("/api/v1/settings/provider")
    async def read_settings() -> dict:
        return await run_in_threadpool(require().public_view)

    @router.put("/api/v1/settings/provider")
    async def update_settings(request: ProviderSettingsUpdate) -> dict:
        active = require()
        try:
            return await run_in_threadpool(
                active.save,
                base_url=request.base_url,
                model=request.model,
                api_key=request.api_key,
            )
        except ProviderSettingsError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
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
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.code,
                retryable=False,
            ) from exc
        return {"schema": "zhimeng.provider-models.v1", "models": models}

    return router
