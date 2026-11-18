"""下一轮照原样带上上一轮发给模型的那一段（9-28，ZCode 档案 12.2 / 总指令 8.3 第 6 件）。

以前下一轮的历史里只有作者的话和模型最后那段回答：上一轮读过什么、改过什么（工具调用和结果）全没了，
9-28 跨轮考题 before1 里第 2 章后面几轮又读了 11 次，问「改过哪些文件」要 8–9 步重新找。
现在一轮答完把「作者的话后面发给模型的那一段 + 回答」存下来（``run_transcript``），下一轮原样放回：
下一轮第一步的开头和上一轮最后一步一字不差（``cache_shape`` = append_only）。
一起改的：grok 那条线路（Responses）和 Anthropic 线只把开头连着的 system 上提，中途的附言留在原位；
「开着哪个文件」「环境变了」「说明文件后来改过」这几条附言和历史里说过的一样就不再说；整理对话按估算的 token 算，整段往来都算进去。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from test_context_api import _write_project
from test_correction_p0_20260926 import _app
from test_open_folder_chat_20260927 import _bound_turn, _open_folder
from test_said_but_not_done_20260927 import StepProvider
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider
from zhimeng_runtime.agent_service import _changed_open_folder_note, _history_weights
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.model_context import resolve_context_window_spec

CHAPTER = "05_正文内容/第一卷/CH001_定稿.md"


def _turn(client, session_id, project, message, key):
    return _bound_turn(
        client,
        session_id,
        _open_folder(client, project),
        {"message": message, "active_chapter_id": None, "writer_mode": "author_review_change_proposal"},
        key,
    )


def test_the_next_turn_starts_with_the_whole_previous_exchange(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = StepProvider(
        [
            ("read_file", {"path": CHAPTER}),
            "读完了，第一章讲的是一封匿名信。",
            "好，改哪一句？",
        ]
    )
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "改稿"}
        ).json()
        first = _turn(client, session["session_id"], project, "读一下第一章", "replay-0001")
        second = _turn(client, session["session_id"], project, "改一句", "replay-0002")
    assert first["status"] == "succeeded", first
    assert second["status"] == "succeeded", second

    last_of_first, first_of_second = provider.requests[1], provider.requests[2]
    # 上一轮最后一步整段原样在前——读到的原文也在（以前下一轮只剩最后那段回答）。
    assert any(message.get("role") == "tool" for message in last_of_first)
    assert first_of_second[: len(last_of_first)] == last_of_first
    assert first_of_second[len(last_of_first):] == [
        {"role": "assistant", "content": "读完了，第一章讲的是一封匿名信。"},
        {"role": "user", "content": "改一句"},
    ]
    with SqliteStore(tmp_path / "runtime.sqlite3").connection() as connection:
        rows = connection.execute(
            "SELECT run_id, payload_json FROM run_event WHERE event_type = 'model_turn_started' ORDER BY created_at"
        ).fetchall()
        saved = connection.execute(
            "SELECT run_id, messages_json, estimated_tokens FROM run_transcript ORDER BY created_at"
        ).fetchall()
    started = [(row["run_id"], json.loads(row["payload_json"])) for row in rows]
    opening_step = next(payload for run_id, payload in started if run_id == second["run_id"] and payload["turn"] == 1)
    assert opening_step["cache_shape"]["reason"] == "append_only"
    # 两轮都存了；第一轮那段 = 工具调用 + 结果 + 回答，估算的 token 把读到的原文也算进去。
    assert [row["run_id"] for row in saved] == [first["run_id"], second["run_id"]]
    first_saved = json.loads(saved[0]["messages_json"])
    assert [message["role"] for message in first_saved] == ["assistant", "tool", "assistant"]
    assert saved[0]["estimated_tokens"] > len("读完了，第一章讲的是一封匿名信。")


def test_mid_conversation_notes_stay_in_place_on_the_responses_and_anthropic_lines() -> None:
    messages = [
        {"role": "system", "content": "系统：Windows"},
        {"role": "system", "content": "【此前对话的摘要】……"},
        {"role": "user", "content": "改一句"},
        {"role": "system", "content": "用户这时在编辑器里开着：a.md"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "Bash:256", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "Bash:256", "content": "{}"},
    ]
    instructions, items = OpenAICompatibleAgentProvider._build_responses_input(messages)
    # 开头连着的两条上提；中途那条附言照原位置变成 user 消息，附言变了 instructions 也不变。
    assert instructions == "系统：Windows\n\n【此前对话的摘要】……"
    assert items[1] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "用户这时在编辑器里开着：a.md"}],
    }
    provider = OpenAICompatibleAgentProvider(
        base_url="https://provider.test/v1",
        model="claude-test",
        api_key="sk-ant-test",
        credential_reference="env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        timeout_seconds=30,
        max_tokens=1024,
        temperature=0.2,
        wire_format="anthropic_messages",
        client=httpx.AsyncClient(),
    )
    system, converted = provider._build_anthropic_input(messages)
    assert system == "系统：Windows\n\n【此前对话的摘要】……"
    assert converted[1] == {"role": "user", "content": "用户这时在编辑器里开着：a.md"}
    # 别家的工具调用 id（Kimi 的带冒号）换成 Anthropic 认的字符，调用和结果对得上。
    assert converted[2]["content"][0]["id"] == "Bash_256"
    assert converted[3]["content"][0]["tool_use_id"] == "Bash_256"


def test_notes_already_said_in_the_replayed_history_are_not_repeated() -> None:
    note = {"role": "system", "content": "用户这时在编辑器里开着：a.md"}
    history = [{"role": "user", "content": "改"}, dict(note), {"role": "assistant", "content": "好"}]
    assert _changed_open_folder_note([dict(note)], history) == []
    other = {"role": "system", "content": "用户这时在编辑器里开着：b.md"}
    assert _changed_open_folder_note([other], history) == [other]
    # 上一轮说开着，这一轮什么都没开：说一声没开着；说过没开着就不再说。
    nothing = {"role": "system", "content": "用户这时没开着文件。"}
    assert _changed_open_folder_note([], history) == [nothing]
    assert _changed_open_folder_note([], [*history, nothing]) == []
    # 前几轮没存往来（历史里没有附言）：照旧每轮都说。
    assert _changed_open_folder_note([dict(note)], []) == [note]

    started = datetime(2026, 9, 28, 2, tzinfo=UTC)
    facts = environment_context.EnvironmentFacts(folder=None, started_at=started, tier="完全访问")
    next_day = started + timedelta(days=1)
    first = environment_context.update_since(facts, now=next_day, tier="完全访问", stated=[])
    assert first == environment_context.update_message(facts, now=next_day, tier="完全访问")
    assert environment_context.update_since(facts, now=next_day, tier="完全访问", stated=[str(first["content"])]) is None
    # 挡位换成只读又换回来：上次说的是只读，现在照现在的说。
    said = environment_context.update_since(facts, now=started, tier="只读", stated=[])
    back = environment_context.update_since(facts, now=started, tier="完全访问", stated=[str(said["content"])])
    assert back == {"role": "system", "content": "挡位：完全访问"}
    assert environment_context.is_update_content(str(said["content"]))
    assert not environment_context.is_update_content(str(environment_context.opening_message(facts, home=Path("C:/"))["content"]))


def test_folding_counts_the_whole_replayed_exchange() -> None:
    pending = [
        SimpleNamespace(role="user", content="读第一章", run_id="run-1"),
        SimpleNamespace(role="assistant", content="写了一半", run_id="run-1"),
        SimpleNamespace(role="assistant", content="写完了", run_id="run-1"),
        SimpleNamespace(role="user", content="好", run_id="run-2"),
        SimpleNamespace(role="assistant", content="嗯", run_id="run-2"),
    ]
    weights = _history_weights(pending, {"run-1": 5_000})
    # 存过往来的那一轮：整段算在最后一条回答上，前面那条回答算 0；没存过的照旧按文字估。
    assert weights[1] == 0 and weights[2] == 5_000
    assert weights[0] > 0 and weights[3] > 0 and weights[4] > 0
    spec = resolve_context_window_spec("256k")
    assert spec.trigger_tokens == 100_000 and spec.keep_recent_tokens == 15_000
    assert resolve_context_window_spec("1m").trigger_tokens == 400_000
    assert spec.history_max_tokens == 2 * spec.trigger_tokens
