from __future__ import annotations

from pathlib import Path

from zhimeng_runtime.nfr_measure import measure_runtime_health_latency, write_report


def test_runtime_health_latency_measurement(tmp_path: Path) -> None:
    report = measure_runtime_health_latency(
        database_path=tmp_path / "runtime.sqlite3",
        samples=8,
    )
    assert report["schema"] == "zhimeng.nfr-runtime-health-latency.v1"
    assert report["samples"] == 8
    assert report["latency_ms"]["p95"] >= report["latency_ms"]["p50"]
    assert report["desktop_shell_cold_start_measured"] is False
    assert report["local_v1_exe"] is False
    assert "desktop_shell_cold_start" in report["not_claiming"]
    path = write_report(report, tmp_path / "nfr-runtime-health-latency.json")
    assert path.is_file()

def test_project_search_latency_measurement(tmp_path: Path) -> None:
    from zhimeng_runtime.nfr_measure import measure_project_search_latency

    library = tmp_path / "library"
    project = library / "book"
    report = measure_project_search_latency(
        database_path=tmp_path / "runtime.sqlite3",
        project_root=project,
        samples=3,
    )
    assert report["schema"] == "zhimeng.nfr-project-search-latency.v1"
    assert report["samples"] == 3
    assert report["rebuild_latency_ms"]["p95"] >= report["rebuild_latency_ms"]["p50"]
    assert report["query_latency_ms"]["p95"] >= report["query_latency_ms"]["p50"]
    assert report["lref_project_1_measured"] is False
    assert "LREF-PROJECT-1" in report["not_claiming"]


def test_context_compile_latency_measurement(tmp_path: Path) -> None:
    from zhimeng_runtime.nfr_measure import measure_context_compile_latency

    report = measure_context_compile_latency(samples=8)
    assert report["schema"] == "zhimeng.nfr-context-compile-latency.v1"
    assert report["samples"] == 8
    assert report["compile_latency_ms"]["p95"] >= report["compile_latency_ms"]["p50"]
    assert report["lref_project_1_measured"] is False
    assert report["lref_kb_1_measured"] is False
    assert report["profile_matrix_complete"] is False
    assert "LREF-KB-1" in report["not_claiming"]
    assert "thirteen_profile_matrix" in report["not_claiming"]


def test_measure_project_asset_index_latency(tmp_path: Path) -> None:
    from zhimeng_runtime.nfr_measure import measure_project_asset_index_latency

    report = measure_project_asset_index_latency(
        database_path=tmp_path / "runtime.sqlite3",
        project_root=tmp_path / "library" / "book",
        samples=4,
    )
    assert report["schema"] == "zhimeng.nfr-project-asset-index-latency.v1"
    assert report["samples"] == 4
    assert report["rebuild_latency_ms"]["p50"] >= 0
    assert report["fetch_latency_ms"]["p95"] >= 0
    assert report["formal_draft_allowed"] is False
    assert report["local_v1_exe"] is False
    assert report["relationship_graph_complete"] is False
    assert report["lref_project_1_measured"] is False
    assert "LREF-PROJECT-1" in report["not_claiming"]

