#!/usr/bin/env python3
"""Validate an EPUB archive and extract readable UTF-8 text using stdlib only."""

from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import sys
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


MAX_ARCHIVE_ENTRIES = 10_000
MAX_SPINE_ITEMS = 5_000
MAX_SINGLE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
MAX_XML_DOCUMENT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class EpubLayout:
    opf_path: str
    opf: ET.Element
    spine: tuple[str, ...]


class TextHTMLParser(HTMLParser):
    block_tags = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = html.unescape(data)
        if text.strip():
            self.parts.append(text)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        raw = re.sub(r"[ \t\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_text(root: ET.Element, wanted: str) -> str:
    for node in root.iter():
        if local_name(node.tag).lower() == wanted.lower() and node.text:
            return node.text.strip()
    return ""


def text_quality(body: str) -> dict[str, object]:
    sample = body[:20_000]
    if not sample:
        return {
            "status": "empty",
            "mojibake_ratio": 1.0,
            "replacement_char_count": 0,
            "cjk_ratio": 0.0,
            "warning": "no extracted text body",
        }

    replacement_count = sample.count("\ufffd")
    marker_count = sum(
        sample.count(marker)
        for marker in ("����", "���", "锟斤拷", "Ã", "Â", "â€", "ï¿½")
    )
    cjk_count = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    ratio = min(1.0, (replacement_count + marker_count) / max(1, len(sample)))
    cjk_ratio = cjk_count / max(1, len(sample))
    status = "ok"
    warning = ""
    if replacement_count > 20 or marker_count > 20 or ratio > 0.02:
        status = "mojibake_suspected"
        warning = "extracted EPUB body appears mojibaked"
    return {
        "status": status,
        "mojibake_ratio": round(ratio, 4),
        "replacement_char_count": replacement_count,
        "cjk_ratio": round(cjk_ratio, 4),
        "warning": warning,
    }


def _safe_archive_name(raw_name: str) -> str:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        raise RuntimeError("EPUB contains an unsafe ZIP path")
    if raw_name.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", raw_name):
        raise RuntimeError("EPUB contains an absolute ZIP path")
    path = PurePosixPath(raw_name.rstrip("/"))
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError("EPUB contains a traversal ZIP path")
    normalized = path.as_posix()
    if normalized != raw_name.rstrip("/"):
        raise RuntimeError("EPUB contains a non-canonical ZIP path")
    return normalized


def _resolve_archive_reference(base: str, raw_href: str) -> str:
    parsed = urlsplit(raw_href)
    if parsed.scheme or parsed.netloc or not parsed.path:
        raise RuntimeError("EPUB spine contains a non-local resource")
    decoded = unquote(parsed.path)
    if "\\" in decoded or decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        raise RuntimeError("EPUB spine contains an unsafe resource path")
    joined = posixpath.normpath(posixpath.join(base, decoded))
    return _safe_archive_name(joined)


def _validate_archive_entries(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if not infos:
        raise RuntimeError("EPUB ZIP archive is empty")
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise RuntimeError("EPUB exceeds the ZIP entry-count limit")

    entries: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed = 0
    for info in infos:
        name = _safe_archive_name(info.filename)
        if name in entries:
            raise RuntimeError("EPUB contains duplicate ZIP paths")
        entries[name] = info
        if info.flag_bits & 0x1:
            raise RuntimeError("EPUB contains an encrypted ZIP entry")
        if info.file_size > MAX_SINGLE_ENTRY_BYTES:
            raise RuntimeError("EPUB exceeds the single-entry size limit")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError("EPUB exceeds the total uncompressed-size limit")
        if info.file_size:
            if info.compress_size <= 0:
                raise RuntimeError("EPUB contains an invalid compression ratio")
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise RuntimeError("EPUB exceeds the compression-ratio limit")
    return entries


def _read_entry(
    zf: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
    name: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    normalized = _safe_archive_name(name)
    info = entries.get(normalized)
    if info is None or info.is_dir():
        raise RuntimeError(f"EPUB is missing required resource: {normalized}")
    if max_bytes is not None and info.file_size > max_bytes:
        raise RuntimeError(f"EPUB resource exceeds its parsing limit: {normalized}")
    data = zf.read(info)
    if len(data) != info.file_size:
        raise RuntimeError(f"EPUB resource size changed while reading: {normalized}")
    return data


def _parse_xml(data: bytes, label: str) -> ET.Element:
    upper = data[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise RuntimeError(f"EPUB {label} contains a forbidden XML declaration")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"EPUB {label} is not valid XML") from exc


def _rootfile_path(
    zf: zipfile.ZipFile,
    entries: dict[str, zipfile.ZipInfo],
) -> str:
    container_data = _read_entry(
        zf,
        entries,
        "META-INF/container.xml",
        max_bytes=MAX_XML_DOCUMENT_BYTES,
    )
    container = _parse_xml(container_data, "container")
    for node in container.iter():
        if local_name(node.tag) == "rootfile":
            full_path = node.attrib.get("full-path")
            if full_path:
                return _safe_archive_name(unquote(full_path))
    raise RuntimeError("EPUB container has no rootfile full-path")


def _spine_items(
    opf: ET.Element,
    opf_path: str,
    entries: dict[str, zipfile.ZipInfo],
) -> tuple[str, ...]:
    manifest: dict[str, tuple[str, str]] = {}
    base = posixpath.dirname(opf_path)
    for node in opf.iter():
        if local_name(node.tag) != "item":
            continue
        item_id = node.attrib.get("id")
        href = node.attrib.get("href")
        if not item_id or not href:
            continue
        if item_id in manifest:
            raise RuntimeError("EPUB manifest contains duplicate item IDs")
        manifest[item_id] = (
            _resolve_archive_reference(base, href),
            node.attrib.get("media-type", ""),
        )

    idrefs = [
        node.attrib.get("idref")
        for node in opf.iter()
        if local_name(node.tag) == "itemref"
    ]
    if not idrefs:
        raise RuntimeError("EPUB OPF has no spine items")
    if len(idrefs) > MAX_SPINE_ITEMS:
        raise RuntimeError("EPUB exceeds the spine-item limit")

    spine: list[str] = []
    for idref in idrefs:
        if not idref or idref not in manifest:
            raise RuntimeError("EPUB spine references a missing manifest item")
        resource, media_type = manifest[idref]
        if resource not in entries:
            raise RuntimeError("EPUB spine references a missing ZIP resource")
        if "html" not in media_type and not resource.lower().endswith(
            (".xhtml", ".html", ".htm")
        ):
            raise RuntimeError("EPUB spine references a non-HTML resource")
        spine.append(resource)
    return tuple(spine)


def validate_epub(zf: zipfile.ZipFile) -> EpubLayout:
    entries = _validate_archive_entries(zf)
    if "mimetype" not in entries:
        raise RuntimeError("EPUB is missing the mimetype entry")
    first = zf.infolist()[0]
    if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
        raise RuntimeError("EPUB mimetype entry must be first and uncompressed")
    mimetype = _read_entry(zf, entries, "mimetype", max_bytes=256)
    if mimetype != b"application/epub+zip":
        raise RuntimeError("EPUB mimetype entry has invalid content")
    if "META-INF/container.xml" not in entries:
        raise RuntimeError("EPUB is missing META-INF/container.xml")

    opf_path = _rootfile_path(zf, entries)
    opf_data = _read_entry(
        zf,
        entries,
        opf_path,
        max_bytes=MAX_XML_DOCUMENT_BYTES,
    )
    opf = _parse_xml(opf_data, "OPF package")
    spine = _spine_items(opf, opf_path, entries)
    return EpubLayout(opf_path=opf_path, opf=opf, spine=spine)


def _decode_document(data: bytes, name: str) -> str:
    encodings = ["utf-8-sig"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    declaration = re.search(
        br"(?:encoding|charset)\s*=\s*['\"]?([A-Za-z0-9._-]+)",
        data[:2048],
        flags=re.IGNORECASE,
    )
    if declaration:
        declared = declaration.group(1).decode("ascii", errors="ignore").lower()
        if declared in {"utf-8", "utf8", "utf-16", "utf-16le", "utf-16be"}:
            encodings.insert(0, declared)
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"EPUB spine resource is not valid Unicode text: {name}")


def extract(epub_path: Path) -> tuple[str, dict[str, object]]:
    try:
        with zipfile.ZipFile(epub_path) as zf:
            layout = validate_epub(zf)
            title = first_text(layout.opf, "title") or epub_path.stem
            creator = first_text(layout.opf, "creator")
            entries = { _safe_archive_name(info.filename): info for info in zf.infolist() }

            sections: list[str] = []
            for index, item in enumerate(layout.spine, start=1):
                data = _read_entry(zf, entries, item)
                parser = TextHTMLParser()
                parser.feed(_decode_document(data, item))
                parser.close()
                text = parser.text()
                if text:
                    sections.append(
                        f"\n\n## EPUB_SPINE_{index:04d} {item}\n\n{text}"
                    )
    except zipfile.BadZipFile as exc:
        raise RuntimeError("EPUB is not a valid ZIP archive") from exc

    body = "".join(sections).strip()
    if not body:
        raise RuntimeError("EPUB extraction produced no readable text")
    quality = text_quality(body)
    if quality["status"] == "mojibake_suspected":
        raise RuntimeError("EPUB extracted text is mojibake_suspected")

    chapter_markers = re.findall(
        r"(?m)^(第[一二三四五六七八九十百千万零〇\d]+[章回节][^\n]{0,60})",
        body,
    )
    metadata: dict[str, object] = {
        "title": title,
        "author": creator,
        "source_format": "epub",
        "spine_item_count": len(layout.spine),
        "chapter_marker_count": len(chapter_markers),
        "chapter_markers_sample": chapter_markers[:80],
        "char_count": len(body),
        "text_quality": quality,
    }

    header = [
        f"# {title}",
        "",
        f"- author: {creator}" if creator else "- author: unknown",
        "- source_epub: managed local snapshot",
        f"- spine_items: {len(layout.spine)}",
        f"- chapter_markers: {len(chapter_markers)}",
        "",
        "Note: local analysis source only. Do not quote or store long protected prose in distillation cards.",
        "",
        "---",
        "",
    ]
    return "\n".join(header) + body + "\n", metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epub", required=True)
    parser.add_argument("--out")
    parser.add_argument("--meta")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    if not args.validate_only and (not args.out or not args.meta):
        parser.error("--out and --meta are required unless --validate-only is used")

    epub_path = Path(args.epub)
    try:
        text, metadata = extract(epub_path)
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "status": "validated",
                        "spine_item_count": metadata["spine_item_count"],
                        "chapter_marker_count": metadata["chapter_marker_count"],
                        "char_count": metadata["char_count"],
                        "text_quality": metadata["text_quality"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        out_path = Path(args.out)
        meta_path = Path(args.meta)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "extracted",
                    "text_path": str(out_path),
                    "metadata_path": str(meta_path),
                    "spine_item_count": metadata["spine_item_count"],
                    "chapter_marker_count": metadata["chapter_marker_count"],
                    "char_count": metadata["char_count"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(
            json.dumps(
                {"status": "rejected", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
