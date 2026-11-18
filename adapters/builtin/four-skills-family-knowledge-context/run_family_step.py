#!/usr/bin/env python3
"""Run a read-only NLCL knowledge-library/context preflight."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


FAMILY = "knowledge_context"
RESULT_SCHEMA = "zhimeng.four-skills-family-step-result.v1"
MAX_CAPTURE_BYTES = 256 * 1024
TIMEOUT_SECONDS = 45


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_from_env(name: str, default: Path, *, directory: bool) -> Path | None:
    raw = os.environ.get(name, "").strip()
    candidate = Path(raw).expanduser() if raw else default
    if (candidate.is_dir() if directory else candidate.is_file()):
        return candidate.resolve()
    return None


def _bundle_root() -> Path | None:
    raw = os.environ.get("ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT", "").strip()
    return Path(raw).expanduser() if raw else None


def _bundle_path(relative: str, *, directory: bool) -> Path | None:
    bundle = _bundle_root()
    if bundle is None:
        return None
    candidate = bundle / relative
    valid = candidate.is_dir() if directory else candidate.is_file()
    if not valid or (not directory and candidate.is_symlink()) or (directory and candidate.is_symlink()):
        return None
    return candidate.resolve()


def _home() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def _safe_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"})
    return env


def _run(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path.cwd()),
            env=_safe_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "failed", "failure_code": "tool_timeout", "exit_code": None, "stdout": "", "stderr": str(exc)[:512]}
    except OSError as exc:
        return {"status": "failed", "failure_code": "tool_start_failed", "exit_code": None, "stdout": "", "stderr": str(exc)[:512]}
    stdout = completed.stdout[:MAX_CAPTURE_BYTES]
    stderr = completed.stderr[:MAX_CAPTURE_BYTES]
    return {
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "failure_code": None if completed.returncode == 0 else "tool_exit_nonzero",
        "exit_code": completed.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace")[:1024],
        "stdout_sha256": _sha256(stdout),
    }


def _emit(request: dict[str, Any], *, status: str, detail: str, tool: dict[str, Any]) -> int:
    artifact = {
        "schema": RESULT_SCHEMA,
        "family": FAMILY,
        "batch_id": request.get("batch_id"),
        "probe_id": request.get("probe_id"),
        "executed": True,
        "writes_project_truth": False,
        "adapter_status": status,
        "detail": detail,
        "tool": tool,
    }
    Path("family-step-result.json").write_text(_json(artifact), encoding="utf-8")
    print(json.dumps({
        "health": "healthy" if status == "succeeded" else "blocked",
        "summary": detail,
        "result": {
            "schema": RESULT_SCHEMA,
            "family": FAMILY,
            "batch_id": request.get("batch_id"),
            "probe_id": request.get("probe_id"),
            "executed": True,
            "writes_project_truth": False,
        },
        "artifacts": [{
            "artifact_type": "four_skills_family_step",
            "relative_path": "family-step-result.json",
            "payload": artifact,
        }],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if status == "succeeded" else 1


def _project_context_command(
    snapshot: Path,
    staging: Path,
    script: Path,
    library: Path,
) -> tuple[list[str], str, Path]:
    # A copied .kb can be assembled safely in the snapshot. Otherwise use the
    # read-only list-libraries command to prove the real NLCL CLI is available.
    kb_dirs = [path for path in snapshot.rglob("*.kb") if path.is_dir() and not path.is_symlink()]
    if kb_dirs:
        project_kb = kb_dirs[0]
        project_name = project_kb.name.removesuffix(".kb")
        out = staging / "knowledge-context.json"
        return (
            [sys.executable, str(script), "--library", str(project_kb.parent), "assemble-context", "--project", project_name, "--chapter-id", "CH001", "--stage", "draft", "--max-chars", "2600", "--out", str(out)],
            "assemble-context",
            project_kb.parent,
        )
    return ([sys.executable, str(script), "--library", str(library), "list-libraries"], "list-libraries", library)


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": f"invalid request: {exc}"}, ensure_ascii=False))
        return 1
    if not isinstance(request, dict) or request.get("family") != FAMILY:
        return _emit(request if isinstance(request, dict) else {}, status="failed", detail="family mismatch", tool={"name": "family-adapter", "failure_code": "family_mismatch"})
    home = _home()
    script = _bundle_path(
        "novel-kb-manager/scripts/kb_manager.py",
        directory=False,
    )
    script_source = "bundled" if script is not None else None
    if script is None:
        script = _path_from_env(
            "ZHIMENG_FOUR_SKILLS_KB_MANAGER_SCRIPT",
            home / ".codex" / "skills" / "novel-kb-manager" / "scripts" / "kb_manager.py" if home else Path("__missing_kb_manager.py"),
            directory=False,
        )
        script_source = "configured" if script is not None and os.environ.get("ZHIMENG_FOUR_SKILLS_KB_MANAGER_SCRIPT", "").strip() else "external" if script is not None else None
    library = (
        _bundle_path("novel-kb-manager/library", directory=True)
        if script_source == "bundled"
        else _path_from_env(
            "ZHIMENG_FOUR_SKILLS_KB_LIBRARY",
            home / ".codex" / "skills" / "novel-kb-manager" / "library" if home else Path("__missing_kb_library"),
            directory=True,
        )
    )
    if script is None:
        return _emit(request, status="failed", detail="novel-kb-manager kb_manager.py is unavailable", tool={"name": "novel-kb-manager", "failure_code": "tool_unavailable"})
    if library is None:
        return _emit(request, status="failed", detail="novel-kb-manager library is unavailable", tool={"name": "novel-kb-manager", "failure_code": "library_unavailable"})
    snapshot = Path(sys.argv[1]).resolve()
    staging = Path.cwd().resolve()
    command, mode, library_used = _project_context_command(snapshot, staging, script, library)
    result = _run(command)
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {}
    output_exists = mode != "assemble-context" or (staging / "knowledge-context.json").is_file()
    status = "succeeded" if result["status"] == "succeeded" and isinstance(payload, (dict, list)) and output_exists else "failed"
    detail = (
        f"kb_manager {mode} completed in the isolated snapshot"
        if status == "succeeded"
        else f"kb_manager {mode} failed: {result.get('stderr') or result.get('failure_code')}"
    )
    tool = {
        "name": "novel-kb-manager.kb_manager",
        "script": str(script),
        "source": script_source,
        "library": str(library_used),
        "command": mode,
        "exit_code": result.get("exit_code"),
        "status": status,
        "library_count": len(payload.get("domain", [])) if isinstance(payload, dict) and isinstance(payload.get("domain"), list) else None,
        "stdout_sha256": result.get("stdout_sha256"),
        "stderr": result.get("stderr", ""),
    }
    return _emit(request, status=status, detail=detail, tool=tool)


if __name__ == "__main__":
    raise SystemExit(main())
