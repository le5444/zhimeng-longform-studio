from __future__ import annotations

from pathlib import Path

from desktop_packaging.portable.release_readiness import (
    evaluate_local_v1_release_readiness,
    write_release_readiness_receipt,
)
from desktop_packaging.portable import assemble_portable_bundle


def _fixture_workbench(tmp_path: Path) -> Path:
    root = tmp_path / "wb-dist"
    root.mkdir()
    (root / "index.html").write_text("<html><body>workbench</body></html>\n", encoding="utf-8")
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


def test_local_v1_release_readiness_fail_closed(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    assemble_portable_bundle(
        output_dir=bundle,
        product_version="0.1.0",
        workbench_dist=_fixture_workbench(tmp_path),
        runtime_src_root=_fixture_runtime_src(tmp_path),
    )
    response = evaluate_local_v1_release_readiness(package_path=bundle)
    receipt = response["receipt"]
    assert receipt["local_v1_ready"] is False
    assert receipt["local_v1_exe"] is False
    assert receipt["formal_draft_allowed"] is False
    assert "signed_package_ready" in receipt["missing_required"]
    assert "clean_vm_install_matrix" in receipt["missing_required"]
    assert "all_local_v1_fr_verified" in receipt["missing_required"]
    assert "local_v1_complete" in response["not_claiming"]
    assert "local_v1_exe" in response["not_claiming"]
    path = write_release_readiness_receipt(response, tmp_path / "readiness.json")
    assert path.is_file()
