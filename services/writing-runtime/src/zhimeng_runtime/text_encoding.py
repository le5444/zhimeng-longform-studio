from __future__ import annotations

import codecs


def decode_project_text(data: bytes, *, final: bool = True) -> tuple[str, str, bytes]:
    """Identify text by its bytes and preserve the original Unicode byte order.

    A BOM selects UTF-8/16/32. Files without one use strict UTF-8, so arbitrary
    binary bytes are never silently replaced with text. The returned encoding
    and BOM can also encode edits without changing an existing file's format.
    For a deliberately bounded prefix, final=False leaves an incomplete final
    codepoint out without replacing it or accepting malformed interior bytes.
    """
    encoding, bom = "utf-8", b""
    for marker, codec in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ):
        if data.startswith(marker):
            encoding, bom = codec, marker
            break
    content = codecs.getincrementaldecoder(encoding)().decode(data[len(bom):], final=final)
    if any(ord(char) < 32 and char not in "\t\r\n\f\b\x1b" for char in content):
        raise ValueError("file contains binary data and cannot be opened as text")
    return content, encoding, bom
