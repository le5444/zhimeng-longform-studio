"""模型能力卡接口测试：设置页选模型时读这张卡。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _settings(tmp_path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(),
        trusted_skill_roots=(),
        managed_skill_root=None,
        allowed_origins=(),
    )


def test_capability_endpoint_returns_card(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        r = client.get("/api/v1/settings/model-capabilities/deepseek-v3.2")
        assert r.status_code == 200, r.text
        card = r.json()
        assert card["model_id"] == "deepseek-v3.2"
        assert card["default_wire"] == "responses"
        assert card["supports_reasoning_content"] is True


def test_capability_suggestion_does_not_change_explicit_provider_wire(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        saved = client.put(
            "/api/v1/settings/providers",
            json={
                "provider_id": "relay",
                "name": "Test relay",
                "base_url": "https://relay.example/v1",
                "api_key": "test-key",
                "model": "deepseek-v3.2",
                "wire_format": "chat",
                "make_active": True,
            },
        )
        assert saved.status_code == 200, saved.text
        before = client.get("/api/v1/settings/provider")
        assert before.status_code == 200, before.text
        assert before.json()["wire_format"] == "chat"

        card = client.get("/api/v1/settings/model-capabilities/deepseek-v3.2")
        assert card.status_code == 200, card.text
        assert card.json()["default_wire"] == "responses"

        after = client.get("/api/v1/settings/provider")
        assert after.status_code == 200, after.text
        assert after.json() == before.json()


def test_capability_endpoint_unknown_model_falls_back(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        r = client.get("/api/v1/settings/model-capabilities/some-new-model")
        assert r.status_code == 200
        card = r.json()
        assert card["default_wire"] in ("chat", "responses", "anthropic_messages")
        assert card["context_window_tier"] in ("256k", "128k", "1m")


def test_capability_endpoint_grok_defaults(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        r = client.get("/api/v1/settings/model-capabilities/grok-4.6")
        card = r.json()
        assert card["default_wire"] == "responses"
        assert "max" in card["effort_levels"]
