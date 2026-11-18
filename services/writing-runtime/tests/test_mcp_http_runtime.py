from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fixtures.mcp_http_fixture import HttpFixture
from test_mcp_runtime import until, ready
from zhimeng_runtime.mcp_client_service import McpClientService
from zhimeng_runtime.mcp_models import McpConnectionError, McpServerUpdate
from zhimeng_runtime.mcp_settings import McpSettingsService


def setup_manager(tmp_path, fixture, **kwargs):
    settings = McpSettingsService(tmp_path / "settings.json")
    config = settings.upsert(McpServerUpdate(name="HTTP 合成服务", transport="http", url=fixture.url, **kwargs))
    return McpClientService(settings, tmp_path), config


@pytest.mark.parametrize("era", ["legacy", "modern"])
@pytest.mark.parametrize("style", ["json", "sse"])
def test_http_real_file_roundtrip_both_eras_and_response_formats(tmp_path, era, style):
    secret = "synthetic header credential 87654321"
    with HttpFixture(tmp_path / "peer", era, style, expected_secret=secret) as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer, headers={"X-Workspace-Secret": secret}, env={"STDIO_ONLY": "stdio scoped value"})
            try:
                await manager.start()
                await ready(manager)
                assert len(manager.catalog()) == 6
                assert manager.views()[0].header_names == ["X-Workspace-Secret"]
                assert manager.views()[0].transport == "http"
                assert secret not in manager.views()[0].model_dump_json()
                assert secret not in manager.settings.path.read_text(encoding="utf-8")
                generation = manager.catalog()[0].generation
                rows = await manager.call(config.connection_id, generation, "read.csv", {})
                assert "设计" in rows["content"][0]["text"]
                text = "# 汇总\n设计：30\n工程：50\n"
                await manager.call(config.connection_id, generation, "write.summary", {"text": text})
                assert (peer.root / "summary.md").read_text(encoding="utf-8") == text
                assert (await manager.call(config.connection_id, generation, "error", {}))["isError"] is True
                echo = await manager.call(config.connection_id, generation, "echo.headers", {})
                assert echo["structuredContent"]["value"] == "[已遮住]"
                assert len(echo["content"]) == 2
                assert all(record["secret_received"] for record in peer.records if "http_method" in record)
                calls = [record for record in peer.records if record.get("method") == "tools/call"]
                if era == "modern":
                    assert all(record["meta"].get("io.modelcontextprotocol/protocolVersion") == "2026-07-28" for record in calls)
                else:
                    assert any(record.get("session") == "fixture-1" for record in peer.records)
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_http_cancel_only_one_request_uses_era_semantics(tmp_path, era):
    with HttpFixture(tmp_path / "peer", era) as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer)
            try:
                await manager.start()
                await ready(manager)
                generation = manager.catalog()[0].generation
                first = asyncio.create_task(manager.call(config.connection_id, generation, "wait", {"gate": "a"}))
                second = asyncio.create_task(manager.call(config.connection_id, generation, "wait", {"gate": "b"}))
                await until(lambda: len([record for record in peer.records if record.get("call") == "wait"]) == 2)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                first_id = next(record["id"] for record in peer.records if record.get("call") == "wait" and record["arguments"]["gate"] == "a")
                await until(lambda: any(record.get("stream_closed") == first_id for record in peer.records))
                assert (first_id in peer.cancelled) is (era == "legacy")
                peer.released.add("b")
                assert (await second)["isError"] is False
                assert manager.views()[0].state == "ready"
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


def test_http_credentials_and_session_expiry_show_true_state(tmp_path):
    with HttpFixture(tmp_path / "peer", unauthorized=True) as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer)
            try:
                await manager.start()
                await until(lambda: manager.views()[0].state == "error")
                assert "401" in manager.views()[0].error
                peer.unauthorized = False
                await manager.refresh(config.connection_id)
                await ready(manager)
                generation = manager.catalog()[0].generation
                peer.expired = True
                with pytest.raises(McpConnectionError):
                    await manager.call(config.connection_id, generation, "read.csv", {})
                await until(lambda: manager.views()[0].state == "error")
                assert manager.catalog() == () and "404" in manager.views()[0].error
                await manager.refresh(config.connection_id)
                await ready(manager)
                assert peer.session_number == 2
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_http_disconnect_after_write_never_reposts(tmp_path, era):
    with HttpFixture(tmp_path / "peer", era) as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer)
            try:
                await manager.start()
                await ready(manager)
                with pytest.raises(McpConnectionError, match="可能已经执行"):
                    await manager.call(config.connection_id, manager.catalog()[0].generation, "disconnect.write", {})
                assert (peer.root / "side-effects.txt").read_text() == "once\n"
                assert len([record for record in peer.records if record.get("call") == "disconnect.write"]) == 1
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


def test_http_cross_origin_redirect_keeps_all_custom_credentials_at_origin(tmp_path):
    secret = "redirect-test-synthetic-secret"
    with HttpFixture(tmp_path / "sink") as sink, HttpFixture(tmp_path / "origin", redirect_target=sink.url) as origin:
        async def scenario():
            manager, config = setup_manager(tmp_path, origin, headers={"X-Workspace-Secret": secret})
            config.url = origin.url.replace("/mcp", "/redirect-other")
            try:
                await manager.apply(config)
                await until(lambda: manager.views()[0].state == "error")
                assert not sink.records
                assert all(record.get("has_custom_header") for record in origin.records)
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


def test_http_same_origin_redirect_and_header_update_remove(tmp_path):
    with HttpFixture(tmp_path / "peer", "modern") as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer, headers={"X-Mixed-Case": "original private value"})
            update = McpServerUpdate(name=config.name, transport="http", url=peer.url.replace("/mcp", "/redirect-same"),
                headers={"x-mixed-case": ""})
            config = manager.settings.upsert(update, config.connection_id)
            assert config.headers == {"X-Mixed-Case": "original private value"}
            config = manager.settings.upsert(update.model_copy(update={"headers": {"x-mixed-case": "replacement private value"}}), config.connection_id)
            assert config.headers == {"x-mixed-case": "replacement private value"}
            config = manager.settings.upsert(update.model_copy(update={"remove_headers": ["X-MIXED-CASE"]}), config.connection_id)
            assert config.headers == {}
            try:
                await manager.start()
                await ready(manager)
                assert len(manager.catalog()) == 6
            finally:
                await manager.shutdown()
        asyncio.run(scenario())


def test_http_shutdown_closes_owned_session_and_inflight_call(tmp_path):
    with HttpFixture(tmp_path / "peer", "legacy") as peer:
        async def scenario():
            manager, config = setup_manager(tmp_path, peer)
            await manager.start()
            await ready(manager)
            call = asyncio.create_task(manager.call(config.connection_id, manager.catalog()[0].generation, "wait", {}))
            await until(lambda: any(record.get("call") == "wait" for record in peer.records))
            await manager.shutdown()
            with pytest.raises(McpConnectionError):
                await call
            assert any(record.get("delete_session") == "fixture-1" for record in peer.records)
        asyncio.run(scenario())


def test_http_header_storage_failure_retains_previous_atomic_config(tmp_path, monkeypatch):
    import os
    settings = McpSettingsService(tmp_path / "settings.json")
    original = McpServerUpdate(name="HTTP", transport="http", url="https://example.com/mcp", headers={"Authorization": "Bearer original-synthetic-key"})
    saved = settings.upsert(original)
    before = settings.path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        settings.upsert(original.model_copy(update={"headers": {"Authorization": "Bearer replacement-synthetic-key"}}), saved.connection_id)
    assert settings.path.read_bytes() == before
    assert settings.get(saved.connection_id).headers == original.headers
    assert not list(tmp_path.glob("*.tmp"))


def test_inactive_http_draft_does_not_block_saving_stdio(tmp_path):
    settings = McpSettingsService(tmp_path / "settings.json")
    saved = settings.upsert(McpServerUpdate(name="切回本地", command="synthetic-command", enabled=False,
        cwd=str(tmp_path), url="https://未填完:", headers={"Authorization": "Bearer retained-fake-value"}))
    assert saved.transport == "stdio"
    assert settings.get(saved.connection_id).headers == {"Authorization": "Bearer retained-fake-value"}
