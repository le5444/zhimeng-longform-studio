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
CONTEXT_MODELS_SOURCE = "services/writing-runtime/src/zhimeng_runtime/context_models.py"
CONTEXT_SERVICE_SOURCE = "services/writing-runtime/src/zhimeng_runtime/context_service.py"
AGENT_SERVICE_SOURCE = "services/writing-runtime/src/zhimeng_runtime/agent_service.py"
CONTEXT_REVIEW_SOURCE = "apps/workbench/src/components/ContextPackReview.tsx"
KNOWLEDGE_REVIEW_SOURCE = "apps/workbench/src/components/KnowledgeReviewPanel.tsx"
CONTRACTS_README = "packages/contracts/README.md"
M3H_IMPLEMENTATION = "docs/M3H_POST_DRAFT_GATE_IMPLEMENTATION.md"
M3H_EVIDENCE = "evidence/post-draft-gate-validation-20260721.md"
M3H_SCREENSHOT = "evidence/post-draft-gate-desktop-1440x900.png"
M3I_IMPLEMENTATION = "docs/M3I_DURABLE_WORKFLOW_IMPLEMENTATION.md"
M3I_EVIDENCE = "evidence/durable-workflow-validation-20260721.md"
M3I_SCREENSHOTS = (
    "evidence/durable-workflow-resume-desktop-1440x900.png",
    "evidence/durable-workflow-resume-mobile-390x844.png",
)
M3J_IMPLEMENTATION = "docs/M3J_CREATION_HANDOFF_IMPLEMENTATION.md"
M3J_EVIDENCE = "evidence/creation-handoff-validation-20260722.md"
M3K_IMPLEMENTATION = "docs/M3K_AGENT_CANDIDATE_RECOVERY_IMPLEMENTATION.md"
M3K_EVIDENCE = "evidence/agent-candidate-recovery-validation-20260723.md"
M3K_SCREENSHOTS = (
    "evidence/provider-recovery-decision-desktop-1440x900.png",
    "evidence/provider-recovery-decision-mobile-390x844.png",
)
M3K_CONTRACTS = (
    "packages/contracts/agent-candidate-output.schema.json",
    "packages/contracts/agent-candidate-output-v2.schema.json",
    "packages/contracts/agent-candidate-output-v1.schema.json",
    "packages/contracts/creation-instruction-activation.schema.json",
    "packages/contracts/creation-instruction-materialization.schema.json",
    "packages/contracts/creation-instruction-dispatch-receipt.schema.json",
    "packages/contracts/writer-task.schema.json",
    "packages/contracts/writer-candidate-result.schema.json",
    "packages/contracts/provider-turn-receipt.schema.json",
    "packages/contracts/provider-turn-receipt-list.schema.json",
    "packages/contracts/provider-recovery.schema.json",
    "packages/contracts/provider-recovery-dossier.schema.json",
    "packages/contracts/provider-recovery-decision.schema.json",
    "packages/contracts/provider-recovery-decision-request.schema.json",
)
M3L_IMPLEMENTATION = "docs/M3L_MANAGED_SKILL_LIFECYCLE_IMPLEMENTATION.md"
M3L_EVIDENCE = "evidence/managed-skill-lifecycle-validation-20260722.md"
M3L_CONTRACTS = (
    "packages/contracts/managed-skill-catalog.schema.json",
    "packages/contracts/managed-skill-lifecycle-catalog.schema.json",
    "packages/contracts/managed-skill-import-request.schema.json",
    "packages/contracts/managed-skill-import.schema.json",
    "packages/contracts/managed-skill-activation-request.schema.json",
    "packages/contracts/managed-skill-activation.schema.json",
)
NLCL_MODELS_SOURCE = "services/writing-runtime/src/zhimeng_runtime/nlcl_models.py"
AGENT_API_SOURCE = "services/writing-runtime/src/zhimeng_runtime/agent_api.py"
AGENT_CANDIDATE_MODELS_SOURCE = (
    "services/writing-runtime/src/zhimeng_runtime/agent_candidate_models.py"
)
KNOWLEDGE_SERVICE_SOURCE = (
    "services/writing-runtime/src/zhimeng_runtime/knowledge_service.py"
)
CANONICAL_WORKFLOW_CHAIN = "project-audit -> prewrite-gate -> creation-handoff"
CANONICAL_WORKFLOW_STEPS = (
    (
        "novel-creation-suite.project-audit",
        "zhimeng.creation-project-audit-request.v1",
    ),
    (
        "novel-creation-suite.prewrite-gate",
        "zhimeng.creation-prewrite-gate-request.v1",
    ),
    (
        "novel-creation-suite.creation-handoff",
        "zhimeng.creation-handoff-request.v1",
    ),
)
CURRENT_CANDIDATE_SCHEMA = "zhimeng.agent-candidate-output.v3"
LEGACY_CANDIDATE_SCHEMAS = (
    "zhimeng.agent-candidate-output.v2",
    "zhimeng.agent-candidate-output.v1",
)
PROVIDER_TURN_RECEIPTS_PATH = (
    "/api/v1/agent/runs/{run_id}/provider-turn-receipts"
)
WRITER_TRACE_PATH = "/api/v1/agent/runs/{run_id}/writer-trace"
SUPPORTED_NATIVE_KNOWLEDGE_TYPES = frozenset({"work", "voice", "validator"})
PROJECT_KB_BINDING_FIELDS = frozenset(
    {"project_kb_state_id", "project_kb_manifest_hash"}
)
PROJECT_KB_BINDING_TABLES = frozenset(
    {"context_pack", "nlcl_route_receipt", "nlcl_route_execution_receipt"}
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

SOURCE_ROOTS = ("adapters", "apps", "services", "packages", "scripts", "tests")
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


def _project_kb_binding_tables_source_truth(
    root: Path,
    errors: list[str],
) -> frozenset[str] | None:
    text = _read(root / DATABASE_SOURCE, errors)
    if not text:
        return None
    try:
        module = ast.parse(text, filename=DATABASE_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {DATABASE_SOURCE}: {exc}")
        return None

    assignment = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "REQUIRED_COLUMNS"
            and isinstance(node.value, ast.Dict)
        ),
        None,
    )
    if assignment is None:
        errors.append(f"{DATABASE_SOURCE} has no literal REQUIRED_COLUMNS mapping")
        return None

    bound_tables: set[str] = set()
    for key, value in zip(assignment.value.keys, assignment.value.values, strict=True):
        if not (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            continue
        try:
            columns = ast.literal_eval(value.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(columns, set) and PROJECT_KB_BINDING_FIELDS.issubset(columns):
            bound_tables.add(key.value)
    return frozenset(bound_tables)


def _class_method(
    module: ast.Module,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    owner = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if owner is None:
        return None
    return next(
        (
            node
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ),
        None,
    )


def _method_call_count(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    called_method: str,
) -> int:
    return sum(
        1
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == called_method
    )


def _method_attribute_count(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    attribute: str,
) -> int:
    return sum(
        1
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute) and node.attr == attribute
    )


def _workflow_source_truth(
    root: Path,
    errors: list[str],
) -> tuple[tuple[str, str], ...] | None:
    source = root / NLCL_MODELS_SOURCE
    text = _read(source, errors)
    if not text:
        return None
    try:
        module = ast.parse(text, filename=NLCL_MODELS_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {NLCL_MODELS_SOURCE}: {exc}")
        return None

    builder = next(
        (
            node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_nlcl_route_receipt"
        ),
        None,
    )
    if builder is None:
        errors.append(f"{NLCL_MODELS_SOURCE} has no build_nlcl_route_receipt")
        return None

    calls: list[tuple[int, str, str]] = []
    for node in ast.walk(builder):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "NlclChapterCall"
        ):
            continue
        keywords = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if keyword.arg
        }
        ordinal = keywords.get("ordinal")
        skill_id = keywords.get("skill_id")
        input_contract = keywords.get("input_contract")
        if not (
            isinstance(ordinal, ast.Constant)
            and isinstance(ordinal.value, int)
            and not isinstance(ordinal.value, bool)
            and isinstance(skill_id, ast.Constant)
            and isinstance(skill_id.value, str)
            and isinstance(input_contract, ast.Constant)
            and isinstance(input_contract.value, str)
        ):
            errors.append(
                f"{NLCL_MODELS_SOURCE} has a non-literal NLCL chapter call identity"
            )
            return None
        calls.append((ordinal.value, skill_id.value, input_contract.value))

    calls.sort(key=lambda item: item[0])
    if [ordinal for ordinal, _, _ in calls] != list(range(1, len(calls) + 1)):
        errors.append(
            f"{NLCL_MODELS_SOURCE} has non-contiguous chapter call ordinals"
        )
        return None
    return tuple((skill_id, input_contract) for _, skill_id, input_contract in calls)


def _require_current_claim(
    texts: Mapping[str, str],
    source: str,
    claim: str,
    errors: list[str],
) -> None:
    if claim not in texts.get(source, ""):
        errors.append(f"{source} is missing current-state claim: {claim}")


def _require_current_pattern(
    texts: Mapping[str, str],
    source: str,
    pattern: str,
    label: str,
    errors: list[str],
) -> None:
    if re.search(pattern, texts.get(source, ""), re.IGNORECASE | re.DOTALL) is None:
        errors.append(f"{source} is missing current-state fact: {label}")


def _candidate_default_schema_source_truth(
    root: Path,
    errors: list[str],
) -> str | None:
    source = root / AGENT_CANDIDATE_MODELS_SOURCE
    text = _read(source, errors)
    if not text:
        return None
    try:
        module = ast.parse(text, filename=AGENT_CANDIDATE_MODELS_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {AGENT_CANDIDATE_MODELS_SOURCE}: {exc}")
        return None

    function = next(
        (
            node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "agent_candidate_output_id"
        ),
        None,
    )
    if function is None:
        errors.append(
            f"{AGENT_CANDIDATE_MODELS_SOURCE} has no agent_candidate_output_id"
        )
        return None
    for argument, default in zip(
        function.args.kwonlyargs,
        function.args.kw_defaults,
        strict=True,
    ):
        if argument.arg != "schema_id" or default is None:
            continue
        try:
            value = ast.literal_eval(default)
        except (ValueError, TypeError):
            value = None
        if isinstance(value, str):
            return value
    errors.append(
        f"{AGENT_CANDIDATE_MODELS_SOURCE} has no literal schema_id default"
    )
    return None


def _native_knowledge_types_source_truth(
    root: Path,
    errors: list[str],
) -> frozenset[str] | None:
    source = root / KNOWLEDGE_SERVICE_SOURCE
    text = _read(source, errors)
    if not text:
        return None
    try:
        module = ast.parse(text, filename=KNOWLEDGE_SERVICE_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {KNOWLEDGE_SERVICE_SOURCE}: {exc}")
        return None

    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id != "SUPPORTED_PROMOTION_CARD_TYPES":
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            break
        try:
            candidate = ast.literal_eval(value.args[0])
        except (ValueError, TypeError):
            candidate = None
        if isinstance(candidate, set) and all(
            isinstance(item, str) for item in candidate
        ):
            return frozenset(candidate)
        break
    errors.append(
        f"{KNOWLEDGE_SERVICE_SOURCE} has no literal "
        "SUPPORTED_PROMOTION_CARD_TYPES frozenset"
    )
    return None


def _validate_current_project_kb_binding_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    source_truth = _sqlite_source_truth(root, errors)
    if source_truth is not None and source_truth[0] < 20:
        errors.append(f"{DATABASE_SOURCE} project-KB binding requires schema v20+")

    binding_tables = _project_kb_binding_tables_source_truth(root, errors)
    if binding_tables is not None and binding_tables != PROJECT_KB_BINDING_TABLES:
        errors.append(
            f"{DATABASE_SOURCE} project-KB binding table drift: "
            f"expected={sorted(PROJECT_KB_BINDING_TABLES)}, "
            f"actual={sorted(binding_tables)}"
        )

    database_text = _read(root / DATABASE_SOURCE, errors)
    try:
        database_module = ast.parse(database_text, filename=DATABASE_SOURCE)
    except SyntaxError:
        database_module = None
    if database_module is not None:
        migration = _class_method(
            database_module,
            "SqliteStore",
            "_migrate_project_kb_bindings_v20",
        )
        if migration is None:
            errors.append(f"{DATABASE_SOURCE} has no v20 project-KB binding migration")
        else:
            migration_text = ast.get_source_segment(database_text, migration) or ""
            if "ALTER TABLE" not in migration_text or "UPDATE " in migration_text:
                errors.append(
                    f"{DATABASE_SOURCE} v20 migration must add nullable columns "
                    "without backfilling historical identities"
                )

    context_models_text = _read(root / CONTEXT_MODELS_SOURCE, errors)
    try:
        context_models_module = ast.parse(
            context_models_text,
            filename=CONTEXT_MODELS_SOURCE,
        )
    except SyntaxError as exc:
        errors.append(f"cannot parse {CONTEXT_MODELS_SOURCE}: {exc}")
        context_models_module = None
    if context_models_module is not None:
        request_model = next(
            (
                node
                for node in context_models_module.body
                if isinstance(node, ast.ClassDef)
                and node.name == "ContextCreateRequest"
            ),
            None,
        )
        request_fields = {
            node.target.id: node
            for node in (() if request_model is None else request_model.body)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
        }
        for field in PROJECT_KB_BINDING_FIELDS:
            declaration = request_fields.get(field)
            if declaration is None or "None" in ast.unparse(declaration.annotation):
                errors.append(
                    f"{CONTEXT_MODELS_SOURCE} ContextCreateRequest must require {field}"
                )
                continue
            if isinstance(declaration.value, ast.Call) and any(
                keyword.arg == "default" for keyword in declaration.value.keywords
            ):
                errors.append(
                    f"{CONTEXT_MODELS_SOURCE} ContextCreateRequest must not default {field}"
                )

    context_service_text = _read(root / CONTEXT_SERVICE_SOURCE, errors)
    try:
        context_service_module = ast.parse(
            context_service_text,
            filename=CONTEXT_SERVICE_SOURCE,
        )
    except SyntaxError as exc:
        errors.append(f"cannot parse {CONTEXT_SERVICE_SOURCE}: {exc}")
        context_service_module = None
    if context_service_module is not None:
        create_pack = _class_method(context_service_module, "ContextService", "create_pack")
        if create_pack is None or _method_attribute_count(
            create_pack,
            "_require_project_kb_binding",
        ) < 2:
            errors.append(
                f"{CONTEXT_SERVICE_SOURCE} must revalidate project KB before "
                "compile and idempotent replay"
            )

    agent_text = _read(root / AGENT_SERVICE_SOURCE, errors)
    try:
        agent_module = ast.parse(agent_text, filename=AGENT_SERVICE_SOURCE)
    except SyntaxError as exc:
        errors.append(f"cannot parse {AGENT_SERVICE_SOURCE}: {exc}")
        agent_module = None
    if agent_module is not None:
        required_calls = {
            "start_run": ("_require_current_project_kb_for_context", 1),
            "resume_run": ("_require_current_project_kb_for_run", 1),
            "_execute": ("_require_current_project_kb_for_run", 4),
            "decide_provider_recovery": ("_require_current_project_kb_for_run", 1),
        }
        for method_name, (called_method, minimum) in required_calls.items():
            method = _class_method(agent_module, "AgentService", method_name)
            actual = 0 if method is None else _method_call_count(method, called_method)
            if actual < minimum:
                errors.append(
                    f"{AGENT_SERVICE_SOURCE} {method_name} project-KB verifier "
                    f"count drift: expected>={minimum}, actual={actual}"
                )
        recovery = _class_method(
            agent_module,
            "AgentService",
            "decide_provider_recovery",
        )
        guarded = recovery is not None and any(
            isinstance(node, ast.If)
            and "adopt_persisted_candidate" in ast.unparse(node.test)
            and any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "_require_current_project_kb_for_run"
                for statement in node.body
                for child in ast.walk(statement)
            )
            for node in ast.walk(recovery)
        )
        if not guarded:
            errors.append(
                f"{AGENT_SERVICE_SOURCE} recovery adoption is not guarded by "
                "current project-KB verification"
            )

    context_review = _read(root / CONTEXT_REVIEW_SOURCE, errors)
    for claim in (
        "project_kb_state_id",
        "project_kb_manifest_hash",
        "projectKbBindingStatus !== 'current'",
    ):
        if claim not in context_review:
            errors.append(f"{CONTEXT_REVIEW_SOURCE} is missing project-KB UI gate: {claim}")
    knowledge_review = _read(root / KNOWLEDGE_REVIEW_SOURCE, errors)
    for claim in ("projectKbStartMessage", "!projectKbBinding", "使用这批知识开始写作"):
        if claim not in knowledge_review:
            errors.append(f"{KNOWLEDGE_REVIEW_SOURCE} is missing project-KB CTA gate: {claim}")

    binding_documents = (
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/DATA_MODEL.md",
        "docs/DEVELOPMENT_BRIEF.md",
        "docs/ROADMAP.md",
        "docs/TEST_AND_EVALUATION_STRATEGY.md",
        "docs/API_COMPATIBILITY.md",
        "docs/REQUIREMENTS_TRACEABILITY.md",
        "services/writing-runtime/README.md",
        CONTRACTS_README,
    )
    for source in binding_documents:
        for field in PROJECT_KB_BINDING_FIELDS:
            _require_current_claim(texts, source, field, errors)

    legacy_pattern = (
        r"v19.{0,100}NULL(?:"
        r"(?=.{0,140}(?:可读|只读|读取))(?=.{0,180}(?:不可执行|不能执行|不得执行))"
        r"|(?=.{0,280}readable)(?=.{0,360}cannot)"
        r")"
    )
    for source in (
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/DATA_MODEL.md",
        "docs/ROADMAP.md",
        "docs/API_COMPATIBILITY.md",
        "services/writing-runtime/README.md",
        CONTRACTS_README,
    ):
        _require_current_pattern(
            texts,
            source,
            legacy_pattern,
            "v19 NULL project-KB bindings are readable but not executable",
            errors,
        )


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


def _validate_current_m3j_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    source_steps = _workflow_source_truth(root, errors)
    if source_steps is not None and source_steps != CANONICAL_WORKFLOW_STEPS:
        errors.append(
            f"{NLCL_MODELS_SOURCE} canonical workflow drift: "
            f"expected={CANONICAL_WORKFLOW_STEPS}, actual={source_steps}"
        )

    canonical_documents = (
        "README.md",
        "docs/ROADMAP.md",
        "docs/ARCHITECTURE.md",
        "docs/DATA_MODEL.md",
        "docs/API_COMPATIBILITY.md",
        "docs/DEVELOPMENT_BRIEF.md",
        "docs/FOUR_SKILLS_CAPABILITY_MAP.md",
        "docs/TEST_AND_EVALUATION_STRATEGY.md",
        M3J_IMPLEMENTATION,
        M3J_EVIDENCE,
    )
    for source in canonical_documents:
        if CANONICAL_WORKFLOW_CHAIN not in texts.get(source, ""):
            errors.append(
                f"{source} is missing canonical M3j workflow chain: "
                f"{CANONICAL_WORKFLOW_CHAIN}"
            )

    required_references = {
        "README.md": (M3J_IMPLEMENTATION, M3J_EVIDENCE),
        "docs/ROADMAP.md": (
            Path(M3J_IMPLEMENTATION).name,
            "VAL-M3J-20260722",
        ),
    }
    for source, references in required_references.items():
        for reference in references:
            _require_current_claim(texts, source, reference, errors)

    implementation = texts.get(M3J_IMPLEMENTATION, "")
    evidence = texts.get(M3J_EVIDENCE, "")
    for source, text in (
        (M3J_IMPLEMENTATION, implementation),
        (M3J_EVIDENCE, evidence),
    ):
        for boundary in (
            "formal_draft_allowed=false",
            "allowed_output=repair_plan_or_risk_draft_only",
            "Provider 后",
            "候选写作包",
        ):
            if boundary not in text:
                errors.append(f"{source} is missing M3j boundary: {boundary}")
    if "Evidence ID: `VAL-M3J-20260722`" not in evidence:
        errors.append(f"{M3J_EVIDENCE} has the wrong or missing Evidence ID")


def _validate_current_m3k_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    required_references = {
        "README.md": (M3K_IMPLEMENTATION, M3K_EVIDENCE),
        "docs/ROADMAP.md": (
            Path(M3K_IMPLEMENTATION).name,
            "VAL-M3K-20260723",
        ),
        "docs/INDEX.md": (
            Path(M3K_IMPLEMENTATION).name,
            "VAL-M3K-20260723",
        ),
        "evidence/README.md": (
            Path(M3K_EVIDENCE).name,
            *(Path(screenshot).name for screenshot in M3K_SCREENSHOTS),
        ),
    }
    for source, references in required_references.items():
        for reference in references:
            _require_current_claim(texts, source, reference, errors)

    current_claims = {
        "README.md": "candidate_output_submit",
        "docs/ROADMAP.md": "provider_replay_count=0",
        "docs/ARCHITECTURE.md": "candidate_output_submit",
        "docs/DATA_MODEL.md": "agent_candidate_output",
        "docs/API_COMPATIBILITY.md": "candidate_output_submit",
        "docs/SECURITY_AND_CAPABILITY_MODEL.md": "provider_replay_count=0",
        "docs/DEVELOPMENT_BRIEF.md": "candidate_output_submit",
        "docs/FOUR_SKILLS_CAPABILITY_MAP.md": "Provider identity/recovery primitive",
        "docs/TEST_AND_EVALUATION_STRATEGY.md": "managed 三步 Run 必须恰好一个",
    }
    for source, claim in current_claims.items():
        _require_current_claim(texts, source, claim, errors)

    implementation = texts.get(M3K_IMPLEMENTATION, "")
    evidence = texts.get(M3K_EVIDENCE, "")
    for source, text in (
        (M3K_IMPLEMENTATION, implementation),
        (M3K_EVIDENCE, evidence),
    ):
        for boundary in (
            "candidate_output_submit",
            "formal_draft_allowed=false",
            "requires_author_review=true",
            "provider_replay_count=0",
            "adopt_persisted_candidate",
            "abandon_and_recompile",
        ):
            if boundary not in text:
                errors.append(f"{source} is missing M3k boundary: {boundary}")
    for boundary in (
        "kind + content",
        "Candidate recovery cutpoint",
        "transaction 2",
        "creation-instruction-materialization.schema.json",
        "creation-instruction-dispatch-receipt.schema.json",
        "writer-task.schema.json",
        "writer-candidate-result.schema.json",
    ):
        if boundary not in implementation:
            errors.append(f"{M3K_IMPLEMENTATION} is missing M3k v3 boundary: {boundary}")
    if "Evidence ID: `VAL-M3K-20260723`" not in evidence:
        errors.append(f"{M3K_EVIDENCE} has the wrong or missing Evidence ID")

    for relative in M3K_SCREENSHOTS:
        screenshot = root / relative
        try:
            screenshot_bytes = screenshot.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read M3k screenshot {screenshot}: {exc}")
            continue
        if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            errors.append(f"{relative} is not a PNG")
        screenshot_hash = hashlib.sha256(screenshot_bytes).hexdigest().upper()
        if screenshot_hash not in evidence:
            errors.append(
                f"{M3K_EVIDENCE} does not bind current screenshot SHA-256 "
                f"{screenshot_hash}"
            )

    for relative in M3K_CONTRACTS:
        contract = root / relative
        try:
            contract_bytes = contract.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read M3k contract {contract}: {exc}")
            continue
        contract_hash = hashlib.sha256(contract_bytes).hexdigest().upper()
        if Path(relative).name not in evidence or contract_hash not in evidence:
            errors.append(
                f"{M3K_EVIDENCE} does not bind current contract {relative} "
                f"SHA-256 {contract_hash}"
            )


def _validate_current_m3l_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    required_references = {
        "README.md": (M3L_IMPLEMENTATION, M3L_EVIDENCE),
        "docs/ROADMAP.md": (
            Path(M3L_IMPLEMENTATION).name,
            "VAL-M3L-20260722",
        ),
        "docs/INDEX.md": (
            Path(M3L_IMPLEMENTATION).name,
            "VAL-M3L-20260722",
        ),
        "evidence/README.md": (
            Path(M3L_EVIDENCE).name,
            "VAL-M3L-20260722",
        ),
    }
    for source, references in required_references.items():
        for reference in references:
            _require_current_claim(texts, source, reference, errors)

    current_claims = {
        "README.md": "M3l 应用级/项目级受控 Skill 生命周期",
        "docs/ROADMAP.md": "M3l Managed Skill Lifecycle",
        "docs/ARCHITECTURE.md": "M3l 受管 Skill 生命周期",
        "docs/DATA_MODEL.md": (
            "`skill_import_receipt`、`skill_activation` 和 "
            "`skill_activation_receipt`"
        ),
        "docs/API_COMPATIBILITY.md": (
            "M3l 已发布 application/project managed Skill lifecycle"
        ),
        "docs/SECURITY_AND_CAPABILITY_MODEL.md": "trusted import 默认 inert",
        "docs/FOUR_SKILLS_CAPABILITY_MAP.md": (
            "M3l 已把这四个已适配受管确定性包"
        ),
        "docs/DEVELOPMENT_BRIEF.md": "M3l 将已适配包稳定复制",
        "docs/TEST_AND_EVALUATION_STRATEGY.md": "已适配确定性包的内容寻址导入",
        "docs/REQUIREMENTS_TRACEABILITY.md": (
            "M3l 已证明已适配确定性包"
        ),
    }
    for source, claim in current_claims.items():
        _require_current_claim(texts, source, claim, errors)

    implementation = texts.get(M3L_IMPLEMENTATION, "")
    evidence = texts.get(M3L_EVIDENCE, "")
    required_boundaries = {
        M3L_IMPLEMENTATION: (
            "Runtime-owned",
            "managed-skills/snapshots/<skill-version-id>",
            "managed_skill_adaptation_required",
            "expected_state_revision",
            "管理 UI",
            "通用原始四 Skills Adapter",
        ),
        M3L_EVIDENCE: (
            "Runtime-owned",
            "managed-skills/snapshots/<skill-version-id>",
            "managed_skill_adaptation_required",
            "inert",
            "Workbench 生命周期 UI",
            "通用原始四 Skills Adapter",
        ),
    }
    for source, boundaries in required_boundaries.items():
        text = implementation if source == M3L_IMPLEMENTATION else evidence
        for boundary in boundaries:
            if boundary not in text:
                errors.append(f"{source} is missing M3l boundary: {boundary}")

    if "Evidence ID: `VAL-M3L-20260722`" not in evidence:
        errors.append(f"{M3L_EVIDENCE} has the wrong or missing Evidence ID")

    source_truth = _sqlite_source_truth(root, errors)
    if source_truth is not None:
        schema_version, table_count = source_truth
        _require_current_claim(
            texts,
            M3L_IMPLEMENTATION,
            f"SQLite schema v{schema_version}、{table_count} 张表",
            errors,
        )
        _require_current_claim(
            texts,
            M3L_EVIDENCE,
            "SQLite schema v19 / 33 tables",
            errors,
        )

    for relative in M3L_CONTRACTS:
        contract = root / relative
        try:
            contract_bytes = contract.read_bytes()
        except OSError as exc:
            errors.append(f"cannot read M3l contract {contract}: {exc}")
            continue
        contract_hash = hashlib.sha256(contract_bytes).hexdigest().upper()
        if Path(relative).name not in evidence or contract_hash not in evidence:
            errors.append(
                f"{M3L_EVIDENCE} does not bind current contract {relative} "
                f"SHA-256 {contract_hash}"
            )


def _validate_current_candidate_knowledge_context_state(
    root: Path,
    texts: Mapping[str, str],
    errors: list[str],
) -> None:
    candidate_schema = _candidate_default_schema_source_truth(root, errors)
    if candidate_schema is not None and candidate_schema != CURRENT_CANDIDATE_SCHEMA:
        errors.append(
            f"{AGENT_CANDIDATE_MODELS_SOURCE} current candidate schema drift: "
            f"expected={CURRENT_CANDIDATE_SCHEMA}, actual={candidate_schema}"
        )
    candidate_source = _read(root / AGENT_CANDIDATE_MODELS_SOURCE, errors)
    for legacy_schema in LEGACY_CANDIDATE_SCHEMAS:
        if legacy_schema not in candidate_source:
            errors.append(
                f"{AGENT_CANDIDATE_MODELS_SOURCE} is missing legacy read-only "
                f"schema: {legacy_schema}"
            )

    candidate_documents = (
        "README.md",
        "docs/ROADMAP.md",
        "docs/ARCHITECTURE.md",
        "docs/DATA_MODEL.md",
        "docs/API_COMPATIBILITY.md",
        "docs/DEVELOPMENT_BRIEF.md",
        "docs/TEST_AND_EVALUATION_STRATEGY.md",
    )
    for source in candidate_documents:
        _require_current_claim(
            texts,
            source,
            "agent-candidate-output.v3",
            errors,
        )

    legacy_boundary = (
        r"(?:历史.{0,30}(?:v1/v2|v2/v1|v1.{0,15}v2)|"
        r"(?:v1/v2|v2/v1).{0,30}历史)"
        r"(?=.{0,500}(?:只读|不得新写))"
        r"(?=.{0,500}(?:(?:不得|不能).{0,120}(?:完成新 Run|recovery|adopt|升级)|"
        r"(?:模型身份|dispatch|因果链).{0,120}(?:未验证|不完整)))"
    )
    for source in candidate_documents:
        _require_current_pattern(
            texts,
            source,
            legacy_boundary,
            "legacy Candidate v1/v2 is read-only and cannot complete a current Run",
            errors,
        )
    for legacy_schema in LEGACY_CANDIDATE_SCHEMAS:
        _require_current_claim(
            texts,
            "docs/API_COMPATIBILITY.md",
            legacy_schema,
            errors,
        )

    agent_api = _read(root / AGENT_API_SOURCE, errors)
    if PROVIDER_TURN_RECEIPTS_PATH not in agent_api:
        errors.append(
            f"{AGENT_API_SOURCE} is missing Provider receipt GET path: "
            f"{PROVIDER_TURN_RECEIPTS_PATH}"
        )
    for source in (
        "README.md",
        "docs/ROADMAP.md",
        "docs/ARCHITECTURE.md",
        "docs/API_COMPATIBILITY.md",
    ):
        _require_current_claim(
            texts,
            source,
            PROVIDER_TURN_RECEIPTS_PATH,
            errors,
        )

    if WRITER_TRACE_PATH not in agent_api:
        errors.append(
            f"{AGENT_API_SOURCE} is missing Writer Trace GET path: "
            f"{WRITER_TRACE_PATH}"
        )
    for source in (
        "README.md",
        "services/writing-runtime/README.md",
        "docs/ARCHITECTURE.md",
        "docs/API_COMPATIBILITY.md",
    ):
        _require_current_claim(texts, source, WRITER_TRACE_PATH, errors)

    native_types = _native_knowledge_types_source_truth(root, errors)
    if native_types is not None and native_types != SUPPORTED_NATIVE_KNOWLEDGE_TYPES:
        errors.append(
            f"{KNOWLEDGE_SERVICE_SOURCE} native knowledge type drift: "
            f"expected={sorted(SUPPORTED_NATIVE_KNOWLEDGE_TYPES)}, "
            f"actual={sorted(native_types)}"
        )
    typed_chain_patterns = {
        "README.md": (
            r"当前版本化本地\s+TXT fixture.{0,500}work/voice/validator"
            r".{0,500}(?:整批正式化|formalization|finalize)"
        ),
        "docs/ROADMAP.md": (
            r"O2 本地\s+TXT 代表性纵切.{0,500}整批正式化"
            r".{0,200}work/voice/validator"
        ),
        "docs/ARCHITECTURE.md": (
            r"受管本地\s+TXT.{0,600}work/voice/validator"
            r".{0,500}(?:整批正式化|formalization|finalize)"
        ),
        "docs/DATA_MODEL.md": (
            r"本地\s+TXT\s*->\s*三类正式卡\s*->\s*Selector\s*->\s*"
            r"Writer/Gate/Apply/Rollback"
        ),
        "docs/DEVELOPMENT_BRIEF.md": (
            r"当前 O2 的受管本地\s+TXT 代表性纵切已经完成"
            r".{0,500}work/voice/validator.{0,500}FormalKnowledgeSelector"
        ),
        "docs/FOUR_SKILLS_CAPABILITY_MAP.md": (
            r"当前 Runtime 的版本化本地\s+TXT 代表性链已走通"
            r".{0,500}work/voice/validator"
            r".{0,500}(?:整批正式化|formalization|finalize)"
        ),
        "docs/TEST_AND_EVALUATION_STRATEGY.md": (
            r"当前 O2 已用版本化本地\s+TXT.{0,500}work/voice/validator"
            r".{0,500}(?:整批正式化|正式化|formalization|finalize)"
        ),
        "docs/REQUIREMENTS_TRACEABILITY.md": (
            r"受管本地\s+TXT.{0,500}work/voice/validator"
            r".{0,500}(?:整批正式化|formalization|finalize)"
        ),
    }
    for source, pattern in typed_chain_patterns.items():
        _require_current_pattern(
            texts,
            source,
            pattern,
            "managed local TXT work/voice/validator review and batch formalization",
            errors,
        )

    context_three_stage_claims = {
        "README.md": (
            "编译 Context -> 审查证据并选择 Writer 模式 -> 明确确认运行"
        ),
        "docs/TEST_AND_EVALUATION_STRATEGY.md": (
            "编译、证据/Writer 模式审查、明确确认三阶段"
        ),
        "docs/REQUIREMENTS_TRACEABILITY.md": (
            "Context 编译/证据与 Writer 模式审查/明确确认三阶段"
        ),
        "apps/workbench/README.md": (
            "Context Pack 编译、证据/Writer 模式审查、明确确认三阶段"
        ),
    }
    for source, claim in context_three_stage_claims.items():
        _require_current_claim(texts, source, claim, errors)


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
        CONTRACTS_README,
        M3H_IMPLEMENTATION,
        M3H_EVIDENCE,
        M3I_IMPLEMENTATION,
        M3I_EVIDENCE,
        M3J_IMPLEMENTATION,
        M3J_EVIDENCE,
        M3K_IMPLEMENTATION,
        M3K_EVIDENCE,
        M3L_IMPLEMENTATION,
        M3L_EVIDENCE,
        "apps/workbench/README.md",
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
    _validate_current_project_kb_binding_state(root, texts, errors)
    _validate_current_m3h_state(root, texts, errors)
    _validate_current_m3i_state(root, texts, errors)
    _validate_current_m3j_state(root, texts, errors)
    _validate_current_m3k_state(root, texts, errors)
    _validate_current_m3l_state(root, texts, errors)
    _validate_current_candidate_knowledge_context_state(root, texts, errors)

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
