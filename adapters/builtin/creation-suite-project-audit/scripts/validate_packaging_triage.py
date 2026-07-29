"""Validate packaging_triage payloads.

The JSON schema owns field shape. This script adds semantic checks for the
low-read triage flow so a low-click/low-read task cannot skip packaging and
jump straight into body-quality rewriting.

Exit code 0 = valid. Non-zero = schema or semantic errors.

Usage:
    python validate_packaging_triage.py triage.json
    type triage.json | python validate_packaging_triage.py -
    python validate_packaging_triage.py --json triage.json
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
    / "packaging-triage-schema.json"
)

LOW_READ_TRIGGERS = {"低阅读", "点击低", "首日数据弱", "曝光转化弱", "没人看"}
PROJECT_TRUTH_ACTIONS = {"novel-title-tags", "novel-synopsis", "novel-selling-points", "novel-market-analysis"}
CONTROL_CARD_ACTIONS = {"novel-golden-three-chapters", "novel-retention-design", "novel-chapter-outline", "novel-chapter-brief"}
BODY_ACTIONS = {"novel-volume-folder"}


def _load_json(path: pathlib.Path | str) -> Any:
    if str(path) == "-":
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text)
    with pathlib.Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _json_path(error: jsonschema.ValidationError) -> str:
    if not error.path:
        return "$"
    return "$." + ".".join(str(part) for part in error.path)


def _as_int_score(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def validate_payload(payload: Any, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    errors: list[str] = []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(payload), key=lambda err: list(err.path)):
        errors.append(f"schema:{_json_path(error)}: {error.message}")

    if not isinstance(payload, dict):
        return errors

    triggers = set(payload.get("trigger") or [])
    next_actions = set(payload.get("next_action") or [])
    verdict = payload.get("triage_verdict")
    return_layer = payload.get("return_layer")
    low_read_triggered = bool(triggers & LOW_READ_TRIGGERS)
    evidence = str(payload.get("evidence") or "").strip().lower()
    title_score = _as_int_score(payload.get("title_score"))
    cover_copy_score = _as_int_score(payload.get("cover_copy_score"))
    synopsis_score = _as_int_score(payload.get("synopsis_front30_score"))
    tag_score = _as_int_score(payload.get("tag_fit_score"))
    front500 = payload.get("chapter_front500_promise_match")

    if low_read_triggered and verdict == "body_quality_review":
        strong_packaging = (
            title_score is not None
            and title_score >= 85
            and synopsis_score is not None
            and synopsis_score >= 80
            and tag_score is not None
            and tag_score >= 80
            and (cover_copy_score is None or cover_copy_score >= 80)
            and front500 in {"pass", "warn"}
        )
        if not strong_packaging:
            errors.append(
                "semantic: low-read triggers cannot route to body_quality_review until title/synopsis/tag/cover/front500 are proven acceptable"
            )
        if not (BODY_ACTIONS & next_actions or "novel-project-reviewer" in next_actions):
            errors.append(
                "semantic: body_quality_review must include novel-volume-folder or novel-project-reviewer in next_action"
            )

    if low_read_triggered and evidence in {"", "pending"} and verdict not in {"platform_wait_and_observe"}:
        errors.append(
            "semantic: low-read triage with pending evidence must use platform_wait_and_observe or collect evidence first"
        )

    if return_layer == "L1_PROJECT_TRUTH" and not (PROJECT_TRUTH_ACTIONS & next_actions):
        errors.append(
            "semantic: L1_PROJECT_TRUTH return_layer must include a project-positioning action such as novel-title-tags or novel-synopsis"
        )
    if return_layer == "L2_CONTROL_CARD" and not (CONTROL_CARD_ACTIONS & next_actions):
        errors.append(
            "semantic: L2_CONTROL_CARD return_layer must include golden-three-chapters, retention, outline, or chapter-brief action"
        )
    if return_layer == "L4_DRAFT_EXECUTION" and not (BODY_ACTIONS & next_actions):
        errors.append(
            "semantic: L4_DRAFT_EXECUTION return_layer must include novel-volume-folder in next_action"
        )
    if return_layer == "L6_REVIEW_ACCEPTANCE" and "manual_decision" not in next_actions and "novel-project-reviewer" not in next_actions:
        errors.append(
            "semantic: L6_REVIEW_ACCEPTANCE return_layer must include manual_decision or novel-project-reviewer"
        )

    if title_score is not None and title_score < 80 and verdict == "body_quality_review":
        errors.append("semantic: title_score < 80 requires title or packaging rework before body review")
    if synopsis_score is not None and synopsis_score < 80 and verdict == "body_quality_review":
        errors.append("semantic: synopsis_front30_score < 80 requires synopsis or packaging rework before body review")
    if tag_score is not None and tag_score < 80 and verdict == "body_quality_review":
        errors.append("semantic: tag_fit_score < 80 requires tag or packaging rework before body review")

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
