from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fail(message: str, *, code: str = "handoff_blocked") -> int:
    result = {
        "schema": "zhimeng.novel-kb-handoff-result.v1",
        "handoff_id": "skill-handoff-" + hashlib.sha256(message.encode("utf-8")).hexdigest()[:32],
        "status": "blocked",
        "upstream_formalization_receipt_id": "knowledge-formalization-" + "0" * 32,
        "upstream_formalization_receipt_hash": "0" * 64,
        "distillation_index_hash": "0" * 64,
        "downstream_target": "novel-kb-manager.import-distillation-index",
        "entries": [],
        "counts": {"work": 1, "voice": 1, "validator": 1},
        "risk_summary": f"{code}: {message}",
        "next_action": "修复 Runtime 交接输入后重新准备，并由作者重新批准。",
    }
    print(canonical_json({"health": "blocked", "summary": message, "result": result, "artifacts": []}))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_snapshot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return fail("stdin is not valid JSON", code="invalid_json")

    snapshot_root = Path(args.project_snapshot)
    snapshot_file = snapshot_root / "project_snapshot.json"
    if not snapshot_root.is_dir() or not snapshot_file.is_file():
        return fail("bound project snapshot is missing project_snapshot.json", code="snapshot_missing")
    try:
        snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fail("bound project snapshot cannot be read", code="snapshot_unreadable")

    project = payload.get("project") if isinstance(payload, dict) else None
    if not isinstance(project, dict):
        return fail("request project binding is missing", code="project_missing")
    if snapshot.get("project_id") != project.get("project_id"):
        return fail("project snapshot does not match request", code="project_mismatch")
    if snapshot.get("revision") != project.get("revision") or snapshot.get("fingerprint") != project.get("fingerprint"):
        return fail("project snapshot is stale", code="project_snapshot_stale")

    approval = payload.get("approval")
    if not isinstance(approval, dict) or approval.get("approved") is not True or approval.get("risk_acknowledged") is not True:
        return fail("author approval is missing", code="approval_missing")
    if payload.get("formal_draft_allowed") is not False:
        return fail("formal draft output is not allowed for this handoff", code="formal_draft_not_allowed")

    upstream = payload.get("upstream")
    if not isinstance(upstream, dict):
        return fail("upstream formalized batch is missing", code="upstream_missing")
    cards = upstream.get("cards")
    if not isinstance(cards, list) or len(cards) != 3:
        return fail("handoff requires exactly three cards", code="card_count_invalid")
    types = [str(card.get("card_type")) for card in cards if isinstance(card, dict)]
    if types != ["work", "voice", "validator"]:
        return fail("cards must be ordered work -> voice -> validator", code="card_order_invalid")

    entries: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            return fail("card entry is not an object", code="card_invalid")
        entries.append({
            "card_id": str(card["card_id"]),
            "card_type": str(card["card_type"]),
            "version_id": str(card["version_id"]),
            "content_hash": str(card["content_hash"]),
            "title": str(card["title"]),
            "route_terms": list(card.get("route_terms") or []),
            "tags": list(card.get("tags") or []),
            "safety_summary": str(card.get("safety_summary") or "只使用抽象机制与路由入口，不复制来源正文。"),
        })
    index = {
        "schema": "zhimeng.nlcl-distillation-index-handoff.v1",
        "handoff_id": payload["handoff_id"],
        "project": project,
        "upstream_formalization_receipt_id": upstream["formalization_receipt_id"],
        "upstream_formalization_receipt_hash": upstream["formalization_receipt_hash"],
        "source_import_id": upstream["source_import_id"],
        "source_skill_id": "novel-distillation",
        "target_skill_id": "novel-kb-manager",
        "target_capability": "import-distillation-index",
        "entries": entries,
        "no_full_card_text_copied": True,
    }
    index_hash = sha256_json(index)
    artifact_path = Path.cwd() / "distillation_index_handoff.json"
    artifact_path.write_text(canonical_json(index), encoding="utf-8")
    result = {
        "schema": "zhimeng.novel-kb-handoff-result.v1",
        "handoff_id": payload["handoff_id"],
        "status": "ready_for_downstream",
        "upstream_formalization_receipt_id": upstream["formalization_receipt_id"],
        "upstream_formalization_receipt_hash": upstream["formalization_receipt_hash"],
        "distillation_index_hash": index_hash,
        "downstream_target": "novel-kb-manager.import-distillation-index",
        "entries": entries,
        "counts": {"work": 1, "voice": 1, "validator": 1},
        "risk_summary": "仅交接 active 卡入口、版本和路由摘要；不复制蒸馏卡全文，不授权正文写回。",
        "next_action": "作者可在后续 O2-D 相邻边中让 novel-kb-manager 显式消费该 index 入口。",
    }
    output = {
        "health": "healthy",
        "summary": "novel-distillation -> novel-kb-manager handoff artifact is ready",
        "result": result,
        "artifacts": [
            {
                "artifact_type": "distillation_index_handoff",
                "relative_path": "distillation_index_handoff.json",
                "payload": {"distillation_index_hash": index_hash, "entry_count": 3},
            }
        ],
    }
    print(canonical_json(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
