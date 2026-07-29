#!/usr/bin/env python3
"""Bounded, read-only inspection of 01-09 novel projects.

The reader deliberately does not execute project scripts, import skills, or
write derived state.  It treats a formal ``ready`` result as something that
must be proven by the current project status, truth state, control card, and a
chapter-specific gate.  Missing or contradictory evidence cannot become a
green status by inference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike[str]]

SCHEMA_VERSION = "novel-project-reader.v1"
MAX_SCAN_FILES = 20_000
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CONTROL_CARD_BYTES = 512 * 1024
MAX_STATUS_ARTIFACTS = 256
MAX_TRUTH_STATE_FILES = 128
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)

SECTION_LABELS = {
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

SECTION_RE = re.compile(r"^0([1-9])(?:[_\-\s]|$)")
CHAPTER_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9])CH[\s_-]*0*(\d{1,6})(?!\d)"),
    re.compile(r"(?i)(?<![A-Za-z0-9])chapter[\s_-]*0*(\d{1,6})(?!\d)"),
    re.compile(r"第\s*0*(\d{1,6})\s*章"),
)

CONTROL_CARD_MARKERS = ("控制卡", "control_card", "control-card", "chapter_brief", "chapter-brief")
FINAL_MARKERS = ("定稿", "完稿", "final", "published", "发布稿")
DRAFT_MARKERS = ("初稿", "草稿", "draft", "候选稿")
REVIEW_MARKERS = (
    "审稿",
    "review",
    "回填",
    "writeback",
    "ai_smell",
    "cleanliness",
    "校验",
    "audit",
)
STATUS_MARKERS = (
    "status",
    "状态",
    "readiness",
    "prewrite",
    "gate",
    "handoff",
    "context_report",
)
CONTEXT_DOCUMENT_MARKERS = (
    "handoff",
    "context_report",
    "operation_package",
    "call_list",
    "route_health",
    "readiness",
    "prewrite",
    "kill_gate",
    "scaffold",
)

TRUTH_STATE_NAMES = {
    "truth_state.json",
    "project_truth.json",
    "project_truth_fingerprint.json",
    "character_state.json",
    "plot_state.json",
    "foreshadowing_state.json",
    "platform_state.json",
}

EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    "archive",
    "archives",
    "backup",
    "backups",
    "quarantine",
    "quarantine_context",
    "temp",
    "tmp",
    "备份",
    "归档",
    "隔离区",
}

BLOCK_VALUES = {
    "block",
    "blocked",
    "contaminated",
    "error",
    "fail",
    "failed",
    "fatal",
    "hard_block",
    "hard_blocked",
    "invalid",
    "not_ready",
    "not_healthy",
    "reject",
    "rejected",
    "stale",
    "unhealthy",
}
RISK_VALUES = {
    "attention",
    "degraded",
    "draft_only",
    "needs_attention",
    "partial",
    "provisional",
    "risk",
    "risk_draft",
    "risk_draft_only",
    "warn",
    "warning",
}
READY_VALUES = {
    "formal_ready",
    "fresh",
    "full_draft",
    "healthy",
    "next_ready",
    "ok",
    "pass",
    "passed",
    "prewrite_ready",
    "ready",
}


class ProjectReaderError(RuntimeError):
    """Base class for project reader failures."""


class UntrustedProjectPathError(ProjectReaderError):
    """Raised when a requested path escapes the caller-provided roots."""


class InvalidNovelProjectError(ProjectReaderError):
    """Raised when a directory is not recognizably an 01-09 novel project."""


@dataclass(frozen=True)
class _StatusSignal:
    status: str
    key: str
    value: Any
    path: str
    detail: str


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_directory(raw: PathLike, label: str) -> Path:
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectReaderError(f"{label} does not resolve to an existing path: {raw}") from exc
    if not resolved.is_dir():
        raise ProjectReaderError(f"{label} is not a directory: {resolved}")
    return resolved


def _resolve_trusted_roots(trusted_roots: Iterable[PathLike]) -> Tuple[Path, ...]:
    roots: List[Path] = []
    for raw in trusted_roots:
        root = _resolve_directory(raw, "trusted root")
        if root not in roots:
            roots.append(root)
    if not roots:
        raise UntrustedProjectPathError("at least one explicit trusted root is required")
    return tuple(roots)


def resolve_trusted_project_root(raw: PathLike, trusted_roots: Iterable[PathLike]) -> Path:
    """Resolve ``raw`` and prove that it is contained by a trusted root.

    ``Path.resolve(strict=True)`` closes ``..`` and symlink escapes before the
    containment check.  The function does not silently trust a parent chosen
    from the requested project path.
    """

    roots = _resolve_trusted_roots(trusted_roots)
    candidate = _resolve_directory(raw, "project root")
    if not any(_is_within(candidate, root) for root in roots):
        raise UntrustedProjectPathError(f"project root is outside trusted roots: {candidate}")
    return candidate


def _normalise_status(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _canonical_chapter_id(number: int) -> str:
    width = max(3, len(str(number)))
    return f"CH{number:0{width}d}"


def _chapter_number(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value)
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    if re.fullmatch(r"0*\d{1,6}", text.strip()):
        return int(text)
    return None


def _chapter_number_from_path(path: Path, scan_root: Path) -> Optional[int]:
    number = _chapter_number(path.stem)
    if number is not None:
        return number
    try:
        relative = path.relative_to(scan_root)
    except ValueError:
        return None
    for part in reversed(relative.parts[:-1]):
        number = _chapter_number(part)
        if number is not None:
            return number
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_limited(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise ProjectReaderError(f"file exceeds read limit ({size} > {max_bytes} bytes): {path}")
    return path.read_text(encoding="utf-8-sig")


def _read_json_limited(path: Path) -> Any:
    text = _read_text_limited(path, MAX_JSON_BYTES)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectReaderError(f"invalid JSON at {path}: line {exc.lineno}, column {exc.colno}") from exc


def _relative_path(path: Path, project_root: Path, kb_root: Optional[Path]) -> Tuple[str, str]:
    if _is_within(path, project_root):
        return "project", str(path.relative_to(project_root))
    if kb_root is not None and _is_within(path, kb_root):
        return "knowledge_base", str(path.relative_to(kb_root))
    return "external", path.name


def _evidence(path: Path, kind: str, project_root: Path, kb_root: Optional[Path]) -> Dict[str, Any]:
    source, relative = _relative_path(path, project_root, kb_root)
    return {
        "kind": kind,
        "source": source,
        "path": str(path),
        "relative_path": relative,
        "bytes": path.stat().st_size,
    }


def _safe_walk_files(
    root: Path,
    warnings: List[str],
    *,
    max_files: int = MAX_SCAN_FILES,
) -> Iterator[Path]:
    seen = 0
    for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_raw)
        safe_dirs: List[str] = []
        for name in sorted(dir_names, key=str.casefold):
            candidate = current / name
            if name.casefold() in EXCLUDED_DIR_NAMES:
                continue
            if candidate.is_symlink():
                warnings.append(f"skipped symlink directory during read-only scan: {candidate}")
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                warnings.append(f"skipped unreadable directory: {candidate}")
                continue
            if not _is_within(resolved, root):
                warnings.append(f"skipped directory outside project boundary: {candidate}")
                continue
            safe_dirs.append(name)
        dir_names[:] = safe_dirs

        for name in sorted(file_names, key=str.casefold):
            candidate = current / name
            if candidate.is_symlink():
                warnings.append(f"skipped symlink file during read-only scan: {candidate}")
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                warnings.append(f"skipped unreadable file: {candidate}")
                continue
            if not resolved.is_file() or not _is_within(resolved, root):
                warnings.append(f"skipped file outside project boundary: {candidate}")
                continue
            seen += 1
            if seen > max_files:
                raise ProjectReaderError(f"scan limit exceeded under {root}: more than {max_files} files")
            yield resolved


def _discover_sections(project_root: Path, warnings: List[str]) -> Dict[int, Path]:
    matches: Dict[int, List[Path]] = {}
    for child in project_root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        match = SECTION_RE.match(child.name)
        if match:
            resolved = child.resolve(strict=True)
            if not _is_within(resolved, project_root):
                warnings.append(f"skipped section outside project boundary: {child}")
                continue
            matches.setdefault(int(match.group(1)), []).append(resolved)

    sections: Dict[int, Path] = {}
    for index, paths in matches.items():
        ordered = sorted(paths, key=lambda item: item.name.casefold())
        sections[index] = ordered[0]
        if len(ordered) > 1:
            warnings.append(
                f"multiple section {index:02d} directories found; using {ordered[0].name}: "
                + ", ".join(item.name for item in ordered)
            )
    return sections


def _extract_binding_value(path: Path) -> Optional[str]:
    try:
        if path.suffix.casefold() == ".json":
            payload = _read_json_limited(path)
            if not isinstance(payload, dict):
                return None
            for key in ("bound_from", "kb_root", "knowledge_base", "project_kb"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        text = _read_text_limited(path, MAX_CONTROL_CARD_BYTES)
        match = re.search(
            r"(?im)^\s*(?:[-*]\s*)?(?:bound_from|kb_root|knowledge_base|project_kb)\s*:\s*(.+?)\s*$",
            text,
        )
        if match:
            return match.group(1).strip().strip("`\"'")
    except (OSError, UnicodeError, ProjectReaderError):
        return None
    return None


def _resolve_kb_root(
    project_root: Path,
    explicit_kb_root: Optional[PathLike],
    trusted_roots: Tuple[Path, ...],
    blockers: List[str],
    warnings: List[str],
) -> Tuple[Optional[Path], Optional[Path]]:
    binding_evidence: Optional[Path] = None
    raw: Optional[PathLike] = explicit_kb_root
    if raw is None:
        candidates = (
            project_root / "README_BINDING.md",
            project_root / ".novel-binding.json",
            project_root / "project_binding.json",
            project_root / "binding.json",
        )
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            value = _extract_binding_value(candidate)
            if value:
                raw = value
                binding_evidence = candidate.resolve(strict=True)
                break
    if raw is None:
        return None, binding_evidence

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = resolve_trusted_project_root(candidate, trusted_roots)
    except ProjectReaderError as exc:
        blockers.append(f"knowledge-base binding rejected: {exc}")
        return None, binding_evidence
    if resolved == project_root:
        warnings.append("knowledge-base binding resolves to the project root; treating it as local state")
    return resolved, binding_evidence


def _state_directories(root: Path, sections: Optional[Dict[int, Path]] = None) -> List[Path]:
    candidates = [root / "state", root / ".novel" / "state"]
    if sections:
        for index in (5, 9):
            section = sections.get(index)
            if section is not None:
                candidates.append(section / "state")
    result: List[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and not candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if _is_within(resolved, root) and resolved not in result:
                result.append(resolved)
    return result


def _find_project_statuses(
    project_root: Path,
    kb_root: Optional[Path],
    sections: Dict[int, Path],
    warnings: List[str],
) -> List[Path]:
    candidates: List[Path] = [project_root / "project_status.json"]
    for directory in _state_directories(project_root, sections):
        candidates.append(directory / "project_status.json")
    if kb_root is not None and kb_root != project_root:
        candidates.append(kb_root / "project_status.json")
        for directory in _state_directories(kb_root):
            candidates.append(directory / "project_status.json")

    found: List[Path] = []
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            owner = project_root if _is_within(resolved, project_root) else kb_root
            if owner is not None and _is_within(resolved, owner) and resolved not in found:
                found.append(resolved)
    if len(found) > 1:
        warnings.append(
            "multiple project_status.json files found; aggregating all status signals conservatively: "
            + ", ".join(str(item) for item in found)
        )
    return found


def _is_truth_state_file(path: Path) -> bool:
    name = path.name.casefold()
    if name == "project_status.json" or path.suffix.casefold() != ".json":
        return False
    return (
        name in TRUTH_STATE_NAMES
        or name.startswith("truth_")
        or "_truth_" in name
        or name.endswith("_truth.json")
        or name.startswith("真值")
        or "真相状态" in name
    )


def _collect_truth_state_files(
    project_root: Path,
    kb_root: Optional[Path],
    sections: Dict[int, Path],
    warnings: List[str],
) -> List[Path]:
    directories = _state_directories(project_root, sections)
    if kb_root is not None and kb_root != project_root:
        directories.extend(_state_directories(kb_root))
    result: List[Path] = []
    for directory in directories:
        try:
            files = list(_safe_walk_files(directory, warnings, max_files=1_000))
        except ProjectReaderError as exc:
            warnings.append(str(exc))
            continue
        for path in files:
            if _is_truth_state_file(path) and path not in result:
                result.append(path)
    ordered = sorted(result, key=lambda item: str(item).casefold())
    if len(ordered) > MAX_TRUTH_STATE_FILES:
        warnings.append(
            f"truth-state file limit reached; inspecting first {MAX_TRUTH_STATE_FILES} of {len(ordered)} files"
        )
        return ordered[:MAX_TRUTH_STATE_FILES]
    return ordered


def _artifact_kind(path: Path, *, allow_prose: bool = True) -> str:
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    prose_suffix = suffix in {".md", ".txt", ".docx"}
    if prose_suffix and any(marker in name for marker in CONTROL_CARD_MARKERS):
        return "control_card"
    if any(marker in name for marker in REVIEW_MARKERS):
        return "review"
    if suffix == ".json" and any(marker in name for marker in STATUS_MARKERS):
        return "chapter_status"
    if any(marker in name for marker in CONTEXT_DOCUMENT_MARKERS):
        return "context_document"
    if not allow_prose and suffix in {".json", ".md", ".txt", ".docx"}:
        return "context_document"
    if allow_prose and prose_suffix and any(marker in name for marker in FINAL_MARKERS):
        return "final_prose"
    if allow_prose and prose_suffix and any(marker in name for marker in DRAFT_MARKERS):
        return "draft_prose"
    if allow_prose and prose_suffix:
        return "prose"
    return "other"


def _chapter_entry(chapters: Dict[int, Dict[str, Any]], number: int) -> Dict[str, Any]:
    return chapters.setdefault(
        number,
        {
            "id": _canonical_chapter_id(number),
            "number": number,
            "synthetic": False,
            "artifacts": [],
            "status": "blocked",
            "stage": "unknown",
            "status_reasons": [],
            "blockers": [],
            "warnings": [],
        },
    )


def _add_chapter_artifacts(
    chapters: Dict[int, Dict[str, Any]],
    scan_root: Path,
    files: Iterable[Path],
    project_root: Path,
    kb_root: Optional[Path],
    *,
    allow_prose: bool,
) -> None:
    for path in files:
        number = _chapter_number_from_path(path, scan_root)
        if number is None:
            continue
        kind = _artifact_kind(path, allow_prose=allow_prose)
        if kind == "other":
            continue
        entry = _chapter_entry(chapters, number)
        item = _evidence(path, kind, project_root, kb_root)
        if not any(existing["path"] == item["path"] for existing in entry["artifacts"]):
            entry["artifacts"].append(item)


def _scan_chapters(
    project_root: Path,
    kb_root: Optional[Path],
    sections: Dict[int, Path],
    warnings: List[str],
) -> Dict[int, Dict[str, Any]]:
    chapters: Dict[int, Dict[str, Any]] = {}
    manuscript_root = sections.get(5)
    if manuscript_root is not None:
        try:
            files = list(_safe_walk_files(manuscript_root, warnings))
        except ProjectReaderError as exc:
            warnings.append(str(exc))
            files = []
        _add_chapter_artifacts(
            chapters,
            manuscript_root,
            files,
            project_root,
            kb_root,
            allow_prose=True,
        )

    roots = [project_root]
    if kb_root is not None and kb_root != project_root:
        roots.append(kb_root)
    for owner in roots:
        context_root = owner / "chapter_context"
        if not context_root.is_dir() or context_root.is_symlink():
            continue
        context_root = context_root.resolve(strict=True)
        if not _is_within(context_root, owner):
            warnings.append(f"skipped chapter_context outside owner boundary: {owner / 'chapter_context'}")
            continue
        files: List[Path] = []
        for candidate in context_root.iterdir():
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if _is_within(resolved, context_root):
                files.append(resolved)
        _add_chapter_artifacts(
            chapters,
            context_root,
            files,
            project_root,
            kb_root,
            allow_prose=False,
        )

    for state_root in _state_directories(project_root, sections) + (
        _state_directories(kb_root) if kb_root is not None and kb_root != project_root else []
    ):
        files = [
            candidate.resolve(strict=True)
            for candidate in state_root.iterdir()
            if candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix.casefold() == ".json"
            and _chapter_number(candidate.name) is not None
        ]
        _add_chapter_artifacts(
            chapters,
            state_root,
            files,
            project_root,
            kb_root,
            allow_prose=False,
        )

    for entry in chapters.values():
        entry["artifacts"].sort(
            key=lambda item: (
                0 if item["source"] == "project" else 1,
                item["kind"],
                item["relative_path"].casefold(),
            )
        )
    return chapters


def _collect_nested_strings(data: Any, keys: Sequence[str], *, limit: int = 64) -> List[str]:
    wanted = {key.casefold() for key in keys}
    values: List[str] = []

    def visit(value: Any) -> None:
        if len(values) >= limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().casefold() in wanted and isinstance(child, (str, int)):
                    text = str(child).strip()
                    if text and text not in values:
                        values.append(text)
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value[:1_000]:
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(data)
    return values


def _normalise_project_identity(value: str) -> str:
    text = Path(value.strip()).name.casefold()
    for suffix in (".kb", "_创作项目"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def _readiness_contract(
    data: Any,
    *,
    project_root: Path,
    kb_root: Optional[Path],
    chapter_number: int,
) -> Tuple[Optional[Dict[str, str]], List[str]]:
    errors: List[str] = []
    expected_projects = {_normalise_project_identity(project_root.name)}
    if kb_root is not None:
        expected_projects.add(_normalise_project_identity(kb_root.name))

    project_values = _collect_nested_strings(data, ("project_name", "project"))
    if not project_values:
        project_values = _collect_nested_strings(data, ("project_id",))
    normal_projects = {_normalise_project_identity(value) for value in project_values}
    if not normal_projects:
        errors.append("project identity is missing")
    elif not normal_projects.issubset(expected_projects):
        errors.append("project identity does not match the bound project")

    chapter_values = _collect_nested_strings(data, ("chapter_id",))
    chapter_numbers = {_chapter_number(value) for value in chapter_values}
    if not chapter_values:
        errors.append("chapter_id is missing")
    elif None in chapter_numbers or chapter_numbers != {chapter_number}:
        errors.append(f"chapter_id does not match {_canonical_chapter_id(chapter_number)}")

    run_values = _collect_nested_strings(data, ("run_id",))
    if len(run_values) != 1:
        errors.append("exactly one run_id is required")

    context_hashes = _collect_nested_strings(data, ("context_pack_sha256",))
    operation_hashes = _collect_nested_strings(data, ("operation_package_sha256",))
    if len(context_hashes) != 1 or not SHA256_RE.fullmatch(context_hashes[0]):
        errors.append("one valid context_pack_sha256 is required")
    if len(operation_hashes) != 1 or not SHA256_RE.fullmatch(operation_hashes[0]):
        errors.append("one valid operation_package_sha256 is required")

    if errors:
        return None, errors
    return {
        "project": next(iter(normal_projects)),
        "chapter_id": _canonical_chapter_id(chapter_number),
        "run_id": run_values[0],
        "context_pack_sha256": context_hashes[0].casefold(),
        "operation_package_sha256": operation_hashes[0].casefold(),
    }, []


def _project_status_chapter_ids(data: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    if not isinstance(data, dict):
        return None, None, None
    last_completed = _chapter_number(data.get("last_completed_chapter"))
    current: Optional[int] = None
    current_task = data.get("current_task")
    if isinstance(current_task, dict):
        current = _chapter_number(current_task.get("chapter_id") or current_task.get("chapter"))

    next_number = _chapter_number(data.get("next_chapter_id") or data.get("next_chapter"))
    interface = data.get("next_chapter_interface")
    if next_number is None and isinstance(interface, dict):
        next_number = _chapter_number(interface.get("chapter_id") or interface.get("chapter"))
        if next_number is None:
            for value in interface.values():
                next_number = _chapter_number(value)
                if next_number is not None:
                    break
    elif next_number is None and isinstance(interface, str):
        next_number = _chapter_number(interface)
    return last_completed, current, next_number


def _select_active_chapter(
    chapters: Dict[int, Dict[str, Any]],
    project_status: Any,
    requested_chapter_id: Optional[str],
) -> int:
    requested = _chapter_number(requested_chapter_id)
    if requested is not None:
        return requested

    last_completed, current, next_number = _project_status_chapter_ids(project_status)
    if next_number is not None:
        return next_number
    if current is not None and current != last_completed:
        return current

    unfinished: List[int] = []
    for number, entry in chapters.items():
        kinds = {item["kind"] for item in entry["artifacts"]}
        if "final_prose" not in kinds and (last_completed is None or number > last_completed):
            unfinished.append(number)
    if unfinished:
        return min(unfinished)
    if last_completed is not None:
        return last_completed + 1
    if chapters:
        return min(chapters)
    return 1


def _control_card_summary(path: Path) -> Dict[str, Any]:
    text = _read_text_limited(path, MAX_CONTROL_CARD_BYTES)
    title = ""
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()[:300]
        match = re.match(r"^[-*]\s*([^:：]{1,80})\s*[:：]\s*(.+)$", stripped)
        if match and len(fields) < 80:
            key = match.group(1).strip()
            value = match.group(2).strip()
            fields[key] = value[:1_000]
    return {
        "path": str(path),
        "title": title,
        "fields": fields,
        "bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _control_card_contract_errors(summary: Dict[str, Any]) -> List[str]:
    fields = summary.get("fields") if isinstance(summary.get("fields"), dict) else {}
    normal_keys = {
        str(key).strip().casefold().replace("-", "_").replace(" ", "_")
        for key, value in fields.items()
        if str(value).strip()
    }
    errors: List[str] = []
    if not str(summary.get("title") or "").strip() and not normal_keys:
        errors.append("chapter control card is empty")
    goal_markers = ("task_goal", "scene_goal", "chapter_goal", "本章目标", "章节目标", "任务目标")
    if not any(any(marker in key for marker in goal_markers) for key in normal_keys):
        errors.append("chapter control card is missing a task/scene goal")
    return errors


def _is_dedicated_status_document(path: Path) -> bool:
    if path.suffix.casefold() != ".json":
        return False
    name = path.name.casefold()
    return (
        name == "project_status.json"
        or _is_truth_state_file(path)
        or any(marker in name for marker in STATUS_MARKERS)
    )


def _signal_from_scalar(key: str, value: Any, trail: Sequence[str], path: Path) -> Optional[_StatusSignal]:
    normal_key = key.strip().lower().replace("-", "_").replace(" ", "_")
    trail_text = ".".join(trail + (key,))

    if normal_key in {"writing_blocking", "is_blocked", "blocked", "hard_blocking"}:
        if value is True:
            return _StatusSignal("blocked", trail_text, value, str(path), f"{trail_text}=true")
        return None
    if not trail and normal_key == "health" and _is_dedicated_status_document(path) and isinstance(value, bool):
        status = "ready" if value else "blocked"
        return _StatusSignal(status, trail_text, value, str(path), f"{trail_text}={str(value).lower()}")
    if normal_key in {"context_degraded", "truth_degraded", "route_degraded"} and value is True:
        return _StatusSignal("risk_draft_only", trail_text, value, str(path), f"{trail_text}=true")
    if normal_key in {
        "formal_draft_allowed",
        "prewrite_formal_draft_allowed",
        "full_draft_allowed",
    } and isinstance(value, bool):
        status = "ready" if value else "risk_draft_only"
        return _StatusSignal(status, trail_text, value, str(path), f"{trail_text}={str(value).lower()}")

    relevant_key = (
        not trail
        and normal_key in {"status", "health"}
        and _is_dedicated_status_document(path)
    ) or (
        normal_key == "status"
        and any(
            marker in component.casefold()
            for component in trail
            for marker in ("gate", "readiness", "prewrite", "writing", "route", "context", "truth")
        )
    ) or any(
        marker in normal_key
        for marker in (
            "gate_status",
            "prewrite_p0_status",
            "readiness",
            "route_health",
            "context_health",
            "state_freshness",
            "truth_health",
            "entity_consistency",
            "contamination_status",
            "allowed_output",
            "writing_gate",
        )
    )
    if not relevant_key or not isinstance(value, (str, int, float)):
        return None

    normal_value = _normalise_status(value)
    if normal_value in BLOCK_VALUES:
        return _StatusSignal("blocked", trail_text, value, str(path), f"{trail_text}={value}")
    if normal_value in RISK_VALUES:
        return _StatusSignal("risk_draft_only", trail_text, value, str(path), f"{trail_text}={value}")
    if normal_value in READY_VALUES:
        return _StatusSignal("ready", trail_text, value, str(path), f"{trail_text}={value}")
    return None


def _status_signals(data: Any, path: Path) -> List[_StatusSignal]:
    signals: List[_StatusSignal] = []

    def visit(value: Any, trail: Tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                normal_key = key_text.strip().lower().replace("-", "_").replace(" ", "_")
                trail_text = ".".join(trail + (key_text,))
                if normal_key in {"blocking_failure_signatures", "failure_signatures"}:
                    if isinstance(child, list) and child:
                        signals.append(
                            _StatusSignal(
                                "blocked",
                                trail_text,
                                f"{len(child)} item(s)",
                                str(path),
                                f"{trail_text} contains {len(child)} blocker(s)",
                            )
                        )
                elif normal_key in {"required_before_full_draft", "risk_gates"}:
                    if isinstance(child, list) and child:
                        signals.append(
                            _StatusSignal(
                                "risk_draft_only",
                                trail_text,
                                f"{len(child)} item(s)",
                                str(path),
                                f"{trail_text} contains {len(child)} unresolved item(s)",
                            )
                        )
                elif normal_key == "blocking_layer" and isinstance(child, str) and child.strip():
                    signals.append(
                        _StatusSignal(
                            "blocked",
                            trail_text,
                            child,
                            str(path),
                            f"{trail_text}={child}",
                        )
                    )
                scalar = _signal_from_scalar(key_text, child, trail, path)
                if scalar is not None:
                    signals.append(scalar)
                visit(child, trail + (key_text,))
        elif isinstance(value, list):
            for item in value[:1_000]:
                if isinstance(item, (dict, list)):
                    visit(item, trail)

    visit(data, ())
    unique: List[_StatusSignal] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for signal in signals:
        marker = (signal.status, signal.key, str(signal.value), signal.path)
        if marker not in seen:
            seen.add(marker)
            unique.append(signal)
    return unique


def _status_from_signals(signals: Sequence[_StatusSignal], default: str) -> str:
    statuses = {signal.status for signal in signals}
    if "blocked" in statuses:
        return "blocked"
    if "risk_draft_only" in statuses:
        return "risk_draft_only"
    if "ready" in statuses:
        return "ready"
    return default


def _status_artifact_rank(item: Dict[str, Any]) -> Tuple[int, int, str]:
    name = Path(item["path"]).name.casefold()
    latest_rank = 0 if "latest" in name else 1 if "ready" in name else 2 if "draft" not in name else 3
    kind_rank = 0 if "p0" in name or "kill_gate" in name else 1 if "readiness" in name else 2
    return latest_rank, kind_rank, name


def _is_formal_ready_signal(signal: _StatusSignal) -> bool:
    key = signal.key.casefold()
    path_name = Path(signal.path).name.casefold() if signal.path else ""
    return (
        any(marker in key for marker in ("gate", "readiness", "allowed_output", "formal_draft_allowed", "full_draft_allowed"))
        or (key in {"status", "health"} and any(marker in path_name for marker in ("gate", "readiness", "prewrite")))
    )


def _read_chapter_signals(
    entry: Dict[str, Any],
    max_artifacts: int,
    *,
    project_root: Path,
    kb_root: Optional[Path],
    chapter_number: int,
) -> Tuple[List[_StatusSignal], List[str], List[str], List[Dict[str, str]], int, int]:
    signals: List[_StatusSignal] = []
    errors: List[str] = []
    contract_warnings: List[str] = []
    contracts: List[Dict[str, str]] = []
    all_artifacts = sorted(
        (item for item in entry["artifacts"] if item["kind"] == "chapter_status"),
        key=_status_artifact_rank,
    )
    artifacts = all_artifacts[:max_artifacts]
    for item in artifacts:
        path = Path(item["path"])
        try:
            payload = _read_json_limited(path)
            item_signals = _status_signals(payload, path)
            non_ready = [signal for signal in item_signals if signal.status != "ready"]
            ready = [
                signal
                for signal in item_signals
                if signal.status == "ready" and _is_formal_ready_signal(signal)
            ]
            signals.extend(non_ready)
            if ready:
                contract, contract_errors = _readiness_contract(
                    payload,
                    project_root=project_root,
                    kb_root=kb_root,
                    chapter_number=chapter_number,
                )
                if contract_errors:
                    detail = f"{path}: readiness same-origin contract invalid: " + "; ".join(contract_errors)
                    contract_warnings.append(detail)
                    signals.append(
                        _StatusSignal(
                            "risk_draft_only",
                            "same_origin_contract",
                            "invalid",
                            str(path),
                            detail,
                        )
                    )
                elif contract is not None:
                    contracts.append(contract)
                    signals.extend(ready)
        except (OSError, UnicodeError, ProjectReaderError) as exc:
            errors.append(str(exc))
    contract_map = {
        (
            item["project"],
            item["chapter_id"],
            item["run_id"],
            item["context_pack_sha256"],
            item["operation_package_sha256"],
        ): item
        for item in contracts
    }
    if len(contract_map) > 1:
        detail = "ready chapter-status artifacts do not share one project/chapter/run/hash contract"
        contract_warnings.append(detail)
        signals.append(
            _StatusSignal(
                "risk_draft_only",
                "same_origin_contract",
                "conflict",
                "",
                detail,
            )
        )
    return signals, errors, contract_warnings, list(contract_map.values()), len(artifacts), len(all_artifacts)


def _compact_truth_summary(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    summary: Dict[str, Any] = {"top_level_keys": list(data.keys())[:80]}
    for key in (
        "schema_version",
        "project",
        "project_name",
        "enforced",
        "updated_from_chapter",
        "last_updated_from_chapter",
        "status",
        "health",
        "writing_blocking",
    ):
        value = data.get(key)
        if isinstance(value, (str, bool, int, float)) or value is None:
            if key in data:
                summary[key] = value
    return summary


def _dedupe_messages(messages: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for message in messages:
        if message and message not in seen:
            seen.add(message)
            result.append(message)
    return result


def inspect_project(
    root: PathLike,
    kb_root: Optional[PathLike] = None,
    chapter_id: Optional[str] = None,
    *,
    trusted_roots: Optional[Iterable[PathLike]] = None,
) -> Dict[str, Any]:
    """Inspect a novel project without executing or writing anything.

    When ``trusted_roots`` is omitted, only the resolved project directory is
    trusted.  This secure default permits local 01-09 state but rejects an
    external ``bound_from`` path.  Callers that support a separate KB library
    must pass both the creation-project root and the KB library root explicitly.
    """

    if trusted_roots is None:
        project_root = _resolve_directory(root, "project root")
        trusted = (project_root,)
    else:
        trusted = _resolve_trusted_roots(trusted_roots)
        project_root = resolve_trusted_project_root(root, trusted)

    warnings: List[str] = []
    blockers: List[str] = []
    status_reasons: List[str] = []

    sections = _discover_sections(project_root, warnings)
    if not sections:
        raise InvalidNovelProjectError(f"no 01-09 section directories found: {project_root}")
    missing_sections = [index for index in SECTION_LABELS if index not in sections]
    if missing_sections:
        blockers.append(
            "missing required 01-09 sections: "
            + ", ".join(f"{index:02d}_{SECTION_LABELS[index]}" for index in missing_sections)
        )

    resolved_kb_root, binding_evidence = _resolve_kb_root(
        project_root,
        kb_root,
        trusted,
        blockers,
        warnings,
    )
    project_status_paths = _find_project_statuses(project_root, resolved_kb_root, sections, warnings)
    project_status_data: Any = None
    project_status_signals: List[_StatusSignal] = []
    project_status_records: List[Dict[str, Any]] = []
    project_status_payloads: List[Any] = []
    if not project_status_paths:
        blockers.append("project_status.json is missing from project and trusted bound state")
    for index, path in enumerate(project_status_paths):
        record = _evidence(path, "project_status", project_root, resolved_kb_root)
        try:
            payload = _read_json_limited(path)
            if not isinstance(payload, dict):
                raise ProjectReaderError(f"project_status.json must contain an object: {path}")
            record["data"] = payload
            project_status_payloads.append(payload)
            project_status_signals.extend(_status_signals(payload, path))
            if index == 0:
                project_status_data = payload
        except (OSError, UnicodeError, ProjectReaderError) as exc:
            record["error"] = str(exc)
            blockers.append(str(exc))
        project_status_records.append(record)

    project_status_conflict = len({
        _project_status_chapter_ids(payload)
        for payload in project_status_payloads
    }) > 1
    if project_status_conflict:
        warnings.append("project_status.json files disagree on completed/current/next chapter state")
        status_reasons.append("project status copies disagree on chapter continuity")

    truth_files = _collect_truth_state_files(project_root, resolved_kb_root, sections, warnings)
    truth_records: List[Dict[str, Any]] = []
    truth_signals: List[_StatusSignal] = []
    truth_errors: List[str] = []
    for path in truth_files:
        record = _evidence(path, "truth_state", project_root, resolved_kb_root)
        try:
            payload = _read_json_limited(path)
            record["summary"] = _compact_truth_summary(payload)
            truth_signals.extend(_status_signals(payload, path))
        except (OSError, UnicodeError, ProjectReaderError) as exc:
            record["error"] = str(exc)
            truth_errors.append(str(exc))
        truth_records.append(record)
    if truth_errors:
        blockers.extend(truth_errors)
    if not truth_records:
        warnings.append("structured truth state is missing; formal readiness cannot be proven")
        status_reasons.append("structured truth state is missing")
    elif not any(signal.status == "ready" for signal in truth_signals):
        warnings.append("truth state has no affirmative ready/healthy signal; formal readiness cannot be proven")
        status_reasons.append("truth state does not affirm readiness")

    chapters = _scan_chapters(project_root, resolved_kb_root, sections, warnings)
    active_number = _select_active_chapter(chapters, project_status_data, chapter_id)
    active_entry = _chapter_entry(chapters, active_number)
    if not active_entry["artifacts"]:
        active_entry["synthetic"] = True

    last_completed, current_task, next_number = _project_status_chapter_ids(project_status_data)
    state_chapter_mismatch = False
    if current_task is not None and current_task != active_number:
        state_chapter_mismatch = True
        warnings.append(
            f"project_status current_task={_canonical_chapter_id(current_task)} does not match "
            f"active chapter={_canonical_chapter_id(active_number)}"
        )
        status_reasons.append("project status points at a different chapter")
    if current_task is not None and last_completed is not None and current_task == last_completed:
        state_chapter_mismatch = True
        warnings.append("project_status current_task still points at last_completed_chapter")
        status_reasons.append("project current task is stale relative to completed state")

    status_artifact_budget = MAX_STATUS_ARTIFACTS
    status_artifact_limit_warned = False
    chapter_order = [active_number] + [number for number in sorted(chapters) if number != active_number]
    for number in chapter_order:
        entry = chapters[number]
        control_items = [item for item in entry["artifacts"] if item["kind"] == "control_card"]
        final_items = [
            item for item in entry["artifacts"]
            if item["kind"] == "final_prose" and int(item.get("bytes") or 0) > 0
        ]
        prose_items = [
            item
            for item in entry["artifacts"]
            if item["kind"] in {"final_prose", "draft_prose", "prose"}
            and int(item.get("bytes") or 0) > 0
        ]
        (
            chapter_signals,
            chapter_errors,
            contract_warnings,
            readiness_contracts,
            artifacts_read,
            artifacts_available,
        ) = _read_chapter_signals(
            entry,
            status_artifact_budget,
            project_root=project_root,
            kb_root=resolved_kb_root,
            chapter_number=number,
        )
        status_artifact_budget -= artifacts_read
        if artifacts_available > artifacts_read and not status_artifact_limit_warned:
            warnings.append(
                f"chapter-status artifact limit reached in {_canonical_chapter_id(number)}; "
                f"read {artifacts_read} of {artifacts_available} prioritized files"
            )
            status_artifact_limit_warned = True
        entry["signals"] = [
            {
                "status": signal.status,
                "key": signal.key,
                "value": signal.value,
                "path": signal.path,
                "detail": signal.detail,
            }
            for signal in chapter_signals
        ]
        entry["readiness_contracts"] = readiness_contracts
        if chapter_errors:
            entry["blockers"].extend(chapter_errors)
        if contract_warnings:
            entry["warnings"].extend(contract_warnings)
            if number == active_number:
                warnings.extend(contract_warnings)

        control_card_errors: List[str] = []
        entry["control_card_path"] = control_items[0]["path"] if control_items else None
        entry["prose_paths"] = [item["path"] for item in prose_items]
        if number == active_number and control_items:
            try:
                entry["control_card"] = _control_card_summary(Path(control_items[0]["path"]))
                control_card_errors = _control_card_contract_errors(entry["control_card"])
            except (OSError, UnicodeError, ProjectReaderError) as exc:
                control_card_errors = [str(exc)]

        completed_by_state = last_completed is not None and number <= last_completed
        if final_items or (completed_by_state and prose_items):
            entry["stage"] = "final"
            entry["status"] = "ready"
            entry["status_reasons"].append("final prose or completed prose is present")
        elif not control_items:
            entry["stage"] = "unprepared" if not prose_items else "draft"
            entry["status"] = "blocked"
            entry["blockers"].append("chapter control card is missing")
            entry["status_reasons"].append("chapter control card is missing")
        elif control_card_errors:
            entry["stage"] = "prewrite"
            entry["status"] = "blocked"
            entry["blockers"].extend(control_card_errors)
            entry["status_reasons"].append("chapter control card is incomplete")
        elif chapter_errors:
            entry["stage"] = "prewrite"
            entry["status"] = "blocked"
            entry["status_reasons"].append("chapter status evidence is unreadable")
        else:
            entry["stage"] = "draft" if prose_items else "prewrite"
            entry["status"] = _status_from_signals(chapter_signals, "risk_draft_only")
            if entry["status"] == "ready":
                entry["status_reasons"].append("chapter-specific gate reports ready/pass")
            elif entry["status"] == "risk_draft_only":
                entry["status_reasons"].append(
                    "chapter gate permits only a risk draft or does not prove formal readiness"
                )
            else:
                entry["status_reasons"].append("chapter-specific gate reports a blocker")
        if contract_warnings and entry["status"] == "risk_draft_only":
            entry["status_reasons"].append("chapter readiness evidence lacks one same-origin contract")
        entry["blockers"] = _dedupe_messages(entry["blockers"])
        entry["warnings"] = _dedupe_messages(entry["warnings"])
        entry["status_reasons"] = _dedupe_messages(entry["status_reasons"])

    active_status = active_entry["status"]
    if active_entry["blockers"]:
        blockers.extend(active_entry["blockers"])

    project_hard_signals = [signal for signal in project_status_signals if signal.status == "blocked"]
    applicable_project_signals: List[_StatusSignal] = list(project_hard_signals)
    if current_task is None or current_task == active_number:
        applicable_project_signals = project_status_signals
    project_signal_status = _status_from_signals(applicable_project_signals, "risk_draft_only")
    truth_signal_status = _status_from_signals(truth_signals, "risk_draft_only")

    if project_hard_signals:
        blockers.extend(signal.detail for signal in project_hard_signals)
    if any(signal.status == "blocked" for signal in truth_signals):
        blockers.extend(signal.detail for signal in truth_signals if signal.status == "blocked")
    if any(signal.status == "risk_draft_only" for signal in truth_signals):
        status_reasons.append("truth state contains a risk/degraded signal")

    inspection_truncated = any("limit reached" in warning for warning in warnings)
    if inspection_truncated:
        status_reasons.append("inspection limits prevented a complete readiness proof")

    if blockers:
        project_status = "blocked"
    elif active_status == "blocked" or project_signal_status == "blocked" or truth_signal_status == "blocked":
        project_status = "blocked"
    elif (
        active_status == "risk_draft_only"
        or project_signal_status == "risk_draft_only"
        or truth_signal_status == "risk_draft_only"
        or state_chapter_mismatch
        or project_status_conflict
        or inspection_truncated
    ):
        project_status = "risk_draft_only"
    else:
        project_status = "ready"

    if active_status == "blocked":
        status_reasons.extend(active_entry["status_reasons"])
    elif active_status == "risk_draft_only":
        status_reasons.extend(active_entry["status_reasons"])
    elif project_status == "ready":
        status_reasons.append("status, truth state, control card, and chapter gate all prove readiness")

    section_records = []
    for index, label in SECTION_LABELS.items():
        path = sections.get(index)
        section_records.append(
            {
                "index": index,
                "label": label,
                "present": path is not None,
                "path": str(path) if path is not None else None,
                "directory_name": path.name if path is not None else None,
            }
        )

    evidence_paths: List[str] = []
    if binding_evidence is not None:
        evidence_paths.append(str(binding_evidence))
    evidence_paths.extend(str(path) for path in project_status_paths)
    evidence_paths.extend(record["path"] for record in truth_records)
    evidence_paths.extend(item["path"] for item in active_entry["artifacts"])
    evidence_paths = list(dict.fromkeys(evidence_paths))

    project_status_record = project_status_records[0] if project_status_records else None

    chapter_records = [chapters[number] for number in sorted(chapters)]
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "project": {
            "name": project_root.name,
            "path": str(project_root),
            "format": "01-09",
            "kb_root": str(resolved_kb_root) if resolved_kb_root is not None else None,
            "binding_evidence_path": str(binding_evidence) if binding_evidence is not None else None,
        },
        "status": project_status,
        "status_reasons": _dedupe_messages(status_reasons),
        "active_chapter_id": _canonical_chapter_id(active_number),
        "last_completed_chapter_id": (
            _canonical_chapter_id(last_completed) if last_completed is not None else None
        ),
        "next_chapter_id_from_state": (
            _canonical_chapter_id(next_number) if next_number is not None else None
        ),
        "sections": section_records,
        "missing_sections": [f"{index:02d}_{SECTION_LABELS[index]}" for index in missing_sections],
        "chapters": chapter_records,
        "state": {
            "project_status": project_status_record,
            "project_status_candidates": project_status_records,
            "truth_state": {
                "health": (
                    "blocked"
                    if truth_errors or any(signal.status == "blocked" for signal in truth_signals)
                    else truth_signal_status
                ),
                "files": truth_records,
            },
            "chapter_mismatch": state_chapter_mismatch,
        },
        "evidence_paths": evidence_paths,
        "blockers": _dedupe_messages(blockers),
        "warnings": _dedupe_messages(warnings),
    }


class NovelProjectReader:
    """Reusable reader with a fixed, explicit trusted-root policy."""

    def __init__(self, trusted_roots: Iterable[PathLike]) -> None:
        self.trusted_roots = _resolve_trusted_roots(trusted_roots)

    def resolve(self, raw: PathLike) -> Path:
        return resolve_trusted_project_root(raw, self.trusted_roots)

    def inspect(
        self,
        root: PathLike,
        kb_root: Optional[PathLike] = None,
        chapter_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return inspect_project(
            root,
            kb_root=kb_root,
            chapter_id=chapter_id,
            trusted_roots=self.trusted_roots,
        )
