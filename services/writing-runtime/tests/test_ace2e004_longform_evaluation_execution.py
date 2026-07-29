from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


def _create_longform_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    chapters = {
        "CH001": "第一章。青崖在旧港找到钥匙。",
        "CH002": "第二章。白灯仍亮，证人未现身。",
        "CH003": "第三章。他把钥匙放回袖中。",
        "CH010": "第十章。秘密代价第一次被点破。",
        "CH011": "第十一章。旧账翻出第二层。",
        "CH012": "第十二章。他选择先还一半。",
    }
    for name, body in chapters.items():
        (volume / f"{name}.md").write_text(body + "\n", encoding="utf-8")


def _discover_chapters(project_root: Path) -> list[str]:
    found: set[str] = set()
    for path in project_root.rglob("*.md"):
        relative = path.relative_to(project_root).as_posix()
        if not relative.startswith("05_正文内容/"):
            continue
        for match in re.findall(r"CH\d{3}", path.stem.upper()):
            found.add(match)
        for match in re.findall(r"CH\d{3}", path.read_text(encoding="utf-8", errors="replace").upper()):
            found.add(match)
    return sorted(found)


def test_ace2e004_longform_evaluation_scenario_execution_matrix(tmp_path: Path) -> None:
    """AC-E2E-004 residual slice:

    real multi-chapter longform project
    -> discover chapter evidence from disk
    -> execute evaluation scenario pack matrix
    -> quality_assessed remains false
    -> external blind-review matrix remains incomplete

    Does not claim literary quality scores or training readiness.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-eval-book"
    _create_longform_project(project)

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        snapshot = inspected.json()
        project_id = snapshot["project"]["project_id"]
        revision = snapshot["project"]["revision"]
        fingerprint = snapshot["project"]["fingerprint"]

        chapters = _discover_chapters(project)
        assert {"CH001", "CH002", "CH003", "CH010", "CH011", "CH012"} <= set(chapters)

        pack_id = "local-v1-longform-quality-scenarios"
        # partial evidence first: only opening chapters + core gate/reader artifacts
        partial = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions",
            json={
                "project_id": project_id,
                "project_revision": revision,
                "project_fingerprint": fingerprint,
                "available_chapters": chapters,
                "available_artifacts": [
                    "gate_pipeline",
                    "reader_review",
                    "writeback_evidence",
                ],
                "member_count": 2,
                "has_provider_fingerprint": True,
                "blind_review_finalized": True,
                "external_blind_review_present": False,
            },
        )
        assert partial.status_code == 201, partial.text
        partial_exec = partial.json()["execution"]
        assert partial_exec["project_id"] == project_id
        assert partial_exec["quality_assessed"] is False
        assert partial_exec["formal_draft_allowed"] is False
        assert partial_exec["matrix_complete"] is False
        lfq001 = next(item for item in partial_exec["results"] if item["scenario_id"] == "LFQ-001")
        assert lfq001["evidence_complete"] is True
        assert lfq001["status"] == "executed"
        lfq002 = next(item for item in partial_exec["results"] if item["scenario_id"] == "LFQ-002")
        assert lfq002["evidence_complete"] is False
        assert "literary_quality_gate" in lfq002["missing_artifacts"]

        full = client.post(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions",
            json={
                "project_id": project_id,
                "project_revision": revision,
                "project_fingerprint": fingerprint,
                "available_chapters": chapters,
                "available_artifacts": [
                    "gate_pipeline",
                    "reader_review",
                    "writeback_evidence",
                    "literary_quality_gate",
                    "hard_fact_gate",
                    "reader_review_decision",
                    "feedback_candidate_bundle",
                    "eval_corpus_export",
                    "eval_corpus_baseline",
                    "eval_corpus_regression",
                    "skill_rule_evolution",
                ],
                "suite_id": "eval-suite-" + ("1" * 32),
                "suite_receipt_hash": "2" * 64,
                "member_count": 2,
                "has_provider_fingerprint": True,
                "blind_review_finalized": True,
                "external_blind_review_present": False,
            },
        )
        assert full.status_code == 201, full.text
        execution = full.json()["execution"]
        assert execution["all_executed"] is True
        assert execution["matrix_complete"] is False
        assert execution["quality_assessed"] is False
        assert execution["literary_quality_score"] is None
        assert all(item["scored"] is False for item in execution["results"])
        assert "external_blind_review_matrix" in full.json()["not_claiming"]

        latest = client.get(
            f"/api/v1/evaluation-scenario-packs/{pack_id}/executions/latest",
            params={"project_id": project_id},
        )
        assert latest.status_code == 200, latest.text
        assert latest.json()["execution"]["execution_id"] == execution["execution_id"]
