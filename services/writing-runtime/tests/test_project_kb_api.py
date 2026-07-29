from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.knowledge_store import KnowledgeStore

from test_knowledge_api import _settings, _write_project


def _rebuild_request(app, project_id: str) -> dict[str, object]:
    store = KnowledgeStore(app.state.runtime.store)
    store.ensure_project_scope(project_id)
    snapshot = store.project_state_snapshot(project_id)
    return {
        "expected_project_revision": snapshot.project.revision,
        "expected_project_fingerprint": snapshot.project.fingerprint,
        "expected_scope_state_revision": snapshot.scope.state_revision,
        "expected_knowledge_fingerprint": canonical_json_hash(
            snapshot.fingerprint_material
        ),
    }


def test_project_kb_api_rebuilds_missing_state_and_reads_current(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    project_root = _write_project(projects / "novel")
    app = create_app(_settings(tmp_path, trusted_project_roots=(projects,)))

    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]

        missing = client.get(f"/api/v1/projects/{project_id}/knowledge/kb-state")
        assert missing.status_code == 200, missing.text
        assert missing.json()["status"] == "missing"

        request = _rebuild_request(app, project_id)
        rebuilt = client.post(
            f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
            json=request,
        )
        assert rebuilt.status_code == 200, rebuilt.text
        payload = rebuilt.json()
        assert payload["reused_snapshot"] is False
        assert payload["record"]["status"] == "current"
        assert payload["record"]["manifest"]["source_of_truth"] == {
            "schema": "zhimeng.project-kb-source-of-truth.v1",
            "project_files": "work_truth",
            "runtime_sqlite": "run_truth",
            "derived": True,
            "rebuildable": True,
        }

        current = client.get(f"/api/v1/projects/{project_id}/knowledge/kb-state")
        assert current.status_code == 200, current.text
        assert current.json() == payload["record"]


def test_project_kb_api_rejects_stale_rebuild_cas(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    project_root = _write_project(projects / "novel")
    app = create_app(_settings(tmp_path, trusted_project_roots=(projects,)))

    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        project_id = inspected.json()["project"]["project_id"]
        request = _rebuild_request(app, project_id)
        request["expected_scope_state_revision"] = int(
            request["expected_scope_state_revision"]
        ) + 1

        response = client.post(
            f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
            json=request,
        )

        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "project_kb_conflict"
        assert error["retryable"] is True
        assert error["details"][0]["field"] == "scope_state_revision"


def test_project_kb_api_is_explicitly_unavailable_without_project_reader(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/projects/project-000000000000000000000000/knowledge/kb-state"
        )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "project_kb_unavailable"
