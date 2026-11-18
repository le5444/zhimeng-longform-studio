from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from zhimeng_runtime.config import RuntimeSettings


def test_environment_parses_json_origin_allowlist() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_ALLOWED_ORIGINS": (
                '["http://127.0.0.1:1420", "https://writer.example.test"]'
            ),
            "ZHIMENG_DATABASE_PATH": "test.sqlite3",
        }
    )

    assert settings.allowed_origins == (
        "http://127.0.0.1:1420",
        "https://writer.example.test",
    )


def test_environment_parses_comma_separated_origin_allowlist() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_ALLOWED_ORIGINS": (
                "http://127.0.0.1:1420,http://localhost:1420"
            )
        }
    )

    assert settings.allowed_origins == (
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    )


def test_provider_secret_uses_stable_non_secret_environment_reference() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_AGENT_PROVIDER_BASE_URL": "https://provider.test/v1",
            "ZHIMENG_AGENT_PROVIDER_MODEL": "test-model",
            "ZHIMENG_AGENT_PROVIDER_API_KEY": "secret-value",
        }
    )

    assert settings.agent_provider_credential_reference == (
        "env:ZHIMENG_AGENT_PROVIDER_API_KEY"
    )
    assert "secret-value" not in repr(settings)


def test_provider_secret_accepts_explicit_credential_reference() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_AGENT_PROVIDER_BASE_URL": "https://provider.test/v1",
            "ZHIMENG_AGENT_PROVIDER_MODEL": "test-model",
            "ZHIMENG_AGENT_PROVIDER_API_KEY": "secret-value",
            "ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE": (
                "secret-store:provider-secondary"
            ),
        }
    )

    assert (
        settings.agent_provider_credential_reference
        == "secret-store:provider-secondary"
    )
    assert "secret-value" not in repr(settings)


def test_blank_provider_credential_reference_uses_environment_slot() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_AGENT_PROVIDER_API_KEY": "secret-value",
            "ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE": "   ",
        }
    )

    assert settings.agent_provider_credential_reference == (
        "env:ZHIMENG_AGENT_PROVIDER_API_KEY"
    )


@pytest.mark.parametrize(
    "credential_reference",
    ["secret store:provider-primary", "x" * 257],
)
def test_provider_credential_reference_must_be_compact(
    credential_reference: str,
) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(
            agent_provider_api_key="secret-value",
            agent_provider_credential_reference=credential_reference,
        )


def test_environment_provider_credential_reference_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.from_environment(
            {
                "ZHIMENG_AGENT_PROVIDER_CREDENTIAL_REFERENCE": (
                    "secret-store:provider-primary"
                )
            }
        )


def test_environment_parses_explicit_reported_model_aliases() -> None:
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_AGENT_PROVIDER_BASE_URL": "https://provider.test/v1",
            "ZHIMENG_AGENT_PROVIDER_MODEL": "requested-model",
            "ZHIMENG_AGENT_PROVIDER_ACCEPTED_REPORTED_MODELS": (
                '["reported-b", "reported-a", "reported-a"]'
            ),
        }
    )

    assert settings.agent_provider_accepted_reported_models == (
        "reported-a",
        "reported-b",
    )


def test_reported_model_aliases_are_valid_without_env_provider() -> None:
    """接受名单不依赖 env provider：模型可在界面（provider-settings.json）配置，
    名单对运行时实际解析出的 provider 全局生效（2026-09-05 中转站改报
    deepseek-ai/deepseek-v4-flash-0731 时，作者正是这种配置）。"""
    settings = RuntimeSettings(
        agent_provider_accepted_reported_models=("reported-model",),
    )

    assert settings.agent_provider_accepted_reported_models == ("reported-model",)


def test_provider_key_and_credential_reference_must_be_paired() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(agent_provider_api_key="secret-value")
    with pytest.raises(ValidationError):
        RuntimeSettings(
            agent_provider_credential_reference="secret-store:provider-primary"
        )


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "http://*.example.test",
        "file:///tmp/index.html",
        "https://example.test/path",
        "https://user:password@example.test",
        "https://example.test/",
    ],
)
def test_origin_allowlist_rejects_non_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(allowed_origins=(origin,))


def test_non_loopback_runtime_host_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(host="0.0.0.0")


def test_environment_parses_explicit_trusted_project_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    settings = RuntimeSettings.from_environment(
        {"ZHIMENG_TRUSTED_PROJECT_ROOTS": f"{first};{second}"}
    )

    assert settings.trusted_project_roots == (first, second)


def test_environment_parses_explicit_trusted_skill_roots(tmp_path: Path) -> None:
    first = tmp_path / "first-skill-root"
    second = tmp_path / "second-skill-root"
    settings = RuntimeSettings.from_environment(
        {"ZHIMENG_TRUSTED_SKILL_ROOTS": json.dumps([str(first), str(second)])}
    )

    assert settings.trusted_skill_roots == (first, second)


def test_distillation_paths_are_backend_only_absolute_configuration(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "novel-distillation"
    library = skill_root / "library"
    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_DISTILLATION_SKILL_ROOT": str(skill_root),
            "ZHIMENG_DISTILLATION_LIBRARY": str(library),
            "ZHIMENG_DISTILLATION_PYTHON_EXECUTABLE": str(
                Path(__file__).resolve()
            ),
            "ZHIMENG_DISTILLATION_TIMEOUT_SECONDS": "90",
        }
    )

    assert settings.distillation_skill_root == skill_root
    assert settings.distillation_library == library
    assert settings.distillation_timeout_seconds == 90


def test_distillation_root_and_library_must_be_configured_together(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(distillation_skill_root=tmp_path)
    with pytest.raises(ValidationError):
        RuntimeSettings(
            distillation_skill_root=Path("relative-skill"),
            distillation_library=Path("relative-library"),
        )


def test_user_level_skills_are_not_auto_discovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tomato_root = tmp_path / ".codex" / "skills" / "tomato-novel-auto-distill"
    tomato_root.mkdir(parents=True)
    downloader = (
        tmp_path
        / "Documents"
        / "Codex"
        / "tools"
        / "tomato-novel-downloader"
        / "Download-TomatoNovel.ps1"
    )
    downloader.parent.mkdir(parents=True)
    downloader.write_text("# fixture", encoding="utf-8")
    distillation_root = tmp_path / ".codex" / "skills" / "novel-distillation"
    (distillation_root / "scripts").mkdir(parents=True)
    (distillation_root / "scripts" / "distill_kb.py").write_text(
        "# fixture", encoding="utf-8"
    )
    library = distillation_root / "library"
    library.mkdir()
    for name in ("manifest.json", "cards.jsonl", "feedback.jsonl"):
        (library / name).write_text("", encoding="utf-8")

    settings = RuntimeSettings.from_environment({})

    assert settings.tomato_skill_root is None
    assert settings.tomato_downloader_script is None
    assert settings.distillation_skill_root is None
    assert settings.distillation_library is None


def test_empty_distillation_python_setting_uses_runtime_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    settings = RuntimeSettings.from_environment(
        {
            "ZHIMENG_DISTILLATION_SKILL_ROOT": "",
            "ZHIMENG_DISTILLATION_LIBRARY": "",
            "ZHIMENG_DISTILLATION_PYTHON_EXECUTABLE": "",
        }
    )

    assert settings.distillation_python_executable == Path(sys.executable).resolve()
