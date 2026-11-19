"""会话对话摘要的纯函数：整理请求怎么构造、模型交回的摘要怎么清洗。"""

from __future__ import annotations

import hashlib

import pytest

from zhimeng_runtime.session_checkpoint import (
    CHECKPOINT_SECTION_TITLES,
    CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS,
    build_checkpoint_request_messages,
    checkpoint_content_hash,
    normalize_checkpoint_text,
    render_checkpoint_block,
)


def test_request_carries_previous_checkpoint_and_both_roles_in_order() -> None:
    messages = build_checkpoint_request_messages(
        previous_checkpoint="【当前目标】写完第三章。",
        messages=[
            ("user", "柳青烟这里太冷静了，她该慌。"),
            ("assistant", "已改：加了手抖和一句自我安慰。"),
        ],
        max_characters=1_200,
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    system = messages[0]["content"]
    for title in CHECKPOINT_SECTION_TITLES:
        assert f"【{title}】" in system
    assert "不超过 1200 字" in system
    user = messages[1]["content"]
    assert "【上一份摘要】\n【当前目标】写完第三章。" in user
    assert user.index("作者：柳青烟这里太冷静了") < user.index("写作助手：已改：加了手抖")


def test_request_without_previous_checkpoint_says_none() -> None:
    messages = build_checkpoint_request_messages(
        previous_checkpoint=None,
        messages=[("user", "你好")],
    )
    assert "【上一份摘要】\n无\n" in messages[1]["content"]


def test_overlong_source_message_is_folded_with_an_omission_marker() -> None:
    long_text = "甲" * (CHECKPOINT_SOURCE_MESSAGE_MAX_CHARACTERS + 500)
    messages = build_checkpoint_request_messages(
        previous_checkpoint=None,
        messages=[("assistant", long_text)],
    )
    user = messages[1]["content"]
    assert "（中间省略约 500 字）" in user
    assert len(user) < len(long_text)


@pytest.mark.parametrize(
    "bad_messages",
    [[], [("tool", "x")], [("system", "x")]],
)
def test_request_rejects_empty_or_foreign_roles(bad_messages) -> None:
    with pytest.raises(ValueError):
        build_checkpoint_request_messages(
            previous_checkpoint=None,
            messages=bad_messages,
        )


def test_normalize_collapses_blank_lines_and_rejects_blank_output() -> None:
    assert normalize_checkpoint_text("  \n\n \r\n") is None
    cleaned = normalize_checkpoint_text("【当前目标】\r\n写完。\n\n\n\n【底线】\n不改正文。 ")
    assert cleaned == "【当前目标】\n写完。\n\n【底线】\n不改正文。"


def test_normalize_truncates_overlong_summary_and_marks_it() -> None:
    cleaned = normalize_checkpoint_text("乙" * 5_000, max_characters=300)
    assert cleaned is not None
    assert len(cleaned) <= 300
    assert cleaned.endswith("（摘要过长，末尾已截断）")


def test_normalize_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        normalize_checkpoint_text("x", max_characters=0)


def test_content_hash_is_sha256_of_utf8() -> None:
    content = "【当前目标】写完第三章。"
    assert checkpoint_content_hash(content) == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def test_rendered_block_states_the_ordinal_and_the_authority_rule() -> None:
    block = render_checkpoint_block(through_ordinal=12, content="【当前目标】写完第三章。")
    assert block.startswith("【此前对话摘要】\n")
    assert "截至第 12 条消息" in block
    assert "作品内容与作者的原话优先于摘要" in block
    assert block.endswith("【当前目标】写完第三章。")
