"""Validate unified issue cards for novel-creation-suite.

The JSON Schema owns field shape. This script adds cross-field checks that keep
issue cards usable by the review -> distillation guidance -> score-improver
loop.

Usage:
    python validate_unified_issue_card.py issue.json
    python validate_unified_issue_card.py issues.json --json
    type issues.json | python validate_unified_issue_card.py -
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import pathlib
import re
import sys
from typing import Any

import jsonschema


DEFAULT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "references"
    / "unified-issue-card-schema.json"
)

LAYER_ORDER = {
    "L0_INPUT": 0,
    "L1_PROJECT_TRUTH": 1,
    "L2_CONTROL_CARD": 2,
    "L3_DISTILLATION_RETRIEVAL": 3,
    "L4_DRAFT_EXECUTION": 4,
    "L5_DEAI_STYLE": 5,
    "L6_REVIEW_ACCEPTANCE": 6,
    "L7_WRITEBACK": 7,
}
SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


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


def iter_issue_cards(payload: Any) -> list[tuple[str, Any]]:
    if isinstance(payload, list):
        return [(f"$[{index}]", item) for index, item in enumerate(payload)]
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return [(f"$.issues[{index}]", item) for index, item in enumerate(payload["issues"])]
    return [("$", payload)]


def _layer_index(value: Any) -> int | None:
    return LAYER_ORDER.get(value) if isinstance(value, str) else None


def _has_distillation_route(issue: dict[str, Any]) -> bool:
    return issue.get("layer_id") == "L3_DISTILLATION_RETRIEVAL" or issue.get("rewrite_from") == "L3_DISTILLATION_RETRIEVAL"


def _status_history_matches(issue: dict[str, Any], status: str) -> bool:
    history = issue.get("status_history")
    if not isinstance(history, list):
        return False
    latest = next((row for row in reversed(history) if isinstance(row, dict)), None)
    return bool(
        isinstance(latest, dict)
        and latest.get("to") == status
        and str(latest.get("evidence") or "").strip()
    )


def _parse_iso8601_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _is_iso8601_datetime(value: Any) -> bool:
    return _parse_iso8601_datetime(value) is not None


def validate_issue_semantics(issue: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    layer_index = _layer_index(issue.get("layer_id"))
    rewrite_index = _layer_index(issue.get("rewrite_from"))
    if layer_index is not None and rewrite_index is not None and rewrite_index > layer_index:
        errors.append(f"{path}: semantic: rewrite_from cannot be later than layer_id")

    do_not_rewrite_below = issue.get("do_not_rewrite_below") or []
    if isinstance(do_not_rewrite_below, list) and rewrite_index is not None:
        blocked = [
            layer
            for layer in do_not_rewrite_below
            if _layer_index(layer) is not None and _layer_index(layer) >= rewrite_index
        ]
        if blocked:
            errors.append(
                f"{path}: semantic: do_not_rewrite_below contains layer(s) at or below rewrite_from: {', '.join(blocked)}"
            )

    confidence = issue.get("layer_confidence")
    status = issue.get("status")
    if isinstance(confidence, (int, float)) and confidence < 0.6 and status == "fixed":
        errors.append(f"{path}: semantic: layer_confidence < 0.6 cannot be status=fixed")
    if status in {"fixed", "rejected"}:
        if not str(issue.get("status_evidence") or "").strip():
            errors.append(f"{path}: semantic: status={status} requires non-empty status_evidence")
        if not _status_history_matches(issue, str(status)):
            errors.append(f"{path}: semantic: status={status} requires status_history entry with matching to={status} and evidence")
    history = issue.get("status_history")
    if isinstance(history, list):
        previous_changed_at: datetime | None = None
        previous_to: str | None = None
        latest_to: str | None = None
        for index, row in enumerate(history):
            if not isinstance(row, dict):
                continue
            changed_at = _parse_iso8601_datetime(row.get("changed_at"))
            if changed_at is None:
                errors.append(f"{path}: semantic: status_history[{index}].changed_at must be ISO 8601 with timezone")
                continue
            if previous_changed_at is not None and changed_at < previous_changed_at:
                errors.append(f"{path}: semantic: status_history changed_at must be chronological")
            previous_changed_at = changed_at
            row_from = row.get("from")
            row_to = row.get("to")
            if index > 0 and previous_to is not None and row_from != previous_to:
                errors.append(f"{path}: semantic: status_history chain must be continuous")
            if isinstance(row_to, str):
                previous_to = row_to
                latest_to = row_to
        if latest_to is not None and status != latest_to:
            errors.append(f"{path}: semantic: status must match latest status_history.to")

    symptom = str(issue.get("problem_symptom") or "").strip()
    if len(symptom) > 40:
        errors.append(f"{path}: semantic: problem_symptom should be concise enough for retrieval, got {len(symptom)} chars")

    signature = str(issue.get("failure_signature") or "").strip()
    if signature and not SIGNATURE_PATTERN.match(signature):
        errors.append(
            f"{path}: semantic: failure_signature should be a stable machine key using letters, digits, '.', ':', '-' or '_'"
        )

    guidance = issue.get("distillation_repair_guidance")
    if isinstance(guidance, dict):
        suggested_layer = guidance.get("suggested_layer")
        accepted = guidance.get("accepted_as_route")
        if accepted is True and suggested_layer not in {issue.get("layer_id"), issue.get("rewrite_from")}:
            errors.append(
                f"{path}: semantic: accepted distillation guidance must match layer_id or rewrite_from"
            )
        if guidance.get("problem_symptom") and guidance.get("problem_symptom") != issue.get("problem_symptom"):
            symptoms = set(issue.get("problem_symptoms") or [])
            if guidance.get("problem_symptom") not in symptoms:
                errors.append(
                    f"{path}: semantic: guidance problem_symptom must match problem_symptom or problem_symptoms"
                )
    elif _has_distillation_route(issue):
        errors.append(
            f"{path}: semantic: L3_DISTILLATION_RETRIEVAL issue should include distillation_repair_guidance or an explicit rejected/deferred rationale"
        )

    return errors


def validate_payload(payload: Any, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    errors: list[str] = []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    for path, issue in iter_issue_cards(payload):
        for error in sorted(validator.iter_errors(issue), key=lambda err: list(err.path)):
            errors.append(f"{path}: schema:{_json_path(error)}: {error.message}")
        if isinstance(issue, dict):
            errors.extend(validate_issue_semantics(issue, path))
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
