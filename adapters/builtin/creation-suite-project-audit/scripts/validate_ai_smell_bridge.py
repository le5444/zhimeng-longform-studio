"""Validate ai_smell_project_audit_bridge payloads.

The JSON schema owns field shape. This script adds semantic checks so AI smell
reports cannot be cited as project-level evidence unless project_audit.py
actually read the report directory through --ai-smell-report-dir.

Usage:
    python validate_ai_smell_bridge.py bridge.json
    python validate_ai_smell_bridge.py bridge.json --json
    type bridge.json | python validate_ai_smell_bridge.py -
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import jsonschema


DEFAULT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "references"
    / "ai-smell-bridge-schema.json"
)


def _load_json(path: pathlib.Path | str) -> Any:
    if str(path) == "-":
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text)
    with pathlib.Path(path).open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _json_path(error: jsonschema.ValidationError) -> str:
    if not error.path:
        return "$"
    return "$." + ".".join(str(part) for part in error.path)


def validate_payload(payload: Any, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    errors: list[str] = []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(payload), key=lambda err: list(err.path)):
        errors.append(f"schema:{_json_path(error)}: {error.message}")

    if not isinstance(payload, dict):
        return errors

    status = payload.get("ai_smell_project_audit_bridge")
    command = str(payload.get("command") or "")
    report_dir = str(payload.get("report_dir") or "").strip()
    reports_seen = payload.get("ai_smell_reports_seen")
    blocking = payload.get("release_blocking_findings")
    health = payload.get("project_audit_health")

    if status == "pass":
        if "--ai-smell-report-dir" not in command:
            errors.append("semantic: pass requires project_audit.py command with --ai-smell-report-dir")
        if "project_audit.py" not in command:
            errors.append("semantic: pass requires project_audit.py command evidence")
        if not report_dir or report_dir in {"<AI味报告所在目录>", "not_applicable"}:
            errors.append("semantic: pass requires concrete report_dir")
        if not isinstance(reports_seen, int) or reports_seen <= 0:
            errors.append("semantic: pass requires ai_smell_reports_seen > 0")
        if isinstance(blocking, int) and blocking > 0:
            errors.append("semantic: pass cannot have release_blocking_findings > 0")
        if health in {"degraded", "blocked"}:
            errors.append("semantic: pass cannot use degraded/blocked project_audit_health")

    if status == "fail":
        if isinstance(blocking, int) and blocking <= 0 and "missing" not in command.lower():
            errors.append("semantic: fail should explain missing bridge or release-blocking findings")

    if status == "not_applicable":
        if report_dir and report_dir not in {"not_applicable", "none", "无"}:
            errors.append("semantic: not_applicable should not name a concrete report_dir")
        if isinstance(reports_seen, int) and reports_seen != 0:
            errors.append("semantic: not_applicable requires ai_smell_reports_seen = 0")
        if isinstance(blocking, int) and blocking != 0:
            errors.append("semantic: not_applicable requires release_blocking_findings = 0")

    return errors


def validate_file(path: pathlib.Path | str, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    return validate_payload(_load_json(path), schema_path=schema_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="JSON files to validate, or '-' for stdin")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH), help="Schema path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result")
    args = parser.parse_args(argv)

    schema_path = pathlib.Path(args.schema)
    all_results: list[dict[str, Any]] = []
    error_count = 0
    for path in args.paths:
        try:
            errors = validate_file(path, schema_path=schema_path)
        except Exception as exc:  # noqa: BLE001 - CLI should report malformed inputs.
            errors = [f"load:{path}: {exc.__class__.__name__}: {exc}"]
        error_count += len(errors)
        all_results.append({"path": path, "ok": not errors, "errors": errors})

    result = {
        "ok": error_count == 0,
        "error_count": error_count,
        "results": all_results,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in all_results:
            status = "OK" if item["ok"] else "ERROR"
            print(f"{status} {item['path']}")
            for error in item["errors"]:
                print(f"  - {error}")
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
