#!/usr/bin/env python3
"""Run the read-only novel-distillation health check for this family."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


FAMILY = "distillation"
RESULT_SCHEMA = "zhimeng.four-skills-family-step-result.v1"
MAX_CAPTURE_BYTES = 256 * 1024
TIMEOUT_SECONDS = 45


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_root() -> Path | None:
    raw = os.environ.get("ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT", "").strip()
    return Path(raw).expanduser() if raw else None


def _script_path() -> tuple[Path | None, str | None]:
    bundle = _bundle_root()
    if bundle is not None:
        candidate = bundle / "novel-distillation" / "scripts" / "distill_kb.py"
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve(), "bundled"

    explicit = os.environ.get("ZHIMENG_FOUR_SKILLS_DISTILLATION_SCRIPT", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return (
            (candidate.resolve(), "configured")
            if candidate.is_file() and not candidate.is_symlink()
            else (None, None)
        )
    try:
        home = Path.home()
    except RuntimeError:
        return None, None
    candidate = home / ".codex" / "skills" / "novel-distillation" / "scripts" / "distill_kb.py"
    return (
        (candidate.resolve(), "external")
        if candidate.is_file() and not candidate.is_symlink()
        else (None, None)
    )


def _library_path(source: str | None) -> Path | None:
    if source == "bundled":
        bundle = _bundle_root()
        if bundle is None:
            return None
        candidate = bundle / "novel-distillation" / "library"
        return (
            candidate.resolve()
            if candidate.is_dir() and not candidate.is_symlink()
            else None
        )

    explicit = os.environ.get("ZHIMENG_FOUR_SKILLS_DISTILLATION_LIBRARY", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return (
            candidate.resolve()
            if candidate.is_dir() and not candidate.is_symlink()
            else None
        )
    try:
        home = Path.home()
    except RuntimeError:
        return None
    candidate = home / ".codex" / "skills" / "novel-distillation" / "library"
    return (
        candidate.resolve()
        if candidate.is_dir() and not candidate.is_symlink()
        else None
    )


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


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": f"invalid request: {exc}"}, ensure_ascii=False))
        return 1
    if not isinstance(request, dict) or request.get("family") != FAMILY:
        return _emit(request if isinstance(request, dict) else {}, status="failed", detail="family mismatch", tool={"name": "family-adapter", "failure_code": "family_mismatch"})
    script, script_source = _script_path()
    library = _library_path(script_source)
    if script is None:
        return _emit(request, status="failed", detail="novel-distillation distill_kb.py is unavailable", tool={"name": "novel-distillation.distill_kb", "failure_code": "tool_unavailable"})
    if library is None:
        return _emit(request, status="failed", detail="novel-distillation library is unavailable", tool={"name": "novel-distillation.distill_kb", "failure_code": "library_unavailable"})
    result = _run([sys.executable, str(script), "health", str(library), "--json"])
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {}
    status = "succeeded" if result["status"] == "succeeded" and isinstance(payload, dict) and isinstance(payload.get("cards"), int) else "failed"
    detail = (
        f"distill_kb health read {payload.get('cards', 0)} cards ({payload.get('health', 'unknown')})"
        if status == "succeeded"
        else f"distill_kb health failed: {result.get('stderr') or result.get('failure_code')}"
    )
    tool = {
        "name": "novel-distillation.distill_kb",
        "script": str(script),
        "source": script_source,
        "library": str(library),
        "command": "health",
        "exit_code": result.get("exit_code"),
        "status": status,
        "health": payload.get("health"),
        "structural_health": payload.get("structural_health"),
        "cards": payload.get("cards"),
        "stdout_sha256": result.get("stdout_sha256"),
        "stderr": result.get("stderr", ""),
    }
    return _emit(request, status=status, detail=detail, tool=tool)


if __name__ == "__main__":
    raise SystemExit(main())
