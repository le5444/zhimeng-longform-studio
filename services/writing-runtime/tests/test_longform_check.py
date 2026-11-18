from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.longform_check_service import _prose_files, check_project_for_author
from zhimeng_runtime.project_reader import (
    SECTION_LABELS,
    in_numbered_non_prose_section,
    prose_directories,
)

REAL_BOOK = Path("__local_acceptance_fixture_not_distributed__")


def _create_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    (root / "03_人物设定" / "人物状态跟踪表.md").write_text(
        "# 人物当前状态表\n\n"
        "## 沈青云（主角）\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前伤势 | 左臂已断 |\n"
        "| 当前境界 | 引气入门 |\n"
        "| 对主角态度 | 自己 |\n",
        encoding="utf-8",
    )
    (root / "02_世界观设定" / "世界规则与禁忌.md").write_text(
        "# 世界规则与禁忌\n\n"
        "## 禁忌\n"
        "- 不允许把药膳直接写成高阶丹药。\n"
        "- 不允许系统无代价直接抬修为。\n",
        encoding="utf-8",
    )
    (root / "04_剧情大纲" / "伏笔埋设与回收.md").write_text(
        "# 未回收伏笔表\n\n"
        "| ID | 埋设章节 | 伏笔内容 | 状态 | 计划回收 | 回收方式 |\n"
        "|---|---|---|---|---|---|\n"
        "| F01 | 第1章 | 青云玉来历 | 🟢 | 第2章 | 本章应碰到 |\n"
        "| F02 | 第1章 | 旧址缝隙 | 🟢 | 第1章 | 已过期 |\n",
        encoding="utf-8",
    )
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    (volume / "001_开篇.md").write_text(
        "沈青云走进前院。\n\n「问题不大。」他挥出双掌。\n",
        encoding="utf-8",
    )
    (volume / "002_来访.md").write_text(
        "沈青云挥出双掌，掌风如刀。药膳端上桌，转眼变成高阶丹药。"
        "「青云玉来历还没人提。」\n",
        encoding="utf-8",
    )


def test_longform_check_reports_pacing_setting_and_foreshadow(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    result = check_project_for_author(project, chapter_id="CH002")

    assert result["advisory_only"] is True
    assert result["pacing"]["char_count"] > 20
    assert "对白" in result["pacing"]["summary"] or "字" in result["pacing"]["summary"]
    titles = " ".join(item["title"] for item in result["setting_conflicts"])
    assert "双掌" in titles or "药膳" in titles
    ledger_titles = " ".join(item["title"] for item in result.get("ledger_suggestions") or [])
    assert "青云玉" in ledger_titles


def test_pacing_check_treats_short_sentences_as_an_author_choice(tmp_path: Path) -> None:
    project = tmp_path / "short-style-book"
    _create_project(project)
    chapter = project / "05_正文内容" / "第一卷" / "CH002_夜探.md"
    chapter.write_text(
        "门开了。\n\n风停了。\n\n他抬头。\n\n刀已出鞘。\n\n人群猛地散开。",
        encoding="utf-8",
    )

    result = check_project_for_author(project, chapter_id="CH002")
    summary = result["pacing"]["summary"]

    assert "短句约占" in summary
    assert "如果不是刻意加速" in summary
    assert "短句过多" not in summary
    assert "这一章偏闷" not in summary
    assert "平均每句" not in summary
    assert result.get("cast") is not None
    assert result.get("hook_bridge") is not None
    hands = next(
        (item for item in result["setting_conflicts"] if "双掌" in item["title"] or "双手" in item["title"]),
        None,
    )
    if hands is not None:
        assert hands.get("passage")
        assert "沈青云" in str(hands["passage"])
    foreshadow_titles = " ".join(item["title"] for item in result["foreshadows"])
    assert "F02" in foreshadow_titles
    assert result["checked"]["character_count"] == 1
    assert result["checked"]["foreshadow_count"] == 2
    assert "已核对人物状态" in result["writing_brief"]
    assert "该补进账本" in result["writing_brief"]


def test_longform_check_preserves_full_workbench_chapter_identity(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)

    result = check_project_for_author(project, chapter_id="VOL001-CH002")

    assert result["chapter_id"] == "VOL001-CH002"


def test_longform_check_api_uses_bound_project(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    with TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(library,),
                allowed_origins=(),
            )
        )
    ) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]
        response = client.get(
            f"/api/v1/projects/{project_id}/longform-check",
            params={"chapter_id": "CH002"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schema"] == "zhimeng.longform-check.v1"
        assert payload["project_id"] == project_id
        assert payload["pacing"]["char_count"] > 20
        assert payload["checked"]["character_count"] >= 1
        assert "ledger_suggestions" in payload
        assert "cast" in payload
        assert "hook_bridge" in payload


def test_longform_check_on_real_zongmen_book() -> None:
    if not REAL_BOOK.is_dir():
        return
    result = check_project_for_author(REAL_BOOK, chapter_id="CH002")
    assert result["pacing"]["char_count"] > 500
    assert result["checked"]["character_count"] >= 1
    assert result["checked"]["foreshadow_count"] >= 1
    assert result["author_summary"]
    assert "第 002 章" in result["chapter_label"]


def test_real_zongmen_chapter_four_does_not_repeat_already_logged_foreshadows() -> None:
    if not REAL_BOOK.is_dir():
        return

    result = check_project_for_author(REAL_BOOK, chapter_id="VOL001-CH004")
    titles = " ".join(item["title"] for item in result["foreshadows"])

    assert "F02" not in titles
    assert "F03" not in titles
    assert result["chapter_id"] == "VOL001-CH004"


def test_longform_check_flags_unbridged_hook_and_missing_cast(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    (project / "03_人物设定" / "人物状态跟踪表.md").write_text(
        "# 人物当前状态表\n\n"
        "## 沈青云（主角）\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前伤势 | 无 |\n"
        "| 当前位置 | 在场 |\n\n"
        "## 柳青烟\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前位置 | 本章应出场 |\n",
        encoding="utf-8",
    )
    volume = project / "05_正文内容" / "第一卷"
    (volume / "001_开篇.md").write_text("沈青云停在门口。门外是谁？\n", encoding="utf-8")
    (volume / "002_来访.md").write_text("厨房里药膳还在锅里响。\n", encoding="utf-8")
    result = check_project_for_author(project, chapter_id="CH002")
    hook_titles = " ".join(item["title"] for item in result["hook_bridge"])
    assert "钩子" in hook_titles
    cast_titles = " ".join(item["title"] for item in result["cast"])
    assert "柳青烟" in cast_titles
    assert "沈青云" in cast_titles


def test_cast_does_not_treat_away_or_unscheduled_lead_as_missing(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    (project / "03_人物设定" / "人物状态跟踪表.md").write_text(
        "# 人物当前状态表\n\n"
        "## 沈青云（主角）\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前伤势 | 无 |\n\n"
        "## 柳青烟\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前位置 | 不在场 |\n",
        encoding="utf-8",
    )
    volume = project / "05_正文内容" / "第一卷"
    (volume / "002_来访.md").write_text("厨房里药膳还在锅里响。\n", encoding="utf-8")
    result = check_project_for_author(project, chapter_id="CH002")
    assert result["cast"] == []


def test_dormant_foreshadow_without_a_window_is_flagged(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    (project / "04_剧情大纲" / "伏笔埋设与回收.md").write_text(
        "# 未回收伏笔表\n\n"
        "| ID | 埋设章节 | 伏笔内容 | 状态 | 计划回收 | 回收方式 |\n"
        "|---|---|---|---|---|---|\n"
        "| F07 | 第1章 | 灶房地砖下的铁盒 | 🟢 | 待定 | 待定 |\n",
        encoding="utf-8",
    )
    result = check_project_for_author(project, chapter_id="CH040")
    titles = " ".join(item["title"] for item in result["foreshadows"])
    assert "F07" in titles
    assert "没写回收窗口" in titles


def test_tracked_character_missing_for_five_chapters_is_flagged(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    (project / "03_人物设定" / "人物状态跟踪表.md").write_text(
        "# 人物当前状态表\n\n"
        "## 沈青云（主角）\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前伤势 | 无 |\n\n"
        "## 柳青烟\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前境界 | 引气 |\n\n"
        "## 陆远舟\n\n"
        "| 维度 | 状态 |\n|---|---|\n"
        "| 当前位置 | 远行 |\n",
        encoding="utf-8",
    )
    volume = project / "05_正文内容" / "第一卷"
    for number in range(2, 7):
        (volume / f"{number:03d}_日常.md").write_text(
            "沈青云在院里练剑，掌风扫过石阶。\n",
            encoding="utf-8",
        )
    result = check_project_for_author(project, chapter_id="CH006")
    cast_titles = " ".join(item["title"] for item in result["cast"])
    assert "柳青烟已经连续 5 章没出现" in cast_titles
    assert "陆远舟" not in cast_titles
    assert "沈青云已经连续" not in cast_titles


def test_banned_list_hits_are_reported(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    (project / "01_项目定位" / "禁用清单.md").write_text(
        "# 本书禁用\n\n- 深吸一口气\n- 命运的齿轮\n- 淡淡\n",
        encoding="utf-8",
    )
    volume = project / "05_正文内容" / "第一卷"
    (volume / "002_来访.md").write_text(
        "沈青云深吸一口气，又深吸一口气，淡淡道：走。\n",
        encoding="utf-8",
    )
    result = check_project_for_author(project, chapter_id="CH002")
    banned_titles = " ".join(item["title"] for item in result["banned"])
    assert "深吸一口气」出现 2 次" in banned_titles
    assert "淡淡" in banned_titles
    assert "命运的齿轮" not in banned_titles
    assert "撞了本书禁用清单" in result["writing_brief"]


def test_banned_lists_are_found_wherever_the_author_put_them(tmp_path: Path) -> None:
    """清单在哪个夹子、拆成几份都算；报的路径必须是它真正所在的那份。"""
    project = tmp_path / "book"
    _create_project(project)
    (project / "01_补充定位").mkdir()
    (project / "规矩").mkdir()
    (project / "01_项目定位" / "禁用清单.md").write_text("- 深吸一口气\n", encoding="utf-8")
    (project / "01_补充定位" / "禁用清单.md").write_text("- 淡淡道\n", encoding="utf-8")
    (project / "规矩" / "本书禁用.md").write_text("- 药膳端上桌\n", encoding="utf-8")
    volume = project / "05_正文内容" / "第一卷"
    (volume / "002_来访.md").write_text(
        "沈青云深吸一口气，淡淡道：药膳端上桌了。\n", encoding="utf-8"
    )

    result = check_project_for_author(project, chapter_id="CH002")
    sources = {item["quote"]: item["source_file"] for item in result["banned"]}

    assert sources == {
        "深吸一口气": "01_项目定位/禁用清单.md",
        "淡淡道": "01_补充定位/禁用清单.md",
        "药膳端上桌": "规矩/本书禁用.md",
    }


def test_banned_list_ignores_archives_and_character_tic_notes(tmp_path: Path) -> None:
    """作者标了「勿作为真值」的归档不算清单；「人物口癖」是设定，也不能当黑名单扫。"""
    project = tmp_path / "book"
    _create_project(project)
    (project / "00_归档_旧结构_勿作为真值").mkdir()
    (project / "03_人物设定" / "人物口癖.md").write_text(
        "## 沈青云\n\n- 深吸一口气\n", encoding="utf-8"
    )
    (project / "00_归档_旧结构_勿作为真值" / "禁用清单.md").write_text(
        "- 掌风如刀\n", encoding="utf-8"
    )
    volume = project / "05_正文内容" / "第一卷"
    (volume / "002_来访.md").write_text(
        "沈青云深吸一口气，掌风如刀。\n", encoding="utf-8"
    )

    result = check_project_for_author(project, chapter_id="CH002")

    assert result["banned"] == []


def test_hook_ignores_common_que_without_a_question(tmp_path: Path) -> None:
    project = tmp_path / "book"
    _create_project(project)
    volume = project / "05_正文内容" / "第一卷"
    (volume / "001_开篇.md").write_text("沈青云却没有回头。忽然风停了。\n", encoding="utf-8")
    (volume / "002_来访.md").write_text("厨房里药膳还在锅里响。\n", encoding="utf-8")
    result = check_project_for_author(project, chapter_id="CH002")
    assert result["hook_bridge"] == []


# ---- 正文夹子不再写死 05_正文内容 ----

def test_default_layout_still_reads_only_the_default_prose_folder(tmp_path: Path) -> None:
    """默认摆法的书行为一字不变：哪怕作者另建了带「草稿」字样的夹子也不掺进来。"""
    project = tmp_path / "book"
    _create_project(project)
    elsewhere = project / "草稿备份"
    elsewhere.mkdir()
    (elsewhere / "001_旧开篇.md").write_text("不要读到这份。\n", encoding="utf-8")

    assert prose_directories(project) == [project / "05_正文内容"]
    assert sorted(_prose_files(project)) == [1, 2]
    assert _prose_files(project)[1].name == "001_开篇.md"


def test_check_reads_chapters_from_a_renamed_prose_folder(tmp_path: Path) -> None:
    """作者把正文夹子改了名（甚至去掉 0N_ 编号），检查不能说「一章都没有」。"""
    project = tmp_path / "book"
    _create_project(project)
    (project / "05_正文内容").rename(project / "正文")

    found = _prose_files(project)
    assert sorted(found) == [1, 2]
    assert prose_directories(project) == [project / "正文"]

    result = check_project_for_author(project, chapter_id="CH002")
    assert result["pacing"]["char_count"] > 20
    assert result["checked"]["character_count"] == 1


def test_same_number_section_is_prose_but_other_numbered_folders_are_not(
    tmp_path: Path,
) -> None:
    """同号 05_ 的夹子算正文；作者标了别的 0N_ 号的绝不是正文，
    哪怕里面那份文件带着章号（第 12 章细纲不是第 12 章正文）。"""
    project = tmp_path / "book"
    project.mkdir()
    prose = project / "05_稿件"
    prose.mkdir()
    (prose / "012_血钥.md").write_text("沈青云握住了血钥。\n", encoding="utf-8")
    outline = project / "04_剧情大纲"
    outline.mkdir()
    (outline / "第12章_细纲.md").write_text("本章要回收血钥。\n", encoding="utf-8")

    assert prose_directories(project) == [prose]
    assert _prose_files(project)[12] == prose / "012_血钥.md"
    assert in_numbered_non_prose_section("04_剧情大纲/第12章_细纲.md") is True
    assert in_numbered_non_prose_section("05_稿件/012_血钥.md") is False
    assert in_numbered_non_prose_section("章节/012_血钥.md") is False


def test_whole_book_fallback_finds_numbered_chapters_outside_any_template(
    tmp_path: Path,
) -> None:
    """没有 0N_ 结构、也没有带「正文」字样的夹子时，退让到整本书扫描，
    但同号非正文分区和归档仍要挡掉。"""
    project = tmp_path / "book"
    project.mkdir()
    chapters = project / "章节"
    chapters.mkdir()
    (chapters / "001_开篇.md").write_text("沈青云走进前院。\n", encoding="utf-8")
    (chapters / "002_来访.md").write_text("沈青云挥出双掌。\n", encoding="utf-8")
    plans = project / "04_剧情大纲"
    plans.mkdir()
    (plans / "第1章_细纲.md").write_text("第一章要出场。\n", encoding="utf-8")
    archive = project / "00_归档_勿作为真值"
    archive.mkdir()
    (archive / "001_废稿.md").write_text("废掉的开篇。\n", encoding="utf-8")

    assert prose_directories(project) == [project]
    found = _prose_files(project)
    assert sorted(found) == [1, 2]
    assert found[1] == chapters / "001_开篇.md"
