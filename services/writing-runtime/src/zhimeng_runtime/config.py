from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:1420",
    "http://localhost:1420",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
DEFAULT_MANAGED_SKILL_ROOT = (
    Path(__file__).resolve().parents[4] / "adapters" / "builtin"
)


class RuntimeSettings(BaseModel):
    """Validated process configuration loaded from ZHIMENG_* variables."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str = "development"
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8787, ge=0, le=65535)
    database_path: Path = Path("data/writing-runtime.sqlite3")
    backup_root: Path | None = None
    diagnostics_root: Path | None = None
    trusted_project_roots: tuple[Path, ...] = ()
    trusted_source_roots: tuple[Path, ...] = ()
    trusted_skill_roots: tuple[Path, ...] = ()
    managed_skill_root: Path | None = DEFAULT_MANAGED_SKILL_ROOT
    tomato_skill_root: Path | None = None
    tomato_downloader_script: Path | None = None
    tomato_download_dir: Path = Path.home() / "Desktop" / "番茄下载"
    tomato_server_url: str = "http://127.0.0.1:18423"
    tomato_max_wait_minutes: int = Field(default=180, ge=5, le=1440)
    powershell_executable: str = "powershell.exe"
    distillation_skill_root: Path | None = None
    distillation_library: Path | None = None
    distillation_python_executable: Path = Path(sys.executable).resolve()
    distillation_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    agent_provider_base_url: str | None = None
    agent_provider_api_key: str | None = Field(default=None, repr=False)
    agent_provider_credential_reference: str | None = None
    agent_provider_model: str | None = None
    agent_provider_accepted_reported_models: tuple[str, ...] = ()
    agent_provider_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    agent_provider_max_tokens: int = Field(default=4096, ge=128, le=131_072)
    agent_provider_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    sse_heartbeat_seconds: float = Field(default=15.0, ge=0.05, le=300.0)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("environment must not be empty")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        unique: list[str] = []
        for origin in origins:
            normalized = _validate_origin(origin)
            if normalized not in unique:
                unique.append(normalized)
        return tuple(unique)

    @field_validator(
        "trusted_project_roots",
        "trusted_source_roots",
        "trusted_skill_roots",
    )
    @classmethod
    def validate_trusted_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        unique: list[Path] = []
        for root in roots:
            candidate = root.expanduser()
            if not candidate.is_absolute():
                raise ValueError("trusted roots must be absolute paths")
            if candidate not in unique:
                unique.append(candidate)
        return tuple(unique)

    @field_validator("managed_skill_root")
    @classmethod
    def validate_managed_skill_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("managed Skill root must be an absolute path")
        return candidate

    @field_validator(
        "tomato_skill_root",
        "tomato_downloader_script",
        "tomato_download_dir",
    )
    @classmethod
    def validate_tomato_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("Tomato adapter paths must be absolute")
        return candidate

    @field_validator("tomato_server_url")
    @classmethod
    def validate_tomato_server_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Tomato downloader server must be a loopback HTTP origin")
        return candidate

    @field_validator("powershell_executable")
    @classmethod
    def validate_powershell_executable(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("powershell_executable must not be empty")
        return candidate

    @field_validator(
        "distillation_skill_root",
        "distillation_library",
        "distillation_python_executable",
    )
    @classmethod
    def validate_distillation_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("Distillation adapter paths must be absolute")
        return candidate

    @model_validator(mode="after")
    def validate_distillation_configuration(self) -> "RuntimeSettings":
        configured = (
            self.distillation_skill_root is not None,
            self.distillation_library is not None,
        )
        if configured[0] != configured[1]:
            raise ValueError(
                "Distillation Skill root and library must be configured together"
            )
        return self

    @field_validator("agent_provider_base_url")
    @classmethod
    def validate_agent_provider_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().rstrip("/")
        if not candidate:
            return None
        parsed = urlsplit(candidate)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if (
            not (parsed.scheme == "https" or loopback_http)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Agent provider must use HTTPS or an explicit loopback HTTP URL"
            )
        return candidate

    @field_validator(
        "agent_provider_model",
        "agent_provider_api_key",
        "agent_provider_credential_reference",
    )
    @classmethod
    def normalize_optional_agent_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        return candidate or None

    @field_validator("agent_provider_accepted_reported_models")
    @classmethod
    def validate_agent_provider_accepted_reported_models(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            candidate = value.strip()
            if not candidate or len(candidate) > 512:
                raise ValueError(
                    "Agent provider accepted reported models must be bounded non-blank names"
                )
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(sorted(normalized))

    @field_validator("agent_provider_credential_reference")
    @classmethod
    def validate_agent_provider_credential_reference(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and (
            len(value) > 256 or any(character.isspace() for character in value)
        ):
            raise ValueError(
                "Agent provider credential reference must be a compact "
                "non-secret identifier"
            )
        return value

    @model_validator(mode="after")
    def validate_agent_provider_configuration(self) -> "RuntimeSettings":
        if (self.agent_provider_base_url is None) != (self.agent_provider_model is None):
            raise ValueError(
                "Agent provider base URL and model must be configured together"
            )
        if (self.agent_provider_api_key is None) != (
            self.agent_provider_credential_reference is None
        ):
            raise ValueError(
                "Agent provider API key and credential reference must be configured together"
            )
        if (
            self.agent_provider_accepted_reported_models
            and self.agent_provider_model is None
        ):
            raise ValueError(
                "Agent provider accepted reported models require a configured provider"
            )
        return self

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeSettings":
        source = os.environ if environ is None else environ
        origins = _parse_origins(
            source.get("ZHIMENG_ALLOWED_ORIGINS"),
            default=DEFAULT_ALLOWED_ORIGINS,
        )
        distillation_root = _optional_adapter_path(
            source.get("ZHIMENG_DISTILLATION_SKILL_ROOT"),
            default=None,
        )
        distillation_library = _optional_adapter_path(
            source.get("ZHIMENG_DISTILLATION_LIBRARY"),
            default=(distillation_root / "library" if distillation_root else None),
        )
        provider_api_key = source.get("ZHIMENG_AGENT_PROVIDER_API_KEY")
        configured_provider_credential_reference = source.get(
            "ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE"
        )
        provider_credential_reference = (
            configured_provider_credential_reference.strip()
            if configured_provider_credential_reference is not None
            and configured_provider_credential_reference.strip()
            else (
                "env:ZHIMENG_AGENT_PROVIDER_API_KEY"
                if provider_api_key is not None and provider_api_key.strip()
                else None
            )
        )
        return cls(
            environment=source.get("ZHIMENG_ENVIRONMENT", "development"),
            host=source.get("ZHIMENG_HOST", "127.0.0.1"),
            port=int(source.get("ZHIMENG_PORT", "8787")),
            database_path=Path(
                source.get(
                    "ZHIMENG_DATABASE_PATH",
                    "data/writing-runtime.sqlite3",
                )
            ).expanduser(),
            backup_root=(
                Path(source["ZHIMENG_BACKUP_ROOT"]).expanduser()
                if source.get("ZHIMENG_BACKUP_ROOT")
                else None
            ),
            diagnostics_root=(
                Path(source["ZHIMENG_DIAGNOSTICS_ROOT"]).expanduser()
                if source.get("ZHIMENG_DIAGNOSTICS_ROOT")
                else None
            ),
            trusted_project_roots=_parse_project_roots(
                source.get("ZHIMENG_TRUSTED_PROJECT_ROOTS")
                or source.get("ZHIMENG_WRITING_ROOTS")
            ),
            trusted_source_roots=_parse_path_roots(
                source.get("ZHIMENG_TRUSTED_SOURCE_ROOTS"),
                variable_name="ZHIMENG_TRUSTED_SOURCE_ROOTS",
            ),
            trusted_skill_roots=_parse_path_roots(
                source.get("ZHIMENG_TRUSTED_SKILL_ROOTS"),
                variable_name="ZHIMENG_TRUSTED_SKILL_ROOTS",
            ),
            managed_skill_root=_optional_adapter_path(
                source.get("ZHIMENG_MANAGED_SKILL_ROOT"),
                default=DEFAULT_MANAGED_SKILL_ROOT,
            ),
            tomato_skill_root=_optional_adapter_path(
                source.get("ZHIMENG_TOMATO_SKILL_ROOT"),
                default=None,
            ),
            tomato_downloader_script=_optional_adapter_path(
                source.get("ZHIMENG_TOMATO_DOWNLOADER_SCRIPT"),
                default=None,
            ),
            tomato_download_dir=Path(
                source.get(
                    "ZHIMENG_TOMATO_DOWNLOAD_DIR",
                    str(Path.home() / "Desktop" / "番茄下载"),
                )
            ).expanduser(),
            tomato_server_url=source.get(
                "ZHIMENG_TOMATO_SERVER_URL",
                "http://127.0.0.1:18423",
            ),
            tomato_max_wait_minutes=int(
                source.get("ZHIMENG_TOMATO_MAX_WAIT_MINUTES", "180")
            ),
            powershell_executable=source.get(
                "ZHIMENG_POWERSHELL_EXECUTABLE",
                "powershell.exe",
            ),
            distillation_skill_root=distillation_root,
            distillation_library=distillation_library,
            distillation_python_executable=Path(
                source.get("ZHIMENG_DISTILLATION_PYTHON_EXECUTABLE")
                or sys.executable
            ).expanduser(),
            distillation_timeout_seconds=float(
                source.get("ZHIMENG_DISTILLATION_TIMEOUT_SECONDS", "120")
            ),
            agent_provider_base_url=source.get("ZHIMENG_AGENT_PROVIDER_BASE_URL"),
            agent_provider_api_key=provider_api_key,
            agent_provider_credential_reference=provider_credential_reference,
            agent_provider_model=source.get("ZHIMENG_AGENT_PROVIDER_MODEL"),
            agent_provider_accepted_reported_models=_parse_string_list(
                source.get("ZHIMENG_AGENT_PROVIDER_ACCEPTED_REPORTED_MODELS")
            ),
            agent_provider_timeout_seconds=float(
                source.get("ZHIMENG_AGENT_PROVIDER_TIMEOUT_SECONDS", "120")
            ),
            agent_provider_max_tokens=int(
                source.get("ZHIMENG_AGENT_PROVIDER_MAX_TOKENS", "4096")
            ),
            agent_provider_temperature=float(
                source.get("ZHIMENG_AGENT_PROVIDER_TEMPERATURE", "0.7")
            ),
            allowed_origins=origins,
            sse_heartbeat_seconds=float(
                source.get("ZHIMENG_SSE_HEARTBEAT_SECONDS", "15")
            ),
        )


def _optional_adapter_path(raw: str | None, *, default: Path | None) -> Path | None:
    if raw is None:
        return default
    candidate = raw.strip()
    return Path(candidate).expanduser() if candidate else None


def _parse_origins(
    raw: str | None,
    *,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped:
        return ()

    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise ValueError("ZHIMENG_ALLOWED_ORIGINS must be a list of strings")
        return tuple(item.strip() for item in parsed if item.strip())

    return tuple(item.strip() for item in stripped.split(",") if item.strip())


def _parse_string_list(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    stripped = raw.strip()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise ValueError("model allowlist must be a list of strings")
        return tuple(item.strip() for item in parsed if item.strip())
    return tuple(item.strip() for item in stripped.split(",") if item.strip())


def _parse_project_roots(raw: str | None) -> tuple[Path, ...]:
    return _parse_path_roots(raw, variable_name="ZHIMENG_TRUSTED_PROJECT_ROOTS")


def _parse_path_roots(
    raw: str | None,
    *,
    variable_name: str,
) -> tuple[Path, ...]:
    if raw is None or not raw.strip():
        return ()
    stripped = raw.strip()
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise ValueError(f"{variable_name} must be a list of paths")
        values = [item.strip() for item in parsed if item.strip()]
    else:
        values = [item.strip() for item in stripped.split(";") if item.strip()]
    return tuple(Path(item).expanduser() for item in values)


def _validate_origin(origin: str) -> str:
    candidate = origin.strip()
    if not candidate or "*" in candidate:
        raise ValueError("CORS origins must be explicit and cannot contain wildcards")

    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid HTTP(S) origin: {candidate!r}")
    if parsed.username or parsed.password:
        raise ValueError("CORS origins cannot contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("CORS origins cannot contain a path, query, or fragment")
    if candidate.endswith("/"):
        raise ValueError("CORS origins must not end with a slash")
    return candidate
