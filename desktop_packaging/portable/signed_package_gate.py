"""Signed package readiness gate for Local v1 packaging claims.

This evaluates whether a candidate Windows package (EXE/MSI) or portable bundle
carries Authenticode / MSI signature evidence. It is intentionally fail-closed:

- unsigned portable bundles never become ``local_v1_exe``
- even a signature-ready EXE/MSI still cannot claim ``local_v1_exe`` until the
  remaining clean-VM install matrix and release packaging gates exist

The gate is an authorization *input* for future release claims, not a release.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from desktop_packaging.portable.clean_vm_matrix import (
    evaluate_clean_vm_install_matrix,
)


POLICY_VERSION = "local-v1-signed-package-gate-1"
RECEIPT_SCHEMA = "zhimeng.signed-package-readiness-receipt.v1"
RESPONSE_SCHEMA = "zhimeng.signed-package-readiness-response.v1"
HASH_SCHEMA = "zhimeng.signed-package-readiness-receipt-hash.v1"

PackageKind = Literal["exe", "msi", "portable_bundle", "unknown"]
GateStatus = Literal["complete", "incomplete"]


class SignedPackageGateError(RuntimeError):
    pass


class SignedPackageGateStateError(SignedPackageGateError):
    pass


@dataclass(frozen=True)
class SignatureInspection:
    """Normalized Authenticode / MSI signature observation for one artifact."""

    present: bool
    valid: bool
    status: str
    signer_subject: str | None = None
    thumbprint: str | None = None
    detail: str | None = None


SignatureInspector = Callable[[Path], SignatureInspection]


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def detect_package_kind(path: Path) -> PackageKind:
    if path.is_dir():
        if (path / "MANIFEST.json").is_file():
            return "portable_bundle"
        return "unknown"
    if not path.is_file():
        return "unknown"
    suffix = path.suffix.lower()
    if suffix == ".exe":
        return "exe"
    if suffix == ".msi":
        return "msi"
    return "unknown"


def default_authenticode_inspector(path: Path) -> SignatureInspection:
    """Inspect Authenticode via PowerShell Get-AuthenticodeSignature.

    Used for both EXE and MSI. Portable directories never reach this helper.
    """
    if not path.is_file():
        return SignatureInspection(
            present=False,
            valid=False,
            status="Missing",
            detail=f"artifact missing: {path}",
        )
    command = (
        "$sig = Get-AuthenticodeSignature -LiteralPath $env:ZHIMENG_SIGN_TARGET; "
        "[pscustomobject]@{"
        "Status=[string]$sig.Status; "
        "StatusMessage=[string]$sig.StatusMessage; "
        "Subject= if ($sig.SignerCertificate) { [string]$sig.SignerCertificate.Subject } else { '' }; "
        "Thumbprint= if ($sig.SignerCertificate) { [string]$sig.SignerCertificate.Thumbprint } else { '' }"
        "} | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={
                **os.environ,
                "ZHIMENG_SIGN_TARGET": str(path),
            },
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SignatureInspection(
            present=False,
            valid=False,
            status="InspectorError",
            detail=f"authenticode inspector failed: {exc}",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "authenticode inspector failed").strip()
        return SignatureInspection(
            present=False,
            valid=False,
            status="InspectorError",
            detail=detail[:500],
        )
    raw = (completed.stdout or "").strip()
    if not raw:
        return SignatureInspection(
            present=False,
            valid=False,
            status="InspectorError",
            detail="authenticode inspector returned empty output",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return SignatureInspection(
            present=False,
            valid=False,
            status="InspectorError",
            detail=f"authenticode inspector returned non-json: {raw[:200]}",
        )
    status = str(payload.get("Status") or "Unknown")
    subject = str(payload.get("Subject") or "").strip() or None
    thumbprint = str(payload.get("Thumbprint") or "").strip() or None
    message = str(payload.get("StatusMessage") or "").strip() or None
    # Only Status == Valid counts as a release-grade signature observation.
    present = status not in {"NotSigned", "UnknownError", "InspectorError", "Missing"}
    valid = status == "Valid"
    if status == "NotSigned":
        present = False
        valid = False
    return SignatureInspection(
        present=present,
        valid=valid,
        status=status,
        signer_subject=subject,
        thumbprint=thumbprint,
        detail=message,
    )


def _version_identity(path: Path, kind: PackageKind) -> tuple[bool, str | None, str]:
    if kind == "portable_bundle":
        manifest_path = path / "MANIFEST.json"
        if not manifest_path.is_file():
            return False, None, "portable MANIFEST.json missing"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, None, f"portable MANIFEST.json unreadable: {exc}"
        version = str(manifest.get("product_version") or "").strip() or None
        if version:
            return True, version, "portable product_version present"
        return False, None, "portable product_version missing"
    if kind in {"exe", "msi"}:
        # Sidecar version identity is accepted before PE/MSI property extraction lands.
        candidates = [
            path.with_name(f"{path.name}.version.json"),
            path.parent / "VERSION.json",
            path.parent / "product-version.json",
        ]
        for sidecar in candidates:
            if not sidecar.is_file():
                continue
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            version = str(
                payload.get("product_version")
                or payload.get("version")
                or payload.get("ProductVersion")
                or ""
            ).strip()
            if version:
                return True, version, f"sidecar version present via {sidecar.name}"
        # Filename fallback is observation only and does not satisfy the required check.
        match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?)", path.name)
        if match:
            return False, match.group(1), "filename version is not authoritative identity"
        return False, None, "no authoritative package version identity"
    return False, None, "package kind does not expose version identity"


def _checksum_present(path: Path, kind: PackageKind) -> tuple[bool, str]:
    if kind == "portable_bundle":
        manifest_path = path / "MANIFEST.json"
        if not manifest_path.is_file():
            return False, "portable MANIFEST.json missing"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"portable MANIFEST.json unreadable: {exc}"
        files = manifest.get("files")
        digest = manifest.get("manifest_hash")
        if isinstance(files, list) and files and isinstance(digest, str) and len(digest) == 64:
            return True, "portable file hashes and manifest_hash present"
        return False, "portable checksum/manifest_hash incomplete"
    if kind in {"exe", "msi"}:
        candidates = [
            path.with_name(f"{path.name}.sha256"),
            path.with_name(f"{path.name}.sha256.txt"),
            path.parent / "SHA256SUMS",
            path.parent / "checksums.sha256",
        ]
        for sidecar in candidates:
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                return True, f"checksum sidecar present via {sidecar.name}"
        return False, "checksum sidecar missing for install package"
    return False, "package kind does not expose checksum evidence"


def _check(
    *,
    code: str,
    required: bool,
    present: bool,
    detail: str,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "required": required,
        "present": present,
        "detail": detail,
        "evidence_id": evidence_id,
    }


def evaluate_signed_package_readiness(
    package_path: Path | str,
    *,
    product_version: str | None = None,
    signature_inspector: SignatureInspector | None = None,
    require_clean_vm_matrix: bool = True,
    clean_vm_matrix_evidence: Path | str | dict | None = None,
) -> dict[str, Any]:
    """Evaluate signature readiness for a package candidate.

    Returns a response envelope with a hashed receipt. ``local_v1_exe_claim_allowed``
    stays false until signature readiness and the remaining release gates pass.
    Clean VM matrix evidence can mark the matrix check present, but never unlocks
    local_v1_exe by itself.
    """
    path = Path(package_path)
    inspector = signature_inspector or default_authenticode_inspector
    checks: list[dict[str, Any]] = []

    package_present = path.exists()
    checks.append(
        _check(
            code="package_artifact_present",
            required=True,
            present=package_present,
            detail="package path exists" if package_present else f"package path missing: {path}",
        )
    )

    kind = detect_package_kind(path) if package_present else "unknown"
    kind_supported = kind in {"exe", "msi", "portable_bundle"}
    checks.append(
        _check(
            code="package_kind_recognized",
            required=True,
            present=kind_supported,
            detail=f"detected package kind: {kind}",
        )
    )

    signature: SignatureInspection | None = None
    if kind in {"exe", "msi"} and package_present:
        signature = inspector(path)
        checks.append(
            _check(
                code="authenticode_signature_present",
                required=True,
                present=signature.present,
                detail=(
                    f"Authenticode status={signature.status}"
                    + (f"; {signature.detail}" if signature.detail else "")
                ),
                evidence_id=signature.thumbprint,
            )
        )
        checks.append(
            _check(
                code="authenticode_signature_valid",
                required=True,
                present=signature.valid,
                detail=(
                    "Authenticode Status=Valid"
                    if signature.valid
                    else f"Authenticode not valid (status={signature.status})"
                ),
                evidence_id=signature.thumbprint,
            )
        )
        if kind == "msi":
            checks.append(
                _check(
                    code="msi_digital_signature_present",
                    required=True,
                    present=signature.present and signature.valid,
                    detail=(
                        "MSI digital signature observed via Authenticode Valid"
                        if signature.present and signature.valid
                        else "MSI digital signature missing or not Valid"
                    ),
                    evidence_id=signature.thumbprint,
                )
            )
    elif kind == "portable_bundle":
        checks.append(
            _check(
                code="authenticode_signature_present",
                required=True,
                present=False,
                detail="portable bundle is not a signed Windows EXE/MSI install package",
            )
        )
        checks.append(
            _check(
                code="authenticode_signature_valid",
                required=True,
                present=False,
                detail="portable bundle cannot satisfy Authenticode validity",
            )
        )
        checks.append(
            _check(
                code="msi_digital_signature_present",
                required=False,
                present=False,
                detail="portable bundle is not an MSI",
            )
        )
    else:
        checks.append(
            _check(
                code="authenticode_signature_present",
                required=True,
                present=False,
                detail="no Authenticode surface for unrecognized package kind",
            )
        )
        checks.append(
            _check(
                code="authenticode_signature_valid",
                required=True,
                present=False,
                detail="no Authenticode validity for unrecognized package kind",
            )
        )

    if package_present and kind_supported:
        version_present, detected_version, version_detail = _version_identity(path, kind)
    else:
        version_present, detected_version, version_detail = False, None, "package unavailable"
    if product_version:
        if detected_version and detected_version != product_version:
            version_present = False
            version_detail = (
                f"declared product_version={product_version} mismatches detected={detected_version}"
            )
        elif not detected_version:
            version_detail = (
                f"declared product_version={product_version}; on-disk version identity still missing"
            )
    checks.append(
        _check(
            code="version_identity_present",
            required=True,
            present=version_present,
            detail=version_detail,
            evidence_id=detected_version,
        )
    )

    if package_present and kind_supported:
        checksum_present, checksum_detail = _checksum_present(path, kind)
    else:
        checksum_present, checksum_detail = False, "package unavailable"
    checks.append(
        _check(
            code="checksum_manifest_present",
            required=True,
            present=checksum_present,
            detail=checksum_detail,
        )
    )

    # Clean VM matrix remains a required release gate for local_v1_exe claims.
    matrix_response = evaluate_clean_vm_install_matrix(
        clean_vm_matrix_evidence,
        product_version=product_version,
    )
    matrix_receipt = matrix_response.get("receipt") or {}
    matrix_ready = bool(matrix_receipt.get("matrix_ready"))
    if require_clean_vm_matrix:
        matrix_detail = str(
            matrix_receipt.get("evidence_detail")
            or (
                "clean Windows VM install matrix ready"
                if matrix_ready
                else "clean Windows VM install matrix incomplete"
            )
        )
        if matrix_receipt.get("missing_required"):
            matrix_detail = (
                f"{matrix_detail}; missing="
                + ",".join(str(item) for item in matrix_receipt.get("missing_required") or [])
            )
    else:
        matrix_ready = True
        matrix_detail = "clean VM matrix check waived for isolated signature inspection"
    checks.append(
        _check(
            code="clean_vm_install_matrix",
            required=require_clean_vm_matrix,
            present=matrix_ready if require_clean_vm_matrix else True,
            detail=matrix_detail,
            evidence_id=(
                None
                if not matrix_receipt.get("receipt_id")
                else str(matrix_receipt.get("receipt_id"))
            ),
        )
    )

    missing_required = [item["code"] for item in checks if item["required"] and not item["present"]]
    # Signature readiness ignores the deferred clean-VM matrix gate.
    signature_blocking = {
        "package_artifact_present",
        "package_kind_recognized",
        "authenticode_signature_present",
        "authenticode_signature_valid",
        "msi_digital_signature_present",
        "version_identity_present",
        "checksum_manifest_present",
    }
    signature_missing = [code for code in missing_required if code in signature_blocking]
    signature_ready = len(signature_missing) == 0 and kind in {"exe", "msi"}
    # Fail-closed product claim: signature readiness is necessary but not sufficient.
    local_v1_exe_claim_allowed = False
    status: GateStatus = "complete" if not missing_required else "incomplete"

    created_at = _now()
    material = {
        "schema": RECEIPT_SCHEMA,
        "package_path": str(path),
        "package_kind": kind,
        "product_version": product_version or detected_version,
        "status": status,
        "signature_ready": signature_ready,
        "local_v1_exe_claim_allowed": local_v1_exe_claim_allowed,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "checks": checks,
        "missing_required": missing_required,
        "signature_status": None if signature is None else signature.status,
        "signer_subject": None if signature is None else signature.signer_subject,
        "signer_thumbprint": None if signature is None else signature.thumbprint,
        "policy_version": POLICY_VERSION,
        "evaluated_at": created_at.isoformat(),
    }
    receipt_id = "signed-package-" + _sha256_text(_canonical_json(material))[:32]
    material["receipt_id"] = receipt_id
    hash_material = {
        "schema": HASH_SCHEMA,
        "receipt_id": receipt_id,
        "package_path": material["package_path"],
        "package_kind": material["package_kind"],
        "product_version": material["product_version"],
        "status": material["status"],
        "signature_ready": material["signature_ready"],
        "local_v1_exe_claim_allowed": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "checks": [
            {
                "code": item["code"],
                "required": item["required"],
                "present": item["present"],
                "detail": item["detail"],
                "evidence_id": item["evidence_id"],
            }
            for item in checks
        ],
        "missing_required": missing_required,
        "signature_status": material["signature_status"],
        "signer_subject": material["signer_subject"],
        "signer_thumbprint": material["signer_thumbprint"],
        "policy_version": POLICY_VERSION,
        "evaluated_at": material["evaluated_at"],
    }
    material["receipt_hash"] = _sha256_text(_canonical_json(hash_material))

    not_claiming = [
        "local_v1_exe",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
    ]
    if require_clean_vm_matrix and not matrix_ready:
        not_claiming.append("clean_vm_install_matrix")
    if not signature_ready:
        not_claiming.extend(
            [
                "signed_msi",
                "code_signing",
                "authenticode_release_package",
            ]
        )

    return {
        "schema": RESPONSE_SCHEMA,
        "receipt": material,
        "clean_vm_matrix": matrix_response,
        "not_claiming": not_claiming,
    }


def assert_local_v1_exe_not_claimed(response: dict[str, Any]) -> None:
    """Fail closed if any envelope tries to claim local_v1_exe from this gate."""
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        raise SignedPackageGateStateError("signed package readiness response missing receipt")
    if receipt.get("local_v1_exe_claim_allowed") is not False:
        raise SignedPackageGateStateError("local_v1_exe_claim_allowed must remain false")
    if receipt.get("formal_draft_allowed") is not False:
        raise SignedPackageGateStateError("formal_draft_allowed must remain false")
    not_claiming = response.get("not_claiming")
    if not isinstance(not_claiming, list) or "local_v1_exe" not in not_claiming:
        raise SignedPackageGateStateError("response must list local_v1_exe under not_claiming")


def write_signed_package_readiness_receipt(
    response: dict[str, Any],
    output_path: Path | str,
) -> Path:
    """Persist the readiness response as a reviewable artifact."""
    assert_local_v1_exe_not_claimed(response)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "POLICY_VERSION",
    "SignatureInspection",
    "SignedPackageGateError",
    "SignedPackageGateStateError",
    "assert_local_v1_exe_not_claimed",
    "default_authenticode_inspector",
    "detect_package_kind",
    "evaluate_signed_package_readiness",
    "write_signed_package_readiness_receipt",
]
