#!/usr/bin/env python3
"""Materialize a Creation handoff without authorizing formal prose."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REQUEST_SCHEMA = "zhimeng.creation-handoff-request.v1"
RESULT_SCHEMA = "zhimeng.creation-handoff-result.v1"


def _expect_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be an object")
    return value


def _expect_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SystemExit(f"{label} must be a lowercase SHA-256 hash")
    return value


def _list_of_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SystemExit(f"{label} must be a string list")
    if len(value) != len(set(value)):
        raise SystemExit(f"{label} must be unique")
    return list(value)


def _evaluate(request: dict[str, Any]) -> dict[str, Any]:
    package = _expect_object(request.get("operation_package"), "operation_package")
    if package.get("formal_draft_allowed") is not False:
        raise SystemExit("operation package must not authorize formal prose")
    if package.get("schema") != "zhimeng.prewrite-operation-package.v1":
        raise SystemExit("operation package schema is unsupported")

    gate_status = request.get("prewrite_gate_status")
    if gate_status not in {"blocked", "risk_draft_only"}:
        raise SystemExit("prewrite_gate_status is invalid")
    if request.get("prewrite_gate_formal_draft_allowed") is not False:
        raise SystemExit("prewrite gate must not authorize formal prose")
    failure_signatures = _list_of_strings(
        request.get("prewrite_gate_failure_signatures", []),
        "prewrite_gate_failure_signatures",
    )
    if gate_status == "blocked" and not failure_signatures:
        raise SystemExit("blocked handoff requires failure signatures")

    workflow_hashes = [
        _expect_hash(request.get("project_audit_checkpoint_hash"), "project_audit_checkpoint_hash"),
        _expect_hash(request.get("prewrite_gate_checkpoint_hash"), "prewrite_gate_checkpoint_hash"),
    ]
    evidence_refs = package.get("relevant_evidence_refs")
    truth_refs = package.get("must_obey_truth")
    character_refs = package.get("current_character_state")
    if not isinstance(evidence_refs, list) or not isinstance(truth_refs, list) or not isinstance(character_refs, list):
        raise SystemExit("operation package evidence refs are invalid")

    missing = _list_of_strings(package.get("missing_requirements", []), "missing_requirements")
    next_actions = [
        f"Resolve {signature} before any formal Writer dispatch."
        for signature in failure_signatures[:32]
    ]
    if not next_actions and gate_status == "risk_draft_only":
        next_actions.append(
            "Use this handoff only for repair planning or risk-draft analysis until formal Writer authorization exists."
        )
    for code in missing[:32]:
        action = f"Close missing requirement {code} before formal prose authorization."
        if action not in next_actions:
            next_actions.append(action)

    return {
        "schema": RESULT_SCHEMA,
        "handoff_status": gate_status,
        "formal_draft_allowed": False,
        "allowed_output": "repair_plan_or_risk_draft_only",
        "workflow_plan_id": request["workflow_plan_id"],
        "workflow_plan_hash": request["workflow_plan_hash"],
        "context_pack_id": package["context_pack_id"],
        "context_pack_hash": package["context_pack_hash"],
        "route_receipt_id": package["route_receipt_id"],
        "route_receipt_hash": package["route_receipt_hash"],
        "operation_package_id": package["operation_package_id"],
        "operation_package_hash": package["package_hash"],
        "project_id": package["project_id"],
        "project_revision": package["project_revision"],
        "project_fingerprint": package["project_fingerprint"],
        "active_chapter_id": package.get("active_chapter_id"),
        "task_goal": package["task_goal"],
        "knowledge_fingerprint": request.get("knowledge_fingerprint"),
        "evidence_ref_count": len(evidence_refs),
        "must_obey_truth_count": len(truth_refs),
        "current_character_state_count": len(character_refs),
        "project_audit_execution_receipt_hash": request["project_audit_execution_receipt_hash"],
        "prewrite_gate_execution_receipt_hash": request["prewrite_gate_execution_receipt_hash"],
        "workflow_checkpoint_hashes": workflow_hashes,
        "prewrite_gate_status": gate_status,
        "failure_signatures": failure_signatures,
        "next_actions": next_actions[:64],
    }


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] != "--json":
        raise SystemExit("expected <project-snapshot> --json")
    if not Path(sys.argv[1]).is_dir():
        raise SystemExit("project snapshot is unavailable")
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise SystemExit("invalid creation handoff request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise SystemExit("unsupported creation handoff request schema")

    result = _evaluate(request)
    artifact_path = Path.cwd() / "creation-handoff.json"
    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    blocked = result["handoff_status"] == "blocked"
    envelope = {
        "health": "blocked" if blocked else "degraded",
        "summary": (
            "Creation handoff materialized; formal prose remains blocked."
            if blocked
            else "Creation handoff materialized for risk-draft analysis only."
        ),
        "result": result,
        "artifacts": [
            {
                "artifact_type": "creation_handoff",
                "relative_path": "creation-handoff.json",
                "payload": result,
            }
        ],
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
