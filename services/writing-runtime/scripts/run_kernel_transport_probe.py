from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(WRITING_RUNTIME / "tests"))

from kernel_harness.adapters.codex import CodexAppServerAdapter  # noqa: E402
from kernel_harness.adapters.opencode import OpenCodeServerAdapter  # noqa: E402
from kernel_harness.adapters.base import (  # noqa: E402
    CleanupReport,
    EVIDENCE_ROOT_LIMIT_BYTES,
)
from kernel_harness.assertions import (  # noqa: E402
    terminal_state_for_run,
    validate_assertion_results,
    validate_harness_run_against_case,
)
from kernel_harness.fixture import (  # noqa: E402
    prepare_disposable_fixture,
    tree_fingerprint,
)
from kernel_harness.evidence import (  # noqa: E402
    build_evidence_index,
    validate_evidence_index,
)
from kernel_harness.models import (  # noqa: E402
    AssertionResult,
    AssertionStatus,
    CleanupStatus,
    EvidenceKind,
    HarnessCase,
    HarnessLane,
    HarnessOperation,
    HarnessRun,
    HarnessStepSpec,
    canonical_case_fingerprint,
)


DEFAULT_CASE = (
    WRITING_RUNTIME / "tests" / "fixtures" / "kernel-harness" / "case.json"
)
NPM_HOME = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm"
DEFAULT_CODEX = (
    NPM_HOME
    / "node_modules"
    / "@openai"
    / "codex"
    / "node_modules"
    / "@openai"
    / "codex-win32-x64"
    / "vendor"
    / "x86_64-pc-windows-msvc"
    / "bin"
    / "codex.exe"
)
DEFAULT_OPENCODE = NPM_HOME / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
SAFE_OUTPUT_BASE = WRITING_RUNTIME / ".runtime" / "kernel-harness" / "runs"
_T = TypeVar("_T")
_ADAPTER_OUTPUT_NAMES = ("workspace", "evidence", "isolation")
_SEALING_FILE_NAMES = frozenset(
    {"cleanup-report.json", "evidence-index.json", "manifest.json"}
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_evidence_files(
    evidence_root: Path,
    *,
    limit_bytes: int = EVIDENCE_ROOT_LIMIT_BYTES,
) -> tuple[list[Path], int]:
    if limit_bytes < 1:
        raise ValueError("evidence root byte limit must be positive")
    total = 0
    files: list[Path] = []
    for path in sorted(evidence_root.rglob("*"), key=lambda item: item.as_posix()):
        if _is_link_or_reparse(path):
            raise ValueError(f"kernel evidence cannot contain a reparse point: {path}")
        if not path.is_file():
            continue
        details = path.stat()
        if details.st_nlink != 1:
            raise ValueError(f"kernel evidence cannot contain a hard link: {path}")
        files.append(path)
        total += details.st_size
        if total > limit_bytes:
            raise ValueError(
                f"kernel evidence exceeds its {limit_bytes}-byte total limit"
            )
    return files, total


def _evidence_tree_size_bytes(
    evidence_root: Path,
    *,
    limit_bytes: int = EVIDENCE_ROOT_LIMIT_BYTES,
) -> int:
    return _bounded_evidence_files(evidence_root, limit_bytes=limit_bytes)[1]


def _adapter_evidence_size_bytes(evidence_root: Path) -> int:
    files, _ = _bounded_evidence_files(evidence_root)
    return sum(path.stat().st_size for path in files if path.name not in _SEALING_FILE_NAMES)


def _failure_fingerprint(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _absolute_lexical_path(path: Path) -> Path:
    """Return an absolute path without resolving links or reparse points."""

    return Path(os.path.abspath(os.fspath(path)))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"cannot inspect kernel harness output path: {path}") from exc
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_attribute)


def _assert_existing_chain_has_no_reparse(path: Path, *, boundary: Path) -> None:
    candidate = _absolute_lexical_path(path)
    root = _absolute_lexical_path(boundary)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("kernel harness output path escapes its safety boundary") from exc

    current = candidate
    while True:
        if _is_link_or_reparse(current):
            raise ValueError(
                "kernel harness output path cannot contain a link, junction, "
                f"or reparse point: {current}"
            )
        if current == root:
            return
        current = current.parent


def _resolve_safe_output_root(requested: Path) -> Path:
    runtime_root = WRITING_RUNTIME.resolve(strict=True)
    runtime_lexical = _absolute_lexical_path(WRITING_RUNTIME)
    allowed_lexical = _absolute_lexical_path(SAFE_OUTPUT_BASE)
    requested_lexical = _absolute_lexical_path(requested)
    if requested_lexical.parent != allowed_lexical:
        raise ValueError(
            "kernel harness output must be a direct named-run child of "
            f"{SAFE_OUTPUT_BASE}"
        )
    _assert_existing_chain_has_no_reparse(
        requested_lexical,
        boundary=runtime_lexical,
    )
    allowed_root = allowed_lexical.resolve(strict=False)
    if not allowed_root.is_relative_to(runtime_root):
        raise ValueError("kernel harness .runtime output root escapes the Writing Runtime")
    candidate = requested_lexical.resolve(strict=False)
    if candidate.parent != allowed_root:
        raise ValueError(
            "kernel harness output must be a direct named-run child of "
            f"{SAFE_OUTPUT_BASE}"
        )
    return candidate


def _assert_adapter_output_layout(
    output_root: Path,
    run_root: Path,
    roots: Sequence[Path],
) -> tuple[Path, tuple[Path, ...]]:
    _assert_existing_chain_has_no_reparse(
        run_root,
        boundary=WRITING_RUNTIME,
    )
    resolved_output = output_root.resolve(strict=True)
    resolved_run = run_root.resolve(strict=True)
    if resolved_run.parent != resolved_output:
        raise ValueError("kernel harness adapter output escapes the named run root")

    resolved_roots: list[Path] = []
    for root in roots:
        _assert_existing_chain_has_no_reparse(
            root,
            boundary=WRITING_RUNTIME,
        )
        resolved = root.resolve(strict=True)
        if resolved.parent != resolved_run:
            raise ValueError(
                "kernel harness workspace, evidence, and isolation roots must be "
                "direct children of the adapter output root"
            )
        resolved_roots.append(resolved)
    return resolved_run, tuple(resolved_roots)


def _prepare_adapter_output_roots(
    output_root: Path,
    name: str,
) -> tuple[Path, Path, Path, Path]:
    if name not in {"codex", "opencode"}:
        raise ValueError(f"unknown adapter: {name}")

    output_root = _resolve_safe_output_root(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    # Re-resolve after creation so a pre-existing named-run link cannot be followed.
    output_root = _resolve_safe_output_root(output_root)
    run_root = output_root / name
    _assert_existing_chain_has_no_reparse(
        run_root,
        boundary=WRITING_RUNTIME,
    )
    if run_root.exists():
        if not run_root.is_dir():
            raise ValueError(f"kernel harness output is not a directory: {run_root}")
        if any(run_root.iterdir()):
            raise ValueError(f"kernel harness output already exists: {run_root}")
    else:
        run_root.mkdir()

    # Validate each directory immediately after creation, before the next path is used.
    resolved_run, _ = _assert_adapter_output_layout(output_root, run_root, ())
    resolved_roots: list[Path] = []
    for child in _ADAPTER_OUTPUT_NAMES:
        root = run_root / child
        root.mkdir()
        _, checked = _assert_adapter_output_layout(output_root, run_root, (root,))
        resolved_roots.append(checked[0])
    workspace_root, evidence_root, isolation_root = resolved_roots
    return resolved_run, workspace_root, evidence_root, isolation_root


def _case_step(case: HarnessCase, operation: HarnessOperation) -> HarnessStepSpec:
    matches = [step for step in case.steps if step.operation is operation]
    if len(matches) != 1:
        raise ValueError(
            f"transport execution contract requires exactly one {operation.value} step"
        )
    return matches[0]


def _transport_execution_contract(case: HarnessCase) -> dict[str, object]:
    preflight = _case_step(case, HarnessOperation.PREPARE)
    executable = [
        _case_step(case, HarnessOperation.TRANSPORT_HANDSHAKE),
        _case_step(case, HarnessOperation.CLEANUP),
    ]
    classified = {preflight.step_id, *(step.step_id for step in executable)}
    return {
        "schema": "zhimeng.kernel-transport-execution-contract.v1",
        "preflight": {
            "step_id": preflight.step_id,
            "operation": preflight.operation.value,
            "counted_as_executed": False,
            "timeout_enforcement": "not_measured",
        },
        "executable_steps": [
            {
                "step_id": step.step_id,
                "operation": step.operation.value,
                "timeout_seconds": step.timeout_seconds,
                "timeout_enforcement": "asyncio_wait_for_wall_clock",
            }
            for step in executable
        ],
        "unexecuted_step_ids": [
            step.step_id for step in case.steps if step.step_id not in classified
        ],
    }


async def _await_case_step(
    case: HarnessCase,
    operation: HarnessOperation,
    awaitable: Awaitable[_T],
) -> _T:
    step = _case_step(case, operation)
    try:
        return await asyncio.wait_for(awaitable, timeout=step.timeout_seconds)
    except TimeoutError as exc:
        raise TimeoutError(
            f"harness step {step.step_id} exceeded its {step.timeout_seconds}s deadline"
        ) from exc


def _cli_exit_code(results: Sequence[dict[str, object]]) -> int:
    if any(
        item.get("status") == "failed"
        or item.get("transport_passed") is False
        or item.get("harness_state") == "failed"
        for item in results
    ):
        return 1
    if any(item.get("harness_state") == "blocked" for item in results):
        return 2
    return 0


def _cli_has_failure(results: Sequence[dict[str, object]]) -> bool:
    return _cli_exit_code(results) != 0


def _encoded_secret_values(values: tuple[str, ...]) -> list[bytes]:
    return [value.encode("utf-8") for value in values if len(value) >= 8]


def _redact_sensitive_text(value: str | None, secrets: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    result = value
    for secret in secrets:
        if len(secret) >= 8:
            result = result.replace(secret, "<redacted>")
    return result


def _assert_evidence_secret_free(evidence_root: Path, values: tuple[str, ...]) -> None:
    encoded_values = _encoded_secret_values(values)
    files, _ = _bounded_evidence_files(evidence_root)
    for path in files:
        if any(secret in path.read_bytes() for secret in encoded_values):
            raise ValueError("evidence secret redaction did not converge")


def _redact_evidence_secrets(evidence_root: Path, values: tuple[str, ...]) -> int:
    encoded_values = _encoded_secret_values(values)
    replacement = b"<redacted>"
    hit_count = 0
    if not encoded_values:
        return 0
    files, _ = _bounded_evidence_files(evidence_root)
    for path in files:
        payload = path.read_bytes()
        updated = payload
        for secret in encoded_values:
            hits = updated.count(secret)
            if hits:
                hit_count += hits
                updated = updated.replace(secret, replacement)
        if updated != payload:
            path.write_bytes(updated)
    _evidence_tree_size_bytes(evidence_root)
    _assert_evidence_secret_free(evidence_root, values)
    return hit_count


def _finalize_cleanup_secret_scan(
    evidence_root: Path,
    cleanup_report: dict[str, object],
    secret_values: tuple[str, ...],
) -> int:
    redaction_count = _redact_evidence_secrets(evidence_root, secret_values)
    cleanup_report.update(
        {
            "evidence_secret_scan_passed": True,
            "evidence_secret_value_count": len(_encoded_secret_values(secret_values)),
            "evidence_secret_redaction_count": redaction_count,
            "evidence_root_bytes_after_adapter_close": _adapter_evidence_size_bytes(
                evidence_root
            ),
        }
    )
    validated = CleanupReport.model_validate(cleanup_report)
    cleanup_report.clear()
    cleanup_report.update(validated.model_dump(mode="json", by_alias=True))
    _write_json(evidence_root / "cleanup-report.json", cleanup_report)
    _evidence_tree_size_bytes(evidence_root)
    _assert_evidence_secret_free(evidence_root, secret_values)
    return redaction_count


def _evidence_catalog(adapter_id: str) -> dict[str, EvidenceKind]:
    prefix = f"evidence://{adapter_id}"
    return {
        f"{prefix}/protocol-transcript": EvidenceKind.PROTOCOL_TRANSCRIPT,
        f"{prefix}/process-lifecycle": EvidenceKind.PROCESS_LIFECYCLE,
        f"{prefix}/capability-snapshot": EvidenceKind.CAPABILITY_SNAPSHOT,
        f"{prefix}/workspace-before": EvidenceKind.WORKSPACE_SNAPSHOT,
        f"{prefix}/workspace-after": EvidenceKind.WORKSPACE_SNAPSHOT,
        f"{prefix}/cleanup-report": EvidenceKind.CLEANUP_REPORT,
        f"{prefix}/isolation-report": EvidenceKind.ISOLATION_REPORT,
    }


def _assertion_results(
    case: HarnessCase,
    *,
    adapter_id: str,
    transport_handshake_passed: bool,
    capability_snapshot_status: AssertionStatus,
    skill_isolation_status: AssertionStatus,
    project_unchanged: bool,
    source_fixture_unchanged: bool,
) -> tuple[list[AssertionResult], dict[str, EvidenceKind]]:
    evidence = _evidence_catalog(adapter_id)
    pass_ids = {
        "assert.fixture-integrity": source_fixture_unchanged,
        "assert.transport-handshake": transport_handshake_passed,
        "assert.cleanup-fixture": source_fixture_unchanged,
    }
    blocked_ids = {"assert.partial-hunks-only", "assert.stale-conflict"}
    kind_refs: dict[EvidenceKind, str] = {
        kind: ref for ref, kind in evidence.items()
    }
    kind_refs[EvidenceKind.WORKSPACE_SNAPSHOT] = (
        f"evidence://{adapter_id}/workspace-after"
    )

    results: list[AssertionResult] = []
    for spec in case.assertions:
        if spec.assertion_id == "assert.capability-snapshot":
            refs = [kind_refs[kind] for kind in spec.required_evidence_kinds]
            message = {
                AssertionStatus.PASS: "Every required capability was measured by this run.",
                AssertionStatus.FAIL: "At least one required capability was not declared.",
                AssertionStatus.NOT_MEASURED: (
                    "The required capabilities were declared by schema or routes, but this run "
                    "did not measure their product semantics."
                ),
                AssertionStatus.BLOCKED: "Capability measurement is blocked by a prerequisite.",
            }[capability_snapshot_status]
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=capability_snapshot_status,
                    expected=spec.expected,
                    observed_ref=refs[0],
                    evidence_refs=refs,
                    failure_fingerprint=(
                        _failure_fingerprint(message)
                        if capability_snapshot_status is AssertionStatus.FAIL
                        else None
                    ),
                    message=message,
                )
            )
        elif spec.assertion_id == "assert.skill-isolation":
            refs = [kind_refs[kind] for kind in spec.required_evidence_kinds]
            message = {
                AssertionStatus.PASS: "Skill discovery stayed within the declared boundary.",
                AssertionStatus.FAIL: "Skill discovery escaped the declared boundary.",
                AssertionStatus.NOT_MEASURED: (
                    "The process roots are isolated, but this run did not verify the kernel's "
                    "effective Skill discovery result."
                ),
                AssertionStatus.BLOCKED: "Skill isolation is blocked by an unavailable prerequisite.",
            }[skill_isolation_status]
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=skill_isolation_status,
                    expected=spec.expected,
                    observed_ref=refs[0] if refs else None,
                    evidence_refs=refs,
                    failure_fingerprint=(
                        _failure_fingerprint(message)
                        if skill_isolation_status is AssertionStatus.FAIL
                        else None
                    ),
                    message=message,
                )
            )
        elif spec.assertion_id == "assert.project-bound-readonly":
            refs = [kind_refs[kind] for kind in spec.required_evidence_kinds]
            passed = project_unchanged
            status = AssertionStatus.NOT_MEASURED if passed else AssertionStatus.FAIL
            message = (
                "The disposable project fingerprint stayed unchanged, but this transport-only "
                "run did not attempt a denied write, so read-only enforcement is not measured."
                if passed
                else "The disposable project fingerprint changed during the transport-only run."
            )
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=status,
                    expected=spec.expected,
                    observed_ref=refs[0],
                    evidence_refs=refs,
                    failure_fingerprint=(
                        None if passed else _failure_fingerprint(message)
                    ),
                    message=message,
                )
            )
        elif spec.assertion_id in pass_ids:
            passed = pass_ids[spec.assertion_id]
            refs = [kind_refs[kind] for kind in spec.required_evidence_kinds]
            message = (
                "Transport evidence satisfies this assertion."
                if passed
                else "Transport evidence contradicts this assertion."
            )
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
                    expected=spec.expected,
                    observed_ref=refs[0],
                    evidence_refs=refs,
                    failure_fingerprint=None if passed else _failure_fingerprint(message),
                    message=message,
                )
            )
        elif spec.assertion_id in blocked_ids:
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=AssertionStatus.BLOCKED,
                    expected=spec.expected,
                    observed_ref=None,
                    evidence_refs=[],
                    failure_fingerprint=None,
                    message=(
                        "Formal ChangeSet transaction and hunk application are not implemented; "
                        "the disposable harness must not masquerade as product writeback."
                    ),
                )
            )
        else:
            results.append(
                AssertionResult(
                    assertion_id=spec.assertion_id,
                    gate=spec.gate,
                    operator=spec.operator,
                    status=AssertionStatus.NOT_MEASURED,
                    expected=spec.expected,
                    observed_ref=None,
                    evidence_refs=[],
                    failure_fingerprint=None,
                    message="This transport-only run did not use a Provider or execute the workflow.",
                )
            )
    validate_assertion_results(case, results, evidence_kinds=evidence)
    return results, evidence


def _write_manifest(evidence_root: Path) -> None:
    files = []
    evidence_files, _ = _bounded_evidence_files(evidence_root)
    for path in evidence_files:
        if path.name == "manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(evidence_root).as_posix(),
                "sha256": _file_hash(path),
                "size_bytes": path.stat().st_size,
            }
        )
    index_path = evidence_root / "evidence-index.json"
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        validate_evidence_index(evidence_root, index)
        manifest_files = {item["path"]: item for item in files}
        for entry in index.get("entries", []):
            for location in entry.get("locations", []):
                target = manifest_files.get(location.get("path"))
                if target is None or target["sha256"] != location.get("sha256"):
                    raise ValueError(
                        f"evidence index is not bound to the manifest: {entry.get('uri')}"
                    )
    manifest = {
        "schema": "zhimeng.kernel-harness-evidence-manifest.v2",
        "evidence_index": "evidence-index.json" if index_path.is_file() else None,
        "files": files,
    }
    _write_json(evidence_root / "manifest.json", manifest)
    _evidence_tree_size_bytes(evidence_root)
    _validate_manifest(evidence_root)


def _validate_manifest(evidence_root: Path) -> None:
    manifest_path = evidence_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = {item["path"]: item for item in manifest.get("files", [])}
    evidence_files, _ = _bounded_evidence_files(evidence_root)
    actual = {
        path.relative_to(evidence_root).as_posix(): path
        for path in evidence_files
        if path.name != "manifest.json"
    }
    if set(listed) != set(actual):
        raise ValueError("kernel evidence manifest file set does not match the sealed tree")
    for relative, path in actual.items():
        entry = listed[relative]
        if entry.get("sha256") != _file_hash(path) or entry.get("size_bytes") != path.stat().st_size:
            raise ValueError(f"kernel evidence manifest entry drifted: {relative}")
    index_path = evidence_root / "evidence-index.json"
    if index_path.is_file():
        validate_evidence_index(
            evidence_root,
            json.loads(index_path.read_text(encoding="utf-8")),
        )


async def _run_adapter(
    name: str,
    *,
    case_file: Path,
    case: HarnessCase,
    output_root: Path,
    codex_binary: Path,
    opencode_binary: Path,
) -> dict[str, object]:
    run_root, workspace_root, evidence_root, isolation_root = (
        _prepare_adapter_output_roots(output_root, name)
    )
    handshake_step = _case_step(case, HarnessOperation.TRANSPORT_HANDSHAKE)
    cleanup_step = _case_step(case, HarnessOperation.CLEANUP)
    execution_contract = _transport_execution_contract(case)
    workspace = prepare_disposable_fixture(case_file, case, workspace_root)
    _, (workspace_root, evidence_root, isolation_root) = _assert_adapter_output_layout(
        output_root,
        run_root,
        (workspace_root, evidence_root, isolation_root),
    )
    source_root = case_file.parent
    source_before = tree_fingerprint(source_root)
    project_before = tree_fingerprint(workspace.project)
    if name == "codex":
        adapter = CodexAppServerAdapter(
            binary=codex_binary,
            project_root=workspace.project,
            skill_roots=[workspace.skills / "executor-test-skill"],
            isolation_root=isolation_root,
            evidence_root=evidence_root,
        )
    elif name == "opencode":
        adapter = OpenCodeServerAdapter(
            binary=opencode_binary,
            project_root=workspace.project,
            isolation_root=isolation_root,
            evidence_root=evidence_root,
        )

    probe = None
    probe_error: str | None = None
    close_error: str | None = None
    secret_values = adapter.evidence_secret_values
    try:
        probe = await _await_case_step(
            case,
            HarnessOperation.TRANSPORT_HANDSHAKE,
            adapter.probe(),
        )
    except Exception as exc:
        probe_error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await _await_case_step(
                case,
                HarnessOperation.CLEANUP,
                adapter.close(),
            )
        except Exception as exc:
            close_error = f"{type(exc).__name__}: {exc}"
    adapter_resource_limits = adapter.resource_limits
    event_buffer_limits = adapter_resource_limits.get("event_buffer")
    event_buffer_overflowed = (
        isinstance(event_buffer_limits, dict)
        and event_buffer_limits.get("overflowed") is True
    )
    resource_limits_passed = (
        adapter_resource_limits.get("adapter_stream_truncated") is False
        and not event_buffer_overflowed
    )
    probe_error = _redact_sensitive_text(probe_error, secret_values)
    close_error = _redact_sensitive_text(close_error, secret_values)
    if close_error is not None:
        probe_error = (
            close_error
            if probe_error is None
            else f"{probe_error}; adapter close failed: {close_error}"
        )
        probe = None

    project_after = tree_fingerprint(workspace.project)
    source_after = tree_fingerprint(source_root)
    project_unchanged = project_before == project_after
    source_unchanged = source_before == source_after
    cleanup_status = (
        CleanupStatus.FAILED
        if close_error is not None or not resource_limits_passed
        else CleanupStatus.DIRTY
        if not project_unchanged or not source_unchanged
        else CleanupStatus.PARTIAL
    )
    snapshot = {
        "schema": "zhimeng.kernel-harness-workspace-snapshot.v1",
        "project_before": project_before,
        "project_after": project_after,
        "project_unchanged": project_unchanged,
        "source_fixture_before": source_before,
        "source_fixture_after": source_after,
        "source_fixture_unchanged": source_unchanged,
    }
    _write_json(evidence_root / "workspace-snapshots.json", snapshot)
    cleanup_report: dict[str, object] = {
        "schema": "zhimeng.kernel-harness-cleanup.v3",
        "overall_status": cleanup_status.value,
        "source_fixture_unchanged": source_unchanged,
        "disposable_project_snapshot_unchanged": project_unchanged,
        "write_denial_exercised": False,
        "provider_used": False,
        "user_root_snapshot_measured": False,
        "root_process_close_status": "pass" if close_error is None else "fail",
        "process_tree_cleanup_status": "not_measured",
        "process_cleanup_error": close_error,
        "timed_step_deadlines": {
            handshake_step.step_id: handshake_step.timeout_seconds,
            cleanup_step.step_id: cleanup_step.timeout_seconds,
        },
        "execution_contract": execution_contract,
        "unmeasured_cleanup_dimensions": ["process_tree", "user_roots"],
        "resource_limits_passed": resource_limits_passed,
        "adapter_resource_limits": adapter_resource_limits,
        "evidence_root_byte_limit": EVIDENCE_ROOT_LIMIT_BYTES,
        "evidence_root_bytes_after_adapter_close": 0,
    }

    if probe is None:
        _write_json(
            evidence_root / "probe-error.json",
            {
                "schema": "zhimeng.kernel-transport-probe-error.v1",
                "adapter": name,
                "error": probe_error,
            },
        )
        _finalize_cleanup_secret_scan(evidence_root, cleanup_report, secret_values)
        _write_manifest(evidence_root)
        _assert_evidence_secret_free(evidence_root, secret_values)
        _validate_manifest(evidence_root)
        return {
            "adapter": name,
            "status": "failed",
            "harness_state": "failed",
            "error": probe_error,
            "evidence_root": str(evidence_root),
        }

    _write_json(
        evidence_root / "transport-probe.json",
        probe.model_dump(mode="json", by_alias=True),
    )
    isolation_status_value = probe.metadata.get("skill_isolation_status")
    try:
        skill_isolation_status = AssertionStatus(str(isolation_status_value))
    except ValueError:
        skill_isolation_status = AssertionStatus.NOT_MEASURED
    _write_json(
        evidence_root / "isolation-report.json",
        {
            "schema": "zhimeng.kernel-harness-isolation-report.v1",
            "adapter": probe.adapter.executor_id,
            "skill_isolation_status": skill_isolation_status.value,
            "unexpected_skill_count": probe.metadata.get("unexpected_skill_count"),
            "process_root_isolation_passed": probe.metadata.get(
                "process_root_isolation_passed"
            ),
            "credential_environment_forwarded": probe.metadata.get(
                "credential_environment_forwarded"
            ),
        },
    )
    transport_handshake_checks = [
        check
        for check in probe.checks
        if not check.check_id.endswith(".isolation")
    ]
    transport_handshake_passed = bool(transport_handshake_checks) and all(
        check.passed for check in transport_handshake_checks
    )
    capability_assertion = next(
        spec
        for spec in case.assertions
        if spec.assertion_id == "assert.capability-snapshot"
    )
    expected_value = capability_assertion.expected
    if not isinstance(expected_value, list) or not all(
        isinstance(value, str) for value in expected_value
    ):
        raise ValueError("capability snapshot assertion must expect a string list")
    expected_capabilities = set(expected_value)
    declared_capabilities = {
        capability.value for capability in probe.capabilities.declared
    }
    measured_capabilities = {
        capability.value for capability in probe.capabilities.measured
    }
    if measured_capabilities.issuperset(expected_capabilities):
        capability_snapshot_status = AssertionStatus.PASS
    elif declared_capabilities.issuperset(expected_capabilities):
        capability_snapshot_status = AssertionStatus.NOT_MEASURED
    else:
        capability_snapshot_status = AssertionStatus.FAIL
    results, _ = _assertion_results(
        case,
        adapter_id=probe.adapter.executor_id,
        transport_handshake_passed=transport_handshake_passed,
        capability_snapshot_status=capability_snapshot_status,
        skill_isolation_status=skill_isolation_status,
        project_unchanged=project_unchanged,
        source_fixture_unchanged=source_unchanged,
    )
    terminal_state = terminal_state_for_run(
        case,
        results,
        cleanup_status=cleanup_status,
    )
    now = datetime.now(UTC)
    receipt = HarnessRun(
        schema="zhimeng.kernel-harness-run.v2",
        harness_run_id=f"harness.{name}.{now.strftime('%Y%m%dT%H%M%S')}",
        case_id=case.case_id,
        case_version=case.version,
        case_fingerprint=canonical_case_fingerprint(case),
        state=terminal_state,
        adapter=probe.adapter,
        binary_hash=probe.binary_hash,
        protocol_schema_hash=probe.protocol_schema_hash,
        config_hash=probe.config_hash,
        provider_model=None,
        normalized_session_id=(
            None if probe.session is None else probe.session.normalized_session_id
        ),
        normalized_run_ids=[],
        case_step_ids=[step.step_id for step in case.steps],
        case_assertion_ids=[item.assertion_id for item in case.assertions],
        step_cursor=len(case.steps),
        created_at=now,
        updated_at=now,
        completed_at=now,
        baseline_snapshot_ref=f"evidence://{probe.adapter.executor_id}/workspace-before",
        final_snapshot_ref=f"evidence://{probe.adapter.executor_id}/workspace-after",
        observation_count=0,
        assertion_results=results,
        cleanup_status=cleanup_status,
    )
    validate_harness_run_against_case(case, receipt)
    _write_json(
        evidence_root / "harness-run.json",
        receipt.model_dump(mode="json", by_alias=True),
    )
    _finalize_cleanup_secret_scan(evidence_root, cleanup_report, secret_values)
    receipt_refs = [receipt.baseline_snapshot_ref, receipt.final_snapshot_ref]
    for result in receipt.assertion_results:
        if result.observed_ref is not None:
            receipt_refs.append(result.observed_ref)
        receipt_refs.extend(result.evidence_refs)
    _write_json(
        evidence_root / "evidence-index.json",
        build_evidence_index(
            evidence_root,
            adapter_name=name,
            adapter_id=probe.adapter.executor_id,
            catalog=_evidence_catalog(probe.adapter.executor_id),
            probe_evidence_refs=[check.evidence_ref for check in probe.checks],
            receipt_evidence_refs=receipt_refs,
        ),
    )
    _write_manifest(evidence_root)
    _assert_evidence_secret_free(evidence_root, secret_values)
    _validate_manifest(evidence_root)
    return {
        "adapter": name,
        "transport_passed": probe.passed,
        "harness_state": terminal_state.value,
        "evidence_root": str(evidence_root),
    }


async def async_main(arguments: argparse.Namespace) -> list[dict[str, object]]:
    case_file = arguments.case.resolve(strict=True)
    case = HarnessCase.model_validate_json(case_file.read_text(encoding="utf-8"))
    if case.lane is not HarnessLane.TRANSPORT:
        raise ValueError(
            "transport probe requires a case with lane='transport'; "
            "compatible-provider and native-provider cases must use their own runner"
        )
    output_root = _resolve_safe_output_root(arguments.output)
    output_root.mkdir(parents=True, exist_ok=True)
    names = ["codex", "opencode"] if arguments.adapter == "all" else [arguments.adapter]
    results = []
    for name in names:
        results.append(
            await _run_adapter(
                name,
                case_file=case_file,
                case=case,
                output_root=output_root,
                codex_binary=arguments.codex_binary,
                opencode_binary=arguments.opencode_binary,
            )
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run credential-free transport probes against isolated Agent Kernels."
    )
    parser.add_argument("--adapter", choices=("codex", "opencode", "all"), default="all")
    parser.add_argument("--case", type=Path, default=DEFAULT_CASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex-binary", type=Path, default=DEFAULT_CODEX)
    parser.add_argument("--opencode-binary", type=Path, default=DEFAULT_OPENCODE)
    return parser.parse_args()


def main() -> None:
    results = asyncio.run(async_main(parse_args()))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    exit_code = _cli_exit_code(results)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
