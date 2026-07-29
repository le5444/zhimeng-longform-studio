from __future__ import annotations

import json
from pathlib import Path

from desktop_packaging.portable.clean_vm_matrix import (
    REQUIRED_CELLS,
    evaluate_clean_vm_install_matrix,
    write_clean_vm_install_matrix_receipt,
)
from desktop_packaging.portable.signed_package_gate import (
    SignatureInspection,
    assert_local_v1_exe_not_claimed,
    evaluate_signed_package_readiness,
)


def _full_passed_evidence(product_version: str = "0.2.0") -> dict:
    return {
        "schema": "zhimeng.clean-vm-install-matrix-evidence.v1",
        "evidence_id": "vm-matrix-fixture-1",
        "product_version": product_version,
        "cells": [
            {
                "os": os_id,
                "scenario": scenario,
                "status": "passed",
                "evidence_id": f"{os_id}-{scenario}",
                "detail": f"{os_id} {scenario} passed on fixture VM",
                "observed_at": "2026-07-26T00:00:00+00:00",
            }
            for os_id, scenario in REQUIRED_CELLS
        ],
    }


def test_clean_vm_matrix_incomplete_without_evidence() -> None:
    response = evaluate_clean_vm_install_matrix()
    receipt = response["receipt"]
    assert receipt["status"] == "incomplete"
    assert receipt["matrix_ready"] is False
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert receipt["formal_draft_allowed"] is False
    assert receipt["required_cell_count"] == 8
    assert receipt["passed_cell_count"] == 0
    assert len(receipt["missing_required"]) >= 8
    assert "clean_vm_install_matrix" in response["not_claiming"]
    assert "local_v1_exe" in response["not_claiming"]


def test_clean_vm_matrix_ready_with_full_passed_evidence(tmp_path: Path) -> None:
    evidence = _full_passed_evidence()
    response = evaluate_clean_vm_install_matrix(evidence, product_version="0.2.0")
    receipt = response["receipt"]
    assert receipt["status"] == "ready"
    assert receipt["matrix_ready"] is True
    assert receipt["passed_cell_count"] == 8
    assert receipt["missing_required"] == []
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert "clean_vm_install_matrix" not in response["not_claiming"]
    out = write_clean_vm_install_matrix_receipt(response, tmp_path / "matrix.json")
    assert out.is_file()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["receipt"]["matrix_ready"] is True


def test_clean_vm_matrix_rejects_version_mismatch() -> None:
    evidence = _full_passed_evidence(product_version="0.1.0")
    response = evaluate_clean_vm_install_matrix(evidence, product_version="0.2.0")
    receipt = response["receipt"]
    assert receipt["matrix_ready"] is False
    assert "product_version_aligned" in receipt["missing_required"]


def test_signed_package_gate_accepts_clean_vm_matrix_evidence(tmp_path: Path) -> None:
    package = tmp_path / "Zhimeng-0.2.0.msi"
    package.write_bytes(b"MSI-signed-fixture")
    (tmp_path / "product-version.json").write_text(
        json.dumps({"product_version": "0.2.0"}),
        encoding="utf-8",
    )
    (tmp_path / "checksums.sha256").write_text(
        "b" * 64 + "  Zhimeng-0.2.0.msi\n",
        encoding="utf-8",
    )

    def inspector(_path: Path) -> SignatureInspection:
        return SignatureInspection(
            present=True,
            valid=True,
            status="Valid",
            signer_subject="CN=Zhimeng Test",
            thumbprint="C" * 40,
            detail="fixture valid signature",
        )

    incomplete = evaluate_signed_package_readiness(
        package,
        product_version="0.2.0",
        signature_inspector=inspector,
        require_clean_vm_matrix=True,
    )
    assert incomplete["receipt"]["signature_ready"] is True
    assert incomplete["receipt"]["status"] == "incomplete"
    assert incomplete["receipt"]["missing_required"] == ["clean_vm_install_matrix"]
    assert incomplete["receipt"]["local_v1_exe_claim_allowed"] is False
    assert "clean_vm_install_matrix" in incomplete["not_claiming"]

    complete = evaluate_signed_package_readiness(
        package,
        product_version="0.2.0",
        signature_inspector=inspector,
        require_clean_vm_matrix=True,
        clean_vm_matrix_evidence=_full_passed_evidence("0.2.0"),
    )
    assert complete["receipt"]["signature_ready"] is True
    assert complete["receipt"]["status"] == "complete"
    assert complete["receipt"]["missing_required"] == []
    assert complete["receipt"]["local_v1_exe_claim_allowed"] is False
    assert complete["clean_vm_matrix"]["receipt"]["matrix_ready"] is True
    assert "clean_vm_install_matrix" not in complete["not_claiming"]
    assert_local_v1_exe_not_claimed(complete)
