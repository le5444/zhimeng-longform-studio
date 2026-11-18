from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, AgentProviderError
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStoreConflict
from zhimeng_runtime.context_models import ContextAuthority, ContextCreateRequest, ContextPack
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.execution_protocol import ActivityKind, RunStatus
from test_agent_service import TurnScriptedProvider
from test_context_api import harness, _complete_turn, _executor
from test_session_checkpoint_wiring import full_summary


def more_turns(h, count=3):
    for n in range(count):
        _complete_turn(h.agent_store, h.session_id, key=f"extra-turn-{n}",
                       message=f"keep request {n}", response=f"keep answer {n}",
                       revision=h.revision, fingerprint=h.fingerprint)


def app_for(service):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_agent_router(service, heartbeat_seconds=0.1))
    return app


def pack_for(h, *, session_id=None, key="context-after-compact", profile="chat"):
    return asyncio.run(h.service.create_pack(
        session_id or h.session_id,
        ContextCreateRequest.model_validate(h.request(profile=profile)),
        idempotency_key=key,
    )).pack


def test_manual_compact_api_persists_and_project_pack_uses_reference_summary(harness):
    h = harness
    more_turns(h)
    before = pack_for(h, key="before")
    before_json = before.model_dump_json(by_alias=True)
    provider = TurnScriptedProvider([[ProviderTextDelta(full_summary("Earlier decisions are preserved.")), ProviderCompleted("stop", None)]])
    service = AgentService(h.agent_store, provider)
    original = h.agent_store.list_messages(h.session_id)
    with TestClient(app_for(service)) as client:
        result = client.post(f"/api/v1/agent/sessions/{h.session_id}/compact")
        assert result.status_code == 200, result.text
        cp = result.json()
        assert cp["through_ordinal"] == 7
        assert client.get(f"/api/v1/agent/sessions/{h.session_id}/checkpoint").json() == cp
    assert h.agent_store.list_messages(h.session_id) == original
    pack = pack_for(h)
    summary = next(x for x in pack.included if x.source_type == "session_summary")
    assert summary.authority is ContextAuthority.REFERENCE
    assert "作者的原话优先于摘要" in summary.content
    assert cp["content"] in pack.exact_provider_messages[0].content
    assert [x.content for x in pack.exact_provider_messages[1:-1]] == ["keep request 2", "keep answer 2"]
    assert ContextPack.model_validate_json(pack.model_dump_json(by_alias=True)) == pack
    assert ContextPack.model_validate_json(before_json) == before
    # Profiles which explicitly omit history do not acquire a summary secretly.
    isolated = pack_for(h, key="no-history", profile="chapter")
    assert not any(x.source_type == "session_summary" for x in isolated.included)


def test_compact_failure_is_visible_and_keeps_previous_history(harness):
    h = harness
    more_turns(h, 1)
    provider = TurnScriptedProvider([[ProviderTextDelta("partial only")]])
    service = AgentService(h.agent_store, provider)
    with TestClient(app_for(service)) as client:
        response = client.post(f"/api/v1/agent/sessions/{h.session_id}/compact")
        assert response.status_code == 503, response.text
    assert h.agent_store.latest_session_checkpoint(h.session_id) is None
    assert len(h.agent_store.completed_history(h.session_id)[1]) == 4


def test_compact_rejects_active_run_and_preserves_recent_turn(harness):
    h = harness
    service = AgentService(h.agent_store, TurnScriptedProvider([]))
    with TestClient(app_for(service)) as client:
        assert client.post(f"/api/v1/agent/sessions/{h.session_id}/compact").status_code == 409
        h.agent_store.create_run(session_id=h.session_id, message="currently working",
                                 expected_project_revision=h.revision, expected_project_fingerprint=h.fingerprint,
                                 active_chapter_id=None, idempotency_key="active-run-busy")
        response = client.post(f"/api/v1/agent/sessions/{h.session_id}/compact")
        assert response.status_code == 409, response.text


def test_compaction_claim_blocks_duplicate_compaction_and_new_run(harness):
    h = harness
    more_turns(h, 1)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class BlockingSummary(TurnScriptedProvider):
            @property
            def identity(self):
                # 与会话创建时的执行器同身份（legacy 升级：补上指纹 instance_id），
                # 排队后的开工才不会被身份核对拦下。
                return _executor().model_copy(
                    update={
                        "instance_id": f"provider-config-{self.dispatch_config_fingerprint}"
                    }
                )

            async def stream_chat(self, *args, **kwargs):
                started.set()
                await release.wait()
                yield ProviderTextDelta(full_summary("saved summary"))
                yield ProviderCompleted("stop", None)
        service = AgentService(h.agent_store, BlockingSummary([]))
        task = asyncio.create_task(service.compact_session(h.session_id))
        await started.wait()
        with pytest.raises(AgentStoreConflict):
            await service.compact_session(h.session_id)
        # 整理期间发消息不再 409：排队等整理完，自动开工。
        queued = asyncio.create_task(
            service.start_run(session_id=h.session_id, message="next",
                              expected_project_revision=h.revision, expected_project_fingerprint=h.fingerprint,
                              active_chapter_id=None, idempotency_key="while-compacting")
        )
        await asyncio.sleep(0.05)
        assert not queued.done()  # 还在排队等整理
        release.set()
        assert (await task).content.startswith(full_summary("saved summary"))
        started_result = await asyncio.wait_for(queued, timeout=5)
        assert started_result.run.run_id
        assert not service._compacting_sessions
        await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("target_role", ["user", "assistant"])
def test_fork_api_copies_only_selected_transcript_and_can_build_next_pack(harness, target_role):
    h = harness
    more_turns(h)
    original = h.agent_store.list_messages(h.session_id)
    target = original[5 if target_role == "user" else 6]
    assert target.role == target_role
    service = AgentService(h.agent_store, TurnScriptedProvider([]))
    with TestClient(app_for(service)) as client:
        response = client.post(f"/api/v1/agent/sessions/{h.session_id}/fork",
                               json={"up_to_message_id": target.message_id})
        assert response.status_code == 201, response.text
        branch_id = response.json()["session_id"]
        assert client.get(f"/api/v1/agent/sessions/{branch_id}/runs").json()["runs"] == []
    inherited = h.agent_store.list_messages(branch_id)
    assert [x.content for x in inherited] == [x.content for x in original if x.ordinal <= target.ordinal]
    assert h.agent_store.list_messages(h.session_id) == original
    assert set(x.message_id for x in inherited).isdisjoint(x.message_id for x in original)
    for run_id in set(x.run_id for x in inherited):
        run = h.agent_store.get_run(run_id)
        assert run.status is RunStatus.INTERRUPTED
        activities = h.agent_store.list_activities(run_id)
        assert all(x.root.kind is ActivityKind.MESSAGE and x.root.session_id == branch_id and x.root.run_id == run_id for x in activities)
        assert set(x.root.payload.message_id for x in activities) == set(x.message_id for x in inherited if x.run_id == run_id)
    with h.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_call WHERE run_id IN (SELECT id FROM run WHERE session_id = ?)", (branch_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM run WHERE session_id = ? AND context_pack_id IS NOT NULL", (branch_id,)).fetchone()[0] == 0
    pack = pack_for(h, session_id=branch_id, key="branch-next")
    serialized = pack.model_dump_json(by_alias=True)
    assert "keep request 2" not in serialized
    if target_role == "user":
        assert "keep answer 1" not in serialized
        assert any(x.source_type == "session_fork_author_message" and target.content in x.content for x in pack.included)
    else:
        assert target.content in [x.content for x in pack.exact_provider_messages]
    assert ContextPack.model_validate_json(serialized) == pack


def test_fork_names_the_branch_after_the_current_title_once(harness):
    # 2026-09-26 真机：第一轮答完后台把对话改名了，前端还拿着旧名「随便聊聊」去起分支名。
    # 现在前端不传名字，后端用源对话此刻的名字；分支再分叉不叠成两个「（分支）」。
    h = harness
    more_turns(h)
    target = h.agent_store.list_messages(h.session_id)[-1]
    h.agent_store.update_session(h.session_id, title="沈青云借法入门七人")
    service = AgentService(h.agent_store, TurnScriptedProvider([]))
    with TestClient(app_for(service)) as client:
        first = client.post(f"/api/v1/agent/sessions/{h.session_id}/fork",
                            json={"up_to_message_id": target.message_id})
        assert first.status_code == 201, first.text
        assert first.json()["title"] == "沈青云借法入门七人（分支）"
        branch_id = first.json()["session_id"]
        branch_target = h.agent_store.list_messages(branch_id)[-1]
        second = client.post(f"/api/v1/agent/sessions/{branch_id}/fork",
                             json={"up_to_message_id": branch_target.message_id})
        assert second.status_code == 201, second.text
        assert second.json()["title"] == "沈青云借法入门七人（分支）"


def test_fork_does_not_copy_a_summary_from_after_branch_point(harness):
    h = harness
    more_turns(h)
    msgs = h.agent_store.list_messages(h.session_id)
    h.agent_store.append_session_checkpoint(h.session_id, run_id=None, through_ordinal=7,
                                            source_message_count=7, content="future secret")
    early = h.agent_store.fork_session(h.session_id, up_to_message_id=msgs[1].message_id)
    assert h.agent_store.latest_session_checkpoint(early.session_id) is None
    later = h.agent_store.fork_session(h.session_id, up_to_message_id=msgs[-1].message_id)
    assert h.agent_store.latest_session_checkpoint(later.session_id).content == "future secret"


def test_checkpoint_compare_and_swap_rejects_stale_summary(harness):
    h = harness
    h.agent_store.append_session_checkpoint(h.session_id, run_id=None, through_ordinal=2,
                                            source_message_count=2, content="first",
                                            expected_previous_through_ordinal=0)
    with pytest.raises(AgentStoreConflict):
        h.agent_store.append_session_checkpoint(h.session_id, run_id=None, through_ordinal=4,
                                                source_message_count=2, content="stale",
                                                expected_previous_through_ordinal=0)
    assert h.agent_store.latest_session_checkpoint(h.session_id).content == "first"


@pytest.mark.parametrize("wire,effective,budget,expected", [
    ("chat", "high", None, "调整为 high"),
    ("responses", None, None, "按服务默认处理"),
    ("anthropic_messages", None, 1024, "1024 tokens"),
])
def test_request_parameter_notice_visible_without_reasoning_text(tmp_path, wire, effective, budget, expected):
    from test_agent_service import build_service, wait_terminal, PROJECT_ID, FINGERPRINT
    async def scenario():
        provider = TurnScriptedProvider([[ProviderTextDelta("answer"), ProviderCompleted(
            "stop", None, effective_reasoning_effort=effective, effective_thinking_budget_tokens=budget)]])
        provider.wire_format = wire
        provider.reasoning_effort = "max"
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        result = await service.start_run(session_id=session.session_id, message="hello",
                                         expected_project_revision=1, expected_project_fingerprint=FINGERPRINT,
                                         active_chapter_id=None, idempotency_key="notice-no-reasoning")
        assert (await wait_terminal(service, result.run.run_id)).status is RunStatus.SUCCEEDED
        activities = service.store.list_activities(result.run.run_id)
        notices = [x.root for x in activities if x.root.kind is ActivityKind.REASONING]
        assert any(expected in item.payload.summary for item in notices)
        assert "本次" not in service.store.list_messages(session.session_id)[-1].content
        await service.shutdown()
    asyncio.run(scenario())


def test_session_history_schemas_match_models():
    from pathlib import Path
    from zhimeng_runtime.agent_models import AgentSessionForkRequest, SessionCheckpointRecord
    root = Path(__file__).resolve().parents[3]
    for name, model in [("agent-session-fork-request", AgentSessionForkRequest), ("session-checkpoint", SessionCheckpointRecord)]:
        saved = json.loads((root / "packages" / "contracts" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert saved == model.model_json_schema(by_alias=True)


def test_automatic_summary_also_runs_after_context_pack_execution(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from test_agent_service import build_service, wait_terminal, PROJECT_ID, FINGERPRINT, PROJECT_KB_STATE_ID, PROJECT_KB_MANIFEST_HASH
    from test_session_checkpoint_wiring import SMALL_SPEC
    from zhimeng_runtime.context_compiler import compile_context_pack
    from zhimeng_runtime.context_store import ContextStore
    from zhimeng_runtime.model_context import CONTEXT_WINDOW_SPECS
    # 折叠机制的端到端用例用小阈值档位（同 test_session_checkpoint_wiring）：
    # 256K 档的真实触发值是 10 万 token（9-28 起按估算的 token：中文一字一个、英文三个字母一个），这个夹具的历史只有一万多字。
    monkeypatch.setitem(CONTEXT_WINDOW_SPECS, "256k", SMALL_SPEC)
    async def scenario():
        provider = TurnScriptedProvider([
            [ProviderTextDelta("answer" * 800), ProviderCompleted("stop", None)],
            [ProviderTextDelta(full_summary("summary from bound context")), ProviderCompleted("stop", None)],
        ])
        service = build_service(tmp_path, provider)
        session = await service.create_session(project_id=PROJECT_ID, title="Writing Agent")
        # 原样只留得下最后一轮（小档位留 4,500 字以内、至少一整轮；9-28 起）：三轮旧对话一次折完。
        for index in range(3):
            _complete_turn(service.store, session.session_id, key=f"long-old-{index}",
                           message=f"older question {index}", response="很长的回答" * 1000,
                           revision=1, fingerprint=FINGERPRINT)
        request = ContextCreateRequest(objective="continue", profile="chat", active_chapter_id="chapter-001",
                                       expected_project_revision=1, expected_project_fingerprint=FINGERPRINT,
                                       project_kb_state_id=PROJECT_KB_STATE_ID, project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH)
        through, history = service.store.completed_history(session.session_id)
        pack = compile_context_pack(context_pack_id="context-pack-auto-summary", session_id=session.session_id,
                                    project_id=PROJECT_ID, history_through_ordinal=through, request=request,
                                    history=history, candidates=(), max_characters=40_000, max_tokens=40_000,
                                    created_at=datetime.now(UTC))
        ContextStore(service.store.database).create_pack(pack)
        result = await service.start_run(session_id=session.session_id, message=request.objective,
                                         expected_project_revision=1, expected_project_fingerprint=FINGERPRINT,
                                         active_chapter_id=request.active_chapter_id, idempotency_key="auto-after-pack",
                                         context_pack_id=pack.context_pack_id, expected_context_pack_hash=pack.pack_hash)
        assert (await wait_terminal(service, result.run.run_id)).status is RunStatus.SUCCEEDED
        task = service._tasks.get(result.run.run_id)
        if task is not None:
            await task
        checkpoint = service.store.latest_session_checkpoint(session.session_id)
        assert checkpoint is not None
        assert checkpoint.content.startswith(full_summary("summary from bound context"))
        assert checkpoint.through_ordinal == 6
        assert ContextPack.model_validate_json(pack.model_dump_json(by_alias=True)) == pack
        await service.shutdown()
    asyncio.run(scenario())


def test_fork_history_cannot_resume_and_branch_lifecycle_cannot_change_source(harness):
    from zhimeng_runtime.agent_store import AgentStoreInvalidState
    h = harness
    more_turns(h, 1)
    original_messages = h.agent_store.list_messages(h.session_id)
    original_session = h.agent_store.get_session(h.session_id)
    original_runs = h.agent_store.list_runs(h.session_id)
    branch = h.agent_store.fork_session(h.session_id, up_to_message_id=original_messages[-1].message_id)
    inherited = h.agent_store.list_messages(branch.session_id)
    provider = TurnScriptedProvider([])
    service = AgentService(h.agent_store, provider)
    with TestClient(app_for(service)) as client:
        for carrier_id in {item.run_id for item in inherited}:
            response = client.post(f"/api/v1/agent/runs/{carrier_id}/resume", json={
                "expected_workflow_plan_id": "workflow-plan-" + "a" * 32,
                "expected_plan_hash": "b" * 64,
                "expected_checkpoint_id": None, "expected_checkpoint_hash": None,
            })
            assert response.status_code == 404, response.text
            with pytest.raises(AgentStoreInvalidState):
                h.agent_store.transition_run(carrier_id, RunStatus.STARTING, detail="must not execute")
        assert provider.start_count == 0
        assert h.agent_store.recover_interrupted() == 0
        assert h.agent_store.list_messages(branch.session_id) == inherited
        h.agent_store.rebind_session(branch.session_id, project_id=None)
        h.agent_store.update_session(branch.session_id, pinned=True)
        assert client.delete(f"/api/v1/agent/sessions/{branch.session_id}").status_code == 204
    assert h.agent_store.get_session(branch.session_id).status == "closed"
    assert not h.agent_store.get_session(branch.session_id).pinned
    assert h.agent_store.get_session(h.session_id) == original_session
    assert h.agent_store.list_messages(h.session_id) == original_messages
    assert h.agent_store.list_runs(h.session_id) == original_runs


def test_a_message_sent_during_the_second_compaction_waits_instead_of_freezing(harness):
    """9-28 压缩考题：一场对话第二次整理时发下一句，start_run 等的是第一次整理留下的、早就亮着的「整理完了」，
    await 立刻返回又看见还在整理，原地空转不让出事件循环——整个后端卡死（一核 100%，所有接口不回），这次整理也做不完。
    场景放在单独的线程里跑、限时 20 秒：旧写法空转时这里的 asyncio 超时根本轮不到执行。"""
    import threading

    h = harness
    more_turns(h, 3)
    outcome: dict[str, object] = {}

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = {"n": 0}

        class SecondSummaryBlocks(TurnScriptedProvider):
            @property
            def identity(self):
                return _executor().model_copy(
                    update={"instance_id": f"provider-config-{self.dispatch_config_fingerprint}"}
                )

            async def stream_chat(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    started.set()
                    await release.wait()
                yield ProviderTextDelta(full_summary(f"summary {calls['n']}"))
                yield ProviderCompleted("stop", None)

        service = AgentService(h.agent_store, SecondSummaryBlocks([]))
        assert (await service.compact_session(h.session_id)).content.startswith(full_summary("summary 1"))
        for n in range(2):
            _complete_turn(h.agent_store, h.session_id, key=f"after-first-compaction-{n}",
                           message=f"later request {n}", response=f"later answer {n}",
                           revision=h.revision, fingerprint=h.fingerprint)
        second = asyncio.create_task(service.compact_session(h.session_id))
        await started.wait()
        queued = asyncio.create_task(
            service.start_run(session_id=h.session_id, message="next",
                              expected_project_revision=h.revision, expected_project_fingerprint=h.fingerprint,
                              active_chapter_id=None, idempotency_key="during-second-compaction")
        )
        await asyncio.sleep(0.05)
        outcome["queued_waiting"] = not queued.done()
        release.set()
        outcome["second"] = (await asyncio.wait_for(second, timeout=5)).content
        outcome["run_id"] = (await asyncio.wait_for(queued, timeout=5)).run.run_id
        await service.shutdown()

    worker = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    worker.start()
    worker.join(20)
    assert not worker.is_alive(), "start_run 在第二次整理时空转，事件循环卡死"
    assert outcome.get("queued_waiting") is True and str(outcome.get("second")).startswith(full_summary("summary 2"))
    assert outcome.get("run_id")
