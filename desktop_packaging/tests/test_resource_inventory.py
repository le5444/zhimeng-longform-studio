from __future__ import annotations

from pathlib import Path

from desktop_packaging.portable.resource_inventory import (
    assemble_and_inventory,
    inventory_portable_bundle,
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


def test_portable_resource_inventory(tmp_path: Path) -> None:
    response = assemble_and_inventory(
        output_dir=tmp_path / "bundle",
        product_version="0.1.0-res",
        workbench_dist=_fixture_workbench(tmp_path),
        runtime_src_root=_fixture_runtime_src(tmp_path),
    )
    receipt = response["receipt"]
    assert receipt["file_count"] >= 3
    assert receipt["total_bytes"] > 0
    assert receipt["process_rss_measured"] is False
    assert receipt["local_v1_exe_claim_allowed"] is False
    assert receipt["signature_ready"] is False
    assert "release_process_rss" in response["not_claiming"]
    assert "local_v1_exe" in response["not_claiming"]

    again = inventory_portable_bundle(tmp_path / "bundle")
    assert again["receipt"]["file_count"] == receipt["file_count"]
