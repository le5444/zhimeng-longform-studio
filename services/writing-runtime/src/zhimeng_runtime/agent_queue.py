"""Durable pending inputs; promotion reuses the ordinary idempotent Run path."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from contextlib import contextmanager
from functools import cached_property
from typing import Literal
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.agent_models import AgentRunCreateRequest, AgentRunCreateResponse
from zhimeng_runtime.agent_images import claim_images
from zhimeng_runtime.agent_store import AgentStoreConflict, AgentStoreInvalidState, AgentStoreNotFound


class QueueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(pattern=r"^[a-zA-Z0-9-]{8,64}$")
    message: str = Field(default="", max_length=100_000)
    image_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None
    writer_mode: Literal["repair_only", "author_review_change_proposal"] = "repair_only"
    manual_file_ids: list[str] = Field(default_factory=list)
    manual_skill_names: list[str] = Field(default_factory=list)
    open_file_path: str | None = None

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        return value

    @model_validator(mode="after")
    def validate_run_input(self):
        request = AgentRunCreateRequest.model_validate(self.model_dump(include={
            "message", "writer_mode", "manual_file_ids", "manual_skill_names", "open_file_path", "image_ids",
        }))
        self.message = request.message
        self.image_ids = request.image_ids
        return self


class QueueItem(QueueInput):
    sending: bool = False
    delivery: Literal["queued", "steer_pending"] = "queued"
    steering_run_id: str | None = None


class QueueState(BaseModel):
    session_id: str
    revision: int
    pause_reason: Literal["user", "failed", "cancelled", "interrupted", "send_failed"] | None = None
    active_run_id: str | None = None
    items: list[QueueItem] = Field(default_factory=list)


class QueueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    action: Literal["pause", "resume", "edit", "remove", "move"]
    item_id: str | None = None
    message: str | None = Field(default=None, max_length=100_000)
    direction: Literal[-1, 1] | None = None

    @model_validator(mode="after")
    def check_action(self):
        if self.action in {"edit", "remove", "move"} and not self.item_id:
            raise ValueError("item_id is required")
        if self.action == "edit" and self.message is None:
            raise ValueError("message is required")
        if self.action == "move" and self.direction is None:
            raise ValueError("direction is required")
        return self


class QueueDispatchResponse(BaseModel):
    queue: QueueState
    accepted: AgentRunCreateResponse


class QueueSteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_id: str = Field(min_length=1)
    input: QueueInput | None = None


class QueueSteerResponse(BaseModel):
    queue: QueueState


class QueueInputError(AgentStoreInvalidState):
    pass


class AgentQueueStore:
    def __init__(self, store):
        self.store = store
        self.database = store.database

    @staticmethod
    def key(item_id: str) -> str:
        return f"queue:{item_id}"

    @contextmanager
    def _state(self, session_id: str):
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute("SELECT * FROM execution_session WHERE id = ?", (session_id,)).fetchone()
                if session is None:
                    raise AgentStoreNotFound("找不到这条对话")
                row = connection.execute("SELECT * FROM session_queue WHERE session_id = ?", (session_id,)).fetchone()
                state = json.loads(row["state_json"]) if row else {"items": [], "pause_reason": None, "observed_terminal": None}
                before = json.dumps(state, ensure_ascii=False)
                revision = int(row["revision"]) if row else 0
                # Admission is the source of truth even when its HTTP acknowledgement was lost.
                state["items"] = [item for item in state["items"] if not connection.execute(
                    "SELECT 1 FROM run WHERE session_id = ? AND idempotency_key = ?",
                    (session_id, self.key(item["input"]["item_id"])),
                ).fetchone()]
                retained = []
                for item in state["items"]:
                    receipt = connection.execute(
                        "SELECT s.*, r.status AS run_status FROM session_steering s JOIN run r ON r.id = s.run_id "
                        "WHERE s.session_id = ? AND s.item_id = ?", (session_id, item["input"]["item_id"]),
                    ).fetchone()
                    if receipt and receipt["status"] == "consumed":
                        continue
                    if receipt and receipt["status"] == "pending" and receipt["run_status"] not in {"queued", "starting", "running", "awaiting_approval"}:
                        connection.execute(
                            "UPDATE session_steering SET status = 'released', updated_at = ? WHERE session_id = ? AND item_id = ?",
                            (datetime.now(UTC).isoformat(), session_id, item["input"]["item_id"]),
                        )
                        item.pop("steering_run_id", None)
                    elif receipt and receipt["status"] == "pending":
                        item["steering_run_id"] = receipt["run_id"]
                    else:
                        item.pop("steering_run_id", None)
                    retained.append(item)
                state["items"] = retained
                latest = connection.execute(
                    "SELECT id, status FROM run WHERE session_id = ? AND run_type != 'conversation_fork' ORDER BY ordinal DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                active_id = None
                if latest:
                    if latest["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                        if latest["id"] != state["observed_terminal"]:
                            if state["items"] and latest["status"] != "succeeded":
                                state["pause_reason"] = latest["status"]
                            state["observed_terminal"] = latest["id"]
                    else:
                        active_id = latest["id"]
                reconciled = json.dumps(state, ensure_ascii=False)
                if reconciled != before:
                    revision += 1
                before = reconciled
                view = QueueState(session_id=session_id, revision=revision, active_run_id=active_id)
                yield connection, session, state, view
                serialized = json.dumps(state, ensure_ascii=False)
                if serialized != before:
                    revision += 1
                connection.execute(
                    "INSERT INTO session_queue(session_id, revision, state_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET revision = excluded.revision, state_json = excluded.state_json",
                    (session_id, revision, serialized),
                )
                connection.commit()
                view.revision = revision
                view.pause_reason = state["pause_reason"]
                view.items = [QueueItem(
                    **item["input"], sending=item.get("request") is not None,
                    delivery="steer_pending" if item.get("steering_run_id") else "queued",
                    steering_run_id=item.get("steering_run_id"),
                ) for item in state["items"]]
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _require_open(session):
        if session["status"] not in {"active", "idle"}:
            raise AgentStoreInvalidState("这条对话已关闭")

    def read(self, session_id: str) -> QueueState:
        with self._state(session_id) as (_, _, _, view):
            pass
        return view

    def pending_session_ids(self) -> list[str]:
        with self.database.connection() as connection:
            return [row["session_id"] for row in connection.execute(
                "SELECT q.session_id FROM session_queue q JOIN execution_session s ON s.id = q.session_id "
                "WHERE s.status IN ('active', 'idle') AND json_array_length(q.state_json, '$.items') > 0"
            )]

    def latest_run_id(self, session_id: str) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id FROM run WHERE session_id = ? AND run_type != 'conversation_fork' ORDER BY ordinal DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return row["id"] if row else None

    def recover_dispatches(self) -> None:
        # Run admission is durable before any Provider call. After process restart,
        # an unresolved prepare has no living sender; retain the input and pause.
        for session_id in self.pending_session_ids():
            with self._state(session_id) as (_, _, state, _):
                for item in state["items"]:
                    if item.get("request") is not None:
                        item["request"] = None
                        state["pause_reason"] = state["pause_reason"] or "interrupted"

    def enqueue(self, session_id: str, entry: QueueInput) -> QueueState:
        with self._state(session_id) as (connection, session, state, view):
            claim_images(connection, session_id, entry.image_ids)
            self._require_open(session)
            if entry.project_id != session["project_binding_id"]:
                raise AgentStoreConflict("对话的项目已经变化，请刷新后再排队")
            existing = next((item for item in state["items"] if item["input"]["item_id"] == entry.item_id), None)
            if existing:
                if QueueInput.model_validate(existing["input"]).model_dump() != entry.model_dump():
                    raise AgentStoreConflict("同一条排队消息的内容不一致，请刷新")
            elif not connection.execute("SELECT 1 FROM run WHERE session_id = ? AND idempotency_key = ?", (session_id, self.key(entry.item_id))).fetchone() and not connection.execute(
                "SELECT 1 FROM session_steering WHERE session_id = ? AND item_id = ? AND status = 'consumed'", (session_id, entry.item_id),
            ).fetchone():
                state["items"].append({"input": entry.model_dump(), "request": None})
        return view

    def update(self, session_id: str, change: QueueUpdate) -> QueueState:
        with self._state(session_id) as (_, session, state, view):
            self._require_open(session)
            if change.revision != view.revision:
                raise AgentStoreConflict("队列已在别处更新，请刷新后再操作")
            if change.action in {"pause", "resume"}:
                state["pause_reason"] = "user" if change.action == "pause" else None
            else:
                index = next((index for index, item in enumerate(state["items"]) if item["input"]["item_id"] == change.item_id), None)
                if index is None:
                    raise AgentStoreNotFound("这条排队消息已处理，请刷新")
                item = state["items"][index]
                if item.get("request") is not None or item.get("steering_run_id"):
                    raise AgentStoreConflict("这条消息正在核对发送结果，请稍后再修改")
                if change.action == "remove":
                    state["items"].pop(index)
                elif change.action == "edit":
                    message = change.message.strip()
                    if not message and not item["input"].get("image_ids"):
                        raise QueueInputError("请输入内容或保留图片后再保存")
                    item["input"]["message"] = message or "[图片]"
                elif change.action == "move":
                    target = index + change.direction
                    if 0 <= target < len(state["items"]):
                        state["items"][index], state["items"][target] = state["items"][target], item
        return view

    def prepare(self, session_id: str, item_id: str) -> AgentRunCreateRequest:
        with self._state(session_id) as (connection, session, state, view):
            self._require_open(session)
            if connection.execute(
                "SELECT 1 FROM session_steering WHERE session_id = ? AND item_id = ? AND status IN ('pending', 'consumed')",
                (session_id, item_id),
            ).fetchone():
                raise AgentStoreConflict("这条消息已经交给当前运行处理，不能再另开一轮发送")
            if state["pause_reason"] or view.active_run_id:
                raise AgentStoreConflict("队列已暂停或上一轮尚未结束")
            if not state["items"] or state["items"][0]["input"]["item_id"] != item_id:
                raise AgentStoreConflict("队首已经变化，请刷新")
            item = state["items"][0]
            entry = QueueInput.model_validate(item["input"])
            if entry.project_id != session["project_binding_id"]:
                raise AgentStoreConflict("这条排队消息属于原项目，请移除或切回原项目")
            if item.get("request") is None:
                payload = entry.model_dump(exclude={"item_id", "project_id"})
                if entry.project_id:
                    project = connection.execute("SELECT revision, fingerprint FROM project_binding WHERE id = ?", (entry.project_id,)).fetchone()
                    if project is None:
                        raise AgentStoreNotFound("排队消息的项目已不存在")
                    payload.update(expected_project_revision=project["revision"], expected_project_fingerprint=project["fingerprint"])
                item["request"] = AgentRunCreateRequest.model_validate(payload).model_dump()
            request = AgentRunCreateRequest.model_validate(item["request"])
        return request

    def steer(self, session_id: str, item_id: str, expected_run_id: str) -> QueueState:
        with self._state(session_id) as (connection, session, state, view):
            self._require_open(session)
            receipt = connection.execute(
                "SELECT * FROM session_steering WHERE session_id = ? AND item_id = ?", (session_id, item_id),
            ).fetchone()
            if receipt and receipt["status"] in {"pending", "consumed"}:
                if receipt["run_id"] != expected_run_id:
                    raise AgentStoreConflict("这条消息已交给另一轮处理，请刷新核对")
            else:
                run = connection.execute("SELECT * FROM run WHERE id = ? AND session_id = ?", (expected_run_id, session_id)).fetchone()
                if not run or view.active_run_id != expected_run_id or run["status"] not in {"queued", "starting", "running", "awaiting_approval"}:
                    raise AgentStoreConflict("这轮已结束或正在停止；消息仍留在队列里，可以等下一轮发送")
                item = next((item for item in state["items"] if item["input"]["item_id"] == item_id), None)
                if item is None:
                    raise AgentStoreNotFound("这条排队消息已处理，请刷新")
                if item.get("request") is not None:
                    raise AgentStoreConflict("这条消息正在核对发送结果，请稍后再插入")
                if item["input"]["project_id"] != session["project_binding_id"] or run["project_binding_id"] != session["project_binding_id"]:
                    raise AgentStoreConflict("消息和当前运行不在同一个项目，请刷新核对")
                now = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO session_steering(session_id,item_id,run_id,input_json,status,message_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,'pending',NULL,?,?) ON CONFLICT(session_id,item_id) DO UPDATE SET "
                    "run_id=excluded.run_id,input_json=excluded.input_json,status='pending',message_id=NULL,updated_at=excluded.updated_at",
                    (session_id, item_id, expected_run_id, json.dumps(item["input"], ensure_ascii=False), now, now),
                )
                item["steering_run_id"] = expected_run_id
        return view

    def failed(self, session_id: str, item_id: str) -> None:
        with self._state(session_id) as (_, _, state, _):
            for item in state["items"]:
                if item["input"]["item_id"] == item_id:
                    item["request"] = None
                    state["pause_reason"] = "send_failed"

    def steering_failed(self, session_id: str, item_id: str) -> None:
        with self._state(session_id) as (_, _, state, _):
            if any(item["input"]["item_id"] == item_id for item in state["items"]):
                state["pause_reason"] = state["pause_reason"] or "user"


class AgentQueueService:
    def __init__(self, agent):
        self.agent = agent
        # Promotion and editing serialize within a conversation. A slow project
        # scan in another conversation must leave this one's controls available.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    @cached_property
    def store(self) -> AgentQueueStore:
        return AgentQueueStore(self.agent.store)

    async def execution_pending(self, session_id: str) -> bool:
        latest = await asyncio.to_thread(self.store.latest_run_id, session_id)
        return latest is not None and self.agent.is_run_executing(latest)

    async def refresh_provider(self, session_id: str) -> None:
        # Credentials, headers and URLs can change while an input is queued.
        # Rebind the session's own selection, never substitute the global model.
        session = await asyncio.to_thread(self.agent.store.get_session, session_id)
        provider_id, model, effort = self.agent._session_provider_preference(session)
        await self.agent.update_session(session_id, rebind_provider=True,
            provider_id=provider_id, model=model, reasoning_effort=effort)

    async def refresh_project(self, session_id: str) -> None:
        session = await asyncio.to_thread(self.agent.store.get_session, session_id)
        if not session.project_id:
            return
        binding = await asyncio.to_thread(self.store.database.get_project_binding, session.project_id)
        executor = self.agent.tool_executor
        if binding is None or executor is None:
            raise AgentStoreInvalidState("排队消息的项目当前不可用")
        locks = self.agent.project_locks or executor.locks
        async with locks.for_path(binding.root_path):
            observed_at = datetime.now(UTC)
            snapshot = await asyncio.to_thread(executor.reader.inspect, binding.root_path)
            project = snapshot["project"]
            await asyncio.to_thread(self.store.database.sync_project_binding,
                project_id=project["project_id"], root_path=project["root_path"],
                display_name=binding.display_name, fingerprint=project["fingerprint"], observed_at=observed_at)

    async def dispatch(self, session_id: str, item_id: str, *, refresh_project: bool = False) -> QueueDispatchResponse:
        if refresh_project:
            # Scanning can take a while, particularly on a network/project drive.
            # Keep pause/edit/steer responsive; admission rechecks the queue below.
            async with self.lock_for(session_id):
                prior = await asyncio.to_thread(self.agent.store.find_run_by_idempotency_key, session_id, self.store.key(item_id))
                needs_refresh = False
                if prior is None:
                    state = await asyncio.to_thread(self.store.read, session_id)
                    if state.pause_reason or state.active_run_id or not state.items or state.items[0].item_id != item_id:
                        raise AgentStoreConflict("队列已暂停、队首变化或上一轮尚未结束")
                    if await self.execution_pending(session_id):
                        raise AgentStoreConflict("上一轮正在收尾保存，请稍后再发送")
                    needs_refresh = not state.items[0].sending
            if needs_refresh:
                try:
                    await self.refresh_project(session_id)
                except Exception:
                    async with self.lock_for(session_id):
                        current = await asyncio.to_thread(self.store.read, session_id)
                        if not current.pause_reason and not current.active_run_id and current.items and current.items[0].item_id == item_id:
                            await asyncio.to_thread(self.store.failed, session_id, item_id)
                    raise
        async with self.lock_for(session_id):
            key = self.store.key(item_id)
            accepted = await asyncio.to_thread(self.agent.store.find_run_by_idempotency_key, session_id, key)
            if accepted is None:
                if await self.execution_pending(session_id):
                    raise AgentStoreConflict("上一轮正在收尾保存，请稍后再发送")
                if refresh_project:
                    current = await asyncio.to_thread(self.store.read, session_id)
                    if current.pause_reason or current.active_run_id or not current.items or current.items[0].item_id != item_id:
                        raise AgentStoreConflict("队列已暂停、队首变化或上一轮尚未结束")
                request = await asyncio.to_thread(self.store.prepare, session_id, item_id)
                try:
                    await self.refresh_provider(session_id)
                    result = await self.agent.start_run(session_id=session_id, idempotency_key=key, **request.model_dump())
                    accepted = (result.run, result.user_message)
                    replayed = result.replayed
                except Exception:
                    # A cancelled/disconnected HTTP response may still have admitted the Run.
                    accepted = await asyncio.to_thread(self.agent.store.find_run_by_idempotency_key, session_id, key)
                    if accepted is None:
                        await asyncio.to_thread(self.store.failed, session_id, item_id)
                        raise
                    replayed = True
            else:
                replayed = True
            queue = await asyncio.to_thread(self.store.read, session_id)
            return QueueDispatchResponse(queue=queue, accepted=AgentRunCreateResponse(run=accepted[0], user_message=accepted[1], replayed=replayed))
