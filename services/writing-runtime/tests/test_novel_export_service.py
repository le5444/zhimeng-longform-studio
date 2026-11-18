"""导出 TXT / EPUB / DOCX 的行为验收。

作者对导出的硬要求（每条都在下面有断言）：
- 「版式要和小说版式一样，不要有空段，无意义字符」→ 零 Markdown 残留、零空段
- 「md 不太方便复制粘贴，要不同时输出 epub」→ EPUB 必须是合法可读的
- Word 目录要「真实可点击」，不是只提示按 F9 → 书签 + 内部超链接
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from xml.dom.minidom import parseString

import pytest

from zhimeng_runtime import novel_export_service as export


# ----------------------------------------------------------------------
# 清洗


def test_strip_markdown_removes_syntax_and_never_emits_blank_paragraphs() -> None:
    raw = (
        "# 第一章 标题\n"
        "\n"
        "他**猛地**回头。\n"
        "\n"
        "\n"
        "> 引用一句\n"
        "- 列表项\n"
        "1. 编号项\n"
        "\n"
        "---\n"
        "\n"
        "| 表头 | 表头 |\n"
        "|---|---|\n"
        "\n"
        "看 [这里](http://x.com) 和 ![图](y.png)。\n"
        "\n"
        "```python\n"
        "print('不该出现')\n"
        "```\n"
        "\n"
        "剩下*一句*话。\n"
    )
    paragraphs = export.strip_markdown(raw)

    assert paragraphs == [
        "第一章 标题",
        "他猛地回头。",
        "引用一句",
        "列表项",
        "编号项",
        "看 这里 和 。",
        "剩下一句话。",
    ]
    # 硬底线：任何空白段都不许留下
    assert all(p.strip() for p in paragraphs)
    assert "print" not in "".join(paragraphs)


def test_strip_markdown_joins_soft_wrapped_lines_into_one_paragraph() -> None:
    """段内换行要拼成一段，否则导出的小说满是断行。"""
    assert export.strip_markdown("上半句，\n下半句。\n\n第二段。") == [
        "上半句，下半句。",
        "第二段。",
    ]


def test_title_from_stem_strips_number_prefix_and_chapter_marker() -> None:
    assert export.title_from_stem("004_玉佩上的青云", 4) == "玉佩上的青云"
    assert export.title_from_stem("第4章_玉佩上的青云", 4) == "玉佩上的青云"
    assert export.title_from_stem("004_玉佩上的青云_控制卡", 4) == "玉佩上的青云"
    assert export.title_from_stem("004", 4) == ""


def test_chapter_heading_does_not_double_the_chapter_marker() -> None:
    assert export.ExportChapter(4, "玉佩上的青云", ["x"]).heading == "第4章 玉佩上的青云"
    assert export.ExportChapter(4, "第4章 玉佩", ["x"]).heading == "第4章 玉佩"
    assert export.ExportChapter(4, "", ["x"]).heading == "第4章"


# ----------------------------------------------------------------------
# 收集


class _File:
    def __init__(self, file_id: str, relative_path: str, kind: str = "prose") -> None:
        self.file_id = file_id
        self.relative_path = relative_path
        self.kind = kind


class _Chapter:
    def __init__(self, number, prose_file_ids, file_ids, volume_id=None) -> None:
        self.number = number
        self.prose_file_ids = prose_file_ids
        self.file_ids = file_ids
        self.volume_id = volume_id


class _Volume:
    def __init__(self, volume_id: str, number: int) -> None:
        self.volume_id = volume_id
        self.number = number


class _Inspection:
    def __init__(self, chapters, files, volumes) -> None:
        self.chapters = chapters
        self.files = files
        self.volumes = volumes


@pytest.fixture()
def book_root(tmp_path):
    prose = tmp_path / "05_正文内容"
    prose.mkdir(parents=True)
    (prose / "001_开局.md").write_text("他推开门。\n\n院子里安静。\n", encoding="utf-8")
    (prose / "002_对峙.md").write_text("刀出鞘。\n\n没人说话。\n", encoding="utf-8")
    outline = prose / "章纲"
    outline.mkdir()
    (outline / "第003章_还没写_控制卡.md").write_text(
        "# 本章要写什么\n\n- 主角要拿到玉佩\n", encoding="utf-8"
    )
    return tmp_path


def test_collect_book_never_exports_a_control_card_as_prose(book_root) -> None:
    """第 3 章只有章纲没有正文：绝不能把「本章要写什么」当正文导出去。"""
    files = [
        _File("f1", "05_正文内容/001_开局.md"),
        _File("f2", "05_正文内容/002_对峙.md"),
        _File("f3", "05_正文内容/章纲/第003章_还没写_控制卡.md", kind="control"),
    ]
    chapters = [
        _Chapter(1, ["f1"], ["f1"], "VOL001"),
        _Chapter(2, ["f2"], ["f2"], "VOL001"),
        _Chapter(3, [], ["f3"], "VOL001"),  # 没有正文，只有控制卡
    ]
    inspection = _Inspection(chapters, files, [_Volume("VOL001", 1)])

    book = export.collect_book(inspection, book_root, "试验书")

    assert [chapter.number for chapter in book.chapters] == [1, 2]
    joined = "\n".join(
        text for chapter in book.chapters for text in chapter.paragraphs
    )
    assert "主角要拿到玉佩" not in joined
    assert "本章要写什么" not in joined


# ----------------------------------------------------------------------
# 三种格式


@pytest.fixture()
def sample_book() -> export.ExportBook:
    return export.ExportBook(
        title="试验书",
        intro="一句简介。",
        volumes=[
            export.ExportVolume(
                number=1,
                heading="第 1 卷",
                chapters=[
                    export.ExportChapter(1, "开局", ["他推开门。", "院子里安静。"]),
                    export.ExportChapter(2, "对峙", ["刀出鞘。"]),
                ],
            )
        ],
    )


def test_render_txt_is_clean_novel_layout(sample_book) -> None:
    text = export.render_txt(sample_book)
    assert text.startswith("《试验书》")
    assert "第1章 开局" in text
    assert "他推开门。" in text
    # 不留 Markdown、不留 4 连换行（即不留空段）
    assert "**" not in text and "# " not in text
    assert "\n\n\n\n" not in text


def test_render_epub_is_a_valid_readable_package(sample_book) -> None:
    blob = export.render_epub(sample_book, "book-1")
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        infos = zf.infolist()
        # mimetype 必须第一个且不压缩，否则很多阅读器判为损坏
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert zf.testzip() is None

        names = set(zf.namelist())
        assert {"META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
                "OEBPS/toc.ncx", "OEBPS/chap0001.xhtml", "OEBPS/chap0002.xhtml"} <= names

        nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
        assert nav.count("<li>") == 3  # 简介 + 两章
        assert "第1章 开局" in nav

        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        assert opf.count("<itemref") == 3

        chapter = zf.read("OEBPS/chap0001.xhtml").decode("utf-8")
        assert "<h1>第1章 开局</h1>" in chapter
        assert chapter.count("<p>") == 2

        # 所有 XML 必须能解析，否则阅读器直接打不开
        for name in names:
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                parseString(zf.read(name))


def test_render_epub_escapes_xml_so_a_book_title_cannot_break_the_package() -> None:
    book = export.ExportBook(
        title="风<起>&落",
        intro=None,
        volumes=[
            export.ExportVolume(1, "", [export.ExportChapter(1, "他说\"走\"", ["A & B"])])
        ],
    )
    with zipfile.ZipFile(BytesIO(export.render_epub(book, "id-1"))) as zf:
        for name in zf.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                parseString(zf.read(name))
        assert "&amp;" in zf.read("OEBPS/content.opf").decode("utf-8")


def test_render_docx_has_clickable_toc_not_just_a_press_f9_hint(sample_book) -> None:
    blob = export.render_docx(sample_book)
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        assert zf.testzip() is None
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/styles.xml", "word/_rels/document.xml.rels"} <= set(zf.namelist())

        doc = zf.read("word/document.xml").decode("utf-8")
        # 每章一个书签 + 一个指向它的内部超链接：打开就能点，不用按 F9
        assert doc.count("<w:bookmarkStart") == 2
        assert doc.count('<w:hyperlink w:anchor="ch1"') == 1
        assert doc.count('<w:hyperlink w:anchor="ch2"') == 1
        # 章标题用 Heading1，Word 导航窗格才能识别
        assert doc.count('w:val="Heading1"') >= 4
        assert "第1章 开局" in doc

        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                parseString(zf.read(name))


def test_render_docx_escapes_xml(sample_book) -> None:
    book = export.ExportBook(
        title="A<B>&C",
        intro=None,
        volumes=[export.ExportVolume(1, "", [export.ExportChapter(1, "x", ["1 < 2 & 3"])])],
    )
    with zipfile.ZipFile(BytesIO(export.render_docx(book))) as zf:
        parseString(zf.read("word/document.xml"))
