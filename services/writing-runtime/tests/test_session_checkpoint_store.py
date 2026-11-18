"""会话摘要的存取（agent_store）。

分工：``test_session_checkpoint.py`` 管纯函数（怎么向模型描述整理任务、
怎么清洗交回来的摘要），本文件只管落库与读取的不变量：
摘要只能往前推进、同一段历史不会被折叠两次、读回来逐字段一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhimeng_runtime.agent_store import (
    AgentStore,
    AgentStoreConflict,
    AgentStoreNotFound,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.session_checkpoint import (
    checkpoint_content_hash,
    render_checkpoint_block,
)

PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "a" * 64

SUMMARY = (
    "【当前目标】写完第 3 章。\n"
    "【试过但不行的做法】把主角改成女性，作者当场否掉了。"
)


@pytest.fixture()
def store(tmp_path: Path) -> AgentStore:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    return AgentStore(database)


@pytest.fixture()
def session_id(store: AgentStore) -> str:
    session = store.create_session(
        project_id=PROJECT_ID,
        title="摘要存取",
        executor=ExecutorIdentity(
            executor_id="executor.test-agent",
            kind=ExecutorKind.EMBEDDED,
            implementation="test-agent",
            version="1",
            protocol_version="1",
            instance_id=None,
        ),
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        ),
    )
    return session.session_id


def test_latest_is_none_before_anything_is_folded(
    store: AgentStore, session_id: str
) -> None:
    assert store.latest_session_checkpoint(session_id) is None


def test_append_then_read_back_is_field_for_field_identical(
    store: AgentStore, session_id: str
) -> None:
    written = store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=4,
        source_message_count=4,
        content=SUMMARY,
        reported_model="grok-4.6",
    )
    read_back = store.latest_session_checkpoint(session_id)

    assert read_back == written
    assert written.content == SUMMARY
    assert written.content_hash == checkpoint_content_hash(SUMMARY)
    assert written.through_ordinal == 4
    assert written.source_message_count == 4
    assert written.reported_model == "grok-4.6"
    assert written.run_id is None


def test_content_is_stored_stripped_and_hashed_over_the_stored_text(
    store: AgentStore, session_id: str
) -> None:
    written = store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=4,
        source_message_count=4,
        content=f"\n\n{SUMMARY}\n  ",
    )
    # 落库的是清洗后的文本，哈希必须算在真正落库的那份上，
    # 否则注入端重新校验时对不上。
    assert written.content == SUMMARY
    assert written.content_hash == checkpoint_content_hash(written.content)


def test_checkpoints_only_move_forward(store: AgentStore, session_id: str) -> None:
    store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=4,
        source_message_count=4,
        content=SUMMARY,
    )

    # 同一段历史不许再折叠一次——否则同一批消息会被算进摘要两遍。
    with pytest.raises(AgentStoreConflict):
        store.append_session_checkpoint(
            session_id,
            run_id=None,
            through_ordinal=4,
            source_message_count=4,
            content="重复折叠",
        )
    with pytest.raises(AgentStoreConflict):
        store.append_session_checkpoint(
            session_id,
            run_id=None,
            through_ordinal=2,
            source_message_count=2,
            content="往回折叠",
        )

    later = store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=6,
        source_message_count=6,
        content="更晚的一份摘要",
    )
    assert store.latest_session_checkpoint(session_id) == later


def test_blank_and_too_small_checkpoints_are_rejected(
    store: AgentStore, session_id: str
) -> None:
    for content, ordinal, count in (
        ("   ", 4, 4),
        ("有内容但折叠得太少", 1, 2),
        ("有内容但折叠得太少", 4, 1),
    ):
        with pytest.raises(ValueError):
            store.append_session_checkpoint(
                session_id,
                run_id=None,
                through_ordinal=ordinal,
                source_message_count=count,
                content=content,
            )
    assert store.latest_session_checkpoint(session_id) is None


def _turn(store: AgentStore, session_id: str, *, index: int) -> None:
    """走一轮完整问答，产生 user / assistant 两条消息。"""
    run, _, _ = store.create_run(
        session_id=session_id,
        message=f"第 {index} 问",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key=f"turn-{index:04d}",
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Starting provider")
    store.transition_run(run.run_id, RunStatus.RUNNING, detail="Provider streaming")
    store.complete_run(
        run.run_id,
        message_id=f"message-{index:04d}-abcdef",
        content=f"第 {index} 答",
        finish_reason="stop",
        usage=None,
    )


def test_history_without_a_checkpoint_is_unchanged(
    store: AgentStore, session_id: str
) -> None:
    """没有摘要的会话（绝大多数）必须与从前逐字一致，不多一条系统消息。"""
    for index in (1, 2, 3):
        _turn(store, session_id, index=index)

    history = store.provider_history(session_id)

    assert all(item["role"] in {"user", "assistant"} for item in history)
    assert [item["content"] for item in history] == [
        "第 1 问",
        "第 1 答",
        "第 2 问",
        "第 2 答",
        "第 3 问",
        "第 3 答",
    ]


def test_folded_history_is_replaced_by_the_summary_not_dropped(
    store: AgentStore, session_id: str
) -> None:
    """§102 第 14 条：超窗要折叠，不是把最前面的消息删掉。"""
    for index in (1, 2, 3):
        _turn(store, session_id, index=index)
    # 前两轮（序号 1–4）折进摘要，第 3 轮原样留着。
    store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=4,
        source_message_count=4,
        content=SUMMARY,
    )

    history = store.provider_history(session_id)

    assert history[0]["role"] == "system"
    assert history[0]["content"] == render_checkpoint_block(
        through_ordinal=4, content=SUMMARY
    )
    # 被折叠的四条不再逐条重发，但它们的内容通过摘要仍然在场。
    assert [item["content"] for item in history[1:]] == ["第 3 问", "第 3 答"]
    assert "试过但不行的做法" in history[0]["content"]


def test_a_later_checkpoint_supersedes_the_earlier_one(
    store: AgentStore, session_id: str
) -> None:
    for index in (1, 2, 3):
        _turn(store, session_id, index=index)
    store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=2,
        source_message_count=2,
        content="早的一份",
    )
    store.append_session_checkpoint(
        session_id,
        run_id=None,
        through_ordinal=4,
        source_message_count=4,
        content=SUMMARY,
    )

    history = store.provider_history(session_id)

    # 只用最新那份，且窗口按最新那份往后切。
    assert history[0]["content"] == render_checkpoint_block(
        through_ordinal=4, content=SUMMARY
    )
    assert "早的一份" not in history[0]["content"]
    assert [item["content"] for item in history[1:]] == ["第 3 问", "第 3 答"]


def test_unknown_session_is_reported_as_not_found(store: AgentStore) -> None:
    with pytest.raises(AgentStoreNotFound):
        store.latest_session_checkpoint("session_does_not_exist")
    with pytest.raises(AgentStoreNotFound):
        store.append_session_checkpoint(
            "session_does_not_exist",
            run_id=None,
            through_ordinal=4,
            source_message_count=4,
            content=SUMMARY,
        )
