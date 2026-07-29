"""作者在界面里配置写作模型：接口地址 + 密钥 + 模型。

设计要点：

* 作者不该被要求去改环境变量或 .env。配置存成本地 JSON，界面里填。
* 密钥**只进不出**：保存后任何读取接口都不回传明文，只回传"已设置/未设置"。
* 「一键获取模型」只探测 `/models` 端点，**不调用模型生成**，不产生费用。
* 环境变量仍然有效，作为回退（方便自动化和验收脚本）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

SETTINGS_SCHEMA = "zhimeng.provider-settings.v1"


class ProviderSettingsError(RuntimeError):
    code = "provider_settings_failed"


class ProviderProbeError(ProviderSettingsError):
    code = "provider_probe_failed"


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    base_url: str | None
    model: str | None
    api_key: str | None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


def normalize_base_url(raw: str) -> str:
    """把作者可能填错的地址收敛成可用的 base。

    允许作者直接粘贴 `https://x.com`、`https://x.com/v1`、
    甚至 `https://x.com/v1/chat/completions`。
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        raise ProviderSettingsError("接口地址不能为空")
    for suffix in ("/chat/completions", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ProviderSettingsError("接口地址必须以 http:// 或 https:// 开头")
    return value


def _friendly_probe_error(status_code: int | None, detail: str) -> str:
    if status_code in (401, 403):
        return "密钥被拒绝（401/403）。请检查 Key 是否填错或已过期。"
    if status_code in (404, 405):
        return (
            "地址能连上，但没有 /models 接口（404/405）。"
            "多数服务要把地址填到 /v1 结尾，例如 https://xxx.com/v1。"
        )
    if status_code is not None and status_code >= 500:
        return f"对方服务器出错（{status_code}），稍后再试。"
    return (detail or "获取模型列表失败。")[:260]


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

    # ---------------------------------------------------------------- 存取
    def _read_file(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _fallback_settings(self) -> ProviderSettings:
        return self._fallback or ProviderSettings(
            base_url=os.environ.get("ZHIMENG_AGENT_PROVIDER_BASE_URL") or None,
            model=os.environ.get("ZHIMENG_AGENT_PROVIDER_MODEL") or None,
            api_key=os.environ.get("ZHIMENG_AGENT_PROVIDER_API_KEY") or None,
        )

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
        )

    def _credential_reference_from(
        self,
        stored: dict,
        effective: ProviderSettings,
    ) -> str | None:
        if not effective.api_key:
            return None
        if stored.get("api_key"):
            material = (
                f"{self._path.resolve(strict=False)}\0{effective.api_key}"
            ).encode("utf-8")
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
        return self._effective_from(self._read_file())

    def credential_reference(self) -> str | None:
        """返回不含密钥明文、但能识别密钥变更的本地引用。"""
        stored = self._read_file()
        return self._credential_reference_from(
            stored,
            self._effective_from(stored),
        )

    def resolve_agent_connection(self) -> tuple[ProviderSettings, str | None]:
        """一次读取内同时解析连接与密钥身份，避免保存时产生混合快照。"""
        stored = self._read_file()
        effective = self._effective_from(stored)
        return effective, self._credential_reference_from(stored, effective)

    def public_view(self) -> dict:
        """给界面看的状态。**绝不回传密钥明文。**"""
        stored = self._read_file()
        effective = self.effective()
        return {
            "schema": SETTINGS_SCHEMA,
            "base_url": effective.base_url or "",
            "model": effective.model or "",
            "api_key_set": bool(effective.api_key),
            # 让作者知道当前这份配置是界面里存的还是环境变量来的
            "source": "settings" if stored.get("base_url") else ("environment" if effective.base_url else "none"),
            "configured": effective.configured,
        }

    def save(
        self,
        *,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
    ) -> dict:
        stored = self._read_file()
        if base_url is not None:
            stored["base_url"] = normalize_base_url(base_url)
        if model is not None:
            stored["model"] = model.strip()
        # api_key 传 None 表示"不修改"，传空串表示"清除"
        if api_key is not None:
            cleaned = api_key.strip()
            if cleaned:
                stored["api_key"] = cleaned
            else:
                stored.pop("api_key", None)
        stored["schema"] = SETTINGS_SCHEMA

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return self.public_view()

    # ---------------------------------------------------------------- 探测
    def list_models(self, *, base_url: str, api_key: str | None) -> list[str]:
        """只探测模型列表端点，不调用模型生成，不产生 token 费用。"""
        base = normalize_base_url(base_url)
        key = (api_key or "").strip() or (self.effective().api_key or "")
        if not key:
            raise ProviderProbeError("还没有填写密钥。")

        request = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ProviderProbeError(_friendly_probe_error(exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderProbeError(f"连不上这个地址：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderProbeError("对方返回的不是合法 JSON，可能地址填错了。") from exc

        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ProviderProbeError(
                "请求成功，但没解析出模型列表；这个服务返回的可能不是 OpenAI 兼容格式。"
            )
        models = [
            str(item["id"])
            for item in entries
            if isinstance(item, dict) and item.get("id")
        ]
        if not models:
            raise ProviderProbeError("模型列表是空的，请确认这个账号有可用模型。")
        return sorted(models)
