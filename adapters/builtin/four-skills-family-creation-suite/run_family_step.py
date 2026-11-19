#!/usr/bin/env python3
"""Run the real creation-suite AI-smell detector on snapshot prose."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


FAMILY = "creation_suite"
RESULT_SCHEMA = "zhimeng.four-skills-family-step-result.v1"
MAX_CAPTURE_BYTES = 128 * 1024
MAX_MANUSCRIPT_CHARS = 120_000
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


def _run(command: list[str], *, cwd: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
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


def _manuscript(snapshot: Path, staging: Path) -> tuple[Path, str]:
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    candidates = [
        path
        for path in snapshot.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".md", ".markdown", ".txt"} and not any(part in ignored for part in path.parts)
    ]
    if candidates:
        source = max(candidates, key=lambda item: item.stat().st_size)
        text = source.read_text(encoding="utf-8", errors="replace")[:MAX_MANUSCRIPT_CHARS]
        if text.strip():
            target = staging / "creation-suite" / "manuscript.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return target, source.relative_to(snapshot).as_posix()
    raise ValueError("project snapshot has no readable Markdown/text manuscript")


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
    home = _home()
    script = _bundle_path(
        "novel-creation-suite/scripts/detect_ai_smell.py",
        directory=False,
    )
    script_source = "bundled" if script is not None else None
    if script is None:
        script = _path_from_env(
            "ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT",
            home / ".codex" / "skills" / "novel-creation-suite" / "scripts" / "detect_ai_smell.py" if home else Path("__missing_creation.py"),
            directory=False,
        )
        script_source = "configured" if script is not None and os.environ.get("ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT", "").strip() else "external" if script is not None else None
    library = (
        _bundle_path("novel-distillation/library", directory=True)
        if script_source == "bundled"
        else _path_from_env(
            "ZHIMENG_FOUR_SKILLS_CREATION_LIBRARY",
            home / ".codex" / "skills" / "novel-distillation" / "library" if home else Path("__missing_creation_library"),
            directory=True,
        )
    )
    if script is None:
        return _emit(request, status="failed", detail="novel-creation-suite detect_ai_smell.py is unavailable", tool={"name": "novel-creation-suite.detect_ai_smell", "failure_code": "tool_unavailable"})
    snapshot = Path(sys.argv[1]).resolve()
    staging = Path.cwd().resolve()
    try:
        manuscript, source = _manuscript(snapshot, staging)
    except (OSError, ValueError) as exc:
        return _emit(request, status="failed", detail=f"manuscript preparation failed: {exc}", tool={"name": "novel-creation-suite.detect_ai_smell", "failure_code": "input_unavailable"})
    report = staging / "creation-suite" / "ai-smell-report.json"
    command = [sys.executable, str(script), str(manuscript), "--top", "3", "--output", str(report)]
    if library is not None:
        command.extend(["--library", str(library)])
    result = _run(command, cwd=staging)
    try:
        payload = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    overall = payload.get("overall_score") if isinstance(payload, dict) else None
    status = "succeeded" if result["status"] == "succeeded" and isinstance(overall, dict) else "failed"
    detail = (
        f"detect_ai_smell scored snapshot prose {overall.get('score', 'n/a')} ({overall.get('level', 'unknown')})"
        if status == "succeeded"
        else f"detect_ai_smell failed: {result.get('stderr') or result.get('failure_code')}"
    )
    tool = {
        "name": "novel-creation-suite.detect_ai_smell",
        "script": str(script),
        "source": script_source,
        "library": str(library) if library is not None else None,
        "source_relative_path": source,
        "exit_code": result.get("exit_code"),
        "status": status,
        "score": overall.get("score") if isinstance(overall, dict) else None,
        "level": overall.get("level") if isinstance(overall, dict) else None,
        "content_hash": payload.get("content_hash") if isinstance(payload, dict) else None,
        "stdout_sha256": result.get("stdout_sha256"),
        "stderr": result.get("stderr", ""),
    }
    return _emit(request, status=status, detail=detail, tool=tool)


if __name__ == "__main__":
    raise SystemExit(main())
