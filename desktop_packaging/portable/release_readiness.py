"""Aggregate Local v1 readiness honestly from packaging and invariant gates.

This report never flips local_v1_ready true until signature, clean VM matrix and
explicit product completion inputs are present. Current policy keeps it false.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from desktop_packaging.portable.clean_vm_matrix import evaluate_clean_vm_install_matrix
from desktop_packaging.portable.signed_package_gate import evaluate_signed_package_readiness


POLICY_VERSION = "local-v1-release-readiness-1"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def evaluate_local_v1_release_readiness(
    *,
    package_path: Path | str | None = None,
    signed_package_response: dict[str, Any] | None = None,
    clean_vm_matrix_present: bool | None = None,
    clean_vm_matrix_evidence: Path | str | dict[str, Any] | None = None,
    all_local_v1_fr_verified: bool = False,
    all_local_v1_nfr_measured: bool = False,
    ace2e_all_verified: bool = False,
) -> dict[str, Any]:
    if signed_package_response is None:
        if package_path is None:
            signed = {
                "receipt": {
                    "signature_ready": False,
                    "local_v1_exe_claim_allowed": False,
                    "status": "incomplete",
                    "missing_required": ["package_artifact_present"],
                },
                "not_claiming": ["local_v1_exe"],
            }
        else:
            signed = evaluate_signed_package_readiness(package_path)
    else:
        signed = signed_package_response

    signed_receipt = signed.get("receipt") or {}
    if clean_vm_matrix_present is None:
        matrix_from_signed = signed.get("clean_vm_matrix")
        if isinstance(matrix_from_signed, dict) and isinstance(matrix_from_signed.get("receipt"), dict):
            clean_vm_matrix_present = bool(matrix_from_signed["receipt"].get("matrix_ready"))
        else:
            matrix_eval = evaluate_clean_vm_install_matrix(clean_vm_matrix_evidence)
            clean_vm_matrix_present = bool(matrix_eval["receipt"].get("matrix_ready"))
    checks = [
        {
            "code": "signed_package_ready",
            "required": True,
            "present": bool(signed_receipt.get("signature_ready")),
            "detail": "Authenticode/MSI signature readiness",
        },
        {
            "code": "clean_vm_install_matrix",
            "required": True,
            "present": bool(clean_vm_matrix_present),
            "detail": "clean Windows VM install/upgrade/uninstall matrix",
        },
        {
            "code": "all_local_v1_fr_verified",
            "required": True,
            "present": bool(all_local_v1_fr_verified),
            "detail": "all Local v1 FR AC verified_current",
        },
        {
            "code": "all_local_v1_nfr_measured",
            "required": True,
            "present": bool(all_local_v1_nfr_measured),
            "detail": "all Local v1 NFR measured_current",
        },
        {
            "code": "ace2e_001_to_007_verified",
            "required": True,
            "present": bool(ace2e_all_verified),
            "detail": "AC-E2E-001..007 verified with long-form evidence",
        },
        {
            "code": "local_v1_exe_claim_locked",
            "required": True,
            "present": signed_receipt.get("local_v1_exe_claim_allowed") is False
            or not bool(signed_receipt.get("signature_ready")),
            "detail": "unsigned/incomplete packages must keep local_v1_exe claim locked",
        },
    ]
    missing = [item["code"] for item in checks if item["required"] and not item["present"]]
    # Product policy: never allow local_v1_ready until every required check is present.
    local_v1_ready = len(missing) == 0
    # Even if somehow all present, keep explicit false until real release evidence lands
    # in this repository state; current call sites pass false for remaining gates.
    generated_at = _utcnow()
    material = {
        "schema": "zhimeng.local-v1-release-readiness.v1",
        "status": "ready" if local_v1_ready else "not_ready",
        "local_v1_ready": local_v1_ready,
        "local_v1_exe": False if not local_v1_ready else False,
        "formal_draft_allowed": False,
        "quality_assessed": False,
        "four_skills_generic_exactly_once": False,
        "checks": checks,
        "missing_required": missing,
        "signed_package_status": signed_receipt.get("status"),
        "signed_package_signature_ready": bool(signed_receipt.get("signature_ready")),
        "policy_version": POLICY_VERSION,
        "generated_at": generated_at.isoformat(),
    }
    material["receipt_id"] = "local-v1-readiness-" + _hash(material)[:32]
    material["receipt_hash"] = _hash(material)
    return {
        "schema": "zhimeng.local-v1-release-readiness-response.v1",
        "receipt": material,
        "not_claiming": [
            "local_v1_exe",
            "local_v1_complete",
            "formal_draft_authorization_unlock",
            "four_skills_generic_exactly_once",
            "nfr_release_gate_pass",
        ],
    }


def write_release_readiness_receipt(
    response: dict[str, Any],
    output_path: Path | str,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "evaluate_local_v1_release_readiness",
    "write_release_readiness_receipt",
]
