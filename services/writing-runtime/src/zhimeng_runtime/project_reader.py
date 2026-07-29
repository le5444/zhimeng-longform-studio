from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Mapping


READER_SCHEMA: Final = "zhimeng.project-inspection.v1"
MAX_SCAN_FILES: Final = 20_000
MAX_SCAN_BYTES: Final = 512 * 1024 * 1024
MAX_SCAN_DIRECTORIES: Final = 5_000
MAX_SCAN_DEPTH: Final = 32
MAX_TEXT_FILE_BYTES: Final = 8 * 1024 * 1024
REPARSE_POINT_ATTRIBUTE: Final = 0x400

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
STATUS_MARKERS: Final = ("status", "状态", "gate", "门禁", "health", "健康")
REVIEW_MARKERS: Final = ("review", "审稿", "校验", "audit", "writeback", "回填")
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
) -> _ScannedFile:
    try:
        before = path.stat()
        if max_bytes is not None and before.st_size > max_bytes:
            raise ProjectScanLimitError(
                f"project scan exceeds its remaining {max_bytes} byte budget: {path}"
            )
        digest = hashlib.sha256()
        bytes_read = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                bytes_read += len(block)
                if max_bytes is not None and bytes_read > max_bytes:
                    raise ProjectScanLimitError(
                        f"project scan exceeds its remaining {max_bytes} byte budget: {path}"
                    )
                digest.update(block)
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
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
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
) -> None:
    expected_files = {item.relative_path for item in files}
    expected_directories = set(directories)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()

    for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_raw)
        if len(current.relative_to(root).parts) > MAX_SCAN_DEPTH:
            raise ProjectScanLimitError(
                f"project scan exceeds the {MAX_SCAN_DEPTH} directory depth limit: {current}"
            )
        safe_directories: list[str] = []
        for name in sorted(dir_names, key=str.casefold):
            candidate = current / name
            if _is_link_or_reparse(candidate):
                raise ProjectChangedDuringReadError(
                    f"project link state changed before snapshot completion: {candidate}"
                )
            if name.casefold() in EXCLUDED_DIRECTORIES:
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

        for name in sorted(file_names, key=str.casefold):
            candidate = current / name
            if _is_link_or_reparse(candidate):
                raise ProjectChangedDuringReadError(
                    f"project link state changed before snapshot completion: {candidate}"
                )
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file() or not _is_within(resolved, root):
                raise UntrustedProjectPathError(
                    f"project file escapes the trusted project root: {candidate}"
                )
            actual_files.add(resolved.relative_to(root).as_posix())
            if len(actual_files) > max_files:
                raise ProjectScanLimitError(
                    f"project scan exceeds the {max_files} file limit: {root}"
                )

    if actual_files != expected_files or actual_directories != expected_directories:
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


def _file_kind(relative_path: str, section_index: int | None, chapter: int | None) -> str:
    suffix = Path(relative_path).suffix.casefold()
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
    if section_index == 5 and chapter is not None and suffix in TEXT_SUFFIXES:
        if _contains_marker(relative_path, NON_PROSE_MARKERS):
            return "document"
        stem = Path(relative_path).stem
        if _contains_marker(stem, PROSE_MARKERS) or any(
            pattern.fullmatch(stem) for pattern in CHAPTER_PROSE_STEM_PATTERNS
        ):
            return "prose"
    if suffix in TEXT_SUFFIXES:
        return "document"
    return "asset"


def classify_project_file_kind(relative_path: str) -> str:
    """Classify an existing or proposed portable 01-09 project path.

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
    return _file_kind(relative_path, section_index, chapter_number)


class ProjectReader:
    """Inspect explicitly trusted 01-09 projects without writing project state."""

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

    def _walk(self, root: Path) -> tuple[list[_ScannedFile], list[str]]:
        scanned: list[_ScannedFile] = []
        directories: list[str] = []
        total_bytes = 0

        for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_raw)
            current_relative = current.relative_to(root)
            if len(current_relative.parts) > MAX_SCAN_DEPTH:
                raise ProjectScanLimitError(
                    f"project scan exceeds the {MAX_SCAN_DEPTH} directory depth limit: {current}"
                )
            safe_directories: list[str] = []
            for name in sorted(dir_names, key=str.casefold):
                candidate = current / name
                if _is_link_or_reparse(candidate):
                    raise UntrustedProjectPathError(
                        f"links and junctions are not allowed in a project scan: {candidate}"
                    )
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ProjectReaderError(f"cannot resolve project directory: {candidate}") from exc
                if not _is_within(resolved, root):
                    raise UntrustedProjectPathError(
                        f"project directory escapes the trusted project root: {candidate}"
                    )
                if name.casefold() in EXCLUDED_DIRECTORIES:
                    continue
                safe_directories.append(name)
                directories.append(resolved.relative_to(root).as_posix())
                if len(directories) > MAX_SCAN_DIRECTORIES:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {MAX_SCAN_DIRECTORIES} directory limit: {root}"
                    )
            dir_names[:] = safe_directories

            for name in sorted(file_names, key=str.casefold):
                candidate = current / name
                if _is_link_or_reparse(candidate):
                    raise UntrustedProjectPathError(
                        f"links and junctions are not allowed in a project scan: {candidate}"
                    )
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ProjectReaderError(f"cannot resolve project file: {candidate}") from exc
                if not resolved.is_file() or not _is_within(resolved, root):
                    raise UntrustedProjectPathError(
                        f"project file escapes the trusted project root: {candidate}"
                    )
                if len(scanned) >= self.max_files:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {self.max_files} file limit: {root}"
                    )
                relative_path = resolved.relative_to(root).as_posix()
                try:
                    candidate_size = resolved.stat().st_size
                except OSError as exc:
                    raise ProjectReaderError(
                        f"cannot inspect project file size: {resolved}"
                    ) from exc
                if total_bytes + candidate_size > self.max_bytes:
                    raise ProjectScanLimitError(
                        f"project scan exceeds the {self.max_bytes} byte limit: {root}"
                    )
                item = _hash_stable_file(
                    resolved,
                    relative_path,
                    max_bytes=self.max_bytes - total_bytes,
                )
                total_bytes += item.size_bytes
                scanned.append(item)

        _verify_snapshot_metadata(scanned)
        _verify_snapshot_manifest(
            root,
            scanned,
            directories,
            max_files=self.max_files,
        )
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

    def inspect(self, raw_root: str | os.PathLike[str]) -> dict[str, object]:
        root = self.resolve(raw_root)
        scanned, directories = self._walk(root)

        section_paths: dict[int, str] = {}
        for directory in directories:
            if "/" in directory:
                continue
            match = SECTION_PATTERN.match(directory)
            if match:
                section_paths.setdefault(int(match.group(1)), directory)
        if not section_paths:
            raise InvalidNovelProjectError(f"no 01-09 section directories found: {root}")

        project_id = _stable_project_id(root)
        hardlinks = [item.path for item in scanned if item.link_count > 1]
        if hardlinks:
            raise UntrustedProjectPathError(
                "hardlinked files are not supported in a trusted project snapshot: "
                + ", ".join(str(path) for path in hardlinks[:5])
            )
        files: list[dict[str, object]] = []
        for item in scanned:
            first_part = Path(item.relative_path).parts[0]
            section_index = next(
                (index for index, path in section_paths.items() if path == first_part),
                None,
            )
            chapter_number = _number_from_path(
                item.relative_path, _chapter_number, CHAPTER_STEM_NUMBER_PATTERN
            )
            volume_number = _number_from_path(item.relative_path, _volume_number)
            file_id = _stable_file_id(project_id, item)
            files.append(
                {
                    "file_id": file_id,
                    "relative_path": item.relative_path,
                    "absolute_path": str(item.path),
                    "section_index": section_index,
                    "chapter_id": (
                        _canonical_chapter_id(chapter_number, volume_number)
                        if chapter_number is not None
                        else None
                    ),
                    "volume_id": (
                        _canonical_volume_id(volume_number)
                        if volume_number is not None
                        else None
                    ),
                    "kind": classify_project_file_kind(item.relative_path),
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
        missing_sections = [index for index in SECTION_LABELS if index not in section_paths]
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
                "format": "01-09",
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
            allowed_kinds=PUBLIC_TEXT_FILE_KINDS,
        )

    def read_text_file_from_snapshot(
        self,
        snapshot: Mapping[str, object],
        file_id: str,
        *,
        allowed_kinds: Iterable[str],
    ) -> dict[str, object]:
        """Read one UTF-8 file without rescanning an already inspected project."""
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
        if size_bytes > MAX_TEXT_FILE_BYTES:
            raise ProjectScanLimitError(
                f"text file exceeds the {MAX_TEXT_FILE_BYTES} byte read limit: {file_id}"
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
            content_bytes = _read_stable_file_limited(resolved, MAX_TEXT_FILE_BYTES)
            if int(getattr(resolved.stat(), "st_nlink", 1)) > 1:
                raise UntrustedProjectPathError(
                    f"project file became a hardlink while reading: {path}"
                )
        except OSError as exc:
            raise ProjectReaderError(f"cannot read project text file: {path}") from exc
        digest = hashlib.sha256(content_bytes).hexdigest()
        if len(content_bytes) != size_bytes or digest != expected_sha256:
            raise ProjectChangedDuringReadError(f"project file changed after snapshot: {path}")
        try:
            content = content_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnreadableProjectFileError(
                f"project text file is not UTF-8: {path}"
            ) from exc

        return {
            "schema": "zhimeng.project-file-content.v1",
            "read_only": True,
            "project_id": project_id,
            "snapshot_revision": revision,
            "project_fingerprint": fingerprint,
            "file": dict(file_record),
            "etag": digest,
            "content": content,
        }
