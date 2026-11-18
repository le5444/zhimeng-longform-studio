"""Saved credentials change the next dispatch identity, not the conversation's model or history."""
from __future__ import annotations

import json
import time

import httpx
from fastapi.testclient import TestClient

from zhimeng_runtime import app as runtime_app
from zhimeng_runtime.agent_service import AgentService
from test_provider_routes_20261005 import _chat_text


def test_header_change_refreshes_same_session_selection_and_preserves_cached_prefix(settings, monkeypatch):
    requests = []
    original_factory = runtime_app.build_agent_provider_from_connection

    async def handler(request):
        requests.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return _chat_text("本机合成回复。")

    def factory(settings, **kwargs):
        provider = original_factory(settings, **kwargs)
        if provider is not None:
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return provider

    async def skip_background(self, *args):
        return None

    monkeypatch.setattr(runtime_app, "build_agent_provider_from_connection", factory)
    monkeypatch.setattr(AgentService, "_maybe_name_session", skip_background)
    app = runtime_app.create_app(settings)

    with TestClient(app) as client:
        for provider_id, model in [("chosen", "chosen-model"), ("other", "other-model")]:
            saved = client.put("/api/v1/settings/providers", json={
                "provider_id": provider_id, "name": provider_id, "base_url": "https://local-test.invalid/v1",
                "model": model, "selected_models": [model], "wire_format": "chat", "reasoning_effort": "high",
                "api_key": "synthetic-credential", "extra_headers": {"X-Old": "synthetic-value"},
                "make_active": provider_id == "chosen",
            })
            assert saved.status_code == 200, saved.text
        session_response = client.post("/api/v1/agent/sessions", json={"title": "连接更新验收"})
        assert session_response.status_code == 201, session_response.text
        first_session = session_response.json()
        session_id = first_session["session_id"]
        preference = first_session["capabilities"]["extensions"]["x-zhimeng.provider-preference"]["config"]

        def finish_run(text, key):
            response = client.post(f"/api/v1/agent/sessions/{session_id}/runs", headers={"Idempotency-Key": key},
                                   json={"message": text, "writer_mode": "repair_only"})
            assert response.status_code == 202, response.text
            run_id = response.json()["run"]["run_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
                if run["status"] in {"succeeded", "failed", "interrupted", "cancelled"}:
                    assert run["status"] == "succeeded", run
                    return
                time.sleep(0.02)
            raise AssertionError("Synthetic run did not complete")

        finish_run("第一轮：确认文件任务。", "settings-refresh-first")
        first_messages = client.get(f"/api/v1/agent/sessions/{session_id}/messages").json()["messages"]
        # 同时更改全局默认，证明 refresh 仍使用会话保存的选择。
        assert client.post("/api/v1/settings/providers/other/activate").status_code == 200
        changed = client.put("/api/v1/settings/providers", json={
            "provider_id": "chosen", "name": "chosen", "remove_header_names": ["x-old"], "make_active": False,
        })
        assert changed.status_code == 200, changed.text

        stale = client.post(f"/api/v1/agent/sessions/{session_id}/runs", headers={"Idempotency-Key": "settings-refresh-stale"},
                            json={"message": "第二轮：确认文件任务。", "writer_mode": "repair_only"})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "workflow_executor_changed"
        refreshed = client.patch(f"/api/v1/agent/sessions/{session_id}", json={"rebind_provider": True, **preference})
        assert refreshed.status_code == 200, refreshed.text
        after = refreshed.json()
        assert after["executor"] != first_session["executor"]
        assert after["capabilities"]["extensions"]["x-zhimeng.provider-preference"]["config"] == preference
        assert client.get(f"/api/v1/agent/sessions/{session_id}/messages").json()["messages"] == first_messages
        finish_run("第二轮：确认文件任务。", "settings-refresh-second")

        assert len(requests) == 2
        before, after = requests
        assert before["headers"]["x-old"] == "synthetic-value" and "x-old" not in after["headers"]
        assert before["body"]["model"] == after["body"]["model"] == "chosen-model"
        assert after["body"]["messages"][:len(before["body"]["messages"])] == before["body"]["messages"]
        assert after["body"].get("tools") == before["body"].get("tools")
        assert client.get("/api/v1/settings/providers").json()["active_provider_id"] == "other"
