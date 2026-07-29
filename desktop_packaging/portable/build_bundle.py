"""CLI: assemble a portable app bundle from Workbench dist + Runtime package src."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from desktop_packaging.portable.bundle import (
    assemble_portable_bundle,
    default_repo_paths,
    verify_portable_bundle,
)
from desktop_packaging.portable.signed_package_gate import (
    evaluate_signed_package_readiness,
    write_signed_package_readiness_receipt,
)


def _git_commit(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--product-version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workbench-dist", type=Path, default=None)
    parser.add_argument("--runtime-src-root", type=Path, default=None)
    parser.add_argument("--managed-skills-source", type=Path, default=None)
    parser.add_argument("--skip-managed-skills", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    defaults = default_repo_paths(repo_root)
    workbench_dist = (args.workbench_dist or defaults["workbench_dist"]).resolve()
    runtime_src_root = (args.runtime_src_root or defaults["runtime_src_root"]).resolve()
    if args.skip_managed_skills:
        managed_skills_source = None
    else:
        managed_skills_source = args.managed_skills_source or defaults.get("managed_skills_source")
        managed_skills_source = (
            None if managed_skills_source is None else Path(managed_skills_source).resolve()
        )
    output_dir = args.output_dir.resolve()

    if args.verify_only:
        manifest = verify_portable_bundle(output_dir)
    else:
        manifest = assemble_portable_bundle(
            output_dir=output_dir,
            product_version=args.product_version,
            workbench_dist=workbench_dist,
            runtime_src_root=runtime_src_root,
            managed_skills_source=managed_skills_source,
            git_commit=_git_commit(repo_root),
        )
        verify_portable_bundle(output_dir)

    readiness = evaluate_signed_package_readiness(
        output_dir,
        product_version=str(manifest.get("product_version") or args.product_version),
    )
    readiness_path = write_signed_package_readiness_receipt(
        readiness,
        output_dir.parent / f"{output_dir.name}.signed-package-readiness.json",
    )
    receipt = readiness["receipt"]
    print(
        json.dumps(
            {
                "product_version": manifest["product_version"],
                "file_count": manifest["file_count"],
                "manifest_hash": manifest["manifest_hash"],
                "output_dir": str(output_dir),
                "not_claiming": manifest["not_claiming"],
                "signed_package_readiness": {
                    "path": str(readiness_path),
                    "status": receipt["status"],
                    "signature_ready": receipt["signature_ready"],
                    "local_v1_exe_claim_allowed": receipt["local_v1_exe_claim_allowed"],
                    "missing_required": receipt["missing_required"],
                    "not_claiming": readiness["not_claiming"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
