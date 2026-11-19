"""调用 novel-creation-suite 的 detect_ai_smell.py，给稿子打「AI 味」分。

为什么是 subprocess 而不是重写：

* `detect_ai_smell.py` 是 5311 行纯标准库的确定性规则引擎，作者已经用了很久。
  重写等于造第二套评分标准，违反 DRY，而且分数不可比。
* 它输出带 `content_hash` 的 JSON，天然适合做证据绑定。

分数怎么解读（真书校准 2026-08-13 的结论，别退回绝对分数）：

* 作者两本书的人写正文均分 37.6、模型改稿 38.5——绝对分数分不出人和机器。
  所以有两层校准：
  1. **本书人写基线**（`project_style_profile`）：分数相对最近几章人写正文的
     中位数解读，还记下本书常年偏高的判罚组件（慢性主因），摘要里对这些主因
     明说「属本书风格而非 AI 病」。
  2. **本书声纹库**（`personal_voice_library`）：从人写正文抽文风签名拼成
     合法的 voice-prose 卡，打分时 `--library` 指过去——「参考文风距离」
     从「离 44 张陌生卡多远」变成「离你这本书多远」。这是「归你味」的落点。

路径策略（按优先级）：
  1. 显式传入 / 设置里配置
  2. 环境变量 `ZHIMENG_AI_SMELL_SCRIPT`
  3. 随包受管快照（`skill_bundle` 统一解析，见该模块 docstring）
  4. 本机 `~/.codex/skills/...` —— 只是兜底，打包后不应命中

查找顺序集中在 `skill_bundle`，不要在这里再写一套。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zhimeng_runtime import skill_bundle
from zhimeng_runtime.fs_atomic import replace_with_retry

DEFAULT_TIMEOUT_SECONDS = 120

# 样本低于这个字数时，密度和分布指标不稳，分数只标参考。
AI_SMELL_SHORT_SAMPLE_CHARACTERS = 300

# 与本书基线差在这个分数带内视为正常波动（真书校准：人写章间波动 ±13 分）。
BASELINE_RELATIVE_BAND = 8

# 判罚组件的英文名 → 作者能看懂的说法。缺的回落原名。
COMPONENT_LABELS = {
    "rhythm_punctuation_spectrum": "节奏标点谱（短句/标点节奏）",
    "compressed_fragments": "压缩残句",
    "semantic_completeness": "语义完整性",
    "voice_signature_distance": "参考文风距离",
    "template_phrases": "模板短语",
    "adjective_stacking": "形容词堆叠",
    "repeated_sentence_starts": "句首重复",
    "sentence_opener_monotony": "句首单调",
    "report_style": "汇报腔",
    "explanatory_narration": "解释性旁白",
    "abstract_emotion": "抽象抒情",
    "paragraph_uniformity": "段落均一",
    "rhythm_uniformity": "节奏均一",
    "idiom_stacking": "成语堆叠",
    "mental_summary": "心理总结",
}


def resolve_library(
    script: Path | None,
    runtime_state_root: Path | None = None,
) -> Path | None:
    """解析 AI 味检测要用的 novel-distillation 卡库路径。

    detect_ai_smell.py 默认用 Path(__file__).parents[2]/novel-distillation/library 找卡库；
    一旦脚本被纳入受管快照、移到别处，这个默认就会指错，voice 维度会静默降级。
    这里显式传 --library 覆盖它：环境变量 → 随包/本机快照 → 基于脚本位置的回退。

    卡库目录必须非空：随包时会预建空的 library 骨架，只看 is_dir() 会把
    「一张卡都没有」当成可用，voice_signature_distance 会静默变 available=false。
    """
    env_lib = os.environ.get("ZHIMENG_AI_SMELL_LIBRARY")
    if env_lib:
        candidate = Path(env_lib)
        if candidate.is_dir():
            return candidate
    resolved = skill_bundle.distillation_library(runtime_state_root)
    if resolved is not None:
        return resolved
    if script is not None:
        fallback = script.resolve().parents[2] / "novel-distillation" / "library"
        if skill_bundle.has_distillation_cards(fallback):
            return fallback
    return None


class AiSmellError(RuntimeError):
    code = "ai_smell_failed"


class AiSmellUnavailable(AiSmellError):
    code = "ai_smell_unavailable"


def summary_note(
    payload: dict | None,
    baseline: float | None = None,
    chronic: tuple[str, ...] | list[str] = (),
    purpose: str = "review",
) -> str:
    """把检测证据带进改稿摘要：分数、本书基线对比、慢性主因、例句、改碎告警。

    分数只是代理指标，测的是「干净」不是「好」，更不是「有没有一个人在写」
    （三探：那一层技术解不了）。真书校准证明绝对分数分不出人和机器，所以
    必须相对这本书自己的人写基线解读。

    purpose:
      - review：作者在审一段改稿，相对基线告诉他漂了没有。
      - draft：这是第一稿。三探的猫鼠警告——把「去AI味」写成优化目标，
        模型只会换一层皮。草稿分只作参考，不招募作者为降分改稿。
    """
    if not payload:
        return ""
    score = float(payload["score"])
    if purpose == "draft":
        note = f"；草稿参考分 {score:.0f}（{payload['level']}）"
    else:
        note = f"；AI 味 {score:.0f} 分（{payload['level']}）"
    voice = payload.get("voice_reference")
    if voice == "personal":
        note += "；对照这本书自己的写法"
    elif voice == "none":
        note += "；暂无文风对照"
    elif voice == "global":
        note += "；对照通用文风样本"
    if baseline is not None:
        diff = score - baseline
        if abs(diff) <= BASELINE_RELATIVE_BAND:
            note += f"，贴近本书人写基线（约 {baseline:.0f} 分），属正常波动"
        elif diff > BASELINE_RELATIVE_BAND:
            if purpose == "draft":
                note += (
                    f"，高出本书人写基线 {diff:.0f} 分"
                    "——草稿如此常见，不必为降分改稿"
                )
            else:
                note += f"，高出本书人写基线 {diff:.0f} 分，建议看例句"
        else:
            note += f"，低于本书人写基线 {-diff:.0f} 分，留意是否洗掉了本书的味"
    top = payload.get("top_components") or []
    if chronic and top:
        first = top[0]
        name = str(first.get("name", "")) if isinstance(first, dict) else str(first[0])
        if name and name in set(chronic):
            label = COMPONENT_LABELS.get(name, name)
            note += f"；判罚主因「{label}」在本书人写正文里同样偏高，属本书风格而非 AI 病"
    if payload.get("sample_characters", 0) < AI_SMELL_SHORT_SAMPLE_CHARACTERS:
        note += "，样本短仅供参考"
    worst = payload.get("worst_sentences") or []
    if worst:
        first_worst = worst[0]
        reasons = "、".join(first_worst.get("reasons") or [])
        sentence = str(first_worst.get("sentence") or "")[:40]
        note += f"；例如「{sentence}」（{reasons}）"
    if (payload.get("deai_drift") or {}).get("risk_level") in {"medium", "high"}:
        note += "；注意：改后句子疑似被改碎（去AI过度）"
    return note


@dataclass(frozen=True, slots=True)
class AiSmellReport:
    score: float
    level: str
    content_hash: str
    detector_version: str
    #  只保留分数最高（最像 AI）的几个维度，作者不需要看全部 30 个 0.0
    top_components: tuple[tuple[str, float], ...]
    # 传了 compare_before 时才有：去AI是否把稿子改碎（overbroken_after_deai）
    deai_drift: dict | None = None
    # 证据要到作者面前：最像 AI 的句子和判罚原因（外部锐评点名的黑箱问题）。
    worst_sentences: tuple[dict, ...] = ()
    # 样本太短时分布指标不稳，分数只能参考，不能横向比较。
    sample_characters: int = 0
    # 「参考文风距离」参照谁："personal"=本书声纹库（归你味），
    # "global"=蒸馏全局卡库，"none"=没有卡库、voice 维度降级。
    voice_reference: str = "global"

    def as_dict(self) -> dict:
        payload = {
            "score": self.score,
            "level": self.level,
            "content_hash": self.content_hash,
            "detector_version": self.detector_version,
            "top_components": [
                {"name": name, "value": value} for name, value in self.top_components
            ],
            "sample_characters": self.sample_characters,
            "voice_reference": self.voice_reference,
        }
        if self.worst_sentences:
            payload["worst_sentences"] = list(self.worst_sentences)
        if self.deai_drift is not None:
            payload["deai_drift"] = self.deai_drift
        return payload


def resolve_script(
    explicit: str | os.PathLike[str] | None = None,
    runtime_state_root: Path | None = None,
) -> Path | None:
    for candidate in (explicit, os.environ.get("ZHIMENG_AI_SMELL_SCRIPT")):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return skill_bundle.find_file(skill_bundle.AI_SMELL_SCRIPT, runtime_state_root)


def _voice_signature(payload: dict) -> dict | None:
    """从一份检测报告里抽出声纹卡需要的六项特征签名。

    字段名与 detect_ai_smell.py 的 voice_distance_for_card 完全对齐
    （2026-08-13 用真章验证过），缺任何一项就放弃这一章，不填零凑数——
    填零会把距离撑大，比没有卡更糟。
    """
    try:
        sentence = payload["sentence_length_variance"]
        supporting = payload.get("supporting_metrics") or {}
        action = supporting["action_description_preference"]
        paragraph = supporting["paragraph_rhythm"]
        dialogue = supporting["dialogue_rhythm"]
        adjective = payload["adjective_stacking_rate"]
        transition = payload["transition_density"]
        return {
            "sentence_length_distribution": {
                key: float(sentence[key]) for key in ("median", "p25", "p75", "std")
            },
            "action_description_preference": {
                key: float(action[key])
                for key in ("direct_action", "mental_setup", "environment_anchor")
            },
            "paragraph_rhythm": {
                "avg_sentences_per_paragraph": float(
                    paragraph["avg_sentences_per_paragraph"]
                )
            },
            "dialogue_rhythm": {
                "dialogue_sentence_median_length": float(
                    dialogue["dialogue_sentence_median_length"]
                )
            },
            "adjective_stacking_rate": {"rate": float(adjective["rate"])},
            "transition_density": {
                "total_per_1000_chars": float(transition["total_per_1000_chars"])
            },
        }
    except (KeyError, TypeError, ValueError):
        return None


def _voice_style_extras(payload: dict) -> dict:
    """签名之外、只给「偏斜指令」用的风格数据（检测器不读这些字段）。

    对白标签前置/后置的习惯、各转折词的分项密度——这两样翻译成生成
    指令时最有约束力（「对白标签一律前置」「别满篇然而」），但不在
    voice_distance_for_card 的距离公式里，所以单独存，缺了也不影响炼卡。
    """
    extras: dict = {}
    supporting = payload.get("supporting_metrics") or {}
    dialogue = supporting.get("dialogue_rhythm")
    if isinstance(dialogue, dict):
        for key in ("tag_preference", "pre_tag_ratio", "post_tag_ratio"):
            if key in dialogue:
                extras[key] = dialogue[key]
    transition = payload.get("transition_density")
    if isinstance(transition, dict):
        extras["transitions"] = {
            str(word): float(value)
            for word, value in transition.items()
            if word != "total_per_1000_chars" and isinstance(value, (int, float))
        }
    return extras


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class AiSmellService:
    def __init__(
        self,
        *,
        script_path: str | os.PathLike[str] | None = None,
        runtime_state_root: str | os.PathLike[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._explicit = script_path
        self._runtime_state_root = (
            Path(runtime_state_root) if runtime_state_root is not None else None
        )
        self._timeout = timeout_seconds
        # 每本书的人写风格画像（基线中位数 + 慢性主因），进程内缓存。
        # None 也缓存：书太小算不出时别每次改稿都白跑一遍检测。
        self._profiles: dict[str, dict | None] = {}

    @property
    def script(self) -> Path | None:
        return resolve_script(self._explicit, self._runtime_state_root)

    @property
    def available(self) -> bool:
        return self.script is not None

    BASELINE_SAMPLE_CHAPTERS = 3
    BASELINE_SAMPLE_CHARACTERS = 4000

    # 声纹库取最近几章人写正文；每章截前若干字算签名。
    VOICE_SAMPLE_CHAPTERS = 5
    VOICE_SAMPLE_CHARACTERS = 6000
    VOICE_MIN_CHAPTER_CHARACTERS = 800
    # 至少两章签名才立库：单章可能正好是打斗章/对白章，锁死会更偏。
    VOICE_MIN_CARDS = 2
    BLACKLIST_CARD_ID = "validator-anti-ai-blacklist-20260530"
    # 卡内容格式版本：升它会让旧库自动重炼（v2 加了 zhimeng_style_extras）。
    SIGNATURE_VERSION = 2

    # ------------------------------------------------------------------
    # 本书人写基线（分数怎么读）

    def project_style_profile(self, project_root: Path | None) -> dict | None:
        """这本书人写正文的风格画像：基线中位数 + 慢性判罚主因。

        chronic 是「在 ≥2 个人写样本的 top 判罚组件里出现过」的组件名——
        真书校准发现作者的白描短句风会让节奏三件套常年偏高，这不是病，
        摘要里要按风格解读。返回 None 表示书里还没有足够正文或检测不可用。
        """
        if project_root is None or not self.available:
            return None
        key = str(Path(project_root).resolve())
        if key in self._profiles:
            return self._profiles[key]
        prose = self._prose_map(project_root)
        if prose is None:
            return None
        numbers = sorted(prose)[-self.BASELINE_SAMPLE_CHAPTERS :]
        scores: list[float] = []
        component_hits: dict[str, int] = {}
        for number in numbers:
            try:
                text = prose[number].read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) < self.VOICE_MIN_CHAPTER_CHARACTERS:
                    continue
                report = self.evaluate_text(
                    text[: self.BASELINE_SAMPLE_CHARACTERS],
                    project_root=project_root,
                )
                scores.append(report.score)
                for name, _value in report.top_components:
                    component_hits[name] = component_hits.get(name, 0) + 1
            except AiSmellError:
                continue
        profile: dict | None
        if not scores:
            profile = None
        else:
            scores.sort()
            chronic = tuple(
                sorted(name for name, hits in component_hits.items() if hits >= 2)
            )
            profile = {
                "median": scores[len(scores) // 2],
                "samples": len(scores),
                "chronic": chronic,
            }
        self._profiles[key] = profile
        return profile

    def project_baseline(self, project_root: Path | None) -> float | None:
        """这本书人写正文的 AI 味基线分（中位数）。兼容旧调用。"""
        profile = self.project_style_profile(project_root)
        return None if profile is None else float(profile["median"])

    # ------------------------------------------------------------------
    # 本书声纹库（「参考文风距离」参照谁）

    def personal_voice_library(self, project_root: Path | None) -> Path | None:
        """这本书专属的声纹卡库；不存在就用最近几章人写正文现炼一个。

        库放在 runtime 状态目录下（.runtime/voice-libraries/<书>/cards.jsonl），
        绝不写进全局蒸馏库。最近几章的章号变了（作者追章/删章）会自动重炼；
        只改内容不改章号不会触发——签名是统计特征，几个错别字不动大局。
        炼库失败会记进 meta，同样的章号不再重试，不能让打分路径反复吃冷启动。
        """
        if (
            project_root is None
            or not self.available
            or self._runtime_state_root is None
        ):
            return None
        root = Path(project_root).resolve()
        prose = self._prose_map(root)
        if prose is None:
            return None
        numbers = sorted(prose)[-self.VOICE_SAMPLE_CHAPTERS :]
        if len(numbers) < self.VOICE_MIN_CARDS:
            return None
        digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
        lib_dir = self._runtime_state_root / "voice-libraries" / digest
        meta_path = lib_dir / "meta.json"
        cards_path = lib_dir / "cards.jsonl"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        if (
            isinstance(meta, dict)
            and meta.get("chapters") == numbers
            and meta.get("signature_version") == self.SIGNATURE_VERSION
        ):
            if meta.get("status") == "ok" and cards_path.is_file():
                return lib_dir
            return None
        return self._build_personal_voice_library(root, lib_dir, numbers, prose)

    def _build_personal_voice_library(
        self,
        project_root: Path,
        lib_dir: Path,
        numbers: list[int],
        prose: dict[int, Path],
    ) -> Path | None:
        global_library = resolve_library(self.script, self._runtime_state_root)
        lines: list[str] = []
        for number in numbers:
            try:
                text = prose[number].read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text.strip()) < self.VOICE_MIN_CHAPTER_CHARACTERS:
                continue
            try:
                # 炼卡用全局库跑检测（library_override 直通，不会递归回个人库）。
                payload = self._run_detector(
                    text[: self.VOICE_SAMPLE_CHARACTERS],
                    library_override=global_library,
                )
            except AiSmellError:
                continue
            signature = _voice_signature(payload)
            if signature is None:
                continue
            card = {
                "card_id": f"voice-prose-book-{number:04d}",
                "card_type": "voice",
                "source": {"name": f"本书人写正文第{number}章"},
                "type_payload": {
                    "voice_prose_signature": signature,
                    "zhimeng_style_extras": _voice_style_extras(payload),
                },
            }
            lines.append(json.dumps(card, ensure_ascii=False))
        card_count = len(lines)
        status = "ok" if card_count >= self.VOICE_MIN_CARDS else "insufficient"
        # 反AI黑名单卡必须一起带走：--library 是整体替换，不带的话
        # ai_blacklist 维度会静默失效。
        blacklist_line = self._global_blacklist_line(global_library)
        if blacklist_line is not None:
            lines.append(blacklist_line)
        try:
            lib_dir.mkdir(parents=True, exist_ok=True)
            if status == "ok":
                tmp = lib_dir / "cards.jsonl.tmp"
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                replace_with_retry(tmp, lib_dir / "cards.jsonl")
            meta = {
                "status": status,
                "chapters": numbers,
                "cards": card_count,
                "project_root": str(project_root),
                "signature_version": self.SIGNATURE_VERSION,
            }
            meta_tmp = lib_dir / "meta.json.tmp"
            meta_tmp.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            replace_with_retry(meta_tmp, lib_dir / "meta.json")
        except OSError:
            return None
        return lib_dir if status == "ok" else None

    def _global_blacklist_line(self, library: Path | None) -> str | None:
        if library is None:
            return None
        cards_path = library / "cards.jsonl"
        try:
            with cards_path.open("r", encoding="utf-8-sig") as handle:
                for raw in handle:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    if f'"{self.BLACKLIST_CARD_ID}"' not in stripped:
                        continue
                    try:
                        card = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if card.get("card_id") == self.BLACKLIST_CARD_ID:
                        return stripped
        except OSError:
            return None
        return None

    def _prose_map(self, project_root: Path) -> dict[int, Path] | None:
        try:
            from zhimeng_runtime.longform_check_service import _prose_files

            return _prose_files(Path(project_root))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 偏斜指令（声纹反过来喂生成）

    def style_bias_hints(self, project_root: Path | None) -> str | None:
        """把本书声纹翻译成给模型的「写法偏斜」指令。

        外部方案（AI味深度再探）说得对：AI 味 = 每次都选概率最高的词，
        解法是把偏斜方向说具体，「写得自然点」是废话。声纹卡里本来就有
        这本书的句长/转折词/心理占比/段落/形容词实测值——反向翻译成
        指令，只报显著偏斜（均值附近不出条目，指令堆多了模型反而趋均值）。
        """
        try:
            library = self.personal_voice_library(project_root)
        except Exception:
            return None
        if library is None:
            return None
        try:
            raw_lines = (library / "cards.jsonl").read_text(encoding="utf-8")
        except OSError:
            return None
        signatures: list[dict] = []
        extras_list: list[dict] = []
        for raw in raw_lines.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                card = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if card.get("card_type") != "voice":
                continue
            payload = card.get("type_payload") or {}
            signature = payload.get("voice_prose_signature")
            if isinstance(signature, dict):
                signatures.append(signature)
                extras = payload.get("zhimeng_style_extras")
                extras_list.append(extras if isinstance(extras, dict) else {})
        if not signatures:
            return None

        def med(path: tuple[str, str]) -> float | None:
            values = [
                float(sig[path[0]][path[1]])
                for sig in signatures
                if isinstance(sig.get(path[0]), dict) and path[1] in sig[path[0]]
            ]
            return _median(values) if values else None

        hints: list[str] = []

        sentence_median = med(("sentence_length_distribution", "median"))
        if sentence_median is not None:
            if sentence_median <= 13:
                hints.append(
                    f"句子偏短：中位约 {sentence_median:.0f} 字，别写长复句。"
                )
            elif sentence_median >= 22:
                hints.append(
                    f"句子偏长：中位约 {sentence_median:.0f} 字，长句缠绕是本书常态，别硬拆成短句。"
                )

        transition_total = med(("transition_density", "total_per_1000_chars"))
        if transition_total is not None:
            if transition_total <= 0.5:
                hints.append(
                    "几乎不用「然而/但是/与此同时」这类承接转折词"
                    f"（每千字约 {transition_total:.1f} 个），句与句直接硬接。"
                )
            elif transition_total >= 3.0:
                counted: dict[str, float] = {}
                for extras in extras_list:
                    for word, value in (extras.get("transitions") or {}).items():
                        counted[word] = counted.get(word, 0.0) + float(value)
                if counted:
                    dominant = max(counted, key=lambda word: counted[word])
                    hints.append(f"转折词偏爱「{dominant}」，别换成别的套话。")

        mental = med(("action_description_preference", "mental_setup"))
        direct = med(("action_description_preference", "direct_action"))
        if mental is not None and direct is not None and mental <= 0.08 and direct >= 0.3:
            hints.append(
                f"叙事以直接动作为主（约 {direct * 100:.0f}%），心理铺垫极少"
                f"（约 {mental * 100:.0f}%）——少写内心独白和情绪总结。"
            )

        paragraph = med(("paragraph_rhythm", "avg_sentences_per_paragraph"))
        if paragraph is not None:
            if paragraph <= 1.8:
                hints.append(
                    f"段落极碎：平均每段约 {paragraph:.1f} 句，常一句一段。"
                )
            elif paragraph >= 4.5:
                hints.append(f"段落偏厚：平均每段约 {paragraph:.1f} 句，别切碎。")

        adjective = med(("adjective_stacking_rate", "rate"))
        if adjective is not None and adjective <= 0.02:
            hints.append("几乎不堆形容词，靠名词和动词说话。")

        pre_ratios = [
            float(extras["pre_tag_ratio"])
            for extras in extras_list
            if isinstance(extras.get("pre_tag_ratio"), (int, float))
        ]
        if pre_ratios:
            pre_ratio = _median(pre_ratios)
            if pre_ratio >= 0.85:
                hints.append(
                    "对白的说话人标签一律前置（「某某道：」），不用后置（「……」他说）。"
                )
            elif pre_ratio <= 0.15:
                hints.append(
                    "对白的说话人标签习惯后置（「……」他说），不用前置（「某某道：」）。"
                )

        if not hints:
            return None
        body = "\n".join(f"- {hint}" for hint in hints[:6])
        # 三探：把「去AI味」写成优化目标，模型只会换一层皮。
        # 这里只陈述这本书已经在用的习惯，不下达禁令。
        return (
            "【这本书已经在用的写法】（从人写正文实测，接着这样写）\n" + body
        )

    # ------------------------------------------------------------------
    # 打分

    def evaluate_text(
        self,
        text: str,
        *,
        genre: str | None = None,
        compare_before: str | None = None,
        project_root: Path | None = None,
    ) -> AiSmellReport:
        """给一段稿子打 AI 味分。

        传了 project_root 就用这本书的声纹库（没有会现炼，首次约 10 秒，
        之后走磁盘缓存）；炼库失败静默回退全局库——个人库任何问题都不能
        挡住打分本身。
        """
        personal: Path | None = None
        if project_root is not None:
            try:
                personal = self.personal_voice_library(project_root)
            except Exception:
                personal = None

        payload = self._run_detector(
            text,
            genre=genre,
            compare_before=compare_before,
            library_override=personal,
        )

        overall = payload.get("overall_score")
        if not isinstance(overall, dict):
            raise AiSmellError("AI 味检测结果缺少 overall_score")

        components = overall.get("components")
        ranked: list[tuple[str, float]] = []
        if isinstance(components, dict):
            numeric = [
                (str(name), float(value))
                for name, value in components.items()
                if isinstance(value, (int, float))
            ]
            ranked = sorted(numeric, key=lambda item: item[1], reverse=True)[:3]

        drift_payload = payload.get("deai_drift")
        drift: dict | None = None
        if isinstance(drift_payload, dict):
            drift = {
                "risk_level": str(drift_payload.get("risk_level", "none")),
                "reasons": list(drift_payload.get("reasons") or []),
                "failure_signature": str(drift_payload.get("failure_signature", "")),
                "length_budget_status": str(
                    drift_payload.get("length_budget_status", "")
                ),
            }

        worst: list[dict] = []
        for item in payload.get("top_ai_smell_sentences") or []:
            if not isinstance(item, dict):
                continue
            sentence = str(item.get("sentence") or item.get("text") or "").strip()
            if not sentence:
                continue
            reasons = item.get("reasons") or item.get("tags") or []
            worst.append(
                {
                    "sentence": sentence[:120],
                    "reasons": [str(reason)[:40] for reason in list(reasons)[:3]],
                }
            )
            if len(worst) >= 3:
                break

        if personal is not None:
            voice_reference = "personal"
        elif resolve_library(self.script, self._runtime_state_root) is not None:
            voice_reference = "global"
        else:
            voice_reference = "none"

        return AiSmellReport(
            score=float(overall.get("score", 0.0)),
            level=str(overall.get("level", "unknown")),
            content_hash=str(payload.get("content_hash", "")),
            detector_version=str((payload.get("detector") or {}).get("version", "")),
            top_components=tuple(ranked),
            deai_drift=drift,
            worst_sentences=tuple(worst),
            sample_characters=len(text),
            voice_reference=voice_reference,
        )

    def _run_detector(
        self,
        text: str,
        *,
        genre: str | None = None,
        compare_before: str | None = None,
        library_override: Path | None = None,
    ) -> dict:
        script = self.script
        if script is None:
            raise AiSmellUnavailable(
                "没找到 AI 味检测脚本 detect_ai_smell.py。"
                "可设置环境变量 ZHIMENG_AI_SMELL_SCRIPT 指向它。"
            )

        with tempfile.TemporaryDirectory(prefix="zhimeng-ai-smell-") as workspace:
            draft = Path(workspace) / "draft.md"
            draft.write_text(text, encoding="utf-8")
            output = Path(workspace) / "report.json"

            command = [
                sys.executable,
                str(script),
                str(draft),
                "--top",
                "3",
                "--output",
                str(output),
            ]
            if genre:
                command += ["--genre", genre]
            if compare_before is not None and compare_before.strip():
                # 防「去AI改过头」：对比改前稿，检测器会产出 deai_drift
                # （短句碎片率、句长、节奏谱的恶化告警）。
                before = Path(workspace) / "before.md"
                before.write_text(compare_before, encoding="utf-8")
                command += ["--compare-before", str(before)]
            library = library_override or resolve_library(
                script, self._runtime_state_root
            )
            if library is not None:
                command += ["--library", str(library)]

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self._timeout,
                    # 检测器只读稿子、算规则分，不需要网络也不该继承调用者的密钥
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if not k.startswith(("ZHIMENG_AGENT_PROVIDER", "ANTHROPIC_", "OPENAI_"))
                    },
                )
            except subprocess.TimeoutExpired as exc:
                raise AiSmellError("AI 味检测超时") from exc
            except OSError as exc:
                raise AiSmellError(f"无法运行 AI 味检测脚本: {exc}") from exc

            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace")[:300]
                raise AiSmellError(f"AI 味检测失败（退出码 {completed.returncode}）: {detail}")

            try:
                return json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AiSmellError("AI 味检测没有产出可解析的结果") from exc
