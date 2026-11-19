"""关机时 start_run 不许原地空转（9-28）。

start_run 在线程里建运行记录，外面用 ``asyncio.shield`` 等它、吞掉取消（作者关页面也要把记录建完）。
可事件循环收尾时会把所有任务都取消，建记录那一步本身也被取消：再 await 它只会立刻又报取消，
``while True`` 这个循环就一直不让出事件循环，整个进程卡住。9-28 一条压缩测试失败后 asyncio.run 收尾时撞上，
py-spy 看到主线程停在 ``await asyncio.shield(create_operation)`` 那一行、一核占满。
场景放在单独的线程里跑、限时 20 秒：旧写法空转时 asyncio 自己的超时根本轮不到执行。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from test_agent_service import FINGERPRINT, PROJECT_ID, TurnScriptedProvider, build_service


class _LoopTornDown(RuntimeError):
    pass


def test_start_run_gives_up_when_the_record_creation_itself_is_cancelled(tmp_path: Path) -> None:
    outcome: dict[str, object] = {}
    creating = threading.Event()

    async def scenario() -> None:
        service = build_service(tmp_path, TurnScriptedProvider([]))
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        original = service.store.create_run

        def slow_create_run(**kwargs):
            creating.set()
            threading.Event().wait(1.0)
            return original(**kwargs)

        service.store.create_run = slow_create_run
        asyncio.create_task(
            service.start_run(
                session_id=session.session_id,
                message="你好",
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id=None,
                idempotency_key="cancel-spin",
            )
        )
        for _ in range(200):
            if creating.is_set():
                break
            await asyncio.sleep(0.01)
        outcome["creating"] = creating.is_set()
        # 事件循环因为别处报错收尾：asyncio.run 会把 start_run 和建记录那一步一起取消。
        raise _LoopTornDown()

    def worker() -> None:
        try:
            asyncio.run(scenario())
        except _LoopTornDown:
            outcome["torn_down"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "start_run 在建记录那一步被取消后原地空转，事件循环收不了尾"
    assert outcome == {"creating": True, "torn_down": True}


@pytest.mark.parametrize("stage", ["finalize", "release", "terminal_store"])
def test_cancelled_internal_operation_propagates_without_spinning(tmp_path, monkeypatch, stage):
    async def scenario():
        service = build_service(tmp_path, TurnScriptedProvider([]))
        session = await service.create_session(project_id=PROJECT_ID, title="Cancellation recovery")
        original_shield = asyncio.shield
        observed = set()

        def bounded_shield(operation):
            if operation.cancelled():
                assert id(operation) not in observed, "cancelled operation was awaited again"
                observed.add(id(operation))
            return original_shield(operation)

        async def cancelled(*_args):
            raise asyncio.CancelledError

        async def finalized(run_id):
            return service.store.get_run(run_id)

        monkeypatch.setattr(asyncio, "shield", bounded_shield)
        monkeypatch.setattr(service, "_finalize_run_start", cancelled if stage == "finalize" else finalized)
        if stage == "release":
            monkeypatch.setattr(service, "_release_start_operation", cancelled)
        with pytest.raises(asyncio.CancelledError):
            if stage == "terminal_store":
                def cancelled_store():
                    raise asyncio.CancelledError
                await service._await_terminal_store_operation(cancelled_store)
            else:
                await service.start_run(
                    session_id=session.session_id, message="Check a text file",
                    expected_project_revision=1, expected_project_fingerprint=FINGERPRINT,
                    active_chapter_id=None, idempotency_key=f"cancel-{stage}",
                )

    asyncio.run(scenario())
