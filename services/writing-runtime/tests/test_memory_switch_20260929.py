"""记忆开关（9-29，作者：「agent根据我对话直接记忆，可以开关和编辑，但是不能只有编辑」）。

- 开着（默认）：对话开头带用户记忆，模型拿得到 memory_write；每轮答完后台再问一次模型，挑出以后还用得上的事记下（自动记忆）。
- 关着：开头不带用户记忆；未打开项目时目录里没有 memory_write，打开项目时仍可记项目说明。
  模型照着前几轮又调 scope=user，执行器照实说关着、不写。
  正在进行的对话也撤下开头和历史附言中的记忆副本，末尾补一句「用户把记忆关了」。
- 设置页逐条改删时带着读到时的 sha256：这中间 Agent 又记了一条，回 409，不把它盖掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_agent_direct_write_tools import _build_runtime as _build_chat_runtime
from test_agent_direct_write_tools import _call
from test_agent_service import wait_terminal
from test_memory_files import _RecordingProvider, _service_with_tools
from test_opening_context_freeze_20260927 import _AllRequests
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_tools import AgentToolError
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_files import (
    MEMORY_SETTINGS_FILE_NAME,
    append_user_memory,
    memory_enabled,
    read_user_memory,
    set_memory_enabled,
)

MEMORY = "用户记忆（以前的对话里记下的）：\n"
# 这个文件测自动记忆：conftest 默认把它关掉（假模型按顺序出答案，多问一次会吃掉后面的答案）。
AUTO_MEMORY = True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_switch_defaults_to_on_and_a_broken_settings_file_pauses_memory(tmp_path: Path) -> None:
    assert memory_enabled(tmp_path) is True
    set_memory_enabled(tmp_path, False)
    assert memory_enabled(tmp_path) is False
    set_memory_enabled(tmp_path, True)
    assert memory_enabled(tmp_path) is True
    (tmp_path / MEMORY_SETTINGS_FILE_NAME).write_text("{不是 json", encoding="utf-8")
    assert memory_enabled(tmp_path) is False


def test_memory_api_reports_the_switch_and_refuses_to_overwrite_a_newer_memory(client: TestClient) -> None:
    first = client.get("/api/v1/memory/user")
    assert first.status_code == 200
    view = first.json()
    assert view["enabled"] is True
    assert view["content"] == ""
    assert view["sha256"] == _sha("")

    saved = client.put("/api/v1/memory/user", json={"content": "我写仙侠\n", "expected_sha256": view["sha256"]})
    assert saved.status_code == 200
    assert saved.json()["content"] == "我写仙侠\n"
    assert saved.json()["sha256"] == _sha("我写仙侠\n")

    # Agent 在这中间又记了一条：拿旧指纹存就回 409，刚记的那条还在。
    state_root = Path(saved.json()["path"]).parent
    append_user_memory(state_root, "笔名叫青崖")
    stale = client.put("/api/v1/memory/user", json={"content": "", "expected_sha256": _sha("我写仙侠\n")})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "user_memory_changed"
    assert read_user_memory(state_root) == "我写仙侠\n笔名叫青崖\n"

    # 不带指纹照旧整份改写（老界面、脚本直接存）。
    assert client.put("/api/v1/memory/user", json={"content": "只剩这条\n"}).status_code == 200

    off = client.put("/api/v1/memory/user/settings", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert client.get("/api/v1/memory/user").json()["enabled"] is False
    assert memory_enabled(state_root) is False


def test_memory_write_refuses_while_the_switch_is_off(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _cs, _project = _build_chat_runtime(tmp_path)
    set_memory_enabled(tmp_path, False)
    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(_call(executor, run, "memory_write", {"scope": "user", "text": "我写仙侠"}, call_id="call-memory-off-0001"))
    assert excinfo.value.code == "agent_tool_memory_disabled"
    assert "关了" in str(excinfo.value)
    assert read_user_memory(tmp_path) == ""


def test_with_memory_off_new_conversations_carry_no_memory_and_no_memory_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        append_user_memory(tmp_path, "我不喜欢每句另起一行")
        set_memory_enabled(tmp_path, False)
        provider = _RecordingProvider()
        service = _service_with_tools(tmp_path, provider)
        session = await service.create_session(project_id=None, title="随便聊")
        started = await service.start_run(
            session_id=session.session_id,
            message="你好。",
            expected_project_revision=None,
            expected_project_fingerprint=None,
            active_chapter_id=None,
            idempotency_key="memory-off-1",
            writer_mode="author_review_change_proposal",
        )
        run = await wait_terminal(service, started.run.run_id)
        assert run.status is RunStatus.SUCCEEDED
        assert not any("用户记忆" in str(message.get("content", "")) for message in provider.messages)
        assert "memory_write" not in provider.tool_names
        # 别的工具照旧在（关的只是记忆）。
        assert "read_file" in provider.tool_names
        await service.shutdown()

    asyncio.run(scenario())


def test_turning_memory_off_mid_conversation_is_said_at_the_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        append_user_memory(tmp_path, "我不喜欢每句另起一行")
        provider = _AllRequests()
        service = _service_with_tools(tmp_path, provider)
        session = await service.create_session(project_id=None, title="随便聊")

        async def turn(text: str, key: str) -> None:
            started = await service.start_run(
                session_id=session.session_id,
                message=text,
                expected_project_revision=None,
                expected_project_fingerprint=None,
                active_chapter_id=None,
                idempotency_key=key,
                writer_mode="author_review_change_proposal",
            )
            run = await wait_terminal(service, started.run.run_id)
            assert run.status is RunStatus.SUCCEEDED

        await turn("你好。", "memory-mid-1")
        assert provider.requests[-1][1] == {"role": "user", "content": MEMORY + "我不喜欢每句另起一行"}
        assert "memory_write" in provider.tool_names

        append_user_memory(tmp_path, "回答时先给结果")
        await turn("接着聊。", "memory-mid-2")
        assert "回答时先给结果" in str(provider.requests[-1][-1]["content"])

        set_memory_enabled(tmp_path, False)
        await turn("再接着聊。", "memory-mid-3")
        latest = provider.requests[-1]
        # 用户的开关优先：开头与历史附言中的记忆副本都撤下，普通对话仍在。
        assert not any("我不喜欢每句另起一行" in str(message.get("content", "")) for message in latest)
        assert not any("回答时先给结果" in str(message.get("content", "")) for message in latest)
        assert any(message.get("content") == "接着聊。" for message in latest)
        assert "用户在设置里把记忆关了" in str(latest[-1]["content"])
        assert "memory_write" not in provider.tool_names
        await service.shutdown()

    asyncio.run(scenario())


def test_background_calls_use_a_low_reasoning_copy_of_the_provider(tmp_path: Path) -> None:
    """起名、挑记忆这类后台小活不跟着对话的 max 想一分多钟（9-29 真机：挑一次记忆等了 99 秒）。"""

    from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider

    provider = OpenAICompatibleAgentProvider(
        base_url="https://relay.example/v1",
        model="grok-4.6",
        api_key="k" * 8,
        credential_reference="provider-settings:test",
        timeout_seconds=30.0,
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort="max",
    )
    quick = AgentService._quick_provider(provider)
    assert quick is not provider
    assert quick.reasoning_effort == "low"
    assert provider.reasoning_effort == "max"
    # 没有这个办法的提供方（测试替身）照原样用。
    plain = _RecordingProvider()
    assert AgentService._quick_provider(plain) is plain


# ---- 自动记忆：每轮答完另问一次（9-29 真机：光靠说明，grok-4.6 顺口提到的习惯 0/4 场记了） ----


class _MemoryAwareProvider(_RecordingProvider):
    """正常的一轮答「好。」；被问「要记住什么」时答一段固定的 JSON，并记下被问了几次、问了什么。"""

    def __init__(self, reply: str | list[str]) -> None:
        super().__init__()
        self.reply = reply
        self.extract_requests: list[list[dict[str, object]]] = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        if str(messages[0].get("content", "")).startswith("你帮一个 Agent 记住关于用户的事"):
            self.extract_requests.append([dict(message) for message in messages])
            reply = self.reply
            if isinstance(reply, list):
                sources = json.loads(messages[1]["content"])["user_messages"]
                source_id, evidence = next(iter(sources.items()))
                reply = json.dumps({"changes": [{"action": "add", "text": text, "source_message_id": source_id, "evidence": evidence} for text in reply]})
            yield ProviderTextDelta(reply)
            yield ProviderCompleted("stop", None)
            return
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def _auto_memory_service(tmp_path: Path, provider: _RecordingProvider) -> AgentService:
    service = _service_with_tools(tmp_path, provider)
    service._auto_memory = True
    return service


async def _one_turn(service: AgentService, message: str, key: str, *, mode: str = "author_review_change_proposal"):
    session = await service.create_session(project_id=None, title="随便聊")
    started = await service.start_run(
        session_id=session.session_id,
        message=message,
        expected_project_revision=None,
        expected_project_fingerprint=None,
        active_chapter_id=None,
        idempotency_key=key,
        writer_mode=mode,
    )
    run = await wait_terminal(service, started.run.run_id)
    assert run.status is RunStatus.SUCCEEDED
    # 记忆是答完以后才另问的（Run 这时已经是终态）：等这一轮的任务整个跑完再看。
    task = service._tasks.get(run.run_id)
    if task is not None:
        await task
    return run


def test_after_a_turn_lasting_facts_are_remembered_without_being_asked(tmp_path: Path) -> None:
    async def scenario() -> None:
        append_user_memory(tmp_path, "回答别太长")
        provider = _MemoryAwareProvider(["白天上班，晚上十点以后才有空写", "写的是仙侠", "回答别太长"])
        service = _auto_memory_service(tmp_path, provider)
        run = await _one_turn(service, "我白天上班，晚上十点以后才写，写的是仙侠。今晚写哪段好？", "auto-memory-1")
        assert len(provider.extract_requests) == 1
        asked = str(provider.extract_requests[0][1]["content"])
        assert "回答别太长" in asked and "今晚写哪段好" in asked and "好。" not in asked
        assert read_user_memory(tmp_path) == "回答别太长\n白天上班，晚上十点以后才有空写\n写的是仙侠\n"
        with service.store.database.connection() as connection:
            rows = connection.execute(
                "SELECT event_type, payload_json FROM run_event WHERE run_id = ? AND event_type LIKE 'memory_auto_%'",
                (run.run_id,),
            ).fetchall()
        assert len(rows) == 1 and rows[0][0] == "memory_auto_saved"
        assert [change["text"] for change in json.loads(rows[0][1])["changes"]] == ["白天上班，晚上十点以后才有空写", "写的是仙侠"]
        await service.shutdown()

    asyncio.run(scenario())


def test_auto_memory_stays_quiet_when_off_read_only_or_the_message_is_trivial(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _MemoryAwareProvider(["写的是仙侠"])
        service = _auto_memory_service(tmp_path, provider)
        await _one_turn(service, "继续", "auto-memory-short")
        await _one_turn(service, "我写的是仙侠，帮我看看这段。", "auto-memory-readonly", mode="repair_only")
        set_memory_enabled(tmp_path, False)
        await _one_turn(service, "我写的是仙侠，帮我看看这段。", "auto-memory-off")
        assert provider.extract_requests == []
        assert read_user_memory(tmp_path) == ""

        # 模型不按格式回（回了一段话）：宁可不记。
        set_memory_enabled(tmp_path, True)
        provider.reply = "用户写仙侠，喜欢晚上写。"
        await _one_turn(service, "我写的是仙侠，帮我看看这段。", "auto-memory-garbled")
        assert len(provider.extract_requests) == 1
        assert read_user_memory(tmp_path) == ""
        await service.shutdown()

    asyncio.run(scenario())


def test_editor_save_and_auto_append_share_one_memory_transaction(client: TestClient, monkeypatch) -> None:
    """设置页保存已开始时，后台追加等它存完再读，两个改动都留下。"""
    import concurrent.futures
    import threading

    from zhimeng_runtime import memory_files

    first = client.put("/api/v1/memory/user", json={"content": "原来的偏好\n"}).json()
    state_root = Path(first["path"]).parent
    started = threading.Event()
    release = threading.Event()
    appending = threading.Event()
    original = memory_files._write_atomic

    def paused_write(path: Path, encoded: bytes) -> None:
        if encoded == "手动改过的偏好\n".encode("utf-8"):
            started.set()
            assert release.wait(5)
        original(path, encoded)

    def append() -> None:
        appending.set()
        append_user_memory(state_root, "后台记下的新偏好")

    monkeypatch.setattr(memory_files, "_write_atomic", paused_write)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        edited = workers.submit(
            client.put,
            "/api/v1/memory/user",
            json={"content": "手动改过的偏好\n", "expected_sha256": first["sha256"]},
        )
        try:
            assert started.wait(5)
            appended = workers.submit(append)
            assert appending.wait(5)
            with pytest.raises(concurrent.futures.TimeoutError):
                appended.result(timeout=0.2)
        finally:
            release.set()
        assert edited.result(timeout=5).status_code == 200
        appended.result(timeout=5)
    assert read_user_memory(state_root) == "手动改过的偏好\n后台记下的新偏好\n"


def test_a_pending_auto_memory_result_respects_a_manual_edit(tmp_path: Path) -> None:
    """模型正在挑记忆时，用户改删过的内容以用户最新保存为准。"""
    from zhimeng_runtime.memory_files import write_user_memory

    class EditedWhileExtracting(_MemoryAwareProvider):
        async def stream_chat(self, messages, *, cancel_event, tools=()):
            if str(messages[0].get("content", "")).startswith("你帮一个 Agent 记住关于用户的事"):
                write_user_memory(tmp_path, "用户手动保留的偏好\n")
            async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
                yield event

    async def scenario() -> None:
        append_user_memory(tmp_path, "旧的偏好")
        provider = EditedWhileExtracting(["已经被用户删掉的偏好"])
        service = _auto_memory_service(tmp_path, provider)
        run = await _one_turn(service, "之前的长期偏好需要纠正，按我现在的要求来。", "auto-memory-edited")
        assert len(provider.extract_requests) == 1
        assert read_user_memory(tmp_path) == "用户手动保留的偏好\n"
        with service.store.database.connection() as connection:
            skipped = connection.execute(
                "SELECT payload_json FROM run_event WHERE run_id = ? AND event_type = 'memory_auto_skipped'",
                (run.run_id,),
            ).fetchone()
        assert skipped is not None
        assert json.loads(skipped[0])["reason"] == "memory_changed"
        await service.shutdown()

    asyncio.run(scenario())
