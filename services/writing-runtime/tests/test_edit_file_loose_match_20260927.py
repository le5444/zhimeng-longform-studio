"""查找替换原样找不到时放宽了再找、彻底找不到时把对得上的原文贴回（9-27）。

照 Codex seek_sequence 第 4 档、Pi edit-diff、OpenCode 的空白放宽、Cline「找不到就贴回原文」：
中文稿里的 “” ——、全角空格和全角标点，模型抄上下文时常写成半角；段落之间空几行、大纲的缩进也常抄不准。
放宽后仍然只许一处；没改的开头结尾照文件原样保留（作者的 “” 不会被换成模型写的 ""）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_agent_direct_write_tools import _build_runtime, _call
from zhimeng_runtime.agent_tools import AgentToolError, _closest_passage, _loose_replace

CHAPTER = "05_正文内容/第一卷/CH001_正文.md"


def _loose(existing: str, find: str, replace: str, *, replace_all: bool = False) -> tuple[str, int]:
    assert find not in existing, "这些用例测的是原样找不到之后"
    return _loose_replace(existing, find, replace, replace_all=replace_all)


def test_curly_quotes_written_straight_still_match_and_the_unchanged_quotes_stay_curly() -> None:
    existing = "他说：“好的。”然后走了。"
    updated, found = _loose(existing, '他说："好的。"然后', '他说："不行。"然后')
    assert found == 1
    assert updated == "他说：“不行。”然后走了。"


def test_one_dash_for_a_double_em_dash_matches_and_keeps_the_double_dash() -> None:
    updated, found = _loose("他——停住了。", "他—停住了", "他—站住了")
    assert (updated, found) == ("他——站住了。", 1)


def test_full_width_punctuation_and_spaces_written_half_width_match() -> None:
    existing = "她问：“真的吗？”　他点头，没说话。"
    updated, found = _loose(existing, '她问:"真的吗?" 他点头,没说话', '她问:"真的吗?" 他摇头,没说话')
    assert found == 1
    assert updated == "她问：“真的吗？”　他摇头，没说话。"


def test_one_blank_line_less_between_paragraphs_matches_and_keeps_the_blank_lines() -> None:
    existing = "第一段。\n\n第二段。\n\n第三段。\n"
    updated, found = _loose(existing, "第一段。\n第二段。", "第一段。\n第二段改了。")
    assert found == 1
    assert updated == "第一段。\n\n第二段改了。\n\n第三段。\n"


def test_outline_indentation_left_out_matches_and_keeps_the_indentation() -> None:
    existing = "- 甲\n  - 乙\n  - 丙\n"
    updated, found = _loose(existing, "- 乙\n- 丙", "- 乙\n- 丁")
    assert found == 1
    assert updated == "- 甲\n  - 乙\n  - 丁\n"


def test_trailing_newline_in_find_and_replace_leaves_the_paragraph_gap_alone() -> None:
    existing = "他说：“走。”\n\n她笑了。"
    updated, found = _loose(existing, '他说："走。"\n', '他说："停。"\n')
    assert (updated, found) == ("他说：“停。”\n\n她笑了。", 1)


def test_a_changed_edge_newline_is_applied_to_the_file() -> None:
    """find 前面带换行、replace 前面没有：模型要去掉这个换行，文件里挨着的空行一起去掉。"""
    existing = "甲。\n\n“乙。”"
    updated, found = _loose(existing, '\n"乙。"', '"乙。"')
    assert (updated, found) == ("甲。“乙。”", 1)


def test_loose_matches_must_still_be_unique_unless_replace_all() -> None:
    existing = "“走。”他说。\n“走。”她说。\n"
    assert _loose(existing, '"走。"', '"停。"') == (existing, 2)
    updated, found = _loose(existing, '"走。"', '"停。"', replace_all=True)
    assert found == 2
    assert updated == "“停。”他说。\n“停。”她说。\n"


def test_no_loose_match_leaves_the_text_alone() -> None:
    assert _loose("他缓缓走进了院子。", "他慢慢走进院子", "x") == ("他缓缓走进了院子。", 0)


def test_a_huge_single_line_file_does_not_hang() -> None:
    """AGENTS.md：对工具结果跑的东西要线性——几十万字的单行文件不能卡住。"""
    existing = "他说：“好。”" * 30_000 + "最后一句：“到了。”"
    updated, found = _loose(existing, '最后一句："到了。"', '最后一句："还没到。"')
    assert found == 1
    assert updated.endswith("最后一句：“还没到。”")
    where, passage = _closest_passage(existing, "根本没有的一句话，" * 50)
    assert (where, passage) == ("", "")


def test_closest_passage_names_where_the_start_agrees_and_shows_the_file_text() -> None:
    existing = "开头。\n他缓缓走进了院子，抬头看天。\n结尾。"
    where, passage = _closest_passage(existing, "他缓缓走进院子，低头看地")
    assert "开头「他缓缓走进」在第 2 行对得上" in where
    assert "从「院子，低头看地」起对不上" in where
    assert passage.startswith("他缓缓走进了院子，抬头看天。")


def test_closest_passage_uses_whichever_end_agrees_longer() -> None:
    existing = "开头。\n他缓缓走进了院子，抬头看天。\n结尾。"
    where, passage = _closest_passage(existing, "她慢慢进了院子，抬头看天")
    assert "结尾「进了院子，抬头看天」在第 2 行对得上" in where
    assert passage.endswith("他缓缓走进了院子，抬头看天")
    # 两头都不到 5 个字对得上：不瞎指。
    assert _closest_passage(existing, "她走进屋里，低头看地") == ("", "")


def test_edit_file_reports_a_loose_match_and_keeps_the_curly_quotes(
    tmp_path: Path
) -> None:
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path,
        chapter_content="他说：“好的。”然后走了。\n\n她没回头。\n",
        writer_mode="direct_write",
    )
    result = asyncio.run(
        _call(
            executor,
            run,
            "edit_file",
            {"path": CHAPTER, "find": '他说："好的。"然后走了。\n她没回头。', "replace": '他说："不行。"然后走了。\n她没回头。'},
        )
    )
    assert result.output["operation"] == "updated"
    assert result.output["match"] == "loose"
    assert "照文件原样保留" in result.output["match_note"]
    assert chapter.read_text(encoding="utf-8") == "他说：“不行。”然后走了。\n\n她没回头。\n"


def test_edit_file_exact_matches_say_nothing_about_loose_matching(
    tmp_path: Path
) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, writer_mode="direct_write"
    )
    result = asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "第二行。", "replace": "改过的第二行。"}))
    assert result.output["occurrences"] == 1
    assert "match" not in result.output


def test_edit_file_find_missing_pastes_back_the_passage_that_agrees(
    tmp_path: Path
) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path,
        chapter_content="开头。\n他缓缓走进了院子，抬头看天。\n结尾。\n",
        writer_mode="direct_write",
    )
    with pytest.raises(AgentToolError) as caught:
        asyncio.run(_call(executor, run, "edit_file", {"path": CHAPTER, "find": "他缓缓走进院子，低头看地", "replace": "x"}))
    assert caught.value.code == "agent_tool_edit_find_missing"
    message = str(caught.value)
    assert "在第 2 行对得上" in message
    assert "那一段现在是：\n他缓缓走进了院子，抬头看天。" in message

    with pytest.raises(AgentToolError) as nowhere:
        asyncio.run(
            _call(executor, run, "edit_file", {"path": CHAPTER, "find": "完全不相干的一句话", "replace": "x"}, call_id="call-0002")
        )
    assert "开头和结尾也都对不上" in str(nowhere.value)
