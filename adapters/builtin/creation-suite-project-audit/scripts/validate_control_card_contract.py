#!/usr/bin/env python3
"""Validate chapter control card contract fields.

This is a lightweight validator for creation-suite control cards. It does not
try to score prose quality; it only checks whether the control card contains
the fields and sub-fields required to safely hand work from NLCL into the
writer hot path.

Usage:
    python validate_control_card_contract.py card.md
    python validate_control_card_contract.py card.md --require-nlcl --require-continuation --json
    type card.md | python validate_control_card_contract.py - --require-xianxia
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any


PLACEHOLDER_MARKERS = {
    "todo",
    "pending",
    "needs_fill",
    "tbd",
    "待补",
    "待填写",
    "待完善",
    "未填写",
}

DRY_RUN_MARKERS = [
    "dry-run 控制卡",
    "dry_run_writer_harness.py",
    "接口压测产物",
    "不是正式正文控制卡",
    "本文件只证明接口形状",
    "dry_run_boundary",
]

SHORT_FORM_MARKERS = [
    "短篇",
    "short_setup",
    "short_draft",
    "short_polish",
    "short_review",
    "short_twist_review",
    "stage/form/profile: short",
    "form/profile: short",
    "form: short",
]

NLCL_TRIGGER_TERMS = [
    "novel-kb-manager",
    "nlcl",
    "chapter_call_list",
    "selected_ops_rules",
    "selected_distillation_entries",
    "route_health",
    "context_degraded",
    "context_health",
    "state_freshness",
]

XIANXIA_TRIGGER_TERMS = [
    "修仙",
    "仙侠",
    "玄幻",
    "东方玄幻",
    "凡人修仙",
    "宗门",
    "灵根",
    "功法",
    "丹药",
    "法宝",
    "秘境",
    "渡劫",
    "xianxia_xuanhuan_chapter_contract",
]


@dataclass(frozen=True)
class Requirement:
    field: str
    required_terms: tuple[str, ...] = ()


CORE_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("spec_success_criteria"),
    Requirement("task_goal"),
    Requirement("reader_promise"),
    Requirement("required_elements_gate"),
    Requirement("acceptance_threshold"),
    Requirement("evidence_required"),
    Requirement("failure_routing"),
    Requirement("do_not_consume_yet"),
    Requirement("word_count_authority_chain"),
)

NLCL_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "context_inclusion_trace",
        ("why_included", "must_consume", "evidence_target"),
    ),
    Requirement(
        "activation_mode_audit",
        ("always_on", "on_demand", "validator_only"),
    ),
    Requirement(
        "distractor_file_budget",
        ("max_nonhot_files", "over_budget_route"),
    ),
    Requirement(
        "block_context_bundle",
        ("truth_bindings", "current_state_bindings", "forbidden_crossing"),
    ),
    Requirement(
        "readonly_bible_gate",
        ("readonly", "writeback"),
    ),
    Requirement(
        "delegated_edit_checkpoint_gate",
        ("draft_scope", "deai_scope", "review_scope"),
    ),
    Requirement(
        "accepted_version_guard",
        ("current_version_id", "accepted_version_id"),
    ),
)

CONTINUATION_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("continuation_state_contract"),
)

XIANXIA_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("xianxia_xuanhuan_chapter_contract"),
    Requirement("realm_state"),
    Requirement("resource_ledger"),
    Requirement("sect_or_rule_pressure"),
    Requirement("technique_cost_limit"),
    Requirement("visible_trial_or_conflict"),
    Requirement("earned_payoff"),
    Requirement("status_delta"),
    Requirement("future_debt"),
)


def _read_text(path: str) -> str:
    if path == "-":
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
        return raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
    return pathlib.Path(path).read_text(encoding="utf-8-sig")


def _normalize(text: str) -> str:
    return text.casefold()


def _looks_like_placeholder(text: str) -> bool:
    normalized = _normalize(text)
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _extract_window(lines: list[str], field: str, span: int = 12) -> tuple[int, str] | None:
    field_cf = field.casefold()
    for idx, line in enumerate(lines):
        if field_cf in line.casefold():
            start = idx
            end = min(len(lines), idx + span)
            return idx, "\n".join(lines[start:end])
    return None


def _detect_chapter_number(path: str, text: str) -> int | None:
    patterns = [
        re.compile(r"\bCH0*(\d{1,4})\b", re.IGNORECASE),
        re.compile(r"第0*(\d{1,4})章"),
    ]
    search_pool = f"{path}\n{text[:1000]}"
    for pattern in patterns:
        match = pattern.search(search_pool)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _missing_required_terms(window: str, required_terms: tuple[str, ...]) -> list[str]:
    if not required_terms:
        return []
    normalized = _normalize(window)
    return [term for term in required_terms if term.casefold() not in normalized]


def _field_value_placeholder(window: str, field: str) -> bool:
    normalized_field = field.casefold()
    for line in window.splitlines():
        if normalized_field in line.casefold():
            if _looks_like_placeholder(line):
                return True
    return False


def _is_dry_run_artifact(path: str, text: str) -> bool:
    normalized = _normalize(f"{path}\n{text[:4000]}")
    return any(marker.casefold() in normalized for marker in DRY_RUN_MARKERS)


def _is_short_form(path: str, text: str) -> bool:
    normalized = _normalize(f"{path}\n{text[:4000]}")
    return any(marker.casefold() in normalized for marker in SHORT_FORM_MARKERS)


def detect_modes(path: str, text: str, args: argparse.Namespace) -> dict[str, bool]:
    normalized = _normalize(text)
    chapter_number = _detect_chapter_number(path, text)
    short_form = _is_short_form(path, text)
    require_nlcl = args.require_nlcl or any(term in normalized for term in NLCL_TRIGGER_TERMS)
    require_continuation = (
        args.require_continuation
        or "continuation_state" in normalized
        or "state_freshness" in normalized
        or (chapter_number is not None and chapter_number >= 2 and not short_form)
    )
    require_xianxia = args.require_xianxia or any(term in normalized for term in XIANXIA_TRIGGER_TERMS)
    return {
        "require_nlcl": require_nlcl,
        "require_continuation": require_continuation,
        "require_xianxia": require_xianxia,
        "short_form": short_form,
    }


def validate_text(path: str, text: str, args: argparse.Namespace) -> dict[str, Any]:
    if _is_dry_run_artifact(path, text):
        return {
            "ok": True,
            "status": "skipped",
            "skipped": True,
            "skip_reason": "dry_run_artifact",
            "path": path,
            "modes": detect_modes(path, text, args),
            "required_fields": [],
            "present_fields": [],
            "missing_fields": [],
            "placeholder_fields": [],
            "subterm_gaps": {},
            "warnings": [
                "skip: dry-run control card artifact; validate a formal control card instead"
            ],
            "errors": [],
        }

    lines = text.splitlines()
    errors: list[str] = []
    warnings: list[str] = []
    present_fields: list[str] = []
    placeholder_fields: list[str] = []
    subterm_gaps: dict[str, list[str]] = {}

    modes = detect_modes(path, text, args)
    requirements: list[Requirement] = list(CORE_REQUIREMENTS)
    if modes["require_nlcl"]:
        requirements.extend(NLCL_REQUIREMENTS)
    if modes["require_continuation"]:
        requirements.extend(CONTINUATION_REQUIREMENTS)
    if modes["require_xianxia"]:
        requirements.extend(XIANXIA_REQUIREMENTS)

    for requirement in requirements:
        found = _extract_window(lines, requirement.field)
        if found is None:
            errors.append(f"missing:{requirement.field}")
            continue
        _, window = found
        present_fields.append(requirement.field)
        if _field_value_placeholder(window, requirement.field):
            placeholder_fields.append(requirement.field)
            errors.append(f"placeholder:{requirement.field}")
        missing_terms = _missing_required_terms(window, requirement.required_terms)
        if missing_terms:
            subterm_gaps[requirement.field] = missing_terms
            errors.append(
                f"subterms:{requirement.field}: missing {', '.join(missing_terms)}"
            )

    if modes["require_nlcl"] and "suppressed_for_project_truth" not in _normalize(text):
        warnings.append(
            "nlcl: no explicit suppressed_for_project_truth trace found; selected/suppressed audit may be incomplete"
        )

    if modes["require_nlcl"] and "validator_only" in _normalize(text) and "block_context_bundle" not in present_fields:
        errors.append("nlcl: validator_only present without block_context_bundle")

    result = {
        "ok": not errors,
        "status": "ok" if not errors else "error",
        "skipped": False,
        "path": path,
        "modes": modes,
        "required_fields": [req.field for req in requirements],
        "present_fields": present_fields,
        "missing_fields": [item.split(":", 1)[1] for item in errors if item.startswith("missing:")],
        "placeholder_fields": placeholder_fields,
        "subterm_gaps": subterm_gaps,
        "warnings": warnings,
        "errors": errors,
    }
    return result


def validate_file(path: str, args: argparse.Namespace) -> dict[str, Any]:
    try:
        text = _read_text(path)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "error",
            "skipped": False,
            "path": path,
            "modes": {},
            "required_fields": [],
            "present_fields": [],
            "missing_fields": [],
            "placeholder_fields": [],
            "subterm_gaps": {},
            "warnings": [],
            "errors": [f"load:{exc.__class__.__name__}: {exc}"],
        }
    return validate_text(path, text, args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Markdown control card file(s), or '-' for stdin")
    parser.add_argument("--require-nlcl", action="store_true", help="Force NLCL contract fields")
    parser.add_argument("--require-continuation", action="store_true", help="Force continuation contract fields")
    parser.add_argument("--require-xianxia", action="store_true", help="Force xianxia/xuanhuan contract fields")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    results = [validate_file(path, args) for path in args.paths]
    error_count = sum(len(item["errors"]) for item in results)
    output = {
        "ok": error_count == 0,
        "error_count": error_count,
        "results": results,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for item in results:
            if item.get("skipped"):
                status = "SKIP"
            else:
                status = "OK" if item["ok"] else "ERROR"
            print(f"{status} {item['path']}")
            if item["modes"]:
                print(f"  modes: {json.dumps(item['modes'], ensure_ascii=False)}")
            for warning in item["warnings"]:
                print(f"  - warning: {warning}")
            for error in item["errors"]:
                print(f"  - {error}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
