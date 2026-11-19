from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.context_api import create_context_router
from zhimeng_runtime.context_models import ContextPack, canonical_json_hash
from zhimeng_runtime.context_service import ContextService
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.distillation_models import DistillationSelectedCard
from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_kb_models import ProjectKbRebuildRequest
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS
from zhimeng_runtime.project_reader import UntrustedProjectPathError


@dataclass(slots=True)
class Harness:
    project: Path
    reader: ProjectReader
    database: SqliteStore
    agent_store: AgentStore
    context_store: ContextStore
    service: ContextService
    session_id: str
    project_id: str
    revision: int
    fingerprint: str
    project_kb_state_id: str
    project_kb_manifest_hash: str

    def app(self, service: ContextService | None = None) -> FastAPI:
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(create_context_router(self.service if service is None else service))
        return app

    def request(
        self,
        *,
        objective: str = "续写第四章前，核对人物立场和上一章结果。",
        active_chapter_id: str = "VOL001-CH003",
        profile: str = "chapter",
    ) -> dict[str, object]:
        return {
            "objective": objective,
            "profile": profile,
            "active_chapter_id": active_chapter_id,
            "expected_project_revision": self.revision,
            "expected_project_fingerprint": self.fingerprint,
            "project_kb_state_id": self.project_kb_state_id,
            "project_kb_manifest_hash": self.project_kb_manifest_hash,
        }


def _executor() -> ExecutorIdentity:
    return ExecutorIdentity(
        executor_id="executor.context-api-test",
        kind=ExecutorKind.EMBEDDED,
        implementation="context-api-test",
        version="1",
        protocol_version="1",
        instance_id=None,
    )


def _capabilities() -> CapabilityProfile:
    return CapabilityProfile(
        protocol_version="1",
        names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
        extensions={},
    )


def _write_project(root: Path) -> Path:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    core_files = {
        "01_项目定位/定位.md": "这是一本悬疑长篇，核心承诺是寻找失踪证人。",
        "02_世界观设定/城市.md": "故事发生在临江城，旧港区夜间封闭。",
        "03_人物设定/人物.json": '{"主角":"林澈","底线":"不牺牲证人"}',
        "04_剧情大纲/主线.md": "主线：找到证人，并揭开旧港失火真相。",
        "06_写作规范/风格.md": "对话克制，事实先于抒情。",
        "07_运营数据/project_status.md": "连载中；当前推进到第三章。",
    }
    for relative_path, content in core_files.items():
        target = root / Path(relative_path)
        target.write_text(content, encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir()
    (volume / "CH001_定稿.md").write_text("第一章：林澈收到匿名信。", encoding="utf-8")
    (volume / "CH002_定稿.md").write_text("第二章：匿名信指向旧港。", encoding="utf-8")
    (volume / "CH003_定稿.md").write_text("第三章：证人留下了带血钥匙。", encoding="utf-8")
    return root


def _complete_turn(
    store: AgentStore,
    session_id: str,
    *,
    key: str,
    message: str,
    response: str,
    revision: int,
    fingerprint: str,
) -> str:
    run, _, _ = store.create_run(
        session_id=session_id,
        message=message,
        expected_project_revision=revision,
        expected_project_fingerprint=fingerprint,
        active_chapter_id="VOL001-CH003",
        idempotency_key=key,
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="test start")
    store.transition_run(run.run_id, RunStatus.RUNNING, detail="test running")
    store.complete_run(
        run.run_id,
        message_id=f"message-{key}",
        content=response,
        finish_reason="stop",
        usage=None,
    )
    return run.run_id


def _fail_turn(
    store: AgentStore,
    session_id: str,
    *,
    key: str,
    message: str,
    revision: int,
    fingerprint: str,
) -> str:
    run, _, _ = store.create_run(
        session_id=session_id,
        message=message,
        expected_project_revision=revision,
        expected_project_fingerprint=fingerprint,
        active_chapter_id="VOL001-CH003",
        idempotency_key=key,
    )
    store.fail_run(
        run.run_id,
        code="test_failed",
        message="failed turn",
        retryable=False,
    )
    return run.run_id


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    library = tmp_path / "library"
    library.mkdir()
    project = _write_project(library / "novel")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)
    project_data = snapshot["project"]
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    binding = database.sync_project_binding(
        project_id=str(project_data["project_id"]),
        root_path=str(project),
        display_name="novel",
        fingerprint=str(project_data["fingerprint"]),
    )
    agent_store = AgentStore(database)
    session = agent_store.create_session(
        project_id=binding.project_id,
        title="Context API",
        executor=_executor(),
        capabilities=_capabilities(),
    )
    _complete_turn(
        agent_store,
        session.session_id,
        key="history-success-1",
        message="先确认第三章留下了什么。",
        response="第三章留下了带血钥匙。",
        revision=binding.revision,
        fingerprint=binding.fingerprint,
    )
    _fail_turn(
        agent_store,
        session.session_id,
        key="history-failed-1",
        message="这条失败消息不能进入上下文。",
        revision=binding.revision,
        fingerprint=binding.fingerprint,
    )
    context_store = ContextStore(database)
    project_kb = ProjectKbService(
        database,
        reader,
        tmp_path / "project-kb",
    )
    project_kb.store.ensure_project_scope(binding.project_id)
    knowledge = project_kb.store.project_state_snapshot(binding.project_id)
    rebuilt = project_kb.rebuild(
        binding.project_id,
        ProjectKbRebuildRequest(
            expected_project_revision=binding.revision,
            expected_project_fingerprint=binding.fingerprint,
            expected_scope_state_revision=knowledge.scope.state_revision,
            expected_knowledge_fingerprint=canonical_json_hash(
                knowledge.fingerprint_material
            ),
        ),
    )
    assert rebuilt.record.manifest is not None
    service = ContextService(
        context_store,
        agent_store,
        reader,
        ProjectLockRegistry(),
        project_kb_service=project_kb,
    )
    return Harness(
        project=project,
        reader=reader,
        database=database,
        agent_store=agent_store,
        context_store=context_store,
        service=service,
        session_id=session.session_id,
        project_id=binding.project_id,
        revision=binding.revision,
        fingerprint=binding.fingerprint,
        project_kb_state_id=rebuilt.record.manifest.state_id,
        project_kb_manifest_hash=rebuilt.record.manifest.manifest_hash,
    )


def _post_pack(
    client: TestClient,
    harness: Harness,
    *,
    key: str = "context-key-0001",
    payload: dict[str, object] | None = None,
):
    return client.post(
        f"/api/v1/agent/sessions/{harness.session_id}/context-packs",
        headers={"Idempotency-Key": key},
        json=harness.request() if payload is None else payload,
    )


def _rebuild_project_kb(
    harness: Harness,
    *,
    project_revision: int,
    project_fingerprint: str,
):
    service = harness.service.project_kb_service
    assert service is not None
    knowledge = service.store.project_state_snapshot(harness.project_id)
    rebuilt = service.rebuild(
        harness.project_id,
        ProjectKbRebuildRequest(
            expected_project_revision=project_revision,
            expected_project_fingerprint=project_fingerprint,
            expected_scope_state_revision=knowledge.scope.state_revision,
            expected_knowledge_fingerprint=canonical_json_hash(
                knowledge.fingerprint_material
            ),
        ),
    )
    assert rebuilt.record.manifest is not None
    return rebuilt.record.manifest


def _bind_payload_to_manifest(
    payload: dict[str, object],
    manifest,
) -> None:
    payload["project_kb_state_id"] = manifest.state_id
    payload["project_kb_manifest_hash"] = manifest.manifest_hash


def test_chat_context_uses_one_snapshot_reads_project_truth_and_complete_history(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_inspect = harness.reader.inspect
    inspect_calls: list[Path] = []

    def inspect(root: str | Path):
        inspect_calls.append(Path(root))
        return real_inspect(root)

    real_completed_history = harness.agent_store.completed_history
    history_limits: list[tuple[int, int, int]] = []

    def completed_history(session_id: str, *, max_runs: int = 20, **kwargs):
        history_limits.append(
            (max_runs, kwargs["max_characters"], kwargs["max_tokens"])
        )
        return real_completed_history(session_id, max_runs=max_runs, **kwargs)

    monkeypatch.setattr(harness.reader, "inspect", inspect)
    monkeypatch.setattr(harness.agent_store, "completed_history", completed_history)
    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            payload=harness.request(profile="chat"),
        )

    assert response.status_code == 201, response.text
    assert inspect_calls == [harness.project] * 7
    assert history_limits == [(8, 12_000, 6_000)]
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.readiness.value == "degraded"
    assert pack.history_through_ordinal == 3
    assert [message.role for message in pack.exact_provider_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    rendered_messages = json.dumps(
        [item.model_dump() for item in pack.exact_provider_messages],
        ensure_ascii=False,
    )
    assert "先确认第三章留下了什么" in rendered_messages
    assert "第三章留下了带血钥匙" in rendered_messages
    assert "这条失败消息不能进入上下文" not in rendered_messages
    assert str(harness.project) not in rendered_messages

    included_titles = {item.title for item in pack.included}
    assert {
        "05_正文内容/第一卷/CH001_定稿.md",
        "05_正文内容/第一卷/CH002_定稿.md",
        "05_正文内容/第一卷/CH003_定稿.md",
        "01_项目定位/定位.md",
        "02_世界观设定/城市.md",
        "03_人物设定/人物.json",
        "04_剧情大纲/主线.md",
        "06_写作规范/风格.md",
        "07_运营数据/project_status.md",
    }.issubset(included_titles)
    assert any(
        item.source_id == "knowledge:distillation"
        and item.exclusion_reason == "distillation_unavailable"
        for item in pack.excluded
    )
    assert pack.pack_hash == response.json()["pack"]["pack_hash"]
    assert pack.provider_messages_hash == response.json()["pack"][
        "provider_messages_hash"
    ]


def test_chapter_context_tracks_history_ordinal_without_injecting_chat(
    harness: Harness,
) -> None:
    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="chapter-without-chat-history",
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.profile == "chapter"
    assert pack.history_through_ordinal == 3
    assert pack.history_message_count == 0
    assert pack.include_session_history is False
    assert [message.role for message in pack.exact_provider_messages] == [
        "system",
        "user",
    ]
    rendered_messages = json.dumps(
        [item.model_dump() for item in pack.exact_provider_messages],
        ensure_ascii=False,
    )
    assert "先确认第三章留下了什么" not in rendered_messages
    assert "第三章留下了带血钥匙" not in rendered_messages


def test_include_session_history_brings_completed_turns_into_chapter_context(
    harness: Harness,
) -> None:
    payload = harness.request()
    payload["include_session_history"] = True
    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="chapter-with-session-history",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.profile == "chapter"
    assert pack.history_through_ordinal == 3
    assert pack.history_message_count == 2
    assert pack.include_session_history is True
    assert [message.role for message in pack.exact_provider_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    rendered_messages = json.dumps(
        [item.model_dump() for item in pack.exact_provider_messages],
        ensure_ascii=False,
    )
    assert "先确认第三章留下了什么" in rendered_messages
    assert "第三章留下了带血钥匙" in rendered_messages
    assert "这条失败消息不能进入上下文" not in rendered_messages


def test_formal_cards_are_injected_while_risk_cards_remain_excluded(
    harness: Harness,
) -> None:
    formal = DistillationSelectedCard(
        card_id="card-formal",
        card_type="scene",
        title="场景压力规则",
        summary="每个场景必须有可观察的压力升级。",
        usage_mode="hard_rule",
        hard_rule_eligible=True,
        safety_risk=0.0,
        tags=["pressure"],
    )
    risk = DistillationSelectedCard(
        card_id="card-risk",
        card_type="voice",
        title="未经验证的声音样本",
        summary="这条内容只能作为风险参考。",
        usage_mode="risk_ready",
        hard_rule_eligible=False,
        safety_risk=0.6,
        tags=["risk"],
    )

    class FakeDistillation:
        def coverage(self, request):
            assert request.profile == "chapter"
            return SimpleNamespace(
                formal_injection_allowed=True,
                library_fingerprint="d" * 64,
                tool_fingerprint="e" * 64,
                selected_cards=[formal, risk],
            )

    service = ContextService(
        harness.context_store,
        harness.agent_store,
        harness.reader,
        ProjectLockRegistry(),
        FakeDistillation(),
        project_kb_service=harness.service.project_kb_service,
    )
    with TestClient(harness.app(service)) as client:
        response = _post_pack(client, harness, key="context-key-cards")
        route_response = client.get(
            f"/api/v1/agent/context-packs/{response.json()['pack']['context_pack_id']}/nlcl-route"
        )
        operation_package_response = client.get(
            f"/api/v1/agent/context-packs/{response.json()['pack']['context_pack_id']}"
            "/prewrite-operation-package"
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert any(item.source_id == "knowledge-card:card-formal" for item in pack.included)
    assert any(
        item.source_id == "knowledge-card:card-risk"
        and item.exclusion_reason == "knowledge_card_risk_only"
        for item in pack.excluded
    )
    assert "这条内容只能作为风险参考" not in pack.exact_provider_messages[0].content
    assert pack.knowledge_fingerprint == canonical_json_hash(
        {"library_fingerprint": "d" * 64, "tool_fingerprint": "e" * 64}
    )
    # NLCL 路由回执不再为对话白建（2026-09-24）：两条只读路由还在，只保证能打开。
    assert route_response.status_code == 200, route_response.text
    assert operation_package_response.status_code == 200

    class GatedDistillation:
        def coverage(self, request):
            del request
            return SimpleNamespace(
                formal_injection_allowed=False,
                library_fingerprint="d" * 64,
                tool_fingerprint="e" * 64,
                selected_cards=[formal],
            )

    gated_service = ContextService(
        harness.context_store,
        harness.agent_store,
        harness.reader,
        ProjectLockRegistry(),
        GatedDistillation(),
        project_kb_service=harness.service.project_kb_service,
    )
    with TestClient(harness.app(gated_service)) as client:
        gated_response = _post_pack(client, harness, key="context-key-gated-card")
        gated_route_response = client.get(
            f"/api/v1/agent/context-packs/{gated_response.json()['pack']['context_pack_id']}/nlcl-route"
        )

    assert gated_response.status_code == 201, gated_response.text
    gated_pack = ContextPack.model_validate(gated_response.json()["pack"])
    assert not any(
        item.source_id == "knowledge-card:card-formal"
        for item in gated_pack.included
    )
    assert any(
        item.source_id == "knowledge-card:card-formal"
        and item.exclusion_reason == "formal_injection_not_allowed"
        for item in gated_pack.excluded
    )
    assert gated_route_response.status_code == 200


def test_missing_active_chapter_produces_a_blocked_pack(harness: Harness) -> None:
    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-missing",
            payload=harness.request(active_chapter_id="VOL001-CH999"),
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.readiness.value == "blocked"
    missing = next(
        item
        for item in pack.excluded
        if item.source_id == "project-chapter:VOL001-CH999"
    )
    assert missing.mandatory is True
    assert missing.exclusion_reason == "active_chapter_not_found"


def test_manual_project_file_is_mandatory_and_reaches_provider_context(
    harness: Harness,
) -> None:
    snapshot = harness.reader.inspect(harness.project)
    selected = next(
        item
        for item in snapshot["files"]
        if item["relative_path"] == "02_世界观设定/城市.md"
    )
    payload = harness.request()
    payload["manual_file_ids"] = [selected["file_id"]]

    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-manual-file",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.manual_file_ids == (selected["file_id"],)
    fragment = next(
        item
        for item in pack.included
        if item.source_id == f"project-file:{selected['file_id']}"
    )
    assert fragment.source_type == "manual_project_file"
    assert fragment.mandatory is True
    assert fragment.title == "02_世界观设定/城市.md"
    assert "旧港区夜间封闭" in pack.exact_provider_messages[0].content


def test_unknown_manual_project_file_is_rejected(harness: Harness) -> None:
    payload = harness.request()
    payload["manual_file_ids"] = ["file-does-not-exist"]

    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-missing-manual-file",
            payload=payload,
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "context_conflict"


def test_new_active_chapter_uses_control_and_outline_without_blocking(
    harness: Harness,
) -> None:
    volume = harness.project / "05_正文内容" / "第一卷"
    (volume / "CH004_控制卡.md").write_text(
        "第四章控制卡：主角必须先验证钥匙来源。",
        encoding="utf-8",
    )
    (volume / "CH004_章纲.md").write_text(
        "第四章章纲：旧港调查受阻，证人线索升级。",
        encoding="utf-8",
    )
    snapshot = harness.reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(snapshot["project"]["fingerprint"]),
    )
    payload = harness.request(active_chapter_id="VOL001-CH004")
    payload["expected_project_revision"] = binding.revision
    payload["expected_project_fingerprint"] = binding.fingerprint
    _bind_payload_to_manifest(
        payload,
        _rebuild_project_kb(
            harness,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint,
        ),
    )

    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-control-only-chapter",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.readiness.value != "blocked"
    materials = [
        item
        for item in pack.included
        if item.source_type == "active_chapter_material"
    ]
    assert [item.title for item in materials] == [
        "05_正文内容/第一卷/CH004_控制卡.md",
        "05_正文内容/第一卷/CH004_章纲.md",
    ]
    assert [item.mandatory for item in materials] == [True, False]
    assert len({item.source_id for item in (*pack.included, *pack.excluded)}) == (
        len(pack.included) + len(pack.excluded)
    )
    assert not any(
        item.source_id == "project-chapter:VOL001-CH004"
        for item in pack.excluded
    )


def test_existing_active_chapter_without_usable_material_remains_blocked(
    harness: Harness,
) -> None:
    volume = harness.project / "05_正文内容" / "第一卷"
    (volume / "CH004_handoff.md").write_text(
        "这只是交接收据，不是可注入的章节材料。",
        encoding="utf-8",
    )
    snapshot = harness.reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(snapshot["project"]["fingerprint"]),
    )
    payload = harness.request(active_chapter_id="VOL001-CH004")
    payload["expected_project_revision"] = binding.revision
    payload["expected_project_fingerprint"] = binding.fingerprint
    _bind_payload_to_manifest(
        payload,
        _rebuild_project_kb(
            harness,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint,
        ),
    )

    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-no-chapter-material",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.readiness.value == "blocked"
    missing = next(
        item
        for item in pack.excluded
        if item.source_id == "project-chapter:VOL001-CH004"
    )
    assert missing.mandatory is True
    assert missing.exclusion_reason == "active_chapter_material_not_found"
    assert not any(
        item.title.endswith("CH004_handoff.md")
        for item in (*pack.included, *pack.excluded)
    )


def test_external_project_mutation_returns_context_conflict(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_inspect = harness.reader.inspect
    calls = 0

    def inspect(root: str | Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (harness.project / "03_人物设定" / "人物.json").write_text(
                '{"主角":"林澈","底线":"已经改变"}',
                encoding="utf-8",
            )
        return real_inspect(root)

    monkeypatch.setattr(harness.reader, "inspect", inspect)
    with TestClient(harness.app()) as client:
        response = _post_pack(client, harness, key="context-key-drift")

    assert calls == 2
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_kb_state_stale"
    assert response.json()["error"]["retryable"] is True
    assert harness.context_store.list_session_packs(harness.session_id) == []


def test_fragment_versions_use_persisted_revision_after_reader_restart(
    harness: Harness,
) -> None:
    (harness.project / "01_项目定位" / "定位.md").write_text(
        "第二版定位：核心承诺仍是寻找失踪证人。",
        encoding="utf-8",
    )
    restarted_reader = ProjectReader([harness.project.parent])
    restarted_snapshot = restarted_reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(restarted_snapshot["project"]["fingerprint"]),
    )
    assert restarted_snapshot["project"]["revision"] == 1
    assert binding.revision == 2

    service = ContextService(
        harness.context_store,
        harness.agent_store,
        restarted_reader,
        ProjectLockRegistry(),
        project_kb_service=harness.service.project_kb_service,
    )
    payload = harness.request()
    payload["expected_project_revision"] = binding.revision
    payload["expected_project_fingerprint"] = binding.fingerprint
    _bind_payload_to_manifest(
        payload,
        _rebuild_project_kb(
            harness,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint,
        ),
    )
    with TestClient(harness.app(service)) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-reader-restart",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert pack.project_revision == 2
    assert {
        item.version
        for item in pack.included
        if item.authority.value == "project_truth"
    } == {"project-revision-2"}


def test_candidate_limit_is_reported_without_exceeding_compiler_hard_limit(
    harness: Harness,
) -> None:
    section = harness.project / "01_项目定位"
    for index in range(250):
        (section / f"补充-{index:03d}.md").write_text(
            f"项目定位补充 {index}",
            encoding="utf-8",
        )
    snapshot = harness.reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(snapshot["project"]["fingerprint"]),
    )
    payload = harness.request()
    payload["expected_project_revision"] = binding.revision
    payload["expected_project_fingerprint"] = binding.fingerprint
    _bind_payload_to_manifest(
        payload,
        _rebuild_project_kb(
            harness,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint,
        ),
    )

    with TestClient(harness.app()) as client:
        response = _post_pack(
            client,
            harness,
            key="context-key-candidate-limit",
            payload=payload,
        )

    assert response.status_code == 201, response.text
    pack = ContextPack.model_validate(response.json()["pack"])
    assert len(pack.included) + len(pack.excluded) == 256
    limit = next(
        item for item in pack.excluded if item.source_id == "context:candidate-limit"
    )
    assert limit.exclusion_reason == "candidate_limit_exceeded"
    assert pack.readiness.value == "degraded"


def test_idempotent_replay_rejects_stale_project_kb_until_explicit_rebuild(
    harness: Harness,
) -> None:
    payload = harness.request()
    with TestClient(harness.app()) as client:
        created = _post_pack(
            client,
            harness,
            key="context-key-stateful-replay",
            payload=payload,
        )
    assert created.status_code == 201, created.text

    (harness.project / "04_剧情大纲" / "主线.md").write_text(
        "主线已经进入第二版。",
        encoding="utf-8",
    )
    changed = harness.reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(changed["project"]["fingerprint"]),
    )
    with TestClient(harness.app()) as client:
        replayed = _post_pack(
            client,
            harness,
            key="context-key-stateful-replay",
            payload=payload,
        )
        old_pack = client.get(
            f"/api/v1/agent/context-packs/{created.json()['pack']['context_pack_id']}"
        )

    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["error"]["code"] == "project_kb_state_stale"
    assert replayed.json()["error"]["retryable"] is True
    assert old_pack.status_code == 200, old_pack.text
    assert old_pack.json()["pack"]["pack_hash"] == created.json()["pack"]["pack_hash"]

    project_kb = harness.service.project_kb_service
    assert project_kb is not None
    knowledge = project_kb.store.project_state_snapshot(harness.project_id)
    rebuilt = project_kb.rebuild(
        harness.project_id,
        ProjectKbRebuildRequest(
            expected_project_revision=binding.revision,
            expected_project_fingerprint=binding.fingerprint,
            expected_scope_state_revision=knowledge.scope.state_revision,
            expected_knowledge_fingerprint=canonical_json_hash(
                knowledge.fingerprint_material
            ),
        ),
    )
    assert rebuilt.record.manifest is not None
    fresh_payload = {
        **payload,
        "expected_project_revision": binding.revision,
        "expected_project_fingerprint": binding.fingerprint,
        "project_kb_state_id": rebuilt.record.manifest.state_id,
        "project_kb_manifest_hash": rebuilt.record.manifest.manifest_hash,
    }
    with TestClient(harness.app()) as client:
        recompiled = _post_pack(
            client,
            harness,
            key="context-key-stateful-recompiled",
            payload=fresh_payload,
        )

    assert recompiled.status_code == 201, recompiled.text
    assert recompiled.json()["pack"]["context_pack_id"] != created.json()["pack"][
        "context_pack_id"
    ]


def test_idempotent_replay_rejects_stale_knowledge_until_explicit_rebuild(
    harness: Harness,
) -> None:
    class MutableDistillation:
        def __init__(self) -> None:
            self.library_fingerprint = "d" * 64
            self.calls = 0

        def coverage(self, request):
            del request
            self.calls += 1
            return SimpleNamespace(
                formal_injection_allowed=True,
                library_fingerprint=self.library_fingerprint,
                tool_fingerprint="e" * 64,
                selected_cards=[],
            )

    distillation = MutableDistillation()
    service = ContextService(
        harness.context_store,
        harness.agent_store,
        harness.reader,
        ProjectLockRegistry(),
        distillation,
        project_kb_service=harness.service.project_kb_service,
    )
    with TestClient(harness.app(service)) as client:
        created = _post_pack(
            client,
            harness,
            key="context-key-knowledge-replay",
        )
        with harness.database.connection() as connection:
            connection.execute(
                "UPDATE knowledge_scope SET state_revision = state_revision + 1 "
                "WHERE project_binding_id = ?",
                (harness.project_id,),
            )
        replayed = _post_pack(
            client,
            harness,
            key="context-key-knowledge-replay",
        )

    assert created.status_code == 201, created.text
    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["error"]["code"] == "project_kb_state_stale"
    assert replayed.json()["error"]["retryable"] is True
    assert distillation.calls == 1

    project_kb = service.project_kb_service
    assert project_kb is not None
    knowledge = project_kb.store.project_state_snapshot(harness.project_id)
    rebuilt = project_kb.rebuild(
        harness.project_id,
        ProjectKbRebuildRequest(
            expected_project_revision=harness.revision,
            expected_project_fingerprint=harness.fingerprint,
            expected_scope_state_revision=knowledge.scope.state_revision,
            expected_knowledge_fingerprint=canonical_json_hash(
                knowledge.fingerprint_material
            ),
        ),
    )
    assert rebuilt.record.manifest is not None
    fresh_payload = {
        **harness.request(),
        "project_kb_state_id": rebuilt.record.manifest.state_id,
        "project_kb_manifest_hash": rebuilt.record.manifest.manifest_hash,
    }
    with TestClient(harness.app(service)) as client:
        recompiled = _post_pack(
            client,
            harness,
            key="context-key-knowledge-recompiled",
            payload=fresh_payload,
        )

    assert recompiled.status_code == 201, recompiled.text
    assert distillation.calls == 2
    assert recompiled.json()["pack"]["pack_hash"] != created.json()["pack"]["pack_hash"]


def test_project_reader_errors_are_path_redacted(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        harness.reader,
        "inspect",
        lambda _root: (_ for _ in ()).throw(
            UntrustedProjectPathError(f"escaped trusted root: {harness.project}")
        ),
    )
    with TestClient(harness.app()) as client:
        response = _post_pack(client, harness, key="context-key-redacted-error")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_kb_state_stale"
    assert response.json()["error"]["retryable"] is True
    assert str(harness.project) not in response.text


def test_idempotency_retrieval_run_binding_and_restart_recovery(
    harness: Harness,
) -> None:
    app = harness.app()
    with TestClient(app) as client:
        created = _post_pack(client, harness, key="context-key-replay")
        replayed = _post_pack(client, harness, key="context-key-replay")
        changed_payload = harness.request(objective="换一个完全不同的写作目标。")
        conflict = _post_pack(
            client,
            harness,
            key="context-key-replay",
            payload=changed_payload,
        )
        missing = client.get("/api/v1/agent/context-packs/context-pack-missing")

    assert created.status_code == 201, created.text
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["pack"]["context_pack_id"] == created.json()["pack"][
        "context_pack_id"
    ]
    assert replayed.json()["pack"]["pack_hash"] == created.json()["pack"]["pack_hash"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "context_conflict"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "context_not_found"

    pack = ContextPack.model_validate(created.json()["pack"])
    run, _, replay = harness.agent_store.create_run(
        session_id=harness.session_id,
        message=pack.objective,
        expected_project_revision=harness.revision,
        expected_project_fingerprint=harness.fingerprint,
        active_chapter_id=pack.active_chapter_id,
        idempotency_key="run-with-context-pack",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
    )
    assert replay is False

    reopened_database = SqliteStore(harness.database.path)
    reopened_database.initialize()
    reopened_service = ContextService(
        ContextStore(reopened_database),
        AgentStore(reopened_database),
        harness.reader,
        ProjectLockRegistry(),
    )
    reopened_app = FastAPI()
    install_exception_handlers(reopened_app)
    reopened_app.include_router(create_context_router(reopened_service))
    with TestClient(reopened_app) as client:
        by_id = client.get(f"/api/v1/agent/context-packs/{pack.context_pack_id}")
        listed = client.get(
            f"/api/v1/agent/sessions/{harness.session_id}/context-packs"
        )
        by_run = client.get(f"/api/v1/agent/runs/{run.run_id}/context-pack")

    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["consumed_by_run_id"] == run.run_id
    assert listed.status_code == 200, listed.text
    assert [item["pack"]["context_pack_id"] for item in listed.json()["context_packs"]] == [
        pack.context_pack_id
    ]
    assert by_run.status_code == 200, by_run.text
    assert by_run.json()["pack"]["pack_hash"] == pack.pack_hash
    assert by_run.json()["consumed_by_run_id"] == run.run_id


def test_history_opt_in_is_part_of_context_idempotency(harness: Harness) -> None:
    without_history = harness.request()
    with_history = harness.request()
    with_history["include_session_history"] = True

    with TestClient(harness.app()) as client:
        created_without = _post_pack(
            client,
            harness,
            key="history-opt-in-conflict-off",
            payload=without_history,
        )
        conflict_on = _post_pack(
            client,
            harness,
            key="history-opt-in-conflict-off",
            payload=with_history,
        )
        created_with = _post_pack(
            client,
            harness,
            key="history-opt-in-conflict-on",
            payload=with_history,
        )
        conflict_off = _post_pack(
            client,
            harness,
            key="history-opt-in-conflict-on",
            payload=without_history,
        )

    assert created_without.status_code == 201, created_without.text
    assert created_without.json()["pack"]["include_session_history"] is False
    assert conflict_on.status_code == 409
    assert conflict_on.json()["error"]["code"] == "context_conflict"
    assert created_with.status_code == 201, created_with.text
    assert created_with.json()["pack"]["include_session_history"] is True
    assert conflict_off.status_code == 409
    assert conflict_off.json()["error"]["code"] == "context_conflict"


def test_context_api_requires_idempotency_key_and_reports_unavailable(
    harness: Harness,
) -> None:
    unavailable_app = FastAPI()
    install_exception_handlers(unavailable_app)
    unavailable_app.include_router(
        create_context_router(None, service_error="Context startup failed")
    )
    with TestClient(harness.app()) as client:
        missing_header = client.post(
            f"/api/v1/agent/sessions/{harness.session_id}/context-packs",
            json=harness.request(),
        )
    with TestClient(unavailable_app) as client:
        unavailable = client.get("/api/v1/agent/context-packs/any")

    assert missing_header.status_code == 422
    assert missing_header.json()["error"]["code"] == "request_validation_failed"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "context_unavailable"


def test_chat_pack_with_chapter_objective_and_unsectioned_root_files(
    harness: Harness,
) -> None:
    """回归：目标里点了章号 + 书根有非 0N_ 前缀的散文件（section_index 为
    None），装配不能再崩成 503（int(None) TypeError，2026-09-22 T9 验收在
    测试副本_宗门七骗子 上抓到：「把第 3 章改名成…」直接 503）。"""
    (harness.project / "项目审稿报告_自动体检.md").write_text(
        "审稿记录。\n", encoding="utf-8"
    )
    snapshot = harness.reader.inspect(harness.project)
    binding = harness.database.sync_project_binding(
        project_id=harness.project_id,
        root_path=str(harness.project),
        display_name="novel",
        fingerprint=str(snapshot["project"]["fingerprint"]),
    )
    manifest = _rebuild_project_kb(
        harness,
        project_revision=binding.revision,
        project_fingerprint=binding.fingerprint,
    )
    payload = harness.request(
        objective="把第 3 章改名成 第 003 章 试剑",
        active_chapter_id=None,
        profile="chat",
    )
    payload["expected_project_revision"] = binding.revision
    payload["expected_project_fingerprint"] = binding.fingerprint
    _bind_payload_to_manifest(payload, manifest)

    with TestClient(harness.app()) as client:
        response = _post_pack(client, harness, payload=payload)

    assert response.status_code == 201, response.text
