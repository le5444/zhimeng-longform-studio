from __future__ import annotations

from zhimeng_runtime.model_context import (
    CONTEXT_WINDOW_SPECS,
    normalize_context_window_tier,
    resolve_context_window_label,
    resolve_context_window_spec,
)


def test_two_tiers_budget_values() -> None:
    spec_256k = resolve_context_window_spec("256k")
    assert spec_256k.window_tokens == 256_000
    assert spec_256k.context_token_budget == float(int(256_000 * 0.82))
    assert spec_256k.checkpoint_trigger_characters == 60_000
    assert spec_256k.checkpoint_max_characters == 8_000
    assert spec_256k.checkpoint_keep_recent_characters == 15_000
    assert spec_256k.checkpoint_author_words_characters == 12_000
    assert spec_256k.pack_max_characters == 200_000
    assert spec_256k.pack_max_tokens == 100_000

    spec_1m = resolve_context_window_spec("1m")
    assert spec_1m.window_tokens == 1_000_000
    assert spec_1m.context_token_budget == float(int(1_000_000 * 0.82))
    assert spec_1m.checkpoint_trigger_characters == 240_000
    assert spec_1m.checkpoint_max_characters == 32_000
    assert spec_1m.checkpoint_keep_recent_characters == 60_000
    assert spec_1m.checkpoint_author_words_characters == 48_000
    assert spec_1m.pack_max_characters == 800_000
    assert spec_1m.pack_max_tokens == 400_000


def test_unknown_values_fall_back_to_256k() -> None:
    assert normalize_context_window_tier(None) == "256k"
    assert normalize_context_window_tier("") == "256k"
    assert normalize_context_window_tier("1M") == "1m"
    assert normalize_context_window_tier("128k") == "256k"
    assert normalize_context_window_tier("1m ") == "1m"
    # 模型名不再被猜测：带容量数字的名字也只得到默认档
    assert normalize_context_window_tier("moonshot-v1-1m") == "256k"
    assert normalize_context_window_tier("gemini-2.0-flash") == "256k"


def test_labels_and_no_estimation_wording() -> None:
    assert resolve_context_window_label("256k") == "256K"
    assert resolve_context_window_label("1m") == "1M"
    for spec in CONTEXT_WINDOW_SPECS.values():
        assert "预估" not in spec.label
