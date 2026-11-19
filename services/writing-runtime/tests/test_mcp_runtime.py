from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from zhimeng_runtime.mcp_client_service import McpClientService, _SecretStream, login_environment
from zhimeng_runtime.mcp_models import McpConnectionError, McpServerUpdate
from zhimeng_runtime.mcp_settings import McpSettingsService


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_stdio_fixture.py"


def config_for(tmp_path, mode="legacy", *, env=None, command=None, args=None):
    (tmp_path / "sample.csv").write_text("department,amount\n设计,10\n设计,20\n工程,50\n", encoding="utf-8")
    settings = McpSettingsService(tmp_path / "mcp.json")
    config = settings.upsert(McpServerUpdate(name="桌面 合成服务", command=command or sys.executable,
        args=args or [str(FIXTURE.resolve()), mode], cwd=str(tmp_path), env=env or {}))
    return settings, config


async def until(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def ready(manager, *, timeout=12):
    await until(lambda: manager.views()[0].state in {"ready", "error"}, timeout=timeout)
    assert manager.views()[0].state == "ready", manager.views()[0].error


def events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            api.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_dpapi_atomic_settings_keep_replace_remove_and_never_plaintext(tmp_path, monkeypatch):
    settings, config = config_for(tmp_path, env={"ODD_CREDENTIAL": "synthetic value 987654321"})
    assert b"synthetic value" not in settings.path.read_bytes()
    assert settings.get(config.connection_id).env == config.env
    update = McpServerUpdate(name="重命名", command=sys.executable, cwd=str(tmp_path), env={"ODD_CREDENTIAL": ""})
    kept = settings.upsert(update, config.connection_id)
    assert kept.env == config.env
    replacement = update.model_copy(update={"env": {"NEW_NAME": "new value"}, "remove_env": ["ODD_CREDENTIAL"]})
    replaced = settings.upsert(replacement, config.connection_id)
    assert replaced.env == {"NEW_NAME": "new value"}
    old = settings.path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk write failed")))
    with pytest.raises(OSError):
        settings.upsert(update, config.connection_id)
    assert settings.path.read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_secret_stream_chunk_boundaries_and_multiline():
    secret = "unusually\nquoted\"secret"
    masker = _SecretStream([secret, "abcabcab"])
    text = "plain " + secret + " end abcabcab end"
    output = "".join(masker.feed(char) for char in text) + masker.feed("", final=True)
    assert secret not in output and "abcabcab" not in output
    assert output == "plain [已遮住] end [已遮住] end"


def test_login_environment_does_not_inherit_ai_host(monkeypatch):
    monkeypatch.setenv("AI_HOST_TEST_SECRET", "host-only-fake")
    assert "AI_HOST_TEST_SECRET" not in login_environment()


@pytest.mark.parametrize("mode,version", [("legacy", "2025-11-25"), ("modern", "2026-07-28")])
def test_real_stdio_pages_file_task_error_secrets_and_shutdown(tmp_path, monkeypatch, mode, version):
    monkeypatch.setenv("AI_HOST_TEST_SECRET", "host-only-fake")
    secret = "synthetic credential quoted\" 123456789"
    settings, config = config_for(tmp_path, mode, env={"ODD_CREDENTIAL": secret})

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            assert len(manager.catalog()) == 7
            assert manager.views()[0].protocol_version == version
            tools = {tool.raw_name: tool for tool in manager.catalog()}
            assert tools["read.csv"].read_only and not tools["write.summary"].read_only
            assert tools["read.csv"].input_schema["properties"]["text"]["type"] == ["string", "null"]
            gen = tools["read.csv"].generation
            rows = await manager.call(config.connection_id, gen, "read.csv", {})
            assert "设计" in rows["content"][0]["text"]
            text = "# 部门汇总\n设计：30\n工程：50\n"
            await manager.call(config.connection_id, gen, "write.summary", {"text": text})
            assert (tmp_path / "summary.md").read_text(encoding="utf-8") == text
            failed = await manager.call(config.connection_id, gen, "error", {})
            assert failed["isError"] is True
            assert len(failed["content"]) == 2
            echo = await manager.call(config.connection_id, gen, "echo.secret", {})
            assert secret not in json.dumps(echo, ensure_ascii=False)
            assert echo["structuredContent"]["value"] == "[已遮住]"
            assert manager.views()[0].env_names == ["ODD_CREDENTIAL"]
            assert "env" not in manager.views()[0].model_dump()
            await until(lambda: (tmp_path / "mcp-logs" / f"{config.connection_id}.log").exists())
            assert secret not in (tmp_path / "mcp-logs" / f"{config.connection_id}.log").read_text(encoding="utf-8")
        finally:
            await manager.shutdown()
        first = events(tmp_path)[0]
        assert first["host_secret_present"] is False
        await until(lambda: not alive(first["pid"]) and not alive(first["child_pid"]))
    asyncio.run(scenario())


def test_cancelling_one_call_preserves_other_call_and_connection(tmp_path):
    settings, config = config_for(tmp_path)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            gen = manager.catalog()[0].generation
            first = asyncio.create_task(manager.call(config.connection_id, gen, "wait", {"gate": "gate-a"}))
            second = asyncio.create_task(manager.call(config.connection_id, gen, "wait", {"gate": "gate-b"}))
            await until(lambda: len([event for event in events(tmp_path) if event.get("call") == "wait"]) == 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await until(lambda: any("cancelled" in event for event in events(tmp_path)))
            (tmp_path / "gate-b").touch()
            assert (await second)["isError"] is False
            assert manager.views()[0].state == "ready"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["handshake-fail", "list-fail", "cursor-repeat"])
def test_failed_connection_has_true_error_and_no_orphan(tmp_path, mode):
    settings, config = config_for(tmp_path, mode, env={"ODD_CREDENTIAL": "secret fixture 12345"})

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await until(lambda: manager.views()[0].state == "error")
            assert manager.catalog() == ()
            assert "secret fixture 12345" not in manager.views()[0].error
        finally:
            await manager.shutdown()
        first = events(tmp_path)[0]
        await until(lambda: not alive(first["pid"]) and not alive(first["child_pid"]))
    asyncio.run(scenario())


def test_disconnect_write_is_not_retried_and_catalog_removed(tmp_path):
    settings, config = config_for(tmp_path)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            gen = manager.catalog()[0].generation
            with pytest.raises(McpConnectionError):
                await manager.call(config.connection_id, gen, "disconnect.write", {})
            await until(lambda: manager.views()[0].state == "error")
            assert manager.catalog() == ()
            assert (tmp_path / "side-effects.txt").read_text() == "once\n"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_notifications_and_disable_immediately_invalidate_old_generation(tmp_path):
    settings, config = config_for(tmp_path)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            generation = manager.catalog()[0].generation
            await manager.call(config.connection_id, generation, "change.catalog", {})
            await until(lambda: len(manager.catalog()) == 9)
            with pytest.raises(McpConnectionError, match="目录已经更新"):
                await manager.call(config.connection_id, generation, "error", {})
            await manager.apply(config.model_copy(update={"enabled": False}))
            assert manager.catalog() == ()
            assert manager.views()[0].state == "disabled"
            with pytest.raises(McpConnectionError):
                await manager.call(config.connection_id, generation, "error", {})
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("relative", [False, True])
def test_cmd_with_spaces_chinese_path_and_arguments(tmp_path, relative):
    folder = tmp_path / "中文 工具服务"
    folder.mkdir()
    launcher = folder / "启动 服务.cmd"
    launcher.write_bytes((f'@echo off\r\n"{sys.executable}" "{FIXTURE.resolve()}" legacy %*\r\n').encode("gbk"))
    settings, config = config_for(folder, command="./启动 服务.cmd" if relative else str(launcher), args=["hello world", "中文 参数"])

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            assert events(folder)[0]["args"] == ["hello world", "中文 参数"]
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_large_argument_and_refresh_remove_recover_without_old_dispatch(tmp_path):
    settings, config = config_for(tmp_path)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await ready(manager)
            generation = manager.catalog()[0].generation
            text = "large frame 中文: " + "abcdef0123456789" * 65536
            await manager.call(config.connection_id, generation, "write.summary", {"text": text})
            assert (tmp_path / "summary.md").read_text(encoding="utf-8") == text
            await manager.refresh(config.connection_id)
            assert manager.catalog() == ()
            with pytest.raises(McpConnectionError):
                await manager.call(config.connection_id, generation, "error", {})
            await ready(manager)
            assert manager.catalog()[0].generation != generation
            await manager.remove(config.connection_id)
            assert manager.views() == [] and manager.catalog() == ()
            with pytest.raises(McpConnectionError):
                await manager.call(config.connection_id, generation, "write.summary", {"text": "unexpected"})
            assert (tmp_path / "summary.md").read_text(encoding="utf-8") == text
            # Re-add after remove while its previous process is still winding down.
            await manager.apply(config)
            await manager.apply(config)
            await ready(manager)
        finally:
            await manager.shutdown()
        for event in events(tmp_path):
            if "child_pid" in event:
                await until(lambda: not alive(event["pid"]) and not alive(event["child_pid"]))
    asyncio.run(scenario())


def test_two_connection_directories_and_envs_stay_separate(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    settings, first = config_for(first_dir, env={"ODD_CREDENTIAL": "secret for first service"})
    config_for(second_dir)
    second = settings.upsert(McpServerUpdate(name="同名也不混", command=sys.executable,
        args=[str(FIXTURE.resolve()), "legacy"], cwd=str(second_dir), env={"ODD_CREDENTIAL": "secret for second service"}))

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            await until(lambda: all(view.state in {"ready", "error"} for view in manager.views()))
            assert all(view.state == "ready" for view in manager.views())
            for config, content in [(first, "first content"), (second, "second content")]:
                generation = next(tool.generation for tool in manager.catalog() if tool.connection_id == config.connection_id)
                await manager.call(config.connection_id, generation, "write.summary", {"text": content})
                assert (Path(config.cwd) / "summary.md").read_text(encoding="utf-8") == content
            await manager.remove(first.connection_id)
            assert all(tool.connection_id == second.connection_id for tool in manager.catalog())
            assert manager.redact(first.connection_id, "secret for first service") == "[已遮住]"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_ordinary_config_does_not_corrupt_schemas_or_tool_results(tmp_path):
    from zhimeng_runtime.mcp_models import McpDiscoveredTool
    from zhimeng_runtime.mcp_tool_adapter import McpToolAdapter

    settings, config = config_for(tmp_path, env={"PYTHONUTF8": "1", "NODE_ENV": "production", "API_KEY": "synthetic-private-key"})
    config = config.model_copy(update={"enabled": False, "headers": {"Accept": "application/json", "Authorization": "Bearer synthetic-auth-token"}})

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        await manager.apply(config)
        payload = {"version1": "120", "date": "2026-10-13", "mime": "application/json", "mode": "production"}
        assert manager._redact_value(payload) == payload
        assert manager.redact(None, "synthetic-private-key synthetic-auth-token") == "[已遮住] [已遮住]"
        schema = {"type": "object", "properties": {"version1": {"type": "string", "enum": ["production"]}}}
        connection = manager._connections[config.connection_id]
        connection.state = "ready"
        connection.tools = (McpDiscoveredTool(connection_id=config.connection_id, generation=connection.generation,
            server_name="fixture", raw_name="sample", title=None, description="production 120",
            input_schema=schema, read_only=True, annotations={}),)
        definition = McpToolAdapter(manager).bind(read_only=False)[0]
        assert definition.parameters == schema
        await manager.shutdown()

    asyncio.run(scenario())


def test_settings_env_names_casefold_on_windows(tmp_path):
    settings, config = config_for(tmp_path, env={"Mixed_Name": "original-secret-value"})
    update = McpServerUpdate(name=config.name, command=sys.executable, cwd=str(tmp_path), env={"MIXED_NAME": "replacement-secret"})
    config = settings.upsert(update, config.connection_id)
    assert config.env == {"MIXED_NAME": "replacement-secret"}
    config = settings.upsert(update.model_copy(update={"env": {}, "remove_env": ["mixed_name"]}), config.connection_id)
    assert config.env == {}


def test_invalid_config_does_not_block_start_but_is_visible(tmp_path):
    settings = McpSettingsService(tmp_path / "mcp.json")
    settings.path.write_text("invalid JSON", encoding="utf-8")

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            assert manager.settings_error
            with pytest.raises(McpConnectionError):
                manager.views()
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["legacy-discovery-disconnect", "legacy-discovery-silent"])
def test_legacy_bridge_dying_on_discover_gets_fresh_handshake_only(tmp_path, mode):
    settings, config = config_for(tmp_path, mode)

    async def scenario():
        manager = McpClientService(settings, tmp_path)
        try:
            await manager.start()
            # A silent peer consumes the SDK's 10-second discovery timeout,
            # then transport teardown and a fresh process/handshake. The usual
            # 12-second helper deadline races that valid recovery on Windows.
            await ready(manager, timeout=30 if mode == "legacy-discovery-silent" else 12)
            assert manager.views()[0].protocol_version == "2025-11-25"
            log = events(tmp_path)
            assert [event["handshake"] for event in log if "handshake" in event] == ["server/discover", "initialize"]
            starts = [event for event in log if "child_pid" in event]
            assert len(starts) == 2 and starts[0]["pid"] != starts[1]["pid"]
            assert not alive(starts[0]["pid"]) and not alive(starts[0]["child_pid"])
            generation = manager.catalog()[0].generation
            await manager.call(config.connection_id, generation, "write.summary", {"text": "executed once"})
            assert len([event for event in events(tmp_path) if event.get("call") == "write.summary"]) == 1
        finally:
            await manager.shutdown()
        for event in events(tmp_path):
            if "child_pid" in event:
                await until(lambda: not alive(event["pid"]) and not alive(event["child_pid"]))
    asyncio.run(scenario())
