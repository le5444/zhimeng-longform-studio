"""Lightweight Local v1 NFR measurement harness entries.

These measurements are Runtime-only. They do not claim desktop-shell cold start
or full release packaging evidence.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def measure_runtime_health_latency(
    *,
    database_path: Path,
    samples: int = 20,
) -> dict[str, Any]:
    samples = max(5, min(samples, 200))
    settings = RuntimeSettings(
        environment="test",
        database_path=database_path,
        allowed_origins=(),
    )
    latencies_ms: list[float] = []
    with TestClient(create_app(settings)) as client:
        client.get("/health/ready")
        for _ in range(samples):
            started = time.perf_counter()
            response = client.get("/health/ready")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if response.status_code != 200:
                raise RuntimeError(f"health/ready failed: {response.status_code}")
            latencies_ms.append(elapsed_ms)
    ordered = sorted(latencies_ms)
    p50 = ordered[int(0.50 * (len(ordered) - 1))]
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    generated_at = datetime.now(UTC).isoformat()
    report = {
        "schema": "zhimeng.nfr-runtime-health-latency.v1",
        "surface": "runtime_health_ready",
        "samples": samples,
        "latency_ms": {
            "min": min(ordered),
            "p50": p50,
            "p95": p95,
            "max": max(ordered),
            "mean": sum(ordered) / len(ordered),
        },
        "formal_draft_allowed": False,
        "local_v1_exe": False,
        "desktop_shell_cold_start_measured": False,
        "policy_version": "local-v1-nfr-measure-1",
        "generated_at": generated_at,
        "not_claiming": [
            "desktop_shell_cold_start",
            "release_package_startup",
            "local_v1_exe",
            "nfr_release_gate_pass",
        ],
    }
    return report


def measure_project_search_latency(
    *,
    database_path: Path,
    project_root: Path,
    samples: int = 10,
) -> dict[str, Any]:
    """Measure project search rebuild + query latency for one small local project.

    Does not claim LREF-PROJECT-1 release benchmark or desktop UI latency.
    """
    from zhimeng_runtime.project_reader import SECTION_LABELS

    samples = max(3, min(samples, 50))
    project_root = Path(project_root)
    if not project_root.exists():
        project_root.mkdir(parents=True)
        for index, label in SECTION_LABELS.items():
            (project_root / f"{index:02d}_{label}").mkdir(exist_ok=True)
        volume = project_root / "05_正文内容" / "第一卷"
        volume.mkdir(parents=True, exist_ok=True)
        (volume / "CH001.md").write_text("青崖在旧港找到带血的钥匙。\n", encoding="utf-8")
        (project_root / "03_人物设定" / "主角.md").write_text(
            "主角青崖擅长借势破局。\n", encoding="utf-8"
        )

    library = project_root.parent
    settings = RuntimeSettings(
        environment="test",
        database_path=database_path,
        trusted_project_roots=(library,),
        allowed_origins=(),
    )
    rebuild_ms: list[float] = []
    query_ms: list[float] = []
    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        if inspected.status_code != 200:
            raise RuntimeError(f"inspect failed: {inspected.status_code} {inspected.text}")
        project_id = inspected.json()["project"]["project_id"]
        for _ in range(samples):
            started = time.perf_counter()
            rebuilt = client.post(
                f"/api/v1/projects/{project_id}/search/rebuild",
                json={"force": True},
            )
            rebuild_ms.append((time.perf_counter() - started) * 1000.0)
            if rebuilt.status_code != 200:
                raise RuntimeError(f"rebuild failed: {rebuilt.status_code} {rebuilt.text}")
            started = time.perf_counter()
            searched = client.post(
                f"/api/v1/projects/{project_id}/search",
                json={"query": "青崖"},
            )
            query_ms.append((time.perf_counter() - started) * 1000.0)
            if searched.status_code != 200:
                raise RuntimeError(f"search query failed: {searched.status_code} {searched.text}")

    def _stats(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "min": min(ordered),
            "p50": ordered[int(0.50 * (len(ordered) - 1))],
            "p95": ordered[int(0.95 * (len(ordered) - 1))],
            "max": max(ordered),
            "mean": sum(ordered) / len(ordered),
        }

    generated_at = datetime.now(UTC).isoformat()
    return {
        "schema": "zhimeng.nfr-project-search-latency.v1",
        "surface": "project_search_rebuild_query",
        "samples": samples,
        "rebuild_latency_ms": _stats(rebuild_ms),
        "query_latency_ms": _stats(query_ms),
        "formal_draft_allowed": False,
        "local_v1_exe": False,
        "lref_project_1_measured": False,
        "policy_version": "local-v1-nfr-measure-1",
        "generated_at": generated_at,
        "not_claiming": [
            "LREF-PROJECT-1",
            "desktop_search_ui_latency",
            "local_v1_exe",
            "nfr_release_gate_pass",
        ],
    }


def write_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path




def measure_context_compile_latency(
    *,
    database_path: Path | None = None,
    project_root: Path | None = None,
    samples: int = 20,
) -> dict[str, Any]:
    """Measure pure Context Pack compile latency with synthetic candidates.

    Runtime compiler path only. Does not claim LREF-PROJECT-1 + LREF-KB-1,
    13-profile matrix, or release p95 gate.
    """
    from zhimeng_runtime.context_compiler import compile_context_pack
    from zhimeng_runtime.context_models import (
        CompletedHistoryMessage,
        ContextAuthority,
        ContextCandidateFragment,
        ContextCreateRequest,
        canonical_json_hash,
    )

    samples = max(5, min(samples, 100))
    fingerprint = canonical_json_hash({"project": "nfr-context"})
    request = ContextCreateRequest(
        objective="续写下一章并保持人物压力连续",
        profile="chapter",
        active_chapter_id="CH001",
        expected_project_revision=1,
        expected_project_fingerprint=fingerprint,
        project_kb_state_id="project-kb-state-" + ("c" * 32),
        project_kb_manifest_hash="d" * 64,
    )
    history = (
        CompletedHistoryMessage(ordinal=1, role="user", content="准备续写。"),
        CompletedHistoryMessage(ordinal=2, role="assistant", content="已核对上一章结果。"),
    )
    candidates = tuple(
        ContextCandidateFragment(
            source_id=f"frag-{index:03d}",
            source_type="chapter_fact",
            title=f"片段{index}",
            authority=ContextAuthority.PROJECT_TRUTH,
            version="revision-1",
            source_hash=canonical_json_hash({"source": f"frag-{index:03d}"}),
            content=f"青崖在旧港的第{index}个线索。",
            priority=index,
            mandatory=False,
            eligible=True,
        )
        for index in range(1, 13)
    )
    compile_ms: list[float] = []
    for sample in range(samples):
        started = time.perf_counter()
        pack = compile_context_pack(
            context_pack_id=f"context-pack-nfr-{sample:04d}",
            session_id="session-nfr-context",
            project_id="project-nfr-context",
            history_through_ordinal=2,
            request=request,
            history=history,
            candidates=candidates,
            max_characters=4000,
            created_at=datetime.now(UTC),
            max_tokens=1200,
        )
        compile_ms.append((time.perf_counter() - started) * 1000.0)
        if pack is None:
            raise RuntimeError("context compile returned no pack")
    ordered = sorted(compile_ms)
    generated_at = datetime.now(UTC).isoformat()
    return {
        "schema": "zhimeng.nfr-context-compile-latency.v1",
        "surface": "context_pack_compile_pure",
        "samples": samples,
        "compile_latency_ms": {
            "min": min(ordered),
            "p50": ordered[int(0.50 * (len(ordered) - 1))],
            "p95": ordered[int(0.95 * (len(ordered) - 1))],
            "max": max(ordered),
            "mean": sum(ordered) / len(ordered),
        },
        "formal_draft_allowed": False,
        "local_v1_exe": False,
        "lref_project_1_measured": False,
        "lref_kb_1_measured": False,
        "profile_matrix_complete": False,
        "policy_version": "local-v1-nfr-measure-2",
        "generated_at": generated_at,
        "not_claiming": [
            "LREF-PROJECT-1",
            "LREF-KB-1",
            "thirteen_profile_matrix",
            "release_p95_gate",
            "local_v1_exe",
            "nfr_release_gate_pass",
        ],
    }



def measure_project_asset_index_latency(
    *,
    database_path: Path,
    project_root: Path,
    samples: int = 10,
) -> dict[str, Any]:
    """Measure project asset index rebuild + fetch latency for one small local project.

    Does not claim LREF-PROJECT-1, full relationship-graph completeness, or desktop UI latency.
    """
    from zhimeng_runtime.project_reader import SECTION_LABELS

    samples = max(3, min(samples, 50))
    project_root = Path(project_root)
    if not project_root.exists():
        project_root.mkdir(parents=True)
        for index, label in SECTION_LABELS.items():
            (project_root / f"{index:02d}_{label}").mkdir(exist_ok=True)
        volume = project_root / "05_正文内容" / "第一卷"
        volume.mkdir(parents=True, exist_ok=True)
        (volume / "CH001.md").write_text("青崖在旧港找到带血的钥匙。\n", encoding="utf-8")
        (project_root / "03_人物设定" / "主角.md").write_text(
            "主角青崖擅长借势破局。\n", encoding="utf-8"
        )
        (project_root / "04_剧情大纲" / "全书大纲.md").write_text(
            "旧港白灯尚未点破。\n", encoding="utf-8"
        )

    library = project_root.parent
    settings = RuntimeSettings(
        environment="test",
        database_path=database_path,
        trusted_project_roots=(library,),
        allowed_origins=(),
    )
    rebuild_ms: list[float] = []
    fetch_ms: list[float] = []
    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        if inspected.status_code != 200:
            raise RuntimeError(f"inspect failed: {inspected.status_code} {inspected.text}")
        project_id = inspected.json()["project"]["project_id"]
        for _ in range(samples):
            started = time.perf_counter()
            rebuilt = client.post(f"/api/v1/projects/{project_id}/assets/rebuild")
            rebuild_ms.append((time.perf_counter() - started) * 1000.0)
            if rebuilt.status_code != 200:
                raise RuntimeError(f"asset rebuild failed: {rebuilt.status_code} {rebuilt.text}")
            started = time.perf_counter()
            fetched = client.get(f"/api/v1/projects/{project_id}/assets")
            fetch_ms.append((time.perf_counter() - started) * 1000.0)
            if fetched.status_code != 200:
                raise RuntimeError(f"asset fetch failed: {fetched.status_code} {fetched.text}")

    def _stats(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "min": min(ordered),
            "p50": ordered[int(0.50 * (len(ordered) - 1))],
            "p95": ordered[int(0.95 * (len(ordered) - 1))],
            "max": max(ordered),
            "mean": sum(ordered) / len(ordered),
        }

    generated_at = datetime.now(UTC).isoformat()
    return {
        "schema": "zhimeng.nfr-project-asset-index-latency.v1",
        "surface": "project_asset_index_rebuild_fetch",
        "samples": samples,
        "rebuild_latency_ms": _stats(rebuild_ms),
        "fetch_latency_ms": _stats(fetch_ms),
        "formal_draft_allowed": False,
        "local_v1_exe": False,
        "relationship_graph_complete": False,
        "lref_project_1_measured": False,
        "policy_version": "local-v1-nfr-measure-3",
        "generated_at": generated_at,
        "not_claiming": [
            "LREF-PROJECT-1",
            "relationship_graph_complete",
            "desktop_assets_ui_latency",
            "local_v1_exe",
            "nfr_release_gate_pass",
        ],
    }


__all__ = [
    "measure_context_compile_latency",
    "measure_project_asset_index_latency",
    "measure_project_search_latency",
    "measure_runtime_health_latency",
    "write_report",
]
