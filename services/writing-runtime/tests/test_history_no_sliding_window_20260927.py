"""对话历史不再按条数截（9-27，Reasonix 档案 7.1）：压缩线以下只追加，前缀才稳定。

以前只取最近 64 条：聊满 32 个来回还没到压缩线时，窗口每轮往后滑一格，整段历史每轮都换开头、提示缓存全不命中。
"""

from __future__ import annotations

import json
from pathlib import Path

from test_agent_store import FINGERPRINT, create_run, create_session, initialized_store

from zhimeng_runtime.cache_shape import compare_shapes, request_shape
from zhimeng_runtime.model_context import CONTEXT_WINDOW_SPECS


def _append_messages(store, session, run, contents: list[str]) -> None:
    with store.database.connection() as connection:
        template_row = connection.execute(
            "SELECT payload_json FROM activity WHERE run_id = ? AND kind = 'message'",
            (run.run_id,),
        ).fetchone()
        template = json.loads(str(template_row["payload_json"]))
        start = int(
            connection.execute(
                "SELECT last_message_ordinal FROM execution_session WHERE id = ?",
                (session.session_id,),
            ).fetchone()[0]
        )
        sequence = int(
            connection.execute(
                "SELECT MAX(sequence) FROM activity WHERE run_id = ?", (run.run_id,)
            ).fetchone()[0]
        )
        for offset, content in enumerate(contents, 1):
            ordinal = start + offset
            sequence += 1
            role = "user" if ordinal % 2 else "assistant"
            activity_id = f"activity-window-{ordinal:05d}"
            template["activity_id"] = activity_id
            template["sequence"] = sequence
            template["payload"]["message_id"] = f"message-window-{ordinal:05d}"
            template["payload"]["content"] = content
            template["payload"]["role"] = role
            connection.execute(
                """
                INSERT INTO activity(
                    id, session_id, run_id, sequence, kind, payload_json,
                    project_revision, project_fingerprint, correlation_id,
                    causation_id, created_at
                ) VALUES (?, ?, ?, ?, 'message', ?, 1, ?, ?, NULL, ?)
                """,
                (
                    activity_id,
                    session.session_id,
                    run.run_id,
                    sequence,
                    json.dumps(template, ensure_ascii=False),
                    FINGERPRINT,
                    run.correlation_id,
                    run.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO session_message(
                    activity_id, session_id, run_id, message_id, ordinal,
                    role, format, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'plain', ?)
                """,
                (
                    activity_id,
                    session.session_id,
                    run.run_id,
                    f"message-window-{ordinal:05d}",
                    ordinal,
                    role,
                    run.created_at.isoformat(),
                ),
            )
        connection.execute(
            "UPDATE execution_session SET last_message_ordinal = ? WHERE id = ?",
            (start + len(contents), session.session_id),
        )


def test_a_long_chat_of_short_messages_keeps_every_message_since_the_last_summary(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    _append_messages(store, session, run, [f"第 {index} 句" for index in range(100)])

    history = store.provider_history(session.session_id)

    contents = [item["content"] for item in history]
    assert contents[-100:] == [f"第 {index} 句" for index in range(100)]
    assert len(history) > 64


def test_one_more_turn_only_adds_to_the_end_of_what_the_model_saw_last_time(tmp_path: Path) -> None:
    # 过了 64 条以后再聊一轮：上一次发出去的历史整段原样留在这一次开头（以前第一条被挤掉，从头就对不上）。
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    _append_messages(store, session, run, [f"第 {index} 句" for index in range(70)])
    before = store.provider_history(session.session_id)
    _append_messages(store, session, run, ["再问一句", "再答一句"])
    after = store.provider_history(session.session_id)

    assert compare_shapes(request_shape(before), request_shape(after))["reason"] == "append_only"


def test_the_character_cap_still_drops_the_oldest_when_something_huge_was_pasted(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    _append_messages(store, session, run, ["老" * 50, "中" * 50, "新" * 50])

    history = store.provider_history(session.session_id, max_characters=120)

    assert [item["content"] for item in history] == ["中" * 50, "新" * 50]


def test_the_history_cap_is_twice_the_summary_line_for_each_window_tier() -> None:
    for spec in CONTEXT_WINDOW_SPECS.values():
        assert spec.history_max_characters == 2 * spec.checkpoint_trigger_characters
