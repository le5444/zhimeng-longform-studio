"""Office 读取的真实 ZIP / XML 样本；不依赖 Office，不读取作者的文件。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from zhimeng_runtime import office_reader as office

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def word(body, namespace=WORD_NS):
    return f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'


def worksheet(rows, namespace=SHEET_NS):
    return f'<worksheet xmlns="{namespace}"><sheetData>{rows}</sheetData></worksheet>'


def relationships(items):
    return f'<Relationships xmlns="{REL_NS}">{items}</Relationships>'


def relation(identifier, kind, target, extra=""):
    return f'<Relationship Id="{identifier}" Type="{OFFICE_REL_NS}/{kind}" Target="{target}" {extra}/>'


def workbook_parts(rows, shared=None, second=None):
    sheets = '<sheet name="中文表" sheetId="7" r:id="content"/>'
    rels = relation("content", "worksheet", "worksheets/renamed.xml")
    parts = {"xl/worksheets/renamed.xml": worksheet(rows)}
    if second is not None:
        sheets += '<sheet name="隐藏表" sheetId="2" state="hidden" r:id="second"/>'
        rels += relation("second", "worksheet", "/xl/worksheets/sheet2.xml")
        parts["xl/worksheets/sheet2.xml"] = worksheet(second)
    if shared is not None:
        rels += relation("strings", "sharedStrings", "texts/custom.xml")
        parts["xl/texts/custom.xml"] = f'<sst xmlns="{SHEET_NS}">{shared}</sst>'
    parts["xl/workbook.xml"] = (
        f'<workbook xmlns="{SHEET_NS}" xmlns:r="{OFFICE_REL_NS}">'
        f'<sheets>{sheets}</sheets></workbook>'
    )
    parts["xl/_rels/workbook.xml.rels"] = relationships(rels)
    return parts


class OfficeReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, kind, parts):
        path = self.root / ("测试文档." + kind)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in parts.items():
                archive.writestr(name, content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def docx(self, body):
        return self.package("docx", {"word/document.xml": word(body)})

    def test_word_keeps_paragraph_table_order_and_chinese(self):
        path = self.docx(
            '<w:p><w:r><w:t>开头</w:t><w:tab/><w:t>标签</w:t><w:br/><w:t>下一行</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>补充</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>乙</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:sdt><w:sdtContent><w:p><w:r><w:t>末尾</w:t></w:r></w:p></w:sdtContent></w:sdt>'
        )
        before = path.read_bytes()
        result = office.read_office_file(path)
        self.assertEqual(result.extracted_text, "开头\t标签\n下一行\n甲 / 补充\t乙\n末尾")
        self.assertEqual(result.kind, "docx")
        self.assertEqual(result.sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(result.size_bytes, len(before))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_word_uses_visible_field_value_and_skips_deleted_text(self):
        path = self.docx(
            '<w:p><w:del><w:r><w:delText>已删</w:delText></w:r></w:del>'
            '<w:r><w:instrText>HYPERLINK external</w:instrText><w:t>已显示</w:t></w:r>'
            '<w:drawing><w:t>图片</w:t></w:drawing></w:p>'
        )
        self.assertEqual(office.read_office_file(path).extracted_text, "已显示")

    def test_word_main_part_resolves_package_relationship(self):
        path = self.package("docx", {
            "_rels/.rels": relationships(relation("main", "officeDocument", "custom/main.xml")),
            "custom/main.xml": word('<w:p><w:r><w:t>另一个位置</w:t></w:r></w:p>'),
        })
        self.assertEqual(office.read_office_file(path).extracted_text, "另一个位置")

    def test_strict_word_namespace_and_uppercase_suffix(self):
        path = self.package("DOCX", {"word/document.xml": word(
            '<w:p><w:r><w:t>严格格式</w:t></w:r></w:p>',
            "http://purl.oclc.org/ooxml/wordprocessingml/main")})
        self.assertEqual(office.read_office_file(path).extracted_text, "严格格式")

    def test_xlsx_uses_relationship_order_shared_inline_and_sparse_positions(self):
        path = self.package("xlsx", workbook_parts(
            '<row r="2"><c r="C2" t="s"><v>0</v></c>'
            '<c r="F2" t="inlineStr"><is><t>内联</t></is></c>'
            '<c r="J2" t="b"><v>1</v></c></row>',
            shared='<si><r><t>中文</t></r><r><t>富文本</t></r><rPh sb="0" eb="1"><t>zhong</t></rPh></si>',
            second='<row r="7"><c r="B7"><v>42</v></c></row>',
        ))
        result = office.read_office_file(path)
        self.assertEqual(result.extracted_text,
                         "【工作表：中文表】\nC2\t中文富文本\nF2\t内联\nJ2\tTRUE\n【工作表：隐藏表】（隐藏）\nB7\t42")
        self.assertEqual(result.kind, "xlsx")

    def test_xlsx_formula_cache_missing_cache_and_dates_stay_raw(self):
        path = self.package("xlsx", workbook_parts(
            '<row r="1"><c r="A1"><f>SUM(1,2)</f><v>3</v></c>'
            '<c r="B1"><f>UNKNOWN_FUNCTION()</f></c>'
            '<c r="C1" s="14"><v>45292</v></c>'
            '<c r="D1" t="d"><v>2024-01-01T00:00:00</v></c>'
            '<c r="E1" t="str"><f>"中文"</f><v>中文</v></c>'
            '<c r="F1" t="e"><v>#DIV/0!</v></c></row>'
        ))
        result = office.read_office_file(path)
        self.assertIn("A1\t3 〔公式缓存，未重新计算〕", result.extracted_text)
        self.assertIn("B1\t〔公式无缓存，未计算〕", result.extracted_text)
        self.assertIn("C1\t45292", result.extracted_text)
        self.assertIn("D1\t2024-01-01T00:00:00", result.extracted_text)
        self.assertIn("E1\t中文 〔公式缓存，未重新计算〕", result.extracted_text)
        self.assertIn("F1\t#DIV/0!", result.extracted_text)
        self.assertIn("可能过期", " ".join(result.warnings))

    def test_empty_sheet_and_optional_cell_references(self):
        path = self.package("xlsx", workbook_parts(
            '<row><c t="inlineStr"><is><t>一\n二</t></is></c><c><v>2</v></c></row>', second=""))
        result = office.read_office_file(path)
        self.assertIn("A1\t一\\n二\nB1\t2", result.extracted_text)
        self.assertTrue(result.extracted_text.endswith("【工作表：隐藏表】（隐藏）"))

    def test_external_links_not_accessed_or_needed_for_visible_text(self):
        parts = workbook_parts('<row><c><v>8</v></c></row>')
        parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
            "</Relationships>", relation("external", "externalLink", "https://example.invalid/data", 'TargetMode="External"')
            + "</Relationships>")
        path = self.package("xlsx", parts)
        with patch("urllib.request.urlopen", side_effect=AssertionError("不应联网")), \
                patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("不应解压")), \
                patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("不应解压")):
            self.assertIn("A1\t8", office.read_office_file(path).extracted_text)

    def test_required_external_sheet_is_refused(self):
        parts = workbook_parts("")
        parts["xl/_rels/workbook.xml.rels"] = relationships(relation(
            "content", "worksheet", "https://example.invalid/sheet.xml", 'TargetMode="External"'))
        with self.assertRaisesRegex(office.OfficeError, "外部"):
            office.read_office_file(self.package("xlsx", parts))

    def test_internal_relationship_cannot_escape_package(self):
        parts = workbook_parts("")
        parts["xl/_rels/workbook.xml.rels"] = relationships(relation("content", "worksheet", "../../outside.xml"))
        with self.assertRaisesRegex(office.OfficeError, "越界"):
            office.read_office_file(self.package("xlsx", parts))

    def test_internal_parent_relative_relationship_is_supported(self):
        parts = workbook_parts('<row><c><v>9</v></c></row>')
        parts["xl/_rels/workbook.xml.rels"] = relationships(relation("content", "worksheet", "../data/main.xml"))
        parts["data/main.xml"] = parts.pop("xl/worksheets/renamed.xml")
        self.assertIn("A1\t9", office.read_office_file(self.package("xlsx", parts)).extracted_text)

    def test_macros_are_ignored_without_rejecting_document_text(self):
        for extension in ('docx', 'docm'):
            path = self.package(extension, {'word/document.xml': word('<w:p><w:r><w:t>可读正文</w:t></w:r></w:p>'),
                'word/vbaProject.bin': b'not executable by reader'})
            result = office.read_office_file(path)
            self.assertEqual(result.extracted_text, '可读正文')
            self.assertIn('宏未执行', ' '.join(result.warnings))

    def test_old_format_encrypted_container_and_missing_file_have_clear_errors(self):
        with self.assertRaisesRegex(office.OfficeError, "暂不支持"):
            office.read_office_file(self.root / "old.doc")
        with self.assertRaisesRegex(office.OfficeError, "无法打开"):
            office.read_office_file(self.root / "missing.docx")
        path = self.root / "encrypted.docx"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1example")
        with self.assertRaisesRegex(office.OfficeError, "加密"):
            office.read_office_file(path)

    def test_bad_zip_missing_parts_and_malformed_xml(self):
        path = self.root / "bad.docx"
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(office.OfficeError, "损坏"):
            office.read_office_file(path)
        with self.assertRaisesRegex(office.OfficeError, "缺少"):
            office.read_office_file(self.package("docx", {}))
        with self.assertRaisesRegex(office.OfficeError, "格式损坏"):
            office.read_office_file(self.package("docx", {"word/document.xml": "<broken"}))

    def test_duplicate_parts_are_refused(self):
        path = self.docx("")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("word/document.xml", word(""))
        with self.assertRaisesRegex(office.OfficeError, "重复"):
            office.read_office_file(path)

    def test_document_type_declarations_are_refused_in_utf8_and_utf16(self):
        source = '<?xml version="1.0" encoding="ENCODING"?><!DOCTYPE document>' + word("")
        for encoding in ("utf-8", "utf-16"):
            path = self.package("docx", {"word/document.xml": source.replace("ENCODING", encoding).encode(encoding)})
            with self.subTest(encoding=encoding), self.assertRaisesRegex(office.OfficeError, "实体声明"):
                office.read_office_file(path)

    def test_encrypted_zip_member_flag_and_nonstandard_compression_are_refused(self):
        path = self.docx("")
        original = zipfile.ZipFile.infolist

        def encrypted_infos(archive):
            infos = original(archive)
            infos[0].flag_bits |= 1
            return infos

        with patch.object(zipfile.ZipFile, "infolist", encrypted_infos), \
                self.assertRaisesRegex(office.OfficeError, "加密"):
            office.read_office_file(path)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
            archive.writestr("word/document.xml", word(""))
        with self.assertRaisesRegex(office.OfficeError, "压缩格式"):
            office.read_office_file(path)

    def test_strict_xlsx_namespaces_and_chart_sheet(self):
        parts = workbook_parts('<row><c><v>5</v></c></row>')
        parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
            "</sheets>", '<sheet name="图表页" sheetId="8" r:id="chart"/></sheets>')
        parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
            "</Relationships>", relation("chart", "chartsheet", "chartsheets/sheet1.xml") + "</Relationships>")
        parts = {name: value.replace(SHEET_NS, "http://purl.oclc.org/ooxml/spreadsheetml/main").replace(
            OFFICE_REL_NS, "http://purl.oclc.org/ooxml/officeDocument/relationships") for name, value in parts.items()}
        result = office.read_office_file(self.package("xlsx", parts))
        self.assertIn("A1\t5", result.extracted_text)
        self.assertIn("【工作表：图表页】\n〔图表工作表没有可提取的单元格文字〕", result.extracted_text)

    def test_archive_member_expanded_text_and_structure_budgets(self):
        path = self.docx('<w:p><w:r><w:t>测试文字</w:t></w:r></w:p>')
        for limit, value, message in (
            ("MAX_ARCHIVE_BYTES", 10, "读取预算"),
            ("MAX_MEMBER_BYTES", 10, "单个文件"),
            ("MAX_EXPANDED_BYTES", 10, "展开后"),
            ("MAX_MEMBERS", 0, "内部文件"),
            ("MAX_TEXT_CHARS", 2, "提取文字"),
            ("MAX_XML_ELEMENTS", 2, "过于复杂"),
            ("MAX_XML_DEPTH", 2, "过于复杂"),
        ):
            with self.subTest(limit=limit), patch.object(office, limit, value), \
                    self.assertRaisesRegex(office.OfficeError, message):
                office.read_office_file(path)

    def test_cell_and_shared_string_budgets(self):
        path = self.package("xlsx", workbook_parts('<row><c><v>1</v></c><c><v>2</v></c></row>'))
        with patch.object(office, "MAX_CELLS", 1), self.assertRaisesRegex(office.OfficeError, "单元格"):
            office.read_office_file(path)
        path = self.package("xlsx", workbook_parts("", shared="<si><t>文字</t></si>"))
        with patch.object(office, "MAX_SHARED_STRINGS", 0), self.assertRaisesRegex(office.OfficeError, "共享文字表"):
            office.read_office_file(path)
        with patch.object(office, "MAX_SHEETS", 0), self.assertRaisesRegex(office.OfficeError, "工作表"):
            office.read_office_file(path)

    def test_malformed_cells_and_missing_shared_strings_never_fabricate_values(self):
        for rows, expected in (
            ('<row r="1"><c r="A1" t="s"><v>0</v></c></row>', "共享文字索引"),
            ('<row r="1"><c r="A2"><v>1</v></c></row>', "单元格位置"),
            ('<row r="1"><c r="A1"/><c r="A1"/></row>', "单元格位置"),
            ('<row r="1048577"><c/></row>', "行号"),
            ('<row r="1"><c r="XFE1"/></row>', "单元格位置"),
        ):
            with self.subTest(rows=rows), self.assertRaisesRegex(office.OfficeError, expected):
                office.read_office_file(self.package("xlsx", workbook_parts(rows)))

    def test_shared_string_reuse_is_bounded_by_expanded_output(self):
        path = self.package("xlsx", workbook_parts(
            '<row><c t="s"><v>0</v></c><c t="s"><v>0</v></c><c t="s"><v>0</v></c></row>',
            shared="<si><t>" + "字" * 80 + "</t></si>"))
        with patch.object(office, "MAX_TEXT_CHARS", 180), self.assertRaisesRegex(office.OfficeError, "提取文字"):
            office.read_office_file(path)

    def test_sha_tracks_raw_package_not_just_extracted_text(self):
        path = self.docx('<w:p><w:r><w:t>同样文字</w:t></w:r></w:p>')
        first = office.read_office_file(path)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("docProps/example.xml", "<changed/>")
        second = office.read_office_file(path)
        self.assertEqual(first.extracted_text, second.extracted_text)
        self.assertNotEqual(first.sha256, second.sha256)


if __name__ == "__main__":
    unittest.main()
