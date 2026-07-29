#!/usr/bin/env python3
"""Create the standard full-chain novel project scaffold.

This script creates the 01-09 file layout and placeholder files only. It does
not generate project truth, review-ready settings, or publishable chapters.
"""

from __future__ import annotations

import argparse
import sys
from math import ceil
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


FILES = [
    ("01_项目定位/市场对标分析.md", "市场对标分析", "novel-market-analysis"),
    ("01_项目定位/目标读者画像.md", "目标读者画像", "novel-reader-avatar"),
    ("01_项目定位/书名与标签.md", "书名与标签", "novel-title-tags"),
    ("01_项目定位/小说简介.md", "小说简介", "novel-synopsis"),
    ("01_项目定位/核心爽点.md", "核心爽点", "novel-core-hook"),
    ("01_项目定位/核心卖点提炼.md", "核心卖点提炼", "novel-selling-points"),
    ("01_项目定位/文风规则.md", "文风规则", "novel-style-rules"),
    ("01_项目定位/统一术语表.md", "统一术语表", "novel-glossary"),
    ("02_世界观设定/世界观总览.md", "世界观总览", "novel-world-overview"),
    ("02_世界观设定/世界历史背景.md", "世界历史背景", "novel-world-history"),
    ("02_世界观设定/时间线总表.md", "时间线总表", "novel-timeline"),
    ("02_世界观设定/世界大地图.md", "世界大地图", "novel-world-map"),
    ("02_世界观设定/重要地点档案.md", "重要地点档案", "novel-location-dossier"),
    ("02_世界观设定/势力分布与关系.md", "势力分布与关系", "novel-faction-web"),
    ("02_世界观设定/力量体系等级.md", "力量体系等级", "novel-power-system"),
    ("02_世界观设定/物品与道具设定.md", "物品与道具设定", "novel-item-system"),
    ("02_世界观设定/金手指设定.md", "金手指设定", "novel-cheat-system"),
    ("02_世界观设定/世界规则与禁忌.md", "世界规则与禁忌", "novel-world-rules"),
    ("02_世界观设定/社会风俗与物价.md", "社会风俗与物价", "novel-society-prices"),
    ("02_世界观设定/世界观BUG修正.md", "世界观BUG修正", "novel-world-bugfix"),
    ("03_人物设定/主角完整档案.md", "主角完整档案", "novel-protagonist-dossier"),
    ("03_人物设定/人物成长弧光表.md", "人物成长弧光表", "novel-growth-arc"),
    ("03_人物设定/配角档案文件夹/女-男主配1.md", "女-男主配1", "novel-supporting-lead-1"),
    ("03_人物设定/配角档案文件夹/重要队友1.md", "重要队友1", "novel-supporting-ally-1"),
    ("03_人物设定/配角档案文件夹/路人配角模板.md", "路人配角模板", "novel-supporting-template"),
    ("03_人物设定/反派与BOSS档案.md", "反派与BOSS档案", "novel-antagonists"),
    ("03_人物设定/人物动机深挖.md", "人物动机深挖", "novel-motivation-deep-dive"),
    ("03_人物设定/人物关系网.md", "人物关系网", "novel-relationship-map"),
    ("03_人物设定/人物状态跟踪表.md", "人物状态跟踪表", "novel-status-tracker"),
    ("03_人物设定/人物口头禅台词库.md", "人物口头禅台词库", "novel-voice-bank"),
    ("04_剧情大纲/主线明线.md", "主线明线", "novel-main-plot"),
    ("04_剧情大纲/暗线隐线.md", "暗线隐线", "novel-hidden-plot"),
    ("04_剧情大纲/全书大框架.md", "全书大框架", "novel-book-outline"),
    ("04_剧情大纲/分卷大纲.md", "分卷大纲", "novel-volume-outline"),
    ("04_剧情大纲/黄金三章细纲.md", "黄金三章细纲", "novel-golden-three-chapters"),
    ("04_剧情大纲/章节细纲.md", "章节细纲", "novel-chapter-outline"),
    ("04_剧情大纲/爽点节奏表.md", "爽点节奏表", "novel-hook-rhythm"),
    ("04_剧情大纲/情绪曲线设计.md", "情绪曲线设计", "novel-emotion-curve"),
    ("04_剧情大纲/伏笔埋设与回收.md", "伏笔埋设与回收", "novel-foreshadowing"),
    ("04_剧情大纲/冲突升级设计.md", "冲突升级设计", "novel-conflict-escalation"),
    ("04_剧情大纲/支线剧情.md", "支线剧情", "novel-subplots"),
    ("04_剧情大纲/名场面设计.md", "名场面设计", "novel-setpiece-scenes"),
    ("04_剧情大纲/毒点避雷清单.md", "毒点避雷清单", "novel-toxic-avoidance"),
    ("04_剧情大纲/结局规划.md", "结局规划", "novel-ending-plan"),
    ("05_正文内容/正文总览.md", "正文总览", "novel-manuscript-overview"),
    ("05_正文内容/每日写作进度表.md", "每日写作进度表", "novel-writing-progress"),
    ("05_正文内容/备用片段.md", "备用片段", "novel-backup-snippets"),
    ("05_正文内容/修改记录.md", "修改记录", "novel-revision-log"),
    ("06_写作规范/章末总结升华规范.md", "章末总结升华规范", "novel-ending-sublimation"),
    ("06_写作规范/章末钩子设计规范.md", "章末钩子设计规范", "novel-cliffhanger-rule"),
    ("06_写作规范/节奏控制.md", "节奏控制", "novel-pacing-control"),
    ("06_写作规范/对话描写规范.md", "对话描写规范", "novel-dialogue-rules"),
    ("06_写作规范/章节校验清单.md", "章节校验清单", "novel-chapter-checklist"),
    ("07_运营数据/更新计划表.md", "更新计划表", "novel-update-plan"),
    ("07_运营数据/追读设计.md", "追读设计", "novel-retention-design"),
    ("07_运营数据/平台敏感词库.md", "平台敏感词库", "novel-sensitive-words"),
    ("08_素材库/环境描写.md", "环境描写", "novel-setting-desc"),
    ("08_素材库/战斗描写.md", "战斗描写", "novel-battle-scenes"),
    ("08_素材库/神态心理描写.md", "神态心理描写", "novel-psychology-scenes"),
    ("08_素材库/梗库与段子.md", "梗库与段子", "novel-joke-bank"),
    ("08_素材库/参考资料.md", "参考资料", "novel-reference-bank"),
    ("08_素材库/备用爽点库.md", "备用爽点库", "novel-hook-bank"),
    ("08_素材库/卡文急救方案.md", "卡文急救方案", "novel-stuck-rescue"),
    ("09_完本复盘/全书总结.md", "全书总结", "novel-full-summary"),
    ("09_完本复盘/读者反馈分析.md", "读者反馈分析", "novel-reader-feedback"),
    ("09_完本复盘/续作规划.md", "续作规划", "novel-sequel-plan"),
    ("09_完本复盘/可复用素材.md", "可复用素材", "novel-reusable-assets"),
]

CHINESE_DIGITS = {
    0: "零",
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
}


def chinese_number(value: int) -> str:
    if value <= 0:
        raise ValueError("volume numbers must be positive")
    if value <= 10:
        return CHINESE_DIGITS[value]
    if value < 20:
        return "十" + CHINESE_DIGITS[value % 10]
    if value < 100:
        tens, ones = divmod(value, 10)
        return CHINESE_DIGITS[tens] + "十" + (CHINESE_DIGITS[ones] if ones else "")
    return str(value)


def volume_dir(number: int) -> str:
    return f"第{chinese_number(number)}卷文件夹"


def chapter_range_dirs(volumes: int, chapters_per_volume: int, batch_size: int) -> list[str]:
    dirs: list[str] = []
    for volume in range(1, volumes + 1):
        base = f"05_正文内容/{volume_dir(volume)}"
        dirs.append(f"{base}/章纲")
        for start in range(1, chapters_per_volume + 1, batch_size):
            end = min(start + batch_size - 1, chapters_per_volume)
            dirs.append(f"{base}/{start:03d}-{end:03d}章")
    return dirs


def chapter_range_dirs_for_total(total_chapters: int, chapters_per_volume: int, batch_size: int) -> list[str]:
    volumes = ceil(total_chapters / chapters_per_volume)
    dirs: list[str] = []
    for volume in range(1, volumes + 1):
        base = f"05_正文内容/{volume_dir(volume)}"
        dirs.append(f"{base}/章纲")
        remaining = total_chapters - ((volume - 1) * chapters_per_volume)
        volume_chapters = min(chapters_per_volume, remaining)
        for start in range(1, volume_chapters + 1, batch_size):
            end = min(start + batch_size - 1, volume_chapters)
            dirs.append(f"{base}/{start:03d}-{end:03d}章")
    return dirs


def suggested_chapters_per_volume(target_words: int, words_per_chapter: int) -> int:
    if target_words <= 200_000:
        return max(20, ceil(target_words / words_per_chapter))
    if target_words <= 600_000:
        return 50
    if target_words <= 1_500_000:
        return 80
    if target_words <= 3_000_000:
        return 100
    return 120


def skill_chain(target_words: int) -> str:
    if target_words <= 200_000:
        return "轻量链"
    if target_words <= 600_000:
        return "中篇链"
    if target_words <= 1_500_000:
        return "标准长篇链"
    if target_words <= 3_000_000:
        return "长篇连载链"
    return "超长篇链"


def project_dir(title: str, base: Path) -> Path:
    clean = title.strip().strip("《》")
    if not clean:
        raise ValueError("title cannot be empty")
    return base / f"《{clean}》创作项目"


def placeholder(title: str, heading: str, skill: str) -> str:
    return (
        f"# {heading}\n\n"
        f"> 项目：{title}\n"
        f"> 专属技能：`{skill}`\n"
        "> 状态：待生成\n\n"
        "## 生成要求\n"
        "- 使用专属技能生成、更新和校验本文件。\n"
        "- 读取上游文件后再写入内容。\n"
        "- 写完后检查是否影响下游文件。\n\n"
        "## 正文\n"
        "待生成。\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="novel title, with or without 《》")
    parser.add_argument("--path", default=".", help="parent directory for the project")
    parser.add_argument("--target-words", type=int, help="target total word count; used to infer chapters and volumes")
    parser.add_argument("--words-per-chapter", type=int, default=2500, help="target words per chapter when inferring scale")
    parser.add_argument("--volumes", type=int, help="override number of volume folders to scaffold")
    parser.add_argument("--chapters-per-volume", type=int, help="override chapter slots per volume")
    parser.add_argument("--batch-size", type=int, default=50, help="chapter directory range size")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing markdown placeholders")
    parser.add_argument("--dry-run", action="store_true", help="print planned paths without writing files")
    args = parser.parse_args()
    if args.target_words is not None and args.target_words < 1:
        parser.error("--target-words must be at least 1")
    if args.words_per_chapter < 1:
        parser.error("--words-per-chapter must be at least 1")
    if args.volumes is not None and args.volumes < 1:
        parser.error("--volumes must be at least 1")
    if args.chapters_per_volume is not None and args.chapters_per_volume < 1:
        parser.error("--chapters-per-volume must be at least 1")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    base = Path(args.path).expanduser().resolve()
    root = project_dir(args.title, base)
    created = 0
    skipped = 0
    total_chapters = None
    inferred_chain = None
    if args.target_words is not None:
        total_chapters = ceil(args.target_words / args.words_per_chapter)
        inferred_chain = skill_chain(args.target_words)
        if args.chapters_per_volume is not None:
            chapters_per_volume = args.chapters_per_volume
        elif args.volumes is not None:
            chapters_per_volume = ceil(total_chapters / args.volumes)
        else:
            chapters_per_volume = suggested_chapters_per_volume(args.target_words, args.words_per_chapter)
        if args.volumes is not None:
            dirs = chapter_range_dirs(args.volumes, chapters_per_volume, args.batch_size)
        else:
            dirs = chapter_range_dirs_for_total(total_chapters, chapters_per_volume, args.batch_size)
    else:
        volumes = args.volumes or 6
        chapters_per_volume = args.chapters_per_volume or 100
        dirs = chapter_range_dirs(volumes, chapters_per_volume, args.batch_size)

    for rel in dirs:
        target = root / rel
        if args.dry_run:
            print(f"dir  {target}")
            continue
        target.mkdir(parents=True, exist_ok=True)

    for rel, heading, skill in FILES:
        target = root / rel
        if args.dry_run:
            print(f"file {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not args.overwrite:
            skipped += 1
            continue
        target.write_text(placeholder(args.title.strip().strip("《》"), heading, skill), encoding="utf-8")
        created += 1

    if not args.dry_run:
        print(f"Project: {root}")
        print("Mode: scaffold_only")
        print("Truth status: placeholders_created_not_generated")
        print("Next step: run file-level skills or pressure-test filler before treating the project as writable truth.")
        if args.target_words is not None:
            print(f"Target words: {args.target_words}")
            print(f"Words per chapter: {args.words_per_chapter}")
            print(f"Estimated chapters: {total_chapters}")
            print(f"Suggested skill chain: {inferred_chain}")
        print(f"Created/updated files: {created}")
        print(f"Skipped existing files: {skipped}")
        print(f"Volume folders: {sum(1 for item in dirs if item.endswith('/章纲'))}")
        print(f"Chapter/brief directories: {len(dirs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
