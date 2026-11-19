"""Deterministic longform checks the author can read without a model.

Absorbs PlotPilot's visible continuity/pacing/foreshadow ledger, Denova's
"show what this round used", and AI-fiction's evidence-pack idea — rewritten
against the author's existing markdown + state files. Never writes the book.
Never blocks save. Findings are advice only.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from zhimeng_runtime.project_reader import (
    EXCLUDED_DIRECTORIES,
    in_numbered_non_prose_section,
    prose_directories,
)
from zhimeng_runtime.project_truth import (
    NON_TRUTH_PATH_MARKERS,
    is_non_truth_project_path,
)

DIALOGUE_MARKERS = ('"', "“", "”", "「", "」", "『", "』")
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")
CHAPTER_ID_RE = re.compile(r"(?:CH|ch)?\s*0*(\d{1,6})\s*$")
PLAN_CHAPTER_RE = re.compile(r"第\s*0*(\d{1,6})(?:\s*[-~—到至]\s*第?\s*0*(\d{1,6}))?\s*章")
TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
# 作者用中文输入法打表格时常敲出全角 ｜；先归一再按半角解析。
FULLWIDTH_PIPE = "｜"


def _table_row_cells(line: str) -> list[str] | None:
    normalized = line.strip().replace(FULLWIDTH_PIPE, "|")
    match = TABLE_ROW_RE.match(normalized)
    if match is None:
        return None
    return [cell.strip() for cell in match.group(1).split("|")]
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
SKIP_STEM_MARKERS = ("控制卡", "章纲", "审稿", "总览", "进度", "修改记录", "校验")
HIGHER_REALMS = ("筑基", "金丹", "元婴", "化神", "合体", "大乘", "渡劫")
INJURY_OK_WORDS = ("养", "好了", "恢复", "无碍", "已愈")
BOTH_HANDS = ("双掌", "双手", "两手", "挥拳", "握拳")
EARLY_PAYOFF = ("真相大白", "彻底平反", "全部揭穿", "一次性揭开")
HOOK_MARKERS = ("？", "?", "难道", "怎么会")
AWAY_MARKERS = ("不在场", "已离场", "远行")
HERE_MARKERS = ("本章应出场", "应出场", "在场", "同行", "跟前")
# 沉睡阈值故意保守（借鉴 Novel-OS 连续性引擎的思路，自己定阈值）：
# 埋了 20 章既没碰又没写回收窗口，才提醒一次；写了窗口的按窗口算。
DORMANT_FORESHADOW_GAP_CHAPTERS = 20
# 状态表里在追踪、又没标不在场/已死的人，连续 5 章正文一次都没出现才提醒。
ABSENT_CAST_GAP_CHAPTERS = 5
# 状态表里这类标题是结构分节，不是人名（真实书里有「主表·角色概览」
# 「【当前唯一真值】C380后快照」这种小节）。
STRUCTURAL_HEADING_MARKERS = (
    "【", "表", "概览", "快照", "真值", "说明", "模板", "索引", "清单", "一览", "规则",
    "记录", "补丁", "回溯", "档案", "备注", "章", "卷",
)


class LongformCheckError(RuntimeError):
    code = "longform_check_error"
    retryable = False


class LongformCheckNotFound(LongformCheckError):
    code = "longform_check_not_found"


def _leading_number(stem: str) -> int | None:
    digits = ""
    for character in stem:
        if character.isdigit():
            digits += character
        elif digits:
            break
    return int(digits) if digits else None


def chapter_number_from_id(chapter_id: str | None) -> int | None:
    if not chapter_id:
        return None
    match = re.search(r"CH\s*0*(\d{1,6})", chapter_id, flags=re.I)
    if match:
        return int(match.group(1))
    digits = _leading_number(chapter_id.lstrip("第"))
    return digits


def format_writing_brief(result: dict[str, Any]) -> str:
    """Compact human brief for the Agent send path. No JSON."""
    pacing = result.get("pacing") or {}
    conflicts = result.get("setting_conflicts") or []
    foreshadows = result.get("foreshadows") or []
    checked = result.get("checked") or {}
    lines = [
        result.get("author_summary") or "",
        f"已核对人物状态 {checked.get('character_count', 0)} 人，"
        f"伏笔 {checked.get('foreshadow_count', 0)} 条，"
        f"世界规则 {checked.get('rule_count', 0)} 条。",
    ]
    if pacing:
        lines.append(str(pacing.get("summary") or ""))
    if conflicts:
        lines.append("设定矛盾：")
        for item in conflicts[:5]:
            lines.append(f"· {item.get('title')}")
    else:
        lines.append("设定矛盾：本章未发现确定性冲突（仍请你过一眼）。")
    due = [item for item in foreshadows if item.get("severity") in {"watch", "problem"}]
    if due:
        lines.append("伏笔：")
        for item in due[:5]:
            lines.append(f"· {item.get('title')}")
    cast = [item for item in (result.get("cast") or []) if item.get("severity") in {"watch", "problem"}]
    if cast:
        lines.append("出场：")
        for item in cast[:4]:
            lines.append(f"· {item.get('title')}")
    hook = [item for item in (result.get("hook_bridge") or []) if item.get("severity") in {"watch", "problem"}]
    if hook:
        lines.append("章间衔接：")
        for item in hook[:2]:
            lines.append(f"· {item.get('title')}")
    ledger = result.get("ledger_suggestions") or []
    if ledger:
        lines.append("该补进账本：")
        for item in ledger[:4]:
            lines.append(f"· {item.get('title')}")
    banned = result.get("banned") or []
    if banned:
        lines.append("撞了本书禁用清单：")
        for item in banned[:4]:
            lines.append(f"· {item.get('title')}")
    return "\n".join(line for line in lines if line).strip()


def check_project(project_root: Path, *, chapter_id: str | None = None) -> dict[str, Any]:
    root = Path(project_root)
    if not root.is_dir():
        raise LongformCheckNotFound(f"project root missing: {root}")

    chapter_number = chapter_number_from_id(chapter_id)
    prose_files = _prose_files(root)
    if chapter_number is None and prose_files:
        chapter_number = max(prose_files)
    current_path = prose_files.get(chapter_number) if chapter_number is not None else None
    current_text = _read_text(current_path) if current_path else ""
    chapter_label = (
        f"第 {chapter_number:03d} 章" if chapter_number is not None else "未选择章节"
    )

    characters = _parse_character_status(root)
    rules = _parse_world_rules(root)
    foreshadow_rows = _parse_foreshadow_table(root)
    pacing = _pacing_report(
        current_text,
        chapter_number=chapter_number,
        chapter_label=chapter_label,
        prose_files=prose_files,
        open_debt_count=sum(1 for row in foreshadow_rows if row["open"]),
    )
    conflicts = _setting_conflicts(current_text, characters, rules, current_path)
    foreshadows = _foreshadow_findings(foreshadow_rows, chapter_number, current_text)
    prev_text = ""
    if chapter_number is not None and (chapter_number - 1) in prose_files:
        prev_text = _read_text(prose_files[chapter_number - 1])
    cast = _cast_findings(current_text, characters)
    cast.extend(_absent_cast_findings(prose_files, chapter_number, characters))
    cast = cast[:8]
    hook = _hook_bridge(prev_text, current_text, chapter_number)
    ledger = _ledger_suggestions(current_text, foreshadow_rows)
    banned = _banned_findings(current_text, root)

    checked_files = sorted(
        {
            *(item["source_file"] for item in characters if item.get("source_file")),
            *(item["source_file"] for item in rules if item.get("source_file")),
            *(item["source_file"] for item in foreshadow_rows if item.get("source_file")),
            *( [current_path.relative_to(root).as_posix()] if current_path else [] ),
        }
    )
    problem_count = sum(1 for item in conflicts if item["severity"] == "problem")
    watch_count = sum(
        1
        for item in (*conflicts, *foreshadows, *cast, *hook, *ledger, *banned)
        if item["severity"] == "watch"
    )
    if not current_text:
        author_summary = f"{chapter_label}还没有正文可检查。打开一章后再看节奏和设定。"
    elif problem_count:
        author_summary = (
            f"{chapter_label}有 {problem_count} 条需要你看的设定问题"
            f"{f'，另有 {watch_count} 条提醒' if watch_count else ''}。"
        )
    elif watch_count:
        author_summary = f"{chapter_label}没有硬冲突，有 {watch_count} 条节奏或伏笔提醒。"
    else:
        author_summary = f"{chapter_label}未发现确定性设定冲突。节奏数字如下，供你判断。"

    return {
        "schema": "zhimeng.longform-check.v1",
        # The workbench identifies chapters as VOL001-CH004. Preserve that
        # identity when it was supplied; collapsing it to CH004 makes the
        # frontend correctly reject a fresh result as belonging to another
        # chapter. Internal callers that use CH004 keep the existing shape.
        "chapter_id": (
            chapter_id.strip()
            if chapter_number is not None
            and chapter_id is not None
            and re.fullmatch(r"VOL\d+-CH\d+", chapter_id.strip(), flags=re.I)
            else f"CH{chapter_number:03d}"
            if chapter_number is not None
            else None
        ),
        "chapter_label": chapter_label,
        "author_summary": author_summary,
        "advisory_only": True,
        "checked": {
            "character_count": len(characters),
            "foreshadow_count": len(foreshadow_rows),
            "rule_count": len(rules),
            "files": checked_files,
        },
        "pacing": pacing,
        "setting_conflicts": conflicts,
        "foreshadows": foreshadows,
        "cast": cast,
        "hook_bridge": hook,
        "ledger_suggestions": ledger,
        "banned": banned,
        "writing_brief": "",
    }


def check_project_for_author(project_root: Path, *, chapter_id: str | None = None) -> dict[str, Any]:
    result = check_project(project_root, chapter_id=chapter_id)
    result["writing_brief"] = format_writing_brief(result)
    return result


SECTION_PREFIX_RE = re.compile(r"^0([1-9])(?:[_\-\s]|$)")


def _section_dirs(root: Path, index: int) -> list[Path]:
    """按「0N_」前缀找分区目录，与 ProjectReader 同口径。

    原来逐字匹配「03_人物设定」这类固定名；作者把目录改名成
    「03_人物」就会静默查空。同号也可能有不止一个夹子——作者的真书里
    出现过 `06_写作规范` 和 `06_写法规矩` 并存，只认第一个会漏掉一半资料。
    这里返回全部，调用方自己合并；找不到时回退调用方的旧路径。
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not entry.is_dir():
            continue
        match = SECTION_PREFIX_RE.match(entry.name)
        if match and int(match.group(1)) == index:
            found.append(entry)
    return found


def _section_markdowns(root: Path, index: int, fallback: str) -> list[Path]:
    """这个分区里的所有 Markdown，含作者自己另开的同号夹子。"""
    folders = _section_dirs(root, index) or [root / fallback]
    found: set[Path] = set()
    for folder in folders:
        if folder.is_dir():
            found.update(folder.rglob("*.md"))
    return sorted(found, key=lambda item: item.as_posix())


def _skip_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = {part.casefold() for part in relative.parts}
    if parts & {name.casefold() for name in EXCLUDED_DIRECTORIES}:
        return True
    posix = relative.as_posix()
    return is_non_truth_project_path(posix)


def _read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _prose_files(root: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    excluded = {name.casefold() for name in EXCLUDED_DIRECTORIES}
    for section in prose_directories(root):
        for dirpath, dirnames, filenames in os.walk(section, followlinks=False):
            dirnames[:] = [
                name
                for name in dirnames
                if name.casefold() not in excluded
                and name != "章纲"
                and not any(marker in name for marker in NON_TRUTH_PATH_MARKERS)
            ]
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                path = Path(dirpath) / filename
                if _skip_path(path, root):
                    continue
                if in_numbered_non_prose_section(
                    path.relative_to(root).as_posix()
                ):
                    continue
                if any(marker in path.stem for marker in SKIP_STEM_MARKERS):
                    continue
                number = _leading_number(path.stem.lstrip("第"))
                if number is None:
                    continue
                previous = found.get(number)
                if previous is None or len(path.stem) < len(previous.stem):
                    found[number] = path
    return found


def _parse_character_status(root: Path) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for path in _section_markdowns(root, 3, "03_人物设定"):
        if _skip_path(path, root):
            continue
        if "状态" not in path.stem and "跟踪" not in path.stem:
            continue
        relative = path.relative_to(root).as_posix()
        current: dict[str, Any] | None = None
        for line in _read_text(path).splitlines():
            heading = HEADING_RE.match(line)
            if heading:
                if current and current.get("name"):
                    people.append(current)
                raw = heading.group(1).strip()
                name = re.split(r"[（(]", raw, maxsplit=1)[0].strip()
                # 人名不会带数字或英文字母；带的多半是「C106后人物状态补丁」
                # 这类分节标题。
                if (
                    any(marker in raw for marker in STRUCTURAL_HEADING_MARKERS)
                    or re.search(r"[A-Za-z0-9]", name) is not None
                    or len(name) > 8
                ):
                    current = None
                    continue
                current = {
                    "name": name,
                    "is_lead": "主角" in raw,
                    "source_file": relative,
                    "fields": {},
                }
                continue
            if current is None:
                continue
            row = _table_cells(line)
            if len(row) >= 2 and row[0] not in {"维度", "---", "----"}:
                current["fields"][row[0].strip()] = row[1].strip()
        if current and current.get("name"):
            people.append(current)
    return people


def _parse_world_rules(root: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for path in _section_markdowns(root, 2, "02_世界观设定"):
        if _skip_path(path, root):
            continue
        if "规则" not in path.stem and "禁忌" not in path.stem:
            continue
        relative = path.relative_to(root).as_posix()
        for line in _read_text(path).splitlines():
            stripped = line.strip()
            if stripped.startswith(("-", "*", "·")):
                text = stripped.lstrip("-*· ").strip()
                if text.startswith("不允许") or "不能" in text[:8] or "禁止" in text[:6]:
                    rules.append({"text": text, "source_file": relative})
    return rules[:64]


def _parse_foreshadow_table(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _section_markdowns(root, 4, "04_剧情大纲"):
        if _skip_path(path, root):
            continue
        text = _read_text(path)
        if "伏笔" not in path.stem and "伏笔" not in text[:80]:
            continue
        relative = path.relative_to(root).as_posix()
        for line in text.splitlines():
            cells = _table_cells(line)
            if len(cells) < 5:
                continue
            if cells[0] in {"ID", "编号"} or set(cells[0]) <= {"-", ":"}:
                continue
            planted = _first_chapter_number(cells[1])
            plan_text = cells[4] if len(cells) > 4 else ""
            plan_start, plan_end = _plan_range(plan_text)
            status = cells[3] if len(cells) > 3 else ""
            open_item = not any(marker in status for marker in ("已回收", "✅", "关闭", "作废"))
            progress_text = f"{status} {plan_text}"
            rows.append(
                {
                    "id": cells[0],
                    "planted_chapter": planted,
                    "content": cells[2],
                    "status": status,
                    "plan_text": plan_text,
                    "plan_start": plan_start,
                    "plan_end": plan_end,
                    "open": open_item,
                    "touched": any(
                        marker in progress_text
                        for marker in ("已触发", "已拾取", "已加压", "加压", "已推进", "🟡")
                    ),
                    "source_file": relative,
                }
            )
    return rows


def _table_cells(line: str) -> list[str]:
    cells = _table_row_cells(line)
    return cells if cells is not None else []


def _first_chapter_number(text: str) -> int | None:
    match = PLAN_CHAPTER_RE.search(text) or re.search(r"(\d{1,6})", text)
    return int(match.group(1)) if match else None


def _plan_range(text: str) -> tuple[int | None, int | None]:
    match = PLAN_CHAPTER_RE.search(text)
    if not match:
        return None, None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    return start, end


def _pacing_report(
    text: str,
    *,
    chapter_number: int | None,
    chapter_label: str,
    prose_files: dict[int, Path],
    open_debt_count: int,
) -> dict[str, Any] | None:
    if not text.strip():
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    paragraphs = [part for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]
    sentences = [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]
    char_count = len(re.sub(r"\s+", "", text))
    dialogue_lines = sum(1 for line in lines if any(marker in line for marker in DIALOGUE_MARKERS))
    dialogue_ratio = (dialogue_lines / len(lines)) if lines else 0.0
    lengths = [len(re.sub(r"\s+", "", sentence)) for sentence in sentences] or [0]
    avg_sentence = sum(lengths) / len(lengths)
    short_ratio = sum(1 for item in lengths if 0 < item <= 12) / len(lengths)
    if len(paragraphs) >= 2:
        para_lens = [len(part) for part in paragraphs]
        mean_len = sum(para_lens) / len(para_lens)
        variance = sum((item - mean_len) ** 2 for item in para_lens) / len(para_lens)
        cv = (math.sqrt(variance) / mean_len) if mean_len else 0.0
    else:
        cv = 0.0

    notes: list[str] = []
    if dialogue_ratio > 0.7:
        notes.append(
            f"对白约占 {round(dialogue_ratio * 100)}%；"
            "如果不是刻意用对话推进，可看看是否缺少动作和现场感"
        )
    elif dialogue_ratio < 0.08:
        notes.append(
            f"对白约占 {round(dialogue_ratio * 100)}%；"
            "如果本章需要人物碰撞，可看看是否太安静"
        )
    if short_ratio > 0.45:
        notes.append(
            f"短句约占 {round(short_ratio * 100)}%；"
            "如果不是刻意加速，可听读看看是否过碎"
        )
    elif avg_sentence > 40:
        notes.append("平均句长偏长；打斗或揭秘处可看看是否拖慢")
    if cv < 0.1 and len(paragraphs) >= 3:
        notes.append("段落长度很接近；如果场面强弱不同，可看看是否需要变化")

    curve: list[dict[str, Any]] = []
    if chapter_number is not None:
        nearby = range(max(1, chapter_number - 9), chapter_number + 1)
    else:
        last = max(prose_files) if prose_files else 0
        nearby = range(max(1, last - 9), last + 1)
    for number in nearby:
        path = prose_files.get(number)
        if path is None:
            continue
        body = _read_text(path) if number == chapter_number else ""
        count = len(re.sub(r"\s+", "", body)) if body else max(0, path.stat().st_size // 3)
        curve.append({"chapter": number, "chars": count})
    flat_run = False
    if len(curve) >= 3:
        values = [item["chars"] for item in curve[-3:]]
        mid = sum(values) / 3
        flat_run = mid > 0 and all(abs(item - mid) / mid < 0.1 for item in values)
        if flat_run:
            notes.append("近三章篇幅接近；如果情节强弱不同，可看看是否写得太齐")
    if chapter_number is not None and chapter_number <= 12 and open_debt_count >= 4:
        if sum(text.count(token) for token in EARLY_PAYOFF) >= 2:
            notes.append("开篇就把账结得很干净，后面容易没钩子")

    summary = "；".join(notes) if notes else "句长、对白和段落起伏都在常见范围内。"
    return {
        "chapter_label": chapter_label,
        "char_count": char_count,
        "sentence_count": len(sentences),
        "avg_sentence_len": round(avg_sentence, 1),
        "short_sentence_ratio": round(short_ratio, 3),
        "dialogue_ratio": round(dialogue_ratio, 3),
        "paragraph_cv": round(cv, 3),
        "recent_chapter_counts": curve,
        "flat_run": flat_run,
        "summary": summary,
    }


def _unique_passage(text: str, *needles: str) -> str | None:
    tokens = [token for token in needles if token]
    if not tokens:
        return None
    parts = [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]
    hits = [part for part in parts if all(token in part for token in tokens)]
    if len(hits) != 1:
        return None
    passage = hits[0]
    if len(passage) < 8 or len(passage) > 400:
        return None
    if text.count(passage) != 1:
        return None
    return passage


def _window_around(text: str, name: str, radius: int = 120) -> str:
    chunks: list[str] = []
    start = 0
    while True:
        index = text.find(name, start)
        if index < 0:
            break
        chunks.append(text[max(0, index - radius) : index + len(name) + radius])
        start = index + len(name)
        if len(chunks) >= 8:
            break
    return "\n".join(chunks)


def _setting_conflicts(
    text: str,
    characters: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    current_path: Path | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not text.strip():
        return findings
    relative = current_path.name if current_path else None
    for person in characters:
        name = str(person.get("name") or "")
        if not name or name not in text:
            continue
        fields = person.get("fields") or {}
        window = _window_around(text, name)
        injury = str(fields.get("当前伤势") or "")
        if injury and injury not in {"无", "无伤", "没有"}:
            if any(token in injury for token in ("断", "残", "缺")) and any(
                token in window for token in BOTH_HANDS
            ):
                if not any(token in window for token in INJURY_OK_WORDS):
                    hand_token = next((token for token in BOTH_HANDS if token in window), None)
                    findings.append(
                        _finding(
                            "setting_conflict",
                            "problem",
                            f"{name}设定里是「{injury}」，本章却写出双手都能用",
                            expected=injury,
                            actual="正文出现双手/双掌动作",
                            source_file=person.get("source_file"),
                            quote=window[:80],
                            passage=_unique_passage(text, name, hand_token or ""),
                        )
                    )
            if any(token in injury for token in ("已死", "身亡", "死亡")) and (
                "「" in window or '"' in window
            ):
                findings.append(
                    _finding(
                        "setting_conflict",
                        "problem",
                        f"{name}设定里已经死了，本章仍在说话",
                        expected=injury,
                        actual="正文仍有对白",
                        source_file=person.get("source_file"),
                        quote=window[:80],
                        passage=_unique_passage(text, name),
                    )
                )
        realm = str(fields.get("当前境界") or "")
        if realm and any(token in realm for token in ("引气", "炼气", "练气", "感气")):
            for hit in HIGHER_REALMS:
                attributed = re.search(
                    rf"{re.escape(name)}(?:已经|已是|是|修为.{{0,6}}|突破.{{0,6}}){hit}",
                    window,
                )
                if attributed:
                    findings.append(
                        _finding(
                            "setting_conflict",
                            "watch",
                            f"{name}设定境界是「{realm}」，本章却写成「{hit}」",
                            expected=realm,
                            actual=hit,
                            source_file=person.get("source_file"),
                            quote=window[:80],
                            passage=_unique_passage(text, name, hit),
                        )
                    )
                    break
        stance = str(fields.get("对主角态度") or "")
        if any(token in stance for token in ("敌对", "仇", "杀意")) and any(
            token in window for token in ("一起喝茶", "毫无防备", "放心交出")
        ):
            close_token = next(
                (token for token in ("一起喝茶", "毫无防备", "放心交出") if token in window),
                None,
            )
            findings.append(
                _finding(
                    "relation",
                    "watch",
                    f"{name}对主角仍是「{stance}」，本章却写出无防备合作",
                    expected=stance,
                    actual="无桥接的亲近动作",
                    source_file=person.get("source_file"),
                    quote=window[:80],
                    passage=_unique_passage(text, name, close_token or ""),
                )
            )
    joined = text
    for rule in rules:
        rule_text = str(rule.get("text") or "")
        if "药膳" in rule_text and "丹药" in rule_text:
            if "药膳" in joined and any(token in joined for token in ("天阶丹", "金丹丹", "高阶丹药")):
                findings.append(
                    _finding(
                        "setting_conflict",
                        "problem",
                        "世界规则禁止把药膳写成高阶丹药，本章同时出现了这两类写法",
                        expected=rule_text,
                        actual="药膳 + 高阶丹药",
                        source_file=rule.get("source_file"),
                        quote=relative,
                        passage=_unique_passage(text, "药膳", "丹"),
                    )
                )
        if "无代价" in rule_text and "修为" in rule_text:
            if re.search(r"系统.{0,12}(直接)?(突破|抬|升到)", joined):
                findings.append(
                    _finding(
                        "setting_conflict",
                        "watch",
                        "世界规则禁止系统无代价抬修为，本章出现了类似写法",
                        expected=rule_text,
                        actual="系统直接提升修为",
                        source_file=rule.get("source_file"),
                        quote=relative,
                        passage=_unique_passage(text, "系统"),
                    )
                )
    return findings[:8]


def _foreshadow_findings(
    rows: list[dict[str, Any]],
    chapter_number: int | None,
    text: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if chapter_number is None:
        return findings
    for row in rows:
        if not row.get("open"):
            continue
        start = row.get("plan_start")
        end = row.get("plan_end")
        title = f"{row['id']} {row['content']}"
        if end is not None and chapter_number > end and not row.get("touched"):
            findings.append(
                _finding(
                    "foreshadow",
                    "problem",
                    f"{title} 计划在第 {end} 章前回收，现在已经过了",
                    expected=row.get("plan_text"),
                    actual=f"当前第 {chapter_number} 章，状态仍是 {row.get('status')}",
                    source_file=row.get("source_file"),
                    quote=row.get("content"),
                )
            )
        elif start is not None and start <= chapter_number <= (end or start):
            if row.get("touched"):
                continue
            snippet = str(row.get("content") or "")[:12]
            touched_now = bool(snippet) and snippet[:4] in text
            # 正文已经碰到但账本尚未更新时，下面的 ledger suggestion 会给
            # 出唯一、可执行的提醒；这里不再同时制造一条重复的“窗口提醒”。
            if not touched_now:
                findings.append(
                    _finding(
                        "foreshadow",
                        "watch",
                        f"{title} 正落在本章回收窗口，本章正文还没碰到",
                        expected=row.get("plan_text"),
                        actual=f"第 {chapter_number} 章",
                        source_file=row.get("source_file"),
                        quote=row.get("content"),
                    )
                )
        elif (
            start is None
            and not row.get("touched")
            and row.get("planted_chapter") is not None
            and chapter_number - int(row["planted_chapter"])
            >= DORMANT_FORESHADOW_GAP_CHAPTERS
        ):
            gap = chapter_number - int(row["planted_chapter"])
            findings.append(
                _finding(
                    "foreshadow",
                    "watch",
                    f"{title} 埋了 {gap} 章既没碰过，也没写回收窗口",
                    expected="补一个计划回收窗口，或在近几章碰一下，或标作废",
                    actual=f"第 {row['planted_chapter']} 章埋下，状态仍是 {row.get('status')}",
                    source_file=row.get("source_file"),
                    quote=row.get("content"),
                )
            )
    return findings[:8]


def _cast_findings(text: str, characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not text.strip():
        return findings
    seen_names: set[str] = set()
    for person in characters:
        name = str(person.get("name") or "")
        if len(name) < 2 or name in seen_names:
            continue
        seen_names.add(name)
        fields = person.get("fields") or {}
        location = str(fields.get("当前位置") or fields.get("是否在场") or "")
        injury = str(fields.get("当前伤势") or "")
        present = name in text
        expected_away = any(token in location for token in AWAY_MARKERS)
        expected_here = (not expected_away) and any(
            token in location for token in HERE_MARKERS
        )
        dead = any(token in injury for token in ("已死", "身亡", "死亡"))
        if expected_away and present and not dead:
            findings.append(
                _finding(
                    "cast",
                    "problem",
                    f"{name}账本写着不在场，本章正文仍出现",
                    expected=location or "不在场",
                    actual="正文出场",
                    source_file=person.get("source_file"),
                    quote=name,
                )
            )
        elif expected_here and not present:
            findings.append(
                _finding(
                    "cast",
                    "watch",
                    f"{name}账本写着应出场，本章没出场",
                    expected=location or "应出场",
                    actual="正文未出现",
                    source_file=person.get("source_file"),
                    quote=name,
                )
            )
    return findings[:8]


def _markdowns(root: Path) -> list[Path]:
    """整本书的 Markdown，浅层在前；跳过排除目录、隐藏目录和作者标了「勿作为真值」的归档。"""
    excluded = {name.casefold() for name in EXCLUDED_DIRECTORIES}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name.casefold() not in excluded
            and not name.startswith(".")
            and not any(marker in name for marker in NON_TRUTH_PATH_MARKERS)
        ]
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            path = base / filename
            if _skip_path(path, root):
                continue
            found.append(path)
    return found


def _banned_list_files(root: Path) -> list[Path]:
    """这本书的禁用清单在哪。

    默认摆法是项目定位夹子里的「禁用清单.md」；作者把它挪进别的夹子、
    或者写成「本书禁用」「禁用词」时，按文件名在整本书里找，找到几份扫几份
    （同号夹子并存时也各算一份）。只认名字里带「禁用」的文件：
    「人物口癖」这类资料是设定，不是黑名单，不能当成禁用清单扫。
    """
    candidates = [path for path in _markdowns(root) if "禁用" in path.stem]
    return sorted(
        candidates,
        key=lambda path: (0 if path.stem == "禁用清单" else 1, path.relative_to(root).as_posix()),
    )


def _banned_terms(root: Path) -> list[tuple[str, str]]:
    terms: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in _banned_list_files(root):
        relative = path.relative_to(root).as_posix()
        for line in _read_text(path).splitlines():
            term = line.strip().lstrip("-*·").strip()
            if not term or term.startswith("#") or len(term) < 2 or len(term) > 64:
                continue
            if term in seen:
                continue
            seen.add(term)
            terms.append((term, relative))
    return terms


def _banned_findings(text: str, root: Path) -> list[dict[str, Any]]:
    """本书自己的禁用清单（烂梗/口癖/废话句式），命中就点名。

    一行一条（可带 - 前缀）。生成时同一份清单会作为负向约束进提示词；
    这里管事后对账，`source_file` 报的是清单真正所在的那份文件。
    """
    if not text.strip():
        return []
    findings: list[dict[str, Any]] = []
    for term, relative in _banned_terms(root):
        count = text.count(term)
        if count <= 0:
            continue
        findings.append(
            _finding(
                "banned",
                "watch",
                f"禁用词「{term}」出现 {count} 次",
                expected="这本书的禁用清单里明确不要它",
                actual=f"本章正文出现 {count} 次",
                source_file=relative,
                quote=term,
                passage=_unique_passage(text, term),
            )
        )
    return findings[:10]


def _absent_cast_findings(
    prose_files: dict[int, Path],
    chapter_number: int | None,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """状态表在追踪的人连续几章完全没出现——多半是被写忘了。

    只看最近 ABSENT_CAST_GAP_CHAPTERS 章都真实存在的书；标了不在场/
    远行/已死的人不算。
    """
    if chapter_number is None or chapter_number < ABSENT_CAST_GAP_CHAPTERS:
        return []
    recent = range(chapter_number - ABSENT_CAST_GAP_CHAPTERS + 1, chapter_number + 1)
    paths = [prose_files.get(number) for number in recent]
    if any(path is None for path in paths):
        return []
    texts = [_read_text(path) for path in paths]
    findings: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for person in characters:
        name = str(person.get("name") or "")
        fields = person.get("fields") or {}
        if len(name) < 2 or name in seen_names or not fields:
            continue
        seen_names.add(name)
        location = str(fields.get("当前位置") or fields.get("是否在场") or "")
        injury = str(fields.get("当前伤势") or "")
        if any(token in location for token in AWAY_MARKERS):
            continue
        if any(token in injury for token in ("已死", "身亡", "死亡")):
            continue
        if any(name in text for text in texts):
            continue
        findings.append(
            _finding(
                "cast",
                "watch",
                f"{name}已经连续 {ABSENT_CAST_GAP_CHAPTERS} 章没出现",
                expected="要么安排出场或提及，要么在状态表标不在场/远行",
                actual=f"第 {recent.start}–{chapter_number} 章正文都没有这个名字",
                source_file=person.get("source_file"),
                quote=name,
            )
        )
    return findings[:6]


def _content_bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s。！？!?「」『』“”…，、,.]", "", text)
    return {cleaned[index:index + 2] for index in range(max(0, len(cleaned) - 1))}


def _hook_bridge(
    prev_text: str,
    current_text: str,
    chapter_number: int | None,
) -> list[dict[str, Any]]:
    if not prev_text.strip() or not current_text.strip() or chapter_number is None:
        return []
    prev_sentences = [part.strip() for part in SENTENCE_SPLIT.split(prev_text) if part.strip()]
    open_sentences = [part.strip() for part in SENTENCE_SPLIT.split(current_text) if part.strip()]
    if not prev_sentences or not open_sentences:
        return []
    prev_tail = "".join(prev_sentences[-3:])
    opening = "".join(open_sentences[:3])
    if open_sentences[0] == prev_sentences[-1]:
        return [
            _finding(
                "hook",
                "problem",
                f"第 {chapter_number:03d} 章开头重复了上一章结尾",
                expected=prev_sentences[-1][:40],
                actual=open_sentences[0][:40],
                source_file=None,
                quote=open_sentences[0][:80],
                passage=open_sentences[0] if current_text.count(open_sentences[0]) == 1 else None,
            )
        ]
    hookish = any(marker in prev_tail for marker in HOOK_MARKERS)
    overlap = _content_bigrams(prev_tail) & _content_bigrams(opening)
    if hookish and len(overlap) < 1:
        return [
            _finding(
                "hook",
                "watch",
                f"上一章结尾像钩子，第 {chapter_number:03d} 章开头没有接上",
                expected=prev_tail[-40:],
                actual=opening[:40],
                source_file=None,
                quote=opening[:80],
                passage=open_sentences[0] if current_text.count(open_sentences[0]) == 1 else None,
            )
        ]
    return []


def _ledger_suggestions(
    text: str,
    foreshadow_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not text.strip():
        return findings
    for row in foreshadow_rows:
        if not row.get("open") or row.get("touched"):
            continue
        snippet = str(row.get("content") or "").strip()
        marker = snippet[:4]
        if len(marker) < 3 or marker not in text:
            continue
        findings.append(
            _finding(
                "ledger",
                "watch",
                f"{row.get('id')} {snippet} 本章正文已经碰到，账本还没标已触发",
                expected=str(row.get("status") or "未触发"),
                actual="正文已碰到",
                source_file=row.get("source_file"),
                quote=snippet,
            )
        )
    return findings[:8]


def _finding(
    kind: str,
    severity: str,
    title: str,
    *,
    expected: str | None,
    actual: str | None,
    source_file: str | None,
    quote: str | None,
    passage: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "title": title,
        "expected": expected,
        "actual": actual,
        "source_file": source_file,
        "quote": quote,
        "passage": passage,
    }
