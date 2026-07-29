"""Isolated deterministic adapter previews for the novel runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List


ADAPTER_ID = "nlcl.prepare-creation-handoff.v1"
REQUEST_SCHEMA = "zhimeng.writing-adapter-preview-request.v1"
RESPONSE_SCHEMA = "zhimeng.writing-adapter-preview.v1"
MAX_SOURCE_FILES = 50_000
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_SOURCE_FILES = 10_000
MAX_TOTAL_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_RUNS = 50
MAX_RUNTIME_RUN_BYTES = 512 * 1024 * 1024
MIN_RUNTIME_FREE_BYTES = 512 * 1024 * 1024
MAX_PROCESS_OUTPUT_CHARS = 80_000
REPARSE_POINT_ATTRIBUTE = 0x400


class AdapterPreviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    version: str
    kb_script: Path
    kb_script_sha256: str
    handoff_contract: Path
    handoff_contract_sha256: str
    consumer_script: Path
    consumer_script_sha256: str
    distill_script: Path
    distill_script_sha256: str
    kb_library: Path
    distillation_library: Path
    timeout_seconds: int = 180


def default_adapter_spec() -> AdapterSpec:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()
    skills = codex_home / "skills"
    return AdapterSpec(
        adapter_id=ADAPTER_ID,
        version="1.0.0",
        kb_script=skills / "novel-kb-manager" / "scripts" / "kb_manager.py",
        kb_script_sha256="2104f7f043629f7902a66517b1a807f02dbc0b35a0a5ac414349a2a12d715d7c",
        handoff_contract=skills / "novel-kb-manager" / "references" / "handoff-api-contract.json",
        handoff_contract_sha256="bdc9d26337b28d5de8c7b26b02e6d0c53e21cfccb104e6dd7268975fafa8aa98",
        consumer_script=skills / "novel-creation-suite" / "scripts" / "consume_kb_manager_context.py",
        consumer_script_sha256="5ad440033d50d64fcabd98f297fc02f9f27800ed131b4c5990644fda3c64c1bf",
        distill_script=skills / "novel-distillation" / "scripts" / "distill_kb.py",
        distill_script_sha256="8dd36dacb2180db75bce2bb9f29c778da325882b54aea8b041b5e22000ca79a2",
        kb_library=skills / "novel-kb-manager" / "library",
        distillation_library=skills / "novel-distillation" / "library",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_file_digest(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AdapterPreviewError(
            "WRITING_ADAPTER_SOURCE_CHANGED",
            f"Source changed while hashing: {path}",
        )
    return after.st_size, digest.hexdigest()


def _is_link_like(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AdapterPreviewError("WRITING_ADAPTER_SOURCE_CHANGED", f"Cannot inspect source path: {path}: {exc}") from exc
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)


def _walk_files(
    root: Path,
    include: Callable[[str], bool] | None = None,
    descend: Callable[[str], bool] | None = None,
) -> List[Path]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or _is_link_like(resolved):
        raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Source root is not a plain directory: {resolved}")
    pending = [resolved]
    files: List[Path] = []
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise AdapterPreviewError("WRITING_ADAPTER_SOURCE_CHANGED", f"Cannot enumerate source: {directory}: {exc}") from exc
        for child in children:
            relative = child.relative_to(resolved).as_posix()
            if _is_link_like(child):
                raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Links and junctions are not allowed in adapter input: {child}")
            if child.is_dir() and (descend is None or descend(relative)):
                pending.append(child)
            elif child.is_file() and (include is None or include(relative)):
                files.append(child)
                if len(files) > MAX_SOURCE_FILES:
                    raise AdapterPreviewError("WRITING_ADAPTER_INPUT_TOO_LARGE", f"Too many source files under {resolved}")
    return sorted(files, key=lambda item: item.relative_to(resolved).as_posix().casefold())


def _tree_manifest(
    role: str,
    root: Path,
    *,
    include: Callable[[str], bool] | None = None,
    descend: Callable[[str], bool] | None = None,
) -> Dict[str, Any]:
    resolved = root.resolve(strict=True)
    records: List[Dict[str, Any]] = []
    total_bytes = 0
    for path in _walk_files(resolved, include, descend):
        size, digest = _stable_file_digest(path)
        total_bytes += size
        if total_bytes > MAX_SOURCE_BYTES:
            raise AdapterPreviewError("WRITING_ADAPTER_INPUT_TOO_LARGE", f"Source tree exceeds byte limit: {resolved}")
        records.append({
            "relative_path": path.relative_to(resolved).as_posix(),
            "bytes": size,
            "sha256": digest,
        })
    semantic = [{"path": item["relative_path"], "bytes": item["bytes"], "sha256": item["sha256"]} for item in records]
    return {
        "role": role,
        "path": str(resolved),
        "file_count": len(records),
        "bytes": total_bytes,
        "sha256": _sha256_bytes(_canonical_json(semantic)),
        "files": records,
    }


def source_tree_manifest(role: str, root: Path) -> Dict[str, Any]:
    """Return the bounded, link-safe content identity used by runtime CAS."""

    return _tree_manifest(role, root)


def _single_file_manifest(role: str, path: Path) -> Dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or _is_link_like(resolved):
        raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Toolchain input is not a plain file: {resolved}")
    size, digest = _stable_file_digest(resolved)
    return {
        "role": role,
        "path": str(resolved),
        "file_count": 1,
        "bytes": size,
        "sha256": digest,
        "files": [{"relative_path": resolved.name, "bytes": size, "sha256": digest}],
    }


def _manifest_identity(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "role": manifest["role"],
        "path": manifest["path"],
        "file_count": manifest["file_count"],
        "bytes": manifest["bytes"],
        "sha256": manifest["sha256"],
        "files": manifest["files"],
    }


def _copy_manifest(manifest: Dict[str, Any], destination: Path) -> None:
    source_root = Path(manifest["path"])
    destination.mkdir(parents=True, exist_ok=True)
    for record in manifest["files"]:
        source = source_root / record["relative_path"]
        target = destination / record["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        size, digest = _stable_file_digest(target)
        if size != record["bytes"] or digest != record["sha256"]:
            raise AdapterPreviewError("WRITING_ADAPTER_SOURCE_CHANGED", f"Snapshot copy mismatch: {source}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path, *, code: str = "WRITING_ADAPTER_OUTPUT_SCHEMA_INVALID") -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterPreviewError(code, f"Cannot read JSON object: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AdapterPreviewError(code, f"Expected JSON object: {path}")
    return value


def _distillation_include(relative: str) -> bool:
    first = relative.split("/", 1)[0].casefold()
    return first not in {"raw", "reports"}


def _distillation_descend(relative: str) -> bool:
    return relative.split("/", 1)[0].casefold() not in {"raw", "reports"}


def _library_support_include(relative: str) -> bool:
    return relative.split("/", 1)[0].casefold() != "projects"


def _library_support_descend(relative: str) -> bool:
    return relative.split("/", 1)[0].casefold() != "projects"


def _skill_contract_descend(relative: str) -> bool:
    parts = {part.casefold() for part in relative.split("/")}
    return "library" not in parts and "__pycache__" not in parts and ".git" not in parts


def _skill_contract_include(relative: str) -> bool:
    lowered = relative.casefold()
    return "__pycache__" not in lowered and not lowered.endswith((".pyc", ".pyo"))


def _ensure_plain_runtime_root(runtime_root: Path) -> tuple[Path, Path, Path]:
    if not re.fullmatch(r"[^<>:\"|?*]+", runtime_root.name):
        raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Invalid runtime directory name: {runtime_root.name}")
    bridge_root = runtime_root.parent.parent.absolute()
    if not bridge_root.exists() or not bridge_root.is_dir() or _is_link_like(bridge_root):
        raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime owner root is not a plain directory: {bridge_root}")
    current = bridge_root
    for part in runtime_root.absolute().relative_to(bridge_root).parts:
        current = current / part
        if current.exists():
            if not current.is_dir() or _is_link_like(current):
                raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime path contains a link or junction: {current}")
        else:
            current.mkdir()
            if _is_link_like(current):
                raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime directory became a link or junction: {current}")
    staging = current / "staging"
    runs = current / "runs"
    for directory in (staging, runs):
        if directory.exists():
            if not directory.is_dir() or _is_link_like(directory):
                raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime output path is not a plain directory: {directory}")
        else:
            directory.mkdir()
            if _is_link_like(directory):
                raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime output path became a link or junction: {directory}")
    return current, staging, runs


def _runtime_usage(directory: Path) -> tuple[int, int]:
    files = _walk_files(directory)
    total = sum(path.stat().st_size for path in files)
    return len(files), total


def _check_runtime_quota(runtime_root: Path, staging_root: Path, runs_root: Path) -> None:
    run_directories = []
    for child in runs_root.iterdir():
        if _is_link_like(child):
            raise AdapterPreviewError("WRITING_ADAPTER_UNTRUSTED_PATH", f"Runtime run path is a link or junction: {child}")
        if child.is_dir():
            run_directories.append(child)
    _, run_bytes = _runtime_usage(runs_root)
    _, staging_bytes = _runtime_usage(staging_root)
    if len(run_directories) >= MAX_RUNTIME_RUNS or run_bytes >= MAX_RUNTIME_RUN_BYTES:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_RUNTIME_QUOTA_EXCEEDED",
            "Writing preview run quota is full; archive or remove reviewed preview runs before continuing",
            details={"run_count": len(run_directories), "run_bytes": run_bytes},
        )
    if staging_bytes >= MAX_TOTAL_SOURCE_BYTES:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_RUNTIME_QUOTA_EXCEEDED",
            "Writing preview staging quota is full; inspect the retained interrupted run before continuing",
            details={"staging_bytes": staging_bytes},
        )
    free = shutil.disk_usage(runtime_root).free
    if free < MIN_RUNTIME_FREE_BYTES:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_RUNTIME_QUOTA_EXCEEDED",
            "Insufficient free disk space for an isolated preview",
            details={"free_bytes": free, "required_free_bytes": MIN_RUNTIME_FREE_BYTES},
        )


def _assert_total_input_budget(manifests: Iterable[Dict[str, Any]], runtime_root: Path) -> None:
    values = list(manifests)
    file_count = sum(int(item["file_count"]) for item in values)
    byte_count = sum(int(item["bytes"]) for item in values)
    if file_count > MAX_TOTAL_SOURCE_FILES or byte_count > MAX_TOTAL_SOURCE_BYTES:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_INPUT_TOO_LARGE",
            "Combined adapter input exceeds the isolated preview budget",
            details={"file_count": file_count, "bytes": byte_count},
        )
    free = shutil.disk_usage(runtime_root).free
    required = (byte_count * 2) + MIN_RUNTIME_FREE_BYTES
    if free < required:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_RUNTIME_QUOTA_EXCEEDED",
            "Insufficient free disk space for the complete preview snapshot",
            details={"free_bytes": free, "required_free_bytes": required, "input_bytes": byte_count},
        )


def _verify_toolchain(spec: AdapterSpec) -> List[Dict[str, Any]]:
    pins = [
        ("nlcl_script", spec.kb_script, spec.kb_script_sha256),
        ("handoff_contract", spec.handoff_contract, spec.handoff_contract_sha256),
        ("creation_consumer", spec.consumer_script, spec.consumer_script_sha256),
        ("distillation_profile_reader", spec.distill_script, spec.distill_script_sha256),
    ]
    result: List[Dict[str, Any]] = []
    for role, path, expected in pins:
        manifest = _single_file_manifest(role, path)
        actual = str(manifest["sha256"]).casefold()
        if actual != expected.casefold():
            raise AdapterPreviewError(
                "WRITING_ADAPTER_SCRIPT_CHANGED",
                f"Pinned toolchain file changed: {path}",
                details={"path": str(path), "expected_sha256": expected, "actual_sha256": actual},
            )
        result.append(manifest)
    return result


def _stage_toolchain(
    spec: AdapterSpec,
    staging_root: Path,
    source_manifests: Dict[str, Dict[str, Any]],
) -> Dict[str, Path]:
    staged_skills_root = staging_root / "toolchain" / "skills"
    for role, skill_name in (
        ("novel_kb_manager_skill", "novel-kb-manager"),
        ("novel_creation_suite_skill", "novel-creation-suite"),
        ("novel_distillation_skill", "novel-distillation"),
        ("tomato_auto_distill_skill", "tomato-novel-auto-distill"),
    ):
        manifest = source_manifests.get(role)
        if manifest is None:
            raise AdapterPreviewError("WRITING_ADAPTER_SOURCE_CHANGED", f"Missing toolchain manifest: {role}")
        _copy_manifest(manifest, staged_skills_root / skill_name)
    staged = {
        "kb_script": staged_skills_root / "novel-kb-manager" / "scripts" / spec.kb_script.name,
        "handoff_contract": staged_skills_root / "novel-kb-manager" / "references" / spec.handoff_contract.name,
        "consumer_script": staged_skills_root / "novel-creation-suite" / "scripts" / spec.consumer_script.name,
        "distill_script": staged_skills_root / "novel-distillation" / "scripts" / spec.distill_script.name,
    }
    sources = {
        "kb_script": (spec.kb_script, spec.kb_script_sha256),
        "handoff_contract": (spec.handoff_contract, spec.handoff_contract_sha256),
        "consumer_script": (spec.consumer_script, spec.consumer_script_sha256),
        "distill_script": (spec.distill_script, spec.distill_script_sha256),
    }
    for key, target in staged.items():
        source, expected_sha256 = sources[key]
        if not target.is_file() or _is_link_like(target):
            raise AdapterPreviewError("WRITING_ADAPTER_SOURCE_CHANGED", f"Staged toolchain file is missing: {source}")
        if _stable_file_digest(target)[1].casefold() != expected_sha256.casefold():
            raise AdapterPreviewError(
                "WRITING_ADAPTER_SOURCE_CHANGED",
                f"Staged toolchain hash mismatch: {source}",
            )
    return staged


def _source_manifests(project_root: Path, kb_root: Path, spec: AdapterSpec) -> List[Dict[str, Any]]:
    skills_root = spec.kb_script.resolve().parents[2]
    skill_contracts = [
        ("novel_kb_manager_skill", spec.kb_script.resolve().parents[1]),
        ("novel_creation_suite_skill", spec.consumer_script.resolve().parents[1]),
        ("novel_distillation_skill", spec.distill_script.resolve().parents[1]),
        ("tomato_auto_distill_skill", skills_root / "tomato-novel-auto-distill"),
    ]
    return [
        _tree_manifest("creation_project", project_root),
        _tree_manifest("knowledge_base", kb_root),
        _tree_manifest(
            "nlcl_library_support",
            spec.kb_library,
            include=_library_support_include,
            descend=_library_support_descend,
        ),
        _tree_manifest(
            "distillation_cards",
            spec.distillation_library,
            include=_distillation_include,
            descend=_distillation_descend,
        ),
        *[
            _tree_manifest(role, root, include=_skill_contract_include, descend=_skill_contract_descend)
            for role, root in skill_contracts
        ],
        *_verify_toolchain(spec),
    ]


def _assert_sources_unchanged(before: Iterable[Dict[str, Any]], after: Iterable[Dict[str, Any]]) -> None:
    before_values = [_manifest_identity(item) for item in before]
    after_values = [_manifest_identity(item) for item in after]
    if before_values != after_values:
        changed = []
        after_by_role = {item["role"]: item for item in after_values}
        for item in before_values:
            current = after_by_role.get(item["role"])
            if current != item:
                changed.append({
                    "role": item["role"],
                    "before_sha256": item["sha256"],
                    "after_sha256": current.get("sha256", "missing") if current else "missing",
                })
        raise AdapterPreviewError(
            "WRITING_ADAPTER_UNEXPECTED_SOURCE_WRITE",
            "Adapter source inputs changed during preview",
            details={"changed": changed},
        )


def _patch_snapshot_binding(snapshot_kb: Path, snapshot_project: Path) -> Dict[str, Any]:
    manifest_path = snapshot_kb / "manifest.json"
    manifest = _read_json(manifest_path, code="WRITING_ADAPTER_OUTPUT_SCHEMA_INVALID")
    project_name = str(manifest.get("project_name") or snapshot_kb.stem).strip()
    if not project_name:
        raise AdapterPreviewError("WRITING_ADAPTER_SCOPE_MISMATCH", "KB manifest has no project_name")
    previous = str(manifest.get("creation_project_root") or "")
    manifest["creation_project_root"] = str(snapshot_project.resolve())
    manifest["creation_project_binding_status"] = "bound"
    _write_json(manifest_path, manifest)
    return {
        "path": str(manifest_path),
        "field": "creation_project_root",
        "source_value": previous,
        "snapshot_value": str(snapshot_project.resolve()),
        "project_name": project_name,
        "genre": str(manifest.get("genre") or ""),
        "platform": str(manifest.get("platform") or ""),
        "style": str(manifest.get("style_target") or manifest.get("style") or ""),
    }


def _operation_package_characters(snapshot_kb: Path, chapter_id: str) -> List[str]:
    context_dir = snapshot_kb / "chapter_context"
    candidates = sorted(context_dir.glob(f"{chapter_id}_operation_package_like*.json"), key=lambda item: item.name.casefold())
    for path in reversed(candidates):
        try:
            payload = _read_json(path)
        except AdapterPreviewError:
            continue
        operation = payload.get("operation_package_like") if isinstance(payload.get("operation_package_like"), dict) else payload
        state = operation.get("current_character_state") if isinstance(operation, dict) else None
        if isinstance(state, dict):
            return sorted({str(key).strip() for key in state if str(key).strip()})
        if isinstance(state, list):
            names = []
            for item in state:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("character_name") or item.get("character_id") or "").strip()
                    if name:
                        names.append(name)
            if names:
                return sorted(set(names))
    return []


def _domain_gap_codes(snapshot_project: Path, snapshot_kb: Path, chapter_id: str) -> List[str]:
    gaps: List[str] = []

    def state(name: str) -> Dict[str, Any]:
        path = snapshot_kb / "state" / name
        return _read_json(path) if path.exists() else {}

    project_status = state("project_status.json")
    current_task = project_status.get("current_task")
    current_chapter = str(current_task.get("chapter_id") if isinstance(current_task, dict) else current_task or "").strip()
    if current_chapter != chapter_id:
        gaps.append("PROJECT_STATUS_CHAPTER_MISMATCH")

    control_cards = sorted(snapshot_project.rglob(f"{chapter_id}*控制卡*.md"), key=lambda item: str(item).casefold())
    control_text = "\n".join(path.read_text(encoding="utf-8-sig", errors="replace") for path in control_cards)
    if "continuation_state_contract" not in control_text:
        gaps.append("SPEC_CONTINUATION_STATE_CONTRACT_MISSING")

    info_boundary = state("truth_info_boundary.json")
    if not isinstance(info_boundary.get("entities"), dict) or not info_boundary.get("entities"):
        gaps.append("TRUTH_INFO_BOUNDARY_EMPTY")

    character_matrix = state("truth_character_matrix.json")
    if not isinstance(character_matrix.get("relationship_edges"), list) or not character_matrix.get("relationship_edges"):
        gaps.append("TRUTH_RELATIONSHIP_EDGES_EMPTY")

    plot_state = state("plot_state.json")
    active_threads = plot_state.get("active_plot_threads")
    if not isinstance(active_threads, list) or not active_threads:
        gaps.append("PLOT_ACTIVE_THREADS_UNSTRUCTURED")

    foreshadowing = state("foreshadowing_state.json")
    if not isinstance(foreshadowing.get("available_evidence"), list) or not foreshadowing.get("available_evidence"):
        gaps.append("FORESHADOW_AVAILABLE_EVIDENCE_EMPTY")
    return gaps


def _run_process(argv: List[str], cwd: Path, timeout_seconds: int) -> Dict[str, Any]:
    sandbox_home = cwd / "process-home"
    sandbox_temp = cwd / "process-temp"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    sandbox_temp.mkdir(parents=True, exist_ok=True)
    passthrough = {
        "COMSPEC",
        "LANG",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in passthrough}
    environment["HOME"] = str(sandbox_home)
    environment["USERPROFILE"] = str(sandbox_home)
    environment["TEMP"] = str(sandbox_temp)
    environment["TMP"] = str(sandbox_temp)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["ZHIMENG_WRITING_SANDBOX_ROOT"] = str(cwd)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterPreviewError("WRITING_ADAPTER_TIMEOUT", f"Adapter command timed out after {timeout_seconds}s") from exc
    return {
        "argv": argv,
        "cwd": str(cwd),
        "environment_policy": "isolated-minimal-v1",
        "returncode": completed.returncode,
        "stdout": completed.stdout[-MAX_PROCESS_OUTPUT_CHARS:],
        "stderr": completed.stderr[-MAX_PROCESS_OUTPUT_CHARS:],
    }


def _scope_route(context_pack: Dict[str, Any]) -> Dict[str, str]:
    effective = context_pack.get("effective_route")
    if not isinstance(effective, dict):
        effective = context_pack.get("requested_route") if isinstance(context_pack.get("requested_route"), dict) else {}
    return {
        "genre": str(effective.get("genre") or ""),
        "style": str(effective.get("style") or effective.get("style_target") or ""),
        "platform": str(effective.get("platform") or ""),
    }


def _validate_outputs(
    context_pack: Dict[str, Any],
    handoff: Dict[str, Any],
    *,
    project_name: str,
    chapter_id: str,
    stage: str,
    context_path: Path,
    snapshot_kb: Path,
) -> tuple[str, str, Path]:
    if str(context_pack.get("project") or "") != project_name:
        raise AdapterPreviewError("WRITING_ADAPTER_SCOPE_MISMATCH", "Context pack project does not match the binding")
    if str(context_pack.get("chapter_id") or "") != chapter_id:
        raise AdapterPreviewError("WRITING_ADAPTER_SCOPE_MISMATCH", "Context pack chapter does not match the request")
    if str(context_pack.get("stage") or "") != stage:
        raise AdapterPreviewError("WRITING_ADAPTER_SCOPE_MISMATCH", "Context pack stage does not match the request")
    scope = handoff.get("scope") if isinstance(handoff.get("scope"), dict) else {}
    if str(scope.get("project") or "") != project_name or str(scope.get("chapter_id") or "") != chapter_id:
        raise AdapterPreviewError("WRITING_ADAPTER_SCOPE_MISMATCH", "Creation handoff scope does not match the request")
    context_sha = _stable_file_digest(context_path)[1]
    source_context = handoff.get("source_context_pack") if isinstance(handoff.get("source_context_pack"), dict) else {}
    if str(source_context.get("sha256") or "").casefold() != context_sha:
        raise AdapterPreviewError("WRITING_ADAPTER_HANDOFF_HASH_MISMATCH", "Creation handoff context pack hash mismatch")
    operation_raw = str(
        context_pack.get("operation_package_like_json")
        or context_pack.get("operation_package_like_latest_json")
        or ""
    ).strip()
    if not operation_raw:
        raise AdapterPreviewError("WRITING_ADAPTER_OUTPUT_MISSING", "Context pack did not bind a run-local operation package")
    try:
        operation_path = Path(operation_raw).resolve(strict=True)
    except OSError as exc:
        raise AdapterPreviewError("WRITING_ADAPTER_OUTPUT_MISSING", f"Operation package is missing: {operation_raw}") from exc
    try:
        operation_path.relative_to(snapshot_kb.resolve())
    except ValueError as exc:
        raise AdapterPreviewError(
            "WRITING_ADAPTER_UNTRUSTED_PATH",
            f"Operation package escaped the snapshot KB: {operation_path}",
        ) from exc
    if not operation_path.is_file() or _is_link_like(operation_path):
        raise AdapterPreviewError("WRITING_ADAPTER_OUTPUT_MISSING", f"Operation package is missing: {operation_path}")
    operation_sha = _stable_file_digest(operation_path)[1]
    return context_sha, operation_sha, operation_path


def _semantic_value(value: Any, volatile_roots: Iterable[Path]) -> Any:
    skipped_keys = {"generated_at", "created_at", "updated_at", "written_to", "timestamp"}
    replacements: List[tuple[str, str]] = []
    for root in volatile_roots:
        raw = str(root.resolve())
        replacements.extend([
            (raw, "<RUNTIME>"),
            (raw.replace("\\", "\\\\"), "<RUNTIME>"),
            (raw.replace("\\", "/"), "<RUNTIME>"),
        ])

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: visit(child) for key, child in sorted(item.items()) if key not in skipped_keys}
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            result = item
            for source, target in replacements:
                result = result.replace(source, target)
            return result
        return item

    return visit(value)


def _artifact_record(
    path: Path,
    role: str,
    semantic_value: Any,
    source_run_root: Path,
    *,
    published_run_root: Path | None = None,
) -> Dict[str, Any]:
    size, digest = _stable_file_digest(path)
    relative_path = path.relative_to(source_run_root).as_posix()
    published_path = (published_run_root or source_run_root) / relative_path
    return {
        "role": role,
        "path": str(published_path),
        "relative_path": relative_path,
        "bytes": size,
        "sha256": digest,
        "semantic_sha256": _sha256_bytes(_canonical_json(semantic_value)),
    }


def _failure_payload(
    error: AdapterPreviewError,
    *,
    adapter_id: str = ADAPTER_ID,
    run_id: str = "",
    project_id: str = "",
    binding_revision: int | None = None,
    project_fingerprint: str = "",
) -> Dict[str, Any]:
    expired_codes = {
        "WRITING_ADAPTER_BINDING_REVISION_MISMATCH",
        "WRITING_ADAPTER_PREVIEW_STALE",
        "WRITING_ADAPTER_SOURCE_CHANGED",
        "WRITING_ADAPTER_SCRIPT_CHANGED",
    }
    return {
        "schema": RESPONSE_SCHEMA,
        "status": "failed",
        "adapter_id": adapter_id,
        "adapter_version": "1.0.0",
        "preview_id": run_id,
        "run_id": run_id,
        "preview_sha256": "",
        "project_id": project_id,
        "binding_revision": binding_revision,
        "project_fingerprint": project_fingerprint,
        "adapter_state": "PREVIEW_EXPIRED" if error.code in expired_codes else "PREVIEW_FAILED",
        "domain_gate": "blocked",
        "formal_draft_allowed": False,
        "promotion_allowed": False,
        "source_writes": [],
        "proposed_source_diff": [],
        "artifacts": [],
        "warnings": [],
        "errors": [{"code": error.code, "message": str(error), "details": error.details}],
    }


def execute_adapter_preview(
    *,
    project_id: str,
    binding_revision: int,
    project_fingerprint: str,
    project_root: Path,
    kb_root: Path,
    chapter_id: str,
    stage: str,
    inspection_status: str,
    runtime_root: Path,
    run_id: str,
    event_sink: Callable[[str, str, Dict[str, Any]], None],
    spec: AdapterSpec | None = None,
    prepublish_guard: Callable[[], None] | None = None,
) -> Dict[str, Any]:
    adapter = spec or default_adapter_spec()
    runtime_root, staging_parent, runs_parent = _ensure_plain_runtime_root(runtime_root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise AdapterPreviewError("WRITING_ADAPTER_REQUEST_INVALID", f"Invalid preview run id: {run_id}")
    staging_root = staging_parent / run_id
    run_root = runs_parent / run_id
    partial_run_root = runs_parent / f".{run_id}.partial"
    failure_partial_root = runs_parent / f".{run_id}.failure.partial"
    snapshot_project = staging_root / "snapshot" / "creation_project" / project_root.name
    snapshot_library = staging_root / "snapshot" / "novel-kb-manager" / "library"
    snapshot_kb = snapshot_library / "projects" / kb_root.name
    snapshot_distillation = staging_root / "snapshot" / "novel-distillation" / "library"
    staging_artifacts = staging_root / "artifacts"
    input_manifest_path = partial_run_root / "input_manifest.json"
    output_manifest_path = partial_run_root / "output_manifest.json"
    receipt_path = partial_run_root / "execution_receipt.json"
    context_path = staging_artifacts / f"{chapter_id}_context_pack.json"
    handoff_path = staging_artifacts / f"{chapter_id}_creation_handoff.json"
    process_records: List[Dict[str, Any]] = []
    before: List[Dict[str, Any]] = []
    partial_created = False

    def publish_failure(error: AdapterPreviewError) -> Dict[str, Any]:
        failure = _failure_payload(
            error,
            adapter_id=adapter.adapter_id,
            run_id=run_id,
            project_id=project_id,
            binding_revision=binding_revision,
            project_fingerprint=project_fingerprint,
        )
        failure_receipt = {
            **failure,
            "schema": "zhimeng.writing-adapter-execution-receipt.v1",
            "processes": process_records,
            "created_at": now_iso(),
        }
        shutil.rmtree(partial_run_root, ignore_errors=True)
        shutil.rmtree(failure_partial_root, ignore_errors=True)
        if partial_created and not run_root.exists():
            failure_partial_root.mkdir(parents=True, exist_ok=False)
            staged_receipt = failure_partial_root / "execution_receipt.json"
            _write_json(staged_receipt, failure_receipt)
            os.replace(failure_partial_root, run_root)
            published_receipt = run_root / staged_receipt.name
            failure["artifacts"] = [{
                "role": "execution_receipt",
                "path": str(published_receipt),
                "relative_path": published_receipt.relative_to(run_root).as_posix(),
                "sha256": _stable_file_digest(published_receipt)[1],
            }]
        failure["staging_retained"] = False
        try:
            event_sink("preview_failed", "failed", {"code": error.code, "message": str(error)})
        except Exception:
            pass
        return failure

    try:
        _check_runtime_quota(runtime_root, staging_parent, runs_parent)
        for path in (staging_root, run_root, partial_run_root, failure_partial_root):
            if path.exists():
                raise AdapterPreviewError("WRITING_ADAPTER_RUN_CONFLICT", f"Preview run path already exists: {path}")
        partial_run_root.mkdir(parents=True, exist_ok=False)
        partial_created = True
        staging_artifacts.mkdir(parents=True, exist_ok=False)
        event_sink("preflighted", "ok", {"adapter_id": adapter.adapter_id})
        before = _source_manifests(project_root, kb_root, adapter)
        _assert_total_input_budget(before, runtime_root)
        by_role = {item["role"]: item for item in before}
        _copy_manifest(by_role["creation_project"], snapshot_project)
        _copy_manifest(by_role["knowledge_base"], snapshot_kb)
        _copy_manifest(by_role["nlcl_library_support"], snapshot_library)
        _copy_manifest(by_role["distillation_cards"], snapshot_distillation)
        staged_toolchain = _stage_toolchain(adapter, staging_root, by_role)
        snapshot_contract = staged_toolchain["handoff_contract"]
        snapshot_patch = _patch_snapshot_binding(snapshot_kb, snapshot_project)
        characters = _operation_package_characters(snapshot_kb, chapter_id)
        initial_gaps = _domain_gap_codes(snapshot_project, snapshot_kb, chapter_id)
        staged_toolchain_manifest = [
            {
                "role": role,
                "path": str(path),
                "bytes": _stable_file_digest(path)[0],
                "sha256": _stable_file_digest(path)[1],
            }
            for role, path in sorted(staged_toolchain.items())
        ]
        input_manifest = {
            "schema": "zhimeng.writing-adapter-input-manifest.v1",
            "run_id": run_id,
            "adapter_id": adapter.adapter_id,
            "binding_revision": binding_revision,
            "project_fingerprint": project_fingerprint,
            "sources": before,
            "staged_toolchain": staged_toolchain_manifest,
            "snapshot_patch": snapshot_patch,
        }
        _write_json(input_manifest_path, input_manifest)
        event_sink("snapshot_ready", "ok", {
            "source_roots": len(before),
            "source_files": sum(int(item["file_count"]) for item in before),
            "characters": characters,
        })

        assemble_argv = [
            sys.executable,
            str(staged_toolchain["kb_script"]),
            "--library",
            str(snapshot_library),
            "assemble-context",
            "--project",
            snapshot_patch["project_name"],
            "--chapter-id",
            chapter_id,
            "--stage",
            stage,
            "--genre",
            snapshot_patch["genre"],
            "--platform",
            snapshot_patch["platform"],
            "--style",
            snapshot_patch["style"],
            "--max-chars",
            "2600",
            "--strict-conflict",
            "--source-library",
            str(snapshot_distillation),
            "--out",
            str(context_path),
        ]
        for character in characters:
            assemble_argv.extend(["--character", character])
        event_sink("nlcl_assembling", "running", {"chapter_id": chapter_id})
        assemble = _run_process(assemble_argv, staging_root, adapter.timeout_seconds)
        process_records.append({"step": "nlcl_assemble_context", **assemble})
        if assemble["returncode"] not in {0, 78}:
            raise AdapterPreviewError(
                "WRITING_ADAPTER_COMMAND_FAILED",
                f"NLCL assemble-context failed with exit {assemble['returncode']}",
                details={"returncode": assemble["returncode"], "stderr": assemble["stderr"][-2000:]},
            )
        if not context_path.is_file():
            raise AdapterPreviewError("WRITING_ADAPTER_OUTPUT_MISSING", f"Context pack was not created: {context_path}")

        consumer_argv = [
            sys.executable,
            str(staged_toolchain["consumer_script"]),
            str(context_path),
            "--out",
            str(handoff_path),
            "--contract",
            str(snapshot_contract),
        ]
        event_sink("creation_handoff_building", "running", {"chapter_id": chapter_id})
        consumer = _run_process(consumer_argv, staging_root, adapter.timeout_seconds)
        process_records.append({"step": "creation_handoff", **consumer})
        if consumer["returncode"] not in {0, 78}:
            raise AdapterPreviewError(
                "WRITING_ADAPTER_COMMAND_FAILED",
                f"Creation handoff consumer failed with exit {consumer['returncode']}",
                details={"returncode": consumer["returncode"], "stderr": consumer["stderr"][-2000:]},
            )
        if not handoff_path.is_file():
            raise AdapterPreviewError("WRITING_ADAPTER_OUTPUT_MISSING", f"Creation handoff was not created: {handoff_path}")

        event_sink("verifying", "running", {"chapter_id": chapter_id})
        context_pack = _read_json(context_path)
        handoff = _read_json(handoff_path)
        context_sha, operation_sha, operation_path = _validate_outputs(
            context_pack,
            handoff,
            project_name=snapshot_patch["project_name"],
            chapter_id=chapter_id,
            stage=stage,
            context_path=context_path,
            snapshot_kb=snapshot_kb,
        )
        after = _source_manifests(project_root, kb_root, adapter)
        _assert_sources_unchanged(before, after)

        run_context_path = partial_run_root / context_path.name
        run_handoff_path = partial_run_root / handoff_path.name
        run_operation_path = partial_run_root / f"{chapter_id}_operation_package.json"
        shutil.copy2(context_path, run_context_path)
        shutil.copy2(handoff_path, run_handoff_path)
        shutil.copy2(operation_path, run_operation_path)
        volatile_roots = [staging_root, partial_run_root, run_root]
        semantic_context = _semantic_value(context_pack, volatile_roots)
        semantic_handoff = _semantic_value(handoff, volatile_roots)
        operation_package = _read_json(run_operation_path)
        semantic_operation = _semantic_value(operation_package, volatile_roots)
        semantic_context_sha = _sha256_bytes(_canonical_json(semantic_context))
        semantic_source = semantic_handoff.get("source_context_pack") if isinstance(semantic_handoff, dict) else None
        if isinstance(semantic_source, dict):
            semantic_source["sha256"] = semantic_context_sha
            semantic_source["binding_mode"] = "semantic_preview"
        artifacts = [
            _artifact_record(run_context_path, "context_pack", semantic_context, partial_run_root, published_run_root=run_root),
            _artifact_record(run_handoff_path, "creation_handoff", semantic_handoff, partial_run_root, published_run_root=run_root),
            _artifact_record(run_operation_path, "operation_package", semantic_operation, partial_run_root, published_run_root=run_root),
        ]
        strict_failures = [str(item) for item in context_pack.get("strict_failures", []) if str(item).strip()]
        writer_gate = handoff.get("writer_gate") if isinstance(handoff.get("writer_gate"), dict) else {}
        handoff_blockers = [str(item) for item in writer_gate.get("blockers", []) if str(item).strip()]
        warning_codes = list(dict.fromkeys(initial_gaps + strict_failures + handoff_blockers))
        handoff_formal = bool(writer_gate.get("formal_draft_allowed"))
        formal_draft_allowed = inspection_status == "ready" and handoff_formal and not warning_codes
        domain_gate = "blocked" if inspection_status == "blocked" or "project_truth_conflict" in strict_failures else (
            "ready" if formal_draft_allowed else "risk_draft_only"
        )
        adapter_state = "PREVIEW_READY" if formal_draft_allowed else "PREVIEW_READY_WITH_GAPS"
        resolved_scope = {
            "project": snapshot_patch["project_name"],
            "chapter_id": chapter_id,
            "stage": stage,
            "route": _scope_route(context_pack),
            "characters": characters,
        }
        semantic_preview = {
            "schema": RESPONSE_SCHEMA,
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.version,
            "binding_revision": binding_revision,
            "project_fingerprint": project_fingerprint,
            "resolved_scope": resolved_scope,
            "input_roots": [
                {key: item[key] for key in ("role", "file_count", "bytes", "sha256")}
                for item in before
            ],
            "artifact_semantic_hashes": [item["semantic_sha256"] for item in artifacts],
            "adapter_state": adapter_state,
            "domain_gate": domain_gate,
            "formal_draft_allowed": formal_draft_allowed,
            "warning_codes": warning_codes,
        }
        preview_sha = _sha256_bytes(_canonical_json(semantic_preview))
        output_manifest = {
            "schema": "zhimeng.writing-adapter-output-manifest.v1",
            "run_id": run_id,
            "preview_sha256": preview_sha,
            "context_pack_sha256": context_sha,
            "operation_package_sha256": operation_sha,
            "artifacts": artifacts,
            "source_writes": [],
            "proposed_source_diff": [],
        }
        _write_json(output_manifest_path, output_manifest)
        receipt = {
            "schema": "zhimeng.writing-adapter-execution-receipt.v1",
            "run_id": run_id,
            "preview_id": run_id,
            "preview_sha256": preview_sha,
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.version,
            "project_id": project_id,
            "binding_revision": binding_revision,
            "project_fingerprint": project_fingerprint,
            "resolved_scope": resolved_scope,
            "context_pack_sha256": context_sha,
            "operation_package_sha256": operation_sha,
            "adapter_state": adapter_state,
            "domain_gate": domain_gate,
            "formal_draft_allowed": formal_draft_allowed,
            "promotion_allowed": False,
            "processes": process_records,
            "source_writes": [],
            "errors": [],
            "warnings": warning_codes,
            "created_at": now_iso(),
        }
        _write_json(receipt_path, receipt)
        manifest_artifacts = [
            _artifact_record(input_manifest_path, "input_manifest", _semantic_value(input_manifest, volatile_roots), partial_run_root, published_run_root=run_root),
            _artifact_record(output_manifest_path, "output_manifest", _semantic_value(output_manifest, volatile_roots), partial_run_root, published_run_root=run_root),
            _artifact_record(receipt_path, "execution_receipt", _semantic_value(receipt, volatile_roots), partial_run_root, published_run_root=run_root),
        ]
        response = {
            "schema": RESPONSE_SCHEMA,
            "status": "ok",
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.version,
            "preview_id": run_id,
            "run_id": run_id,
            "preview_sha256": preview_sha,
            "project_id": project_id,
            "binding_revision": binding_revision,
            "project_fingerprint": project_fingerprint,
            "resolved_scope": resolved_scope,
            "input_manifest": [
                {key: item[key] for key in ("role", "path", "file_count", "bytes", "sha256")}
                for item in before
            ],
            "toolchain_manifest": [
                {
                    "role": item["role"],
                    "source_path": item["path"],
                    "staged_path": str(staged_toolchain[
                        {
                            "nlcl_script": "kb_script",
                            "handoff_contract": "handoff_contract",
                            "creation_consumer": "consumer_script",
                            "distillation_profile_reader": "distill_script",
                        }[item["role"]]
                    ]),
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                }
                for item in before
                if item["role"] in {"nlcl_script", "handoff_contract", "creation_consumer", "distillation_profile_reader"}
            ],
            "planned_reads": [item["path"] for item in before],
            "staging_writes": [str(staging_root)],
            "source_writes": [],
            "artifacts": artifacts + manifest_artifacts,
            "proposed_source_diff": [],
            "adapter_state": adapter_state,
            "domain_gate": domain_gate,
            "formal_draft_allowed": formal_draft_allowed,
            "promotion_allowed": False,
            "preconditions": initial_gaps,
            "errors": [],
            "warnings": warning_codes,
            "context_pack_sha256": context_sha,
            "operation_package_sha256": operation_sha,
        }
        if prepublish_guard is not None:
            prepublish_guard()
        event_sink(adapter_state.casefold(), domain_gate, {
            "preview_sha256": preview_sha,
            "warning_count": len(warning_codes),
            "formal_draft_allowed": formal_draft_allowed,
        })
        os.replace(partial_run_root, run_root)
        response["staging_retained"] = False
        return response
    except AdapterPreviewError as error:
        return publish_failure(error)
    except Exception as error:
        return publish_failure(AdapterPreviewError(
            "WRITING_ADAPTER_INTERNAL_ERROR",
            f"Unexpected adapter preview failure: {error}",
            details={"exception_type": type(error).__name__},
        ))
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        shutil.rmtree(partial_run_root, ignore_errors=True)
        shutil.rmtree(failure_partial_root, ignore_errors=True)


def failure_payload(
    code: str,
    message: str,
    *,
    adapter_id: str = ADAPTER_ID,
    project_id: str = "",
    binding_revision: int | None = None,
    project_fingerprint: str = "",
    details: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return _failure_payload(
        AdapterPreviewError(code, message, details=details),
        adapter_id=adapter_id,
        project_id=project_id,
        binding_revision=binding_revision,
        project_fingerprint=project_fingerprint,
    )


def valid_chapter_id(value: str) -> bool:
    return bool(re.fullmatch(r"CH\d{3,6}", value))
