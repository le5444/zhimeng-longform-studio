"""Validate CHxxx_review_acceptance.json artifacts.

The JSON schema owns shape. This script adds semantic checks so a review
acceptance artifact cannot claim approved/writeback-ready when local AI patch
application or freeform-AI parsing gates are missing, failed, or internally
inconsistent, or when no accountable human judgment accepted the exact prose
version.

Usage:
    python validate_review_acceptance.py CH001_review_acceptance.json
    python validate_review_acceptance.py CH001_review_acceptance.json --json
    type review_acceptance.json | python validate_review_acceptance.py -
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import sys
from typing import Any

import jsonschema


DEFAULT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "references"
    / "review-acceptance-schema.json"
)

APPROVED_STATUSES = {"approved"}
PLACEHOLDER_TEXT = {
    "",
    "pending",
    "missing",
    "not_applicable",
    "none",
    "无",
    "todo",
    "待补",
    "needs_fill",
    "not_applicable_until_selection_or_local_replace_requested",
    "not_applicable_until_ai_result_candidate_selected",
    "not_applicable_until_freeform_ai_output_is_parsed",
}


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


def _is_placeholder_text(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    return value.strip().casefold() in PLACEHOLDER_TEXT


def _nonempty_ref(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() not in PLACEHOLDER_TEXT


def _is_rfc3339_datetime(value: Any) -> bool:
    """JSON Schema format is annotation-only under draft 2020-12 in some runtimes."""
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        normalized,
    ):
        return False
    try:
        dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _normalized_sha256(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1].strip()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _accepted_version_binding_errors(
    accepted_version: Any,
    *,
    approved_like: bool,
    artifact_path: pathlib.Path | None,
) -> list[str]:
    """Require an approved acceptance to identify and bind exact prose bytes."""
    if not approved_like:
        return []
    errors: list[str] = []
    if not isinstance(accepted_version, dict):
        return ["semantic: approved review requires accepted_version object"]
    reviewed_file = accepted_version.get("reviewed_file")
    if not _nonempty_ref(reviewed_file):
        return ["semantic: approved review requires accepted_version.reviewed_file"]
    declared_hash = _normalized_sha256(accepted_version.get("content_hash"))
    if not declared_hash:
        return ["semantic: approved review requires accepted_version.content_hash SHA-256"]
    # `validate_payload` is deliberately usable by in-memory callers.  Byte
    # verification occurs whenever the normal file-based CLI/API has a base
    # directory, avoiding a false claim that an arbitrary relative path was
    # checked without a filesystem anchor.
    if artifact_path is None:
        return errors
    prose_path = pathlib.Path(str(reviewed_file))
    if not prose_path.is_absolute():
        prose_path = artifact_path.parent / prose_path
    if not prose_path.is_file():
        return [
            "semantic: accepted_version.reviewed_file is missing or not a regular file: "
            f"{prose_path}"
        ]
    actual_hash = hashlib.sha256(prose_path.read_bytes()).hexdigest()
    if actual_hash != declared_hash:
        errors.append(
            "semantic: accepted_version.content_hash does not match reviewed_file bytes"
        )
    return errors


def _selection_apply_gate_errors(payload: dict[str, Any], approved_like: bool) -> list[str]:
    errors: list[str] = []
    gate = payload.get("selection_apply_manifest_gate")
    if not isinstance(gate, dict):
        errors.append("semantic: review_acceptance requires selection_apply_manifest_gate")
        return errors

    status = str(gate.get("status") or "missing")
    applies = gate.get("applies") is True
    if approved_like and status in {"fail", "missing"}:
        errors.append("semantic: approved review requires selection_apply_manifest_gate not fail/missing")
    if not applies:
        if status not in {"not_applicable", "pass", "warn"}:
            errors.append("semantic: non-applicable selection_apply_manifest_gate should be not_applicable/pass/warn")
        return errors

    for field in (
        "target_artifact",
        "target_span_ref",
        "target_span_hash_before",
        "source_result_id",
        "acceptance_owner",
        "rollback_ref",
    ):
        if _is_placeholder_text(gate.get(field)):
            errors.append(f"semantic: selection_apply_manifest_gate.{field} required when applies=true")
    if gate.get("preview_required") is not True:
        errors.append("semantic: selection_apply_manifest_gate.preview_required must be true when applies=true")
    if gate.get("diff_required") is not True:
        errors.append("semantic: selection_apply_manifest_gate.diff_required must be true when applies=true")
    mode = str(gate.get("apply_mode") or "")
    allowed = {str(item) for item in gate.get("allowed_mutations", [])} if isinstance(gate.get("allowed_mutations"), list) else set()
    if mode not in {"insert_after", "replace_selection", "append_candidate", "patch_suggestion"}:
        errors.append("semantic: selection_apply_manifest_gate.apply_mode invalid when applies=true")
    elif mode not in allowed:
        errors.append("semantic: selection_apply_manifest_gate.apply_mode must be listed in allowed_mutations")
    if not isinstance(gate.get("selected_text_char_count"), int) or gate.get("selected_text_char_count") < 0:
        errors.append("semantic: selection_apply_manifest_gate.selected_text_char_count must be a non-negative integer")
    if approved_like and status != "pass":
        errors.append("semantic: approved review with selection apply requires selection_apply_manifest_gate.status=pass")
    return errors


def _parse_confidence_gate_errors(payload: dict[str, Any], approved_like: bool) -> list[str]:
    errors: list[str] = []
    gate = payload.get("ai_output_parse_confidence_gate")
    if not isinstance(gate, dict):
        errors.append("semantic: review_acceptance requires ai_output_parse_confidence_gate")
        return errors

    status = str(gate.get("status") or "missing")
    applies = gate.get("applies") is True
    if approved_like and status in {"fail", "missing"}:
        errors.append("semantic: approved review requires ai_output_parse_confidence_gate not fail/missing")
    if not applies:
        if status not in {"not_applicable", "pass", "warn"}:
            errors.append("semantic: non-applicable ai_output_parse_confidence_gate should be not_applicable/pass/warn")
        return errors

    for field in ("expected_schema", "parse_method", "candidate_status"):
        if _is_placeholder_text(gate.get(field)):
            errors.append(f"semantic: ai_output_parse_confidence_gate.{field} required when applies=true")
    required = [str(item) for item in gate.get("required_fields", [])] if isinstance(gate.get("required_fields"), list) else []
    presence = gate.get("parsed_field_presence")
    if not isinstance(presence, dict):
        errors.append("semantic: ai_output_parse_confidence_gate.parsed_field_presence must be object")
        presence = {}
    missing = [field for field in required if presence.get(field) is not True]
    if missing:
        errors.append(f"semantic: ai_output_parse_confidence_gate missing parsed fields: {missing}")

    confidence_raw = gate.get("parse_confidence")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = -1.0
        errors.append("semantic: ai_output_parse_confidence_gate.parse_confidence must be numeric")
    if not math.isfinite(confidence):
        confidence = -1.0
        errors.append("semantic: ai_output_parse_confidence_gate.parse_confidence must be finite")
    ambiguous = gate.get("ambiguous_or_unparsed_blocks")
    ambiguous_count = len(ambiguous) if isinstance(ambiguous, list) else 0

    if confidence < 0.85 or missing or ambiguous_count:
        if gate.get("manual_review_required") is not True:
            errors.append("semantic: low-confidence/ambiguous ai_output_parse_confidence_gate requires manual_review_required=true")
        if gate.get("do_not_promote_to_truth") is not True and not _nonempty_ref(gate.get("author_decision_ref")):
            errors.append("semantic: low-confidence/ambiguous ai_output_parse_confidence_gate cannot promote to truth without author_decision_ref")
        if approved_like:
            errors.append("semantic: approved review cannot rely on low-confidence/ambiguous ai_output_parse_confidence_gate")
    if gate.get("do_not_promote_to_truth") is False:
        if gate.get("candidate_status") != "adopted_with_author_decision":
            errors.append("semantic: ai_output_parse_confidence_gate may promote to truth only when candidate_status=adopted_with_author_decision")
        if not _nonempty_ref(gate.get("author_decision_ref")):
            errors.append("semantic: ai_output_parse_confidence_gate truth promotion requires author_decision_ref")
    if approved_like and status != "pass":
        errors.append("semantic: approved review with parse apply requires ai_output_parse_confidence_gate.status=pass")
    return errors


def _human_judgment_gate_errors(payload: dict[str, Any], approved_like: bool) -> list[str]:
    """Require accountable human judgment before a review can accept prose."""
    errors: list[str] = []
    gate = payload.get("human_judgment_gate")
    if not isinstance(gate, dict):
        errors.append("semantic: review_acceptance requires human_judgment_gate")
        return errors

    if not approved_like:
        return errors

    if gate.get("status") != "pass":
        errors.append("semantic: approved review requires human_judgment_gate.status=pass")
    if gate.get("reviewer_mode") not in {"human", "human_with_ai_assist"}:
        errors.append("semantic: approved review requires human_judgment_gate.reviewer_mode human or human_with_ai_assist")
    if gate.get("truth_checked") is not True:
        errors.append("semantic: approved review requires human_judgment_gate.truth_checked=true")
    for field in ("cold_read_evidence", "acceptance_owner", "reason"):
        if _is_placeholder_text(gate.get(field)):
            errors.append(f"semantic: approved review requires human_judgment_gate.{field}")
    return errors


def validate_payload(
    payload: Any,
    schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH,
    *,
    artifact_path: pathlib.Path | None = None,
) -> list[str]:
    errors: list[str] = []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    for error in sorted(validator.iter_errors(payload), key=lambda err: list(err.path)):
        errors.append(f"schema:{_json_path(error)}: {error.message}")

    if not isinstance(payload, dict):
        return errors

    approved_like = str(payload.get("review_status") or "") in APPROVED_STATUSES
    errors.extend(_selection_apply_gate_errors(payload, approved_like))
    errors.extend(_parse_confidence_gate_errors(payload, approved_like))
    errors.extend(_human_judgment_gate_errors(payload, approved_like))

    if approved_like:
        score = payload.get("score")
        threshold = payload.get("threshold")
        score_is_finite = isinstance(score, (int, float)) and math.isfinite(float(score))
        threshold_is_finite = isinstance(threshold, (int, float)) and math.isfinite(float(threshold))
        if not score_is_finite:
            errors.append("semantic: approved review requires finite score")
        if not threshold_is_finite:
            errors.append("semantic: approved review requires finite threshold")
        if score_is_finite and threshold_is_finite and score < threshold:
            errors.append("semantic: approved review requires score >= threshold")
        accepted_version = payload.get("accepted_version")
        if not isinstance(accepted_version, dict) or not _is_rfc3339_datetime(accepted_version.get("accepted_at")):
            errors.append("semantic: approved review requires accepted_version.accepted_at as RFC3339 date-time")
        errors.extend(
            _accepted_version_binding_errors(
                accepted_version,
                approved_like=True,
                artifact_path=artifact_path,
            )
        )
        readability_cap = payload.get("readability_score_cap")
        if isinstance(readability_cap, dict) and readability_cap.get("triggered") is True:
            cap_score = readability_cap.get("cap_score")
            if not isinstance(cap_score, (int, float)) or not math.isfinite(float(cap_score)):
                errors.append("semantic: approved review with readability cap requires finite numeric cap_score")
            elif score_is_finite and score > cap_score:
                errors.append("semantic: approved review score cannot exceed triggered readability_score_cap")
        closure = payload.get("mandatory_usage_closure")
        if not isinstance(closure, dict) or closure.get("overall_status") not in {"closed", "not_applicable"}:
            errors.append("semantic: approved review requires mandatory_usage_closure.overall_status=closed or not_applicable")
        open_high_priority = [
            str(item.get("issue_id") or item.get("failure_signature") or "unnamed")
            for item in payload.get("issue_cards", [])
            if isinstance(item, dict)
            and item.get("priority") in {"P0", "P1"}
            and item.get("status") == "open"
        ]
        if open_high_priority:
            errors.append(f"semantic: approved review cannot retain open P0/P1 issue_cards: {open_high_priority}")
        for field in ("hard_logic_review", "soft_quality_review"):
            gate = payload.get(field)
            if isinstance(gate, dict) and gate.get("status") != "pass":
                errors.append(f"semantic: approved review requires {field}.status=pass")

    return errors


def validate_file(path: pathlib.Path | str, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    source = None if str(path) == "-" else pathlib.Path(path)
    return validate_payload(
        _load_json(path),
        schema_path=schema_path,
        artifact_path=source,
    )


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

    result = {"ok": error_count == 0, "error_count": error_count, "results": all_results}
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
