"""MCP persistence, runtime application, and actual dispatch are separate facts."""
import asyncio
import json
from pathlib import Path
import sys
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.mcp_api import create_mcp_router
from zhimeng_runtime.mcp_client_service import McpClientService
from zhimeng_runtime.mcp_models import McpConnectionError, McpServerUpdate
from zhimeng_runtime.mcp_settings import McpSettingsService
from zhimeng_runtime.mcp_tool_adapter import McpToolAdapter
from zhimeng_runtime.settings_versions import SettingsConflictError
from test_mcp_runtime import config_for, ready


@pytest.fixture
def mcp_client(tmp_path):
    settings = McpSettingsService(tmp_path / "mcp.json")
    manager = McpClientService(settings, tmp_path)
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_mcp_router(manager, McpToolAdapter(manager)))
    with TestClient(app) as client:
        yield client, settings, manager


def sample(tmp_path, **changes):
    return {"name": "配置验收", "command": "fixture-only", "enabled": False, "cwd": str(tmp_path), **changes}


def test_two_mcp_editors_conflict_and_recovery_preserve_credentials(mcp_client, tmp_path):
    client, settings, _ = mcp_client
    body = sample(tmp_path, env={"ACCESS_TOKEN": "synthetic-sensitive"})
    original = client.post("/api/v1/mcp/servers", json=body).json()
    path = "/api/v1/mcp/servers/" + original["connection_id"]
    first = client.put(path, json={**body, "name": "first edit", "expected_revision": original["revision"]})
    assert first.status_code == 200 and first.json()["revision"] != original["revision"]
    before = settings.path.read_bytes()
    stale = client.put(path, json={**body, "name": "stale edit", "expected_revision": original["revision"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"
    assert settings.path.read_bytes() == before
    assert "synthetic-sensitive" not in first.text + stale.text + before.decode()
    response = client.put(path, json={**body, "name": "resolved edit", "expected_revision": first.json()["revision"]})
    assert response.status_code == 200
    result = response.json()
    assert result["revision"] == result["applied_revision"] and result["configuration_status"] == "applied"
    assert settings.get(original["connection_id"]).env == {"ACCESS_TOKEN": "synthetic-sensitive"}


def test_retried_creation_cannot_duplicate_or_overwrite_a_saved_connection(mcp_client, tmp_path):
    client, settings, manager = mcp_client
    body = sample(tmp_path, creation_id="mcp-" + "a" * 32, expected_revision="missing")
    first = client.post("/api/v1/mcp/servers", json=body)
    assert first.status_code == 201
    assert first.json()["connection_id"] == body["creation_id"]
    generation, before = manager._generation, settings.path.read_bytes()
    retry = client.post("/api/v1/mcp/servers", json={**body, "name": "must not overwrite"})
    assert retry.status_code == 409 and manager._generation == generation
    assert settings.path.read_bytes() == before and len(settings.list_configs()) == 1
    assert client.get("/api/v1/mcp/servers").json()["servers"][0]["connection_id"] == body["creation_id"]


@pytest.mark.parametrize("operation", ["enabled", "delete", "reconnect"])
def test_stale_mcp_actions_do_not_change_file_or_runtime(mcp_client, tmp_path, operation):
    client, settings, manager = mcp_client
    original = client.post("/api/v1/mcp/servers", json=sample(tmp_path)).json()
    path = "/api/v1/mcp/servers/" + original["connection_id"]
    assert client.put(path, json=sample(tmp_path, name="updated")).status_code == 200
    before, generation = settings.path.read_bytes(), manager._generation
    if operation == "enabled":
        response = client.patch(path + "/enabled", json={"enabled": True, "expected_revision": original["revision"]})
    else:
        response = client.request("DELETE" if operation == "delete" else "POST",
            path + ("/reconnect" if operation == "reconnect" else ""), params={"expected_revision": original["revision"]})
    assert response.status_code == 409
    assert settings.path.read_bytes() == before and manager._generation == generation


def test_saved_but_not_applied_is_returned_and_survives_refresh(mcp_client, tmp_path, monkeypatch):
    client, settings, manager = mcp_client

    async def unavailable(config):
        raise McpConnectionError("合成连接服务暂时不可用")

    original_apply = manager.apply
    monkeypatch.setattr(manager, "apply", unavailable)
    response = client.post("/api/v1/mcp/servers", json=sample(tmp_path))
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["configuration_status"] == "pending" and saved["applied_revision"] is None
    assert "合成连接服务暂时不可用" in saved["apply_error"]
    assert settings.get(saved["connection_id"]).name == "配置验收"
    refreshed = client.get("/api/v1/mcp/servers").json()["servers"][0]
    assert refreshed == saved
    monkeypatch.setattr(manager, "apply", original_apply)
    applied = client.post(f"/api/v1/mcp/servers/{saved['connection_id']}/reconnect",
                          params={"expected_revision": saved["revision"]}).json()
    assert applied["configuration_status"] == "applied" and applied["apply_error"] is None
    assert applied["revision"] == applied["applied_revision"]
    assert len(settings.list_configs()) == 1


def test_external_edit_is_visible_and_cannot_dispatch_old_tools(tmp_path):
    settings, config = config_for(tmp_path)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            generation = manager.catalog()[0].generation
            changed = settings.upsert(McpServerUpdate(name="外部新配置", command=sys.executable,
                args=config.args, cwd=str(tmp_path), expected_revision=config.revision), config.connection_id)
            view = manager.views(settings.list_configs())[0]
            assert view.name == "外部新配置" and view.configuration_status == "pending"
            assert view.applied_revision == config.revision and view.revision == changed.revision
            assert view.tool_count == 0 and manager.catalog() == ()
            with pytest.raises(McpConnectionError, match="配置已更改"):
                await manager.call(config.connection_id, generation, "write.summary", {"text": "must not write"})
            assert not (tmp_path / "summary.md").exists()
            await manager.refresh(config.connection_id)
            await ready(manager)
            generation = manager.catalog()[0].generation
            await manager.call(config.connection_id, generation, "write.summary", {"text": "新配置已实际调用"})
            assert (tmp_path / "summary.md").read_text(encoding="utf-8") == "新配置已实际调用"
            assert manager.views(settings.list_configs())[0].configuration_status == "applied"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["reconnect", "remove"])
def test_connection_retired_while_reading_config_is_never_dispatched(tmp_path, monkeypatch, operation):
    settings, config = config_for(tmp_path)
    reading, release = threading.Event(), threading.Event()
    original_get = settings.get

    def paused_get(connection_id):
        saved = original_get(connection_id)
        reading.set()
        if not release.wait(10):
            raise TimeoutError("test did not release configuration read")
        return saved

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        pending = None
        try:
            await manager.start()
            await ready(manager)
            generation = manager.catalog()[0].generation
            monkeypatch.setattr(settings, "get", paused_get)
            pending = asyncio.create_task(manager.call(config.connection_id, generation,
                "write.summary", {"text": "retired connection must not write"}))
            assert await asyncio.to_thread(reading.wait, 5)
            if operation == "reconnect":
                await manager.apply(config)
                await ready(manager)
            else:
                await manager.remove(config.connection_id)
            release.set()
            with pytest.raises(McpConnectionError, match="工具连接已经更新.*本次未调用工具"):
                await pending
            assert not (tmp_path / "summary.md").exists()
        finally:
            release.set()
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(scenario())


def test_mcp_unknown_fields_and_external_write_during_save_are_preserved(tmp_path, monkeypatch):
    settings = McpSettingsService(tmp_path / "mcp.json")
    original = settings.upsert(McpServerUpdate(**sample(tmp_path)))
    document = json.loads(settings.path.read_bytes())
    document["extension-root"] = ["future"]
    document["connections"][0]["extension-connection"] = {"keep": True}
    settings.path.write_text(json.dumps(document), encoding="utf-8")
    update = McpServerUpdate(**sample(tmp_path, name="rename"))
    settings.upsert(update, original.connection_id)
    saved = json.loads(settings.path.read_bytes())
    assert saved["extension-root"] == ["future"] and saved["connections"][0]["extension-connection"] == {"keep": True}
    external = json.loads(settings.path.read_bytes())
    external["connections"][0]["name"] = "external editor"
    external_bytes = json.dumps(external).encode()
    real_fsync = __import__("os").fsync

    def edit_during_flush(fd):
        real_fsync(fd)
        settings.path.write_bytes(external_bytes)

    monkeypatch.setattr("zhimeng_runtime.mcp_settings.os.fsync", edit_during_flush)
    with pytest.raises(SettingsConflictError):
        settings.upsert(update, original.connection_id)
    assert settings.path.read_bytes() == external_bytes
    assert not list(tmp_path.glob("*.tmp"))
