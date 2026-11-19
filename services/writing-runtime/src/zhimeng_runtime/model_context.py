"""上下文窗口两档设置：256K（默认）/ 1M（最大）。

一个设置项决定所有预算，不再按模型名猜测。作者在设置页选档位，
各模块只从这张表取值，别处不再自己算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ContextWindowTier = Literal["256k", "1m"]
DEFAULT_CONTEXT_WINDOW_TIER: ContextWindowTier = "256k"
CONTEXT_BUDGET_RATIO = 0.82


@dataclass(frozen=True, slots=True)
class ContextWindowSpec:
    tier: ContextWindowTier
    label: str
    window_tokens: int
    # 对话历史 + 工具结果的 token 预算（窗口 × 0.82）。
    context_token_budget: float
    # 对话整理（摘要）：还没整理的对话超过触发字数就整理，只原样留下最近的几轮（加起来不超过 keep_recent 字，至少一整轮），
    # 更早的一次折完。9-28 以前是「固定留最近 6 轮、一次最多折 4 万字」：几轮长的（贴原文、写整章）加起来就过了线，
    # 于是过线以后每说一句都整理一次、摘要每轮都换，缓存每轮不命中（9-28 压缩考题 base1）。
    checkpoint_trigger_characters: int
    checkpoint_max_characters: int
    checkpoint_keep_recent_characters: int
    # 折起来那段里作者说的原话，由程序原样附在摘要后面，最多这么多字（照 Codex 压缩后保留的作者原话，约 2 万 token）。
    checkpoint_author_words_characters: int
    # 资料包装箱预算。
    pack_max_characters: int
    pack_max_tokens: int
    # 9-28 起下一轮照原样带上上一轮的工具往来（ZCode 档案 12.2），历史里有了读过的原文、命令输出这些 JSON，
    # 按字数算就不准了（英文和 JSON 三个字母才一个 token），整理改按估算的 token（UTF-8 字节 ÷ 3，中文一字一个）：
    # 没整理的历史到这么多就整理，原样留下最近的几轮不超过 keep_recent（至少一整轮）。没给就按上面的字数。
    checkpoint_trigger_tokens: int | None = None
    checkpoint_keep_recent_tokens: int | None = None

    @property
    def trigger_tokens(self) -> int:
        return self.checkpoint_trigger_tokens or self.checkpoint_trigger_characters

    @property
    def keep_recent_tokens(self) -> int:
        return self.checkpoint_keep_recent_tokens or self.checkpoint_keep_recent_characters

    @property
    def history_max_tokens(self) -> int:
        """发给模型的历史（摘要之后那段）最多这么多 token：整理线的两倍。和 history_max_characters 一样只防一次贴进来的超长内容。"""
        return 2 * self.trigger_tokens

    @property
    def history_max_characters(self) -> int:
        """发给模型的对话历史（摘要之后那段）最多这么多字：压缩线的两倍（256K 12 万字、1M 48 万字）。

        不按条数截（9-27）：压缩线以下只追加，前缀才稳定；这个数只防一次贴进来的超长内容，平时碰不到。
        以前一律 12 万字，1M 档 24 万字才压缩，窗口先顶上、每轮往后滑。
        """
        return 2 * self.checkpoint_trigger_characters


CONTEXT_WINDOW_SPECS: dict[ContextWindowTier, ContextWindowSpec] = {
    "256k": ContextWindowSpec(
        tier="256k",
        label="256K",
        window_tokens=256_000,
        context_token_budget=float(int(256_000 * CONTEXT_BUDGET_RATIO)),
        checkpoint_trigger_characters=60_000,
        checkpoint_max_characters=8_000,
        checkpoint_keep_recent_characters=15_000,
        checkpoint_author_words_characters=12_000,
        pack_max_characters=200_000,
        pack_max_tokens=100_000,
        # 窗口的四成：一轮带工具的往来通常 1–3 万 token，整理完剩摘要 + 最近一轮，还能再走几轮；这一轮里自己的预算是窗口 × 0.82。
        checkpoint_trigger_tokens=100_000,
        checkpoint_keep_recent_tokens=15_000,
    ),
    "1m": ContextWindowSpec(
        tier="1m",
        label="1M",
        window_tokens=1_000_000,
        context_token_budget=float(int(1_000_000 * CONTEXT_BUDGET_RATIO)),
        checkpoint_trigger_characters=240_000,
        checkpoint_max_characters=32_000,
        checkpoint_keep_recent_characters=60_000,
        checkpoint_author_words_characters=48_000,
        pack_max_characters=800_000,
        pack_max_tokens=400_000,
        checkpoint_trigger_tokens=400_000,
        checkpoint_keep_recent_tokens=60_000,
    ),
}


def normalize_context_window_tier(raw: object) -> ContextWindowTier:
    """只认两个档位，其余一律回落到 256K。"""
    return "1m" if str(raw or "").strip().lower() == "1m" else "256k"


def resolve_context_window_spec(raw: object) -> ContextWindowSpec:
    return CONTEXT_WINDOW_SPECS[normalize_context_window_tier(raw)]


def resolve_context_window_label(raw: object) -> str:
    return resolve_context_window_spec(raw).label
