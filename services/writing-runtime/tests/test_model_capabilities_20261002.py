"""ModelCapability 能力卡测试：按家族给默认 wire / 思考档 / vision。

2026-10-05：接口建议 = 这家模型自己原生的接口（GPT / o 系列 / codex、grok → Responses，
Claude → Anthropic，其余 → Chat）。建议只决定「自动」先试哪个；中转站上通不通由测活真测决定。
"""
from __future__ import annotations

from zhimeng_runtime.model_capabilities import (
    clamp_effort,
    resolve_model_capability,
)


def test_deepseek_default_chat_and_reasoning() -> None:
    card = resolve_model_capability("deepseek-v3.2")
    assert card.default_wire == "responses"
    assert card.supports_reasoning_content is True


def test_grok_default_responses_and_max_effort() -> None:
    """grok 原生 Responses（xAI 推荐），220 上 10-05 实测 Responses 出字、调工具都通。"""
    card = resolve_model_capability("grok-4.6")
    assert card.default_wire == "responses"
    assert "max" in card.effort_levels


def test_kimi_k3_default_chat_and_vision() -> None:
    """kimi 实测 chat 通，anthropic_messages 空响应（220 中转站 2026-10-04）。"""
    card = resolve_model_capability("kimi-k3")
    assert card.default_wire == "responses"  # 10-07 作者：「尽可能 res，res 不能就 chat」
    assert card.vision is True


def test_gemini_default_chat() -> None:
    card = resolve_model_capability("gemini-3.8-flash")
    assert card.default_wire == "responses"  # 10-07 作者：「尽可能 res，res 不能就 chat」


def test_openai_models_suggest_responses_first() -> None:
    """作者 10-05：「gpt这种肯定是res」。GPT 自家原生是 Responses；220 上不通时测活会换回 Chat。"""
    for name in ("gpt-5.6-luna", "gpt-oss-120b", "openai/gpt-5", "[opencode]gpt-5.1", "gpt-5-codex",
                 "o3", "o4-mini", "openai/o1-pro"):
        assert resolve_model_capability(name).default_wire == "responses", name


def test_claude_suggests_anthropic_first() -> None:
    assert resolve_model_capability("claude-sonnet-4.5").default_wire == "anthropic_messages"


def test_o_series_rule_does_not_catch_other_families() -> None:
    for name in ("qwen3-omni", "glm-4o", "kimi-k3", "deepseek-v4-pro", "gemini-3.8-flash", "mimo-v2"):
        assert resolve_model_capability(name).default_wire == "responses", name  # 10-07：全部 responses 优先


def test_unknown_model_falls_back_safely() -> None:
    card = resolve_model_capability("some-new-model-2607")
    assert card.context_window_tier == "256k"
    assert card.default_effort == "medium"
    assert card.default_wire == "chat"  # 10-07 认不出家族的还是保守给 chat


def test_clamp_effort_to_family_ladder() -> None:
    # grok 能吃 max，给它就还
    assert clamp_effort("grok-4.6", "max") == "max"
    # deepseek 吃不到 max，压到它最高的（xhigh）
    assert clamp_effort("deepseek-v4-pro", "max") != "max"
    # glm 吃不到 xhigh，压到它默认的 medium
    assert clamp_effort("glm-5.3", "xhigh") == "medium"


def test_new_models_rule_based_auto_assign_wire() -> None:
    """新模型按规则配：没认出来的一律先试 chat；GPT / grok 家族先试 responses。"""
    # deepseek-v5-pro（小表没标）按规则自动配 chat + reasoning_content
    card = resolve_model_capability("deepseek-v5-pro")
    assert card.default_wire == "responses"
    assert card.supports_reasoning_content is True
    # grok-4.7（小表没标）按规则先试 responses
    card2 = resolve_model_capability("grok-4.7")
    assert card2.default_wire == "responses"
    # qwen3.8（小表没标）按规则自动配 chat + vision
    card3 = resolve_model_capability("qwen3.8-max")
    assert card3.default_wire == "responses"
    assert card3.vision is True
    # minimax/miki/mimo 实测没验过，保守给 chat
    card4 = resolve_model_capability("minimax-m2.7")
    assert card4.default_wire == "responses"
