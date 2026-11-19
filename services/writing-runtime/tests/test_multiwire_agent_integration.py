"""Exercise real adapters and the Agent loop against a loopback-only relay."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.provider_settings_service import ProviderSettingsService, resolve_model_connection


MODEL = "fixture-model"
KEY = "fixture-model-key"
MARKER = "LOCAL_FILE_PROOF_72419"
ENDPOINTS = {
    "chat": "/chat/completions",
    "responses": "/responses",
    "anthropic_messages": "/messages",
}


def _tool_results(body: dict) -> list[str]:
    results = []
    for message in body.get("messages", []):
        if message.get("role") == "tool":
            assert message["tool_call_id"] == "call_read"
            results.append(message["content"])
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    assert block["tool_use_id"] == "call_read"
                    results.append(str(block["content"]))
    for item in body.get("input", []):
        if item.get("type") == "function_call_output":
            assert item["call_id"] == "call_read"
            results.append(item["output"])
    return results


def _events(wire: str, *, text: str | None = None, path: str = "README.md") -> bytes:
    args = json.dumps({"path": path})
    if wire == "chat":
        delta = {"content": text} if text is not None else {"tool_calls": [{
            "index": 0, "id": "call_read", "type": "function",
            "function": {"name": "read_file", "arguments": args},
        }]}
        events = [
            {"model": MODEL, "choices": [{"delta": delta}]},
            {"choices": [{"delta": {}, "finish_reason": "stop" if text is not None else "tool_calls"}]},
            "[DONE]",
        ]
    elif wire == "responses":
        if text is not None:
            events = [{"type": "response.output_text.delta", "delta": text}]
            output = []
        else:
            item = {"id": "fc_read", "type": "function_call", "call_id": "call_read", "name": "read_file", "arguments": args}
            events = [{"type": "response.output_item.added", "output_index": 0, "item": item}]
            output = [item]
        events.append({"type": "response.completed", "response": {"model": MODEL, "status": "completed", "output": output}})
    else:
        block = {"type": "text", "text": text} if text is not None else {
            "type": "tool_use", "id": "call_read", "name": "read_file", "input": {},
        }
        events = [
            {"type": "message_start", "message": {"id": "msg_read", "model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
        ]
        if text is None:
            events.append({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": args}})
        events.extend([
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn" if text is not None else "tool_use"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ])
    return "".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events).encode()


@contextmanager
def _relay(wire: str, file_path: Path, *, overridden: bool):
    state = {"phase": "probe", "requests": [], "returned_file": False}
    prefix = "/model/v1" if overridden else "/v1"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((state["phase"], self.path, body))
            authorized = (
                self.headers.get("x-api-key") == KEY if self.path.endswith("/messages")
                else self.headers.get("Authorization") == f"Bearer {KEY}"
            )
            routed = not overridden or self.headers.get("X-Model-Route") == "isolated"
            if self.path != prefix + ENDPOINTS[wire] or not authorized or not routed:
                payload = json.dumps({"error": {"message": "fixture connection does not match saved model configuration"}}).encode()
                self.send_response(401 if not authorized or not routed else 404)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            results = _tool_results(body)
            if not body.get("tools"):
                payload = _events(wire, text="The connection can return text.")
            elif results:
                if state["phase"] == "run":
                    state["returned_file"] = MARKER in "\n".join(results)
                    text = MARKER if state["returned_file"] else "Tool result did not contain the file."
                else:
                    text = "The probe tool result was received."
                payload = _events(wire, text=text)
            else:
                payload = _events(wire, path=str(file_path) if state["phase"] == "run" else "README.md")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("wire", list(ENDPOINTS))
@pytest.mark.parametrize("mode", ["service-config", "model-override", "auto-model-override"])
def test_settings_probe_and_real_agent_use_the_same_connection(tmp_path: Path, wire: str, mode: str) -> None:
    overridden = mode != "service-config"
    auto = mode == "auto-model-override"
    library = tmp_path / "library"
    library.mkdir()
    note = library / "readme.txt"
    note.write_text(MARKER, encoding="utf-8")
    settings = RuntimeSettings(
        environment="test", database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,), allowed_origins=(), sse_heartbeat_seconds=0.05,
    )
    with _relay(wire, note, overridden=overridden) as (url, state):
        with TestClient(create_app(settings)) as client:
            profile = {
                "provider_id": "fixture", "name": "Local fixture", "base_url": url + "/v1",
                "api_key": "unused-parent-key" if overridden else KEY,
                "model": MODEL, "selected_models": [MODEL], "wire_format": "auto" if auto else wire,
                "reasoning_effort": "none", "max_retries": 0, "make_active": True,
            }
            if overridden:
                profile["model_overrides"] = {MODEL: {
                    "base_url": url + "/model/v1", "api_key": KEY,
                    **({} if auto else {"wire_format": wire}),
                    "extra_headers": {"X-Model-Route": "isolated"},
                }}
            saved = client.put("/api/v1/settings/providers", json=profile)
            assert saved.status_code == 200, saved.text
            assert state["requests"] == []
            tested = client.post("/api/v1/settings/provider/models/liveness", json={
                "provider_id": "fixture", "base_url": url + "/v1", "models": [MODEL],
                "wire_format": "auto" if auto else wire, "prompt": "Summarize what a local file-reading assistant does.",
            })
            assert tested.status_code == 200, tested.text
            probe = tested.json()["results"][0]
            if auto:
                reloaded = ProviderSettingsService(tmp_path / "provider-settings.json")
                stored, _ = reloaded.resolve_profile("fixture")
                assert stored is not None
                connection = resolve_model_connection(stored, MODEL)
                assert connection.wire_format == wire
                assert connection.base_url == url + "/model/v1"
                assert reloaded.catalog_view()["providers"][0]["verified_routes"][MODEL]["wire_format"] == wire
                # Moving the model to a different account must not inherit this route.
                changed = replace(stored, model_overrides={MODEL: replace(stored.model_overrides[MODEL], api_key="other-account")})
                assert reloaded._verified_routes_view(changed) == {}
                state["phase"] = "retest"
                retest = client.post("/api/v1/settings/provider/models/liveness", json={
                    "provider_id": "fixture", "base_url": url + "/v1", "models": [MODEL],
                    "wire_format": "auto", "prompt": "Explain how a file-reading task completes.",
                })
                assert retest.status_code == 200, retest.text
                assert retest.json()["results"][0]["tools"] == "ok"
                assert len([r for r in state["requests"] if r[0] == "retest"]) == 3
            state["phase"] = "single-test"
            single = client.post("/api/v1/settings/provider/test", json={
                "provider_id": "fixture", "base_url": url + "/v1", "model": MODEL,
                "wire_format": "auto" if auto else wire, "prompt": "Explain the purpose of a read-only tool.",
            })
            assert single.status_code == 200, single.text
            assert single.json()["wire_format"] == wire
            assert len([r for r in state["requests"] if r[0] == "single-test"]) == 1

            state["phase"] = "run"
            session = client.post("/api/v1/agent/sessions", json={"title": "Protocol integration"})
            assert session.status_code == 201, session.text
            bound = client.patch(f"/api/v1/agent/sessions/{session.json()['session_id']}", json={
                "provider_id": "fixture", "model": MODEL, "reasoning_effort": "none", "rebind_provider": True,
            })
            assert bound.status_code == 200, bound.text
            started = client.post(f"/api/v1/agent/sessions/{session.json()['session_id']}/runs", headers={"Idempotency-Key": "multiwire-run"}, json={
                "message": f"Read {note} and report its contents.", "writer_mode": "repair_only",
            })
            assert started.status_code == 202, started.text
            run_id = started.json()["run"]["run_id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
                if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    break
                time.sleep(0.02)
            assert run["status"] == "succeeded", run
            messages = client.get(f"/api/v1/agent/sessions/{session.json()['session_id']}/messages").json()["messages"]
            assert any(m["role"] == "assistant" and m["content"] == MARKER for m in messages)
            activities = client.get(f"/api/v1/agent/runs/{run_id}/activities").json()["activities"]
            results = [a["payload"] for a in activities if a["kind"] == "tool_result"]
            assert len(results) == 1 and results[0]["status"] == "succeeded", results
            assert state["returned_file"] is True
            assert note.read_text(encoding="utf-8") == MARKER
            assert probe["wire_format"] == wire
            assert probe["tools"] == "ok", probe
            assert probe["tool_roundtrip"] == "ok", probe
            assert len([r for r in state["requests"] if r[0] == "probe"]) == len(probe["tried"]) + 2
            assert len([r for r in state["requests"] if r[0] == "run"]) == 2
            if auto:
                cleared = client.request("DELETE", "/api/v1/settings/provider/routes", json={
                    "provider_id": "fixture", "base_url": url + "/v1",
                })
                assert cleared.status_code == 200, cleared.text
                assert cleared.json() == {"cleared": 1}
                assert reloaded.catalog_view()["providers"][0]["verified_routes"] == {}
