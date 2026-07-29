#!/usr/bin/env python3
"""Normalize the managed Creation Suite audit into the Runtime Skill envelope."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


AUDIT_SCRIPT = (
    Path(__file__).resolve().parent
    / "references"
    / "bundled-skills"
    / "novel-suite-orchestrator"
    / "scripts"
    / "project_audit.py"
)


def _load_audit_module():
    spec = importlib.util.spec_from_file_location(
        "zhimeng_managed_creation_project_audit",
        AUDIT_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("managed project audit module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] != "--json":
        raise SystemExit("expected <project-snapshot> --json")
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request).difference({"schema"}):
        raise SystemExit("invalid creation project audit request")
    if request.get("schema", "zhimeng.creation-project-audit-request.v1") != (
        "zhimeng.creation-project-audit-request.v1"
    ):
        raise SystemExit("unsupported creation project audit request schema")

    module = _load_audit_module()
    report = module.audit_project(Path(sys.argv[1]))
    report["project"] = "bound_project_snapshot"
    artifact_path = Path.cwd() / "project-audit.json"
    artifact_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    counts = report.get("counts", {})
    summary = (
        "Creation Suite project audit completed: "
        f"P0={counts.get('P0', 0)}, P1={counts.get('P1', 0)}, "
        f"P2={counts.get('P2', 0)}."
    )
    envelope = {
        "health": report["health"],
        "summary": summary,
        "result": report,
        "artifacts": [
            {
                "artifact_type": "project_audit",
                "relative_path": "project-audit.json",
                "payload": report,
            }
        ],
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 1 if report["health"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
