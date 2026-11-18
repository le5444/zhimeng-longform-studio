"""Queued work follows updated credentials for its own saved model selection."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from test_provider_routes_20261005 import _chat_text
from zhimeng_runtime import app as runtime_app


@pytest.mark.parametrize("prepared", [False, True])
def test_queued_input_refreshes_changed_headers_without_switching_model_or_request(settings, monkeypatch, prepared):
    calls = []
    original_factory = runtime_app.build_agent_provider_from_connection

    async def handler(request):
        calls.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return _chat_text("Local queue response.")

    def factory(settings, **kwargs):
        provider = original_factory(settings, **kwargs)
        if provider is not None:
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return provider

    monkeypatch.setattr(runtime_app, "build_agent_provider_from_connection", factory)
    with TestClient(runtime_app.create_app(settings)) as client:
        service = client.app.state.agent_service
        worker = client.app.state.agent_queue_scheduler
        client.portal.call(worker.stop)
        for provider_id in ["chosen", "other"]:
            response = client.put("/api/v1/settings/providers", json={
                "provider_id": provider_id, "name": provider_id, "base_url": "https://local-test.invalid/v1",
                "model": provider_id + "-model", "selected_models": [provider_id + "-model"],
                "wire_format": "chat", "reasoning_effort": "high", "api_key": "synthetic-credential",
                "extra_headers": {"X-Old": "synthetic-value"}, "make_active": provider_id == "chosen",
            })
            assert response.status_code == 200, response.text
        response = client.post("/api/v1/agent/sessions", json={"title": "Queued credential update"})
        assert response.status_code == 201, response.text
        current = response.json()
        sid = current["session_id"]
        preference = current["capabilities"]["extensions"]["x-zhimeng.provider-preference"]["config"]
        item_id = "queue-model-refresh"
        queue_url = f"/api/v1/agent/sessions/{sid}/queue"
        response = client.post(queue_url, json={"item_id": item_id, "message": "Summarize the document plan."})
        assert response.status_code == 200, response.text
        frozen = client.app.state.agent_queue_service.store.prepare(sid, item_id).model_dump() if prepared else None
        assert client.post("/api/v1/settings/providers/other/activate").status_code == 200
        response = client.put("/api/v1/settings/providers", json={
            "provider_id": "chosen", "name": "chosen", "remove_header_names": ["x-old"], "make_active": False,
        })
        assert response.status_code == 200, response.text

        started_requests = []
        original_start = service.start_run

        async def record_start(**kwargs):
            started_requests.append(kwargs)
            return await original_start(**kwargs)

        monkeypatch.setattr(service, "start_run", record_start)
        client.portal.call(worker.tick)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            runs = service.store.list_runs(sid)
            if runs and runs[0].status in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(.01)
        assert len(runs) == 1 and runs[0].status == "succeeded", runs
        assert len(calls) == 1
        assert calls[0]["body"]["model"] == "chosen-model"
        assert "x-old" not in calls[0]["headers"]
        after = client.get(f"/api/v1/agent/sessions/{sid}").json()
        assert after["executor"] != current["executor"]
        assert after["capabilities"]["extensions"]["x-zhimeng.provider-preference"]["config"] == preference
        if frozen is not None:
            assert {name: value for name, value in started_requests[0].items() if name not in {"session_id", "idempotency_key"}} == frozen
        assert client.get("/api/v1/settings/providers").json()["active_provider_id"] == "other"
        # An accepted replay returns its durable receipt, even after config changes.
        response = client.put("/api/v1/settings/providers", json={
            "provider_id": "chosen", "name": "chosen", "extra_headers": {"X-New": "next-request-only"}, "make_active": False,
        })
        assert response.status_code == 200, response.text
        response = client.post(f"{queue_url}/{item_id}/dispatch")
        assert response.status_code == 200, response.text
        assert response.json()["accepted"]["replayed"] is True
        assert response.json()["accepted"]["run"]["run_id"] == runs[0].run_id
        assert client.get(f"/api/v1/agent/sessions/{sid}").json()["executor"] == after["executor"]
        assert len(calls) == len(started_requests) == 1
