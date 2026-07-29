"""Portable package resource inventory for NFR-RES input evidence.

This measures on-disk portable bundle size only. It does not measure live
process RSS/CPU or claim release desktop resource gates.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from desktop_packaging.portable import (
    assemble_portable_bundle,
    evaluate_signed_package_readiness,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inventory_portable_bundle(bundle_dir: Path) -> dict[str, Any]:
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"bundle dir missing: {bundle_dir}")
    files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        rel = path.relative_to(bundle_dir).as_posix()
        files.append({"path": rel, "bytes": size})
    readiness = evaluate_signed_package_readiness(bundle_dir)
    generated_at = _utcnow()
    material = {
        "schema": "zhimeng.portable-resource-inventory.v1",
        "bundle_dir": str(bundle_dir),
        "file_count": len(files),
        "total_bytes": total,
        "largest_files": sorted(files, key=lambda item: item["bytes"], reverse=True)[:20],
        "signature_ready": readiness["receipt"]["signature_ready"],
        "local_v1_exe_claim_allowed": False,
        "process_rss_measured": False,
        "cpu_measured": False,
        "formal_draft_allowed": False,
        "policy_version": "local-v1-portable-resource-1",
        "generated_at": generated_at.isoformat(),
    }
    material["receipt_id"] = "portable-resource-" + _sha256_text(
        json.dumps(material, ensure_ascii=False, sort_keys=True)
    )[:32]
    material["receipt_hash"] = _sha256_text(
        json.dumps(material, ensure_ascii=False, sort_keys=True)
    )
    return {
        "schema": "zhimeng.portable-resource-inventory-response.v1",
        "receipt": material,
        "not_claiming": [
            "release_process_rss",
            "release_cpu_budget",
            "local_v1_exe",
            "nfr_release_gate_pass",
        ],
    }


def assemble_and_inventory(
    *,
    output_dir: Path,
    product_version: str,
    workbench_dist: Path,
    runtime_src_root: Path,
    managed_skills_source: Path | None = None,
) -> dict[str, Any]:
    assemble_portable_bundle(
        output_dir=output_dir,
        product_version=product_version,
        workbench_dist=workbench_dist,
        runtime_src_root=runtime_src_root,
        managed_skills_source=managed_skills_source,
    )
    return inventory_portable_bundle(output_dir)


__all__ = ["assemble_and_inventory", "inventory_portable_bundle"]
