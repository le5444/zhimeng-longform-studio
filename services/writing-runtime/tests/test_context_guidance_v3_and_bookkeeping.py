"""2026-09-13 作者反馈的两处后端收口：

1. 【回答要求】第 3 版（context-compiler.v5）点名禁掉「窗口 / 工具次数 / 回填」这类内部说法，
   要求把记账文件里的英文状态词翻成人话；存量 v4 Pack 仍按第 2 版文字逐字节重渲染。
2. 书里的记账文件（总览、修改记录、进度表、写回 / 审稿报告）进上下文时算「参考资料」，
   不再当「作品内容（唯一真值）」；正文、设定、控制卡、人物状态表仍是作品内容。
"""

from __future__ import annotations

from datetime import datetime, timezone

from zhimeng_runtime.context_compiler import CONTEXT_COMPILER_VERSION, compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextPack,
    human_readable_for,
    include_response_guidance_for,
    response_guidance_version_for,
)
from zhimeng_runtime.context_service import project_file_authority

from test_context_compiler import history, request


def _chat_pack(compiler_version: str) -> ContextPack:
    return compile_context_pack(
        context_pack_id=f"context-pack-{compiler_version}",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=request().model_copy(update={"profile": "chat"}),
        history=history(),
        candidates=(),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        compiler_version=compiler_version,
    )


def test_version_helpers_pin_each_compiler_version() -> None:
    assert CONTEXT_COMPILER_VERSION == "context-compiler.v6"
    assert response_guidance_version_for("context-compiler.v2") == 1
    assert response_guidance_version_for("context-compiler.v3") == 2
    assert response_guidance_version_for("context-compiler.v4") == 2
    assert response_guidance_version_for("context-compiler.v5") == 3
    assert response_guidance_version_for("context-compiler.v6") == 0
    assert human_readable_for("context-compiler.v4")
    assert human_readable_for("context-compiler.v5")
    assert human_readable_for("context-compiler.v6")
    assert not human_readable_for("context-compiler.v3")
    assert not include_response_guidance_for("context-compiler.v1")
    assert include_response_guidance_for("context-compiler.v5")
    assert not include_response_guidance_for("context-compiler.v6")


def test_current_chat_pack_has_no_response_guidance() -> None:
    """v6（作者 2026-09-20「都删了吧」）：资料包不再教模型怎么说话、怎么自称。"""
    pack = _chat_pack(CONTEXT_COMPILER_VERSION)
    system_content = pack.exact_provider_messages[0].content

    assert "【回答要求】" not in system_content
    for phrase in ("工程术语", "工具次数", "翻成人话", "称自己为 Agent"):
        assert phrase not in system_content, phrase
    assert "【上下文说明】" in system_content
    assert not system_content.startswith("{")
    restored = ContextPack.model_validate_json(pack.model_dump_json())
    assert restored.compiler_version == "context-compiler.v6"


def test_v5_chat_pack_bans_internal_phrases_and_translates_status_words() -> None:
    pack = _chat_pack("context-compiler.v5")
    system_content = pack.exact_provider_messages[0].content

    assert "【回答要求】" in system_content
    assert "工程术语" in system_content
    for phrase in ("工具次数", "回填", "阻断项", "ready", "翻成人话", "Agent", "这本书"):
        assert phrase in system_content, phrase
    # 还是人话版渲染，不是 JSON
    assert not system_content.startswith("{")


def test_v4_pack_keeps_second_version_guidance_byte_for_byte() -> None:
    old_pack = _chat_pack("context-compiler.v4")
    restored = ContextPack.model_validate_json(old_pack.model_dump_json())
    system_content = restored.exact_provider_messages[0].content

    assert restored.compiler_version == "context-compiler.v4"
    assert "工程术语" in system_content
    assert "工具次数" not in system_content
    assert "翻成人话" not in system_content


def test_bookkeeping_files_are_reference_not_book_truth() -> None:
    truth = [
        {"kind": "prose", "relative_path": "05_正文内容/第一卷文件夹/001-050章/004_玉佩上的青云.md"},
        {"kind": "document", "relative_path": "03_人物设定/主要人物.md"},
        {"kind": "document", "relative_path": "02_世界观设定/境界体系.md"},
        {"kind": "control", "relative_path": "05_正文内容/第一卷文件夹/章纲/第005章_控制卡.md"},
        # 带「状态」二字的真设定不能被当成记账
        {"kind": "status", "relative_path": "03_人物设定/人物状态表.md"},
    ]
    reference = [
        {"kind": "document", "relative_path": "00_项目总览.md"},
        {"kind": "document", "relative_path": "05_正文内容/正文总览.md"},
        {"kind": "document", "relative_path": "05_正文内容/修改记录.md"},
        {"kind": "document", "relative_path": "05_正文内容/每日写作进度表.md"},
        {"kind": "status", "relative_path": "07_运营数据/写回同步记录.md"},
        {"kind": "review", "relative_path": "05_正文内容/第004章_审稿报告.md"},
        {"kind": "structured_state", "relative_path": "07_运营数据/state.json"},
        {"kind": "handoff", "relative_path": "09_完本复盘/交接包.md"},
    ]
    for record in truth:
        assert project_file_authority(record) is ContextAuthority.PROJECT_TRUTH, record
    for record in reference:
        assert project_file_authority(record) is ContextAuthority.REFERENCE, record
