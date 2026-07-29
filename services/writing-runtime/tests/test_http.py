from __future__ import annotations

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SCHEMA_VERSION


def test_health_and_runtime_status(client: TestClient) -> None:
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    runtime = client.get("/api/v1/runtime/status")

    assert live.status_code == 200
    assert live.json()["schema"] == "zhimeng.health.v1"
    assert live.json()["status"] == "live"
    assert ready.status_code == 200
    assert ready.json()["schema"] == "zhimeng.health.v1"
    assert ready.json()["status"] == "ready"
    assert ready.json()["database_schema_version"] == SCHEMA_VERSION
    assert runtime.status_code == 200
    assert runtime.json()["schema"] == "zhimeng.runtime-status.v1"
    assert runtime.json()["state"] == "ready"
    assert runtime.json()["database"] == {
        "ready": True,
        "journal_mode": "wal",
        "schema_version": SCHEMA_VERSION,
    }
    assert runtime.json()["event_transport"] == "sse"


def test_openapi_contains_phase_one_endpoints(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {
        "/health/live",
        "/health/ready",
        "/api/v1/runtime/status",
        "/api/v1/events/stream",
    }.issubset(paths)


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    allowed = client.get(
        "/health/live",
        headers={"Origin": "http://127.0.0.1:1420"},
    )
    denied = client.get(
        "/health/live",
        headers={"Origin": "http://localhost:9999"},
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:1420"
    assert "ETag" in allowed.headers["access-control-expose-headers"]
    assert "access-control-allow-origin" not in denied.headers

    preflight = client.options(
        "/api/v1/projects/project-test/files/file-test",
        headers={
            "Origin": "http://127.0.0.1:1420",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "If-None-Match",
        },
    )
    assert preflight.status_code == 200
    assert "if-none-match" in preflight.headers["access-control-allow-headers"].lower()


def test_not_found_uses_structured_error_envelope(client: TestClient) -> None:
    response = client.get(
        "/missing",
        headers={"X-Request-ID": "test-request-123"},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "test-request-123"
    assert response.json() == {
        "schema": "zhimeng.error.v1",
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "request_id": "test-request-123",
            "retryable": False,
        }
    }


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get(
        "/health/live",
        headers={"X-Request-ID": "contains spaces"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "contains spaces"


def test_readiness_failure_uses_structured_error_envelope(tmp_path) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("block database parent creation", encoding="utf-8")
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=blocking_file / "runtime.sqlite3",
            allowed_origins=(),
        )
    )

    with TestClient(app) as degraded_client:
        live = degraded_client.get("/health/live")
        ready = degraded_client.get(
            "/health/ready",
            headers={"X-Request-ID": "readiness-check"},
        )

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["error"]["code"] == "runtime_not_ready"
    assert ready.json()["error"]["request_id"] == "readiness-check"
