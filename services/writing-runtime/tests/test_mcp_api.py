from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from test_agent_api import ApiProvider
from zhimeng_runtime.app import create_app
from zhimeng_runtime.mcp_api import McpEnabledRequest, McpServerListResponse, McpToolListResponse
from zhimeng_runtime.mcp_models import McpServerUpdate, McpServerView


def test_mcp_all_endpoints_roundtrip_and_private_env_not_returned(settings, tmp_path):
    body = {"name": "合成测试工具", "command": "nonexistent-test-fixture", "args": ["中文 path"],
            "cwd": str(tmp_path), "enabled": False, "env": {"PRIVATE_SAMPLE": "mcp-api-synthetic-private-873"}}
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        base = "/api/v1/mcp/servers"
        assert client.get(base).json() == {"servers": []}
        created = client.post(base, json=body)
        assert created.status_code == 201, created.text
        entry = created.json()
        cid = entry["connection_id"]
        assert entry["env_names"] == ["PRIVATE_SAMPLE"]
        assert entry["state"] == "disabled" and entry["enabled"] is False
        assert "mcp-api-synthetic-private" not in created.text
        assert "mcp-api-synthetic-private" not in client.get(base).text
        path = client.app.state.mcp_service.settings.path
        assert "mcp-api-synthetic-private" not in path.read_text(encoding="utf-8")
        body["env"] = {"PRIVATE_SAMPLE": ""}
        body["name"] = "已改名"
        updated = client.put(base + "/" + cid, json=body)
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "已改名"
        assert client.app.state.mcp_service.settings.get(cid).env["PRIVATE_SAMPLE"] == "mcp-api-synthetic-private-873"
        assert client.get(base + "/" + cid + "/tools").json() == {"tools": []}
        reconnect = client.post(base + "/" + cid + "/reconnect")
        assert reconnect.status_code == 200, reconnect.text
        enabled = client.patch(base + "/" + cid + "/enabled", json={"enabled": True})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["state"] in {"connecting", "error"}
        assert enabled.json()["enabled"] is True
        disabled = client.patch(base + "/" + cid + "/enabled", json={"enabled": False})
        assert disabled.status_code == 200 and disabled.json()["state"] == "disabled"
        body["remove_env"] = ["PRIVATE_SAMPLE"]
        assert client.put(base + "/" + cid, json=body).status_code == 200
        assert client.app.state.mcp_service.settings.get(cid).env == {}
        assert client.delete(base + "/" + cid).status_code == 204
        assert client.get(base).json() == {"servers": []}
        assert client.get("/openapi.json").status_code == 200
        for method, suffix, payload in [("get", "/tools", None), ("post", "/reconnect", None),
                                        ("delete", "", None), ("put", "", body), ("patch", "/enabled", {"enabled": True})]:
            response = client.request(method, base + "/" + cid + suffix, json=payload)
            assert response.status_code == 404, response.text


def test_invalid_mcp_config_is_validation_failure(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        base = "/api/v1/mcp/servers"
        for body in ({"name": "", "command": "x"}, {"name": "x", "command": ""},
                     {"name": "x", "command": "x", "cwd": str(tmp_path / "missing")},
                     {"name": "x", "command": "x", "arbitrary": True}):
            response = client.post(base, json=body)
            assert response.status_code == 422, response.text
        assert client.get(base).json() == {"servers": []}


def test_http_settings_keep_transport_and_private_headers_when_toggled_or_restarted(settings, tmp_path):
    secret = "synthetic-http-header-private-947"
    replacement = "synthetic-http-replacement-213"
    base = "/api/v1/mcp/servers"
    body = {"name": "网络文档工具", "transport": "http", "url": "http://127.0.0.1:1/mcp",
            "enabled": False, "headers": {"Authorization": secret, "X-Service-Key": replacement}}
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        created = client.post(base, json=body)
        assert created.status_code == 201, created.text
        cid = created.json()["connection_id"]
        assert created.json()["transport"] == "http" and created.json()["url"] == body["url"]
        assert set(created.json()["header_names"]) == {"Authorization", "X-Service-Key"}
        assert secret not in created.text and replacement not in created.text
        assert secret not in client.get(base).text
        path = client.app.state.mcp_service.settings.path
        assert secret.encode() not in path.read_bytes() and replacement.encode() not in path.read_bytes()
        for enabled in (True, False):
            toggled = client.patch(base + "/" + cid + "/enabled", json={"enabled": enabled})
            assert toggled.status_code == 200, toggled.text
            assert toggled.json()["transport"] == "http" and toggled.json()["url"] == body["url"]
            config = client.app.state.mcp_service.settings.get(cid)
            assert config.headers["Authorization"] == secret and config.enabled is enabled
        body["headers"] = {"authorization": ""}
        preserved = client.put(base + "/" + cid, json=body)
        assert preserved.status_code == 200, preserved.text
        assert {key.lower(): value for key, value in client.app.state.mcp_service.settings.get(cid).headers.items()}["authorization"] == secret
        body["headers"] = {"authorization": replacement}
        body["remove_headers"] = ["x-service-key"]
        changed = client.put(base + "/" + cid, json=body)
        assert changed.status_code == 200, changed.text
        assert len(changed.json()["header_names"]) == 1
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        restored = client.get(base).json()["servers"][0]
        assert restored["transport"] == "http" and restored["state"] == "disabled"
        assert restored["url"] == body["url"]
        config = client.app.state.mcp_service.settings.get(cid)
        assert {key.lower(): value for key, value in config.headers.items()} == {"authorization": replacement}
        assert client.delete(base + "/" + cid).status_code == 204


@pytest.mark.parametrize("url", ["", "file:///C:/fixture", "ftp://example.invalid/mcp", "not-a-url"])
def test_invalid_http_address_does_not_save_connection(settings, url):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        response = client.post("/api/v1/mcp/servers", json={"name": "网络工具", "transport": "http", "url": url})
        assert response.status_code == 422, response.text
        assert client.get("/api/v1/mcp/servers").json() == {"servers": []}


def test_corrupt_encrypted_configuration_never_exposes_decrypted_env_in_api_errors(settings, tmp_path):
    secret = "fake-ZYXA987"
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        response = client.post("/api/v1/mcp/servers", json={"name": "合成配置", "command": "fixture",
                               "enabled": False, "cwd": str(tmp_path), "env": {"ODD": secret}})
        assert response.status_code == 201
        path = client.app.state.mcp_service.settings.path
    raw = json.loads(path.read_bytes())
    del raw["connections"][0]["name"]
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    assert secret.encode() not in path.read_bytes()
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        failed = client.get("/api/v1/mcp/servers")
        assert failed.status_code == 503
        assert secret not in failed.text and "input_value" not in failed.text
        assert "配置格式不正确" in failed.json()["error"]["message"]
        save = client.post("/api/v1/mcp/servers", json={"name": "修订", "command": "fixture", "enabled": False})
        assert save.status_code == 422
        assert secret not in save.text and "input_value" not in save.text


@pytest.mark.parametrize("filename,model", [
    ("mcp-server-update.schema.json", McpServerUpdate), ("mcp-server-view.schema.json", McpServerView),
    ("mcp-server-list.schema.json", McpServerListResponse), ("mcp-enabled-request.schema.json", McpEnabledRequest),
    ("mcp-tool-list.schema.json", McpToolListResponse),
])
def test_mcp_exported_contract_matches_model(filename, model):
    path = Path(__file__).resolve().parents[3] / "packages/contracts" / filename
    saved = json.loads(path.read_bytes())
    Draft202012Validator.check_schema(saved)
    saved.pop("$id")
    saved.pop("$schema")
    assert saved == model.model_json_schema(by_alias=True)
