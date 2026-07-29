from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


@pytest.fixture
def settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=("http://127.0.0.1:1420",),
        sse_heartbeat_seconds=0.05,
    )


@pytest.fixture
def app(settings: RuntimeSettings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client
