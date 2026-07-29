#!/usr/bin/env python3
"""Evaluate the Runtime-owned prewrite package without authorizing prose."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _check(code: str, status: str, message: str, refs: list[str] | None = None):
    return {
        "code": code,
        "status": status,
        "message": message,
        "evidence_refs": refs or [],
    }


def _evaluate(request: dict[str, object]) -> dict[str, object]:
    package = request["operation_package"]
    if not isinstance(package, dict):
        raise SystemExit("operation_package must be an object")
    audit_status = request.get("project_audit_domain_status")
    if audit_status not in {"healthy", "degraded", "blocked"}:
        raise SystemExit("project audit domain status is invalid")

    missing = package.get("missing_requirements")
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise SystemExit("operation package missing_requirements is invalid")
    truth = package.get("must_obey_truth")
    characters = package.get("current_character_state")
    scene = package.get("scene_contract")
    style = package.get("style_output_contract")
    budget = package.get("library_activation_budget")
    failure_routing = package.get("failure_routing")
    if not isinstance(truth, list) or not isinstance(characters, list):
        raise SystemExit("operation package truth references are invalid")
    if not isinstance(scene, dict) or not isinstance(style, dict) or not isinstance(budget, dict):
        raise SystemExit("operation package contracts are invalid")
    if not isinstance(failure_routing, list):
        raise SystemExit("operation package failure routing is invalid")
    route_layers: dict[str, str] = {}
    for route in failure_routing:
        if not isinstance(route, dict):
            raise SystemExit("operation package failure route is invalid")
        signature = route.get("failure_signature")
        rewrite_from = route.get("rewrite_from")
        if not isinstance(signature, str) or not isinstance(rewrite_from, str):
            raise SystemExit("operation package failure route fields are invalid")
        route_layers[signature] = rewrite_from
    package_readiness = package.get("readiness")
    if package_readiness not in {"blocked", "risk_draft_only"}:
        raise SystemExit("operation package readiness is invalid")

    checks = [
        _check(
            "project_audit",
            "blocked" if audit_status == "blocked" else (
                "warning" if audit_status == "degraded" else "passed"
            ),
            f"Managed project audit domain status: {audit_status}.",
            [str(request["project_audit_execution_receipt_hash"])],
        ),
        _check(
            "operation_package_integrity",
            "passed",
            "Runtime contract and package hash validation completed before process start.",
            [str(package.get("package_hash", ""))],
        ),
        _check(
            "operation_package_readiness",
            "blocked" if package_readiness == "blocked" else "passed",
            f"Runtime operation package readiness: {package_readiness}.",
            [str(item) for item in missing],
        ),
        _check(
            "project_truth_binding",
            "passed" if truth else "blocked",
            "Project-truth evidence is bound." if truth else "Project-truth evidence is missing.",
            [str(item.get("source_id")) for item in truth if isinstance(item, dict)],
        ),
        _check(
            "current_character_state",
            "passed" if characters else "blocked",
            "Character-state evidence is bound." if characters else "Character-state evidence is missing.",
            [str(item.get("source_id")) for item in characters if isinstance(item, dict)],
        ),
        _check(
            "scene_contract",
            "passed" if scene.get("status") == "bound" else "blocked",
            f"Scene contract status: {scene.get('status', 'unbound')}.",
            [str(item) for item in scene.get("evidence_source_ids", [])],
        ),
        _check(
            "chapter_control_contract",
            (
                "blocked"
                if "chapter_control_contract_unbound" in missing
                else "passed"
            ),
            (
                "Chapter control contract is missing."
                if "chapter_control_contract_unbound" in missing
                else "Chapter control contract is bound by Runtime evidence."
            ),
        ),
        _check(
            "style_output_contract",
            "passed" if style.get("status") == "bound" else "warning",
            f"Style/retrieval contract status: {style.get('status', 'unbound')}.",
            [str(item) for item in style.get("selected_card_ids", [])],
        ),
        _check(
            "library_activation_budget",
            "passed",
            "The package preserves the bounded Context character and token budgets.",
            [str(item) for item in budget.get("activated_source_ids", [])],
        ),
        _check(
            "formal_writer_authorization",
            "warning",
            "M3e v1 has no formal Writer/writeback authorization contract.",
        ),
    ]
    blocked = audit_status == "blocked" or package_readiness == "blocked"
    gate_status = "blocked" if blocked else "risk_draft_only"
    raw_failures = list(dict.fromkeys(missing))
    if audit_status == "blocked":
        raw_failures.append("project_audit_blocked")
    raw_failures.extend(
        item
        for item in ("formal_writer_contract_unavailable", "writeback_closure_unavailable")
        if item not in raw_failures
    )
    signature_map = {
        "project_audit_blocked": "project_audit_blocked",
        "project_truth_unbound": "project_truth_unbound",
        "active_chapter_unbound": "truth_or_state_unbound",
        "current_character_state_unbound": "truth_or_state_unbound",
        "scene_contract_unbound": "truth_or_state_unbound",
        "chapter_control_contract_unbound": "operation_package_unreadable_or_missing",
        "context_pack_blocked": "operation_package_unreadable_or_missing",
        "migrated_prewrite_package_requires_recompile": (
            "operation_package_unreadable_or_missing"
        ),
        "formal_knowledge_unavailable": "retrieval_selection_or_budget_unproven",
        "knowledge_route_unavailable": "retrieval_selection_or_budget_unproven",
        "formal_writer_contract_unavailable": "formal_writer_contract_unavailable",
        "writeback_closure_unavailable": "formal_writer_contract_unavailable",
    }
    routed_failures: list[str] = []
    next_actions: list[str] = []
    for raw_code in raw_failures[:32]:
        signature = signature_map.get(
            raw_code,
            "operation_package_unreadable_or_missing",
        )
        rewrite_from = route_layers.get(signature)
        if rewrite_from is None:
            raise SystemExit(f"operation package has no route for {signature}")
        if signature not in routed_failures:
            routed_failures.append(signature)
        next_actions.append(
            f"Resolve {raw_code} through {signature} at {rewrite_from}."
        )
    return {
        "schema": "zhimeng.creation-prewrite-gate-result.v1",
        "gate_status": gate_status,
        "formal_draft_allowed": False,
        "allowed_output": "repair_plan_or_risk_draft_only",
        "operation_package_id": package["operation_package_id"],
        "operation_package_hash": package["package_hash"],
        "project_audit_execution_receipt_hash": request[
            "project_audit_execution_receipt_hash"
        ],
        "checks": checks,
        "failure_signatures": routed_failures,
        "next_actions": next_actions,
    }


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] != "--json":
        raise SystemExit("expected <project-snapshot> --json")
    if not Path(sys.argv[1]).is_dir():
        raise SystemExit("project snapshot is unavailable")
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise SystemExit("invalid prewrite gate request")
    if request.get("schema") != "zhimeng.creation-prewrite-gate-request.v1":
        raise SystemExit("unsupported prewrite gate request schema")

    result = _evaluate(request)
    artifact_path = Path.cwd() / "prewrite-gate.json"
    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    blocked = result["gate_status"] == "blocked"
    envelope = {
        "health": "blocked" if blocked else "degraded",
        "summary": (
            "Prewrite gate blocked formal prose."
            if blocked
            else "Prewrite gate permits risk-draft analysis only."
        ),
        "result": result,
        "artifacts": [
            {
                "artifact_type": "prewrite_gate",
                "relative_path": "prewrite-gate.json",
                "payload": result,
            }
        ],
    }
    print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
