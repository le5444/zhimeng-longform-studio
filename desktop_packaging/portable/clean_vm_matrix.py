"""Clean Windows VM install matrix gate for Local v1 packaging.

Productizes the previously hard-coded clean_vm_install_matrix check.

Fail-closed policy:
- missing/invalid evidence => matrix not ready
- every required OS x scenario cell must be status=passed
- never grants local_v1_exe or formal draft rights
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


POLICY_VERSION = "local-v1-clean-vm-install-matrix-1"
EVIDENCE_SCHEMA = "zhimeng.clean-vm-install-matrix-evidence.v1"
RECEIPT_SCHEMA = "zhimeng.clean-vm-install-matrix-receipt.v1"
RESPONSE_SCHEMA = "zhimeng.clean-vm-install-matrix-response.v1"
HASH_SCHEMA = "zhimeng.clean-vm-install-matrix-receipt-hash.v1"

OsId = Literal["win10", "win11"]
ScenarioId = Literal[
    "clean_install",
    "upgrade",
    "uninstall_preserve",
    "reinstall_restore",
]
CellStatus = Literal["passed", "failed", "missing", "skipped"]

REQUIRED_CELLS: tuple[tuple[OsId, ScenarioId], ...] = (
    ("win10", "clean_install"),
    ("win11", "clean_install"),
    ("win10", "upgrade"),
    ("win11", "upgrade"),
    ("win10", "uninstall_preserve"),
    ("win11", "uninstall_preserve"),
    ("win10", "reinstall_restore"),
    ("win11", "reinstall_restore"),
)


class CleanVmMatrixError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cell_key(os_id: str, scenario: str) -> str:
    return f"{os_id}:{scenario}"


def load_clean_vm_matrix_evidence(
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


def evaluate_clean_vm_install_matrix(
    evidence: Path | str | dict[str, Any] | None = None,
    *,
    product_version: str | None = None,
) -> dict[str, Any]:
    """Evaluate required clean VM install matrix evidence.

    Without complete passed evidence for every required cell, matrix_ready stays
    false and local_v1_exe remains locked.
    """
    payload = load_clean_vm_matrix_evidence(evidence)
    observed_cells: dict[str, dict[str, Any]] = {}
    evidence_product_version: str | None = None
    evidence_id: str | None = None
    evidence_detail = "no clean VM matrix evidence provided"

    if payload is None:
        if evidence is not None and not isinstance(evidence, dict):
            evidence_detail = f"clean VM matrix evidence missing or unreadable: {evidence}"
    else:
        schema = payload.get("schema")
        if schema not in {EVIDENCE_SCHEMA, "zhimeng.clean-vm-install-matrix-evidence.v1"}:
            evidence_detail = f"unsupported clean VM matrix evidence schema: {schema!r}"
        else:
            evidence_product_version = (
                str(payload.get("product_version"))
                if payload.get("product_version") is not None
                else None
            )
            evidence_id = (
                str(payload.get("evidence_id"))
                if payload.get("evidence_id") is not None
                else None
            )
            cells = payload.get("cells")
            if not isinstance(cells, list):
                evidence_detail = "clean VM matrix evidence cells must be a list"
            else:
                evidence_detail = f"loaded {len(cells)} clean VM matrix evidence cell(s)"
                for item in cells:
                    if not isinstance(item, dict):
                        continue
                    os_id = str(item.get("os") or "").strip().lower()
                    scenario = str(item.get("scenario") or "").strip().lower()
                    status = str(item.get("status") or "missing").strip().lower()
                    if os_id not in {"win10", "win11"}:
                        continue
                    if scenario not in {
                        "clean_install",
                        "upgrade",
                        "uninstall_preserve",
                        "reinstall_restore",
                    }:
                        continue
                    if status not in {"passed", "failed", "missing", "skipped"}:
                        status = "missing"
                    key = _cell_key(os_id, scenario)
                    observed_cells[key] = {
                        "os": os_id,
                        "scenario": scenario,
                        "status": status,
                        "evidence_id": item.get("evidence_id"),
                        "detail": str(item.get("detail") or status),
                        "observed_at": item.get("observed_at"),
                    }

    checks: list[dict[str, Any]] = []
    for os_id, scenario in REQUIRED_CELLS:
        key = _cell_key(os_id, scenario)
        observed = observed_cells.get(key)
        if observed is None:
            present = False
            detail = f"required cell {key} missing from clean VM matrix evidence"
            evidence_cell_id = None
            status: CellStatus = "missing"
        else:
            status = observed["status"]  # type: ignore[assignment]
            present = status == "passed"
            detail = str(observed.get("detail") or f"cell {key} status={status}")
            evidence_cell_id = (
                str(observed["evidence_id"])
                if observed.get("evidence_id") is not None
                else None
            )
        checks.append(
            {
                "code": f"cell_{os_id}_{scenario}",
                "required": True,
                "present": present,
                "os": os_id,
                "scenario": scenario,
                "status": status if observed is not None else "missing",
                "detail": detail,
                "evidence_id": evidence_cell_id,
            }
        )

    version_ok = True
    version_detail = "product_version not constrained"
    if product_version:
        if evidence_product_version is None:
            version_ok = False
            version_detail = (
                f"expected product_version={product_version} but evidence has none"
            )
        elif evidence_product_version != product_version:
            version_ok = False
            version_detail = (
                f"evidence product_version={evidence_product_version} "
                f"does not match expected {product_version}"
            )
        else:
            version_detail = f"evidence product_version matches {product_version}"
    checks.append(
        {
            "code": "product_version_aligned",
            "required": bool(product_version),
            "present": version_ok if product_version else True,
            "os": None,
            "scenario": None,
            "status": "passed" if (version_ok if product_version else True) else "failed",
            "detail": version_detail,
            "evidence_id": evidence_id,
        }
    )

    missing_required = [item["code"] for item in checks if item["required"] and not item["present"]]
    matrix_ready = len(missing_required) == 0
    status_value: Literal["ready", "incomplete"] = "ready" if matrix_ready else "incomplete"
    evaluated_at = _utcnow()
    material = {
        "schema": RECEIPT_SCHEMA,
        "status": status_value,
        "matrix_ready": matrix_ready,
        "local_v1_exe_claim_allowed": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "required_cell_count": len(REQUIRED_CELLS),
        "passed_cell_count": sum(
            1
            for item in checks
            if item["code"].startswith("cell_") and item["present"] is True
        ),
        "product_version": product_version or evidence_product_version,
        "evidence_id": evidence_id,
        "evidence_detail": evidence_detail,
        "checks": checks,
        "missing_required": missing_required,
        "policy_version": POLICY_VERSION,
        "evaluated_at": evaluated_at.isoformat(),
    }
    receipt_id = "clean-vm-matrix-" + _sha256_text(_canonical_json(material))[:32]
    material["receipt_id"] = receipt_id
    hash_material = {
        "schema": HASH_SCHEMA,
        "receipt_id": receipt_id,
        "status": material["status"],
        "matrix_ready": material["matrix_ready"],
        "local_v1_exe_claim_allowed": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "required_cell_count": material["required_cell_count"],
        "passed_cell_count": material["passed_cell_count"],
        "product_version": material["product_version"],
        "evidence_id": material["evidence_id"],
        "evidence_detail": material["evidence_detail"],
        "checks": [
            {
                "code": item["code"],
                "required": item["required"],
                "present": item["present"],
                "os": item["os"],
                "scenario": item["scenario"],
                "status": item["status"],
                "detail": item["detail"],
                "evidence_id": item["evidence_id"],
            }
            for item in checks
        ],
        "missing_required": missing_required,
        "policy_version": POLICY_VERSION,
        "evaluated_at": material["evaluated_at"],
    }
    material["receipt_hash"] = _sha256_text(_canonical_json(hash_material))

    not_claiming = [
        "local_v1_exe",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "signed_msi",
        "code_signing",
    ]
    if not matrix_ready:
        not_claiming.append("clean_vm_install_matrix")

    return {
        "schema": RESPONSE_SCHEMA,
        "receipt": material,
        "not_claiming": not_claiming,
    }


def write_clean_vm_install_matrix_receipt(
    response: dict[str, Any],
    output_path: Path | str,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "CleanVmMatrixError",
    "REQUIRED_CELLS",
    "evaluate_clean_vm_install_matrix",
    "load_clean_vm_matrix_evidence",
    "write_clean_vm_install_matrix_receipt",
]
