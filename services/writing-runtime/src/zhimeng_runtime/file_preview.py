"""Verified, bounded text previews shared by change history and recovery."""
import hashlib
from pathlib import Path

from zhimeng_runtime.text_encoding import decode_project_text


def verified_text(path: Path, digest: str | None) -> str:
    if path.stat().st_size > 512_000:
        raise ValueError("文件较大，右栏仅显示操作记录。")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("文件内容已变化，无法核验这次修改的完整前后对照。")
    try:
        return decode_project_text(data)[0]
    except ValueError as exc:
        raise ValueError("这是非文本文件或无法识别的文本编码，右栏仅显示操作记录。") from exc
