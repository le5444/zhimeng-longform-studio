from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from fixtures.mcp_http_fixture import HttpFixture
from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app
from zhimeng_runtime.mcp_tool_adapter import tool_alias


class CsvHttpProvider(ApiProvider):
    def __init__(self, root: Path, in_project: bool):
        self.root = root
        self.in_project = in_project
        self.connection_id = None
        self.step = 0

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        latest = [item for item in messages if item.get("role") == "tool"]
        result = json.loads(latest[-1]["content"]) if latest else {}
        output = result.get("output", result)
        path = "summary.md" if self.in_project else str(self.root / "summary.md")
        if self.step == 0:
            name, arguments = tool_alias(self.connection_id, "read.csv"), {}
        elif self.step == 1:
            rows = json.loads(output["content"][0]["text"])
            totals = {}
            for row in rows:
                totals[row["department"]] = totals.get(row["department"], 0) + int(row["amount"])
            summary = "# 部门汇总\n" + "\n".join(f"{key}：{value}" for key, value in totals.items()) + "\n"
            name, arguments = tool_alias(self.connection_id, "write.summary"), {"text": summary}
        elif self.step == 2:
            name, arguments = "read_file", {"path": path}
        elif self.step == 3:
            assert "设计：30" in latest[-1]["content"]
            name, arguments = "edit_file", {"path": path, "find": "# 部门汇总", "replace": "# 核对汇总"}
        else:
            assert output.get("applied") is True
            yield ProviderTextDelta("已通过网络工具汇总，读回并编辑核对：设计30，工程50。")
            yield ProviderCompleted("stop", None)
            return
        assert name in {tool.name for tool in tools}
        self.step += 1
        yield ProviderToolCall(0, f"http-csv-{self.step}", name, arguments)
        yield ProviderCompleted("tool_calls", None)


@pytest.mark.parametrize("era", ["legacy", "modern"])
@pytest.mark.parametrize("in_project", [False, True])
def test_http_settings_to_agent_csv_write_builtin_read_and_edit(settings, tmp_path, era, in_project):
    settings = settings.model_copy(update={"trusted_project_roots": (tmp_path,)})
    project = tmp_path / "普通表格"
    secret = "synthetic-network-service-secret"
    provider = CsvHttpProvider(project, in_project)
    with HttpFixture(project, era=era, response_style="sse", expected_secret=secret) as peer:
        original_hash = hashlib.sha256((project / "sample.csv").read_bytes()).hexdigest()
        with TestClient(create_app(settings, agent_provider=provider)) as client:
            base = "/api/v1/mcp/servers"
            created = client.post(base, json={"name": "网络表格", "transport": "http", "url": peer.url,
                                             "headers": {"X-Workspace-Secret": secret}})
            assert created.status_code == 201, created.text
            cid = provider.connection_id = created.json()["connection_id"]
            deadline = time.monotonic() + 20
            while True:
                view = client.get(base).json()["servers"][0]
                if view["state"] in {"ready", "error"}:
                    break
                assert time.monotonic() < deadline, view
                time.sleep(.03)
            assert view["state"] == "ready", view
            assert view["tool_count"] == 6
            assert view["protocol_version"] == ("2025-11-25" if era == "legacy" else "2026-07-28")
            assert len(client.get(base + "/" + cid + "/tools").json()["tools"]) == 6
            body = {"message": "读CSV汇总、读回并编辑标题。", "writer_mode": "author_review_change_proposal"}
            if in_project:
                inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
                assert inspected.status_code == 200, inspected.text
                snapshot = inspected.json()["project"]
                session_response = client.post(f"/api/v1/projects/{snapshot['project_id']}/agent/sessions",
                                               json={"title": "普通项目网络工具"})
                assert session_response.status_code == 201, session_response.text
                sid = session_response.json()["session_id"]
                body.update(expected_project_revision=snapshot["revision"], expected_project_fingerprint=snapshot["fingerprint"])
            else:
                sid = session(client)
            accepted = client.post(f"/api/v1/agent/sessions/{sid}/runs", json=body,
                                   headers={"Idempotency-Key": f"http-csv-{era}-{in_project}"})
            assert accepted.status_code == 202, accepted.text
            rid = accepted.json()["run"]["run_id"]
            assert wait_for_success(client, rid)["status"] == "succeeded"
            calls = client.app.state.agent_service.store.list_tool_calls(rid)
            assert len(calls) == 4 and all(call.status == "succeeded" for call in calls)
            assert calls[1].output["source_mcp"] is True
            if in_project:
                assert calls[1].output["result_revision"] > snapshot["revision"]
            assert (project / "summary.md").read_text(encoding="utf-8") == "# 核对汇总\n设计：30\n工程：50\n"
            assert hashlib.sha256((project / "sample.csv").read_bytes()).hexdigest() == original_hash
            assert [item["call"] for item in peer.records if "call" in item] == ["read.csv", "write.summary"]
            assert all(item["secret_received"] for item in peer.records if item.get("http_method"))
            assert secret not in client.get(f"/api/v1/agent/runs/{rid}/activities").text
            assert secret not in client.get(base).text
