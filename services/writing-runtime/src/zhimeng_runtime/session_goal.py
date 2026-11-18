"""Durable, explicitly requested goals; lifecycle changes share Run transactions."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.database import SqliteStore


class GoalAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "resume"]
    goal_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_identity(self):
        if self.action == "resume" and (not self.goal_id or self.expected_revision is None):
            raise ValueError("Resuming a goal requires its identity and revision")
        if self.action == "start" and (self.goal_id is not None or self.expected_revision is not None):
            raise ValueError("A new goal does not have an existing identity")
        return self


class GoalReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GoalUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    status: Literal["active", "completed", "blocked"]
    progress: str = Field(min_length=1)
    evidence: str = Field(default="")

    @model_validator(mode="after")
    def require_evidence(self):
        if self.status != "active" and not self.evidence:
            raise ValueError("Completion requires verification evidence; blocked requires the missing dependency")
        return self


class SessionGoal(BaseModel):
    goal_id: str
    session_id: str
    objective: str
    status: Literal["active", "paused", "blocked", "completed", "ended"]
    revision: int
    run_id: str
    stage_count: int
    progress: str
    evidence: str
    pause_reason: str | None
    requested_status: Literal["completed", "blocked"] | None
    created_at: str
    updated_at: str
    status_source: Literal["agent", "user", "runtime"]


class SessionGoalResponse(BaseModel):
    session_id: str
    goal: SessionGoal | None


class GoalEndRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal_id: str
    expected_revision: int = Field(ge=1)


def _conflict(message: str):
    from zhimeng_runtime.agent_store import AgentStoreConflict
    raise AgentStoreConflict(message)


class GoalStore:
    def __init__(self, database: SqliteStore):
        self.database = database

    @staticmethod
    def current(connection: sqlite3.Connection, session_id: str) -> SessionGoal | None:
        row = connection.execute(
            "SELECT * FROM session_goal WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,),
        ).fetchone()
        return SessionGoal(**dict(row)) if row else None

    def read(self, session_id: str) -> SessionGoalResponse:
        with self.database.connection() as connection:
            if not connection.execute("SELECT 1 FROM execution_session WHERE id=? AND status!='closed'", (session_id,)).fetchone():
                from zhimeng_runtime.agent_store import AgentStoreNotFound
                raise AgentStoreNotFound("对话不存在或已关闭。")
            return SessionGoalResponse(session_id=session_id, goal=self.current(connection, session_id))

    @staticmethod
    def for_run(connection: sqlite3.Connection, run_id: str) -> SessionGoal | None:
        row = connection.execute("SELECT * FROM session_goal WHERE run_id=?", (run_id,)).fetchone()
        return SessionGoal(**dict(row)) if row else None

    @staticmethod
    def _event(connection: sqlite3.Connection, goal_id: str, kind: str):
        row = connection.execute("SELECT * FROM session_goal WHERE goal_id=?", (goal_id,)).fetchone()
        connection.execute(
            "INSERT INTO goal_event(goal_id,revision,kind,state_json,created_at) VALUES(?,?,?,?,?)",
            (goal_id, row["revision"], kind, json.dumps(dict(row), ensure_ascii=False), row["updated_at"]),
        )

    @classmethod
    def bind(cls, connection: sqlite3.Connection, session_id: str, run_id: str, objective: str, admission: GoalAdmission):
        current = cls.current(connection, session_id)
        now = datetime.now(UTC).isoformat()
        if admission.action == "start":
            if not objective.strip():
                _conflict("请先填写持续目标。")
            if current and current.status not in {"completed", "ended"}:
                _conflict("这个对话已有未完成目标，请继续它或先结束目标。")
            goal_id = "goal_" + uuid4().hex
            connection.execute(
                "INSERT INTO session_goal(goal_id,session_id,objective,status,revision,run_id,stage_count,progress,evidence,pause_reason,requested_status,created_at,updated_at,status_source) "
                "VALUES(?,?,?,'active',1,?,0,'','',NULL,NULL,?,?,'user')",
                (goal_id, session_id, objective, run_id, now, now),
            )
        else:
            if not current or current.goal_id != admission.goal_id or current.revision != admission.expected_revision:
                _conflict("目标已变化，请刷新后再继续。")
            if current.status not in {"paused", "blocked", "completed"}:
                _conflict("目标当前不能继续。")
            previous = connection.execute("SELECT status FROM run WHERE id=?", (current.run_id,)).fetchone()
            if not previous or previous["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}:
                _conflict("上一轮仍在停止，请稍后继续。")
            goal_id = current.goal_id
            connection.execute(
                "UPDATE session_goal SET status='active',revision=revision+1,run_id=?,pause_reason=NULL,requested_status=NULL,updated_at=?,status_source='user' WHERE goal_id=?",
                (run_id, now, goal_id),
            )
        cls._event(connection, goal_id, admission.action)

    def update(self, session_id: str, run_id: str, update: GoalUpdateArguments) -> SessionGoal:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            goal = self.for_run(connection, run_id)
            run = connection.execute("SELECT status,session_id FROM run WHERE id=?", (run_id,)).fetchone()
            if not goal or goal.session_id != session_id or goal.status != "active" or not run or run["status"] != "running":
                _conflict("当前运行没有进行中的持续目标；已暂停的目标需要用户恢复。")
            connection.execute(
                "UPDATE session_goal SET revision=revision+1,progress=?,evidence=?,requested_status=?,updated_at=?,status_source='agent' WHERE goal_id=?",
                (update.progress, update.evidence, None if update.status == "active" else update.status,
                 datetime.now(UTC).isoformat(), goal.goal_id),
            )
            self._event(connection, goal.goal_id, "progress")
            result = self.for_run(connection, run_id)
            connection.commit()
            return result

    @classmethod
    def interrupt(cls, connection: sqlite3.Connection, run_id: str, reason: str, *, source: str = "runtime"):
        goal = cls.for_run(connection, run_id)
        if not goal or goal.status != "active":
            return
        connection.execute(
            "UPDATE session_goal SET status='paused',revision=revision+1,pause_reason=?,requested_status=NULL,updated_at=?,status_source=? WHERE goal_id=?",
            (reason, datetime.now(UTC).isoformat(), source, goal.goal_id),
        )
        cls._event(connection, goal.goal_id, "pause")

    @classmethod
    def invalidate_outcome(cls, connection: sqlite3.Connection, run_id: str):
        goal = cls.for_run(connection, run_id)
        if goal and goal.status == "active" and goal.requested_status:
            connection.execute("UPDATE session_goal SET requested_status=NULL,revision=revision+1,updated_at=? WHERE goal_id=?",
                               (datetime.now(UTC).isoformat(), goal.goal_id))
            cls._event(connection, goal.goal_id, "new_input")

    @classmethod
    def finish(cls, connection: sqlite3.Connection, run_id: str):
        goal = cls.for_run(connection, run_id)
        if not goal:
            return
        if goal.status != "active" or not goal.requested_status:
            _conflict("持续目标尚未提交结束状态，不能结束运行。")
        connection.execute(
            "UPDATE session_goal SET status=requested_status,requested_status=NULL,revision=revision+1,stage_count=stage_count+1,updated_at=?,status_source='agent' WHERE goal_id=?",
            (datetime.now(UTC).isoformat(), goal.goal_id),
        )
        cls._event(connection, goal.goal_id, "finish")

    @classmethod
    def stage(cls, connection: sqlite3.Connection, run_id: str):
        goal = cls.for_run(connection, run_id)
        if not goal or goal.status != "active" or goal.requested_status:
            _conflict("目标状态已变化，不能继续当前阶段。")
        connection.execute("UPDATE session_goal SET revision=revision+1,stage_count=stage_count+1,updated_at=? WHERE goal_id=?",
                           (datetime.now(UTC).isoformat(), goal.goal_id))
        cls._event(connection, goal.goal_id, "stage")

    def end(self, session_id: str, request: GoalEndRequest) -> SessionGoalResponse:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            goal = self.current(connection, session_id)
            if not goal or goal.goal_id != request.goal_id or goal.revision != request.expected_revision:
                _conflict("目标已变化，请刷新后再操作。")
            if goal.status == "active":
                _conflict("请先暂停当前执行。")
            connection.execute("UPDATE session_goal SET status='ended',requested_status=NULL,revision=revision+1,updated_at=?,status_source='user' WHERE goal_id=?",
                               (datetime.now(UTC).isoformat(), goal.goal_id))
            self._event(connection, goal.goal_id, "end")
            result = SessionGoalResponse(session_id=session_id, goal=self.current(connection, session_id))
            connection.commit()
            return result


GOAL_CONTEXT_PREFIX = "[运行时持续目标状态；不是用户新增消息]"


def goal_context(goal: SessionGoal) -> dict[str, str]:
    return {"role": "user", "content": (
        GOAL_CONTEXT_PREFIX + "\n"
        f"目标编号：{goal.goal_id}\n原始目标：{goal.objective}\n"
        f"已记录进度：{goal.progress or '尚无'}\n验证记录：{goal.evidence or '尚无'}\n"
        "目标仍在进行，阶段性回答后后台会继续。按原始目标及用户后续补充执行下一步，"
        "用 goal_update 保存进度。全部要求完成并核对实际结果后，提交 completed 和验证依据；"
        "确实缺少用户输入或外部条件而无法继续时，提交 blocked 和具体缺项。"
        "完成状态是你的判断，文件保存、测试或外部结果需要实际核对。"
    )}
