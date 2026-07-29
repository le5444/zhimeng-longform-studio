"""Tests for signed package readiness gate (AC-E2E-006 / NFR-PKG input)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop_packaging.portable import (
    SignatureInspection,
    SignedPackageGateStateError,
    assemble_portable_bundle,
    assert_local_v1_exe_not_claimed,
    evaluate_signed_package_readiness,
    write_signed_package_readiness_receipt,
)


def _fixture_workbench(tmp_path: Path) -> Path:
    root = tmp_path / "wb-dist"
    root.mkdir()
    (root / "index.html").write_text("<html><body>workbench</body></html>\n", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    return root


def _fixture_runtime_src(tmp_path: Path) -> Path:
    project = tmp_path / "writing-runtime"
    src = project / "src"
    package = src / "zhimeng_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (package / "app.py").write_text("def create_app():\n    return None\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname="zhimeng-writing-runtime"\n', encoding="utf-8")
    return src


def _codes(response: dict) -> dict[str, dict]:
    return {item["code"]: item for item in response["receipt"]["checks"]}


def test_portable_bundle_never_claims_local_v1_exe(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle-0.1.0"
    assemble_portable_bundle(
        output_dir=bundle_dir,
        product_version="0.1.0",
        workbench_dist=_fixture_workbench(tmp_path),
        runtime_src_root=_fixture_runtime_src(tmp_path),
        git_commit="deadbeef",
    )
    response = evaluate_signed_package_readiness(bundle_dir)
    receipt = response["receipt"]
    assert receipt["package_kind"] == "portable_bundle"
    assert receipt["status"] == "incomplete"
    assert receipt["signature_ready"] is False
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert receipt["formal_draft_allowed"] is False
    assert "local_v1_exe" in response["not_claiming"]
    assert "signed_msi" in response["not_claiming"]
    codes = _codes(response)
    assert codes["authenticode_signature_present"]["present"] is False
    assert codes["authenticode_signature_valid"]["present"] is False
    assert codes["version_identity_present"]["present"] is True
    assert codes["checksum_manifest_present"]["present"] is True
    assert "authenticode_signature_present" in receipt["missing_required"]
    assert "clean_vm_install_matrix" in receipt["missing_required"]
    assert_local_v1_exe_not_claimed(response)

    out = tmp_path / "receipts" / "portable-signed-package-readiness.json"
    written = write_signed_package_readiness_receipt(response, out)
    saved = json.loads(written.read_text(encoding="utf-8"))
    assert saved["receipt"]["receipt_hash"] == receipt["receipt_hash"]
    assert "local_v1_exe" in saved["not_claiming"]


def test_unsigned_exe_is_signature_not_ready(tmp_path: Path) -> None:
    package = tmp_path / "Zhimeng-Setup-0.1.0.exe"
    package.write_bytes(b"MZ-unsigned-fixture")
    (tmp_path / "Zhimeng-Setup-0.1.0.exe.version.json").write_text(
        json.dumps({"product_version": "0.1.0"}),
        encoding="utf-8",
    )
    (tmp_path / "Zhimeng-Setup-0.1.0.exe.sha256").write_text(
        "a" * 64 + "  Zhimeng-Setup-0.1.0.exe\n",
        encoding="utf-8",
    )

    def inspector(_path: Path) -> SignatureInspection:
        return SignatureInspection(
            present=False,
            valid=False,
            status="NotSigned",
            detail="fixture unsigned",
        )

    response = evaluate_signed_package_readiness(
        package,
        product_version="0.1.0",
        signature_inspector=inspector,
    )
    receipt = response["receipt"]
    assert receipt["package_kind"] == "exe"
    assert receipt["signature_ready"] is False
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert "local_v1_exe" in response["not_claiming"]
    assert "code_signing" in response["not_claiming"]
    codes = _codes(response)
    assert codes["authenticode_signature_present"]["present"] is False
    assert codes["version_identity_present"]["present"] is True
    assert codes["checksum_manifest_present"]["present"] is True
    assert "authenticode_signature_valid" in receipt["missing_required"]


def test_valid_signed_msi_is_signature_ready_but_still_not_local_v1_exe(
    tmp_path: Path,
) -> None:
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

    response = evaluate_signed_package_readiness(
        package,
        product_version="0.2.0",
        signature_inspector=inspector,
        require_clean_vm_matrix=True,
    )
    receipt = response["receipt"]
    assert receipt["package_kind"] == "msi"
    assert receipt["signature_ready"] is True
    assert receipt["status"] == "incomplete"
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert receipt["formal_draft_allowed"] is False
    assert "local_v1_exe" in response["not_claiming"]
    assert "clean_vm_install_matrix" in response["not_claiming"]
    assert receipt["missing_required"] == ["clean_vm_install_matrix"]
    codes = _codes(response)
    assert codes["authenticode_signature_valid"]["present"] is True
    assert codes["msi_digital_signature_present"]["present"] is True
    assert codes["clean_vm_install_matrix"]["present"] is False
    assert_local_v1_exe_not_claimed(response)


def test_assert_rejects_forged_local_v1_exe_claim() -> None:
    forged = {
        "schema": "zhimeng.signed-package-readiness-response.v1",
        "receipt": {
            "local_v1_exe_claim_allowed": True,
            "formal_draft_allowed": False,
        },
        "not_claiming": [],
    }
    with pytest.raises(SignedPackageGateStateError):
        assert_local_v1_exe_not_claimed(forged)


def test_missing_package_is_incomplete(tmp_path: Path) -> None:
    response = evaluate_signed_package_readiness(tmp_path / "missing-setup.exe")
    receipt = response["receipt"]
    assert receipt["status"] == "incomplete"
    assert receipt["signature_ready"] is False
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert "package_artifact_present" in receipt["missing_required"]
    assert "local_v1_exe" in response["not_claiming"]

def test_build_bundle_cli_emits_signed_package_readiness(tmp_path: Path) -> None:
    from desktop_packaging.portable.build_bundle import main

    workbench = _fixture_workbench(tmp_path)
    runtime_src = _fixture_runtime_src(tmp_path)
    output = tmp_path / "bundle-cli"
    code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--product-version",
            "0.1.0-cli",
            "--output-dir",
            str(output),
            "--workbench-dist",
            str(workbench),
            "--runtime-src-root",
            str(runtime_src),
            "--skip-managed-skills",
        ]
    )
    assert code == 0
    readiness_path = tmp_path / "bundle-cli.signed-package-readiness.json"
    assert readiness_path.is_file()
    payload = json.loads(readiness_path.read_text(encoding="utf-8"))
    assert payload["receipt"]["signature_ready"] is False
    assert payload["receipt"]["local_v1_exe_claim_allowed"] is False
    assert "local_v1_exe" in payload["not_claiming"]

