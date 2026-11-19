"""Per-model capability cards（照 YAgent 的 model-capabilities.js 形状做织梦版）。

每家模型一张卡：窗口、思考档、能收图 / 能产图、最大输出、是不是 responses 线、
思考台阶按真实家族给（不是按模型名猜一条）。

用法：`provider_settings_service` 按 conversation 的 model id 查这张表，
让模型接入自动配「这个模型默认的思考档 / wire / 输出上限」，
作者改的话让作者改，不给作者硬塞。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# 思考台阶（按真实家族，越深越耗 token；挂低档快而浅）。
EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# 哪些模型能收图（按 YAgent 的 STATIC_VISION_MODEL_IDS，加上织梦这边的实测）
STATIC_VISION_MODEL_IDS = frozenset({
    "agnes-image-2.0-flash",
    "agnes-image-2.1-flash",
    "kimi-k3",
    "glm-5v-turbo",
    "deepseek-vision",
    "google/gemma-3-12b-it",
    "google/gemma-3-4b-it",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-pro",
    "qwen3.8-max-preview",
})

# 哪些只收字（按 YAgent 的 STATIC_TEXT_ONLY_MODEL_IDS）
STATIC_TEXT_ONLY_MODEL_IDS = frozenset({
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v3.1",
    "deepseek-v3.2",
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "glm-4.7",
    "glm-4.6",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "qwen3.7-plus",
    "qwen3.7-flash",
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "MiniMax-M3",
    "minimax-m2.7",
})


@dataclass(frozen=True)
class ModelCapability:
    """一家模型的一张能力卡。"""

    model_id: str
    context_window_tier: str = "256k"          # 窗口档位（256k / 128k / 1m）
    default_effort: str = "medium"             # 推荐思考档（EFFORT_LADDER 里的值）
    effort_levels: tuple[str, ...] = ("low", "medium", "high")  # 这模型能吃哪几档
    default_wire: str = "chat"                 # 推荐协议（chat / responses / anthropic_messages）
    max_output_tokens: int = 32768             # 单次输出上限
    vision: bool = False                       # 能收图
    image_generation: bool = False             # 能产图（图像生成）
    supports_tools: bool = True                # 工具调用
    supports_json_schema: bool = False         # 结构化输出 / JSON schema
    supports_reasoning_content: bool = False   # 推理字段外露（deepseek/kimi 的 reasoning_content）


def _entry(**kwargs) -> ModelCapability:
    return ModelCapability(**kwargs)


# 能力卡表：**家族里每个模型的特殊配置**——大多数模型靠家族规则配 wire，
# 但这张表单独标那些有**独特思考档 / 独特输出上限 / 独特 vision** 的个体。
MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    # deepseek 家族里带「大窗口 / X 档」的个体
    "deepseek-v4-pro": _entry(
        model_id="deepseek-v4-pro", default_wire="responses", supports_reasoning_content=True,
        default_effort="high", effort_levels=("low", "medium", "high", "xhigh"), max_output_tokens=65536,
    ),
    "deepseek-v3.2": _entry(
        model_id="deepseek-v3.2", default_wire="responses", supports_reasoning_content=True,
        default_effort="medium", max_output_tokens=65536,
    ),
    # grok 家族默认 responses，思考档吃 xhigh/max
    "grok-4.6": _entry(
        model_id="grok-4.6", default_wire="responses", supports_reasoning_content=True,
        default_effort="high", effort_levels=("low", "medium", "high", "xhigh", "max"),
    ),
    # kimi / moonshot 实测 chat 通（220 中转站 2026-10-04），anthropic_messages 空响应
    "kimi-k3": _entry(
        model_id="kimi-k3", default_wire="responses", supports_reasoning_content=True,
        default_effort="high", vision=True,
    ),
    # GLM 5.3 智谱通用 chat
    "glm-5.3": _entry(
        model_id="glm-5.3", default_wire="responses", supports_reasoning_content=True,
        default_effort="medium", effort_levels=("low", "medium", "high"),
    ),
    # GPT（OpenAI）自家原生是 Responses：OpenAI 推荐它、Codex 只用它，sub2api 的 OpenAI 账号走它是原样直通。
    # 10-04 在 220 上它的 Responses 回空，那是 220 给这个模型配的上游没接 Responses——「自动」测活会换回 Chat。
    "gpt-5.6-luna": _entry(
        model_id="gpt-5.6-luna", default_wire="responses",
        effort_levels=("low", "medium", "high", "xhigh", "max"),
    ),
}


# OpenAI 的 o 系列（o1 / o3 / o4-mini，前面可能带 openai/ 之类的中转前缀）：名字太短，单独认。
_OPENAI_O_SERIES = re.compile(r"(?:^|[/\]\s_-])o[1-9](?:-|$)")


def resolve_model_capability(model_id: str) -> ModelCapability:
    """按模型 id 查能力卡；查不到就按家族规则配默认 wire。

    设计（照 Codex/Pi/CC Switch）：小表只放「特例」，其余都按模型名匹配规则——
    新模型进来按规则自动配，不用手填；只有不吃默认 wire 的个体才标特例。
    """
    m = (model_id or "").strip()
    if not m:
        return ModelCapability(model_id="unknown")
    if m in MODEL_CAPABILITIES:
        return MODEL_CAPABILITIES[m]
    lowered = m.lower()

    # ── 家族规则：新模型进来按名字给个「先试哪种」的建议 ───────────────
    # 建议 = 这家模型自己原生的接口：GPT / o 系列 / codex、grok 是 Responses，Claude 是 Anthropic，
    # 其余（国产模型、gemini 等）是 Chat。建议只决定「自动」时先试哪个、没测过时先走哪个；
    # 中转站上通不通由测活真测决定（provider_settings_service.test_models），不通就换下一种。
    if _OPENAI_O_SERIES.search(lowered):
        return ModelCapability(
            model_id=m, default_wire="responses", effort_levels=("low", "medium", "high", "xhigh", "max")
        )
    family_rules: list[tuple[tuple[str, ...], dict]] = [
        (("deepseek",), {"default_wire": "responses", "supports_reasoning_content": True}),
        (("grok-", "grok_"), {"default_wire": "responses", "effort_levels": ("low", "medium", "high", "xhigh", "max")}),
        (("kimi", "moonshot"), {"default_wire": "responses", "supports_reasoning_content": True, "vision": True}),
        (("glm", "zhipu", "chatglm"), {"default_wire": "responses", "supports_reasoning_content": True}),
        (("qwen", "tongyi", "alibaba"), {"default_wire": "responses", "vision": True}),
        (("gemini", "google"), {"default_wire": "responses", "vision": True}),
        (("gpt", "openai", "codex"), {"default_wire": "responses", "effort_levels": ("low", "medium", "high", "xhigh", "max")}),
        (("claude", "anthropic"), {"default_wire": "anthropic_messages"}),
        (("minimax",), {"default_wire": "responses"}),
        (("miki", "mimo"), {"default_wire": "responses"}),
        (("agnes-",), {"default_wire": "responses"}),
    ]
    for keys, patch in family_rules:
        if any(k in lowered for k in keys):
            return ModelCapability(model_id=m, **patch)

    # ── vision 兜底：名字里带 vision / image 且不是 coder 的当能收图 ──
    if any(k in lowered for k in ("vision", "-vl", "vl-", "image")) and not any(
        k in lowered for k in ("coder", "code", "flash-lite")
    ):
        return ModelCapability(model_id=m, vision=True)

    # ── 最后兜底：chat（最保守，最不可能错） ──
    return ModelCapability(model_id=m)


def clamp_effort(model_id: str, effort: str) -> str:
    """把作者的思考档压到这模型真能吃的那层（照 YAgent 的 ladderIndex 做法）。"""
    card = resolve_model_capability(model_id)
    levels = list(card.effort_levels)
    if effort in levels:
        return effort
    # 压到它默认档；压不下就压到它最高的那层
    if card.default_effort in levels:
        return card.default_effort
    return levels[-1]
