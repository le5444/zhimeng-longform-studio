"""压缩重做第一刀（9-28，总指令 8.3 第 5 件）：摘要按小节截、缺节认得出、程序在摘要后面附工具事实和作者原话。

9-28 压缩考题 base1（隔离实例 grok-4.6）量出来的：摘要把「倒叙否掉、照顺叙」记成「顺叙，从验宗那天写起」，
「当前目标」写成了「整理对话记录」；以前超长从末尾截，第一个丢的是「还没做、接下来要做的事」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.compaction_facts import (
    AUTHOR_MESSAGE_MAX_CHARACTERS,
    author_words_block,
    tool_facts_block,
)
from zhimeng_runtime.session_checkpoint import (
    CHECKPOINT_AUTHOR_WORDS_TITLE,
    CHECKPOINT_FACTS_TITLE,
    CHECKPOINT_SECTION_TITLES,
    build_checkpoint_request_messages,
    compose_checkpoint_content,
    missing_checkpoint_sections,
    model_summary_part,
    normalize_checkpoint_text,
)

from test_agent_service import FINGERPRINT, PROJECT_ID, TurnScriptedProvider, build_service, wait_terminal

NEXT_STEPS = "先写第 5 章《哑姑的来历》，写完再回头把第 3 章后半段的节奏收紧。"


def _summary(**bodies: str) -> str:
    return "\n".join(f"【{title}】{bodies.get(title, '无')}" for title in CHECKPOINT_SECTION_TITLES)


def test_an_overlong_summary_is_trimmed_by_section_and_keeps_the_next_steps() -> None:
    text = _summary(
        当前目标="写完第一卷。",
        已经做完的事="\n".join(f"- 总结了第 {index} 份文件的大概内容。" for index in range(200)),
        试过但不行的做法="第 3 章倒叙，节奏乱，否掉了。",
        **{"还没做、接下来要做的事": NEXT_STEPS},
    )
    fitted = normalize_checkpoint_text(text, max_characters=1_200)
    assert fitted is not None and len(fitted) <= 1_200
    # 以前从末尾截，最先丢的就是这一节。
    assert NEXT_STEPS in fitted
    assert "【当前目标】写完第一卷。" in fitted
    assert "第 3 章倒叙，节奏乱，否掉了。" in fitted
    assert "（这一节太长，后面的省略了）" in fitted
    assert missing_checkpoint_sections(fitted) == []


def test_protected_sections_are_trimmed_only_after_everything_else() -> None:
    long_steps = "\n".join(f"- 第 {index} 步：接着写。" for index in range(300))
    text = _summary(已经定下的事="剑叫断水。", **{"还没做、接下来要做的事": long_steps})
    fitted = normalize_checkpoint_text(text, max_characters=900)
    assert fitted is not None and len(fitted) <= 900
    assert "【已经定下的事】剑叫断水。" in fitted
    assert "- 第 0 步：接着写。" in fitted
    assert fitted.endswith("（这一节太长，后面的省略了）")


def test_a_summary_without_section_titles_is_still_cut_from_the_end() -> None:
    fitted = normalize_checkpoint_text("乙" * 5_000, max_characters=300)
    assert fitted is not None and len(fitted) <= 300
    assert fitted.endswith("（摘要过长，末尾已截断）")


def test_missing_sections_are_listed_in_order() -> None:
    assert missing_checkpoint_sections("【当前目标】写完。\n【已经定下的事】无") == [
        title for title in CHECKPOINT_SECTION_TITLES if title not in ("当前目标", "已经定下的事")
    ]
    assert missing_checkpoint_sections(_summary()) == []


def test_the_retry_note_goes_into_the_same_user_message() -> None:
    request = build_checkpoint_request_messages(
        previous_checkpoint=None,
        messages=[("user", "你好"), ("assistant", "你好")],
        retry_missing=["试过但不行的做法", "还没做、接下来要做的事"],
    )
    assert len(request) == 2
    user = request[-1]["content"]
    assert user.startswith("【上一份摘要】")
    assert "上一次整理少了这几节：【试过但不行的做法】、【还没做、接下来要做的事】" in user
    # 系统说明里说清楚后面会附什么，摘要里不用逐字抄。
    assert "系统会另外原样附在摘要后面" in request[0]["content"]


def test_only_the_model_written_part_goes_back_to_the_model() -> None:
    summary = _summary(当前目标="写完第 3 章。")
    content = compose_checkpoint_content(
        summary,
        CHECKPOINT_FACTS_TITLE + "\n改过的文件：\n- a.md：改",
        None,
        CHECKPOINT_AUTHOR_WORDS_TITLE + "\n- 剑叫断水",
    )
    assert content.startswith(summary)
    assert model_summary_part(content) == summary
    assert model_summary_part(summary) == summary


def _call(tool_name: str, arguments: dict, status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, status=status, arguments=arguments, output={})


def test_tool_facts_list_files_by_their_final_state() -> None:
    change_sets = [
        {"status": "applied", "changes": [{"relative_path": "正文/002.md", "operation": "update"}]},
        {"status": "applied", "changes": [{"relative_path": "正文/002.md", "operation": "update"}]},
        {"status": "reviewing", "changes": [{"relative_path": "正文/003.md", "operation": "update"}]},
        {"status": "rolled_back", "changes": [{"relative_path": "正文/004.md", "operation": "update"}]},
        {"status": "applied", "changes": [{"relative_path": "草稿.md", "operation": "move", "target_relative_path": "旧稿/草稿.md"}]},
        {"status": "applied", "changes": [{"relative_path": "新章.md", "operation": "create"}]},
    ]
    file_changes = [{"operation": "update", "path": "C:/Users/me/Desktop/笔记.txt", "target_path": None, "undone_at": "2026-09-28"}]
    block = tool_facts_block(change_sets=change_sets, file_changes=file_changes)
    assert block is not None and block.startswith(CHECKPOINT_FACTS_TITLE)
    assert "- 正文/002.md：改 ×2" in block
    assert "- 正文/003.md：改（提了修改卡，还没保存）" in block
    assert "- 正文/004.md：改（后来撤回了）" in block
    assert "- 草稿.md：挪到 旧稿/草稿.md" in block
    assert "- 新章.md：新建" in block
    assert "- C:/Users/me/Desktop/笔记.txt：改（后来撤回了）" in block


def test_tool_facts_carry_commands_memory_the_latest_plan_and_skills() -> None:
    calls = [
        _call("computer_run_command", {"command": "git status"}),
        _call("computer_run_command", {"command": "rm -rf build"}, status="failed"),
        _call("memory_write", {"scope": "user", "text": "沈青云那把佩剑叫「断水」。"}),
        _call("plan_update", {"items": [{"content": "旧计划", "status": "pending"}]}),
        _call("plan_update", {"items": [
            {"content": "写第 5 章", "status": "in_progress"},
            {"content": "收紧第 3 章后半段", "status": "pending"},
        ]}),
        _call("skill_read_file", {"skill": "小说创作", "path": "SKILL.md"}),
        _call("skill_read_file", {"skill": "小说创作", "path": "参考.md"}),
    ]
    block = tool_facts_block(tool_calls=calls)
    assert block is not None
    assert "- git status" in block and "rm -rf build" not in block
    assert "- （用户记忆）沈青云那把佩剑叫「断水」。" in block
    assert "- [进行中] 写第 5 章" in block and "- [没开始] 收紧第 3 章后半段" in block
    assert "旧计划" not in block
    assert "读过的技能：小说创作" in block
    assert block.count("小说创作") == 1


def test_no_facts_means_no_block() -> None:
    assert tool_facts_block() is None
    assert tool_facts_block(tool_calls=[_call("read_file", {"path": "a.md"})]) is None


def test_plan_checkpoint_uses_confirmed_output_and_keeps_failed_updates_separate() -> None:
    saved = _call("plan_update", {"items": [{"content": "请求里的旧进度", "status": "completed"}]})
    saved.output = {"schema": "zhimeng.tool-result.plan-update.v1", "items": [
        {"content": "检查采购表", "status": "in_progress"},
        {"content": "核对合计", "status": "pending"},
    ]}
    broken = _call("plan_update", {"items": [{"content": "损坏结果不能从参数补成成功", "status": "completed"}]})
    broken.output = {"items": "invalid"}
    calls = [saved, broken, *[
        _call("plan_update", {"items": [{"content": "失败的完成声明", "status": "completed"}]}, status=status)
        for status in ("failed", "cancelled", "running")
    ]]
    block = tool_facts_block(tool_calls=calls)
    assert block is not None
    assert "[进行中] 检查采购表" in block and "[没开始] 核对合计" in block
    assert "状态由 Agent 标记，未经独立核验" in block
    assert "请求里的旧进度" not in block and "失败的完成声明" not in block
    assert "损坏结果" not in block


def test_author_words_keep_the_newest_and_say_how_many_older_ones_were_left_out() -> None:
    texts = [f"第 {index} 句交代。" for index in range(1, 41)]
    block = author_words_block(texts, max_characters=60)
    assert block is not None and block.startswith(CHECKPOINT_AUTHOR_WORDS_TITLE)
    assert "- 第 40 句交代。" in block
    assert "- 第 1 句交代。" not in block
    assert "（更早的" in block and "条没放进来" in block
    # 放进来的按时间排。
    assert block.index("第 39 句") < block.index("第 40 句")


def test_a_long_pasted_message_keeps_only_its_head_and_tail() -> None:
    pasted = "开头这句是交代。" + "原文" * 3_000 + "结尾这句也是交代。"
    block = author_words_block([pasted], max_characters=10_000)
    assert block is not None
    assert "开头这句是交代。" in block and "结尾这句也是交代。" in block
    assert f"（这一条共 {len(pasted):,} 字，中间省略）" in block
    assert len(block) < AUTHOR_MESSAGE_MAX_CHARACTERS + 200


def test_the_store_returns_only_records_of_the_folded_turns(tmp_path: Path) -> None:
    """``compaction_source_records`` 只取到第 N 条为止那几轮的记录；修改记录按表里现在的状态。"""

    async def scenario() -> None:
        provider = TurnScriptedProvider(
            [[ProviderTextDelta(f"第 {index} 答"), ProviderCompleted("stop", None)] for index in (1, 2)]
        )
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        run_ids = []
        for index in (1, 2):
            result = await service.start_run(
                session_id=session.session_id,
                message=f"第 {index} 问",
                expected_project_revision=1,
                expected_project_fingerprint=FINGERPRINT,
                active_chapter_id="chapter-001",
                idempotency_key=f"records-{index}",
            )
            await wait_terminal(service, result.run.run_id)
            run_ids.append(result.run.run_id)
        for _ in range(500):
            if not service._tasks:
                break
            await asyncio.sleep(0.01)

        store = service.store
        with store.database.connection() as connection:
            binding = connection.execute("SELECT id FROM project_binding LIMIT 1").fetchone()[0]
            for number, run_id in enumerate(run_ids, 1):
                connection.execute(
                    "INSERT INTO tool_call(id, run_id, tool_name, tool_version, status, request_hash, input_json, output_json, started_at) "
                    "VALUES (?, ?, 'plan_update', '1', 'succeeded', ?, ?, '{}', ?)",
                    (f"call-{number}", run_id, "0" * 64,
                     json.dumps({"items": [{"content": f"第 {number} 轮的计划", "status": "pending"}]}, ensure_ascii=False),
                     f"2026-09-28T00:00:0{number}+00:00"),
                )
                connection.execute(
                    "INSERT INTO change_set(id, project_binding_id, status, base_revision, base_fingerprint, review_revision, payload_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, 0, ?, ?, ?)",
                    (f"changeset-{number}", binding, "rolled_back" if number == 1 else "applied", FINGERPRINT,
                     json.dumps({"source_run_id": run_id, "title": f"改第 {number} 处",
                                 "changes": [{"relative_path": f"第{number}章.md", "operation": "update"}]}, ensure_ascii=False),
                     f"2026-09-28T00:00:0{number}+00:00", f"2026-09-28T00:00:0{number}+00:00"),
                )
                connection.execute(
                    "INSERT INTO file_change(change_id, seq, session_id, run_id, tool_call_id, title, operation, path, created_at) "
                    "VALUES (?, 0, ?, ?, ?, '写桌面', 'create', ?, ?)",
                    (f"fc-{number}", session.session_id, run_id, f"call-{number}", f"C:/桌面/{number}.txt",
                     f"2026-09-28T00:00:0{number}+00:00"),
                )
            connection.commit()

        messages = store.list_messages(session.session_id)
        first_turn_end = [message.ordinal for message in messages if message.run_id == run_ids[0]][-1]
        calls, change_sets, file_changes, author_texts = store.compaction_source_records(
            session.session_id, through_ordinal=first_turn_end
        )
        assert [call.tool_name for call in calls] == ["plan_update"]
        assert calls[0].arguments["items"][0]["content"] == "第 1 轮的计划"
        assert change_sets == [{"status": "rolled_back", "title": "改第 1 处",
                                "changes": [{"relative_path": "第1章.md", "operation": "update"}]}]
        assert [item["path"] for item in file_changes] == ["C:/桌面/1.txt"]
        assert author_texts == ["第 1 问"]

        everything = store.compaction_source_records(session.session_id, through_ordinal=messages[-1].ordinal)
        assert len(everything[0]) == 2 and len(everything[1]) == 2 and everything[3] == ["第 1 问", "第 2 问"]
        await service.shutdown()

    asyncio.run(scenario())
