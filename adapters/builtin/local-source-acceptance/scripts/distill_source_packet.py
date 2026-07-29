#!/usr/bin/env python3
"""Create auditable draft distillation cards from a prepared source packet.

This script intentionally does **not** replace human/model distillation.  Its
job is to turn the ``source packet -> distillation cards`` handoff from a purely
natural-language step into a typed, inspectable artifact:

- read ``packet.manifest.json`` plus text/metadata artifacts;
- emit a conservative ``cards.draft.json`` containing schema-shaped candidate
  cards with evidence scopes and contamination boundaries;
- emit ``distillation_report.md`` and a JSON tool-result envelope.

The generated cards are marked as draft candidates.  They can be reviewed,
edited, and then added with ``distill_kb.py add --no-strict`` when appropriate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


MAX_SAMPLE_CHARS = 1200
CHAPTER_HEADING_RE = re.compile(
    r"^\s*(第[0-9一二三四五六七八九十百千万零〇两]+章[^\n\r]*|chapter\s+\d+[^\n\r]*)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
SMALL_CHUNK_TARGET = 700
SMALL_CHUNK_OVERLAP = 50
SUPER_CHAPTER_GROUP_SIZE = 12
FEATURE_KEYWORD_MAP: dict[str, list[str]] = {
    "reward": ["奖励", "名额", "资格", "突破", "机缘", "资源", "灵石", "积分", "权限", "回报"],
    "suspense": ["秘密", "异样", "线索", "追查", "疑点", "真相", "不对劲", "异常", "谜", "伏笔"],
    "oppression": ["压", "逼", "罚", "追杀", "规矩", "门槛", "威胁", "看不起", "剥夺", "拦"],
    "humor": ["笑", "乐", "吐槽", "调侃", "讥", "滑稽", "尴尬", "逗", "玩笑"],
    "world": ["宗门", "王朝", "学院", "基地市", "禁区", "秘境", "规矩", "制度", "势力", "地图"],
    "relation": ["师兄", "师姐", "朋友", "仇", "债", "家族", "同门", "敌", "盟", "长辈"],
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_text(path: Path, limit: int | None = None) -> str:
    data = path.read_text(encoding="utf-8", errors="replace")
    return data if limit is None else data[:limit]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def safe_slug(value: str, fallback: str = "source") -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value).strip("-_")
    slug = slug[:48].strip("-_")
    return slug or fallback


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_packet_manifest(packet: Path) -> Path:
    if packet.is_file():
        return packet
    candidate = packet / "packet.manifest.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"packet manifest not found: {packet}")


def resolve_artifact_path(raw: str, packet_dir: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (packet_dir / path).resolve()


def artifact_by_kind(manifest: dict[str, Any], packet_dir: Path, kind: str) -> Path | None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") == kind and item.get("path"):
            path = resolve_artifact_path(str(item["path"]), packet_dir)
            if path.exists():
                return path
    return None


def infer_text_path(manifest: dict[str, Any], packet_dir: Path) -> Path | None:
    by_kind = artifact_by_kind(manifest, packet_dir, "packet_text")
    if by_kind:
        return by_kind
    candidates = sorted(packet_dir.glob("*.txt"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    return candidates[0] if candidates else None


def infer_metadata_path(manifest: dict[str, Any], packet_dir: Path) -> Path | None:
    by_kind = artifact_by_kind(manifest, packet_dir, "packet_metadata")
    if by_kind:
        return by_kind
    candidates = sorted(packet_dir.glob("*.metadata.json"))
    return candidates[0] if candidates else None


def list_terms(text: str, mapping: dict[str, list[str]], default: list[str]) -> list[str]:
    hits: list[str] = []
    lowered = text.casefold()
    for label, terms in mapping.items():
        if any(term.casefold() in lowered for term in terms):
            hits.append(label)
    return hits or default


def sentence_lengths(text: str) -> list[int]:
    sentences = re.split(r"[。！？!?；;\n]+", text)
    return [len(s.strip()) for s in sentences if s.strip()]


def split_chapters(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return [
            {
                "chapter_id": "CH0001",
                "title": "全文",
                "heading_source": "implicit_full_text",
                "char_start": 0,
                "char_end": 0,
                "char_count": 0,
                "text": "",
                "text_sha256": sha256_text(""),
            }
        ]

    chapters: list[dict[str, Any]] = []
    current_title = ""
    current_lines: list[str] = []
    current_start = 0
    current_heading_source = "implicit_pre_heading"
    position = 0

    def flush(end_position: int) -> None:
        nonlocal current_lines, current_title, current_start, current_heading_source
        if not current_lines:
            return
        chapter_text = "".join(current_lines)
        chapter_number = len(chapters) + 1
        title = current_title or f"CH{chapter_number:04d}"
        chapters.append(
            {
                "chapter_id": f"CH{chapter_number:04d}",
                "title": title,
                "heading_source": current_heading_source,
                "char_start": current_start,
                "char_end": end_position,
                "char_count": max(0, end_position - current_start),
                "text": chapter_text,
                "text_sha256": sha256_text(chapter_text),
            }
        )
        current_lines = []
        current_title = ""
        current_heading_source = "implicit_pre_heading"

    for raw_line in lines:
        stripped = raw_line.strip()
        match = CHAPTER_HEADING_RE.match(stripped)
        if match:
            flush(position)
            current_title = stripped
            current_heading_source = "explicit_heading"
            current_start = position
            current_lines = [raw_line]
        else:
            if not current_lines:
                current_title = "全文起始"
                current_heading_source = "implicit_pre_heading"
                current_start = position
                current_lines = []
            current_lines.append(raw_line)
        position += len(raw_line)
    flush(position)

    if not chapters:
        return [
            {
                "chapter_id": "CH0001",
                "title": "全文",
                "heading_source": "implicit_full_text",
                "char_start": 0,
                "char_end": len(text),
                "char_count": len(text),
                "text": text,
                "text_sha256": sha256_text(text),
            }
        ]
    return chapters


def overlapping_chapter_ids(chapters: list[dict[str, Any]], char_start: int, char_end: int) -> list[str]:
    return [
        str(chapter["chapter_id"])
        for chapter in chapters
        if char_start < int(chapter["char_end"]) and char_end > int(chapter["char_start"])
    ]


def build_chunk_indexes(text: str, chapters: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    text_length = len(text)
    small_chunks: list[dict[str, Any]] = []
    if text_length <= SMALL_CHUNK_TARGET:
        small_chunks.append(
            {
                "chunk_id": "small-0001",
                "level": "small",
                "char_start": 0,
                "char_end": text_length,
                "char_count": text_length,
                "chapter_ids": [str(chapter["chapter_id"]) for chapter in chapters],
                "chunk_sha256": sha256_text(text),
                "overlap_previous_chars": 0,
            }
        )
    else:
        start = 0
        chunk_number = 1
        while start < text_length:
            end = min(text_length, start + SMALL_CHUNK_TARGET)
            chunk_text = text[start:end]
            small_chunks.append(
                {
                    "chunk_id": f"small-{chunk_number:04d}",
                    "level": "small",
                    "char_start": start,
                    "char_end": end,
                    "char_count": max(0, end - start),
                    "chapter_ids": overlapping_chapter_ids(chapters, start, end),
                    "chunk_sha256": sha256_text(chunk_text),
                    "overlap_previous_chars": 0 if chunk_number == 1 else SMALL_CHUNK_OVERLAP,
                }
            )
            if end >= text_length:
                break
            next_start = max(0, end - SMALL_CHUNK_OVERLAP)
            if next_start <= start:
                next_start = end
            start = next_start
            chunk_number += 1

    big_chunks = [
        {
            "chunk_id": f"big-{index:04d}",
            "level": "big",
            "chapter_id": str(chapter["chapter_id"]),
            "chapter_title": str(chapter["title"]),
            "char_start": int(chapter["char_start"]),
            "char_end": int(chapter["char_end"]),
            "char_count": int(chapter["char_count"]),
            "chapter_ids": [str(chapter["chapter_id"])],
            "chunk_sha256": str(chapter["text_sha256"]),
        }
        for index, chapter in enumerate(chapters, start=1)
    ]

    super_chunks: list[dict[str, Any]] = []
    for start_index in range(0, len(chapters), SUPER_CHAPTER_GROUP_SIZE):
        group = chapters[start_index : start_index + SUPER_CHAPTER_GROUP_SIZE]
        if not group:
            continue
        char_start = int(group[0]["char_start"])
        char_end = int(group[-1]["char_end"])
        chunk_text = text[char_start:char_end]
        super_chunks.append(
            {
                "chunk_id": f"super-{len(super_chunks) + 1:04d}",
                "level": "super",
                "chapter_ids": [str(chapter["chapter_id"]) for chapter in group],
                "chapter_titles": [str(chapter["title"]) for chapter in group],
                "chapter_span": f"{group[0]['chapter_id']}..{group[-1]['chapter_id']}",
                "char_start": char_start,
                "char_end": char_end,
                "char_count": max(0, char_end - char_start),
                "chunk_sha256": sha256_text(chunk_text),
            }
        )
    return {"small": small_chunks, "big": big_chunks, "super": super_chunks}


def chapter_feature_row(chapter: dict[str, Any]) -> dict[str, Any]:
    chapter_text = str(chapter.get("text") or "")
    lowered = chapter_text.casefold()
    lengths = sentence_lengths(chapter_text)
    avg_sentence_len = round(sum(lengths) / max(1, len(lengths)), 2)
    short_ratio = round(sum(1 for size in lengths if size <= 12) / max(1, len(lengths)), 3)
    feature_counts = {
        feature: sum(lowered.count(term.casefold()) for term in terms)
        for feature, terms in FEATURE_KEYWORD_MAP.items()
    }
    feature_density = {
        feature: round(count / max(1, int(chapter.get("char_count") or 0)) * 1000, 3)
        for feature, count in feature_counts.items()
    }
    strongest_features = [
        feature
        for feature, _ in sorted(feature_counts.items(), key=lambda item: (-item[1], item[0]))
        if feature_counts[feature] > 0
    ][:3]
    return {
        "chapter_id": str(chapter["chapter_id"]),
        "chapter_title": str(chapter["title"]),
        "heading_source": str(chapter.get("heading_source") or ""),
        "char_count": int(chapter["char_count"]),
        "sentence_count": len(lengths),
        "avg_sentence_len": avg_sentence_len,
        "short_sentence_ratio": short_ratio,
        "feature_counts": feature_counts,
        "feature_density_per_1k_chars": feature_density,
        "strongest_features": strongest_features,
        "chapter_sha256": str(chapter["text_sha256"]),
    }


def build_chapter_features(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [chapter_feature_row(chapter) for chapter in chapters]


def build_metrics(profile: dict[str, Any], chapters: list[dict[str, Any]], chunk_indexes: dict[str, list[dict[str, Any]]], chapter_features: list[dict[str, Any]]) -> dict[str, Any]:
    chapter_sizes = [int(chapter["char_count"]) for chapter in chapters]
    feature_summary: dict[str, dict[str, float]] = {}
    for feature in FEATURE_KEYWORD_MAP:
        counts = [int(row["feature_counts"].get(feature, 0)) for row in chapter_features]
        densities = [float(row["feature_density_per_1k_chars"].get(feature, 0.0)) for row in chapter_features]
        feature_summary[feature] = {
            "total_count": sum(counts),
            "avg_density_per_1k_chars": round(sum(densities) / max(1, len(densities)), 3),
            "max_density_per_1k_chars": round(max(densities) if densities else 0.0, 3),
        }
    explicit_heading_count = sum(1 for chapter in chapters if chapter.get("heading_source") == "explicit_heading")
    return {
        "generated_at": now_iso(),
        "source_profile": {
            "title": profile["title"],
            "platform": profile["platform"],
            "audience": profile["audience"],
            "genre": profile["genre"],
        },
        "chapter_stats": {
            "chapter_count": len(chapters),
            "explicit_heading_count": explicit_heading_count,
            "avg_chapter_chars": round(sum(chapter_sizes) / max(1, len(chapter_sizes)), 2),
            "max_chapter_chars": max(chapter_sizes) if chapter_sizes else 0,
            "min_chapter_chars": min(chapter_sizes) if chapter_sizes else 0,
        },
        "chunk_counts": {level: len(rows) for level, rows in chunk_indexes.items()},
        "opening_surface": {
            "avg_sentence_len": profile["avg_sentence_len_opening"],
            "short_sentence_ratio": profile["short_sentence_ratio_opening"],
            "text_chars": profile["text_chars"],
        },
        "feature_density_summary": feature_summary,
        "artifact_contract_checks": {
            "chapter_index_ready": len(chapters) > 0,
            "chunk_indexes_ready": all(chunk_indexes.get(level) for level in ["small", "big", "super"]),
            "chapter_features_ready": len(chapter_features) == len(chapters),
            "validation_report_required": True,
        },
    }


def build_source_index(
    manifest_path: Path,
    text_path: Path,
    metadata_path: Path | None,
    profile: dict[str, Any],
    chapters: list[dict[str, Any]],
    chunk_indexes: dict[str, list[dict[str, Any]]],
    metrics_path: Path,
    chapter_features_path: Path,
) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "packet_manifest": str(manifest_path),
        "packet_text": str(text_path),
        "packet_metadata": str(metadata_path) if metadata_path else "",
        "source_title": profile["title"],
        "source_platform": profile["platform"],
        "source_audience": profile["audience"],
        "source_genre": profile["genre"],
        "chapter_count": len(chapters),
        "chunk_counts": {level: len(rows) for level, rows in chunk_indexes.items()},
        "metrics_path": str(metrics_path),
        "chapter_features_path": str(chapter_features_path),
        "chapters": [
            {
                "chapter_id": str(chapter["chapter_id"]),
                "chapter_title": str(chapter["title"]),
                "heading_source": str(chapter["heading_source"]),
                "char_start": int(chapter["char_start"]),
                "char_end": int(chapter["char_end"]),
                "char_count": int(chapter["char_count"]),
                "chapter_sha256": str(chapter["text_sha256"]),
            }
            for chapter in chapters
        ],
    }


def validation_status_for_profile(profile: dict[str, Any], chapters: list[dict[str, Any]], chunk_indexes: dict[str, list[dict[str, Any]]], chapter_features: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    explicit_heading_count = sum(1 for chapter in chapters if chapter.get("heading_source") == "explicit_heading")
    return {
        "internal_consistency": {
            "status": "pass" if len(chapters) == len(chapter_features) and all(chunk_indexes.get(level) for level in ["small", "big", "super"]) else "warn",
            "evidence": [
                f"chapters={len(chapters)}",
                f"chapter_features={len(chapter_features)}",
                f"chunk_counts={ {level: len(rows) for level, rows in chunk_indexes.items()} }",
            ],
        },
        "predictive_validation": {
            "status": "warn",
            "evidence": [
                "draft artifact only; use chapter_features/chunk indexes to review whether inferred route matches real chapters before promotion."
            ],
        },
        "blind_sample_validation": {
            "status": "warn",
            "evidence": [
                "reviewer should sample chunk_index_small.jsonl / chapter_features.jsonl without loading long prose before promoting cards."
            ],
        },
        "extreme_scenario_stress_test": {
            "status": "pass" if explicit_heading_count > 0 or profile["text_chars"] <= SMALL_CHUNK_TARGET else "warn",
            "evidence": [
                f"explicit_heading_count={explicit_heading_count}",
                f"text_chars={profile['text_chars']}",
            ],
        },
    }


def markdown_validation_report(
    profile: dict[str, Any],
    validation_statuses: dict[str, dict[str, Any]],
    metrics: dict[str, Any],
    source_index_path: Path,
    chunk_paths: dict[str, Path],
    chapter_features_path: Path,
) -> str:
    lines = [
        "# Source Packet Validation Report",
        "",
        f"- generated_at: `{now_iso()}`",
        f"- source_title: `{profile['title']}`",
        f"- source_index: `{source_index_path}`",
        f"- chapter_features: `{chapter_features_path}`",
        f"- chunk_index_small: `{chunk_paths['small']}`",
        f"- chunk_index_big: `{chunk_paths['big']}`",
        f"- chunk_index_super: `{chunk_paths['super']}`",
        "",
        "## Artifact Contract Checks",
        "",
        f"- chapter_count: `{metrics['chapter_stats']['chapter_count']}`",
        f"- explicit_heading_count: `{metrics['chapter_stats']['explicit_heading_count']}`",
        f"- chunk_counts: `{metrics['chunk_counts']}`",
        f"- opening_avg_sentence_len: `{metrics['opening_surface']['avg_sentence_len']}`",
        f"- opening_short_sentence_ratio: `{metrics['opening_surface']['short_sentence_ratio']}`",
        "",
        "## Validation Ladder (draft)",
        "",
    ]
    for step, payload in validation_statuses.items():
        lines.append(f"- `{step}` → `{payload.get('status', 'warn')}`")
        for evidence in payload.get("evidence", []):
            lines.append(f"  - {evidence}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- 这些索引和特征只证明 source packet 已进入可审计草案阶段，不等于正式机制蒸馏完成。",
            "- chunk/chapter artifacts 不保存长原文，只保存跨度、hash、统计和验证入口。",
            "- 正式 promoted 卡仍需人工/模型把 pending 机制补实，并通过 validate/health。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def source_profile(manifest: dict[str, Any], metadata: dict[str, Any], text: str) -> dict[str, Any]:
    title = (
        str(metadata.get("title") or "")
        or str(metadata.get("book_name") or "")
        or str(manifest.get("packet_label") or "")
        or "未命名来源"
    )
    author = str(metadata.get("author") or metadata.get("writer") or "")
    backend = str(manifest.get("source_backend") or "")
    platform = ["番茄"] if ("tomato" in backend.lower() or "番茄" in text[:2000]) else ["未知平台"]
    genre_terms = {
        "玄幻": ["玄幻", "灵气", "血脉", "神通", "古族", "秘境", "天骄"],
        "仙侠": ["修仙", "仙侠", "炼气", "筑基", "金丹", "灵根", "宗门", "丹药", "法宝"],
        "系统流": ["系统", "面板", "任务", "奖励", "属性", "签到", "熟练度"],
        "高武": ["高武", "气血", "武考", "武馆", "异兽", "基地市", "前线"],
        "都市": ["都市", "公司", "学校", "医院", "警察", "直播"],
        "历史": ["大唐", "皇帝", "朝堂", "县令", "边军", "公主"],
        "悬疑": ["尸体", "凶手", "线索", "案子", "侦查", "诡异"],
    }
    genres = list_terms(text[:20000] + title, genre_terms, ["待分类"])
    audience = ["男频"] if any(g in genres for g in ["玄幻", "仙侠", "系统流", "高武", "历史"]) else ["待判断"]
    chapter_markers = re.findall(r"(?:^|\n)\s*(?:第[0-9一二三四五六七八九十百千万零〇]+章|chapter\s+\d+)", text, flags=re.I)
    sentences = re.split(r"[。！？!?；;\n]+", text[:20000])
    sentence_lengths = [len(s.strip()) for s in sentences if s.strip()]
    avg_sentence_len = round(sum(sentence_lengths) / max(1, len(sentence_lengths)), 2)
    short_ratio = round(sum(1 for n in sentence_lengths if n <= 12) / max(1, len(sentence_lengths)), 3)
    return {
        "title": title,
        "author": author,
        "platform": platform,
        "genre": genres[:4],
        "audience": audience,
        "chapter_marker_count": len(chapter_markers),
        "text_chars": len(text),
        "avg_sentence_len_opening": avg_sentence_len,
        "short_sentence_ratio_opening": short_ratio,
    }


def base_card_fields(profile: dict[str, Any], manifest: dict[str, Any], text_sha: str, card_id: str, card_type: str, title: str) -> dict[str, Any]:
    route_terms = [*profile["platform"], *profile["audience"], *profile["genre"], "source_packet", "draft_candidate"]
    return {
        "card_id": card_id,
        "card_type": card_type,
        "title": title,
        "source": {
            "name": profile["title"],
            "author": profile.get("author", ""),
            "scope": "source packet draft scan; opening/statistical evidence only",
            "evidence_quality": "weak",
            "notes": f"packet_label={manifest.get('packet_label','')}; text_sha256={text_sha}; draft candidate requires human/model distillation review before formal use.",
        },
        "taxonomy": {
            "platform": profile["platform"],
            "audience": profile["audience"],
            "genre": profile["genre"],
            "chapter_role": ["source_intake"],
            "scene_function": ["opening_scan"],
            "emotion": ["待验证"],
            "craft": ["source_packet_handoff"],
            "content_boundary": ["不复刻原文", "不复制设定壳"],
        },
        "tags": route_terms[:10] + ["机制候选", "防污染"],
        "keywords": route_terms[:8],
        "applicable_tasks": ["distill_review", "coverage_seed", "chapter_brief", "review"],
        "reader_promise": "只提供蒸馏入口候选和待验证问题，不直接作为写作模仿依据。",
        "reader_effect": {
            "author_intent": "unknown_until_review",
            "observed_reader_effect": "unknown_until_review",
            "effect_evidence": ["source packet metadata and opening statistics only"],
            "effect_failure_boundary": "If used as a final mechanism card without review, it may become formulaic or contaminated.",
            "misread_risk": "Treating draft candidates as validated source cards.",
            "when_it_turns_bad": "When source names, setting shells, scene order, or prose rhythm are copied into a project.",
        },
        "mechanics": [
            {
                "name": "source_packet_handoff_candidate",
                "use_when": "When a prepared source packet needs a typed draft artifact before human/model distillation.",
                "rule": "Generate conservative candidate fields and evidence scopes first; promote only after review validates transferable mechanisms.",
                "failure_mode": "Using packet statistics as if they were proven craft mechanisms.",
            }
        ],
        "narrative_engines": {
            "worldview": "pending",
            "desire": "pending",
            "conflict": "pending",
            "progression": "pending",
            "suspense": "pending",
            "emotion": "pending",
            "rhythm": "pending",
        },
        "style_dna": {
            "pov_distance": "pending",
            "sentence_rhythm": f"opening_avg={profile['avg_sentence_len_opening']}; short_ratio={profile['short_sentence_ratio_opening']}",
            "dialogue_mode": "pending",
            "description_mode": "pending",
            "do_not_copy": ["source prose", "signature phrases", "recognizable scene order"],
        },
        "validation_tests": [
            "Human/model review must replace pending fields with transferable mechanisms before formal retrieval.",
            "Anti-contamination review must confirm no source entity, setting shell, scene order, or signature phrase is used.",
        ],
        "validation_ladder": {
            "internal_consistency": {"status": "warn", "evidence": ["draft card generated from packet manifest"], "notes": "review required"},
            "predictive_validation": {"status": "warn", "evidence": ["not yet validated"], "notes": "review required"},
            "blind_sample_validation": {"status": "warn", "evidence": ["not yet validated"], "notes": "review required"},
            "extreme_scenario_stress_test": {"status": "warn", "evidence": ["not yet validated"], "notes": "review required"},
        },
        "scores": {"evidence": 0.3, "specificity": 0.35, "generativity": 0.3, "retrieval": 0.25, "safety_risk": 0.2},
        "usage": {"use_count": 0, "success_count": 0, "fail_count": 0, "rejected_count": 0, "mixed_count": 0},
        "retrieval": {
            "preferred_queries": [" ".join(route_terms[:6])],
            "negative_queries": ["正式写作 直接模仿 原文复刻"],
        },
        "trigger": {
            "query_terms": route_terms,
            "profiles": ["review", "draft_assist"],
            "applicable_tasks": ["distill_review"],
            "taxonomy_terms": {"genre": profile["genre"], "platform": profile["platform"]},
            "notes": "Draft-only route seed; use for distillation review, not final writing.",
        },
        "avoid_when": {
            "query_terms": ["正式对标", "模仿文风", "照着写", "直接起稿"],
            "profiles": ["benchmark"],
            "applicable_tasks": ["draft_scene"],
            "taxonomy_terms": {},
            "notes": "Not a validated benchmark or voice card.",
        },
        "priority": "low",
        "evidence_gate": {
            "min_scores": {"evidence": 0.2, "specificity": 0.2, "retrieval": 0.1, "safety_risk_max": 0.45},
            "allowed_source_quality": ["weak", "medium", "strong"],
            "block_on_rejected_contamination": True,
            "notes": "Draft card only; formal use requires promoted replacement.",
        },
        "nlcl_route_hints": {
            "recommended_profiles": ["review"],
            "nlcl_layers": ["distillation_index"],
            "route_terms": route_terms,
            "hard_filters": {"genre": profile["genre"]},
            "negative_routes": ["source imitation", "author voice cloning"],
            "safety_summary": "Draft candidate: do not copy source prose, names, setting shells, scene order, or voice fingerprints.",
            "feedback_default_layer": "L3_DISTILLATION_RETRIEVAL",
        },
        "draft_candidate": True,
        "requires_human_distillation": True,
        "safety": {
            "copyright_boundary": "abstract mechanisms only; draft candidate stores no long source prose",
            "do_not_reuse": ["source names", "source setting shells", "scene order", "signature phrases", "author voice"],
            "source_entity_names": [profile["title"]] if profile["title"] else [],
            "source_setting_shells": [],
            "signature_phrases": [],
            "contamination_risks": ["source_shell", "entity_leakage", "voice_overfit"],
        },
    }


def build_cards(profile: dict[str, Any], manifest: dict[str, Any], text_sha: str) -> list[dict[str, Any]]:
    seed = f"{safe_slug(profile['title'], 'packet')}-{text_sha[:8]}-draft"
    work = base_card_fields(profile, manifest, text_sha, f"work-{seed}", "work", f"{profile['title']} · source packet draft work card")
    work["summary"] = "Source packet draft work card: captures packet scope and review questions before formal distillation."
    work["type_payload"] = {
        "premise_pattern": "pending_human_distillation",
        "core_reader_promise": "pending_human_distillation",
        "opening_contract": "Use packet opening as evidence scope only; do not copy prose or scene order.",
        "progression_map": ["pending: identify visible pressure", "pending: identify payoff/debt loop"],
        "recurring_scene_loops": ["pending: review repeated chapter functions"],
        "transfer_limits": ["draft candidate", "no source names", "no setting shell", "no scene order"],
    }

    voice = base_card_fields(profile, manifest, text_sha, f"voice-{seed}", "voice", f"{profile['title']} · source packet draft voice audit")
    voice["summary"] = "Draft voice audit: records only safe readability statistics and review questions, not author voice imitation."
    voice["taxonomy"]["style"] = ["待验证"]
    voice["taxonomy"]["pov"] = ["待验证"]
    voice["type_payload"] = {
        "pov_distance": "pending_review",
        "sentence_rhythm": f"opening_avg={profile['avg_sentence_len_opening']}; short_ratio={profile['short_sentence_ratio_opening']}",
        "diction_pool": ["pending_review"],
        "dialogue_behavior": "pending_review",
        "description_behavior": "pending_review",
        "emotion_handling": "pending_review",
        "human_web_prose": "Review whether prose is naturally readable, semantically complete, and scene-anchored before making a real voice card.",
        "sentence_variety": "Do not infer style from one short statistic; use only as a warning against over-shortening.",
        "scene_anchor_rules": ["Find actions/resources/body reactions that change reader judgment."],
        "dialogue_pressure_rules": ["Check whether dialogue carries desire, relationship stance, or information pressure."],
        "anti_ai_failure_signatures": ["over-shortening", "semantic fragments", "report voice", "same-voice dialogue"],
        "readability_acceptance_tests": ["2000+ character human read-through required before promotion."],
        "do_not_copy": ["author voice", "phrases", "rhythm chain", "jokes", "口头禅"],
    }

    validator = base_card_fields(profile, manifest, text_sha, f"validator-{seed}", "validator", f"{profile['title']} · source packet promotion gate")
    validator["summary"] = "Validator for promoting draft packet cards: blocks direct use until mechanism, evidence, safety and transferability are reviewed."
    validator["taxonomy"]["problem_symptom"] = ["draft_card_used_as_validated"]
    validator["type_payload"] = {
        "validator_name": "source_packet_draft_promotion_gate",
        "applies_to": ["source packet draft cards", "distillation review", "NLCL import"],
        "layer_id": "L3_DISTILLATION_RETRIEVAL",
        "checks": [
            "mechanism fields no longer pending",
            "evidence scope is explicit",
            "anti-contamination boundary is explicit",
            "reader effect has at least one source-backed inference",
        ],
        "reject_conditions": [
            "draft_candidate remains true for formal writing",
            "source names or setting shells appear in project rules",
            "card instructs imitation of prose or author voice",
        ],
        "rewrite_routes": ["L3_DISTILLATION_RETRIEVAL", "manual/model distillation review"],
    }
    return [work, voice, validator]


def markdown_report(
    profile: dict[str, Any],
    manifest_path: Path,
    text_path: Path | None,
    cards_path: Path,
    source_index_path: Path,
    metrics_path: Path,
    chapter_features_path: Path,
    chunk_paths: dict[str, Path],
    validation_report_path: Path,
) -> str:
    lines = [
        "# Source Packet Draft Distillation Report",
        "",
        f"- generated_at: `{now_iso()}`",
        f"- manifest: `{manifest_path}`",
        f"- text_path: `{text_path or ''}`",
        f"- source_title: `{profile['title']}`",
        f"- platform: `{', '.join(profile['platform'])}`",
        f"- audience: `{', '.join(profile['audience'])}`",
        f"- inferred_genre: `{', '.join(profile['genre'])}`",
        f"- text_chars: `{profile['text_chars']}`",
        f"- chapter_marker_count: `{profile['chapter_marker_count']}`",
        f"- opening_avg_sentence_len: `{profile['avg_sentence_len_opening']}`",
        f"- opening_short_sentence_ratio: `{profile['short_sentence_ratio_opening']}`",
        f"- cards_draft: `{cards_path}`",
        f"- source_index: `{source_index_path}`",
        f"- metrics: `{metrics_path}`",
        f"- chapter_features: `{chapter_features_path}`",
        f"- chunk_index_small: `{chunk_paths['small']}`",
        f"- chunk_index_big: `{chunk_paths['big']}`",
        f"- chunk_index_super: `{chunk_paths['super']}`",
        f"- validation_report: `{validation_report_path}`",
        "",
        "## Boundary",
        "",
        "- This is a draft handoff artifact, not completed distillation.",
        "- It stores no long source prose and does not authorize style imitation.",
        "- Promote only after a reviewer fills transferable mechanisms, reader effects, validation tests and contamination boundaries.",
        "",
        "## Added Artifact Surface",
        "",
        "- `source_index.json`：packet 输入、章节索引、chunk 计数和 artifact 路径。",
        "- `metrics.json`：开篇句长、章节规模和 feature density 摘要。",
        "- `chapter_features.jsonl`：按章节落盘的统计/feature 行，不存长原文。",
        "- `chunk_index_small/big/super.jsonl`：small/big/super 三层 chunk 索引，用于后续蒸馏审查、coverage 和 blind review。",
        "- `validation_report.md`：draft 级 validation ladder 与 artifact contract 检查。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = resolve_packet_manifest(Path(args.packet))
    packet_dir = manifest_path.parent
    manifest = read_json(manifest_path)
    text_path = infer_text_path(manifest, packet_dir)
    metadata_path = infer_metadata_path(manifest, packet_dir)
    if not text_path:
        return {
            "tool_result_envelope": "novel_distillation.source_packet_draft.v1",
            "status": "needs_attention",
            "success_criteria": {
                "required_status": "draft_ready",
                "required_output_artifacts": [
                    "cards_draft_json",
                    "distillation_report_md",
                    "source_index_json",
                    "metrics_json",
                    "chapter_features_jsonl",
                    "chunk_index_small_jsonl",
                    "chunk_index_big_jsonl",
                    "chunk_index_super_jsonl",
                    "validation_report_md",
                    "tool_result_envelope_json",
                ],
                "required_checkpoints": [
                    "packet_manifest_resolved",
                    "packet_text_present",
                    "draft_cards_written",
                    "artifact_contract_complete",
                ],
                "ready_for_formal_ingest": False,
            },
            "failure_codes": ["packet_text_missing"],
            "input_artifacts": {
                "packet_manifest": str(manifest_path),
                "packet_text": "",
                "packet_metadata": str(metadata_path) if metadata_path else "",
            },
            "output_artifacts": {},
            "warnings": ["No packet_text artifact or .txt file found."],
            "checkpoints": {
                "packet_manifest_resolved": True,
                "packet_text_present": False,
                "draft_cards_written": False,
                "artifact_contract_complete": False,
            },
            "fallback_manual_step": "Repair source packet text artifact or rerun prepare_downloaded_sources/prepare_tomato_sources before drafting distillation cards.",
            "next_action": "Repair source packet before drafting distillation cards.",
        }
    text = read_text(text_path)
    metadata = read_json(metadata_path) if metadata_path else {}
    text_sha = sha256_file(text_path)
    profile = source_profile(manifest, metadata, text)
    chapters = split_chapters(text)
    chunk_indexes = build_chunk_indexes(text, chapters)
    chapter_features = build_chapter_features(chapters)
    cards = build_cards(profile, manifest, text_sha)
    out_dir = Path(args.out_dir) if args.out_dir else (packet_dir / "distillation_draft")
    out_dir.mkdir(parents=True, exist_ok=True)
    cards_path = out_dir / "cards.draft.json"
    report_path = out_dir / "distillation_report.md"
    source_index_path = out_dir / "source_index.json"
    metrics_path = out_dir / "metrics.json"
    chapter_features_path = out_dir / "chapter_features.jsonl"
    chunk_paths = {
        "small": out_dir / "chunk_index_small.jsonl",
        "big": out_dir / "chunk_index_big.jsonl",
        "super": out_dir / "chunk_index_super.jsonl",
    }
    validation_report_path = out_dir / "validation_report.md"
    envelope_path = out_dir / "tool_result_envelope.json"
    metrics = build_metrics(profile, chapters, chunk_indexes, chapter_features)
    source_index = build_source_index(
        manifest_path,
        text_path,
        metadata_path,
        profile,
        chapters,
        chunk_indexes,
        metrics_path,
        chapter_features_path,
    )
    validation_statuses = validation_status_for_profile(profile, chapters, chunk_indexes, chapter_features)
    write_json(cards_path, cards)
    write_json(source_index_path, source_index)
    write_json(metrics_path, metrics)
    write_jsonl(chapter_features_path, chapter_features)
    for level, rows in chunk_indexes.items():
        write_jsonl(chunk_paths[level], rows)
    validation_report_path.write_text(
        markdown_validation_report(
            profile,
            validation_statuses,
            metrics,
            source_index_path,
            chunk_paths,
            chapter_features_path,
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        markdown_report(
            profile,
            manifest_path,
            text_path,
            cards_path,
            source_index_path,
            metrics_path,
            chapter_features_path,
            chunk_paths,
            validation_report_path,
        ),
        encoding="utf-8",
    )
    envelope = {
        "tool_result_envelope": "novel_distillation.source_packet_draft.v1",
        "status": "draft_ready",
        "generated_at": now_iso(),
        "input_artifacts": {
            "packet_manifest": str(manifest_path),
            "packet_text": str(text_path),
            "packet_metadata": str(metadata_path) if metadata_path else "",
            "packet_text_sha256": text_sha,
        },
        "output_artifacts": {
            "cards_draft_json": str(cards_path),
            "distillation_report_md": str(report_path),
            "source_index_json": str(source_index_path),
            "metrics_json": str(metrics_path),
            "chapter_features_jsonl": str(chapter_features_path),
            "chunk_index_small_jsonl": str(chunk_paths["small"]),
            "chunk_index_big_jsonl": str(chunk_paths["big"]),
            "chunk_index_super_jsonl": str(chunk_paths["super"]),
            "validation_report_md": str(validation_report_path),
            "tool_result_envelope_json": str(envelope_path),
        },
        "checkpoints": {
            "packet_manifest_resolved": True,
            "packet_text_present": True,
            "draft_cards_written": cards_path.exists(),
            "artifact_contract_complete": all(path.exists() for path in [cards_path, report_path, source_index_path, metrics_path, chapter_features_path, chunk_paths["small"], chunk_paths["big"], chunk_paths["super"], validation_report_path]),
            "card_count": len(cards),
            "cards_are_draft_candidates": True,
            "requires_human_distillation": True,
            "no_long_source_prose_stored": True,
            "source_profile": profile,
            "chapter_count": len(chapters),
            "chunk_counts": {level: len(rows) for level, rows in chunk_indexes.items()},
            "chapter_features_ready": len(chapter_features) == len(chapters),
            "validation_ladder_draft": validation_statuses,
        },
        "success_criteria": {
            "required_status": "draft_ready",
            "required_output_artifacts": [
                "cards_draft_json",
                "distillation_report_md",
                "source_index_json",
                "metrics_json",
                "chapter_features_jsonl",
                "chunk_index_small_jsonl",
                "chunk_index_big_jsonl",
                "chunk_index_super_jsonl",
                "validation_report_md",
                "tool_result_envelope_json",
            ],
            "required_checkpoints": [
                "packet_manifest_resolved",
                "packet_text_present",
                "draft_cards_written",
                "artifact_contract_complete",
                "cards_are_draft_candidates",
                "requires_human_distillation",
                "no_long_source_prose_stored",
                "chapter_features_ready",
            ],
            "ready_for_formal_ingest": False,
            "formal_ingest_requires": [
                "human_or_model_review_replaces_pending_fields",
                "distill_kb_add",
                "distill_kb_validate",
                "distill_kb_health",
                "nlcl_post_distill_sync",
            ],
        },
        "warnings": [
            "Draft cards are not validated mechanisms.",
            "Review and edit before distill_kb.py add or NLCL formal use.",
        ],
        "failure_codes": [],
        "fallback_manual_step": "Review cards.draft.json, replace pending fields with transferable mechanisms, rerun human/model distillation review, then use distill_kb.py add -> validate -> health -> NLCL sync.",
        "next_action": "Review cards.draft.json, replace pending fields with transferable mechanisms, then add with distill_kb.py and run validate/health.",
    }
    write_json(envelope_path, envelope)
    return envelope


def main() -> int:
    # Windows pipes may default to a legacy code page; force UTF-8 so the
    # JSON envelope is machine-readable and Chinese fields do not corrupt tests
    # or downstream tool consumers.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Generate draft distillation cards from a source packet.")
    parser.add_argument("packet", help="Source packet directory or packet.manifest.json")
    parser.add_argument("--out-dir", default="", help="Output directory for cards.draft.json and report")
    args = parser.parse_args()
    envelope = run(args)
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return 0 if envelope.get("status") == "draft_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
