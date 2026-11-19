from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


@pytest.fixture
def settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=("http://127.0.0.1:1420",),
        sse_heartbeat_seconds=0.05,
    )


@pytest.fixture
def app(settings: RuntimeSettings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolated_user_skill_roots(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """技能菜单会进 Agent 提示词：测试里默认给一个空技能根、不挂 ~/.codex/skills，
    免得机器上真实的技能目录改变提示词内容与消息条数；要测技能就在用例里自己 setenv。"""
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(tmp_path_factory.mktemp("user-skills")))
    monkeypatch.setenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", "")


@pytest.fixture(autouse=True)
def _no_turn_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """一步还没出字就临时失败时，重来之前真机要歇 3 秒 / 10 秒；测试里不歇，要测歇不歇就在用例里自己改回来。"""
    from zhimeng_runtime import agent_service

    monkeypatch.setattr(agent_service, "TURN_STREAM_RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture(autouse=True)
def _no_auto_memory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """每轮答完 Agent 会再问一次模型、挑出以后用得上的事记下来（自动记忆，9-29）。测试里的假模型按顺序出答案，
    多问的那一次会吃掉后面几步的答案——默认不挑；要测自动记忆的测试文件里写一行 AUTO_MEMORY = True。"""
    if getattr(request.module, "AUTO_MEMORY", False):
        return
    from zhimeng_runtime.agent_service import AgentService

    async def _skip(self, run_id, run, assistant_content) -> None:
        return None

    monkeypatch.setattr(AgentService, "_maybe_remember", _skip)


@pytest.fixture(autouse=True)
def _isolated_provider_resilience() -> None:
    """熔断器与并发限流是进程内模块级注册表，按 provider 地址/ID 共享——
    测试之间必须复位，否则一个用例烧出来的熔断状态会污染下一个用例。"""
    from zhimeng_runtime.agent_provider import _reset_resilience_registries

    _reset_resilience_registries()
