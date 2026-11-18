"""「只读工具并行、写工具串行独占」的执行语义测试。

外在可观测结果必须与串行一致：tool 结果消息严格按模型给出的
tool_calls 顺序回喂；写调用执行时不与任何只读调用重叠；单个只读
失败不拖垮同批其他调用；并发落库不撞 activity 序号唯一约束。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import (
    READONLY_TOOL_NAMES,
    AgentToolError,
    AgentToolExecution,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)

PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "b" * 64


class TurnProvider:
    def __init__(self, turns: list[list[ProviderEvent]]) -> None:
        self.turns = turns
        self.start_count = 0
        self.messages_by_turn: list[list[dict[str, object]]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "b" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.turn-provider",
            kind=ExecutorKind.EMBEDDED,
            implementation="turn-provider",
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
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        self.start_count += 1
        self.messages_by_turn.append([dict(message) for message in messages])
        if self.start_count > len(self.turns):
            raise AssertionError("Provider was dispatched for too many turns")
        for event in self.turns[self.start_count - 1]:
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield event


def _tool_definition(name: str) -> ProviderToolDefinition:
    return ProviderToolDefinition(
        name=name,
        version="1",
        description=f"fake {name}",
        parameters={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "additionalProperties": True,
        },
    )


class ConcurrencyTrackingExecutor:
    """记录只读/写调用真实重叠情况的假执行器。"""

    def __init__(self, *, delay: float = 0.2, fail_file_ids: set[str] | None = None) -> None:
        self.delay = delay
        self.fail_file_ids = set(fail_file_ids or ())
        self.definitions = (
            _tool_definition("project_read_file"),
            _tool_definition("project_list_files"),
            _tool_definition("project_search"),
            _tool_definition("change_set_propose"),
        )
        self.calls: list[str] = []
        self.active_readonly = 0
        self.max_active_readonly = 0
        self.write_active = False
        self.write_overlapped_readonly = False
        self.readonly_during_write = 0
        self.first_readonly_started: float | None = None
        self.last_readonly_finished: float | None = None

    @staticmethod
    def request_hash(tool_name: str, tool_version: str, arguments: dict[str, object]) -> str:
        material = json.dumps(
            [tool_name, tool_version, arguments],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def execute(
        self,
        run,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        request_hash: str,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        del run, request_hash, cancel_event
        self.calls.append(tool_name)
        if tool_name in READONLY_TOOL_NAMES:
            if self.write_active:
                self.readonly_during_write += 1
            self.active_readonly += 1
            self.max_active_readonly = max(self.max_active_readonly, self.active_readonly)
            started = time.perf_counter()
            if self.first_readonly_started is None:
                self.first_readonly_started = started
            try:
                await asyncio.sleep(self.delay)
                if arguments.get("file_id") in self.fail_file_ids:
                    raise AgentToolError(
                        "这个文件读不出来（假故障）",
                        code="agent_tool_validation_failed",
                        retryable=False,
                    )
                return AgentToolExecution(
                    output={"tool": tool_name, "call": tool_call_id}
                )
            finally:
                self.active_readonly -= 1
                self.last_readonly_finished = time.perf_counter()
        self.write_active = True
        if self.active_readonly != 0:
            self.write_overlapped_readonly = True
        try:
            await asyncio.sleep(self.delay)
            return AgentToolExecution(output={"proposed": tool_call_id})
        finally:
            self.write_active = False


def build_service(
    tmp_path: Path,
    provider: TurnProvider,
    *,
    tool_executor: ConcurrencyTrackingExecutor,
) -> AgentService:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    return AgentService(AgentStore(database), provider, tool_executor=tool_executor)


async def wait_terminal(service: AgentService, run_id: str):
    for _ in range(300):
        run = await asyncio.to_thread(service.store.get_run, run_id)
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("Run did not reach a terminal state")


async def create_turn(service: AgentService):
    session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
    result = await service.start_run(
        session_id=session.session_id,
        message="把这几份资料都看一下。",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key="turn-00000001",
    )
    return session, result


def tool_batch_turn(calls: list[tuple[str, str, dict[str, object]]]) -> list[ProviderEvent]:
    events: list[ProviderEvent] = [
        ProviderToolCall(
            index=index,
            tool_call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        for index, (call_id, tool_name, arguments) in enumerate(calls)
    ]
    events.append(ProviderCompleted("tool_calls", None))
    return events


def final_turn(text: str = "都看完了。") -> list[ProviderEvent]:
    return [ProviderTextDelta(text), ProviderCompleted("stop", None)]


def tool_messages_in_order(provider: TurnProvider) -> list[dict[str, object]]:
    """取「带着工具结果回喂模型」那一次请求里的 tool 消息。

    不能读最后一次请求：Run 收尾之后还有起名这类后台模型调用会追加进
    messages_by_turn，读 [-1] 就是在和它赌时序——2026-09-20 实测时红时绿，
    整套跑满时必红。
    """
    for turn in provider.messages_by_turn:
        tool_messages = [message for message in turn if message.get("role") == "tool"]
        if tool_messages:
            return tool_messages
    return []


def test_readonly_batch_runs_in_parallel_with_ordered_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ConcurrencyTrackingExecutor(delay=0.2)
        provider = TurnProvider(
            [
                tool_batch_turn(
                    [
                        ("call_a", "project_read_file", {"file_id": "file-" + "1" * 32}),
                        ("call_b", "project_list_files", {}),
                        ("call_c", "project_read_file", {"file_id": "file-" + "2" * 32}),
                    ]
                ),
                final_turn(),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        # 真的并行了：同一时刻至少两个只读调用在执行。
        assert executor.max_active_readonly >= 2
        # 结果消息严格按模型给出的调用顺序回喂。
        tool_messages = tool_messages_in_order(provider)
        assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b", "call_c"]
        for message in tool_messages:
            assert json.loads(str(message["content"]))["status"] == "succeeded"
        # 并行窗口明显小于三次串行（3 × 0.2s = 0.6s）。
        assert executor.first_readonly_started is not None
        assert executor.last_readonly_finished is not None
        window = executor.last_readonly_finished - executor.first_readonly_started
        assert window < 0.5
        await service.shutdown()

    asyncio.run(scenario())


def test_mixed_batch_keeps_write_calls_exclusive(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ConcurrencyTrackingExecutor(delay=0.1)
        provider = TurnProvider(
            [
                tool_batch_turn(
                    [
                        ("call_a", "project_read_file", {"file_id": "file-" + "1" * 32}),
                        ("call_w", "change_set_propose", {"title": "改稿"}),
                        ("call_b", "project_read_file", {"file_id": "file-" + "2" * 32}),
                        ("call_c", "project_search", {"query": "伏笔"}),
                    ]
                ),
                final_turn(),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        # 写调用执行时不与任何只读调用重叠。
        assert executor.write_overlapped_readonly is False
        assert executor.readonly_during_write == 0
        # 写调用之后的连续只读段（call_b + call_c）仍然并行了。
        assert executor.max_active_readonly >= 2
        # 四个结果按原始顺序回喂，写调用夹在中间不错位。
        tool_messages = tool_messages_in_order(provider)
        assert [m["tool_call_id"] for m in tool_messages] == [
            "call_a",
            "call_w",
            "call_b",
            "call_c",
        ]
        await service.shutdown()

    asyncio.run(scenario())


def test_single_readonly_failure_does_not_break_the_batch(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ConcurrencyTrackingExecutor(
            delay=0.05, fail_file_ids={"file-" + "9" * 32}
        )
        provider = TurnProvider(
            [
                tool_batch_turn(
                    [
                        ("call_a", "project_read_file", {"file_id": "file-" + "1" * 32}),
                        ("call_b", "project_read_file", {"file_id": "file-" + "9" * 32}),
                        ("call_c", "project_list_files", {}),
                    ]
                ),
                final_turn("中间那份读不出来，另外两份看完了。"),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        # 一次非重试失败不终止 Run，同批其他结果照常回喂。
        assert run.status is RunStatus.SUCCEEDED
        tool_messages = tool_messages_in_order(provider)
        assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b", "call_c"]
        statuses = {
            str(m["tool_call_id"]): json.loads(str(m["content"]))["status"]
            for m in tool_messages
        }
        assert statuses == {
            "call_a": "succeeded",
            "call_b": "failed",
            "call_c": "succeeded",
        }
        await service.shutdown()

    asyncio.run(scenario())


def test_parallel_readonly_calls_never_collide_on_activity_sequence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = ConcurrencyTrackingExecutor(delay=0.03)
        provider = TurnProvider(
            [
                tool_batch_turn(
                    [
                        (f"call_{index}", "project_read_file", {"file_id": f"file-{index:032d}"})
                        for index in range(6)
                    ]
                ),
                final_turn(),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        activities = service.store.list_activities(run.run_id)
        sequences = [activity.root.sequence for activity in activities]
        # UNIQUE(run_id, sequence) 没撞号：序号集合无重复且连续。
        assert len(sequences) == len(set(sequences))
        assert sequences == list(range(1, len(sequences) + 1))
        stored_calls = service.store.list_tool_calls(run.run_id)
        assert len(stored_calls) == 6
        assert {stored.status for stored in stored_calls} == {"succeeded"}
        await service.shutdown()

    asyncio.run(scenario())


def test_rejected_call_keeps_its_position_in_the_ordered_results(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        executor = ConcurrencyTrackingExecutor(delay=0.02)
        provider = TurnProvider(
            [
                tool_batch_turn(
                    [
                        ("call_x", "delete_file", {}),
                        ("call_a", "project_read_file", {"file_id": "file-" + "1" * 32}),
                        ("call_b", "project_list_files", {}),
                    ]
                ),
                final_turn("未通告的工具没有执行，另外两份看完了。"),
            ]
        )
        service = build_service(tmp_path, provider, tool_executor=executor)
        _, result = await create_turn(service)
        run = await wait_terminal(service, result.run.run_id)

        assert run.status is RunStatus.SUCCEEDED
        # 未通告的工具不进执行器，后面的只读段照常并行执行。
        assert executor.calls == ["project_read_file", "project_list_files"]
        tool_messages = tool_messages_in_order(provider)
        assert [m["tool_call_id"] for m in tool_messages] == ["call_x", "call_a", "call_b"]
        first = json.loads(str(tool_messages[0]["content"]))
        assert first["status"] == "failed"
        assert first["error"]["error"]["code"] == "agent_tool_not_advertised"
        await service.shutdown()

    asyncio.run(scenario())
