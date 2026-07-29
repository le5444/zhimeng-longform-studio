from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


FAMILIES = (
    ("source_ingest", "raw-source-ingest"),
    ("distillation", "raw-distillation"),
    ("knowledge_context", "raw-knowledge-context"),
    ("creation_suite", "raw-creation-suite"),
)


def _create_longform_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    (volume / "CH001.md").write_text("第一章。青崖找到钥匙。\n", encoding="utf-8")
    (volume / "CH002.md").write_text("第二章。旧港只剩一盏白灯。\n", encoding="utf-8")
    (volume / "CH003.md").write_text("第三章。他仍未开门。\n", encoding="utf-8")


def _write_raw_skill(root: Path, *, name: str, description: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Instruction-only raw skill body for adaptation.\n",
        encoding="utf-8",
    )
    return root


def test_ace2e001_longform_plus_four_skills_workflow_binding(tmp_path: Path) -> None:
    """AC-E2E-001 residual slice:

    long-form multi-chapter project
    -> four raw skills plan/materialize
    -> generic instruction workflow binding
    -> formal draft remains denied

    Does not claim four_skills_generic_exactly_once or raw SKILL.md execution.
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "longform-book"
    _create_longform_project(project)
    trusted = tmp_path / "trusted-skills"
    members = []
    for family, name in FAMILIES:
        path = _write_raw_skill(
            trusted / name,
            name=name,
            description=f"Raw instruction package for {family}.",
        )
        members.append({"family": family, "source_path": str(path)})

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            trusted_skill_roots=(trusted,),
            managed_skill_root=Path(__file__).resolve().parents[3] / "adapters" / "builtin",
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
        files = [
            item
            for item in snapshot["files"]
            if item["relative_path"].endswith((".md",))
            and "05_正文内容" in item["relative_path"]
        ]
        assert len(files) >= 3

        planned = client.post(
            "/api/v1/four-skills-raw-adaptations/plan",
            json={"members": members},
        )
        assert planned.status_code == 201, planned.text
        batch_id = planned.json()["receipt"]["batch_id"]
        batch_hash = planned.json()["receipt"]["batch_hash"]

        materialized = client.post(
            "/api/v1/four-skills-raw-adaptations/materialize",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": batch_hash,
                "import_after_materialize": True,
            },
        )
        assert materialized.status_code == 200, materialized.text
        mat = materialized.json()["receipt"]
        assert mat["authorization_input_ready"] is True
        assert mat["four_skills_generic_exactly_once"] is False
        assert mat["raw_skill_md_execution"] is False

        bound = client.post(
            f"/api/v1/four-skills-raw-adaptations/{batch_id}/workflow-bindings",
            json={
                "expected_batch_id": batch_id,
                "expected_batch_hash": mat["batch_hash"],
            },
        )
        assert bound.status_code == 201, bound.text
        binding = bound.json()["receipt"]
        assert binding["workflow_ready"] is True
        assert binding["executable"] is False
        assert binding["raw_skill_md_execution"] is False
        assert binding["four_skills_generic_exactly_once"] is False
        assert all(step["runner"] == "instruction_source" for step in binding["steps"])

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
            },
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["formal_draft_allowed"] is False
        assert formal_receipt.get("decision", "denied") in {"denied", "deny", "not_allowed"} or formal_receipt["formal_draft_allowed"] is False
