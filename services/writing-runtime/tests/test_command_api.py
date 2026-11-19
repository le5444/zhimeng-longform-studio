"""Desktop command controls exercise real processes on isolated runtime stores."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from test_agent_api import ApiProvider, wait_for_success
from test_agent_queue import session
from test_command_session_tools import InteractiveProvider, script_command, start
from zhimeng_runtime.app import create_app
from zhimeng_runtime.command_api import CommandSessionSummary, CommandSessionListResponse, CommandSessionReadResponse
from zhimeng_runtime.command_sessions import CommandSessionError, CommandSessionManager


def manager_of(client):
    return client.portal.call(client.app.state.agent_service.tool_executor._command_session_manager)


def launch(client, sid, tmp_path, source, rid="test-command-run"):
    return manager_of(client).start(owner_session_id=sid, owner_run_id=rid,
        command=script_command(tmp_path, source), cwd=tmp_path, yield_time_ms=100)


def read_until(client, url, predicate):
    deadline = time.monotonic() + 10
    while True:
        response = client.get(url)
        assert response.status_code == 200, response.text
        value = response.json()
        if predicate(value):
            return value
        assert time.monotonic() < deadline, value
        time.sleep(0.02)


def test_commands_api_reuses_tool_manager_and_reads_incremental_utf8(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        base = f"/api/v1/agent/sessions/{sid}/commands"
        assert client.get(base).json() == {"commands": []}
        manager = manager_of(client)
        item = launch(client, sid, tmp_path,
            "import sys\nsys.stdout.buffer.write('准备🙂> '.encode('utf-8')); sys.stdout.flush()\n"
            "input()\nsys.stdout.buffer.write('结束中英文🙂'.encode('utf-8')); sys.stdout.flush()\n"
            "print('diagnostic', file=sys.stderr, flush=True)\nsys.exit(7)\n")
        url = base + "/" + item["session_id"]
        first = read_until(client, url, lambda value: "准备" in value["stdout"]["text"])
        assert first["status"] == "running"
        assert first["stdout"]["text"] == "准备🙂> "
        listed = client.get(base).json()["commands"]
        assert len(listed) == 1 and listed[0]["command_session_id"] == item["session_id"]
        assert listed[0]["run_id"] == "test-command-run"
        assert listed[0]["duration_ms"] >= 0
        assert listed[0]["stdout_bytes"] == first["stdout"]["total_bytes"]
        assert not {"stdout_file", "stderr_file", "owner_session_id", "pid"} & listed[0].keys()
        assert manager_of(client) is manager
        assert manager.read(sid, item["session_id"])["status"] == "running"
        offsets = {name + "_offset": first[name]["next_offset"] for name in ("stdout", "stderr")}
        empty = client.get(url, params=offsets).json()
        assert empty["stdout"]["text"] == empty["stderr"]["text"] == ""
        manager.write(sid, item["session_id"], text="continue\n")
        done = read_until(client, url, lambda value: value["status"] == "exited")
        delta = client.get(url, params=offsets).json()
        assert done["exit_code"] == 7 and done["ended_at"] is not None
        assert delta["stdout"]["text"] == "结束中英文🙂"
        assert "diagnostic" in delta["stderr"]["text"]
        assert client.post(url + "/stop").json()["status"] == "exited"
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200 and "CommandSessionReadResponse" in openapi.text


def test_stop_command_keeps_owning_run_and_other_command_alive(settings, tmp_path):
    provider = InteractiveProvider(script_command(tmp_path,
        "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n"), hold_after_start=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        rid = start(client, sid)
        assert provider.started.wait(10)
        manager = manager_of(client)
        other = launch(client, sid, tmp_path, "print('other', flush=True)\ninput()\n", rid=rid)
        url = f"/api/v1/agent/sessions/{sid}/commands/{provider.session_id}"
        stopped = client.post(url + "/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "cancelled"
        assert manager.read(sid, other["session_id"])["status"] == "running"
        active = client.get(f"/api/v1/agent/runs/{rid}").json()["run"]
        assert active["status"] == "running"
        provider.hold_after_start = False
        assert wait_for_success(client, rid)["status"] == "succeeded"
        assert client.get(url).json()["stdout"]["text"].strip() == "Ready"
        assert client.post(url + "/stop").json()["status"] == "cancelled"


def test_session_scope_and_invalid_cursors_are_enforced(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        owner, other = session(client), session(client)
        item = launch(client, owner, tmp_path,
            "import sys\nsys.stdout.buffer.write('中文🙂'.encode('utf-8')); sys.stdout.flush()\ninput()\n")
        url = f"/api/v1/agent/sessions/{owner}/commands/{item['session_id']}"
        read_until(client, url, lambda value: value["stdout"]["text"] == "中文🙂")
        for sid in (other, "missing-session"):
            base = f"/api/v1/agent/sessions/{sid}/commands"
            listed = client.get(base)
            assert listed.status_code == (200 if sid == other else 404)
            if sid == other:
                assert listed.json()["commands"] == []
            assert client.get(base + "/" + item["session_id"]).status_code == 404
            assert client.post(base + "/" + item["session_id"] + "/stop").status_code == 404
        for params in ({"stdout_offset": -1}, {"stdout_offset": 1}, {"stdout_offset": 100000},
                       {"stderr_offset": 1}, {"stdout_before": 1}, {"stdout_before": 0, "stdout_offset": 0},
                       {"max_bytes": 0}, {"max_bytes": 1048577}):
            assert client.get(url, params=params).status_code == 422, params
        assert client.get(url).json()["status"] == "running"
        assert client.post(f"/api/v1/agent/sessions/{owner}/commands", json={"command": "anything"}).status_code == 405


def test_unavailable_command_executor_returns_clear_retryable_503(settings, monkeypatch):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        service = client.app.state.agent_service
        def failed_start():
            raise OSError("private internal path")
        for executor in (None, object(), type("BrokenExecutor", (), {"_command_session_manager": staticmethod(failed_start)})()):
            with monkeypatch.context() as scoped:
                scoped.setattr(service, "tool_executor", executor)
                for method, suffix in (("get", ""), ("get", "/cmd_fake"), ("post", "/cmd_fake/stop")):
                    response = getattr(client, method)(f"/api/v1/agent/sessions/{sid}/commands{suffix}")
                    assert response.status_code == 503, response.text
                    assert "command_service_unavailable" in response.text
                    assert "private internal path" not in response.text


def test_large_output_tail_and_both_paging_directions_recover_full_utf8(settings, tmp_path):
    original = "资料🙂abc\n" * 17000
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        item = launch(client, sid, tmp_path,
            "import sys\nsys.stdout.buffer.write(('资料🙂abc\\n' * 17000).encode('utf-8')); sys.stdout.flush()\n")
        url = f"/api/v1/agent/sessions/{sid}/commands/{item['session_id']}"
        tail = read_until(client, url, lambda value: value["status"] == "exited")["stdout"]
        assert tail["truncated_before"] and not tail["has_more"]
        assert 65532 <= len(tail["text"].encode("utf-8")) <= 65536
        assert tail["total_bytes"] == len(original.encode("utf-8"))
        chunks, offset = [], 0
        while True:
            page = client.get(url, params={"stdout_offset": offset, "stderr_offset": 0, "max_bytes": 4096}).json()["stdout"]
            assert page["start_offset"] == offset
            assert page["next_offset"] - offset == len(page["text"].encode("utf-8"))
            assert 0 < len(page["text"].encode("utf-8")) <= 4096
            chunks.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        assert "".join(chunks) == original
        chunks, before = [tail["text"]], tail["start_offset"]
        while before:
            page = client.get(url, params={"stdout_before": before, "stderr_before": 0, "max_bytes": 4096}).json()["stdout"]
            assert page["next_offset"] == before
            assert page["has_more"]
            chunks.insert(0, page["text"])
            before = page["start_offset"]
        assert "".join(chunks) == original


def test_stop_racing_blocked_model_input_and_ui_read_terminates_cleanly(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        item = launch(client, sid, tmp_path, "import time\nprint('Ready', flush=True)\ntime.sleep(300)\n")
        cid = item["session_id"]
        url = f"/api/v1/agent/sessions/{sid}/commands/{cid}"
        read_until(client, url, lambda value: "Ready" in value["stdout"]["text"])
        manager = manager_of(client)
        started = threading.Event()
        def blocked_input():
            started.set()
            try:
                return manager.write(sid, cid, text="x" * (2 * 1024 * 1024))
            except CommandSessionError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(blocked_input)
            assert started.wait(2)
            reading = pool.submit(client.get, url)
            stopped = client.post(url + "/stop")
            assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
            assert reading.result(timeout=5).status_code == 200
            outcome = pending.result(timeout=5)
            assert outcome in ("command_session_stdin_closed", "command_session_not_running") if isinstance(outcome, str) else outcome["status"] == "cancelled"


def test_restored_interrupted_and_exited_commands_remain_readable(settings, tmp_path):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        item = launch(client, sid, tmp_path, "print('saved output', flush=True)\n")
        url = f"/api/v1/agent/sessions/{sid}/commands/{item['session_id']}"
        read_until(client, url, lambda value: value["status"] == "exited")
    folder = settings.database_path.parent / "command-sessions" / "cmd_orphan"
    folder.mkdir()
    (folder / "stdout.log").write_bytes("重启前的输出🙂".encode("utf-8"))
    (folder / "stderr.log").write_bytes(b"")
    (folder / "session.json").write_bytes(json.dumps({"schema": "zhimeng.command-session.v1",
        "session_id": "cmd_orphan", "owner_session_id": sid, "owner_run_id": "old-run",
        "status": "running", "exit_code": None, "command": "old", "cwd": str(tmp_path),
        "created_at": time.time() - 10, "ended_at": None}).encode("utf-8"))
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        assert restarted.get(url).json()["stdout"]["text"].strip() == "saved output"
        stale = f"/api/v1/agent/sessions/{sid}/commands/cmd_orphan"
        first = restarted.get(stale).json()
        assert first["status"] == "interrupted"
        assert first["stdout"]["text"] == "重启前的输出🙂"
        assert first["interruption_reason"]
        assert restarted.get(stale).json()["duration_ms"] == first["duration_ms"]
        assert restarted.post(stale + "/stop").json()["status"] == "interrupted"


@pytest.mark.parametrize("page_size", range(4, 12))
def test_every_small_utf8_page_boundary_preserves_all_characters(tmp_path, page_size):
    text = "中🙂a文é\n" * 5
    target = tmp_path / "output.log"
    target.write_bytes(text.encode("utf-8"))
    forward, offset = [], 0
    while True:
        page = CommandSessionManager._log_page(target, offset=offset, before=None, max_bytes=page_size)
        assert 0 < len(page["text"].encode("utf-8")) <= page_size
        forward.append(page["text"])
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    assert "".join(forward) == text
    backward, before = [], len(text.encode("utf-8"))
    while before:
        page = CommandSessionManager._log_page(target, offset=None, before=before, max_bytes=page_size)
        assert 0 < len(page["text"].encode("utf-8")) <= page_size
        backward.insert(0, page["text"])
        before = page["start_offset"]
    assert "".join(backward) == text


@pytest.mark.parametrize(("filename", "model"), [
    ("command-session-summary.schema.json", CommandSessionSummary),
    ("command-session-list.schema.json", CommandSessionListResponse),
    ("command-session-read.schema.json", CommandSessionReadResponse),
])
def test_exported_command_contracts_match_global_models(filename, model):
    root = Path(__file__).resolve().parents[3]
    actual = json.loads((root / "packages" / "contracts" / filename).read_bytes())
    Draft202012Validator.check_schema(actual)
    expected = model.model_json_schema(by_alias=True)
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    expected["$id"] = f"https://zhimeng.local/contracts/{filename}"
    assert actual == expected
