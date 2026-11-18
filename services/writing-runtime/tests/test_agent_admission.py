from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_models import AgentRunCreateResponse, AgentRunAdmissionWithdrawResponse
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.agent_service import AgentUnavailableError
from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore

from test_agent_api import ApiProvider, wait_for_success


HEADERS = {"Idempotency-Key": "ordinary-admission-0001"}
PAYLOAD = {"message": "检查这份说明的三个重点"}


def session(client: TestClient) -> str:
    response = client.post("/api/v1/agent/sessions", json={"title": "说明检查"})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def runs_url(sid: str) -> str:
    return f"/api/v1/agent/sessions/{sid}/runs"


def lookup(client: TestClient, sid: str):
    return client.get(f"{runs_url(sid)}/admission", headers=HEADERS)


def withdraw(client: TestClient, sid: str):
    return client.post(f"{runs_url(sid)}/admission/withdraw", headers=HEADERS)


def assert_single_input(client: TestClient, sid: str, run_id: str) -> None:
    runs = client.get(runs_url(sid)).json()["runs"]
    assert [run["run_id"] for run in runs] == [run_id]
    messages = client.get(f"/api/v1/agent/sessions/{sid}/messages").json()["messages"]
    assert len([message for message in messages if message["role"] == "user"]) == 1


class HeldProvider(ApiProvider):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.calls += 1
        self.started.set()
        while not self.release.is_set():
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
        yield ProviderTextDelta("三个重点已整理。")
        yield ProviderCompleted("stop", None)


def test_admission_lookup_is_read_only_and_validates_session_and_key(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        missing = lookup(client, sid)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "agent_admission_not_found"
        assert missing.headers["Cache-Control"] == "no-store"
        assert missing.headers["Vary"] == "Idempotency-Key"
        assert client.get(runs_url(sid)).json()["runs"] == []
        assert lookup(client, "session-missing").json()["error"]["code"] == "agent_resource_not_found"
        assert client.get(f"{runs_url(sid)}/admission").status_code == 422
        assert client.get(f"{runs_url(sid)}/admission", headers={"Idempotency-Key": "short"}).status_code == 422
        operation = client.get("/openapi.json").json()["paths"][f"/api/v1/agent/sessions/{{session_id}}/runs/admission"]["get"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("AgentRunCreateResponse")


def test_lost_ack_can_recover_running_admission_without_starting_again(settings, monkeypatch):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider), raise_server_exceptions=False) as client:
        sid = session(client)
        service = client.app.state.agent_service
        original_start = service.start_run

        async def lose_ack(**kwargs):
            await original_start(**kwargs)
            raise ConnectionError("POST reply was lost after the transaction committed")

        monkeypatch.setattr(service, "start_run", lose_ack)
        try:
            assert client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD).status_code == 500
            assert provider.started.wait(timeout=10)
            receipt = lookup(client, sid)
            assert receipt.status_code == 200, receipt.text
            body = receipt.json()
            AgentRunCreateResponse.model_validate(body)
            assert body["replayed"] is True
            assert body["run"]["status"] == "running"
            assert body["user_message"]["content"] == PAYLOAD["message"]
            assert receipt.headers["Cache-Control"] == "no-store"
            assert receipt.headers["Vary"] == "Idempotency-Key"
            monkeypatch.setattr(service, "start_run", original_start)
            replay = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD)
            assert replay.status_code == 202, replay.text
            assert replay.json()["run"]["run_id"] == body["run"]["run_id"]
            assert_single_input(client, sid, body["run"]["run_id"])
            assert provider.calls == 1
        finally:
            provider.release.set()


@pytest.mark.parametrize("changed", [
    {"message": "改成另一份说明"},
    {"manual_skill_names": ["表格整理"]},
    {"open_file_path": "another.md"},
    {"writer_mode": "author_review_change_proposal"},
    {"manual_file_ids": ["file_other_000001"]},
])
def test_same_key_rejects_changed_input_even_if_provider_became_unavailable(settings, monkeypatch, changed):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        payload = {**PAYLOAD, "manual_skill_names": ["说明整理"], "open_file_path": "README.md"}
        accepted = client.post(runs_url(sid), headers=HEADERS, json=payload)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run"]["run_id"]
        assert wait_for_success(client, run_id)["status"] == "succeeded"
        service = client.app.state.agent_service

        def unavailable(_):
            raise AgentUnavailableError("模型配置已移除")

        monkeypatch.setattr(service, "_require_provider_for_session", unavailable)
        same = client.post(runs_url(sid), headers=HEADERS, json=payload)
        assert same.status_code == 202, same.text
        assert same.json()["replayed"] is True
        assert same.json()["run"]["run_id"] == run_id
        assert lookup(client, sid).json()["run"]["run_id"] == run_id
        different = client.post(runs_url(sid), headers=HEADERS, json={**payload, **changed})
        assert different.status_code == 409, different.text
        assert "different request" in different.json()["error"]["message"]
        assert_single_input(client, sid, run_id)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "interrupted"])
def test_terminal_receipt_survives_restart_and_replay_does_not_execute(settings, monkeypatch, terminal):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        accepted = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD).json()
        run_id = accepted["run"]["run_id"]
        assert wait_for_success(client, run_id)["status"] == "succeeded"
        with client.app.state.agent_service.store.database.connection() as connection:
            error_json = json.dumps({"schema": "zhimeng.error.v1", "error": {
                "code": "test_terminal_failure", "message": "Fixture terminal failure",
                "request_id": "fixture-request", "retryable": False,
            }}) if terminal == "failed" else None
            connection.execute("UPDATE run SET status = ?, error_json = ? WHERE id = ?", (terminal, error_json, run_id))
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        service = client.app.state.agent_service

        def unexpected_launch(*args, **kwargs):
            raise AssertionError("terminal replay launched another execution")

        monkeypatch.setattr(service, "_launch", unexpected_launch)
        receipt = lookup(client, sid)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["run"]["status"] == terminal
        replay = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD)
        assert replay.status_code == 202, replay.text
        assert replay.json()["run"]["run_id"] == run_id
        assert replay.json()["run"]["status"] == terminal
        assert_single_input(client, sid, run_id)
        assert provider.calls == 0


def test_admission_is_scoped_to_session_and_keeps_queue_unchanged(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        first, second = session(client), session(client)
        queued = client.post(f"/api/v1/agent/sessions/{first}/queue", json={"item_id": "queued-0001", "message": "稍后检查"}).json()
        accepted = client.post(runs_url(first), headers=HEADERS, json=PAYLOAD).json()
        assert lookup(client, second).status_code == 404
        second_response = client.post(runs_url(second), headers=HEADERS, json={"message": "另一个会话的输入"})
        assert second_response.status_code == 202, second_response.text
        assert second_response.json()["run"]["run_id"] != accepted["run"]["run_id"]
        assert lookup(client, first).json()["user_message"]["content"] == PAYLOAD["message"]
        assert lookup(client, second).json()["user_message"]["content"] == "另一个会话的输入"
        current_queue = client.get(f"/api/v1/agent/sessions/{first}/queue").json()
        assert current_queue["items"] == queued["items"]
        assert current_queue["revision"] == queued["revision"]
        assert current_queue["pause_reason"] == queued["pause_reason"]


def test_admission_keeps_original_user_message_after_same_run_steering(settings):
    from test_agent_steering import BoundaryProvider

    provider = BoundaryProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        first = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD).json()
        run_id = first["run"]["run_id"]
        try:
            assert provider.entered.wait(timeout=10)
            queued = client.post(f"/api/v1/agent/sessions/{sid}/queue", json={
                "item_id": "correction-0001", "message": "使用表格整理",
            })
            assert queued.status_code == 200, queued.text
            steered = client.post(f"/api/v1/agent/sessions/{sid}/queue/correction-0001/steer", json={"expected_run_id": run_id})
            assert steered.status_code == 200, steered.text
            provider.release.set()
            assert wait_for_success(client, run_id)["status"] == "succeeded"
            assert lookup(client, sid).json()["user_message"] == first["user_message"]
            replay = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD)
            assert replay.status_code == 202, replay.text
            assert replay.json()["user_message"] == first["user_message"]
        finally:
            provider.release.set()


def test_lookup_before_commit_then_concurrent_retry_recovers_exactly_one_run(settings, monkeypatch):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        service = client.app.state.agent_service
        original_create = service.store.create_run
        before_commit = threading.Event()
        allow_commit = threading.Event()

        def delayed_create(**kwargs):
            before_commit.set()
            assert allow_commit.wait(timeout=15)
            return original_create(**kwargs)

        monkeypatch.setattr(service.store, "create_run", delayed_create)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, runs_url(sid), headers=HEADERS, json=PAYLOAD)
            try:
                assert before_commit.wait(timeout=10)
                assert lookup(client, sid).status_code == 404
                retry = pool.submit(client.post, runs_url(sid), headers=HEADERS, json=PAYLOAD)
                allow_commit.set()
                replies = [first.result(timeout=20), retry.result(timeout=20)]
                assert [reply.status_code for reply in replies] == [202, 202]
                assert sorted(reply.json()["replayed"] for reply in replies) == [False, True]
                run_ids = [reply.json()["run"]["run_id"] for reply in replies]
                assert run_ids[0] == run_ids[1]
                assert lookup(client, sid).json()["run"]["run_id"] == run_ids[0]
                assert provider.started.wait(timeout=10)
                assert provider.calls == 1
                assert_single_input(client, sid, run_ids[0])
            finally:
                allow_commit.set()
                provider.release.set()


def test_lookup_can_see_committed_input_before_post_ack(settings, monkeypatch):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        service = client.app.state.agent_service
        original_start = service.start_run
        admitted = threading.Event()
        release_ack = threading.Event()

        async def delayed_ack(**kwargs):
            result = await original_start(**kwargs)
            admitted.set()
            while not release_ack.is_set():
                await asyncio.sleep(0.01)
            return result

        monkeypatch.setattr(service, "start_run", delayed_ack)
        with ThreadPoolExecutor(max_workers=1) as pool:
            post = pool.submit(client.post, runs_url(sid), headers=HEADERS, json=PAYLOAD)
            try:
                assert admitted.wait(timeout=10)
                receipt = lookup(client, sid)
                assert receipt.status_code == 200, receipt.text
                assert post.done() is False
                retraction = withdraw(client, sid)
                assert retraction.status_code == 200, retraction.text
                assert retraction.json()["state"] == "accepted"
                assert retraction.json()["accepted"]["run"]["run_id"] == receipt.json()["run"]["run_id"]
                release_ack.set()
                reply = post.result(timeout=10)
                assert reply.json()["run"]["run_id"] == receipt.json()["run"]["run_id"]
                assert_single_input(client, sid, receipt.json()["run"]["run_id"])
            finally:
                release_ack.set()
                provider.release.set()


def test_withdrawn_identity_is_durable_and_allows_fresh_send_without_affecting_other_session(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid, other = session(client), session(client)
        assert withdraw(client, sid).json() == {"state": "withdrawn", "accepted": None}
        repeated = withdraw(client, sid)
        assert repeated.status_code == 200
        assert repeated.json()["state"] == "withdrawn"
        assert repeated.headers["Cache-Control"] == "no-store"
        assert lookup(client, sid).status_code == 410
        assert lookup(client, sid).json()["error"]["code"] == "agent_admission_withdrawn"
        assert lookup(client, sid).headers["Cache-Control"] == "no-store"
        assert client.get(runs_url(sid)).json()["runs"] == []
        assert lookup(client, other).status_code == 404
        assert withdraw(client, "session-does-not-exist").status_code == 404
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        assert lookup(client, sid).status_code == 410
        old = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD)
        assert old.status_code == 409, old.text
        assert old.json()["error"]["code"] == "agent_admission_withdrawn"
        assert old.json()["error"]["retryable"] is False
        edited = client.post(runs_url(sid), headers={"Idempotency-Key": "edited-admission-0002"}, json={"message": "重新编辑后的要求"})
        assert edited.status_code == 202, edited.text
        assert client.post(runs_url(other), headers=HEADERS, json=PAYLOAD).status_code == 202
        assert len(client.get(runs_url(sid)).json()["runs"]) == 1


def test_withdraw_after_admission_returns_receipt_without_cancelling_run(settings):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        first = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD).json()
        try:
            assert provider.started.wait(timeout=10)
            response = withdraw(client, sid)
            assert response.status_code == 200, response.text
            receipt = AgentRunAdmissionWithdrawResponse.model_validate(response.json())
            assert receipt.state == "accepted"
            assert receipt.accepted.run.run_id == first["run"]["run_id"]
            assert receipt.accepted.user_message.message_id == first["user_message"]["message_id"]
            assert receipt.accepted.run.status.value == "running"
            assert withdraw(client, sid).json()["state"] == "accepted"
            assert lookup(client, sid).status_code == 200
            assert provider.calls == 1
        finally:
            provider.release.set()
        assert wait_for_success(client, first["run"]["run_id"])["status"] == "succeeded"
        assert withdraw(client, sid).json()["accepted"]["run"]["status"] == "succeeded"


def test_withdraw_wins_before_delayed_post_commits(settings, monkeypatch):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        store = client.app.state.agent_service.store
        original_create = store.create_run
        entered, release = threading.Event(), threading.Event()

        def delayed(**kwargs):
            entered.set()
            assert release.wait(timeout=15)
            return original_create(**kwargs)

        monkeypatch.setattr(store, "create_run", delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            post = pool.submit(client.post, runs_url(sid), headers=HEADERS, json=PAYLOAD)
            try:
                assert entered.wait(timeout=10)
                assert lookup(client, sid).status_code == 404
                assert withdraw(client, sid).json()["state"] == "withdrawn"
                release.set()
                late = post.result(timeout=15)
                assert late.status_code == 409, late.text
                assert late.json()["error"]["code"] == "agent_admission_withdrawn"
                assert lookup(client, sid).status_code == 410
                assert provider.calls == 0
                assert client.get(runs_url(sid)).json()["runs"] == []
            finally:
                release.set()
                provider.release.set()


def test_admission_and_withdrawal_transactions_have_one_winner(settings, monkeypatch):
    provider = HeldProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        store = client.app.state.agent_service.store
        original_create, original_withdraw = store.create_run, store.withdraw_run_admission
        barrier = threading.Barrier(2)

        def create_at_barrier(**kwargs):
            barrier.wait(timeout=15)
            return original_create(**kwargs)

        def withdraw_at_barrier(*args):
            barrier.wait(timeout=15)
            return original_withdraw(*args)

        monkeypatch.setattr(store, "create_run", create_at_barrier)
        monkeypatch.setattr(store, "withdraw_run_admission", withdraw_at_barrier)
        with ThreadPoolExecutor(max_workers=2) as pool:
            post = pool.submit(client.post, runs_url(sid), headers=HEADERS, json=PAYLOAD)
            retraction = pool.submit(withdraw, client, sid)
            try:
                accepted, removed = post.result(timeout=25), retraction.result(timeout=25)
                assert removed.status_code == 200, removed.text
                if removed.json()["state"] == "withdrawn":
                    assert accepted.status_code == 409, accepted.text
                    assert lookup(client, sid).status_code == 410
                    expected_runs = 0
                else:
                    assert accepted.status_code == 202, accepted.text
                    assert accepted.json()["run"]["run_id"] == removed.json()["accepted"]["run"]["run_id"]
                    assert lookup(client, sid).status_code == 200
                    expected_runs = 1
                with store.database.connection() as connection:
                    runs = connection.execute("SELECT count(*) FROM run WHERE session_id = ?", (sid,)).fetchone()[0]
                    tombstones = connection.execute("SELECT count(*) FROM session_admission_withdrawal WHERE session_id = ?", (sid,)).fetchone()[0]
                    assert runs == expected_runs
                    assert runs + tombstones == 1
            finally:
                provider.release.set()


def test_schema_28_migrates_to_admission_withdrawal_without_losing_history(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = session(client)
        first = client.post(runs_url(sid), headers=HEADERS, json=PAYLOAD).json()
        assert wait_for_success(client, first["run"]["run_id"])["status"] == "succeeded"
    database = SqliteStore(settings.database_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE session_admission_withdrawal")
        connection.execute("PRAGMA user_version = 28")
    health = database.initialize()
    assert health.ready
    assert health.schema_version == SCHEMA_VERSION
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        receipt = lookup(client, sid)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["user_message"] == first["user_message"]
        assert withdraw(client, sid).json()["state"] == "accepted"
        assert withdraw(client, session(client)).json()["state"] == "withdrawn"


def test_admission_withdrawal_schema_matches_model():
    schema = json.loads((Path(__file__).resolve().parents[3] / "packages/contracts/agent-run-admission-withdraw.schema.json").read_text(encoding="utf-8"))
    schema.pop("$schema")
    schema.pop("$id")
    assert schema == AgentRunAdmissionWithdrawResponse.model_json_schema(by_alias=True)
