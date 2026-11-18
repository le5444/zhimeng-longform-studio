#!/usr/bin/env python3
"""Run the source-ingest family against the isolated project snapshot.

The four family packages are snapshotted independently by ManagedSkillService,
so this small runner intentionally carries its own orchestration code instead
of importing project code or executing a raw SKILL.md file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


FAMILY = "source_ingest"
REQUEST_SCHEMA = "zhimeng.four-skills-family-step-request.v1"
RESULT_SCHEMA = "zhimeng.four-skills-family-step-result.v1"
MAX_SCAN_FILES = 200
MAX_FILE_CHARS = 48_000
MAX_PACKET_CHARS = 240_000
MAX_CAPTURE_BYTES = 256 * 1024
TIMEOUT_SECONDS = 45


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _script_path() -> tuple[Path | None, str | None]:
    # A packaged CLI must win over developer overrides and the profile copy;
    # otherwise a portable app can report bundled readiness while executing a
    # different machine-local version.
    bundle_raw = os.environ.get("ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT", "").strip()
    if bundle_raw:
        candidate = (
            Path(bundle_raw).expanduser()
            / "novel-distillation"
            / "scripts"
            / "distill_source_packet.py"
        )
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve(), "bundled"

    explicit = os.environ.get("ZHIMENG_FOUR_SKILLS_SOURCE_INGEST_SCRIPT", "").strip()
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
    candidates = [
        home / ".codex" / "skills" / "novel-distillation" / "scripts" / "distill_source_packet.py",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve(), "external"
    return None, None


def _python() -> str:
    return sys.executable


def _safe_env() -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
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
        return {
            "status": "failed",
            "failure_code": "tool_timeout",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc)[:512],
        }
    except OSError as exc:
        return {
            "status": "failed",
            "failure_code": "tool_start_failed",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc)[:512],
        }
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


def _text_files(snapshot: Path) -> list[Path]:
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    files: list[Path] = []
    for path in snapshot.rglob("*"):
        if len(files) >= MAX_SCAN_FILES:
            break
        if not path.is_file() or path.is_symlink() or any(part in ignored for part in path.parts):
            continue
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        files.append(path)
    return sorted(files, key=lambda item: str(item).casefold())


def _build_packet(snapshot: Path, staging: Path) -> tuple[Path, int, str]:
    packet = staging / "source-packet"
    packet.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    total = 0
    source_files = _text_files(snapshot)
    for path in source_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            continue
        relative = path.relative_to(snapshot).as_posix()
        piece = f"\n\n===== {relative} =====\n{text}"
        if total + len(piece) > MAX_PACKET_CHARS:
            piece = piece[: max(0, MAX_PACKET_CHARS - total)]
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
        if total >= MAX_PACKET_CHARS:
            break
    text = "".join(chunks).lstrip()
    if not text:
        raise ValueError("project snapshot has no readable Markdown/text source")
    text_path = packet / "packet.txt"
    metadata_path = packet / "packet.metadata.json"
    text_path.write_text(text, encoding="utf-8")
    metadata_path.write_text(
        _json(
            {
                "title": snapshot.name,
                "source_backend": "generic_local_snapshot",
                "source_file_count": len(source_files),
                "captured_chars": len(text),
            }
        ),
        encoding="utf-8",
    )
    manifest_path = packet / "packet.manifest.json"
    manifest_path.write_text(
        _json(
            {
                "schema": "novel_distillation.source_packet_manifest.v1",
                "packet_label": snapshot.name,
                "source_backend": "generic_local_snapshot",
                "artifacts": [
                    {"kind": "packet_text", "path": "packet.txt"},
                    {"kind": "packet_metadata", "path": "packet.metadata.json"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return packet, len(source_files), _sha256(text.encode("utf-8"))


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
    envelope = {
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
        "artifacts": [
            {
                "artifact_type": "four_skills_family_step",
                "relative_path": "family-step-result.json",
                "payload": artifact,
            }
        ],
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 0 if status == "succeeded" else 1


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": f"invalid request: {exc}"}, ensure_ascii=False))
        return 1
    if not isinstance(request, dict) or request.get("family") != FAMILY:
        expected = request.get("family") if isinstance(request, dict) else None
        return _emit(
            request if isinstance(request, dict) else {},
            status="failed",
            detail=f"family mismatch expected={FAMILY} actual={expected}",
            tool={"name": "family-adapter", "failure_code": "family_mismatch"},
        )
    script, script_source = _script_path()
    if script is None:
        return _emit(
            request,
            status="failed",
            detail="novel-distillation distill_source_packet.py is unavailable",
            tool={"name": "novel-distillation.distill_source_packet", "failure_code": "tool_unavailable"},
        )
    snapshot = Path(sys.argv[1]).resolve()
    staging = Path.cwd().resolve()
    try:
        packet, file_count, source_hash = _build_packet(snapshot, staging)
    except (OSError, ValueError) as exc:
        return _emit(
            request,
            status="failed",
            detail=f"source packet preparation failed: {exc}",
            tool={"name": "novel-distillation.distill_source_packet", "failure_code": "input_unavailable"},
        )
    output_dir = staging / "source-packet" / "distillation-draft"
    result = _run(
        [_python(), str(script), str(packet), "--out-dir", str(output_dir)],
        cwd=staging,
    )
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {}
    status = "succeeded" if result["status"] == "succeeded" and payload.get("status") == "draft_ready" else "failed"
    detail = (
        f"distill_source_packet generated a draft from {file_count} snapshot text files"
        if status == "succeeded"
        else f"distill_source_packet failed: {result.get('stderr') or payload.get('status') or result.get('failure_code')}"
    )
    tool = {
        "name": "novel-distillation.distill_source_packet",
        "script": str(script),
        "source": script_source,
        "exit_code": result.get("exit_code"),
        "status": status,
        "source_file_count": file_count,
        "source_text_sha256": source_hash,
        "stdout_sha256": result.get("stdout_sha256"),
        "output_status": payload.get("status"),
        "stderr": result.get("stderr", ""),
    }
    return _emit(request, status=status, detail=detail, tool=tool)


if __name__ == "__main__":
    raise SystemExit(main())
