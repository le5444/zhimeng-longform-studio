from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from zhimeng_runtime.agent_store import (
    AgentContextPackConflict,
    AgentContextPackSourceUnavailable,
    AgentExecutorConflict,
    AgentStore,
    AgentStoreConflict,
    AgentStoreInvalidState,
)
from zhimeng_runtime.agent_models import AgentSessionCreateRequest
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    ActivityKind,
    ArtifactPayload,
    CapabilityName,
    CapabilityProfile,
    ChangeProposalPayload,
    ExecutorIdentity,
    ExecutorKind,
    MessageUsage,
    RunStatus,
    SkillCallPayload,
)
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.prewrite_models import build_prewrite_operation_package


PROJECT_ID = "project-0123456789abcdef01234567"
FINGERPRINT = "a" * 64


def executor() -> ExecutorIdentity:
    return ExecutorIdentity(
        executor_id="executor.test-agent",
        kind=ExecutorKind.EMBEDDED,
        implementation="test-agent",
        version="1",
        protocol_version="1",
        instance_id=None,
    )


def capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        protocol_version="1",
        names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
        extensions={},
    )


def initialized_store(tmp_path: Path) -> AgentStore:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    return AgentStore(database)


def create_session(store: AgentStore):
    return store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=executor(),
        capabilities=capabilities(),
    )


def create_run(store: AgentStore, session_id: str, *, key: str = "turn-0001"):
    return store.create_run(
        session_id=session_id,
        message="收紧开头，但不要改动关键线索。",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key=key,
    )


def start_run(store: AgentStore, run_id: str) -> None:
    store.transition_run(run_id, RunStatus.STARTING, detail="Starting provider")
    store.transition_run(run_id, RunStatus.RUNNING, detail="Provider streaming")


def begin_tool_call(
    store: AgentStore,
    run_id: str,
    *,
    tool_call_id: str = "toolcall-0123456789abcdef",
    tool_name: str = "project.inspect",
    tool_version: str = "1.0.0",
    arguments: dict[str, object] | None = None,
    idempotency_key: str | None = "tool-call-idempotency-0001",
):
    return store.begin_tool_call(
        run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_version=tool_version,
        arguments=arguments or {"include_files": True},
        request_hash="b" * 64,
        idempotency_key=idempotency_key,
    )


def create_context_pack(
    store: AgentStore,
    session_id: str,
    *,
    context_pack_id: str = "context-pack-agent-store",
    knowledge_fingerprint: str | None = None,
):
    history_ordinal, history = store.completed_history(session_id)
    pack = compile_context_pack(
        context_pack_id=context_pack_id,
        session_id=session_id,
        project_id=PROJECT_ID,
        history_through_ordinal=history_ordinal,
        request=ContextCreateRequest(
            objective="收紧开头，但不要改动关键线索。",
            profile="rewrite",
            active_chapter_id="chapter-001",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            project_kb_manifest_hash="b" * 64,
        ),
        history=history,
        candidates=(
            ContextCandidateFragment(
                source_id="project:character-state",
                source_type="project_section",
                title="03_人物设定/人物状态.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                version="revision-1",
                source_hash=canonical_json_hash({"source": "character-state"}),
                content="Lin refuses to sacrifice the witness.",
                priority=900,
                mandatory=True,
                eligible=True,
            ),
            ContextCandidateFragment(
                source_id="project:active-chapter",
                source_type="active_chapter",
                title="05_正文内容/第一卷/CH001_正文.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                version="revision-1",
                source_hash=canonical_json_hash({"source": "active-chapter"}),
                content="The witness left a blood-stained key.",
                priority=900,
                mandatory=True,
                eligible=True,
            ),
            ContextCandidateFragment(
                source_id="project:chapter-control",
                source_type="chapter_control",
                title="04_剧情大纲/CH001_控制卡.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                version="revision-1",
                source_hash=canonical_json_hash({"source": "chapter-control"}),
                content="Recover the witness without losing the key clue.",
                priority=900,
                mandatory=True,
                eligible=True,
            ),
        ),
        max_characters=20_000,
        max_tokens=20_000,
        created_at=datetime.now(UTC),
        knowledge_fingerprint=knowledge_fingerprint,
    )
    route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
    )
    ContextStore(store.database).create_pack(pack, nlcl_route=route)
    return pack


def knowledge_observation(
    pack,
    *,
    state: KnowledgeObservationState = KnowledgeObservationState.MATCHED,
    observed_fingerprint: str | None = None,
) -> KnowledgeFingerprintObservation:
    if state is KnowledgeObservationState.MATCHED:
        observed_fingerprint = pack.knowledge_fingerprint
    return KnowledgeFingerprintObservation(
        context_pack_id=pack.context_pack_id,
        pack_hash=pack.pack_hash,
        expected_fingerprint=pack.knowledge_fingerprint,
        observed_fingerprint=observed_fingerprint,
        state=state,
        observed_at=datetime.now(UTC),
    )


def test_session_run_messages_and_idempotency_are_durable(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)

    run, user_message, replayed = create_run(store, session.session_id)
    replay_run, replay_message, replayed_again = create_run(store, session.session_id)

    assert replayed is False
    assert replayed_again is True
    assert replay_run.run_id == run.run_id
    assert replay_message == user_message
    assert [item.root.sequence for item in store.list_activities(run.run_id)] == [1, 2]
    assert [item.content for item in store.list_messages(session.session_id)] == [
        "收紧开头，但不要改动关键线索。"
    ]
    assert store.provider_history(session.session_id) == [
        {"role": "user", "content": "收紧开头，但不要改动关键线索。"}
    ]

    with pytest.raises(AgentStoreConflict, match="different request"):
        store.create_run(
            session_id=session.session_id,
            message="换一条不同指令",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id="chapter-001",
            idempotency_key="turn-0001",
        )


def test_legacy_session_executor_is_upgraded_for_the_first_new_run(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    current_executor = executor().model_copy(
        update={"instance_id": f"provider-config-{'c' * 64}"}
    )
    current_capabilities = capabilities().model_copy(
        update={
            "names": [
                CapabilityName.STREAMING,
                CapabilityName.CANCEL,
                CapabilityName.TOOL_CALLS,
            ]
        }
    )

    run, _, replayed = store.create_run(
        session_id=session.session_id,
        message="收紧开头，但不要改动关键线索。",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key="turn-legacy-upgrade",
        current_executor=current_executor,
        current_capabilities=current_capabilities,
    )

    upgraded = store.get_session(session.session_id)
    assert replayed is False
    assert run.executor == current_executor
    assert run.capabilities == current_capabilities
    assert upgraded.executor == current_executor
    assert upgraded.capabilities == current_capabilities



def test_managed_o2e_session_executor_can_be_adopted(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    handoff_executor = ExecutorIdentity(
        executor_id="executor.context-handoff-runtime",
        kind=ExecutorKind.EMBEDDED,
        implementation="runtime-context-handoff-compiler",
        version="1",
        protocol_version="1",
        instance_id="runtime.context-handoff",
    )
    session_id = "session-o2e-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with store.database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO execution_session(
                id, project_binding_id, title, status, revision,
                latest_project_revision, latest_project_fingerprint,
                executor_json, capabilities_json, correlation_id,
                last_run_ordinal, last_message_ordinal, created_at, updated_at
            ) VALUES (?, ?, ?, 'idle', 1, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                session_id,
                PROJECT_ID,
                "O2-E assemble-context",
                1,
                FINGERPRINT,
                json.dumps(handoff_executor.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                json.dumps(capabilities().model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                "context-handoff-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "2026-07-25T00:00:00+00:00",
                "2026-07-25T00:00:00+00:00",
            ),
        )
        connection.commit()

    pack = create_context_pack(
        store,
        session_id,
        context_pack_id="context-pack-o2e-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        knowledge_fingerprint="c" * 64,
    )
    # Rewrite the NLCL route source to external_distillation to mirror O2-E packs.
    with store.database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE nlcl_route_receipt
            SET source = 'external_distillation'
            WHERE context_pack_id = ?
            """,
            (pack.context_pack_id,),
        )
        connection.commit()

    live_executor = executor().model_copy(
        update={"instance_id": f"provider-config-{'d' * 64}"}
    )
    live_capabilities = capabilities().model_copy(
        update={
            "names": [
                CapabilityName.STREAMING,
                CapabilityName.CANCEL,
                CapabilityName.TOOL_CALLS,
            ]
        }
    )

    run, _, replayed = store.create_run(
        session_id=session_id,
        message=pack.objective,
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="turn-o2e-adopt",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
        knowledge_observation=knowledge_observation(pack),
        current_executor=live_executor,
        current_capabilities=live_capabilities,
    )

    adopted = store.get_session(session_id)
    assert replayed is False
    assert run.executor == live_executor
    assert run.capabilities == live_capabilities
    assert adopted.executor == live_executor
    assert adopted.capabilities == live_capabilities
    bound = ContextStore(store.database).get_pack(pack.context_pack_id)
    assert bound.consumed_by_run_id == run.run_id


def test_nonlegacy_session_executor_drift_is_rejected(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    first_executor = executor().model_copy(
        update={"instance_id": f"provider-config-{'a' * 64}"}
    )
    session = store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=first_executor,
        capabilities=capabilities(),
    )
    changed_executor = first_executor.model_copy(
        update={"instance_id": f"provider-config-{'b' * 64}"}
    )

    with pytest.raises(
        AgentExecutorConflict,
        match="executor configuration changed",
    ) as drift:
        store.create_run(
            session_id=session.session_id,
            message="收紧开头，但不要改动关键线索。",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id="chapter-001",
            idempotency_key="turn-provider-drift",
            current_executor=changed_executor,
            current_capabilities=capabilities(),
        )

    assert drift.value.code == "workflow_executor_changed"
    assert drift.value.retryable is False
    assert store.list_runs(session.session_id) == []


def test_new_run_uses_current_capability_snapshot_for_same_executor(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    current_executor = executor().model_copy(
        update={"instance_id": f"provider-config-{'a' * 64}"}
    )
    session = store.create_session(
        project_id=PROJECT_ID,
        title="Writing Agent",
        executor=current_executor,
        capabilities=capabilities(),
    )
    current_capabilities = capabilities().model_copy(
        update={
            "names": [
                CapabilityName.STREAMING,
                CapabilityName.CANCEL,
                CapabilityName.TOOL_CALLS,
                CapabilityName.SKILLS,
            ]
        }
    )

    run, _, _ = store.create_run(
        session_id=session.session_id,
        message="收紧开头，但不要改动关键线索。",
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id="chapter-001",
        idempotency_key="turn-capability-refresh",
        current_executor=current_executor,
        current_capabilities=current_capabilities,
    )

    assert run.capabilities == current_capabilities
    assert store.get_session(session.session_id).capabilities == current_capabilities


def test_blank_session_title_is_rejected() -> None:
    with pytest.raises(ValueError, match="title must not be blank"):
        AgentSessionCreateRequest(title="   ")


def test_provider_history_uses_latest_messages_after_storage_window(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)

    with store.database.connection() as connection:
        template_row = connection.execute(
            "SELECT payload_json FROM activity WHERE run_id = ? AND sequence = 1",
            (run.run_id,),
        ).fetchone()
        template = json.loads(str(template_row["payload_json"]))
        for ordinal in range(2, 506):
            activity_id = f"activity-history-{ordinal:04d}"
            message_id = f"message-history-{ordinal:04d}"
            template["activity_id"] = activity_id
            template["sequence"] = ordinal + 1
            template["payload"]["message_id"] = message_id
            template["payload"]["content"] = f"message {ordinal}"
            connection.execute(
                """
                INSERT INTO activity(
                    id, session_id, run_id, sequence, kind, payload_json,
                    project_revision, project_fingerprint, correlation_id,
                    causation_id, created_at
                ) VALUES (?, ?, ?, ?, 'message', ?, 1, ?, ?, NULL, ?)
                """,
                (
                    activity_id,
                    session.session_id,
                    run.run_id,
                    ordinal + 1,
                    json.dumps(template, ensure_ascii=False),
                    FINGERPRINT,
                    run.correlation_id,
                    run.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO session_message(
                    activity_id, session_id, run_id, message_id, ordinal,
                    role, format, created_at
                ) VALUES (?, ?, ?, ?, ?, 'user', 'plain', ?)
                """,
                (
                    activity_id,
                    session.session_id,
                    run.run_id,
                    message_id,
                    ordinal,
                    run.created_at.isoformat(),
                ),
            )
        connection.execute(
            "UPDATE execution_session SET last_message_ordinal = 505 WHERE id = ?",
            (session.session_id,),
        )

    history = store.provider_history(session.session_id, max_messages=3)
    assert [item["content"] for item in history] == [
        "message 503",
        "message 504",
        "message 505",
    ]


def test_only_one_non_terminal_run_is_allowed_per_session(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    create_run(store, session.session_id)

    with pytest.raises(AgentStoreConflict, match="non-terminal"):
        create_run(store, session.session_id, key="turn-0002")


def test_concurrent_activity_sequences_remain_contiguous(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    message_id = "message-0123456789abcdef"

    with ThreadPoolExecutor(max_workers=6) as pool:
        activities = list(
            pool.map(
                lambda index: store.append_assistant_delta(
                    run.run_id,
                    message_id=message_id,
                    delta=f"片段{index}",
                ),
                range(12),
            )
        )

    persisted = store.list_activities(run.run_id)
    assert [item.root.sequence for item in persisted] == list(range(1, 17))
    assert sorted(item.root.sequence for item in activities) == list(range(5, 17))
    assert all(item.root.kind is ActivityKind.MESSAGE for item in activities)


def test_tool_call_begin_finish_and_replay_are_atomic_and_causal(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)

    tool_call, replayed = begin_tool_call(store, run.run_id)
    replay, replayed_again = begin_tool_call(store, run.run_id)

    assert replayed is False
    assert replayed_again is True
    assert replay == tool_call
    assert tool_call.status == "running"
    assert tool_call.arguments == {"include_files": True}
    assert tool_call.call_activity_id is not None
    assert [item.root.kind for item in store.list_activities(run.run_id)][-1] is (
        ActivityKind.TOOL_CALL
    )
    with pytest.raises(AgentStoreConflict, match="different request"):
        begin_tool_call(
            store,
            run.run_id,
            arguments={"include_files": False},
        )

    finished, finish_replayed = store.finish_tool_call(
        run.run_id,
        tool_call_id=tool_call.tool_call_id,
        status="succeeded",
        output={"revision": 1, "fingerprint": FINGERPRINT},
        error=None,
        duration_ms=12,
    )
    replayed_finish, finish_replayed_again = store.finish_tool_call(
        run.run_id,
        tool_call_id=tool_call.tool_call_id,
        status="succeeded",
        output={"revision": 1, "fingerprint": FINGERPRINT},
        error=None,
        duration_ms=12,
    )

    assert finish_replayed is False
    assert finish_replayed_again is True
    assert replayed_finish == finished
    assert finished.status == "succeeded"
    assert finished.result_activity_id is not None
    assert finished.result_hash is not None and len(finished.result_hash) == 64
    assert finished.duration_ms == 12
    activities = store.list_activities(run.run_id)
    call_activity = next(
        item for item in activities if item.root.kind is ActivityKind.TOOL_CALL
    )
    result_activity = next(
        item for item in activities if item.root.kind is ActivityKind.TOOL_RESULT
    )
    assert result_activity.root.causation_id == call_activity.root.activity_id
    assert result_activity.root.payload.tool_call_id == tool_call.tool_call_id
    assert store.get_tool_call(tool_call.tool_call_id) == finished
    assert store.list_tool_calls(run.run_id) == [finished]
    assert len(store.list_run_events(run.run_id)) == len(activities)

    with pytest.raises(AgentStoreConflict, match="different result"):
        store.finish_tool_call(
            run.run_id,
            tool_call_id=tool_call.tool_call_id,
            status="succeeded",
            output={"revision": 2},
            error=None,
            duration_ms=12,
        )


def test_skill_receipt_artifact_and_tool_result_commit_in_one_causal_chain(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    tool_call, _ = begin_tool_call(
        store,
        run.run_id,
        tool_call_id="toolcall-skill-receipt-0001",
        tool_name="skill_execute",
        arguments={"skill_id": "novel-creation-suite.project-audit", "input": {}},
    )
    skill_call = SkillCallPayload(
        skill_call_id="skillcall-project-audit-0001",
        skill_id="novel-creation-suite.project-audit",
        skill_version="2026.07.20-m3a.1",
        status="succeeded",
        input={},
        output={"receipt": {"receipt_id": "skillreceipt-project-audit-0001"}},
    )
    artifact = ArtifactPayload(
        artifact_id="artifact-project-audit-0001",
        artifact_type="project_audit",
        name="Creation Suite project audit",
        uri="runtime://skill-runs/skillcall-project-audit-0001/project-audit.json",
        media_type="application/json",
        content_hash="b" * 64,
        size_bytes=123,
    )

    stored, replayed = store.finish_tool_call(
        run.run_id,
        tool_call_id=tool_call.tool_call_id,
        status="succeeded",
        output={"receipt_id": "skillreceipt-project-audit-0001"},
        error=None,
        duration_ms=18,
        skill_call=skill_call,
        artifacts=(artifact,),
    )

    assert replayed is False
    assert stored.status == "succeeded"
    activities = store.list_activities(run.run_id)
    tail = activities[-4:]
    assert [item.root.kind for item in tail] == [
        ActivityKind.TOOL_CALL,
        ActivityKind.SKILL_CALL,
        ActivityKind.ARTIFACT,
        ActivityKind.TOOL_RESULT,
    ]
    assert tail[1].root.causation_id == tail[0].root.activity_id
    assert tail[2].root.causation_id == tail[1].root.activity_id
    assert tail[3].root.causation_id == tail[2].root.activity_id
    with store.database.connection() as connection:
        stored_artifact = connection.execute(
            "SELECT artifact_type, content_hash FROM artifact WHERE id = ?",
            (artifact.artifact_id,),
        ).fetchone()
    assert stored_artifact is not None
    assert tuple(stored_artifact) == ("project_audit", "b" * 64)


def test_tool_call_begin_rolls_back_activity_and_event_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    before_activities = store.list_activities(run.run_id)
    before_events = store.list_run_events(run.run_id)
    original_insert = AgentStore._insert_activity

    def fail_after_tool_call_insert(connection, run_row, **kwargs):
        item = original_insert(connection, run_row, **kwargs)
        if kwargs["kind"] is ActivityKind.TOOL_CALL:
            raise RuntimeError("injected ToolCall activity failure")
        return item

    monkeypatch.setattr(
        AgentStore,
        "_insert_activity",
        staticmethod(fail_after_tool_call_insert),
    )

    with pytest.raises(RuntimeError, match="injected ToolCall"):
        begin_tool_call(store, run.run_id)

    assert store.list_tool_calls(run.run_id) == []
    assert store.list_activities(run.run_id) == before_activities
    assert store.list_run_events(run.run_id) == before_events
    with store.database.connection() as connection:
        persisted_sequence = int(
            connection.execute(
                "SELECT last_activity_sequence FROM run WHERE id = ?",
                (run.run_id,),
            ).fetchone()[0]
        )
    assert persisted_sequence == before_activities[-1].root.sequence


def test_tool_result_and_change_proposal_commit_in_one_causal_chain(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    tool_call, _ = begin_tool_call(
        store,
        run.run_id,
        tool_call_id="toolcall-change-proposal-0001",
        idempotency_key="tool-call-proposal-key-0001",
    )
    proposal = ChangeProposalPayload(
        proposal_id="proposal-0123456789abcdef",
        title="Revise chapter opening",
        summary="Tighten the opening while preserving the clue.",
        base_revision=1,
        base_fingerprint=FINGERPRINT,
        files=[
            {
                "file_id": "file-0123456789abcdef",
                "path": "05_正文内容/第一卷/CH001_正文.md",
                "operation": "update",
                "content_hash_before": "c" * 64,
                "content_hash_after": "d" * 64,
            }
        ],
    )

    finished, replayed = store.finish_tool_call(
        run.run_id,
        tool_call_id=tool_call.tool_call_id,
        status="succeeded",
        output={"change_set_id": proposal.proposal_id},
        error=None,
        duration_ms=25,
        change_proposal=proposal,
    )

    assert replayed is False
    activities = store.list_activities(run.run_id)
    result_activity = activities[-2]
    proposal_activity = activities[-1]
    assert result_activity.root.kind is ActivityKind.TOOL_RESULT
    assert proposal_activity.root.kind is ActivityKind.CHANGE_PROPOSAL
    assert proposal_activity.root.causation_id == result_activity.root.activity_id
    assert finished.result_activity_id == result_activity.root.activity_id
    assert proposal_activity.root.payload == proposal


@pytest.mark.parametrize("terminal_mode", ["fail", "cancel", "interrupt", "recover"])
def test_run_terminal_paths_close_open_tool_calls(
    tmp_path: Path,
    terminal_mode: str,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    tool_call, _ = begin_tool_call(store, run.run_id)

    if terminal_mode == "fail":
        terminal = store.fail_run(
            run.run_id,
            code="provider_failed",
            message="Provider failed while a tool was running",
            retryable=True,
        )
        expected_run_status = RunStatus.FAILED
        expected_tool_status = "failed"
    elif terminal_mode == "cancel":
        cancelling = store.cancel_run(run.run_id)
        assert cancelling.status is RunStatus.CANCELLING
        terminal = store.mark_cancelled(run.run_id)
        expected_run_status = RunStatus.CANCELLED
        expected_tool_status = "cancelled"
    elif terminal_mode == "interrupt":
        terminal = store.mark_interrupted(
            run.run_id,
            detail="Runtime stopped during ToolCall",
        )
        expected_run_status = RunStatus.INTERRUPTED
        expected_tool_status = "failed"
    else:
        assert store.recover_interrupted() == 1
        terminal = store.get_run(run.run_id)
        expected_run_status = RunStatus.INTERRUPTED
        expected_tool_status = "failed"

    closed = store.get_tool_call(tool_call.tool_call_id)
    assert terminal.status is expected_run_status
    assert closed.status == expected_tool_status
    assert closed.result_activity_id is not None
    assert closed.finished_at is not None
    assert closed.result_hash is not None
    tool_results = [
        item
        for item in store.list_activities(run.run_id)
        if item.root.kind is ActivityKind.TOOL_RESULT
    ]
    assert len(tool_results) == 1
    assert tool_results[0].root.causation_id == tool_call.call_activity_id
    with store.database.connection() as connection:
        open_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM tool_call
                WHERE run_id = ? AND status IN ('pending', 'running')
                """,
                (run.run_id,),
            ).fetchone()[0]
        )
    assert open_count == 0


def test_tool_result_and_proposal_roll_back_together_on_activity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)
    tool_call, _ = begin_tool_call(store, run.run_id)
    before_activities = store.list_activities(run.run_id)
    original_insert = AgentStore._insert_activity

    def fail_after_proposal_insert(connection, run_row, **kwargs):
        item = original_insert(connection, run_row, **kwargs)
        if kwargs["kind"] is ActivityKind.CHANGE_PROPOSAL:
            raise RuntimeError("injected proposal activity failure")
        return item

    monkeypatch.setattr(
        AgentStore,
        "_insert_activity",
        staticmethod(fail_after_proposal_insert),
    )
    proposal = ChangeProposalPayload(
        proposal_id="proposal-rollback-0001",
        title="Rollback fixture",
        summary="The transaction must not leave a partial ToolResult.",
        base_revision=1,
        base_fingerprint=FINGERPRINT,
        files=[
            {
                "file_id": "file-rollback-0001",
                "path": "05_正文内容/第一卷/CH001_正文.md",
                "operation": "update",
                "content_hash_before": "c" * 64,
                "content_hash_after": "d" * 64,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="injected proposal"):
        store.finish_tool_call(
            run.run_id,
            tool_call_id=tool_call.tool_call_id,
            status="succeeded",
            output={"change_set_id": proposal.proposal_id},
            error=None,
            duration_ms=5,
            change_proposal=proposal,
        )

    persisted = store.get_tool_call(tool_call.tool_call_id)
    assert persisted.status == "running"
    assert persisted.result_activity_id is None
    assert store.list_activities(run.run_id) == before_activities


def test_invalid_terminal_writes_do_not_corrupt_run(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    run, _, _ = create_run(store, session.session_id)
    start_run(store, run.run_id)

    for status in (
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    ):
        with pytest.raises(AgentStoreInvalidState, match="dedicated"):
            store.transition_run(run.run_id, status, detail="invalid shortcut")
    with pytest.raises(AgentStoreInvalidState, match="non-blank"):
        store.complete_run(
            run.run_id,
            message_id="message-0123456789abcdef",
            content="   ",
            finish_reason="stop",
            usage=None,
        )
    with pytest.raises(AgentStoreInvalidState, match="finish reason"):
        store.complete_run(
            run.run_id,
            message_id="message-0123456789abcdef",
            content="未完成正文",
            finish_reason="length",
            usage=None,
        )

    assert store.get_run(run.run_id).status is RunStatus.RUNNING


def test_completed_message_and_restart_recovery_update_session(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    completed_run, _, _ = create_run(store, session.session_id)
    start_run(store, completed_run.run_id)
    completed, assistant = store.complete_run(
        completed_run.run_id,
        message_id="message-0123456789abcdef",
        content="## 修改建议\n\n保留钥匙线索，并压缩第一段。",
        finish_reason="stop",
        usage=MessageUsage(input_tokens=20, output_tokens=18, total_tokens=38),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert assistant.format == "markdown"
    assert [item.role for item in store.list_messages(session.session_id)] == [
        "user",
        "assistant",
    ]
    assert store.get_session(session.session_id).status.value == "idle"

    interrupted_run, _, _ = create_run(store, session.session_id, key="turn-0002")
    store.transition_run(
        interrupted_run.run_id,
        RunStatus.STARTING,
        detail="Starting provider",
    )
    assert store.recover_interrupted() == 1
    assert store.get_run(interrupted_run.run_id).status is RunStatus.INTERRUPTED


def test_context_pack_binding_and_injection_receipt_are_atomic(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(store, session.session_id)

    run, _, replayed = store.create_run(
        session_id=session.session_id,
        message=pack.objective,
        expected_project_revision=pack.project_revision,
        expected_project_fingerprint=pack.project_fingerprint,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="turn-context-0001",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
    )

    assert replayed is False
    assert [item.root.kind for item in store.list_activities(run.run_id)] == [
        ActivityKind.ARTIFACT,
        ActivityKind.ARTIFACT,
        ActivityKind.MESSAGE,
        ActivityKind.RUN_STATE,
    ]
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Preparing context")
    prepared = store.prepare_context_injection(run.run_id)
    replayed_receipt = store.prepare_context_injection(run.run_id)

    assert prepared == replayed_receipt
    assert prepared is not None
    assert prepared.pack_hash == pack.pack_hash
    assert prepared.provider_messages_hash == pack.provider_messages_hash
    assert list(prepared.messages) == [
        message.model_dump(mode="json")
        for message in pack.exact_provider_messages
    ]
    with store.database.connection() as connection:
        artifact_types = [
            str(row[0])
            for row in connection.execute(
                "SELECT artifact_type FROM artifact WHERE run_id = ? ORDER BY created_at, id",
                (run.run_id,),
            ).fetchall()
        ]
        bound_pack_id = str(
            connection.execute(
                "SELECT context_pack_id FROM run WHERE id = ?",
                (run.run_id,),
            ).fetchone()[0]
        )
    assert sorted(artifact_types) == sorted(
        [
            "context_pack",
            "writer_intent",
            "context_injection_receipt",
        ]
    )
    assert bound_pack_id == pack.context_pack_id
    assert [item.root.sequence for item in store.list_activities(run.run_id)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_workflow_plan_builder_failure_rolls_back_run_and_context_binding(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(store, session.session_id)

    def fail_plan(run):
        assert run.run_id.startswith("run_")
        raise RuntimeError("plan compilation failed")

    with pytest.raises(RuntimeError, match="plan compilation failed"):
        store.create_run(
            session_id=session.session_id,
            message=pack.objective,
            expected_project_revision=pack.project_revision,
            expected_project_fingerprint=pack.project_fingerprint,
            active_chapter_id=pack.active_chapter_id,
            idempotency_key="turn-context-plan-failure",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
            workflow_plan_builder=fail_plan,
        )

    assert store.list_runs(session.session_id) == []
    assert store.list_messages(session.session_id) == []
    assert ContextStore(store.database).get_pack(
        pack.context_pack_id
    ).consumed_by_run_id is None
    unchanged = store.get_session(session.session_id)
    assert unchanged.last_run_ordinal == 0
    assert unchanged.last_message_ordinal == 0


def test_context_pack_binding_requires_current_knowledge_but_replays_first(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(
        store,
        session.session_id,
        knowledge_fingerprint="d" * 64,
    )
    arguments = {
        "session_id": session.session_id,
        "message": pack.objective,
        "expected_project_revision": pack.project_revision,
        "expected_project_fingerprint": pack.project_fingerprint,
        "active_chapter_id": pack.active_chapter_id,
        "context_pack_id": pack.context_pack_id,
        "expected_context_pack_hash": pack.pack_hash,
    }

    with pytest.raises(AgentContextPackConflict) as changed:
        store.create_run(
            **arguments,
            idempotency_key="turn-context-knowledge-changed",
            knowledge_observation=knowledge_observation(
                pack,
                state=KnowledgeObservationState.CHANGED,
                observed_fingerprint="e" * 64,
            ),
        )
    assert changed.value.code == "context_pack_source_changed"
    assert store.list_runs(session.session_id) == []
    assert ContextStore(store.database).get_pack(pack.context_pack_id).consumed_by_run_id is None

    with pytest.raises(AgentContextPackSourceUnavailable):
        store.create_run(
            **arguments,
            idempotency_key="turn-context-knowledge-unavailable",
            knowledge_observation=knowledge_observation(
                pack,
                state=KnowledgeObservationState.UNAVAILABLE,
            ),
        )
    assert store.list_runs(session.session_id) == []

    created, _, replayed = store.create_run(
        **arguments,
        idempotency_key="turn-context-knowledge-replay",
        knowledge_observation=knowledge_observation(pack),
    )
    replay, _, replayed_again = store.create_run(
        **arguments,
        idempotency_key="turn-context-knowledge-replay",
        knowledge_observation=knowledge_observation(
            pack,
            state=KnowledgeObservationState.CHANGED,
            observed_fingerprint="e" * 64,
        ),
    )
    assert replayed is False
    assert replayed_again is True
    assert replay.run_id == created.run_id


def test_provider_preflight_blocks_changed_knowledge_before_and_after_receipt(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(
        store,
        session.session_id,
        knowledge_fingerprint="d" * 64,
    )
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=pack.objective,
        expected_project_revision=pack.project_revision,
        expected_project_fingerprint=pack.project_fingerprint,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="turn-context-provider-knowledge",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
        knowledge_observation=knowledge_observation(pack),
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Start")

    with pytest.raises(AgentContextPackConflict) as changed:
        store.prepare_context_injection(
            run.run_id,
            knowledge_observation=knowledge_observation(
                pack,
                state=KnowledgeObservationState.CHANGED,
                observed_fingerprint="e" * 64,
            ),
        )
    assert changed.value.code == "context_pack_source_changed"
    with pytest.raises(AgentContextPackSourceUnavailable):
        store.prepare_context_injection(
            run.run_id,
            knowledge_observation=knowledge_observation(
                pack,
                state=KnowledgeObservationState.UNAVAILABLE,
            ),
        )
    with store.database.connection() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM artifact
            WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
            """,
            (run.run_id,),
        ).fetchone()[0] == 0

    prepared = store.prepare_context_injection(
        run.run_id,
        knowledge_observation=knowledge_observation(pack),
    )
    with pytest.raises(AgentContextPackConflict) as changed_after_receipt:
        store.prepare_context_injection(
            run.run_id,
            knowledge_observation=knowledge_observation(
                pack,
                state=KnowledgeObservationState.CHANGED,
                observed_fingerprint="e" * 64,
            ),
        )
    assert changed_after_receipt.value.code == "context_pack_source_changed"
    replayed = store.prepare_context_injection(
        run.run_id,
        knowledge_observation=knowledge_observation(pack),
    )
    assert replayed == prepared
    with store.database.connection() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM artifact
            WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
            """,
            (run.run_id,),
        ).fetchone()[0] == 1


def test_context_pack_binding_rejects_mismatch_and_second_consumption(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(store, session.session_id)

    with pytest.raises(AgentContextPackConflict) as mismatched:
        store.create_run(
            session_id=session.session_id,
            message=pack.objective,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=pack.active_chapter_id,
            idempotency_key="turn-context-wrong-hash",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash="f" * 64,
        )
    assert mismatched.value.code == "context_pack_hash_mismatch"

    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=pack.objective,
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="turn-context-consume",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Start")
    store.fail_run(
        run.run_id,
        code="fixture_failure",
        message="fixture failed after binding",
        retryable=False,
    )
    with pytest.raises(AgentContextPackConflict) as consumed:
        store.create_run(
            session_id=session.session_id,
            message=pack.objective,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=pack.active_chapter_id,
            idempotency_key="turn-context-reuse",
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
        )
    assert consumed.value.code == "context_pack_already_consumed"


def test_migrated_prewrite_operation_package_cannot_start_run(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    pack = create_context_pack(store, session.session_id)
    context_store = ContextStore(store.database)
    route = context_store.get_nlcl_route(pack.context_pack_id)
    migrated_package = build_prewrite_operation_package(
        pack,
        route,
        extra_missing_requirements=(
            "migrated_prewrite_package_requires_recompile",
        ),
    )
    assert migrated_package.readiness == "blocked"
    with store.database.connection() as connection:
        connection.execute(
            """
            UPDATE prewrite_operation_package
            SET readiness = ?, package_hash = ?, payload_json = ?
            WHERE context_pack_id = ?
            """,
            (
                migrated_package.readiness,
                migrated_package.package_hash,
                json.dumps(
                    migrated_package.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                pack.context_pack_id,
            ),
        )
    assert context_store.get_prewrite_operation_package(
        pack.context_pack_id
    ) == migrated_package

    with pytest.raises(AgentContextPackConflict) as blocked:
        store.create_run(
            session_id=session.session_id,
            message=pack.objective,
            expected_project_revision=pack.project_revision,
            expected_project_fingerprint=pack.project_fingerprint,
            active_chapter_id=pack.active_chapter_id,
            context_pack_id=pack.context_pack_id,
            expected_context_pack_hash=pack.pack_hash,
            idempotency_key="turn-blocked-prewrite-package",
            knowledge_observation=knowledge_observation(pack),
        )

    assert blocked.value.code == "prewrite_operation_package_recompile_required"
    assert store.list_runs(session.session_id) == []


def test_context_pack_cannot_be_swapped_after_run_binding(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    reviewed = create_context_pack(store, session.session_id)
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=reviewed.objective,
        expected_project_revision=1,
        expected_project_fingerprint=FINGERPRINT,
        active_chapter_id=reviewed.active_chapter_id,
        idempotency_key="turn-context-swap",
        context_pack_id=reviewed.context_pack_id,
        expected_context_pack_hash=reviewed.pack_hash,
    )
    swapped = compile_context_pack(
        context_pack_id=reviewed.context_pack_id,
        session_id=session.session_id,
        project_id=PROJECT_ID,
        history_through_ordinal=0,
        request=ContextCreateRequest(
            objective=reviewed.objective,
            profile="chapter",
            active_chapter_id=reviewed.active_chapter_id,
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            project_kb_manifest_hash="b" * 64,
        ),
        history=(),
        candidates=(),
        max_characters=20_000,
        max_tokens=20_000,
        created_at=datetime.now(UTC),
    )
    with store.database.connection() as connection:
        connection.execute(
            """
            UPDATE context_pack
            SET profile = ?, readiness = ?, pack_hash = ?, injection_hash = ?,
                provider_messages_hash = ?, payload_json = ?, created_at = ?
            WHERE id = ?
            """,
            (
                swapped.profile,
                swapped.readiness.value,
                swapped.pack_hash,
                swapped.injection_hash,
                swapped.provider_messages_hash,
                json.dumps(
                    swapped.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                swapped.created_at.isoformat(),
                swapped.context_pack_id,
            ),
        )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Start")

    with pytest.raises(AgentContextPackConflict) as integrity:
        store.prepare_context_injection(run.run_id)

    assert integrity.value.code == "context_injection_integrity_failed"


def test_context_pack_becomes_stale_when_project_changes_after_binding(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    reviewed = create_context_pack(store, session.session_id)
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=reviewed.objective,
        expected_project_revision=reviewed.project_revision,
        expected_project_fingerprint=reviewed.project_fingerprint,
        active_chapter_id=reviewed.active_chapter_id,
        idempotency_key="turn-context-project-changed",
        context_pack_id=reviewed.context_pack_id,
        expected_context_pack_hash=reviewed.pack_hash,
    )
    store.database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint="b" * 64,
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Start")

    with pytest.raises(AgentContextPackConflict) as stale:
        store.prepare_context_injection(run.run_id)

    assert stale.value.code == "context_pack_stale"


@pytest.mark.parametrize(
    "tamper",
    ["content_hash", "metadata_json", "duplicate"],
)
def test_context_injection_receipt_tampering_is_rejected(
    tmp_path: Path,
    tamper: str,
) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    reviewed = create_context_pack(store, session.session_id)
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message=reviewed.objective,
        expected_project_revision=reviewed.project_revision,
        expected_project_fingerprint=reviewed.project_fingerprint,
        active_chapter_id=reviewed.active_chapter_id,
        idempotency_key=f"turn-context-receipt-{tamper}",
        context_pack_id=reviewed.context_pack_id,
        expected_context_pack_hash=reviewed.pack_hash,
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="Start")
    assert store.prepare_context_injection(run.run_id) is not None

    with store.database.connection() as connection:
        if tamper == "content_hash":
            connection.execute(
                """
                UPDATE artifact SET content_hash = ?
                WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                """,
                ("0" * 64, run.run_id),
            )
        elif tamper == "metadata_json":
            connection.execute(
                """
                UPDATE artifact SET metadata_json = ?
                WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                """,
                ("{invalid", run.run_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO artifact(
                    id, run_id, artifact_type, relative_path,
                    content_hash, metadata_json, created_at
                )
                SELECT ?, run_id, artifact_type, relative_path,
                       content_hash, metadata_json, created_at
                FROM artifact
                WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
                """,
                ("artifact-duplicate-context-receipt", run.run_id),
            )

    with pytest.raises(AgentContextPackConflict) as integrity:
        store.prepare_context_injection(run.run_id)

    assert integrity.value.code == "context_injection_integrity_failed"


def test_completed_history_omits_failed_run_user_message(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    session = create_session(store)
    succeeded, _, _ = create_run(store, session.session_id)
    start_run(store, succeeded.run_id)
    store.complete_run(
        succeeded.run_id,
        message_id="message-completed-history",
        content="保留既有线索。",
        finish_reason="stop",
        usage=None,
    )
    failed, _, _ = create_run(store, session.session_id, key="turn-failed-history")
    store.transition_run(failed.run_id, RunStatus.STARTING, detail="Start")
    store.fail_run(
        failed.run_id,
        code="fixture_failure",
        message="Do not inherit this turn",
        retryable=False,
    )

    through_ordinal, completed = store.completed_history(session.session_id)

    assert through_ordinal == 3
    assert [message.role for message in completed] == ["user", "assistant"]
    assert [message.content for message in completed] == [
        "收紧开头，但不要改动关键线索。",
        "保留既有线索。",
    ]
