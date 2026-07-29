from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


NORMATIVE_DOCS = (
    "docs/INDEX.md",
    "docs/PRODUCT_REQUIREMENTS.md",
    "docs/NON_FUNCTIONAL_REQUIREMENTS.md",
    "docs/REQUIREMENTS_TRACEABILITY.md",
    "docs/ROADMAP.md",
    "docs/ARCHITECTURE.md",
    "docs/DATA_MODEL.md",
    "docs/API_COMPATIBILITY.md",
    "docs/DESIGN_SYSTEM.md",
    "docs/SECURITY_AND_CAPABILITY_MODEL.md",
    "docs/TEST_AND_EVALUATION_STRATEGY.md",
    "docs/PACKAGING_AND_OPERATIONS.md",
    "docs/DEVELOPMENT_MULTI_AGENT_PLAYBOOK.md",
    "docs/FOUR_SKILLS_CAPABILITY_MAP.md",
    "docs/DEVELOPMENT_BRIEF.md",
    "docs/adr/README.md",
)

PRODUCT_REQUIREMENTS = "docs/PRODUCT_REQUIREMENTS.md"
NON_FUNCTIONAL_REQUIREMENTS = "docs/NON_FUNCTIONAL_REQUIREMENTS.md"
TRACEABILITY = "docs/REQUIREMENTS_TRACEABILITY.md"
DATABASE_SOURCE = "services/writing-runtime/src/zhimeng_runtime/database.py"
M3H_IMPLEMENTATION = "docs/M3H_POST_DRAFT_GATE_IMPLEMENTATION.md"
M3H_EVIDENCE = "evidence/post-draft-gate-validation-20260721.md"
M3H_SCREENSHOT = "evidence/post-draft-gate-desktop-1440x900.png"
M3I_IMPLEMENTATION = "docs/M3I_DURABLE_WORKFLOW_IMPLEMENTATION.md"
M3I_EVIDENCE = "evidence/durable-workflow-validation-20260721.md"
M3I_SCREENSHOTS = (
    "evidence/durable-workflow-resume-desktop-1440x900.png",
    "evidence/durable-workflow-resume-mobile-390x844.png",
)

IMPLEMENTATION_STATUSES = frozenset(
    {
        "verified_current",
        "partial_current",
        "not_current",
        "future_horizon",
        "contradicted",
    }
)
MEASUREMENT_STATUSES = frozenset(
    {
        "measured_current",
        "partial_current_evidence",
        "not_measured",
        "not_current",
    }
)

STALE_PATTERNS = (
    (re.compile(r"current schema v4|当前 schema v4", re.IGNORECASE), "schema v4 current-state claim"),
    (re.compile(r"Activity 写入仍待实现", re.IGNORECASE), "Activity pending claim"),
    (re.compile(r"Activity SSE.{0,40}待实现|待实现.{0,40}Activity SSE", re.IGNORECASE), "Activity SSE pending claim"),
    (re.compile(r"Agent.{0,30}运行活动仍是占位", re.IGNORECASE), "Agent/Activity placeholder claim"),
    (re.compile(r"下一步把合同接到 SQLite.{0,80}SSE", re.IGNORECASE), "execution contract wiring claim"),
)

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
INLINE_CODE = re.compile(r"`([^`\r\n]+)`")

DOMAIN = r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*"
FR_ID_TEXT = rf"FR-{DOMAIN}-\d{{3}}"
NFR_ID_TEXT = rf"NFR-{DOMAIN}-\d{{3}}"
AC_FR_ID_TEXT = rf"AC-FR-{DOMAIN}-\d{{3}}-\d{{2}}"
AC_NFR_ID_TEXT = rf"AC-NFR-{DOMAIN}-\d{{3}}-\d{{2}}"
AC_E2E_ID_TEXT = r"AC-E2E-\d{3}"
STABLE_ID = re.compile(
    rf"(?<![A-Z0-9-])(?:{AC_FR_ID_TEXT}|{AC_NFR_ID_TEXT}|{AC_E2E_ID_TEXT}|{FR_ID_TEXT}|{NFR_ID_TEXT})(?![A-Z0-9-])"
)
REQUIREMENT_DEFINITION = re.compile(
    rf"(?m)^#{{3,6}}\s+`(?P<id>{FR_ID_TEXT}|{NFR_ID_TEXT})`(?:\s|$)"
)
ACCEPTANCE_DEFINITION = re.compile(
    rf"(?m)^\s*-\s+`(?P<id>{AC_FR_ID_TEXT}|{AC_NFR_ID_TEXT})`[：:]"
)
E2E_DEFINITION = re.compile(
    rf"(?m)^\|\s*`(?P<id>{AC_E2E_ID_TEXT})`\s*\|"
)
CURRENT_MEASUREMENT_STATUS = re.compile(
    r"(?m)^\s*-\s*当前状态：`(?P<status>[a-z_]+)`"
)
TRACE_STATUS = re.compile(r"^\s*`(?P<status>[a-z_]+)`(?:\s*[：:]|\s*$)")

SOURCE_ROOTS = ("apps", "services", "packages", "scripts", "tests")
EXCLUDED_SOURCE_DIRS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".runtime",
        ".venv",
        "__pycache__",
        "dist",
        "node_modules",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)


@dataclass(frozen=True)
class Definition:
    identifier: str
    source: str
    line: int
    start: int
    end: int


@dataclass(frozen=True)
class TableRow:
    line: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class SourceIndex:
    files: tuple[str, ...]
    by_name: Mapping[str, tuple[str, ...]]


def _read(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read UTF-8 file {path}: {exc}")
        return ""


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _local_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().strip("<>")
    if not target or target.startswith(("#", "http://", "https://", "mailto:", "codex:")):
        return None
    target = target.split("#", 1)[0]
    if not target or re.match(r"^[A-Za-z]:[\\/]", target):
        return None
    return (source.parent / target).resolve()


def _split_markdown_row(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def _is_separator_row(cells: tuple[str, ...]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _table_rows(
    text: str,
    *,
    first_header: str,
    source: str,
    errors: list[str],
) -> list[TableRow]:
    lines = text.splitlines()
    matches: list[int] = []
    for index, line in enumerate(lines):
        cells = _split_markdown_row(line)
        if cells and cells[0] == first_header:
            matches.append(index)
    if len(matches) != 1:
        errors.append(
            f"{source} must contain exactly one '{first_header}' table, found {len(matches)}"
        )
        return []

    header_index = matches[0]
    if header_index + 1 >= len(lines):
        errors.append(f"{source}:{header_index + 1} table has no separator row")
        return []
    separator = _split_markdown_row(lines[header_index + 1])
    if separator is None or not _is_separator_row(separator):
        errors.append(f"{source}:{header_index + 2} table separator is invalid")
        return []

    rows: list[TableRow] = []
    for index in range(header_index + 2, len(lines)):
        cells = _split_markdown_row(lines[index])
        if cells is None:
            break
        if _is_separator_row(cells):
            errors.append(f"{source}:{index + 1} contains an unexpected table separator")
            continue
        rows.append(TableRow(line=index + 1, cells=cells))
    return rows


def _definitions(
    texts: Mapping[str, str],
    errors: list[str],
) -> dict[str, set[str]]:
    occurrences: dict[str, list[Definition]] = defaultdict(list)
    for source in (PRODUCT_REQUIREMENTS, NON_FUNCTIONAL_REQUIREMENTS):
        text = texts.get(source, "")
        for pattern in (
            REQUIREMENT_DEFINITION,
            ACCEPTANCE_DEFINITION,
            E2E_DEFINITION,
        ):
            for match in pattern.finditer(text):
                identifier = match.group("id")
                occurrences[identifier].append(
                    Definition(
                        identifier=identifier,
                        source=source,
                        line=_line_number(text, match.start()),
                        start=match.start(),
                        end=match.end(),
                    )
                )

    for identifier, definitions in sorted(occurrences.items()):
        if len(definitions) > 1:
            locations = ", ".join(
                f"{definition.source}:{definition.line}"
                for definition in definitions
            )
            errors.append(f"duplicate stable ID definition: {identifier} ({locations})")
        expected_source = (
            NON_FUNCTIONAL_REQUIREMENTS
            if identifier.startswith(("NFR-", "AC-NFR-"))
            else PRODUCT_REQUIREMENTS
        )
        for definition in definitions:
            if definition.source != expected_source:
                errors.append(
                    f"stable ID defined in wrong document: {identifier} at "
                    f"{definition.source}:{definition.line}; expected {expected_source}"
                )

    defined = set(occurrences)
    requirement_mentions = set(
        STABLE_ID.findall(
            texts.get(PRODUCT_REQUIREMENTS, "")
            + "\n"
            + texts.get(NON_FUNCTIONAL_REQUIREMENTS, "")
        )
    )
    for identifier in sorted(requirement_mentions - defined):
        errors.append(f"stable ID is mentioned but never defined: {identifier}")

    groups = {
        "fr": {identifier for identifier in defined if identifier.startswith("FR-")},
        "nfr": {identifier for identifier in defined if identifier.startswith("NFR-")},
        "ac_fr": {identifier for identifier in defined if identifier.startswith("AC-FR-")},
        "ac_nfr": {identifier for identifier in defined if identifier.startswith("AC-NFR-")},
        "ac_e2e": {identifier for identifier in defined if identifier.startswith("AC-E2E-")},
    }

    for acceptance_group, requirement_group in (("ac_fr", "fr"), ("ac_nfr", "nfr")):
        acceptances_by_parent: dict[str, list[str]] = defaultdict(list)
        for acceptance in groups[acceptance_group]:
            parent = acceptance.removeprefix("AC-").rsplit("-", 1)[0]
            acceptances_by_parent[parent].append(acceptance)
            if parent not in groups[requirement_group]:
                errors.append(
                    f"acceptance criterion has no defined parent requirement: {acceptance} -> {parent}"
                )
        for requirement in sorted(groups[requirement_group]):
            if requirement not in acceptances_by_parent:
                errors.append(f"requirement has no acceptance criteria: {requirement}")

    return groups


def _documented_statuses(
    text: str,
    *,
    source: str,
    expected: frozenset[str],
    errors: list[str],
) -> None:
    rows = _table_rows(
        text,
        first_header="状态",
        source=source,
        errors=errors,
    )
    documented: set[str] = set()
    for row in rows:
        if not row.cells:
            continue
        match = re.fullmatch(r"`([a-z_]+)`", row.cells[0])
        if match is None:
            errors.append(f"{source}:{row.line} status name must be a single code literal")
            continue
        documented.add(match.group(1))
    if documented != expected:
        missing = sorted(expected - documented)
        extra = sorted(documented - expected)
        errors.append(
            f"{source} status dictionary drift: missing={missing}, extra={extra}"
        )


def _validate_nfr_measurement_statuses(text: str, errors: list[str]) -> None:
    definitions = [
        match
        for match in REQUIREMENT_DEFINITION.finditer(text)
        if match.group("id").startswith("NFR-")
    ]
    for index, definition in enumerate(definitions):
        end = definitions[index + 1].start() if index + 1 < len(definitions) else len(text)
        block = text[definition.end():end]
        statuses = list(CURRENT_MEASUREMENT_STATUS.finditer(block))
        identifier = definition.group("id")
        line = _line_number(text, definition.start())
        if len(statuses) != 1:
            errors.append(
                f"{NON_FUNCTIONAL_REQUIREMENTS}:{line} {identifier} must declare exactly one current measurement status, found {len(statuses)}"
            )
            continue
        status = statuses[0].group("status")
        if status not in MEASUREMENT_STATUSES:
            errors.append(
                f"invalid NFR measurement status for {identifier}: {status}"
            )


def _build_source_index(root: Path) -> SourceIndex:
    files: list[str] = []
    by_name: dict[str, list[str]] = defaultdict(list)
    for relative_root in SOURCE_ROOTS:
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in EXCLUDED_SOURCE_DIRS
            ]
            directory_path = Path(directory)
            for filename in filenames:
                path = directory_path / filename
                relative = path.relative_to(root).as_posix()
                files.append(relative)
                by_name[filename.casefold()].append(relative)
    return SourceIndex(
        files=tuple(sorted(files)),
        by_name={
            name: tuple(sorted(paths))
            for name, paths in by_name.items()
        },
    )


def _looks_like_source_locator(raw: str) -> bool:
    candidate = raw.strip().replace("\\", "/")
    if not candidate or candidate == "-":
        return False
    name = candidate.rsplit("/", 1)[-1]
    suffix = Path(name.replace("*", "x").replace("?", "x")).suffix.casefold()
    if suffix in SOURCE_SUFFIXES:
        return True
    return any(character in candidate for character in "*?[") and "/" in candidate


def _resolve_source_locator(
    locator: str,
    *,
    root: Path,
    index: SourceIndex,
    source: str,
    line: int,
    errors: list[str],
) -> None:
    normalized = locator.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
        errors.append(f"{source}:{line} source locator must be repository-relative: {locator}")
        return
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts:
        errors.append(f"{source}:{line} source locator escapes repository root: {locator}")
        return

    has_glob = any(character in normalized for character in "*?[")
    if "/" in normalized:
        pattern = "/".join(parts)
        matches = [
            path
            for path in index.files
            if fnmatch.fnmatchcase(path.casefold(), pattern.casefold())
        ]
    elif has_glob:
        matches = [
            path
            for path in index.files
            if fnmatch.fnmatchcase(Path(path).name.casefold(), normalized.casefold())
        ]
    else:
        matches = list(index.by_name.get(normalized.casefold(), ()))

    if not matches:
        errors.append(f"{source}:{line} unresolved source locator: {locator}")
    elif not has_glob and "/" not in normalized and len(matches) > 1:
        errors.append(
            f"{source}:{line} ambiguous bare source locator: {locator} -> {matches}"
        )


def _validate_traceability(
    root: Path,
    text: str,
    definitions: Mapping[str, set[str]],
    errors: list[str],
) -> None:
    specs = (
        (
            "functional",
            "Requirement / AC",
            5,
            (1, 2, 3),
            ((4, IMPLEMENTATION_STATUSES, "implementation"),),
        ),
        (
            "e2e",
            "Acceptance",
            3,
            (1,),
            ((2, IMPLEMENTATION_STATUSES, "implementation"),),
        ),
        (
            "nfr",
            "NFR / AC",
            5,
            (1, 2),
            (
                (3, IMPLEMENTATION_STATUSES, "implementation"),
                (4, MEASUREMENT_STATUSES, "measurement"),
            ),
        ),
    )
    expected_by_kind = {
        "functional": definitions["fr"] | definitions["ac_fr"],
        "e2e": definitions["ac_e2e"],
        "nfr": definitions["nfr"] | definitions["ac_nfr"],
    }
    traced_by_kind: dict[str, Counter[str]] = {
        kind: Counter() for kind in expected_by_kind
    }
    source_index = _build_source_index(root)

    for kind, first_header, expected_cells, locator_cells, status_cells in specs:
        rows = _table_rows(
            text,
            first_header=first_header,
            source=TRACEABILITY,
            errors=errors,
        )
        for row in rows:
            if len(row.cells) != expected_cells:
                errors.append(
                    f"{TRACEABILITY}:{row.line} expected {expected_cells} table cells, found {len(row.cells)}"
                )
                continue
            identifiers = STABLE_ID.findall(row.cells[0])
            for identifier in identifiers:
                traced_by_kind[kind][identifier] += 1

            if kind == "functional":
                primary = [value for value in identifiers if value.startswith("FR-")]
                acceptances = [value for value in identifiers if value.startswith("AC-FR-")]
                if len(primary) != 1:
                    errors.append(
                        f"{TRACEABILITY}:{row.line} functional row must contain exactly one FR ID"
                    )
                elif any(
                    acceptance.removeprefix("AC-").rsplit("-", 1)[0] != primary[0]
                    for acceptance in acceptances
                ):
                    errors.append(
                        f"{TRACEABILITY}:{row.line} functional AC does not belong to {primary[0]}"
                    )
            elif kind == "nfr":
                primary = [value for value in identifiers if value.startswith("NFR-")]
                acceptances = [value for value in identifiers if value.startswith("AC-NFR-")]
                if len(primary) != 1:
                    errors.append(
                        f"{TRACEABILITY}:{row.line} NFR row must contain exactly one NFR ID"
                    )
                elif any(
                    acceptance.removeprefix("AC-").rsplit("-", 1)[0] != primary[0]
                    for acceptance in acceptances
                ):
                    errors.append(
                        f"{TRACEABILITY}:{row.line} NFR AC does not belong to {primary[0]}"
                    )
            elif len(identifiers) != 1 or not identifiers[0].startswith("AC-E2E-"):
                errors.append(
                    f"{TRACEABILITY}:{row.line} E2E row must contain exactly one AC-E2E ID"
                )

            for status_index, allowed_statuses, axis in status_cells:
                status_match = TRACE_STATUS.match(row.cells[status_index])
                if status_match is None:
                    errors.append(
                        f"{TRACEABILITY}:{row.line} traceability row must start "
                        f"with a coded {axis} status"
                    )
                    continue
                status = status_match.group("status")
                if status not in allowed_statuses:
                    label = (
                        "traceability status"
                        if axis == "implementation"
                        else "traceability measurement status"
                    )
                    errors.append(
                        f"{TRACEABILITY}:{row.line} invalid {label}: {status}"
                    )

            for cell_index in locator_cells:
                for locator in INLINE_CODE.findall(row.cells[cell_index]):
                    if not _looks_like_source_locator(locator):
                        continue
                    _resolve_source_locator(
                        locator,
                        root=root,
                        index=source_index,
                        source=TRACEABILITY,
                        line=row.line,
                        errors=errors,
                    )

    for kind, expected in expected_by_kind.items():
        traced = set(traced_by_kind[kind])
        for identifier in sorted(expected - traced):
            errors.append(f"stable ID missing from {kind} traceability table: {identifier}")
        for identifier in sorted(traced - expected):
            errors.append(f"undefined stable ID in {kind} traceability table: {identifier}")
        for identifier, count in sorted(traced_by_kind[kind].items()):
            if count > 1:
                errors.append(
                    f"stable ID appears more than once in {kind} traceability table: {identifier} ({count})"
                )


def _sqlite_source_truth(root: Path, errors: list[str]) -> tuple[int, int] | None:
    source = root / DATABASE_SOURCE
    text = _read(source, errors)
    if not text:
        return None
    try:
        module = ast.parse(text, filename=DATABASE_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {DATABASE_SOURCE}: {exc}")
        return None

    version: int | None = None
    required_tables: set[str] | None = None
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "SCHEMA_VERSION":
            try:
                candidate = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                candidate = None
            if isinstance(candidate, int):
                version = candidate
        elif target.id == "REQUIRED_TABLES":
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
                and len(value.args) == 1
            ):
                try:
                    candidate = ast.literal_eval(value.args[0])
                except (ValueError, TypeError):
                    candidate = None
                if isinstance(candidate, set) and all(
                    isinstance(item, str) for item in candidate
                ):
                    required_tables = candidate

    if version is None:
        errors.append(f"{DATABASE_SOURCE} has no literal SCHEMA_VERSION")
    if required_tables is None:
        errors.append(f"{DATABASE_SOURCE} has no literal REQUIRED_TABLES set")
    if version is None or required_tables is None:
        return None
    return version, len(required_tables)


def _require_current_claim(
    texts: Mapping[str, str],
    source: str,
    claim: str,
    errors: list[str],
) -> None:
    if claim not in texts.get(source, ""):
        errors.append(f"{source} is missing current-state claim: {claim}")


def _validate_current_m3h_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    source_truth = _sqlite_source_truth(root, errors)
    if source_truth is None:
        return
    schema_version, table_count = source_truth

    expected_claims = {
        "README.md": f"SQLite schema v{schema_version} 共 {table_count} 张表",
        "docs/ROADMAP.md": f"SQLite v{schema_version} Session/Run/Activity/Message",
        "docs/DATA_MODEL.md": f"Writing Runtime SQLite `schema v{schema_version}`",
        "docs/ARCHITECTURE.md": (
            f"当前 schema v{schema_version} 已实际建立 {table_count} 张表"
        ),
        "services/writing-runtime/README.md": (
            f"SQLite schema v{schema_version} persists the Pack"
        ),
    }
    for source, claim in expected_claims.items():
        _require_current_claim(texts, source, claim, errors)
    _require_current_claim(
        texts,
        "docs/DATA_MODEL.md",
        f"### 3.2 当前 v{schema_version} Mermaid ER",
        errors,
    )
    _require_current_claim(
        texts,
        "docs/DATA_MODEL.md",
        f"下图对应当前 {table_count} 张表",
        errors,
    )

    roadmap = texts.get("docs/ROADMAP.md", "")
    if not re.search(r"\|\s*M3\s*\|[^\n]*\|\s*in_progress\s*\|", roadmap):
        errors.append("ROADMAP must keep M3 as the current in_progress milestone")

    required_references = {
        "README.md": (M3H_IMPLEMENTATION, M3H_EVIDENCE),
        "docs/INDEX.md": (
            Path(M3H_IMPLEMENTATION).name,
            "VAL-M3H-20260721",
        ),
        "evidence/README.md": (
            Path(M3H_EVIDENCE).name,
            Path(M3H_SCREENSHOT).name,
        ),
    }
    for source, references in required_references.items():
        for reference in references:
            _require_current_claim(texts, source, reference, errors)

    implementation = texts.get(M3H_IMPLEMENTATION, "")
    evidence = texts.get(M3H_EVIDENCE, "")
    for source, text in (
        (M3H_IMPLEMENTATION, implementation),
        (M3H_EVIDENCE, evidence),
    ):
        for boundary in (
            "quality_assessed=false",
            "formal_draft_allowed=false",
        ):
            if boundary not in text:
                errors.append(f"{source} is missing M3h boundary: {boundary}")
    if "Evidence ID: `VAL-M3H-20260721`" not in evidence:
        errors.append(f"{M3H_EVIDENCE} has the wrong or missing Evidence ID")

    screenshot = root / M3H_SCREENSHOT
    try:
        screenshot_bytes = screenshot.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read M3h screenshot {screenshot}: {exc}")
        return
    if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        errors.append(f"{M3H_SCREENSHOT} is not a PNG")
    screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest().upper()
    if screenshot_hash not in evidence:
        errors.append(
            f"{M3H_EVIDENCE} does not bind current screenshot SHA-256 {screenshot_hash}"
        )


def _validate_current_m3i_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    required_references = {
        "README.md": (M3I_IMPLEMENTATION, M3I_EVIDENCE),
        "docs/INDEX.md": (
            Path(M3I_IMPLEMENTATION).name,
            "VAL-M3I-20260721",
        ),
        "evidence/README.md": (
            Path(M3I_EVIDENCE).name,
            *(Path(screenshot).name for screenshot in M3I_SCREENSHOTS),
        ),
    }
    for source, references in required_references.items():
        for reference in references:
            _require_current_claim(texts, source, reference, errors)

    implementation = texts.get(M3I_IMPLEMENTATION, "")
    evidence = texts.get(M3I_EVIDENCE, "")
    for source, text in (
        (M3I_IMPLEMENTATION, implementation),
        (M3I_EVIDENCE, evidence),
    ):
        if "formal_draft_allowed=false" not in text:
            errors.append(
                f"{source} is missing M3i boundary: formal_draft_allowed=false"
            )
    if "at-most-once" not in implementation:
        errors.append(f"{M3I_IMPLEMENTATION} is missing M3i at-most-once boundary")
    if "Evidence ID: `VAL-M3I-20260721`" not in evidence:
        errors.append(f"{M3I_EVIDENCE} has the wrong or missing Evidence ID")

    for relative in M3I_SCREENSHOTS:
        screenshot = root / relative
        try:
            screenshot_bytes = screenshot.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read M3i screenshot {screenshot}: {exc}")
            continue
        if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            errors.append(f"{relative} is not a PNG")
        screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest().upper()
        if screenshot_hash not in evidence:
            errors.append(
                f"{M3I_EVIDENCE} does not bind current screenshot SHA-256 "
                f"{screenshot_hash}"
            )


def validate_repository(
    root: Path,
    *,
    text_overrides: Mapping[str, str] | None = None,
) -> list[str]:
    root = root.resolve()
    overrides = dict(text_overrides or {})
    errors: list[str] = []
    texts: dict[str, str] = {}

    for relative in NORMATIVE_DOCS:
        path = root / relative
        if not path.is_file():
            errors.append(f"missing normative document: {relative}")
            continue
        text = overrides.get(relative)
        if text is None:
            text = _read(path, errors)
        texts[relative] = text
        if len(text.strip()) < 80:
            errors.append(f"normative document is unexpectedly empty: {relative}")

    readme = root / "README.md"
    if readme.is_file():
        texts["README.md"] = overrides.get("README.md", _read(readme, errors))
    else:
        errors.append("missing README.md")

    for relative in (
        "services/writing-runtime/README.md",
        M3H_IMPLEMENTATION,
        M3H_EVIDENCE,
        M3I_IMPLEMENTATION,
        M3I_EVIDENCE,
        "evidence/README.md",
    ):
        path = root / relative
        if not path.is_file():
            errors.append(f"missing current-state document: {relative}")
            texts[relative] = ""
            continue
        texts[relative] = overrides.get(relative, _read(path, errors))

    for relative, text in texts.items():
        source = root / relative
        for match in MARKDOWN_LINK.finditer(text):
            target = _local_link_target(source, match.group(1))
            if target is not None and not target.exists():
                errors.append(f"broken local link in {relative}: {match.group(1)}")

    canonical_text = "\n".join(texts.values())
    for pattern, label in STALE_PATTERNS:
        if pattern.search(canonical_text):
            errors.append(f"stale canonical statement detected: {label}")

    definitions = _definitions(texts, errors)
    _documented_statuses(
        texts.get(TRACEABILITY, ""),
        source=TRACEABILITY,
        expected=IMPLEMENTATION_STATUSES,
        errors=errors,
    )
    _documented_statuses(
        texts.get(NON_FUNCTIONAL_REQUIREMENTS, ""),
        source=NON_FUNCTIONAL_REQUIREMENTS,
        expected=MEASUREMENT_STATUSES,
        errors=errors,
    )
    _validate_nfr_measurement_statuses(
        texts.get(NON_FUNCTIONAL_REQUIREMENTS, ""),
        errors,
    )
    _validate_traceability(
        root,
        texts.get(TRACEABILITY, ""),
        definitions,
        errors,
    )
    _validate_current_m3h_state(root, texts, errors)
    _validate_current_m3i_state(root, texts, errors)

    roadmap = texts.get("docs/ROADMAP.md", "")
    in_progress_rows = len(re.findall(r"\|\s*in_progress\s*\|", roadmap))
    if in_progress_rows != 1:
        errors.append(
            f"ROADMAP must contain exactly one in_progress milestone, found {in_progress_rows}"
        )

    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = validate_repository(root)
    if errors:
        for error in errors:
            print(f"LIFECYCLE_DOC_ERROR: {error}")
        return 1
    print("LIFECYCLE_DOCS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
