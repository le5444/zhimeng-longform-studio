from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.knowledge_api import create_knowledge_router
from zhimeng_runtime.knowledge_service import (
    KnowledgeNotFoundError,
)


PROJECT_ID = "project-0123456789abcdef01234567"


class _ErrorService:
    """Small service double used to test routing and error translation only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def get_scope(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("get_scope", args, kwargs))
        raise KnowledgeNotFoundError("project knowledge scope was not found")

    def list_cards(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("list_cards", args, kwargs))
        raise KnowledgeNotFoundError("knowledge cards were not found")


def _client(service: _ErrorService) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_knowledge_router(service))
    return TestClient(app)


def test_project_knowledge_routes_are_exposed_with_stable_paths() -> None:
    service = _ErrorService()
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_knowledge_router(service))
    paths = app.openapi()["paths"]

    expected_project_paths = {
        f"/api/v1/projects/{{project_id}}/knowledge/scope",
        f"/api/v1/projects/{{project_id}}/knowledge/cards",
        f"/api/v1/projects/{{project_id}}/knowledge/candidates",
        f"/api/v1/projects/{{project_id}}/knowledge/source-imports/{{source_import_id}}/candidates",
        f"/api/v1/projects/{{project_id}}/knowledge/candidates/{{candidate_id}}",
        f"/api/v1/projects/{{project_id}}/knowledge/candidates/{{candidate_id}}/promote",
        f"/api/v1/projects/{{project_id}}/knowledge/candidates/{{candidate_id}}/reject",
        f"/api/v1/projects/{{project_id}}/knowledge/source-imports/{{source_import_id}}/formalization",
        f"/api/v1/projects/{{project_id}}/knowledge/source-imports/{{source_import_id}}/formalization/finalize",
        f"/api/v1/projects/{{project_id}}/knowledge/cards/{{card_id}}/enable",
        f"/api/v1/projects/{{project_id}}/knowledge/cards/{{card_id}}/disable",
        f"/api/v1/projects/{{project_id}}/knowledge/cards/{{card_id}}/rollback",
    }
    assert expected_project_paths <= paths.keys()

    expected_legacy_paths = {
        "/api/v1/knowledge/candidates",
        "/api/v1/knowledge/candidates/{candidate_id}",
        "/api/v1/knowledge/candidates/{candidate_id}/promote",
        "/api/v1/knowledge/candidates/{candidate_id}/reject",
        "/api/v1/knowledge/cards",
        "/api/v1/knowledge/cards/{card_id}/disable",
        "/api/v1/knowledge/cards/{card_id}/enable",
        "/api/v1/knowledge/cards/{card_id}/rollback",
        "/api/v1/knowledge/source-imports/{source_import_id}/candidates",
        "/api/v1/knowledge/source-imports/{source_import_id}/formalization",
        "/api/v1/knowledge/source-imports/{source_import_id}/finalize",
    }
    assert expected_legacy_paths <= paths.keys()


def test_project_scope_errors_are_mapped_and_project_id_is_forwarded() -> None:
    service = _ErrorService()
    with _client(service) as client:
        response = client.get(f"/api/v1/projects/{PROJECT_ID}/knowledge/scope")

    assert response.status_code == 404
    payload = response.json()
    assert payload["schema"] == "zhimeng.error.v1"
    assert payload["error"]["code"] == "knowledge_not_found"
    assert service.calls == [("get_scope", (), {"project_id": PROJECT_ID})]


def test_legacy_cards_route_does_not_receive_project_scope_arguments() -> None:
    service = _ErrorService()
    with _client(service) as client:
        response = client.get("/api/v1/knowledge/cards")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "knowledge_not_found"
    assert service.calls == [("list_cards", (), {})]
