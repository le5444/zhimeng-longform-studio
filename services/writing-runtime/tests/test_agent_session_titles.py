"""对话的 AI 起名：对话收尾后请模型把主题概括成短名字。

Codex 的做法是拿第一句话当标题、改名靠手动；这里比它多走一步，
让模型把「作者想干什么」浓缩成名字。守卫只有一条：系统起的名字
（占位标题或从第一句话提炼出来的标题）可以被换掉，作者自己改过的名字绝不动。
老对话（第一轮没起成名的）下次说话收尾时照样补上名字。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import _SESSION_TITLE_PROMPT
from zhimeng_runtime.agent_store import AgentStore, _derive_session_title
from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)

PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "c" * 64


class NamingProvider:
    """主回答固定说「好的」；认出起名请求（系统提示词）就回一个名字。"""

    def __init__(self, name: str = "收紧开头的改法") -> None:
        self.name = name
        self.naming_requests = 0
        self.answer_requests = 0

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "d" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.naming-provider",
            kind=ExecutorKind.EMBEDDED,
            implementation="naming-provider",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        is_naming = any(
            message.get("role") == "system" and message.get("content") == _SESSION_TITLE_PROMPT
            for message in messages
        )
        if is_naming:
            self.naming_requests += 1
            text = self.name
        else:
            self.answer_requests += 1
            text = "好的，这就帮你处理。"
        for event in (ProviderTextDelta(text), ProviderCompleted("stop", None)):
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event


def wait_for_success(client: TestClient, run_id: str) -> dict[str, object]:
    for _ in range(100):
        payload = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("API Run did not complete")


def wait_for_title(client: TestClient, session_id: str, title: str) -> dict[str, object]:
    """起名是回答落库之后才发生的收尾动作，轮询等它，而不是赌时序。"""
    for _ in range(100):
        session = client.get(f"/api/v1/agent/sessions/{session_id}").json()
        if session["title"] == title:
            return session
        time.sleep(0.02)
    raise AssertionError(f"title did not become {title!r}: {session['title']!r}")


def send_message(client: TestClient, session_id: str, message: str, key: str) -> None:
    started = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"message": message},
    )
    assert started.status_code == 202, started.text
    run = wait_for_success(client, str(started.json()["run"]["run_id"]))
    assert run["status"] == "succeeded", run["error"]


def test_first_run_gets_a_model_written_title(settings, tmp_path) -> None:
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        created = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"})
        assert created.status_code == 201, created.text
        session_id = str(created.json()["session_id"])

        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-1")

        session = wait_for_title(client, session_id, "收紧开头的改法")
        assert session["title"] == "收紧开头的改法"
        # 起名请求带的是起名专用提示词，主回答不受影响。
        assert provider.answer_requests == 1
        assert provider.naming_requests == 1


def test_later_runs_do_not_rename_again(settings, tmp_path) -> None:
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()["session_id"])
        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-2a")
        wait_for_title(client, session_id, "收紧开头的改法")

        provider.name = "另一个名字"
        send_message(client, session_id, "再聊聊人物", "naming-turn-2b")
        time.sleep(0.1)

        session = client.get(f"/api/v1/agent/sessions/{session_id}").json()
        assert session["title"] == "收紧开头的改法"
        # 第二轮只回答，不再起名。
        assert provider.answer_requests == 2
        assert provider.naming_requests == 1


def test_older_conversation_gets_named_on_its_next_run(settings, tmp_path) -> None:
    """老对话（第一轮没起成名，标题还是系统提炼的）下次说话收尾时照样起名。"""
    provider = NamingProvider(name="")  # 第一轮模型回了空名字，起名被跳过
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()["session_id"])
        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-lazy-1")
        # 起名在回答完以后才发；以前固定等 0.1 秒，机器忙的时候（9-28 整套 18 分钟那次）还没发出去就红了。
        deadline = time.monotonic() + 5
        while provider.naming_requests < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
        session = client.get(f"/api/v1/agent/sessions/{session_id}").json()
        assert session["title"] == _derive_session_title("帮我把第二章的开头收紧一点")
        assert provider.naming_requests == 1

        provider.name = "第二章开头改紧凑"
        send_message(client, session_id, "再改改结尾", "naming-turn-lazy-2")

        session = wait_for_title(client, session_id, "第二章开头改紧凑")
        assert session["title"] == "第二章开头改紧凑"
        assert provider.answer_requests == 2
        assert provider.naming_requests == 2


def test_older_placeholder_conversation_gets_named_too(settings, tmp_path) -> None:
    """第一轮中途崩掉、标题还停在占位的老对话，下次说话也会补上名字。"""
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()["session_id"])
        # 模拟老库里的存量：标题直接停在占位，第一轮没有提炼也没有起名。
        store = app.state.agent_service.store
        store.update_session(session_id, title="新对话 · 9/11 14:24")
        send_message(client, session_id, "随便聊聊这本书的基调", "naming-turn-lazy-3")

        session = wait_for_title(client, session_id, "收紧开头的改法")
        assert session["title"] == "收紧开头的改法"


def test_old_default_writing_title_is_a_placeholder_too(settings, tmp_path) -> None:
    """9-27 以前在书里直接打字开的对话都叫「写作对话」（前端写死）、一直没被改名：下次说话收尾时补上名字。"""
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "写作对话"}).json()["session_id"])
        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-writing-default")

        session = wait_for_title(client, session_id, "收紧开头的改法")
        assert session["title"] == "收紧开头的改法"


def test_author_renamed_title_is_never_overwritten(settings, tmp_path) -> None:
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()["session_id"])
        renamed = client.patch(
            f"/api/v1/agent/sessions/{session_id}",
            json={"title": "我和编辑的约定"},
        )
        assert renamed.status_code == 200, renamed.text

        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-3")
        time.sleep(0.1)

        session = client.get(f"/api/v1/agent/sessions/{session_id}").json()
        assert session["title"] == "我和编辑的约定"


def test_session_created_with_a_real_title_is_not_named_again(settings, tmp_path) -> None:
    """建会话时就起了正经名字的，第一轮跑完连起名这一问都不打（省钱）。"""
    provider = NamingProvider()
    app = create_app(settings, agent_provider=provider)
    with TestClient(app) as client:
        session_id = str(client.post("/api/v1/agent/sessions", json={"title": "结构梳理"}).json()["session_id"])
        send_message(client, session_id, "帮我把第二章的开头收紧一点", "naming-turn-4")
        time.sleep(0.1)

        session = client.get(f"/api/v1/agent/sessions/{session_id}").json()
        assert session["title"] == "结构梳理"
        assert provider.answer_requests == 1
        assert provider.naming_requests == 0


# ---------------------------------------------------------------------------
#  store 层的守卫：只有系统起的名字才能被换


def _store(tmp_path: Path) -> AgentStore:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    return AgentStore(database)


def _executor() -> ExecutorIdentity:
    return ExecutorIdentity(
        executor_id="executor.test",
        kind=ExecutorKind.EMBEDDED,
        implementation="test",
        version="1",
        protocol_version="1",
        instance_id="instance-test",
    )


def _capabilities() -> CapabilityProfile:
    return CapabilityProfile(protocol_version="1", names=[CapabilityName.STREAMING], extensions={})


def test_auto_title_replaces_placeholder(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=_executor(),
        capabilities=_capabilities(),
    )
    assert store.maybe_update_auto_session_title(
        session.session_id, first_objective="随便问了一句", new_title="  收紧开头的改法  "
    ) is True
    assert store.get_session(session.session_id).title == "收紧开头的改法"


def test_auto_title_replaces_first_run_derived_title(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=_executor(),
        capabilities=_capabilities(),
    )
    objective = "收紧开头，但不要改动关键线索。"
    store.create_run(
        session_id=session.session_id,
        message=objective,
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key="turn-derive-0001",
    )
    # 第一轮建 Run 时标题已被提炼成第一句话。
    assert store.get_session(session.session_id).title == _derive_session_title(objective)
    assert store.maybe_update_auto_session_title(
        session.session_id, first_objective=objective, new_title="收紧开头的改法"
    ) is True


def test_auto_title_never_touches_author_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=_executor(),
        capabilities=_capabilities(),
    )
    store.update_session(session.session_id, title="我和编辑的约定")
    assert store.maybe_update_auto_session_title(
        session.session_id, first_objective="随便问了一句", new_title="收紧开头的改法"
    ) is False
    assert store.get_session(session.session_id).title == "我和编辑的约定"
    assert store.maybe_update_auto_session_title(
        session.session_id, first_objective="随便问了一句", new_title="   "
    ) is False
