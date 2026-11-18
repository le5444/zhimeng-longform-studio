from __future__ import annotations

import pytest

from zhimeng_runtime.provider_url_resolver import (
    resolve_endpoint,
    resolve_models_url,
    strip_known_suffixes,
)


def test_resolve_endpoint_appends_protocol_path() -> None:
    assert (
        resolve_endpoint("https://x.com/v1", "chat")
        == "https://x.com/v1/chat/completions"
    )
    assert (
        resolve_endpoint("https://x.com/v1", "responses")
        == "https://x.com/v1/responses"
    )


def test_resolve_endpoint_handles_trailing_slash_and_existing_path() -> None:
    # 末尾斜杠与中转常见的已有 path（如 /api/v1）都不能拼出双斜杠或丢 path
    assert (
        resolve_endpoint("https://x.com/v1/", "chat")
        == "https://x.com/v1/chat/completions"
    )
    assert (
        resolve_endpoint("https://x.com/api/v1", "responses")
        == "https://x.com/api/v1/responses"
    )


def test_resolve_endpoint_does_not_duplicate_when_base_already_ends_with_it() -> None:
    assert (
        resolve_endpoint("https://x.com/v1/chat/completions", "chat")
        == "https://x.com/v1/chat/completions"
    )
    assert (
        resolve_endpoint("https://x.com/v1/responses/", "responses")
        == "https://x.com/v1/responses"
    )


def test_resolve_endpoint_explicit_url_wins() -> None:
    assert (
        resolve_endpoint(
            "https://x.com/v1",
            "chat",
            explicit_url="https://other.example/special/endpoint",
        )
        == "https://other.example/special/endpoint"
    )


def test_resolve_endpoint_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError):
        resolve_endpoint("https://x.com/v1", "grpc")


def test_resolve_models_url_prefers_explicit_then_appends() -> None:
    assert (
        resolve_models_url("https://x.com/v1", "https://x.com/internal/model-list")
        == "https://x.com/internal/model-list"
    )
    assert resolve_models_url("https://x.com/v1") == "https://x.com/v1/models"
    assert resolve_models_url("https://x.com/v1/") == "https://x.com/v1/models"
    assert (
        resolve_models_url("https://x.com/v1/models") == "https://x.com/v1/models"
    )


def test_strip_known_suffixes_powers_normalize_base_url() -> None:
    assert strip_known_suffixes("https://x.com/v1/chat/completions") == "https://x.com/v1"
    assert strip_known_suffixes("https://x.com/v1/models") == "https://x.com/v1"
    assert strip_known_suffixes("https://x.com/v1/responses") == "https://x.com/v1"
    assert strip_known_suffixes("  https://x.com/v1/  ") == "https://x.com/v1"
    assert strip_known_suffixes("https://x.com/api/v1") == "https://x.com/api/v1"


@pytest.mark.parametrize("raw", ["https://x.com/v1/messages", "  https://x.com/v1/messages/  "])
def test_pasted_anthropic_endpoint_normalizes_for_models_and_protocol_switch(raw: str) -> None:
    from zhimeng_runtime.provider_settings_service import normalize_base_url

    base = normalize_base_url(raw)
    assert base == "https://x.com/v1"
    assert resolve_models_url(base) == "https://x.com/v1/models"
    assert resolve_endpoint(base, "anthropic_messages") == "https://x.com/v1/messages"
    assert resolve_endpoint(base, "responses") == "https://x.com/v1/responses"
