"""把作品导出成作者能直接用的格式：TXT / EPUB / DOCX。

为什么单独一个模块：
- 取章、清 Markdown、切段这套逻辑三种格式共用一条，别复制三份（DRY）。
- project_api.py 已经 1900 多行，导出逻辑不该再往路由里塞。

作者对导出的原话（都兑现在这里）：
- 「md 不太方便复制粘贴，要不你就在弄 md 章节的同时输出 epub 文件呢」
- 「以后生成小说，改小说，都同步输出，而且版式要和小说版式一样，
   不要有空段，无意义字符」
- DOCX 要「真实可点击目录」，不是只提示按 F9

全部只用标准库（zipfile + xml.sax.saxutils），不引入 ebooklib / python-docx —
桌面包要能在没装任何东西的机器上跑。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# ----------------------------------------------------------------------
# 结构

@dataclass
class ExportChapter:
    number: int
    title: str
    paragraphs: list[str]

    @property
    def heading(self) -> str:
        """「第3章 章名」；文件名里已经带「第N章」就不再套一层。"""
        marker_spaced = f"第 {self.number} 章"
        marker = f"第{self.number}章"
        if self.title.startswith(marker) or self.title.startswith(marker_spaced):
            return self.title
        if not self.title:
            return marker
        return f"{marker} {self.title}"


@dataclass
class ExportVolume:
    number: int
    heading: str
    chapters: list[ExportChapter] = field(default_factory=list)


@dataclass
class ExportBook:
    title: str
    intro: str | None
    volumes: list[ExportVolume] = field(default_factory=list)
    # 收集时被跳过的章节，逐条写清原因。
    # 以前这些 continue 一声不响：200 章的书中间几章读不出或格式异常，
    # 导出的文件会安静地少掉那几章，接口还是 200、文件照样下载，
    # 作者贴到平台上才发现断章。
    skipped: list[str] = field(default_factory=list)

    @property
    def chapters(self) -> list[ExportChapter]:
        return [chapter for volume in self.volumes for chapter in volume.chapters]

    @property
    def has_real_volumes(self) -> bool:
        """只有一卷且没有卷名时不必在正文里插卷标题。"""
        return len(self.volumes) > 1


# ----------------------------------------------------------------------
# 正文清洗

_MD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 图片先于链接处理，否则 ![]() 会被链接规则吃掉前缀
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*"), ""),
    (re.compile(r"^\s{0,3}>\s?"), ""),
    (re.compile(r"^\s{0,3}[-*+]\s+"), ""),
    (re.compile(r"^\s{0,3}\d+[.)]\s+"), ""),
    (re.compile(r"\*\*\*(.+?)\*\*\*"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"`+([^`]*)`+"), r"\1"),
)

_HORIZONTAL_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
# 这些行本身就是一个块：清掉标记后各自成段，不能和上一行拼在一起，
# 否则「> 引用」紧跟「- 列表」会黏成「引用一句列表项」。
_BLOCK_START = re.compile(r"^\s{0,3}(?:#{1,6}\s|>|[-*+]\s|\d+[.)]\s)")


def strip_markdown(text: str) -> list[str]:
    """把一章 Markdown 正文清成纯段落列表。

    作者要「版式和小说版式一样，不要有空段，无意义字符」，所以：
    - 去掉 #、**、-、>、```、表格、分割线、链接语法
    - 空行只当段落分隔符，不作为空段输出
    - 段内换行（软换行）拼成一段，不留断行
    """
    paragraphs: list[str] = []
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        if buffer:
            joined = "".join(buffer).strip()
            if joined:
                paragraphs.append(joined)
            buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            flush()
            continue
        if in_fence:
            continue
        if not line.strip():
            flush()
            continue
        if _HORIZONTAL_RULE.match(line) or _TABLE_ROW.match(line):
            flush()
            continue
        starts_block = bool(_BLOCK_START.match(line))
        cleaned = line
        for pattern, repl in _MD_PATTERNS:
            cleaned = pattern.sub(repl, cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        if starts_block:
            flush()
            paragraphs.append(cleaned)
            continue
        buffer.append(cleaned)
    flush()
    return paragraphs


_CHAPTER_STEM_PREFIX = re.compile(r"^0*\d{1,6}(?:[\s_.\-]+|$)")
_CONTROL_SUFFIX = re.compile(r"[_\-\s]*(?:控制卡|章纲|大纲)$")


def title_from_stem(stem: str, number: int) -> str:
    """从「004_玉佩上的青云」这类文件名取出章节名。

    项目模型里 ProjectChapter/ProjectVolume 都没有 title 字段（章名只存在
    文件名里），旧的 export-txt 直接读 ch.title / vol_obj.title，
    对任何有分卷的书都会 AttributeError → 500。
    """
    name = _CHAPTER_STEM_PREFIX.sub("", stem).strip()
    name = _CONTROL_SUFFIX.sub("", name).strip()
    name = re.sub(r"^第\s*0*%d\s*章[\s_\-:：]*" % number, "", name).strip()
    return name


# ----------------------------------------------------------------------
# 从项目快照收集

def collect_book(inspection, root_path: Path, display_name: str) -> ExportBook:
    """把项目快照收集成可导出的结构。三种格式共用这一条路径。"""
    intro = _read_intro(root_path)
    files_by_id = {item.file_id: item for item in inspection.files}
    volumes_by_id = {volume.volume_id: volume for volume in inspection.volumes}

    buckets: dict[str | None, ExportVolume] = {}
    skipped: list[str] = []
    for chapter in sorted(inspection.chapters, key=lambda item: item.number):
        # 只导正文。绝不回退到 file_ids —— 那里面还有章纲/控制卡，
        # 回退会把「本章要写什么」当成「本章正文」导给读者。
        prose_ids = list(getattr(chapter, "prose_file_ids", None) or [])
        if not prose_ids:
            skipped.append(f"第{chapter.number}章：还没有正文文件")
            continue
        file_obj = next(
            (files_by_id[fid] for fid in prose_ids if fid in files_by_id), None
        )
        if file_obj is None:
            skipped.append(f"第{chapter.number}章：正文文件不在快照里")
            continue
        if getattr(file_obj, "kind", None) not in (None, "prose"):
            skipped.append(f"第{chapter.number}章：{file_obj.relative_path} 不是正文文件")
            continue
        chapter_path = root_path / file_obj.relative_path
        try:
            raw = chapter_path.read_text(encoding="utf-8")
        except OSError as error:
            # 单章读不出来不能让整本导出失败，但必须让作者知道少了哪一章。
            skipped.append(f"第{chapter.number}章：读不出来（{type(error).__name__}）")
            continue
        paragraphs = strip_markdown(raw)
        if not paragraphs:
            skipped.append(f"第{chapter.number}章：清洗后没有正文内容")
            continue
        stem = Path(file_obj.relative_path).stem
        title = title_from_stem(stem, chapter.number)

        volume_id = getattr(chapter, "volume_id", None)
        bucket = buckets.get(volume_id)
        if bucket is None:
            volume_obj = volumes_by_id.get(volume_id) if volume_id else None
            number = getattr(volume_obj, "number", 0) if volume_obj else 0
            bucket = ExportVolume(
                number=number,
                heading=f"第 {number} 卷" if volume_obj else "",
            )
            buckets[volume_id] = bucket
        bucket.chapters.append(
            ExportChapter(number=chapter.number, title=title, paragraphs=paragraphs)
        )

    volumes = [bucket for bucket in buckets.values() if bucket.chapters]
    volumes.sort(key=lambda item: (item.number, item.chapters[0].number))
    return ExportBook(title=display_name, intro=intro, volumes=volumes, skipped=skipped)


def _read_intro(root_path: Path) -> str | None:
    # 老书：项目卡片的「核心立意与简介」；新书：《这本书.md》的「现在的想法」。
    for relative, heading in (
        ("01_项目定位/01_项目卡片.md", "## 核心立意与简介"),
        ("这本书.md", "## 现在的想法"),
    ):
        try:
            text = (root_path / relative).read_text(encoding="utf-8")
        except OSError:
            continue
        if heading in text:
            return _intro_section(text, heading)
    return None


def _intro_section(text: str, heading: str) -> str | None:
    body = text.split(heading, 1)[1]
    # 简介到下一个二级标题为止，别把整份项目卡都当简介。
    body = re.split(r"\n#{1,2} ", body, maxsplit=1)[0]
    paragraphs = strip_markdown(body)
    return "\n".join(paragraphs) if paragraphs else None


# ----------------------------------------------------------------------
# TXT

def render_txt(book: ExportBook) -> str:
    lines: list[str] = [f"《{book.title}》", ""]
    if book.intro:
        lines.extend(["【作品简介】", book.intro, "", "=" * 20, ""])
    for volume in book.volumes:
        if book.has_real_volumes and volume.heading:
            lines.extend(["=" * 20, volume.heading, "=" * 20, ""])
        for chapter in volume.chapters:
            lines.append(chapter.heading)
            lines.append("")
            lines.extend(chapter.paragraphs)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# EPUB（EPUB 3，同时带 toc.ncx 兼容老阅读器）

_EPUB_CSS = """@charset "utf-8";
body { margin: 0 6%; line-height: 1.75; font-size: 1em; }
h1 { font-size: 1.25em; margin: 1.6em 0 1.2em; font-weight: 600; text-align: center; }
h2 { font-size: 1.5em; margin: 3em 0; text-align: center; }
p { margin: 0; text-indent: 2em; }
p.intro { text-indent: 0; margin-bottom: 0.8em; }
"""


def _xhtml(title: str, body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><title>{xml_escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    ).encode("utf-8")


def render_epub(book: ExportBook, book_id: str) -> bytes:
    chapters = book.chapters
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必须是第一个条目且不压缩，否则部分阅读器判为损坏
        zf.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED
        )
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>\n'
            "</container>\n",
        )
        zf.writestr("OEBPS/style.css", _EPUB_CSS)

        if book.intro:
            intro_body = "<h2>作品简介</h2>\n" + "\n".join(
                f'<p class="intro">{xml_escape(line)}</p>'
                for line in book.intro.split("\n")
            )
            zf.writestr("OEBPS/intro.xhtml", _xhtml("作品简介", intro_body))

        for index, chapter in enumerate(chapters, 1):
            body = f"<h1>{xml_escape(chapter.heading)}</h1>\n" + "\n".join(
                f"<p>{xml_escape(text)}</p>" for text in chapter.paragraphs
            )
            zf.writestr(f"OEBPS/chap{index:04d}.xhtml", _xhtml(chapter.heading, body))

        manifest = [
            '<item id="css" href="style.css" media-type="text/css"/>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
            'properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        ]
        spine = []
        if book.intro:
            manifest.append(
                '<item id="intro" href="intro.xhtml" '
                'media-type="application/xhtml+xml"/>'
            )
            spine.append('<itemref idref="intro"/>')
        for index in range(1, len(chapters) + 1):
            manifest.append(
                f'<item id="chap{index}" href="chap{index:04d}.xhtml" '
                'media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="chap{index}"/>')

        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="pub-id">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="pub-id">urn:uuid:{xml_escape(book_id)}</dc:identifier>\n'
            f"    <dc:title>{xml_escape(book.title)}</dc:title>\n"
            "    <dc:language>zh-CN</dc:language>\n"
            '    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>\n'
            "  </metadata>\n"
            "  <manifest>\n    " + "\n    ".join(manifest) + "\n  </manifest>\n"
            '  <spine toc="ncx">\n    ' + "\n    ".join(spine) + "\n  </spine>\n"
            "</package>\n",
        )

        nav_items = []
        if book.intro:
            nav_items.append('<li><a href="intro.xhtml">作品简介</a></li>')
        for index, chapter in enumerate(chapters, 1):
            nav_items.append(
                f'<li><a href="chap{index:04d}.xhtml">'
                f"{xml_escape(chapter.heading)}</a></li>"
            )
        zf.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">\n'
            "<head><title>目录</title></head>\n<body>\n"
            '<nav epub:type="toc" id="toc"><h1>目录</h1>\n<ol>\n'
            + "\n".join(nav_items)
            + "\n</ol></nav>\n</body>\n</html>\n",
        )

        points = []
        order = 1
        if book.intro:
            points.append(
                f'<navPoint id="nav{order}" playOrder="{order}">'
                "<navLabel><text>作品简介</text></navLabel>"
                '<content src="intro.xhtml"/></navPoint>'
            )
            order += 1
        for index, chapter in enumerate(chapters, 1):
            points.append(
                f'<navPoint id="nav{order}" playOrder="{order}">'
                f"<navLabel><text>{xml_escape(chapter.heading)}</text></navLabel>"
                f'<content src="chap{index:04d}.xhtml"/></navPoint>'
            )
            order += 1
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'  <head><meta name="dtb:uid" content="urn:uuid:{xml_escape(book_id)}"/></head>\n'
            f"  <docTitle><text>{xml_escape(book.title)}</text></docTitle>\n"
            "  <navMap>\n    " + "\n    ".join(points) + "\n  </navMap>\n"
            "</ncx>\n",
        )
    return buffer.getvalue()


# ----------------------------------------------------------------------
# DOCX（OOXML，标题用 Heading 1，目录是真实可点击的内部超链接）

_DOCX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:pPr><w:spacing w:after="0" w:line="360" w:lineRule="auto"/>
      <w:ind w:firstLine="480"/></w:pPr>
    <w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="360" w:after="240"/>
      <w:ind w:firstLine="0"/><w:jc w:val="center"/></w:pPr>
    <w:rPr><w:rFonts w:eastAsia="黑体"/><w:b/><w:sz w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TOCEntry">
    <w:name w:val="TOC Entry"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:firstLine="0"/><w:spacing w:after="60"/></w:pPr>
  </w:style>
  <w:style w:type="character" w:styleId="Hyperlink">
    <w:name w:val="Hyperlink"/>
    <w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>
  </w:style>
</w:styles>
"""


def _docx_paragraph(text: str, style: str | None = None) -> str:
    pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return (
        f"<w:p>{pr}<w:r><w:t xml:space=\"preserve\">{xml_escape(text)}</w:t></w:r></w:p>"
    )


def render_docx(book: ExportBook) -> bytes:
    chapters = book.chapters
    body: list[str] = []

    body.append(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{xml_escape(book.title)}</w:t></w:r></w:p>'
    )
    if book.intro:
        for line in book.intro.split("\n"):
            body.append(_docx_paragraph(line))

    # 目录：用书签 + 内部超链接，打开就能点，不需要作者按 F9。
    body.append(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>目录</w:t></w:r></w:p>'
    )
    for index, chapter in enumerate(chapters, 1):
        body.append(
            f'<w:p><w:pPr><w:pStyle w:val="TOCEntry"/></w:pPr>'
            f'<w:hyperlink w:anchor="ch{index}">'
            f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
            f'<w:t xml:space="preserve">{xml_escape(chapter.heading)}</w:t></w:r>'
            f"</w:hyperlink></w:p>"
        )

    for index, chapter in enumerate(chapters, 1):
        body.append(f'<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        body.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f'<w:bookmarkStart w:id="{index}" w:name="ch{index}"/>'
            f'<w:r><w:t xml:space="preserve">{xml_escape(chapter.heading)}</w:t></w:r>'
            f'<w:bookmarkEnd w:id="{index}"/></w:p>'
        )
        for text in chapter.paragraphs:
            body.append(_docx_paragraph(text))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "<w:body>\n" + "\n".join(body) + "\n"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>\n'
        "</w:body>\n</w:document>\n"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="xml" ContentType="application/xml"/>\n'
            '  <Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
            '  <Override PartName="/word/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.styles+xml"/>\n'
            "</Types>\n",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">\n'
            '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\n'
            "</Relationships>\n",
        )
        zf.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">\n'
            '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/styles" Target="styles.xml"/>\n'
            "</Relationships>\n",
        )
        zf.writestr("word/styles.xml", _DOCX_STYLES)
        zf.writestr("word/document.xml", document)
    return buffer.getvalue()
