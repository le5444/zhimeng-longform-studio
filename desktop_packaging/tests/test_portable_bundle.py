"""Tests for portable app bundle assembly and install handoff."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from desktop_packaging.portable import (
    BundleStateError,
    PortableLayout,
    assemble_portable_bundle,
    default_repo_paths,
    install_clean,
    verify_portable_bundle,
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
    (project / "README.md").write_text("runtime\n", encoding="utf-8")
    return src


def _fixture_managed_skills(tmp_path: Path) -> Path:
    root = tmp_path / "managed-skills-src"
    skill = root / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo skill\n", encoding="utf-8")
    return root


def _fixture_four_skills_runtime(tmp_path: Path) -> Path:
    root = tmp_path / "four-skills-runtime-src"
    for relative in (
        "novel-distillation/scripts/distill_source_packet.py",
        "novel-distillation/scripts/distill_kb.py",
        "novel-kb-manager/scripts/kb_manager.py",
        "novel-creation-suite/scripts/detect_ai_smell.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# bundled fixture\n", encoding="utf-8")
    (root / "novel-distillation/library").mkdir(parents=True)
    (root / "novel-kb-manager/library").mkdir(parents=True)
    return root


def test_assemble_verify_and_install_bundle(tmp_path: Path) -> None:
    workbench = _fixture_workbench(tmp_path)
    runtime_src = _fixture_runtime_src(tmp_path)
    managed_skills = _fixture_managed_skills(tmp_path)
    bundle_dir = tmp_path / "bundle-0.1.0"
    manifest = assemble_portable_bundle(
        output_dir=bundle_dir,
        product_version="0.1.0",
        workbench_dist=workbench,
        runtime_src_root=runtime_src,
        managed_skills_source=managed_skills,
        git_commit="deadbeef",
    )
    assert manifest["product_version"] == "0.1.0"
    assert manifest["formal_draft_allowed"] is False
    assert manifest["includes_secrets"] is False
    assert "local_v1_exe" in manifest["not_claiming"]
    assert (bundle_dir / "workbench" / "index.html").is_file()
    assert (bundle_dir / "runtime" / "src" / "zhimeng_runtime" / "__init__.py").is_file()
    assert (bundle_dir / "runtime" / "pyproject.toml").is_file()
    assert (bundle_dir / "launch" / "start-runtime.ps1").is_file()
    assert (bundle_dir / "managed-skills" / "demo-skill" / "SKILL.md").is_file()
    assert "demo-skill" in manifest["components"]["managed_skills"]["package_names"]
    verified = verify_portable_bundle(bundle_dir)
    assert verified["manifest_hash"] == manifest["manifest_hash"]

    layout = PortableLayout(tmp_path / "Zhimeng")
    installed = install_clean(layout, product_version="0.1.0", bundle_source=bundle_dir)
    assert installed["outcome"] == "clean_install"
    app = layout.app_version_dir("0.1.0")
    assert (app / "MANIFEST.json").is_file()
    assert (app / "workbench" / "index.html").is_file()
    assert (app / "runtime" / "src" / "zhimeng_runtime" / "__init__.py").is_file()
    assert (layout.skills_root / "demo-skill" / "SKILL.md").is_file()
    assert (layout.skills_root / ".seeded-from-bundle.txt").read_text(encoding="utf-8").strip() == "demo-skill"


def test_assemble_includes_four_skills_runtime_and_exports_bundle_pointer(
    tmp_path: Path,
) -> None:
    bundle_source = _fixture_four_skills_runtime(tmp_path)
    bundle_dir = tmp_path / "bundle-with-four-skills"
    manifest = assemble_portable_bundle(
        output_dir=bundle_dir,
        product_version="0.1.0-skills",
        workbench_dist=_fixture_workbench(tmp_path),
        runtime_src_root=_fixture_runtime_src(tmp_path),
        four_skills_runtime_source=bundle_source,
    )

    assert manifest["components"]["four_skills_runtime"] == {
        "kind": "domain_cli_bundle",
        "source": str(bundle_source),
        "entry": "four-skills-runtime",
        "portable": True,
    }
    assert (
        bundle_dir
        / "four-skills-runtime"
        / "novel-distillation"
        / "scripts"
        / "distill_kb.py"
    ).is_file()
    launcher = (bundle_dir / "launch" / "start-runtime.ps1").read_text(
        encoding="utf-8"
    )
    assert "ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT" in launcher
    verify_portable_bundle(bundle_dir)


def test_verify_detects_tamper(tmp_path: Path) -> None:
    workbench = _fixture_workbench(tmp_path)
    runtime_src = _fixture_runtime_src(tmp_path)
    bundle_dir = tmp_path / "bundle-0.1.1"
    assemble_portable_bundle(
        output_dir=bundle_dir,
        product_version="0.1.1",
        workbench_dist=workbench,
        runtime_src_root=runtime_src,
    )
    target = bundle_dir / "workbench" / "index.html"
    target.write_text("<html>tampered</html>\n", encoding="utf-8")
    with pytest.raises(BundleStateError):
        verify_portable_bundle(bundle_dir)


def test_assemble_from_repo_workbench_dist_when_present() -> None:
    repo = Path.cwd()
    paths = default_repo_paths(repo)
    if not (paths["workbench_dist"] / "index.html").is_file():
        pytest.skip("apps/workbench/dist not built")
    if not (paths["runtime_src_root"] / "zhimeng_runtime" / "__init__.py").is_file():
        pytest.skip("runtime package source missing")
    out = repo / "desktop_packaging" / ".tmp-bundle-test"
    if out.exists():
        shutil.rmtree(out)
    try:
        manifest = assemble_portable_bundle(
            output_dir=out,
            product_version="0.1.0-dev",
            workbench_dist=paths["workbench_dist"],
            runtime_src_root=paths["runtime_src_root"],
            managed_skills_source=paths.get("managed_skills_source"),
            git_commit=None,
        )
        verify_portable_bundle(out)
        assert manifest["file_count"] >= 3
        assert (out / "workbench" / "index.html").is_file()
        assert (out / "runtime" / "src" / "zhimeng_runtime" / "app.py").is_file()
        assert manifest["components"]["managed_skills"]["package_names"]
    finally:
        shutil.rmtree(out, ignore_errors=True)
