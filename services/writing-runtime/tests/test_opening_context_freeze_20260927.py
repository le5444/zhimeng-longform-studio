"""用户记忆和文件夹说明文件在对话开头冻结，改过的在末尾追加（9-27，照 Kimi Code 档案 #2）。

以前每轮重读、放在开头第 2、3 条：模型写一次记忆（或作者改了 AGENTS.md），下一轮开头就变，整场对话的提示缓存作废一次。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from test_agent_service import wait_terminal
from test_memory_files import _RecordingProvider, _service_with_tools
from zhimeng_runtime.cache_shape import compare_shapes, request_shape
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.memory_files import append_user_memory

MEMORY = "用户记忆（以前的对话里记下的）：\n"


class _AllRequests(_RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[list[dict[str, object]]] = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.requests.append([dict(message) for message in messages])
        async for event in super().stream_chat(messages, cancel_event=cancel_event, tools=tools):
            yield event


def test_memory_written_mid_conversation_is_appended_at_the_end_and_the_opening_stays(tmp_path: Path) -> None:
    async def scenario() -> None:
        append_user_memory(tmp_path, "我不喜欢每句另起一行")
        provider = _AllRequests()
        service = _service_with_tools(tmp_path, provider)
        session = await service.create_session(project_id=None, title="随便聊")

        async def turn(text: str, key: str):
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

        await turn("你好。", "opening-freeze-1")
        first = provider.requests[-1]
        assert first[1] == {"role": "user", "content": MEMORY + "我不喜欢每句另起一行"}

        # 这场对话中途记了一条（模型调 memory_write 或作者在设置页改都一样）。
        append_user_memory(tmp_path, "主角叫沈砚")
        await turn("接着聊。", "opening-freeze-2")
        second = provider.requests[-1]
        # 开头那条原样不动：上一轮的请求整段都还是这一轮的开头（只在末尾追加）。
        assert second[1] == first[1]
        assert compare_shapes(request_shape(first, ()), request_shape(second, ()))["kept_messages"] == len(first)
        assert "（这场对话开始以后改过）" in str(second[-1]["content"])
        assert "主角叫沈砚" in str(second[-1]["content"])

        # 新开一场对话：开头就是现在的记忆，末尾不再追加。
        session2 = await service.create_session(project_id=None, title="随便聊2")
        started = await service.start_run(
            session_id=session2.session_id,
            message="又见面了。",
            expected_project_revision=None,
            expected_project_fingerprint=None,
            active_chapter_id=None,
            idempotency_key="opening-freeze-3",
        )
        await wait_terminal(service, started.run.run_id)
        fresh = provider.requests[-1]
        assert "主角叫沈砚" in str(fresh[1]["content"])
        assert not any("这场对话开始以后改过" in str(message.get("content", "")) for message in fresh)
        await service.shutdown()

    asyncio.run(scenario())


def test_unreadable_memory_is_not_recorded_or_reported_as_empty(tmp_path: Path) -> None:
    async def scenario() -> None:
        append_user_memory(tmp_path, "保留这条偏好")
        provider = _AllRequests()
        service = _service_with_tools(tmp_path, provider)
        session = await service.create_session(project_id=None, title="记忆读取失败")

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

        original_read = Path.read_bytes

        def read(candidate):
            if candidate == tmp_path / "用户记忆.md":
                raise PermissionError("temporarily locked")
            return original_read(candidate)

        # Exercise the actual filesystem reader; mocking its wrapper hid a swallowed error.
        with patch.object(Path, "read_bytes", read):
            await turn("第一次读取。", "opening-unreadable-1")
        first = provider.requests[-1]
        assert not any("用户记忆（以前的对话里记下的）" in str(message.get("content", "")) for message in first)
        assert not any("（空了）" in str(message.get("content", "")) for message in first)

        with patch("zhimeng_runtime.agent_service.read_user_memory", return_value="保留这条偏好"):
            await turn("读取恢复。", "opening-unreadable-2")
        second = provider.requests[-1]
        assert "用户记忆现在是：\n保留这条偏好" in str(second[-1]["content"])
        assert "（空了）" not in str(second[-1]["content"])
        await service.shutdown()

    asyncio.run(scenario())
