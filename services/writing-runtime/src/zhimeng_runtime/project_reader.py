from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Callable
from typing import Final, Iterable, Mapping

from zhimeng_runtime.project_truth import is_non_truth_project_path
from zhimeng_runtime.office_reader import OFFICE_SUFFIXES, MAX_ARCHIVE_BYTES, OfficeError, extract_office_bytes, extraction_receipt
from zhimeng_runtime.text_encoding import decode_project_text


READER_SCHEMA: Final = "zhimeng.project-inspection.v1"
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_SCAN_FILES: Final = _env_int("ZHIMENG_READER_MAX_SCAN_FILES", 20_000)
MAX_SCAN_BYTES: Final = _env_int("ZHIMENG_READER_MAX_SCAN_BYTES", 512 * 1024 * 1024)
MAX_SCAN_DIRECTORIES: Final = 5_000
MAX_SCAN_DEPTH: Final = 32
MAX_TEXT_FILE_BYTES: Final = _env_int("ZHIMENG_READER_MAX_TEXT_FILE_BYTES", 8 * 1024 * 1024)
REPARSE_POINT_ATTRIBUTE: Final = 0x400
HASH_CHUNK_BYTES: Final = 1024 * 1024
# 文件状态（大小、修改时间、文件身份、硬链接数）没变就沿用上次算的哈希，和 git 一样。
# 修改时间离算哈希那一刻不到这么久的不算数：Windows 修改时间约 16 毫秒一跳、FAT 两秒一跳，
# 同一跳里写两次，状态一样内容不同；过了这个窗口再改，修改时间必然不同。
RACY_WINDOW_NS: Final = 3_000_000_000
MAX_REMEMBERED_PATHS: Final = 200_000

SECTION_LABELS: Final = {
    1: "项目定位",
    2: "世界观设定",
    3: "人物设定",
    4: "剧情大纲",
    5: "正文内容",
    6: "写作规范",
    7: "运营数据",
    8: "素材库",
    9: "完本复盘",
}
SECTION_PATTERN: Final = re.compile(r"^0([1-9])(?:[_\-\s]|$)")
CHAPTER_PATTERNS: Final = (
    re.compile(r"(?i)(?<![A-Za-z0-9])CH[\s_-]*0*(\d{1,6})(?!\d)"),
    re.compile(r"(?i)(?<![A-Za-z0-9])chapter[\s_-]*0*(\d{1,6})(?!\d)"),
    re.compile(r"第\s*0*(\d{1,6})\s*章"),
    re.compile(r"第\s*([零〇一二三四五六七八九十百千两]{1,12})\s*章"),
)
CHAPTER_PROSE_STEM_PATTERNS: Final = (
    re.compile(r"(?i)^(?:CH|chapter)[\s_-]*0*\d{1,6}(?:[\s_-]+.+)?$"),
    re.compile(r"^第\s*0*\d{1,6}\s*章(?:[\s_-]+.+)?$"),
    re.compile(
        r"^第\s*[零〇一二三四五六七八九十百千两]{1,12}\s*章(?:[\s_-]+.+)?$"
    ),
    # 作者真实项目里最常见的「001_章节名.md」写法。
    re.compile(r"^0*\d{1,6}[\s_.-]+(?!\d)\S.*$"),
)
# 只允许作用在文件名上的章号模式。故意不喂给父目录，
# 否则「001-050章」这种范围目录会把里面的 README 误判成第 1 章。
# 分隔符后用 (?!\d) 排除「001-050」这类区间写法。
CHAPTER_STEM_NUMBER_PATTERN: Final = re.compile(r"^0*(\d{1,6})[\s_.-]+(?!\d)\S.*$")
VOLUME_PATTERNS: Final = (
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:VOL|volume)[\s_-]*0*(\d{1,5})(?!\d)"),
    re.compile(r"第\s*0*(\d{1,5})\s*卷"),
    re.compile(r"卷[\s_-]*0*(\d{1,5})(?!\d)"),
    re.compile(r"第\s*([零〇一二三四五六七八九十百千两]{1,12})\s*卷"),
    re.compile(r"卷\s*([零〇一二三四五六七八九十百千两]{1,12})"),
)

TEXT_SUFFIXES: Final = frozenset({".md", ".markdown", ".txt", ".rst"})
STRUCTURED_SUFFIXES: Final = frozenset(
    {".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".csv"}
)
EXCLUDED_DIRECTORIES: Final = frozenset(
    {
        ".git",
        ".idea",
        ".vscode",
        "__pycache__",
        "node_modules",
        "archive",
        "archives",
        "backup",
        "backups",
        "temp",
        "tmp",
        "备份",
        "归档",
        "隔离区",
    }
)
# 打开任意文件夹当项目以后（纠偏 P2，专题《项目即文件夹》第 3 节第 8 条）：点号开头的文件夹（.venv、.next、.cache、
# 织梦自己的 .runtime 这类）和常见的生成 / 依赖文件夹不进项目快照。只是不列、不算指纹；要看里面的东西照样能用
# 电脑上的读文件工具按路径读。
GENERATED_DIRECTORIES: Final = frozenset(
    {"venv", "dist", "build", "target", "coverage", "site-packages", "bower_components"}
)


def skip_scan_directory(name: str) -> bool:
    """项目快照里不进去的文件夹：老的排除名单 + 点号开头的 + 常见生成 / 依赖文件夹。"""
    folded = name.casefold()
    return folded in EXCLUDED_DIRECTORIES or folded in GENERATED_DIRECTORIES or name.startswith(".")


CONTROL_MARKERS: Final = ("控制卡", "control_card", "control-card", "chapter_brief")
HANDOFF_MARKERS: Final = ("handoff", "交接", "operation_package", "operation-package", "工作包")
CONTEXT_MARKERS: Final = (
    "context",
    "上下文",
    "call_list",
    "route_health",
    "readiness",
    "prewrite",
    "scaffold",
)
STATUS_MARKERS: Final = (
    "status",
    "状态",
    "gate",
    "门禁",
    "health",
    "健康",
    "写回",
    "同步",
)
REVIEW_MARKERS: Final = (
    "review",
    "审稿",
    "校验",
    "audit",
    "writeback",
    "回填",
    # These reports sit beside real prose in some of the author's projects.
    "report",
    "ai_smell",
    "cleanliness",
)
NON_PROSE_MARKERS: Final = (
    "chapter-brief",
    "chapter_brief",
    "brief",
    "章纲",
    "细纲",
    "续写卡",
    "控制卡",
    "记忆保险丝",
    "写作指令",
    "任务卡",
    "检查清单",
    "checklist",
    "outline",
    "plan",
    "prompt",
    "summary",
    "摘要",
    "metadata",
    "元数据",
)
PROSE_MARKERS: Final = (
    "正文",
    "初稿",
    "草稿",
    "定稿",
    "完稿",
    "发布稿",
    "manuscript",
    "prose",
    "draft",
    "final",
)
PROJECT_FILE_KINDS: Final = frozenset(
    {
        "prose",
        "document",
        "control",
        "handoff",
        "context",
        "review",
        "status",
        "structured_state",
        "asset",
    }
)
PUBLIC_TEXT_FILE_KINDS: Final = frozenset({"prose", "document", "control", "review"})
WRITING_CONTEXT_FILE_KINDS: Final = frozenset(
    {
        "prose",
        "document",
        "control",
        "handoff",
        "review",
        "context",
        "status",
        "structured_state",
        "asset",
    }
)


class ProjectReaderError(RuntimeError):
    """Base class for bounded, read-only project inspection failures."""


class UntrustedProjectPathError(ProjectReaderError):
    """The requested path is not a real directory under an explicit trusted root."""


class InvalidNovelProjectError(ProjectReaderError):
    """The requested directory does not contain a recognizable 01-09 project."""


class ProjectScanLimitError(ProjectReaderError):
    """The explicit project exceeds the bounded reader budget."""


class ProjectChangedDuringReadError(ProjectReaderError):
    """A file changed while its snapshot hash was being calculated."""


class ProjectFileNotFoundError(ProjectReaderError):
    """A stable file ID is absent from the current project snapshot."""


class UnreadableProjectFileError(ProjectReaderError):
    """A project file is not an allowed UTF-8 text document."""


@dataclass(frozen=True, slots=True)
class _RevisionState:
    fingerprint: str
    revision: int


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    path: Path
    relative_path: str
    size_bytes: int
    sha256: str
    identity: str
    link_count: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _HashMemo:
    """上次扫描时这个文件的状态和算出的结果；下次状态一模一样就不再读内容。"""

    device: int
    inode: int
    size: int
    mtime_ns: int
    links: int
    hashed_at_ns: int
    item: _ScannedFile

    @classmethod
    def remember(cls, item: _ScannedFile, info: os.stat_result, hashed_at_ns: int) -> _HashMemo:
        return cls(
            device=int(info.st_dev),
            inode=int(getattr(info, "st_ino", 0)),
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            links=int(getattr(info, "st_nlink", 1)),
            hashed_at_ns=hashed_at_ns,
            item=item,
        )

    def still_matches(self, info: os.stat_result) -> bool:
        # 修改时间在不在窗口外、有没有文件身份号，都是「能不能信上次的哈希」的前提。
        return (
            self.inode != 0
            and self.hashed_at_ns - self.mtime_ns >= RACY_WINDOW_NS
            and (
                int(info.st_dev),
                int(getattr(info, "st_ino", 0)),
                int(info.st_size),
                int(info.st_mtime_ns),
                int(getattr(info, "st_nlink", 1)),
            )
            == (self.device, self.inode, self.size, self.mtime_ns, self.links)
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError as exc:
        raise ProjectReaderError(f"cannot inspect path metadata: {path}") from exc
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _file_is_link_or_reparse(info: os.stat_result) -> bool:
    """文件这一项是不是链接 / 重解析点：用一次 lstat 的结果判断（junction 也是重解析点）。"""
    if stat_module.S_ISLNK(info.st_mode):
        return True
    return bool(int(getattr(info, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE)


def _resolve_directory(raw: str | os.PathLike[str], *, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise UntrustedProjectPathError(f"{label} must be an absolute path: {raw}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UntrustedProjectPathError(
            f"{label} does not resolve to an existing directory: {raw}"
        ) from exc
    if not resolved.is_dir():
        raise UntrustedProjectPathError(f"{label} is not a directory: {resolved}")
    return resolved


def _canonical_path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve(strict=True))).replace("\\", "/")
    return normalized


def _stable_project_id(root: Path) -> str:
    digest = hashlib.sha256(_canonical_path_identity(root).encode("utf-8")).hexdigest()
    return f"project-{digest[:24]}"


def _stable_file_id(
    project_id: str,
    scanned: _ScannedFile,
) -> str:
    digest = hashlib.sha256(
        f"{project_id}\0{scanned.identity}".encode("utf-8")
    ).hexdigest()
    return f"file-{digest[:32]}"


def _stat_identity(stat_result: os.stat_result, relative_path: str) -> str:
    inode = int(getattr(stat_result, "st_ino", 0))
    device = int(getattr(stat_result, "st_dev", 0))
    if inode:
        return f"fs:{device}:{inode}"

    # Modern Windows and Unix filesystems expose an inode/file index. This
    # fallback keeps the reader usable on unusual filesystems, but cannot
    # promise rename-stability there.
    birth_ns = int(
        getattr(
            stat_result,
            "st_birthtime_ns",
            getattr(stat_result, "st_ctime_ns", 0),
        )
    )
    return f"fallback:{birth_ns}:{relative_path.casefold()}"


def _hash_stable_file(
    path: Path,
    relative_path: str,
    *,
    max_bytes: int | None = None,
    before: os.stat_result | None = None,
) -> _ScannedFile:
    """``before`` 是扫描时刚查过的文件状态（普通文件的 lstat 与 stat 相同），给了就不再查一遍。"""
    try:
        if before is None:
            before = path.stat()
        if max_bytes is not None and before.st_size > max_bytes:
            raise ProjectScanLimitError(
                f"project scan exceeds its remaining {max_bytes} byte budget: {path}"
            )
        digest = hashlib.sha256()
        bytes_read = 0
        # 缓冲按文件大小开（最多 1MB）、反复用。以前每次 read(1MB) 都新要一块 1MB 内存，
        # 读到结尾还要再要一次；《拜托》1759 个文件光这一项 3–4 秒，按文件大小读 0.4 秒。
        buffer = memoryview(bytearray(min(before.st_size + 1, HASH_CHUNK_BYTES)))
        with path.open("rb", buffering=0) as handle:
            while count := handle.readinto(buffer):
                bytes_read += count
                if max_bytes is not None and bytes_read > max_bytes:
                    raise ProjectScanLimitError(
                        f"project scan exceeds its remaining {max_bytes} byte budget: {path}"
                    )
                digest.update(buffer[:count])
        after = path.stat()
    except OSError as exc:
        raise ProjectReaderError(f"cannot read project file: {path}") from exc

    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise ProjectChangedDuringReadError(f"project file changed during inspection: {path}")
    return _ScannedFile(
        path=path,
        relative_path=relative_path,
        size_bytes=after.st_size,
        sha256=digest.hexdigest(),
        identity=_stat_identity(after, relative_path),
        link_count=int(getattr(after, "st_nlink", 1)),
        mtime_ns=after.st_mtime_ns,
    )


def _read_stable_file_limited(path: Path, max_bytes: int) -> bytes:
    try:
        before = path.stat()
        if before.st_size > max_bytes:
            raise ProjectScanLimitError(
                f"text file exceeds the {max_bytes} byte read limit: {path}"
            )
        # 按文件实际大小多读一个字节（上面已确认不超上限）：以前每读一个文件都先要 max_bytes（8MB）内存。
        # 读的过程中文件变长了，下面前后两次状态对不上会报出来。
        with path.open("rb") as handle:
            content = handle.read(before.st_size + 1)
        after = path.stat()
    except ProjectReaderError:
        raise
    except OSError as exc:
        raise ProjectReaderError(f"cannot read project text file: {path}") from exc

    before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise ProjectChangedDuringReadError(f"project file changed while reading: {path}")
    if len(content) > max_bytes:
        raise ProjectScanLimitError(
            f"text file exceeds the {max_bytes} byte read limit: {path}"
        )
    return content


def _verify_snapshot_metadata(files: Iterable[_ScannedFile]) -> None:
    for item in files:
        try:
            current = item.path.stat()
        except OSError as exc:
            raise ProjectChangedDuringReadError(
                f"project file disappeared before snapshot completion: {item.path}"
            ) from exc
        if (
            current.st_size != item.size_bytes
            or current.st_mtime_ns != item.mtime_ns
            or _stat_identity(current, item.relative_path) != item.identity
        ):
            raise ProjectChangedDuringReadError(
                f"project changed before snapshot completion: {item.path}"
            )


def _verify_snapshot_manifest(
    root: Path,
    files: Iterable[_ScannedFile],
    directories: Iterable[str],
    *,
    max_files: int,
    skipped_files: frozenset[str] = frozenset(),
) -> None:
    expected_files = {item.relative_path for item in files}
    expected_directories = set(directories)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()

    # 第二遍只比名单：文件夹照旧逐个核「不是链接、还在书里」（文件夹不多）；
    # 文件只记名字——扫描时每个文件都核过，最后的逐个 stat 核对（大小、时间、身份）
    # 也会发现「扫完之后被换成了链接」，这里不用再为每个文件解析一遍真实路径。
    for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_raw)
        current_relative = current.relative_to(root)
        if len(current_relative.parts) > MAX_SCAN_DEPTH:
            raise ProjectScanLimitError(
                f"project scan exceeds the {MAX_SCAN_DEPTH} directory depth limit: {current}"
            )
        prefix = current_relative.as_posix() + "/" if current_relative.parts else ""
        safe_directories: list[str] = []
        for name in sorted(dir_names, key=str.casefold):
            candidate = current / name
            if skip_scan_directory(name):
                continue
            if _is_link_or_reparse(candidate):
                # 第一遍也跳过了它（没算进 directories）；要是两遍之间才变成链接，下面名单对不上会报「变了」。
                continue
            resolved = candidate.resolve(strict=True)
            if not _is_within(resolved, root):
                raise UntrustedProjectPathError(
                    f"project directory escapes the trusted project root: {candidate}"
                )
            safe_directories.append(name)
            actual_directories.add(resolved.relative_to(root).as_posix())
            if len(actual_directories) > MAX_SCAN_DIRECTORIES:
                raise ProjectScanLimitError(
                    f"project scan exceeds the {MAX_SCAN_DIRECTORIES} directory limit: {root}"
                )
        dir_names[:] = safe_directories

        for name in file_names:
            actual_files.add(prefix + name)
            if len(actual_files) > max_files:
                raise ProjectScanLimitError(
                    f"project scan exceeds the {max_files} file limit: {root}"
                )

    if actual_files - skipped_files != expected_files or actual_directories != expected_directories:
        raise ProjectChangedDuringReadError(
            "project directory entries changed before snapshot completion"
        )


def _chapter_number(value: str) -> int | None:
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(value)
        if match:
            return _ordinal_number(match.group(1))
    return None


def _volume_number(value: str) -> int | None:
    for pattern in VOLUME_PATTERNS:
        match = pattern.search(value)
        if match:
            return _ordinal_number(match.group(1))
    return None


def _ordinal_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    if not any(character in units for character in value):
        return int("".join(str(digits[character]) for character in value))
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
        else:
            total += (current or 1) * units[character]
            current = 0
    return total + current


def _number_from_path(relative_path: str, parser, stem_pattern=None) -> int | None:
    path = Path(relative_path)
    number = parser(path.stem)
    if number is not None:
        return number
    # stem_pattern 只看文件名，绝不下探父目录，避免范围目录污染章号。
    if stem_pattern is not None:
        match = stem_pattern.match(path.stem)
        if match:
            return _ordinal_number(match.group(1))
    for part in reversed(path.parts[:-1]):
        number = parser(part)
        if number is not None:
            return number
    return None


def _canonical_chapter_id(number: int, volume_number: int | None) -> str:
    chapter_id = f"CH{number:0{max(3, len(str(number)))}d}"
    if volume_number is None:
        return chapter_id
    return f"{_canonical_volume_id(volume_number)}-{chapter_id}"


def _canonical_volume_id(number: int) -> str:
    return f"VOL{number:0{max(3, len(str(number)))}d}"


def _contains_marker(value: str, markers: Iterable[str]) -> bool:
    folded = value.casefold()
    return any(marker.casefold() in folded for marker in markers)


MACHINE_COMPANION_MARKERS: Final = (
    # 带章号却不是正文的机器伴生文件：控制卡、审稿 / 检测 / 体检报告、AI 味与
    # 干净度 json 的 md 副本、受管链的交接包与状态文件。全是英文机器词或
    # 完整的中文报告名，作者给章节起名撞不上它们。
    *CONTROL_MARKERS,
    "ai_smell",
    "cleanliness",
    "report",
    "audit",
    "writeback",
    "handoff",
    "operation_package",
    "operation-package",
    "prewrite",
    "scaffold",
    "route_health",
    "readiness",
    "call_list",
    "审稿报告",
    "检测报告",
    "体检报告",
    "审稿意见",
)


def _prose_stem(stem: str) -> bool:
    return _contains_marker(stem, PROSE_MARKERS) or any(
        pattern.fullmatch(stem) for pattern in CHAPTER_PROSE_STEM_PATTERNS
    )


def _file_kind(relative_path: str, section_index: int | None, chapter: int | None) -> str:
    suffix = Path(relative_path).suffix.casefold()
    stem = Path(relative_path).stem
    # 带章号的文本文件先按正文判。作者的章名可以叫「交接」「健康的代价」
    # 「门禁之夜」「上下文」——以前受管链的五组关键词先于章号匹配整条路径，
    # 这些章要么读不到、要么不算正文（2026-09-22 审阅 A0 §1.1）。
    # 只有名字里带机器伴生标记（控制卡、审稿报告、ai_smell…）的才不算正文。
    if (
        section_index in {None, 5}
        and chapter is not None
        and suffix in TEXT_SUFFIXES
        and not _contains_marker(stem, NON_PROSE_MARKERS)
        and not _contains_marker(stem, MACHINE_COMPANION_MARKERS)
        and _prose_stem(stem)
    ):
        return "prose"
    if _contains_marker(relative_path, CONTROL_MARKERS):
        return "control"
    if _contains_marker(relative_path, HANDOFF_MARKERS):
        return "handoff"
    if _contains_marker(relative_path, CONTEXT_MARKERS):
        return "context"
    if _contains_marker(relative_path, REVIEW_MARKERS):
        return "review"
    if _contains_marker(relative_path, STATUS_MARKERS):
        return "status"
    if suffix in STRUCTURED_SUFFIXES:
        return "structured_state"
    if section_index in {None, 5} and chapter is not None and suffix in TEXT_SUFFIXES:
        if _contains_marker(relative_path, NON_PROSE_MARKERS):
            return "document"
        if _prose_stem(stem):
            return "prose"
    if suffix in TEXT_SUFFIXES:
        return "document"
    return "asset"


def classify_project_file_kind(relative_path: str) -> str:
    """Classify an existing or proposed project path.

    ChangeSet creation uses this public helper for files that do not exist in a
    Project Reader snapshot yet. Keeping the path rules here prevents writeback
    and Gate policy from growing a second, divergent prose classifier.
    """

    path = Path(relative_path)
    first_part = path.parts[0] if path.parts else ""
    section_match = SECTION_PATTERN.match(first_part)
    section_index = int(section_match.group(1)) if section_match else None
    chapter_number = _number_from_path(
        relative_path, _chapter_number, CHAPTER_STEM_NUMBER_PATTERN
    )
    if chapter_number is not None and is_non_truth_project_path(relative_path):
        # 标着「勿作为真值 / 嵌套项目副本」的夹子是旧稿副本：续写、检查、搜索早就跳过它，
        # 目录和列文件也别把里面的旧章当正文（2026-09-25 真机：宗门旧归档夹的第 1–3 章排在正文最前面）。
        chapter_number = None
    if (
        chapter_number is not None
        and len(path.parts) == 1
        and _chapter_number(path.stem) is None
        and _root_stem_is_not_a_chapter(path.stem, chapter_number)
    ):
        # 书根目录下只靠开头数字的文件：`00_项目总览.md`、`00_标准化迁移说明.md` 是总览和说明，
        # 不是第 0 章（2026-09-25 真机：目录里冒出「第 000 章 标准化迁移说明」）。根目录的章要么带
        # 「第 N 章 / CH」这类明确标记，要么是 `001_开篇.md` 这种三位以上的章号。
        chapter_number = None
    return _file_kind(relative_path, section_index, chapter_number)


def _root_stem_is_not_a_chapter(stem: str, chapter_number: int) -> bool:
    digits = re.match(r"\d+", stem)
    return chapter_number == 0 or digits is None or len(digits.group(0)) < 3


DEFAULT_PROSE_SECTION: Final = "05_正文内容"


def prose_directories(project_root: Path) -> list[Path]:
    """这本书里放正文的夹子，可能有不止一个。

    以前检查、续写、AI 味基线四处写死 `05_正文内容`：作者把夹子改了名，
    这几处一起静默查空——不报错，只是「一章都没有」。这里统一按
    默认夹子 → 同号 `05_*` → 名字带正文/初稿/draft 的夹子 → 整本书退让。
    **默认摆法的书行为一字不变**（第一条就命中）。
    """
    root = Path(project_root)
    default = root / DEFAULT_PROSE_SECTION
    if default.is_dir():
        return [default]
    if not root.is_dir():
        return []
    excluded = {name.casefold() for name in EXCLUDED_DIRECTORIES}
    named: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not entry.is_dir() or entry.name.casefold() in excluded:
            continue
        if is_non_truth_project_path(entry.name):
            continue
        match = SECTION_PATTERN.match(entry.name)
        if match is not None:
            # 别的 0N_ 分区是作者自己标了号的非正文资料，不算正文。
            if match.group(1) == "5":
                named.append(entry)
            continue
        folded = entry.name.casefold()
        if any(marker.casefold() in folded for marker in PROSE_MARKERS):
            named.append(entry)
    if named:
        return named
    # 一个像正文的夹子都没有：章可能直接摆在书根或卷目录下，整本书交给
    # 文件分类器判断（它只认带章号的文本文件，docx/epub 导出档不算）。
    return [root]


def in_numbered_non_prose_section(relative_path: str) -> bool:
    """作者自己标了 `0N_` 号、又不在 05_ 里的分区绝不是正文。

    只在整本书兜底扫描时用：`04_剧情大纲/第12章_细纲.md` 带着章号，
    不挡就会被当成第十二章的正文去查节奏和设定。
    """
    parts = relative_path.replace("\\", "/").split("/")
    if not parts or not parts[0]:
        return False
    match = SECTION_PATTERN.match(parts[0])
    return match is not None and match.group(1) != "5"


class ProjectReader:
    """Inspect explicitly trusted project folders without writing project state."""

    def __init__(
        self,
        trusted_roots: Iterable[str | os.PathLike[str]],
        *,
        max_files: int = MAX_SCAN_FILES,
        max_bytes: int = MAX_SCAN_BYTES,
    ) -> None:
        roots: list[Path] = []
        for raw_root in trusted_roots:
            root = _resolve_directory(raw_root, label="trusted root")
            if root not in roots:
                roots.append(root)
        if not roots:
            raise UntrustedProjectPathError("at least one explicit trusted root is required")
        if max_files < 1 or max_bytes < 1:
            raise ValueError("project scan budgets must be positive")
        self.trusted_roots = tuple(roots)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._revisions: dict[str, _RevisionState] = {}
        self._revision_lock = threading.Lock()
        # Agent 每调一次工具都要把整本书扫一遍核对版本；书多半没变。
        # 按书记住每个文件上次的哈希（状态没变就不再读内容），按路径记住分类结果（只看路径）。
        self._hash_memo: dict[str, dict[str, _HashMemo]] = {}
        self._path_facts: dict[str, tuple[int | None, int | None, str]] = {}
        self._memo_lock = threading.Lock()

    def trust_root(self, raw_root: str | os.PathLike[str]) -> Path:
        """Dynamically allow a project directory if it was bound by the author."""
        candidate = Path(raw_root).expanduser().resolve(strict=False)
        if not any(_is_within(candidate, r) for r in self.trusted_roots):
            self.trusted_roots = self.trusted_roots + (candidate,)
        return candidate

    def untrust_root(self, raw_root: str | os.PathLike[str]) -> Path:
        """设置页移除一个可信文件夹：当场失效（重启后由持久化清单保证不再回来）。"""
        candidate = Path(raw_root).expanduser().resolve(strict=False)
        self.trusted_roots = tuple(
            root for root in self.trusted_roots if root != candidate
        )
        return candidate

    def resolve(self, raw_root: str | os.PathLike[str]) -> Path:
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            raise UntrustedProjectPathError(f"project root must be an absolute path: {raw_root}")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UntrustedProjectPathError(
                f"project root does not resolve to an existing directory: {raw_root}"
            ) from exc
        if not resolved.is_dir():
            raise UntrustedProjectPathError(f"project root is not a directory: {resolved}")
        if not any(_is_within(resolved, root) for root in self.trusted_roots):
            raise UntrustedProjectPathError(
                f"project root is outside configured trusted roots: {resolved}"
            )
        return resolved

    def _walk(
        self,
        root: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[_ScannedFile], list[str]]:
        scanned: list[_ScannedFile] = []
        directories: list[str] = []
        total_bytes = 0
        memo_key = os.path.normcase(str(root))
        with self._memo_lock:
            previous = self._hash_memo.get(memo_key, {})
        fresh: dict[str, _HashMemo] = {}

        # 大书导入要有真实进度：先快速数一遍文件总数（只列目录，不读内容），
        # 哈希阶段每若干个文件回报一次 done/total。
        total_hint = 0
        if progress is not None:
            for _current, hint_dirs, file_names in os.walk(root, followlinks=False):
                hint_dirs[:] = [name for name in hint_dirs if not skip_scan_directory(name)]
                total_hint += len(file_names)
            progress(0, total_hint)
        # 链接文件、不是普通文件的那几项：不进快照；第二遍核名单时要减掉它们。
        skipped_files: set[str] = set()

        hashed = 0

        def report() -> None:
            nonlocal hashed
            if progress is not None and (hashed == 1 or hashed % 25 == 0):
                progress(hashed, max(total_hint, hashed))

        for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_raw)
            current_relative = current.relative_to(root)
            if len(current_relative.parts) > MAX_SCAN_DEPTH:
                raise ProjectScanLimitError(
                    f"project scan exceeds the {MAX_SCAN_DEPTH} directory depth limit: {current}"
                )
            prefix = current_relative.as_posix() + "/" if current_relative.parts else ""
            safe_directories: list[str] = []
            for name in sorted(dir_names, key=str.casefold):
                candidate = current / name
                if skip_scan_directory(name):
                    continue
                if _is_link_or_reparse(candidate):
                    # 链接 / 联接点指向别处：不跟进去、不算进这个项目（以前整个扫描直接失败，
                    # 「文档」这类带系统联接点的文件夹就打不开）。外面的东西照样一个字都不读。
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ProjectReaderError(f"cannot resolve project directory: {candidate}") from exc
                if not _is_within(resolved, root):
                    raise UntrustedProjectPathError(
                        f"project directory escapes the trusted project root: {candidate}"
                    )
                safe_directories.append(name)
                directories.append(resolved.relative_to(root).as_posix())
                if len(directories) > MAX_SCAN_DIRECTORIES:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {MAX_SCAN_DIRECTORIES} directory limit: {root}"
                    )
            dir_names[:] = safe_directories

            # 文件只做一次 lstat：是不是链接 / 重解析点、是不是普通文件、多大，都从这一次里看。
            # 所在的每一级文件夹上面已经逐个核过「不是链接、还在书里」，所以普通文件
            # 一定在书里，不用再为每个文件解析一遍真实路径（Windows 上每次解析要开两回句柄，
            # 九百多章的书光这一项就几秒）。
            for name in sorted(file_names, key=str.casefold):
                candidate = current / name
                try:
                    info = os.lstat(candidate)
                except OSError as exc:
                    raise ProjectReaderError(f"cannot inspect path metadata: {candidate}") from exc
                if _file_is_link_or_reparse(info) or not stat_module.S_ISREG(info.st_mode):
                    # 链接文件 / 不是普通文件：不进快照、不读内容（以前整个扫描失败）。
                    skipped_files.add(prefix + name)
                    continue
                if len(scanned) >= self.max_files:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {self.max_files} file limit: {root}"
                    )
                if total_bytes + info.st_size > self.max_bytes:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {self.max_bytes} byte limit: {root}"
                    )
                relative_path = prefix + name
                remembered = previous.get(relative_path)
                if remembered is not None and remembered.still_matches(info):
                    item = replace(remembered.item, path=candidate)
                    fresh[relative_path] = remembered
                else:
                    item = _hash_stable_file(
                        candidate,
                        relative_path,
                        max_bytes=self.max_bytes - total_bytes,
                        before=info,
                    )
                    fresh[relative_path] = _HashMemo.remember(item, info, time.time_ns())
                total_bytes += item.size_bytes
                scanned.append(item)
                hashed += 1
                report()

        _verify_snapshot_metadata(scanned)
        _verify_snapshot_manifest(
            root,
            scanned,
            directories,
            max_files=self.max_files,
            skipped_files=frozenset(skipped_files),
        )
        # 整次扫描核对通过才记下；中途出错的那次不留。
        with self._memo_lock:
            self._hash_memo[memo_key] = fresh
        return scanned, sorted(directories, key=str.casefold)

    def _next_revision(self, project_id: str, fingerprint: str) -> int:
        with self._revision_lock:
            previous = self._revisions.get(project_id)
            if previous is None:
                revision = 1
            elif previous.fingerprint == fingerprint:
                revision = previous.revision
            else:
                revision = previous.revision + 1
            self._revisions[project_id] = _RevisionState(fingerprint, revision)
            return revision

    def inspect(
        self,
        raw_root: str | os.PathLike[str],
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        root = self.resolve(raw_root)
        scanned, directories = self._walk(root, progress)

        creation_mode = "manual"
        for item in scanned:
            # 老书看 01_项目定位/01_项目卡片.md，新书（2026-09-24 起）看书根《这本书.md》。
            if item.relative_path.replace("\\", "/") not in {
                "01_项目定位/01_项目卡片.md",
                "这本书.md",
            }:
                continue
            if item.size_bytes > 256 * 1024:
                break
            try:
                project_card = item.path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                break
            if "## 现在的想法" in project_card and (
                "## 已经确定的内容" in project_card or "## 已经定下的事" in project_card
            ):
                creation_mode = "agent"
            break

        section_paths: dict[int, str] = {}
        for directory in directories:
            if "/" in directory:
                continue
            match = SECTION_PATTERN.match(directory)
            if match:
                section_paths.setdefault(int(match.group(1)), directory)

        project_id = _stable_project_id(root)
        hardlinks = [item.path for item in scanned if item.link_count > 1]
        if hardlinks:
            raise UntrustedProjectPathError(
                "hardlinked files are not supported in a trusted project snapshot: "
                + ", ".join(str(path) for path in hardlinks[:5])
            )
        files: list[dict[str, object]] = []
        for item in scanned:
            first_part = item.relative_path.split("/", 1)[0]
            section_index = next(
                (index for index, path in section_paths.items() if path == first_part),
                None,
            )
            facts = self._path_facts.get(item.relative_path)
            if facts is None:
                facts = (
                    _number_from_path(item.relative_path, _chapter_number, CHAPTER_STEM_NUMBER_PATTERN),
                    _number_from_path(item.relative_path, _volume_number),
                    classify_project_file_kind(item.relative_path),
                )
                if len(self._path_facts) < MAX_REMEMBERED_PATHS:
                    self._path_facts[item.relative_path] = facts
            chapter_number, volume_number, kind = facts
            file_id = _stable_file_id(project_id, item)
            files.append(
                {
                    "file_id": file_id,
                    "relative_path": item.relative_path,
                    "absolute_path": str(item.path),
                    "section_index": section_index,
                    "chapter_id": (
                        _canonical_chapter_id(chapter_number, volume_number)
                        if chapter_number is not None and (section_index == 5 or kind == "prose")
                        else None
                    ),
                    "volume_id": (
                        _canonical_volume_id(volume_number)
                        if volume_number is not None
                        else None
                    ),
                    "kind": kind,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
            )
        files.sort(key=lambda item: str(item["relative_path"]).casefold())
        file_ids = [str(item["file_id"]) for item in files]
        if len(file_ids) != len(set(file_ids)):
            raise ProjectReaderError("project snapshot produced duplicate stable file IDs")

        chapter_map: dict[str, dict[str, object]] = {}
        for item in files:
            chapter_id = item["chapter_id"]
            if not isinstance(chapter_id, str):
                continue
            entry = chapter_map.setdefault(
                chapter_id,
                {
                    "chapter_id": chapter_id,
                    "number": int(chapter_id.rsplit("CH", 1)[1]),
                    "volume_id": item["volume_id"],
                    "file_ids": [],
                    "prose_file_ids": [],
                },
            )
            entry["file_ids"].append(item["file_id"])
            if item["kind"] == "prose":
                entry["prose_file_ids"].append(item["file_id"])
            if entry["volume_id"] is None and item["volume_id"] is not None:
                entry["volume_id"] = item["volume_id"]

        def chapter_sort_key(item: dict[str, object]) -> tuple[int, int, int]:
            volume_id = item["volume_id"]
            if isinstance(volume_id, str):
                return (0, int(volume_id[3:]), int(item["number"]))
            # Keep the API order aligned with the Workbench tree, which renders
            # explicitly numbered volumes before its final unassigned group.
            return (1, 0, int(item["number"]))

        chapters = sorted(chapter_map.values(), key=chapter_sort_key)
        volume_map: dict[str, dict[str, object]] = {}
        for chapter in chapters:
            volume_id = chapter["volume_id"]
            if not isinstance(volume_id, str):
                continue
            entry = volume_map.setdefault(
                volume_id,
                {
                    "volume_id": volume_id,
                    "number": int(volume_id[3:]),
                    "chapter_ids": [],
                },
            )
            entry["chapter_ids"].append(chapter["chapter_id"])
        volumes = sorted(volume_map.values(), key=lambda item: int(item["number"]))

        sections = [
            {
                "index": index,
                "label": label,
                "present": index in section_paths,
                "relative_path": section_paths.get(index),
            }
            for index, label in SECTION_LABELS.items()
        ]
        missing_sections = (
            [index for index in SECTION_LABELS if index not in section_paths]
            if section_paths else []
        )
        fingerprint_payload = {
            "schema": READER_SCHEMA,
            "project_id": project_id,
            "directories": directories,
            "sections": sections,
            "files": [
                {
                    "file_id": item["file_id"],
                    "relative_path": item["relative_path"],
                    "sha256": item["sha256"],
                    "size_bytes": item["size_bytes"],
                    "kind": item["kind"],
                }
                for item in files
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        revision = self._next_revision(project_id, fingerprint)
        warnings = (
            ["missing 01-09 sections: " + ", ".join(f"{item:02d}" for item in missing_sections)]
            if missing_sections
            else []
        )

        return {
            "schema": READER_SCHEMA,
            "read_only": True,
            "project": {
                "project_id": project_id,
                "display_name": root.name,
                "root_path": str(root),
                "format": "01-09" if section_paths else "folder",
                "creation_mode": creation_mode,
                "revision": revision,
                "fingerprint": fingerprint,
            },
            "sections": sections,
            "missing_sections": missing_sections,
            "volumes": volumes,
            "chapters": chapters,
            "files": files,
            "warnings": warnings,
        }

    def read_text_file(
        self,
        raw_root: str | os.PathLike[str],
        file_id: str,
    ) -> dict[str, object]:
        snapshot = self.inspect(raw_root)
        return self.read_text_file_from_snapshot(
            snapshot,
            file_id,
            allowed_kinds=PROJECT_FILE_KINDS,
        )

    def read_text_file_from_snapshot(
        self,
        snapshot: Mapping[str, object],
        file_id: str,
        *,
        allowed_kinds: Iterable[str],
    ) -> dict[str, object]:
        """Read a text file without rescanning an already inspected project."""
        allowed = frozenset(allowed_kinds)
        if not allowed:
            raise ValueError("allowed_kinds must contain at least one project file kind")
        unsupported = allowed - PROJECT_FILE_KINDS
        if unsupported:
            raise ValueError(
                "allowed_kinds contains unsupported project file kinds: "
                + ", ".join(sorted(unsupported))
            )

        if snapshot.get("schema") != READER_SCHEMA or snapshot.get("read_only") is not True:
            raise ProjectReaderError("invalid project inspection snapshot")
        project = snapshot.get("project")
        files = snapshot.get("files")
        if not isinstance(project, Mapping) or not isinstance(files, list):
            raise ProjectReaderError("invalid project inspection snapshot")

        raw_snapshot_root = project.get("root_path")
        project_id = project.get("project_id")
        revision = project.get("revision")
        fingerprint = project.get("fingerprint")
        if (
            not isinstance(raw_snapshot_root, str)
            or not isinstance(project_id, str)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ProjectReaderError("invalid project identity in inspection snapshot")
        root = self.resolve(raw_snapshot_root)
        if _stable_project_id(root) != project_id:
            raise UntrustedProjectPathError(
                "project snapshot identity does not match its trusted root"
            )

        file_record = next(
            (
                item
                for item in files
                if isinstance(item, Mapping) and item.get("file_id") == file_id
            ),
            None,
        )
        if file_record is None:
            raise ProjectFileNotFoundError(
                f"file_id is not present in the current snapshot: {file_id}"
            )
        kind = file_record.get("kind")
        if not isinstance(kind, str) or kind not in allowed:
            raise UnreadableProjectFileError(
                f"file_id is not a readable text document: {file_id}"
            )
        size_value = file_record.get("size_bytes")
        expected_sha256 = file_record.get("sha256")
        relative_value = file_record.get("relative_path")
        absolute_value = file_record.get("absolute_path")
        if (
            not isinstance(size_value, int)
            or size_value < 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or not isinstance(relative_value, str)
            or not isinstance(absolute_value, str)
        ):
            raise ProjectReaderError(f"invalid file record in inspection snapshot: {file_id}")
        size_bytes = size_value
        read_limit = MAX_ARCHIVE_BYTES if Path(relative_value).suffix.lower() in OFFICE_SUFFIXES else MAX_TEXT_FILE_BYTES
        if size_bytes > read_limit:
            raise ProjectScanLimitError(
                f"file exceeds the {read_limit} byte read limit: {file_id}"
            )

        relative_path = Path(relative_value)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise UntrustedProjectPathError(
                f"project snapshot contains an unsafe relative path: {relative_value}"
            )
        path = root.joinpath(*relative_path.parts)
        recorded_path = Path(absolute_value)
        recorded_identity = os.path.normcase(str(recorded_path))
        expected_identity = os.path.normcase(str(path))
        if not recorded_path.is_absolute() or recorded_identity != expected_identity:
            raise UntrustedProjectPathError(
                f"project snapshot file path does not match its trusted root: {absolute_value}"
            )

        current = root
        for part in relative_path.parts:
            current /= part
            if _is_link_or_reparse(current):
                raise UntrustedProjectPathError(
                    f"project file became a link or junction: {current}"
                )
        try:
            resolved = path.resolve(strict=True)
            if not _is_within(resolved, root):
                raise UntrustedProjectPathError(f"project file escaped its snapshot root: {path}")
            if int(getattr(resolved.stat(), "st_nlink", 1)) > 1:
                raise UntrustedProjectPathError(
                    f"project file became a hardlink after inspection: {path}"
                )
            content_bytes = _read_stable_file_limited(resolved, read_limit)
            if int(getattr(resolved.stat(), "st_nlink", 1)) > 1:
                raise UntrustedProjectPathError(
                    f"project file became a hardlink while reading: {path}"
                )
        except OSError as exc:
            raise ProjectReaderError(f"cannot read project text file: {path}") from exc
        digest = hashlib.sha256(content_bytes).hexdigest()
        if len(content_bytes) != size_bytes or digest != expected_sha256:
            raise ProjectChangedDuringReadError(f"project file changed after snapshot: {path}")
        # File kinds describe the old writing workspace; a source/configuration
        # file can have kind=asset or structured_state and still be plain text.
        # Inspect bytes rather than maintaining a second extension allowlist.
        extraction = None
        try:
            if path.suffix.lower() in OFFICE_SUFFIXES:
                extracted = extract_office_bytes(content_bytes, path.suffix)
                content = extracted.extracted_text
                extraction = extraction_receipt(extracted)
            else:
                content, _, _ = decode_project_text(content_bytes)
        except OfficeError as exc:
            raise UnreadableProjectFileError(str(exc)) from exc
        except UnicodeDecodeError as exc:
            raise UnreadableProjectFileError(
                f"file is not readable text (UTF-8 or Unicode with a byte-order mark): {path}"
            ) from exc
        except ValueError as exc:
            raise UnreadableProjectFileError(f"{exc}: {path}") from exc

        return {
            "schema": "zhimeng.project-file-content.v1",
            "read_only": True,
            "project_id": project_id,
            "snapshot_revision": revision,
            "project_fingerprint": fingerprint,
            "file": dict(file_record),
            "etag": digest,
            "content": content,
            **({"extraction": extraction} if extraction else {}),
        }
