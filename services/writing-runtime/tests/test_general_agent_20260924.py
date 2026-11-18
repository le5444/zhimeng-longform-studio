# -*- coding: utf-8 -*-
"""2026-09-24「普通 Agent」整改的回归：章节认错、资产索引白名单、并行工具调用。

审阅依据：《全量代码审阅-20260922.md》2.2 / 2.9 与 T18。
"""
from __future__ import annotations

from pathlib import Path

from zhimeng_runtime.project_asset_service import ProjectAssetService
from zhimeng_runtime.project_reader import (
    WRITING_CONTEXT_FILE_KINDS,
    classify_project_file_kind,
)


def test_chapter_named_after_a_managed_chain_keyword_is_still_prose() -> None:
    """A0 §1.1 那张表：章名带「交接 / 工作包 / 健康 / 审稿 / 门禁 / 上下文」照样是正文。"""
    prose = [
        "05_正文内容/第015章_交接.md",
        "05_正文内容/第009章_工作包.md",
        "05_正文内容/第022章_健康的代价.md",
        "05_正文内容/第007章_审稿风波.md",
        "05_正文内容/第040章_门禁之夜.md",
        "05_正文内容/第003章_上下文.md",
        "05_正文内容/第009章_试剑.md",
        "05_正文内容/第一卷/CH012_状态同步.md",
        "05_正文内容/第一卷/001_回填.md",
        "正文/第001章 校验.md",
        "正文/第一卷/第002章_写回.md",
        "05_正文内容/第088章_健康检查.txt",
    ]
    for path in prose:
        assert classify_project_file_kind(path) == "prose", path


def test_machine_companions_beside_chapters_are_still_not_prose() -> None:
    """带章号的控制卡、审稿报告、ai_smell 副本不是正文；章纲 / 细纲仍是 document。"""
    assert classify_project_file_kind("05_正文内容/第009章_控制卡.md") == "control"
    assert classify_project_file_kind("05_正文内容/第一卷/CH001/CH001_审稿报告.md") == "review"
    assert classify_project_file_kind("05_正文内容/第一卷/CH001/CH001_ai_smell.md") == "review"
    assert classify_project_file_kind("05_正文内容/第一卷/CH001/CH001_draft_body_cleanliness.md") == "review"
    assert classify_project_file_kind("05_正文内容/第005章_章纲.md") == "document"
    assert classify_project_file_kind("05_正文内容/第005章_摘要.md") == "document"
    # 正文夹之外、不带章号的交接说明：读书工具现在也读得到。
    assert classify_project_file_kind("08_素材库/交接说明.md") == "handoff"
    assert "handoff" in WRITING_CONTEXT_FILE_KINDS


def test_asset_index_no_longer_requires_numbered_folders(tmp_path: Path) -> None:
    """资产索引不再只认 01–09 编号夹（审阅 2.9，也是新书夹结构的前提）。"""
    book = tmp_path / "书"
    (book / "设定").mkdir(parents=True)
    (book / "大纲").mkdir()
    (book / "素材").mkdir()
    (book / "正文").mkdir()
    (book / "设定" / "人物-柳青烟.md").write_text("# 柳青烟\n\n青云宗弟子。\n", encoding="utf-8")
    (book / "大纲" / "总纲.md").write_text("# 总纲\n\n三卷。\n", encoding="utf-8")
    (book / "素材" / "灵感.md").write_text("# 灵感\n\n一个梦。\n", encoding="utf-8")
    (book / "正文" / "第001章 开局.md").write_text("# 第001章 开局\n\n正文。\n", encoding="utf-8")
    (book / "这本书.md").write_text("# 这本书\n\n作者的一句想法。\n", encoding="utf-8")

    index = ProjectAssetService(state_root=tmp_path / "state").rebuild(
        project_id="project-test", project_root=book
    )
    paths = sorted(item["relative_path"] for item in index["index"]["assets"])
    assert "设定/人物-柳青烟.md" in paths
    assert "大纲/总纲.md" in paths
    assert "素材/灵感.md" in paths
    assert "这本书.md" in paths
    # 正文夹照旧不进资产索引。
    assert not any(path.startswith("正文/") for path in paths)


def test_provider_requests_no_longer_pin_parallel_tool_calls_off() -> None:
    """T18：不再写死 parallel_tool_calls=False，模型一轮能发多个工具调用。"""
    source = Path(__file__).resolve().parents[1] / "src" / "zhimeng_runtime" / "agent_provider.py"
    assert "parallel_tool_calls" not in source.read_text(encoding="utf-8")
