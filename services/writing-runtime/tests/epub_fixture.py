from __future__ import annotations

import struct
import zipfile
from pathlib import Path


DEFAULT_CHAPTERS = (
    "第一章 起点\n夜雨压住城墙，守门人必须在钟响前作出选择。",
    "第二章 代价\n选择保住了眼前的人，却留下了一笔必须偿还的债。",
)


def write_synthetic_epub(
    path: Path,
    *,
    chapters: tuple[str, ...] = DEFAULT_CHAPTERS,
    include_mimetype: bool = True,
    include_container: bool = True,
    include_opf: bool = True,
    include_spine: bool = True,
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    chapter_items = "\n".join(
        f'<item id="chapter-{index}" href="text/chapter-{index}.xhtml" media-type="application/xhtml+xml"/>'
        for index in range(1, len(chapters) + 1)
    )
    spine_items = "\n".join(
        f'<itemref idref="chapter-{index}"/>'
        for index in range(1, len(chapters) + 1)
    )
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic runtime source</dc:title>
    <dc:creator>fixture</dc:creator>
  </metadata>
  <manifest>{chapter_items}</manifest>
  <spine>{spine_items if include_spine else ''}</spine>
</package>
"""
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    with zipfile.ZipFile(path, "w") as archive:
        if include_mimetype:
            archive.writestr(
                "mimetype",
                b"application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
        if include_container:
            archive.writestr(
                "META-INF/container.xml",
                container.encode("utf-8"),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        if include_opf:
            archive.writestr(
                "OEBPS/content.opf",
                opf.encode("utf-8"),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        for index, chapter in enumerate(chapters, start=1):
            document = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"<h1>{chapter.splitlines()[0]}</h1>"
                f"<p>{' '.join(chapter.splitlines()[1:])}</p>"
                "</body></html>"
            )
            archive.writestr(
                f"OEBPS/text/chapter-{index}.xhtml",
                document.encode("utf-8"),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        for name, data in extra_entries:
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


def mark_zip_entries_encrypted(path: Path) -> None:
    payload = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while True:
            cursor = payload.find(signature, cursor)
            if cursor < 0:
                break
            offset = cursor + flag_offset
            flags = struct.unpack_from("<H", payload, offset)[0]
            struct.pack_into("<H", payload, offset, flags | 0x1)
            cursor += len(signature)
    path.write_bytes(payload)


__all__ = ["DEFAULT_CHAPTERS", "mark_zip_entries_encrypted", "write_synthetic_epub"]
