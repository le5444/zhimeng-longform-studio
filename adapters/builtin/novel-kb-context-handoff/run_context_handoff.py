from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


CANONICAL_TYPES = ["work", "voice", "validator"]


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fail(message: str, *, code: str = "context_handoff_blocked") -> int:
    result = {
        "schema": "zhimeng.novel-kb-context-handoff-result.v1",
        "context_handoff_id": "context-handoff-" + hashlib.sha256(message.encode("utf-8")).hexdigest()[:32],
        "status": "blocked",
        "source_handoff_id": "skill-handoff-" + "0" * 32,
        "distillation_index_hash": "0" * 64,
        "downstream_target": "novel-kb-manager.assemble-context",
        "route_health": "blocked",
        "route_evidence_status": "not_validated",
        "context_health": "blocked",
        "readiness": "blocked",
        "selected_entries": [],
        "constraints": {
            "no_full_card_text_copied": True,
            "formal_draft_allowed": False,
            "project_truth_not_written": True,
            "route_health_ready_is_not_validated": True,
            "writes_formal_prose": False,
        },
        "risk_summary": f"{code}: {message}",
        "next_action": "修复上游受控入口后重新准备，并由作者重新批准。",
    }
    print(canonical_json({"health": "blocked", "summary": message, "result": result, "artifacts": []}))
    return 1


def _project_snapshot_ok(snapshot_root: Path, project: dict[str, Any]) -> bool:
    snapshot_file = snapshot_root / "project_snapshot.json"
    if not snapshot_root.is_dir() or not snapshot_file.is_file():
        return False
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    return (
        snapshot.get("project_id") == project.get("project_id")
        and snapshot.get("revision") == project.get("revision")
        and snapshot.get("fingerprint") == project.get("fingerprint")
    )


def _entry_public(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": str(entry["card_id"]),
        "card_type": str(entry["card_type"]),
        "version_id": str(entry["version_id"]),
        "content_hash": str(entry["content_hash"]),
        "title": str(entry["title"]),
        "route_terms": list(entry.get("route_terms") or []),
        "tags": list(entry.get("tags") or []),
        "safety_summary": str(entry.get("safety_summary") or "只使用入口摘要，不复制正式卡全文。"),
    }


def _unique(items: list[object], *, limit: int = 64) -> list[str]:
    seen: list[str] = []
    for item in items:
        value = " ".join(str(item).split())[:160]
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_snapshot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return fail("stdin is not valid JSON", code="invalid_json")
    if not isinstance(payload, dict):
        return fail("request payload is not an object", code="invalid_request")

    project = payload.get("project")
    if not isinstance(project, dict):
        return fail("request project binding is missing", code="project_missing")
    try:
        if not _project_snapshot_ok(Path(args.project_snapshot), project):
            return fail("project snapshot does not match request", code="project_snapshot_stale")
    except (OSError, json.JSONDecodeError):
        return fail("bound project snapshot cannot be read", code="snapshot_unreadable")

    approval = payload.get("approval")
    if not isinstance(approval, dict) or approval.get("approved") is not True or approval.get("risk_acknowledged") is not True:
        return fail("author approval is missing", code="approval_missing")
    if payload.get("formal_draft_allowed") is not False:
        return fail("formal draft output is not allowed", code="formal_draft_not_allowed")

    constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}
    for key in ["no_full_card_text_copied", "project_truth_not_written", "route_health_ready_is_not_validated"]:
        if constraints.get(key) is not True:
            return fail(f"required constraint missing: {key}", code="constraint_missing")

    upstream = payload.get("upstream")
    index = payload.get("distillation_index")
    route = payload.get("route")
    if not isinstance(upstream, dict) or not isinstance(index, dict) or not isinstance(route, dict):
        return fail("upstream distillation_index or route request is missing", code="upstream_missing")
    if index.get("no_full_card_text_copied") is not True:
        return fail("distillation_index must not copy full card text", code="full_text_copy_forbidden")
    distillation_index_hash = str(upstream.get("distillation_index_hash") or "")
    if sha256_json(index) != distillation_index_hash:
        return fail("distillation_index hash does not match upstream receipt", code="index_hash_mismatch")

    entries = index.get("entries")
    if not isinstance(entries, list) or len(entries) != 3:
        return fail("distillation_index must contain exactly three entries", code="entry_count_invalid")
    forbidden = {"summary", "reader_promise", "type_payload", "content"}
    selected_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return fail("distillation_index entry is not an object", code="entry_invalid")
        if forbidden.intersection(entry):
            return fail("distillation_index entry contains full-card fields", code="full_text_field_forbidden")
        selected_entries.append(_entry_public(entry))
    if [entry["card_type"] for entry in selected_entries] != CANONICAL_TYPES:
        return fail("entries must be ordered work -> voice -> validator", code="entry_order_invalid")
    if any(not entry["route_terms"] for entry in selected_entries):
        return fail("route needs library work: every entry must carry route_terms", code="route_needs_library_work")

    profile = str(route.get("profile") or "chapter")
    stage = str(route.get("stage") or "draft")
    chapter_id = str(route.get("chapter_id") or "CH001")
    task_intent = str(route.get("task_intent") or "assemble context")
    route_terms = _unique([
        profile,
        stage,
        chapter_id,
        *[term for entry in selected_entries for term in entry.get("route_terms", [])],
        *[tag for entry in selected_entries for tag in entry.get("tags", [])],
    ])
    context_handoff_id = str(payload.get("context_handoff_id") or "")
    source_handoff_id = str(upstream.get("source_handoff_id") or index.get("handoff_id") or "")

    context_intent = {
        "schema": "zhimeng.o2e-context-pack-intent.v1",
        "context_handoff_id": context_handoff_id,
        "source_handoff_id": source_handoff_id,
        "distillation_index_hash": distillation_index_hash,
        "project": project,
        "profile": profile,
        "stage": stage,
        "chapter_id": chapter_id,
        "task_intent": task_intent,
        "route_terms": route_terms,
        "selected_entries": selected_entries,
        "selection_reasons": [
            f"{entry['card_type']} 入口匹配 {profile}/{stage}/{chapter_id} 写前任务。"
            for entry in selected_entries
        ],
        "context_health": "ready",
        "route_health": "ready",
        "route_evidence_status": "not_validated",
        "readiness": "ready",
        "constraints": constraints,
    }
    route_receipt = {
        "schema": "zhimeng.o2e-nlcl-route-receipt-intent.v1",
        "context_handoff_id": context_handoff_id,
        "source_handoff_id": source_handoff_id,
        "distillation_index_hash": distillation_index_hash,
        "downstream_target": "novel-kb-manager.assemble-context",
        "route_health": "ready",
        "route_evidence_status": "not_validated",
        "route_health_ready_is_not_validated": True,
        "project_truth_not_written": True,
        "formal_draft_allowed": False,
        "selected_entry_ids": [entry["card_id"] for entry in selected_entries],
    }
    context_path = Path.cwd() / "context_pack_intent.json"
    route_path = Path.cwd() / "nlcl_route_receipt_intent.json"
    context_path.write_text(canonical_json(context_intent), encoding="utf-8")
    route_path.write_text(canonical_json(route_receipt), encoding="utf-8")
    context_intent_hash = sha256_json(context_intent)
    route_receipt_intent_hash = sha256_json(route_receipt)

    result = {
        "schema": "zhimeng.novel-kb-context-handoff-result.v1",
        "context_handoff_id": context_handoff_id,
        "status": "context_ready",
        "source_handoff_id": source_handoff_id,
        "distillation_index_hash": distillation_index_hash,
        "downstream_target": "novel-kb-manager.assemble-context",
        "route_health": "ready",
        "route_evidence_status": "not_validated",
        "context_health": "ready",
        "readiness": "ready",
        "profile": profile,
        "stage": stage,
        "chapter_id": chapter_id,
        "task_intent": task_intent,
        "route_terms": route_terms,
        "selected_entries": selected_entries,
        "context_pack_intent_hash": context_intent_hash,
        "route_receipt_intent_hash": route_receipt_intent_hash,
        "constraints": constraints,
        "risk_summary": "route_health=ready 只说明写前上下文可装配，不等于 validated；本轮不写正文、不写项目真值。",
        "next_action": "由 Runtime 持久化 Context Pack / NLCL route receipt，作者再审阅写前上下文。",
    }
    output = {
        "health": "healthy",
        "summary": "novel-kb-manager assemble-context intent is ready",
        "result": result,
        "artifacts": [
            {
                "artifact_type": "context_pack_intent",
                "relative_path": "context_pack_intent.json",
                "payload": {"context_pack_intent_hash": context_intent_hash, "selected_count": 3},
            },
            {
                "artifact_type": "nlcl_route_receipt",
                "relative_path": "nlcl_route_receipt_intent.json",
                "payload": {"route_receipt_intent_hash": route_receipt_intent_hash, "route_health": "ready"},
            },
        ],
    }
    print(canonical_json(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
