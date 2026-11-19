"""只读提取 DOCX / XLSX 的文字，不调用 Office，不解压到磁盘。

格式依据 Microsoft Open XML 的 WordprocessingML / SpreadsheetML 文档；
体验借鉴 Qoder 的附件读取，代码自写。公式只读缓存，外部关系不访问。
https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/working-with-formulas
https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/working-with-the-shared-string-table
https://docs.python.org/3/library/zipfile.html
https://docs.python.org/3/library/xml.html
来源：作者旧项目文渊阁 65daff313d126ebefe7ea36a7e2f33d23de5914f。
本次适配：同一字节快照解析、宏仅跳过不执行、向 UI 与模型传递提取范围。
OfficeText.sha256 是原二进制文件哈希，不能登记为纯文本编辑指纹。
提取结果不能用于把 Office 文件当成纯文本回写。
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_XML_ELEMENTS = 250_000
MAX_XML_DEPTH = 128
MAX_TEXT_CHARS = 2_000_000
MAX_CELLS = 100_000
MAX_SHARED_STRINGS = 100_000
MAX_SHEETS = 128

OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".docm", ".xlsm"})
_WORD_NS = {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "http://purl.oclc.org/ooxml/wordprocessingml/main"}
_SHEET_NS = {"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
             "http://purl.oclc.org/ooxml/spreadsheetml/main"}
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = {"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                  "http://purl.oclc.org/ooxml/officeDocument/relationships"}
_CELL_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")


class OfficeError(Exception):
    """可以直接给作者看的读取错误。"""


@dataclass(frozen=True)
class OfficeText:
    extracted_text: str
    kind: str
    sha256: str
    size_bytes: int
    warnings: tuple[str, ...] = ()


class _BoundedTree(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.elements = 0
        self.characters = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_XML_DEPTH or self.elements > MAX_XML_ELEMENTS:
            raise OfficeError("文档内部结构过于复杂，超过读取上限；请拆分后再读。")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)

    def data(self, data):
        self.characters += len(data)
        if self.characters > MAX_MEMBER_BYTES:
            raise OfficeError("文档内部文字超过读取上限；请拆分后再读。")
        return super().data(data)

    def doctype(self, name, pubid, system):
        raise OfficeError("文档含有不支持的 XML 文档类型或实体声明，未读取。")


class _Package:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.members: dict[str, zipfile.ZipInfo] = {}
        self.read_bytes = 0
        self.has_macros = False
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise OfficeError(f"文档内部文件超过 {MAX_MEMBERS:,} 项，请拆分后再读。")
        expanded = 0
        for info in infos:
            name = info.filename
            if (not name or name.startswith("/") or "\\" in name or ":" in name
                    or any(part in {".", ".."} for part in name.split("/"))):
                raise OfficeError("文档内部路径不合法，无法读取。")
            if name in self.members:
                raise OfficeError("文档内部有重复文件，无法可靠读取；请重新另存。")
            if info.flag_bits & 1:
                raise OfficeError("这是加密文档，请先另存为未加密的 .docx 或 .xlsx。")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise OfficeError("文档使用了不支持的压缩格式，请用 Word 或 Excel 重新另存。")
            if info.file_size > MAX_MEMBER_BYTES:
                raise OfficeError(f"文档内部单个文件超过 {MAX_MEMBER_BYTES // (1024 * 1024)} MB，请拆分后再读。")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise OfficeError(f"文档展开后超过 {MAX_EXPANDED_BYTES // (1024 * 1024)} MB，请拆分后再读。")
            lowered = name.lower()
            if lowered.endswith("vbaproject.bin") or "/macrosheets/" in lowered:
                self.has_macros = True
            self.members[name] = info

    def xml(self, name: str) -> ET.Element:
        info = self.members.get(name)
        if info is None or info.is_dir():
            raise OfficeError(f"文档缺少必要内容（{name}），可能已损坏；请重新另存。")
        allowance = min(MAX_MEMBER_BYTES, MAX_EXPANDED_BYTES - self.read_bytes)
        if info.file_size > allowance:
            raise OfficeError("文档展开后的读取总量超过上限，请拆分后再读。")
        with self.archive.open(info, "r") as stream:
            raw = stream.read(allowance + 1)
        if len(raw) > allowance:
            raise OfficeError("文档展开后的读取总量超过上限，请拆分后再读。")
        self.read_bytes += len(raw)
        try:
            return ET.fromstring(raw, parser=ET.XMLParser(target=_BoundedTree()))
        except ET.ParseError:
            raise OfficeError(f"文档内部文字格式损坏（{name}），请重新另存。") from None

    def check_content_types(self) -> None:
        if "[Content_Types].xml" not in self.members:
            return
        root = self.xml("[Content_Types].xml")
        for item in root:
            kind = item.get("ContentType", "").lower()
            if any(marker in kind for marker in ("macroenabled", "vbaproject", "macrosheet")):
                self.has_macros = True


class _Output:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.characters = 0

    def add(self, line: str) -> None:
        self.characters += len(line) + 1
        if self.characters > MAX_TEXT_CHARS:
            raise OfficeError(f"提取文字超过 {MAX_TEXT_CHARS:,} 字，请拆分文档后再读。")
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)


def _namespace(root: ET.Element, local: str, allowed: set[str]) -> str:
    for namespace in allowed:
        if root.tag == f"{{{namespace}}}{local}":
            return "{" + namespace + "}"
    raise OfficeError("文档内容与扩展名不符，或使用了不支持的 Office 格式；请重新另存。")


def _relationship_path(source: str) -> str:
    folder, name = posixpath.split(source)
    return posixpath.join(folder, "_rels", name + ".rels")


def _relationships(package: _Package, source: str) -> dict[str, tuple[str, str, bool]]:
    path = _relationship_path(source) if source else "_rels/.rels"
    if path not in package.members:
        return {}
    root = package.xml(path)
    if root.tag != f"{{{_REL_NS}}}Relationships":
        raise OfficeError("文档的内部关联信息损坏，请重新另存。")
    result = {}
    for item in root.findall(f"{{{_REL_NS}}}Relationship"):
        identifier = item.get("Id", "")
        if not identifier or identifier in result:
            raise OfficeError("文档的内部关联编号缺失或重复，请重新另存。")
        mode = item.get("TargetMode", "Internal")
        if mode not in {"Internal", "External"}:
            raise OfficeError("文档含有不支持的关联方式，请重新另存。")
        result[identifier] = (item.get("Type", ""), item.get("Target", ""), mode == "External")
    return result


def _rel_is(kind: str, local: str) -> bool:
    return any(kind == namespace + "/" + local for namespace in _OFFICE_REL_NS)


def _resolve_part(source: str, relationship: tuple[str, str, bool]) -> str:
    _, target, external = relationship
    if external:
        raise OfficeError("所需正文或工作表指向外部文件，当前不会访问外链；请先在 Office 中另存完整文档。")
    parsed = urlsplit(target)
    decoded = unquote(parsed.path)
    if (not decoded or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or "\\" in decoded or ":" in decoded or "\x00" in decoded):
        raise OfficeError("文档内部关联路径不合法，无法读取。")
    path = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/")
                              else posixpath.join(posixpath.dirname(source), decoded))
    if path in {".", ".."} or path.startswith("../"):
        raise OfficeError("文档内部关联路径越界，无法读取。")
    return path


def _main_part(package: _Package, default: str) -> str:
    roots = [rel for rel in _relationships(package, "").values() if _rel_is(rel[0], "officeDocument")]
    if len(roots) > 1:
        raise OfficeError("文档含有多个正文入口，无法可靠读取；请重新另存。")
    return _resolve_part("", roots[0]) if roots else default


def _paragraph(node: ET.Element, ns: str) -> str:
    parts: list[str] = []

    def visit(item):
        if item.tag in {ns + "del", ns + "moveFrom", ns + "drawing", ns + "pict"}:
            return
        if item.tag == ns + "t":
            parts.append(item.text or "")
        elif item.tag == ns + "tab":
            parts.append("\t")
        elif item.tag in {ns + "br", ns + "cr"}:
            parts.append("\n")
        else:
            for child in item:
                visit(child)

    visit(node)
    return "".join(parts)


def _word_blocks(node: ET.Element, ns: str):
    for child in node:
        if child.tag == ns + "p":
            yield _paragraph(child, ns)
        elif child.tag == ns + "tbl":
            # 一行表格一行文字；单元格内换段用 / 标出，不与下一行混淆。
            for row in child.findall(ns + "tr"):
                cells = [" / ".join(_word_blocks(cell, ns)).replace("\n", " / ")
                         for cell in row.findall(ns + "tc")]
                yield "\t".join(cells)
        elif child.tag not in {ns + "del", ns + "moveFrom", ns + "sectPr"}:
            yield from _word_blocks(child, ns)


def _read_docx(package: _Package) -> tuple[str, tuple[str, ...]]:
    root = package.xml(_main_part(package, "word/document.xml"))
    ns = _namespace(root, "document", _WORD_NS)
    body = root.find(ns + "body")
    if body is None:
        raise OfficeError("Word 文档缺少正文，请重新另存。")
    output = _Output()
    for paragraph in _word_blocks(body, ns):
        output.add(paragraph)
    return output.text(), ("只提取正文与表格文字；图片、页眉页脚、批注和完整排版不包含在内。",)


def _rich_string(node: ET.Element | None, ns: str) -> str:
    if node is None:
        return ""
    # rPh 是拼音注音，不能混入实际单元格值。
    return "".join((child.text or "") if child.tag == ns + "t"
                   else "".join(t.text or "" for t in child.findall(ns + "t"))
                   for child in node if child.tag in {ns + "t", ns + "r"})


def _column_number(label: str) -> int:
    result = 0
    for char in label:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _column_label(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _cell_value(cell: ET.Element, ns: str, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    value_node = cell.find(ns + "v")
    raw = (value_node.text or "") if value_node is not None else ""
    formula = cell.find(ns + "f") is not None
    if formula and not raw:
        return "〔公式无缓存，未计算〕"
    if kind == "s":
        if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) >= len(shared):
            raise OfficeError("Excel 共享文字索引损坏或缺失，请重新另存。")
        value = shared[int(raw)]
    elif kind == "inlineStr":
        value = _rich_string(cell.find(ns + "is"), ns)
    elif kind == "b":
        if raw not in {"0", "1", ""}:
            raise OfficeError("Excel 布尔单元格的值不合法，请重新另存。")
        value = {"0": "FALSE", "1": "TRUE", "": ""}[raw]
    elif kind in {"n", "str", "d", "e"}:
        value = raw
    else:
        raise OfficeError(f"Excel 使用了不支持的单元格类型（{kind[:40]}），请重新另存。")
    return value + (" 〔公式缓存，未重新计算〕" if formula else "")


def _one_line(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace("\t", "\\t")


def _read_xlsx(package: _Package) -> tuple[str, tuple[str, ...]]:
    path = _main_part(package, "xl/workbook.xml")
    root = package.xml(path)
    ns = _namespace(root, "workbook", _SHEET_NS)
    relationships = _relationships(package, path)
    shared: list[str] = []
    shared_parts = [rel for rel in relationships.values() if _rel_is(rel[0], "sharedStrings")]
    if len(shared_parts) > 1:
        raise OfficeError("Excel 含有多个共享文字表，无法可靠读取；请重新另存。")
    if shared_parts:
        string_root = package.xml(_resolve_part(path, shared_parts[0]))
        string_ns = _namespace(string_root, "sst", _SHEET_NS)
        total_chars = 0
        for item in string_root.findall(string_ns + "si"):
            text = _rich_string(item, string_ns)
            total_chars += len(text)
            if len(shared) >= MAX_SHARED_STRINGS or total_chars > MAX_TEXT_CHARS:
                raise OfficeError("Excel 共享文字表超过读取上限，请拆分后再读。")
            shared.append(text)
    sheets = root.find(ns + "sheets")
    if sheets is None:
        raise OfficeError("Excel 缺少工作表清单，请重新另存。")
    sheet_list = sheets.findall(ns + "sheet")
    if len(sheet_list) > MAX_SHEETS:
        raise OfficeError(f"Excel 超过 {MAX_SHEETS} 张工作表，请拆分后再读。")
    output = _Output()
    count = 0
    for sheet in sheet_list:
        relation_id = next((sheet.get("{" + rel_ns + "}id") for rel_ns in _OFFICE_REL_NS
                            if sheet.get("{" + rel_ns + "}id")), "")
        relationship = relationships.get(relation_id)
        if relationship is None:
            raise OfficeError("Excel 的工作表关联缺失，请重新另存。")
        title = sheet.get("name", "未命名")
        state = "（隐藏）" if sheet.get("state") in {"hidden", "veryHidden"} else ""
        output.add(f"【工作表：{_one_line(title)}】{state}")
        if _rel_is(relationship[0], "chartsheet"):
            output.add("〔图表工作表没有可提取的单元格文字〕")
            continue
        if "macrosheet" in relationship[0].lower():
            output.add("〔宏工作表未提取，宏未执行〕")
            continue
        if not _rel_is(relationship[0], "worksheet"):
            raise OfficeError("Excel 包含不支持的工作表类型，请另存为普通 .xlsx。")
        sheet_root = package.xml(_resolve_part(path, relationship))
        sheet_ns = _namespace(sheet_root, "worksheet", _SHEET_NS)
        data = sheet_root.find(sheet_ns + "sheetData")
        previous_row = 0
        if data is None:
            continue
        for row in data.findall(sheet_ns + "row"):
            row_text = row.get("r", str(previous_row + 1))
            if not re.fullmatch(r"[1-9][0-9]{0,6}", row_text):
                raise OfficeError("Excel 行号不合法，请重新另存。")
            row_number = int(row_text)
            if row_number <= previous_row or row_number > 1_048_576:
                raise OfficeError("Excel 行号重复、乱序或越界，请重新另存。")
            previous_row = row_number
            previous_column = 0
            for cell in row.findall(sheet_ns + "c"):
                count += 1
                if count > MAX_CELLS:
                    raise OfficeError(f"Excel 超过 {MAX_CELLS:,} 个单元格，请拆分后再读。")
                reference = cell.get("r", f"{_column_label(previous_column + 1)}{row_number}")
                match = _CELL_REF.fullmatch(reference)
                column = _column_number(match[1]) if match else 0
                if (not match or int(match[2]) != row_number or column <= previous_column
                        or column > 16_384):
                    raise OfficeError("Excel 单元格位置重复、乱序或越界，请重新另存。")
                previous_column = column
                value = _cell_value(cell, sheet_ns, shared)
                if value:
                    output.add(f"{reference}\t{_one_line(value)}")
    return output.text(), ("单元格保留原始值和位置；数字不按显示格式转换为日期。",
                           "公式只显示文件内已有缓存，可能过期；不重新计算，不访问外部链接。",
                           "图片、图表内容、批注和完整排版不包含在内；单元格内换行显示为 \\n。")


def extract_office_bytes(raw: bytes, suffix: str) -> OfficeText:
    """Parse the caller's verified byte snapshot; never reread or modify its path."""
    kind = suffix.lower().lstrip(".")
    if "." + kind not in OFFICE_SUFFIXES:
        raise OfficeError("暂不支持这种 Office 格式，可用相应格式工具转换后读取。")
    try:
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise OfficeError(f"本次 Office 提取超过 {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB 读取预算，可用格式工具分段处理。")
        if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise OfficeError("这是加密文档或旧版 Office 格式，需先解密或转换为 Open XML 格式。")
        with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
            package = _Package(archive)
            package.check_content_types()
            text, warnings = _read_docx(package) if kind in {"docx", "docm"} else _read_xlsx(package)
            if package.has_macros or kind in {"docm", "xlsm"}:
                warnings += ("仅读取文档内容，宏未执行、未提取。",)
    except OfficeError:
        raise
    except (zipfile.BadZipFile, EOFError, zlib.error):
        raise OfficeError("Office 文件不完整或已损坏，请用 Word / Excel 检查后重新读取。") from None
    except (NotImplementedError, RuntimeError):
        raise OfficeError("Office 文件已加密或压缩格式不受支持，可用相应格式工具转换后读取。") from None
    except (OSError, ValueError):
        raise OfficeError("Office 文件内部数据无法读取，请检查文件是否完整。") from None
    return OfficeText(text, kind, hashlib.sha256(raw).hexdigest(), len(raw), warnings)


def read_office_file(path: Path) -> OfficeText:
    """Read a bounded snapshot for paths outside a project."""
    path = Path(path)
    if path.suffix.lower() not in OFFICE_SUFFIXES:
        raise OfficeError("暂不支持这种 Office 格式，可用相应格式工具转换后读取。")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
    except OSError:
        raise OfficeError("无法打开 Office 文件，请确认文件存在、有读取权限且未被占用。") from None
    return extract_office_bytes(raw, path.suffix)


def extraction_receipt(result: OfficeText) -> dict[str, object]:
    return {"format": result.kind, "parser": "openxml-text-v1", "read_only": True,
            "source_sha256": result.sha256, "source_bytes": result.size_bytes,
            "warnings": list(result.warnings)}


def extracted_context_text(content: str, extraction: object) -> str:
    """Keep extraction scope with the text on every model-input path."""
    if not isinstance(extraction, Mapping):
        return content
    scope = "；".join(str(warning) for warning in extraction.get("warnings", []))
    return f"[文档文字提取视图：{extraction.get('format')}；{scope}]\n{content}"
