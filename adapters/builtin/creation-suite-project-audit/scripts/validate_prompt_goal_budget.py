"""Validate prompt_goal_budget payloads.

The JSON schema owns field shape. This script adds semantic checks so long
prompts, forbidden-word lists and polish lists are compressed into a small
writing budget instead of being passed to drafting as dead commands.

Usage:
    python validate_prompt_goal_budget.py budget.json
    type budget.json | python validate_prompt_goal_budget.py -
    python validate_prompt_goal_budget.py budget.json --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import jsonschema


DEFAULT_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "references"
    / "prompt-goal-budget-schema.json"
)

DEAD_COMMAND_TERMS = [
    "全部执行",
    "全都执行",
    "所有要求",
    "所有规则",
    "逐条执行",
    "照单全收",
    "死命令",
]

PROJECT_TRUTH_TERMS = ["项目真值", "已定稿正文", "人物声音", "章节使命", "用户指令"]
FENCED_JSON_RE = re.compile(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", re.DOTALL)


def _load_json(path: pathlib.Path | str) -> Any:
    if str(path) == "-":
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text)
    with pathlib.Path(path).open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _read_text(path: pathlib.Path | str) -> str:
    if str(path) == "-":
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        return raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
    return pathlib.Path(path).read_text(encoding="utf-8-sig")


def _find_prompt_goal_budget(payload: Any) -> Any | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("prompt_goal_budget"), dict):
            return payload
        if all(key in payload for key in ["version", "accepted_goals", "rejected_or_deferred_rules"]):
            return payload
        for value in payload.values():
            found = _find_prompt_goal_budget(value)
            if found is not None:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_prompt_goal_budget(value)
            if found is not None:
                return found
    return None


def _load_markdown_prompt_goal_budget(path: pathlib.Path | str) -> Any:
    text = _read_text(path)
    candidates: list[str] = [match.group(1) for match in FENCED_JSON_RE.finditer(text)]
    if not candidates and text.lstrip().startswith("{"):
        candidates.append(text)
    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"candidate JSONDecodeError: {exc}")
            continue
        found = _find_prompt_goal_budget(payload)
        if found is not None:
            return found
    if errors:
        raise ValueError("no prompt_goal_budget JSON block found; " + "; ".join(errors[:3]))
    raise ValueError("no prompt_goal_budget JSON block found")


def _json_path(error: jsonschema.ValidationError) -> str:
    if not error.path:
        return "$"
    return "$." + ".".join(str(part) for part in error.path)


def unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("prompt_goal_budget"), dict):
        return payload["prompt_goal_budget"]
    return payload


def validate_payload(payload: Any, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    errors: list[str] = []
    payload = unwrap_payload(payload)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(payload), key=lambda err: list(err.path)):
        errors.append(f"schema:{_json_path(error)}: {error.message}")

    if not isinstance(payload, dict):
        return errors

    accepted_goals = [str(item).strip() for item in payload.get("accepted_goals") or [] if str(item).strip()]
    rejected = " ".join(str(item) for item in payload.get("rejected_or_deferred_rules") or [])
    conflict_resolution = str(payload.get("conflict_resolution") or "")
    acceptance_tests = [str(item).strip() for item in payload.get("acceptance_tests") or [] if str(item).strip()]

    if any(any(term in goal for term in DEAD_COMMAND_TERMS) for goal in accepted_goals):
        errors.append(
            "semantic: accepted_goals must not preserve dead-command language such as 全部执行/逐条执行/死命令"
        )

    if not any(term in rejected for term in ["不逐条", "不全部", "不全都", "延期", "拒绝", "降权"]):
        errors.append(
            "semantic: rejected_or_deferred_rules must explicitly reject/defer/discount part of the long prompt or list"
        )

    if not any(term in conflict_resolution for term in PROJECT_TRUTH_TERMS):
        errors.append(
            "semantic: conflict_resolution must name project truth, finalized prose, user instruction, character voice, or chapter mission priority"
        )

    if len(accepted_goals) != len(set(accepted_goals)):
        errors.append("semantic: accepted_goals must be unique after trimming whitespace")

    if len(acceptance_tests) != len(set(acceptance_tests)):
        errors.append("semantic: acceptance_tests must be unique after trimming whitespace")

    return errors


def validate_file(path: pathlib.Path | str, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    return validate_payload(_load_json(path), schema_path=schema_path)


def validate_markdown_file(path: pathlib.Path | str, schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH) -> list[str]:
    return validate_payload(_load_markdown_prompt_goal_budget(path), schema_path=schema_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="JSON files to validate, Markdown files with fenced JSON, or '-' for stdin")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH), help="Schema path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable result")
    parser.add_argument(
        "--from-markdown",
        action="store_true",
        help="Extract prompt_goal_budget from a fenced JSON block in Markdown before validating.",
    )
    args = parser.parse_args(argv)

    schema_path = pathlib.Path(args.schema)
    all_results: list[dict[str, Any]] = []
    error_count = 0
    for path in args.paths:
        try:
            if args.from_markdown:
                errors = validate_markdown_file(path, schema_path=schema_path)
            else:
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
