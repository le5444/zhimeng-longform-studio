"""External blind-review matrix gate for Local v1 evaluation residuals.

Fail-closed:
- missing/invalid evidence => matrix not ready
- every required reviewer x sample cell must be status=passed
- never sets quality_assessed=true or unlocks formal draft
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


POLICY_VERSION = "local-v1-external-blind-review-matrix-1"
EVIDENCE_SCHEMA = "zhimeng.external-blind-review-matrix-evidence.v1"
RECEIPT_SCHEMA = "zhimeng.external-blind-review-matrix-receipt.v1"
RESPONSE_SCHEMA = "zhimeng.external-blind-review-matrix-response.v1"
HASH_SCHEMA = "zhimeng.external-blind-review-matrix-receipt-hash.v1"

ReviewerId = Literal[
    "external_reviewer_1",
    "external_reviewer_2",
    "external_reviewer_3",
]
SampleId = Literal[
    "chapter_sample_a",
    "chapter_sample_b",
]
CellStatus = Literal["passed", "failed", "missing", "skipped"]

REQUIRED_CELLS: tuple[tuple[ReviewerId, SampleId], ...] = (
    ("external_reviewer_1", "chapter_sample_a"),
    ("external_reviewer_1", "chapter_sample_b"),
    ("external_reviewer_2", "chapter_sample_a"),
    ("external_reviewer_2", "chapter_sample_b"),
    ("external_reviewer_3", "chapter_sample_a"),
    ("external_reviewer_3", "chapter_sample_b"),
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cell_key(reviewer_id: str, sample_id: str) -> str:
    return f"{reviewer_id}:{sample_id}"


def load_external_blind_review_matrix_evidence(
    evidence: Path | str | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    if isinstance(evidence, dict):
        return evidence
    path = Path(evidence)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def evaluate_external_blind_review_matrix(
    evidence: Path | str | dict[str, Any] | None = None,
    *,
    suite_id: str | None = None,
) -> dict[str, Any]:
    payload = load_external_blind_review_matrix_evidence(evidence)
    observed: dict[str, dict[str, Any]] = {}
    evidence_suite_id: str | None = None
    evidence_id: str | None = None
    evidence_detail = "no external blind-review matrix evidence provided"

    if payload is None:
        if evidence is not None and not isinstance(evidence, dict):
            evidence_detail = (
                f"external blind-review matrix evidence missing or unreadable: {evidence}"
            )
    else:
        schema = payload.get("schema")
        if schema != EVIDENCE_SCHEMA:
            evidence_detail = (
                f"unsupported external blind-review matrix evidence schema: {schema!r}"
            )
        else:
            evidence_suite_id = (
                str(payload.get("suite_id"))
                if payload.get("suite_id") is not None
                else None
            )
            evidence_id = (
                str(payload.get("evidence_id"))
                if payload.get("evidence_id") is not None
                else None
            )
            cells = payload.get("cells")
            if not isinstance(cells, list):
                evidence_detail = "external blind-review matrix evidence cells must be a list"
            else:
                evidence_detail = "external blind-review matrix evidence loaded"
                for cell in cells:
                    if not isinstance(cell, dict):
                        continue
                    reviewer_id = str(cell.get("reviewer_id") or "")
                    sample_id = str(cell.get("sample_id") or "")
                    status = str(cell.get("status") or "missing")
                    if status not in {"passed", "failed", "missing", "skipped"}:
                        status = "missing"
                    key = _cell_key(reviewer_id, sample_id)
                    observed[key] = {
                        "reviewer_id": reviewer_id,
                        "sample_id": sample_id,
                        "status": status,
                        "score": cell.get("score"),
                        "notes": cell.get("notes"),
                        "evidence_ref": cell.get("evidence_ref"),
                    }

    version_ok = True
    if suite_id is not None and evidence_suite_id is not None and suite_id != evidence_suite_id:
        version_ok = False
        evidence_detail = (
            f"suite_id mismatch: expected {suite_id!r}, evidence has {evidence_suite_id!r}"
        )

    cells_out: list[dict[str, Any]] = []
    missing_required: list[str] = []
    passed_count = 0
    for reviewer_id, sample_id in REQUIRED_CELLS:
        key = _cell_key(reviewer_id, sample_id)
        observed_cell = observed.get(key)
        if observed_cell is None:
            status: CellStatus = "missing"
            detail = "required cell missing from evidence"
            missing_required.append(key)
        else:
            status = observed_cell["status"]  # type: ignore[assignment]
            if status == "passed":
                passed_count += 1
                detail = "external blind review cell passed"
            else:
                missing_required.append(key)
                detail = f"required cell status={status}"
        cells_out.append(
            {
                "reviewer_id": reviewer_id,
                "sample_id": sample_id,
                "required": True,
                "status": status,
                "present": observed_cell is not None,
                "detail": detail,
                "score": None if observed_cell is None else observed_cell.get("score"),
                "evidence_ref": (
                    None if observed_cell is None else observed_cell.get("evidence_ref")
                ),
            }
        )

    matrix_ready = (
        version_ok
        and len(missing_required) == 0
        and passed_count == len(REQUIRED_CELLS)
    )
    evaluated_at = _utcnow()
    receipt_body = {
        "schema": RECEIPT_SCHEMA,
        "policy_version": POLICY_VERSION,
        "suite_id": suite_id or evidence_suite_id,
        "evidence_id": evidence_id,
        "matrix_ready": matrix_ready,
        "required_cell_count": len(REQUIRED_CELLS),
        "passed_cell_count": passed_count,
        "missing_required": missing_required,
        "cells": cells_out,
        "quality_assessed": False,
        "formal_draft_allowed": False,
        "llm_literary_score_claim_allowed": False,
        "local_v1_exe_claim_allowed": False,
        "detail": (
            "external blind-review matrix complete; still does not claim quality_assessed"
            if matrix_ready
            else evidence_detail
        ),
        "evaluated_at": evaluated_at.isoformat(),
    }
    receipt_hash = _sha256_text(
        _canonical_json(
            {
                "schema": HASH_SCHEMA,
                **{k: v for k, v in receipt_body.items() if k != "schema"},
            }
        )
    )
    receipt_id = "external-blind-review-matrix-" + receipt_hash[:32]
    receipt = {
        **receipt_body,
        "receipt_id": receipt_id,
        "receipt_hash": receipt_hash,
    }
    not_claiming = [
        "quality_assessed",
        "subjective_literary_score",
        "llm_literary_score",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    ]
    if not matrix_ready:
        not_claiming.append("external_blind_review_matrix")
    return {
        "schema": RESPONSE_SCHEMA,
        "receipt": receipt,
        "not_claiming": not_claiming,
    }


def write_external_blind_review_matrix_receipt(
    response: dict[str, Any],
    destination: Path | str,
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "REQUIRED_CELLS",
    "evaluate_external_blind_review_matrix",
    "load_external_blind_review_matrix_evidence",
    "write_external_blind_review_matrix_receipt",
]
