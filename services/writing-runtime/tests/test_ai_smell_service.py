"""本书声纹库（归你味）与人写风格画像的行为验收。

fake 检测脚本会把收到的 --library 写进 detector.version，
这样测试能断言「打这本书的分时 voice 参照确实换成了本书声纹库」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zhimeng_runtime.ai_smell_service import AiSmellService


FAKE_DETECTOR = '''import json
import sys
from pathlib import Path

argv = sys.argv[1:]
output = Path(argv[argv.index("--output") + 1])
library = argv[argv.index("--library") + 1] if "--library" in argv else "none"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "content_hash": "a" * 64,
    "detector": {"version": "lib=" + library},
    "overall_score": {
        "score": 42.0,
        "level": "clean",
        "components": {
            "rhythm_punctuation_spectrum": 61.0,
            "compressed_fragments": 44.0,
            "template_phrases": 2.0,
        },
    },
    "sentence_length_variance": {
        "median": 11.0, "p25": 7.0, "p75": 17.0, "std": 9.2,
    },
    "adjective_stacking_rate": {"rate": 0.004},
    "transition_density": {
        "total_per_1000_chars": 0.2, "然而": 0.0, "但是": 0.2, "与此同时": 0.0,
    },
    "supporting_metrics": {
        "action_description_preference": {
            "direct_action": 0.43, "mental_setup": 0.02, "environment_anchor": 0.21,
        },
        "paragraph_rhythm": {"avg_sentences_per_paragraph": 1.3},
        "dialogue_rhythm": {
            "dialogue_sentence_median_length": 13.0,
            "tag_preference": "pre_tag",
            "pre_tag_ratio": 1.0,
            "post_tag_ratio": 0.0,
        },
    },
}, ensure_ascii=False), encoding="utf-8")
'''

BLACKLIST_LINE = json.dumps(
    {
        "card_id": "validator-anti-ai-blacklist-20260530",
        "card_type": "validator",
        "type_payload": {"ai_blacklist_words": ["某种意义上"]},
    },
    ensure_ascii=False,
)


@pytest.fixture()
def service_and_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AiSmellService, Path]:
    script = tmp_path / "fake-detect.py"
    script.write_text(FAKE_DETECTOR, encoding="utf-8")

    global_library = tmp_path / "global-library"
    global_library.mkdir()
    (global_library / "cards.jsonl").write_text(
        BLACKLIST_LINE + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("ZHIMENG_AI_SMELL_LIBRARY", str(global_library))

    book = tmp_path / "book"
    prose = book / "05_正文内容"
    prose.mkdir(parents=True)
    for number in (1, 2, 3):
        (prose / f"{number:03d}_第{number}章.md").write_text(
            "他把刀放回鞘里。院子里安静。\n" * 60, encoding="utf-8"
        )

    service = AiSmellService(
        script_path=script,
        runtime_state_root=tmp_path / "state",
    )
    return service, book


def test_personal_voice_library_is_built_from_this_books_prose(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    service, book = service_and_book

    library = service.personal_voice_library(book)
    assert library is not None

    lines = [
        json.loads(line)
        for line in (library / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    voice_cards = [card for card in lines if card.get("card_type") == "voice"]
    assert len(voice_cards) == 3
    for card in voice_cards:
        assert card["card_id"].startswith("voice-prose-book-")
        signature = card["type_payload"]["voice_prose_signature"]
        # 六项特征与 detect_ai_smell.py 的距离函数字段完全对齐。
        assert set(signature) == {
            "sentence_length_distribution",
            "action_description_preference",
            "paragraph_rhythm",
            "dialogue_rhythm",
            "adjective_stacking_rate",
            "transition_density",
        }
        # 偏斜指令要用的额外风格数据（检测器不读，缺了不影响距离）。
        extras = card["type_payload"]["zhimeng_style_extras"]
        assert extras["tag_preference"] == "pre_tag"
        assert "但是" in extras["transitions"]
    # 反AI黑名单卡必须跟着走，不然 --library 整体替换会弄丢黑名单维度。
    assert any(
        card.get("card_id") == "validator-anti-ai-blacklist-20260530"
        for card in lines
    )

    meta = json.loads((library / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok"
    assert meta["chapters"] == [1, 2, 3]


def test_evaluate_uses_personal_library_and_reports_reference(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    service, book = service_and_book

    report = service.evaluate_text("院子里安静。" * 80, project_root=book)
    assert report.voice_reference == "personal"
    # fake 脚本把 --library 写进 detector.version：确认真指向本书声纹库。
    assert "voice-libraries" in report.detector_version

    plain = service.evaluate_text("院子里安静。" * 80)
    assert plain.voice_reference == "global"
    assert "global-library" in plain.detector_version


def test_voice_library_rebuilds_when_chapters_change(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    service, book = service_and_book
    first = service.personal_voice_library(book)
    assert first is not None

    (book / "05_正文内容" / "004_第4章.md").write_text(
        "刀出鞘又归鞘。\n" * 80, encoding="utf-8"
    )
    second = service.personal_voice_library(book)
    assert second is not None
    meta = json.loads((second / "meta.json").read_text(encoding="utf-8"))
    assert meta["chapters"] == [1, 2, 3, 4]


def test_style_bias_hints_translate_signature_into_concrete_instructions(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    """声纹反过来喂生成：偏斜要具体到方向，「写得自然点」是废话。"""
    service, book = service_and_book

    hints = service.style_bias_hints(book)
    assert hints is not None
    assert "【这本书已经在用的写法】" in hints
    assert "句子偏短：中位约 11 字" in hints
    assert "几乎不用「然而/但是/与此同时」" in hints
    assert "心理铺垫极少" in hints
    assert "段落极碎" in hints
    assert "几乎不堆形容词" in hints
    assert "说话人标签一律前置" in hints

    # 没有声纹库（书不存在）就不给指令，绝不编造。
    assert service.style_bias_hints(book / "不存在") is None


def test_voice_library_rebuilds_when_signature_version_bumps(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    service, book = service_and_book
    library = service.personal_voice_library(book)
    assert library is not None

    meta_path = library / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["signature_version"] = 1  # 模拟旧版库
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    rebuilt = service.personal_voice_library(book)
    assert rebuilt is not None
    refreshed = json.loads(meta_path.read_text(encoding="utf-8"))
    assert refreshed["signature_version"] == AiSmellService.SIGNATURE_VERSION


def test_project_style_profile_marks_chronic_components(
    service_and_book: tuple[AiSmellService, Path],
) -> None:
    service, book = service_and_book

    profile = service.project_style_profile(book)
    assert profile is not None
    assert profile["median"] == 42.0
    assert profile["samples"] == 3
    # fake 检测里节奏谱和压缩残句每章都进 top3 → 慢性主因。
    assert "rhythm_punctuation_spectrum" in profile["chronic"]
    assert "compressed_fragments" in profile["chronic"]

    # 书太小 / 根本不存在时给 None，且缓存后不再反复跑检测。
    assert service.project_style_profile(book / "不存在") is None
