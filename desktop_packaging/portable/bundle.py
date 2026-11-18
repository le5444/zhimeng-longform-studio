"""Assemble immutable portable app bundles for Local v1 package-tests.

This is not a signed EXE/MSI builder. It freezes a versioned app payload:
Workbench static dist + Runtime package tree + launch helpers + manifest hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


class BundleError(RuntimeError):
    pass


class BundleStateError(BundleError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise BundleStateError(f"source is not a directory: {source}")
    if destination.exists():
        raise BundleStateError(f"destination already exists: {destination}")
    shutil.copytree(source, destination)


def _require_workbench_dist(workbench_dist: Path) -> None:
    index = workbench_dist / "index.html"
    if not workbench_dist.is_dir() or not index.is_file():
        raise BundleStateError(
            f"workbench dist is invalid (missing index.html): {workbench_dist}"
        )


def _require_runtime_src(runtime_src_root: Path) -> None:
    package_init = runtime_src_root / "zhimeng_runtime" / "__init__.py"
    if not package_init.is_file():
        raise BundleStateError(
            f"runtime package source is invalid (missing zhimeng_runtime/__init__.py): {runtime_src_root}"
        )


def _write_launchers(
    bundle_dir: Path,
    *,
    product_version: str,
    includes_four_skills_runtime: bool,
    includes_skill_contracts: bool = False,
) -> None:
    launch = bundle_dir / "launch"
    launch.mkdir(parents=True, exist_ok=True)
    (launch / "start-runtime.ps1").write_text(
        "\n".join(
            [
                "param(",
                "  [Parameter(Mandatory = $true)][string]$PortableRoot",
                ")",
                "$ErrorActionPreference = 'Stop'",
                f"$ProductVersion = '{product_version}'",
                "$AppDir = Split-Path -Parent $PSScriptRoot",
                "$RuntimeSrc = Join-Path $AppDir 'runtime\\src'",
                "$ContractsRoot = Join-Path $AppDir 'contracts'",
                "if (Test-Path -LiteralPath $ContractsRoot) {",
                "  $env:ZHIMENG_SKILL_CONTRACTS_ROOT = $ContractsRoot",
                "}",
                "$FourSkillsRoot = Join-Path $AppDir 'four-skills-runtime'",
                "if (Test-Path -LiteralPath $FourSkillsRoot) {",
                "  $env:ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT = $FourSkillsRoot",
                "  $TomatoRoot = Join-Path $FourSkillsRoot 'tomato-novel-auto-distill'",
                "  if (Test-Path -LiteralPath $TomatoRoot) {",
                "    $env:ZHIMENG_TOMATO_SKILL_ROOT = $TomatoRoot",
                "  }",
                "}",
                "$EnvFile = Join-Path $PortableRoot 'runtime.env'",
                "if (Test-Path -LiteralPath $EnvFile) {",
                "  Get-Content -LiteralPath $EnvFile | ForEach-Object {",
                "    if ($_ -match '^\\s*#' -or $_ -notmatch '=') { return }",
                "    $name, $value = $_.Split('=', 2)",
                "    Set-Item -Path \"Env:$name\" -Value $value",
                "  }",
                "}",
                "$env:PYTHONPATH = $RuntimeSrc",
                "python -m zhimeng_runtime",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (launch / "open-workbench.ps1").write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$AppDir = Split-Path -Parent $PSScriptRoot",
                "$Index = Join-Path $AppDir 'workbench\\index.html'",
                "if (-not (Test-Path -LiteralPath $Index)) { throw \"workbench index missing: $Index\" }",
                "Start-Process $Index",
                "",
            ]
        ),
        encoding="utf-8",
    )


def assemble_portable_bundle(
    *,
    output_dir: Path,
    product_version: str,
    workbench_dist: Path,
    runtime_src_root: Path,
    managed_skills_source: Path | None = None,
    four_skills_runtime_source: Path | None = None,
    skill_contracts_source: Path | None = None,
    git_commit: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one immutable app bundle directory and return its manifest."""
    if not product_version or any(sep in product_version for sep in ("/", "\\", "..")):
        raise BundleStateError(f"invalid product version: {product_version!r}")
    _require_workbench_dist(workbench_dist)
    _require_runtime_src(runtime_src_root)
    if output_dir.exists():
        raise BundleStateError(f"bundle output already exists: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        _copy_tree(workbench_dist, output_dir / "workbench")
        runtime_dest = output_dir / "runtime" / "src"
        runtime_dest.mkdir(parents=True, exist_ok=False)
        _copy_tree(runtime_src_root / "zhimeng_runtime", runtime_dest / "zhimeng_runtime")
        # Keep package metadata when present next to src layout alternatives.
        for name in ("pyproject.toml", "README.md"):
            candidate = runtime_src_root.parent / name
            if candidate.is_file():
                shutil.copy2(candidate, output_dir / "runtime" / name)
        # Skill contract schemas. The registry's fallback guesses
        # <package>/../../../../packages/contracts, which resolves outside the
        # bundle, so the launcher points ZHIMENG_SKILL_CONTRACTS_ROOT here.
        if skill_contracts_source is not None:
            if not skill_contracts_source.is_dir():
                raise BundleStateError(
                    f"skill contracts source is not a directory: {skill_contracts_source}"
                )
            _copy_tree(skill_contracts_source, output_dir / "contracts")
        if four_skills_runtime_source is not None:
            _copy_tree(four_skills_runtime_source, output_dir / "four-skills-runtime")
        _write_launchers(
            output_dir,
            product_version=product_version,
            includes_four_skills_runtime=four_skills_runtime_source is not None,
            includes_skill_contracts=skill_contracts_source is not None,
        )
        managed_skill_names: list[str] = []
        if managed_skills_source is not None:
            if not managed_skills_source.is_dir():
                raise BundleStateError(
                    f"managed skills source is not a directory: {managed_skills_source}"
                )
            skills_dest = output_dir / "managed-skills"
            skills_dest.mkdir(parents=True, exist_ok=False)
            for child in sorted(managed_skills_source.iterdir()):
                if not child.is_dir():
                    continue
                name = child.name
                if name.startswith(".") or name in {"snapshots", "__pycache__"}:
                    continue
                has_skill = (
                    (child / "SKILL.md").is_file()
                    or (child / "skill.json").is_file()
                    # four-skills-family-* 与其它内置包声明的是 skill.manifest.json。
                    # 漏掉这一种会让它们被静默跳过：包照样构建成功，但运行时
                    # app.py 的 BUILTIN_MANAGED_SKILL_PACKAGES 少了四个 family 壳。
                    or (child / "skill.manifest.json").is_file()
                    or any(child.glob("**/SKILL.md"))
                    or any(child.glob("**/skill.manifest.json"))
                )
                if not has_skill:
                    continue
                _copy_tree(child, skills_dest / name)
                managed_skill_names.append(name)
        (output_dir / "VERSION").write_text(product_version + "\n", encoding="utf-8")

        files: list[dict[str, Any]] = []
        for path in _iter_files(output_dir):
            relative = path.relative_to(output_dir).as_posix()
            files.append(
                {
                    "path": relative,
                    "sha256": _sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        created_at = _now()
        manifest: dict[str, Any] = {
            "schema": "zhimeng.portable-app-bundle-manifest.v1",
            "product_version": product_version,
            "created_at": created_at.isoformat(),
            "git_commit": git_commit,
            "components": {
                "workbench": {
                    "kind": "static_dist",
                    "source": str(workbench_dist),
                    "entry": "workbench/index.html",
                },
                "runtime": {
                    "kind": "python_package_src",
                    "source": str(runtime_src_root),
                    "entry": "runtime/src/zhimeng_runtime",
                    "launcher": "launch/start-runtime.ps1",
                },
                "managed_skills": {
                    "kind": "package_snapshot",
                    "source": None if managed_skills_source is None else str(managed_skills_source),
                    "entry": "managed-skills",
                    "package_names": managed_skill_names,
                },
                "four_skills_runtime": {
                    "kind": "domain_cli_bundle",
                    "source": None if four_skills_runtime_source is None else str(four_skills_runtime_source),
                    "entry": "four-skills-runtime",
                    "portable": four_skills_runtime_source is not None,
                },
            },
            "files": files,
            "file_count": len(files),
            "includes_secrets": False,
            "includes_novel_body": False,
            "includes_project_files": False,
            "formal_draft_allowed": False,
            "not_claiming": [
                "local_v1_exe",
                "signed_msi",
                "code_signing",
                "four_skills_generic_exactly_once",
            ],
            "policy_version": "local-v1-portable-bundle-1",
        }
        if extra_metadata:
            manifest["extra"] = extra_metadata
        material = {
            "schema": "zhimeng.portable-app-bundle-manifest-hash.v1",
            "product_version": product_version,
            "created_at": manifest["created_at"],
            "git_commit": git_commit,
            "files": files,
            "includes_secrets": False,
            "includes_novel_body": False,
            "includes_project_files": False,
            "formal_draft_allowed": False,
            "policy_version": manifest["policy_version"],
        }
        manifest["manifest_hash"] = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        _atomic_write_json(output_dir / "MANIFEST.json", manifest)
        return manifest
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def verify_portable_bundle(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise BundleStateError(f"bundle manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BundleStateError("bundle manifest has no files")
    for item in files:
        relative = str(item["path"])
        path = bundle_dir.joinpath(*relative.split("/"))
        if not path.is_file():
            raise BundleStateError(f"bundle file missing: {relative}")
        actual = _sha256_file(path)
        expected = str(item["sha256"])
        if actual != expected:
            raise BundleStateError(f"bundle file hash mismatch: {relative}")
    if not (bundle_dir / "workbench" / "index.html").is_file():
        raise BundleStateError("bundle workbench entry missing")
    if not (bundle_dir / "runtime" / "src" / "zhimeng_runtime" / "__init__.py").is_file():
        raise BundleStateError("bundle runtime package missing")
    if str(manifest.get("formal_draft_allowed")) not in {"False", "false"} and manifest.get(
        "formal_draft_allowed"
    ) is not False:
        raise BundleStateError("bundle must lock formal_draft_allowed=false")
    return manifest


def default_repo_paths(repo_root: Path) -> dict[str, Path]:
    return {
        "workbench_dist": repo_root / "apps" / "workbench" / "dist",
        "runtime_src_root": repo_root / "services" / "writing-runtime" / "src",
        "runtime_project": repo_root / "services" / "writing-runtime",
        "managed_skills_source": repo_root / "adapters" / "builtin",
        "skill_contracts_source": repo_root / "packages" / "contracts",
        "four_skills_runtime_source": repo_root / "desktop_packaging" / "resources" / "four-skills-runtime",
    }


__all__ = [
    "BundleError",
    "BundleStateError",
    "assemble_portable_bundle",
    "default_repo_paths",
    "verify_portable_bundle",
]
