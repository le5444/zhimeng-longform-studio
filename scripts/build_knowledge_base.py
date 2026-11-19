"""把蒸馏卡库 cards.jsonl 生成为可读的 Markdown 知识库：skills/网文知识库/。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\build_knowledge_base.py

- 单一事实源仍是 skills/novel-distillation/library/cards.jsonl（打分器 detect_ai_smell.py
  和写作指导都从它读）。本脚本只生成「人和 Agent 能直接看」的视图，可随时重跑。
- 目录按卡片类型分文件夹，再按题材分子文件夹，一张卡一个 .md，正文是表格和短句，
  不含任何原著文字（卡片本身就只存机制，不存原文）。
- skills/网文知识库/SKILL.md 是给 Agent 加载的总说明：只在不存在时生成，之后由作者/Agent
  自己演进，脚本不覆盖；README.md（数量索引）每次重生成。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "skills/novel-distillation/library/cards.jsonl"
OUT = ROOT / "skills/网文知识库"

TYPE_DIR = {
    "work": "作品机制",
    "genre": "类型规律",
    "scene": "场景写法",
    "trope": "套路模板",
    "voice": "文风声纹",
    "packaging": "包装标题",
    "platform": "平台规则",
    "validator": "质量校验",
    "benchmark": "基准样本",
}
TASK_ZH = {
    "chapter_brief": "章纲",
    "draft_scene": "写场景",
    "review": "审稿",
    "rewrite": "改稿",
    "packaging": "包装",
    "outline": "大纲",
}
ENGINE_ZH = {
    "conflict": "冲突",
    "desire": "欲望",
    "emotion": "情绪",
    "progression": "推进",
    "rhythm": "节奏",
    "suspense": "悬念",
    "worldview": "世界规则",
}
KEY_ZH = {
    "author_intent": "作者意图",
    "reader_feeling": "读者感受",
    "reader_takeaway": "读者收获",
    "risk": "风险",
    "risks": "风险",
    "allowed": "允许",
    "forbidden": "禁止",
    "boundary": "边界",
    "notes": "说明",
    "evidence_scope": "证据范围",
    "url": "链接",
    "title": "标题",
    "author": "作者",
    "platform": "平台",
    "chapters": "章节",
    "sentence_length": "句长",
    "paragraphing": "分段",
    "dialogue": "对白",
    "description": "描写",
    "pov": "视角",
    "tense": "时态",
    "diction": "用词",
    "rhythm": "节奏",
    "tone": "语气",
}
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def zh(key: str) -> str:
    return KEY_ZH.get(key, ENGINE_ZH.get(key, key))


def esc_cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_value(value: object, depth: int = 0) -> list[str]:
    """把任意 JSON 值渲染成 Markdown 列表行（最多三层）。"""
    pad = "  " * depth
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}- {zh(str(k))}：")
                lines.extend(render_value(v, depth + 1))
            elif v not in (None, "", [], {}):
                lines.append(f"{pad}- {zh(str(k))}：{esc_cell(v)}")
    elif isinstance(value, list):
        if all(isinstance(x, (str, int, float)) for x in value):
            if value:
                lines.append(f"{pad}- " + "、".join(esc_cell(x) for x in value))
        else:
            for x in value:
                if isinstance(x, dict):
                    head = x.get("name") or x.get("title") or x.get("rule") or ""
                    lines.append(f"{pad}- {esc_cell(head) if head else '·'}")
                    rest = {k: v for k, v in x.items() if k not in ("name", "title")}
                    lines.extend(render_value(rest, depth + 1))
                else:
                    lines.extend(render_value(x, depth))
    elif value not in (None, ""):
        lines.append(f"{pad}- {esc_cell(value)}")
    return lines


def safe_name(title: str, fallback: str) -> str:
    name = _ILLEGAL.sub(" ", title or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    return name[:60].rstrip()


def genre_folder(card: dict) -> str:
    tax = card.get("taxonomy") or {}
    g = tax.get("genre")
    if isinstance(g, str):
        g = [g]
    g = [str(x).strip() for x in (g or []) if str(x).strip()]
    return "+".join(g) if g else "通用"


def render_card(card: dict) -> str:
    ctype = card.get("card_type", "")
    tax = card.get("taxonomy") or {}
    genres = tax.get("genre") if isinstance(tax.get("genre"), list) else [tax.get("genre")] if tax.get("genre") else []
    tags = card.get("tags") or []
    created = str(card.get("created_at") or "")[:10]
    tasks = [TASK_ZH.get(t, t) for t in (card.get("applicable_tasks") or [])]
    out: list[str] = []
    out.append("---")
    out.append(f"card_id: {card.get('card_id', '')}")
    out.append(f"类型: {TYPE_DIR.get(ctype, ctype)}")
    out.append("题材: " + "、".join(str(x) for x in genres))
    if tax.get("subgenre"):
        out.append("子题材: " + esc_cell("、".join(tax["subgenre"]) if isinstance(tax["subgenre"], list) else tax["subgenre"]))
    if tax.get("platform"):
        out.append("平台: " + esc_cell("、".join(tax["platform"]) if isinstance(tax["platform"], list) else tax["platform"]))
    if tags:
        out.append("标签: " + "、".join(str(t) for t in tags[:20]))
    out.append(f"创建: {created}")
    out.append("来源: skills/novel-distillation/library/cards.jsonl（脚本生成，改卡请改源文件后重跑 scripts/build_knowledge_base.py）")
    out.append("---")
    out.append("")
    out.append(f"# {card.get('title') or card.get('card_id')}")
    out.append("")
    if card.get("summary"):
        out.append(str(card["summary"]).strip())
        out.append("")
    rp = card.get("reader_promise")
    if rp:
        out.append("## 读者承诺")
        out.append("")
        out.extend(render_value(rp) if not isinstance(rp, str) else [rp.strip()])
        out.append("")
    mech = card.get("mechanics") or []
    if mech:
        out.append("## 机制")
        out.append("")
        out.append("| 机制 | 规则 | 什么时候用 | 常见失败 |")
        out.append("|---|---|---|---|")
        for m in mech:
            if isinstance(m, dict):
                out.append(f"| {esc_cell(m.get('name', ''))} | {esc_cell(m.get('rule', ''))} | {esc_cell(m.get('use_when', ''))} | {esc_cell(m.get('failure_mode', ''))} |")
            else:
                out.append(f"| {esc_cell(m)} |  |  |  |")
        out.append("")
    eng = card.get("narrative_engines")
    if eng:
        out.append("## 叙事引擎")
        out.append("")
        out.extend(render_value(eng))
        out.append("")
    eff = card.get("reader_effect")
    if eff:
        out.append("## 读者效果")
        out.append("")
        out.extend(render_value(eff))
        out.append("")
    dna = card.get("style_dna")
    if dna:
        out.append("## 文风 DNA")
        out.append("")
        out.extend(render_value(dna))
        out.append("")
    if tasks:
        out.append("## 适用任务")
        out.append("")
        out.append("- " + "、".join(tasks))
        out.append("")
    kw = card.get("keywords") or []
    if kw:
        out.append("## 关键词")
        out.append("")
        out.append("- " + "、".join(esc_cell(k) for k in kw[:40]))
        out.append("")
    extra = {k: card.get(k) for k in ("safety", "source", "scores") if card.get(k)}
    other_tax = {k: v for k, v in tax.items() if k not in ("genre", "subgenre", "platform") and v}
    if other_tax:
        extra = {"分类": other_tax, **extra}
    if extra:
        out.append("## 边界、分类与来源")
        out.append("")
        out.extend(render_value(extra))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


SKILL_MD = """---
name: 网文知识库
description: 网文写作知识库——从 400 多本番茄小说蒸馏出的一千多张机制卡，按作品机制、类型规律、场景写法、套路模板、文风声纹、包装标题、平台规则、质量校验分类的 Markdown 文件。写章纲、写场景、改稿、起书名章名、判断 AI 味时按需查阅；只学机制，不抄原文。
---

# 网文知识库

这是一座可以直接打开看的知识库：每张卡一个 Markdown 文件，按下面的文件夹分类；
数量和题材分布见同目录 `README.md`。所有卡片只记「机制、读者承诺、节奏、失败模式」，
不含任何原著文字，用的时候**学路径、换实体**，不照搬人名、设定壳、名场面顺序和标志句。

## 文件夹

| 文件夹 | 装什么 | 什么时候翻 |
|---|---|---|
| `类型规律/<题材>/` | 这一类小说读者要什么、章节怎么兑现承诺 | 开新书、定位、写章纲之前 |
| `场景写法/<题材>/` | 首章杠杆、打斗、审讯、反转等场景的功能拆解 | 写某个具体场景卡壳时 |
| `作品机制/<题材>/` | 单本头部作品的开篇承诺、收益-债务循环、去污染迁移法 | 想参考同题材爆款怎么起步时 |
| `套路模板/` | 可复用的桥段结构与失败模式 | 需要一个能跑的桥段骨架时 |
| `文风声纹/` | 短句、白描、情绪外化等写法偏斜与 AI 味来源 | 改稿、润色、去 AI 味时 |
| `包装标题/` | 书名、章名、简介的承诺写法 | 起名、写简介时 |
| `平台规则/` | 番茄等平台的读者习惯与审核边界 | 定平台、看留存时 |
| `质量校验/` | 反 AI 黑名单、逻辑与节奏检查项 | 审稿、自检时 |

## 怎么用

1. 先按题材进 `类型规律`，再看对应 `场景写法`；每张卡的「机制」表里，
   「规则」是做法，「常见失败」是别踩的坑，「什么时候用」告诉你适不适合当前这一章。
2. 卡片是建议，不是规定。作者正文、01–09 真值文件和作者当下的话永远优先。
3. 想补新知识：改 `skills/novel-distillation/library/cards.jsonl` 后重跑
   `scripts/build_knowledge_base.py`；这份 SKILL.md 可以直接编辑，脚本不会覆盖它。
"""


def main() -> int:
    if not CARDS.is_file():
        print("cards.jsonl not found:", CARDS)
        return 1
    raw = CARDS.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    cards: list[dict] = []
    bad = 0
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cards.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1

    # 只重建脚本自己拥有的类型文件夹；SKILL.md 与作者放进来的其他文件不动
    for folder in TYPE_DIR.values():
        target = OUT / folder
        if target.exists():
            shutil.rmtree(target)
    OUT.mkdir(parents=True, exist_ok=True)

    counts: Counter[str] = Counter()
    genres: Counter[str] = Counter()
    archived = 0
    used_names: dict[Path, set[str]] = defaultdict(set)
    for card in cards:
        if card.get("status") == "archived":
            archived += 1
            continue
        ctype = card.get("card_type") or "unknown"
        folder = OUT / TYPE_DIR.get(ctype, ctype) / genre_folder(card)
        folder.mkdir(parents=True, exist_ok=True)
        base = safe_name(str(card.get("title") or ""), str(card.get("card_id") or "card"))
        # Windows 文件名不分大小写：按小写去重，否则 TOP1 与 top1 两张卡会互相覆盖
        name = base
        if name.lower() in used_names[folder]:
            # 后缀用 card_id 的哈希：两张卡的 id 也可能只差大小写，直接截尾仍会撞
            name = f"{base}-{hashlib.sha1(str(card.get('card_id', '')).encode('utf-8')).hexdigest()[:6]}"
        used_names[folder].add(name.lower())
        (folder / f"{name}.md").write_text(render_card(card), encoding="utf-8", newline="\n")
        counts[TYPE_DIR.get(ctype, ctype)] += 1
        genres[genre_folder(card)] += 1

    lines = [
        "# 网文知识库 · 索引",
        "",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"来源：`skills/novel-distillation/library/cards.jsonl`（sha256 `{digest[:16]}…`，{len(cards)} 行，坏行 {bad}，已归档 {archived} 张未导出）  ",
        "重新生成：仓库根目录运行 `.venv\\Scripts\\python.exe scripts\\build_knowledge_base.py`  ",
        "",
        "## 按类型",
        "",
        "| 文件夹 | 卡片数 |",
        "|---|---|",
    ]
    for folder, n in counts.most_common():
        lines.append(f"| `{folder}/` | {n} |")
    lines += ["", "## 按题材（前 20）", "", "| 题材 | 卡片数 |", "|---|---|"]
    for g, n in genres.most_common(20):
        lines.append(f"| {g} | {n} |")
    lines += [
        "",
        "## 说明",
        "",
        "- 每张卡只存机制、读者承诺、节奏与失败模式，不含原著文字；用时学路径、换实体。",
        "- 这些 `.md` 由脚本生成，手改会在下次重跑时被覆盖；要改内容请改 `cards.jsonl` 源文件。",
        "- `SKILL.md` 是给 Agent 加载的总说明，作者和 Agent 可以直接编辑，脚本不覆盖。",
        "",
    ]
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    if not (OUT / "SKILL.md").exists():
        (OUT / "SKILL.md").write_text(SKILL_MD, encoding="utf-8", newline="\n")

    total = sum(counts.values())
    print(f"knowledge base written: {OUT}  cards={total} archived_skipped={archived} bad_lines={bad}")
    for folder, n in counts.most_common():
        print(f"  {n:5d}  {folder}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
