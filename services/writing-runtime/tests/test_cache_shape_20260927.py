"""缓存形状（cache_shape.py）：和上一次请求比，说得出开头留下了几条、为什么变了。"""

from __future__ import annotations

from zhimeng_runtime.cache_shape import compare_shapes, request_shape

SYSTEM = {"role": "system", "content": "环境：Windows；今天 2026-09-27"}
TOOLS = [{"name": "read_file", "description": "读一个文件"}]


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def test_the_first_request_of_a_conversation_has_nothing_to_compare_with() -> None:
    result = compare_shapes(None, request_shape([SYSTEM, _user("你好")], TOOLS))

    assert result == {"reason": "first_request", "messages": 2}


def test_a_request_that_only_adds_to_the_end_keeps_the_whole_previous_one() -> None:
    before = request_shape([SYSTEM, _user("你好")], TOOLS)
    after = request_shape([SYSTEM, _user("你好"), _assistant("你好！"), _user("接着写")], TOOLS)

    result = compare_shapes(before, after)

    assert result["reason"] == "append_only"
    assert (result["kept_messages"], result["previous_messages"], result["messages"]) == (2, 2, 4)


def test_a_changed_tool_list_breaks_everything_after_it() -> None:
    before = request_shape([SYSTEM, _user("你好")], TOOLS)
    after = request_shape([SYSTEM, _user("你好"), _user("再来")], [*TOOLS, {"name": "write_file"}])

    result = compare_shapes(before, after)

    assert result["reason"] == "tools"
    assert result["tools_changed"] is True


def test_a_changed_first_message_is_told_apart() -> None:
    before = request_shape([SYSTEM, _user("你好")], TOOLS)
    after = request_shape([{"role": "system", "content": "环境：Windows；今天 2026-09-28"}, _user("你好")], TOOLS)

    assert compare_shapes(before, after)["reason"] == "first_message"


def test_a_sliding_history_window_diverges_right_after_the_first_message() -> None:
    # 滑动窗口：最老的一条被挤掉，第二条起全都对不上了——只剩开头一条留下。
    before = request_shape([SYSTEM, _user("一"), _assistant("二"), _user("三"), _assistant("四")], TOOLS)
    after = request_shape([SYSTEM, _assistant("二"), _user("三"), _assistant("四"), _user("五")], TOOLS)

    result = compare_shapes(before, after)

    assert result["reason"] == "diverged"
    assert result["kept_messages"] == 1


def test_the_next_turn_dropping_last_turns_tool_steps_keeps_most_of_the_prefix() -> None:
    # 织梦的历史只存每轮的回答文字：上一轮里的工具往来下一轮不带，从那儿分开，前面的历史照样留着。
    history = [SYSTEM, _user("一"), _assistant("二"), _user("三"), _assistant("四"), _user("接着写")]
    tool_steps = [
        {"role": "user", "content": "用户这时在编辑器里开着：第5章.md"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "read_file"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "第 4 章正文"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "name": "write_file"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "写好了"},
    ]
    before = request_shape([*history, *tool_steps], TOOLS)
    after = request_shape([*history, _assistant("第 5 章写好了"), _user("再改改")], TOOLS)

    result = compare_shapes(before, after)

    assert result["reason"] == "diverged"
    assert result["kept_messages"] == len(history)


def test_dropping_only_the_per_turn_tail_note_counts_as_normal() -> None:
    # 每轮末尾那条「开着哪个文件」不进历史：下一次在它的位置上是模型的回答。
    tail = {"role": "user", "content": "用户这时在编辑器里开着：第5章.md"}
    before = request_shape([SYSTEM, _user("接着写"), tail], TOOLS)
    after = request_shape([SYSTEM, _user("接着写"), _assistant("写好了"), _user("再改改"), tail], TOOLS)

    result = compare_shapes(before, after)

    assert result["reason"] == "tail"
    assert result["kept_messages"] == 2


def test_tool_definitions_that_are_pydantic_models_are_fingerprinted_by_their_fields() -> None:
    from zhimeng_runtime.agent_provider import ProviderToolDefinition

    one = ProviderToolDefinition(name="read_file", version="1", description="读", parameters={"type": "object"})
    same = ProviderToolDefinition(name="read_file", version="1", description="读", parameters={"type": "object"})
    other = ProviderToolDefinition(name="read_file", version="1", description="读文件", parameters={"type": "object"})

    assert request_shape([SYSTEM], [one]).tools == request_shape([SYSTEM], [same]).tools
    assert request_shape([SYSTEM], [one]).tools != request_shape([SYSTEM], [other]).tools
