from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import (
    ContextProviderMessage,
    canonical_json_hash,
    context_provider_messages_hash,
    knowledge_source_fingerprint,
)
from zhimeng_runtime.context_store import ContextStoreError
from zhimeng_runtime.distillation_models import DistillationSelectedCard
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    nlcl_route_receipt_hash,
)
from zhimeng_runtime.prewrite_models import build_prewrite_operation_package
from zhimeng_runtime.project_reader import SECTION_LABELS
from zhimeng_runtime.skill_executor import SkillExecutionOutcome
from zhimeng_runtime.skill_models import SkillExecutionReceipt
from zhimeng_runtime.source_import_receipt_models import SourceImportExecutionReceipt

from test_knowledge_api import (
    _draft_cards,
    _finalize_source_batch,
    _import_candidate_batch,
    _review_and_promote_work_candidate,
)
from test_source_import_api import (
    FakeTomatoAdapter,
    _artifact as _source_artifact,
    _wait_for_status as _wait_for_source_import_status,
)


LIBRARY_FINGERPRINT = "d" * 64
TOOL_FINGERPRINT = "e" * 64
FULL_CHAIN_CARD_ID = "work-source-to-prewrite-e2e"


def _candidate_submission_arguments(
    messages: Sequence[Mapping[str, Any]],
    *,
    title: str = "受管 Workflow 候选",
) -> dict[str, Any]:
    """Build Provider-owned content while Runtime supplies immutable references."""

    writer_dispatch: dict[str, Any] | None = None
    instruction_dispatch: dict[str, Any] | None = None
    for message in messages:
        if message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schema") == "zhimeng.writer-task-dispatch.v1":
            writer_dispatch = payload
        elif payload.get("schema") == "zhimeng.creation-instruction-dispatch.v1":
            instruction_dispatch = payload
    if writer_dispatch is None or instruction_dispatch is None:
        raise AssertionError("Runtime Writer instruction dispatch was not exposed")
    assert writer_dispatch["runtime_adds_writer_task_reference"] is True
    assert instruction_dispatch["runtime_adds_materialization_reference"] is True
    kind = str(writer_dispatch["required_candidate_kind"])
    if kind == "change_proposal":
        for tool_message in reversed(messages):
            if tool_message.get("role") != "tool":
                continue
            tool_payload = json.loads(str(tool_message.get("content", "{}")))
            output = tool_payload.get("output") if isinstance(tool_payload, dict) else None
            if (
                isinstance(output, dict)
                and output.get("schema") == "zhimeng.tool-result.change-set-propose.v1"
            ):
                break
        else:
            raise AssertionError("Writer change proposal preceded its ChangeSet")
    return {
        "kind": kind,
        "content": {
            "title": title,
            "summary": "受管 Writer 结果已绑定，候选仅供作者审查。",
            "body": (
                "章节改动已保存为 ChangeSet，必须逐项审阅并通过 Gate。"
                if kind == "change_proposal"
                else "先修复上游证据，不生成或写入正式正文。"
            ),
            "format": "markdown",
        },
    }


def _provider_skill_result(
    messages: Sequence[Mapping[str, Any]],
    skill_id: str,
) -> tuple[int, int, dict[str, Any]]:
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for call_index, message in enumerate(messages):
        tool_calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if (
                not isinstance(function, dict)
                or function.get("name") != "skill_execute"
            ):
                continue
            arguments = json.loads(str(function.get("arguments", "{}")))
            if (
                not isinstance(arguments, dict)
                or arguments.get("skill_id") != skill_id
            ):
                continue
            provider_tool_call_id = tool_call.get("id")
            for result_index in range(call_index + 1, len(messages)):
                result_message = messages[result_index]
                if (
                    result_message.get("role") == "tool"
                    and result_message.get("tool_call_id") == provider_tool_call_id
                ):
                    payload = json.loads(str(result_message.get("content", "{}")))
                    if not isinstance(payload, dict):
                        raise AssertionError(
                            "Provider ToolResult payload must be an object"
                        )
                    matches.append((call_index, result_index, payload))
                    break
            else:
                raise AssertionError(f"Missing Provider ToolResult for {skill_id}")
    if len(matches) != 1:
        raise AssertionError(
            f"Expected exactly one Provider ToolResult for {skill_id}"
        )
    return matches[0]


class RecordingProvider:
    def __init__(self) -> None:
        self.received_messages: list[dict[str, str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "c" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.context-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="context-e2e",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.received_messages = [dict(message) for message in messages]
        if cancel_event.is_set():
            return
        if any(tool.name == "candidate_output_submit" for tool in tools):
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_candidate_output_0001",
                tool_name="candidate_output_submit",
                arguments=_candidate_submission_arguments(messages),
            )
            yield ProviderCompleted("tool_calls", None)
            return
        yield ProviderTextDelta("Reviewed context received.")
        yield ProviderCompleted("stop", None)


class ToolCallingProvider:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_catalogs: list[list[str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "d" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.tool-context-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="tool-context-e2e",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if cancel_event.is_set():
            return
        request = [dict(message) for message in messages]
        self.requests.append(request)
        self.tool_catalogs.append([tool.name for tool in tools])
        turn = len(self.requests)
        if any(tool.name == "candidate_output_submit" for tool in tools) and turn >= 4:
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_candidate_output_0004",
                tool_name="candidate_output_submit",
                arguments=_candidate_submission_arguments(request),
            )
            yield ProviderCompleted("tool_calls", None)
            return
        if turn == 1:
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_list_files_0001",
                tool_name="project_list_files",
                arguments={"query": "CH001_正文.md", "limit": 10},
            )
            yield ProviderCompleted("tool_calls", None)
            return

        previous = json.loads(str(request[-1]["content"]))
        assert previous["status"] == "succeeded"
        if turn == 2:
            file_id = previous["output"]["files"][0]["file_id"]
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_read_file_0002",
                tool_name="project_read_file",
                arguments={"file_id": file_id, "start_line": 1, "max_lines": 40},
            )
            yield ProviderCompleted("tool_calls", None)
            return
        if turn == 3:
            file_id = previous["output"]["file_id"]
            assert "blood-stained key" in previous["output"]["content"]
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_propose_change_0003",
                tool_name="change_set_propose",
                arguments={
                    "title": "Tighten the opening clue",
                    "summary": "Keep the key clue while giving the sentence a deadline.",
                    "changes": [
                        {
                            "operation": "update",
                            "file_id": file_id,
                            "after_content": (
                                "Lin finds the blood-stained key before the old port closes."
                            ),
                            "rationale": "Preserve the clue and add immediate pressure.",
                        }
                    ],
                },
            )
            yield ProviderCompleted("tool_calls", None)
            return

        assert previous["output"]["requires_author_review"] is True
        assert previous["output"]["applied"] is False
        assert request[-2]["tool_calls"][0]["id"] == "call_propose_change_0003"
        assert request[-1]["tool_call_id"] == "call_propose_change_0003"
        yield ProviderTextDelta("候选改动已生成，请审阅。")
        yield ProviderCompleted("stop", None)


class SkillCallingProvider:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_catalogs: list[list[str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "e" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.skill-context-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="skill-context-e2e",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if cancel_event.is_set():
            return
        request = [dict(message) for message in messages]
        self.requests.append(request)
        self.tool_catalogs.append([tool.name for tool in tools])
        if any(tool.name == "candidate_output_submit" for tool in tools):
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_candidate_output_0001",
                tool_name="candidate_output_submit",
                arguments=_candidate_submission_arguments(request),
            )
            yield ProviderCompleted("tool_calls", None)
            return
        previous = json.loads(str(request[-1]["content"]))
        assert previous["status"] == "succeeded"
        assert previous["output"]["execution_status"] == "succeeded"
        assert previous["output"]["domain_status"] == "blocked"
        assert previous["output"]["project_unchanged"] is True
        assert previous["output"]["writes_project_truth"] is False
        yield ProviderTextDelta("项目审计已完成，当前存在阻塞项，请先处理审计结果。")
        yield ProviderCompleted("stop", None)


class StableKnowledgeAdapter:
    def __init__(self) -> None:
        self.card = DistillationSelectedCard(
            card_id="card-context-e2e",
            card_type="scene",
            title="Escalating scene pressure",
            summary="Every scene must contain an observable pressure change.",
            usage_mode="hard_rule",
            hard_rule_eligible=True,
            safety_risk=0.0,
            tags=["pressure"],
        )

    def coverage(self, request):
        return SimpleNamespace(
            formal_injection_allowed=True,
            library_fingerprint=LIBRARY_FINGERPRINT,
            tool_fingerprint=TOOL_FINGERPRINT,
            selected_cards=[self.card],
            request=request,
        )

    def observe_knowledge_fingerprint(self) -> str:
        return knowledge_source_fingerprint(
            library_fingerprint=LIBRARY_FINGERPRINT,
            tool_fingerprint=TOOL_FINGERPRINT,
        )


class FullChainTomatoAdapter(FakeTomatoAdapter):
    def draft(self, source, workspace, cancel_event):
        draft = super().draft(source, workspace, cancel_event)
        cards_payload = _draft_cards(FULL_CHAIN_CARD_ID)
        cards_path = Path(draft.cards_path)
        cards_path.write_text(
            json.dumps(cards_payload, ensure_ascii=False),
            encoding="utf-8",
        )

        envelope_path = Path(draft.envelope_path)
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        envelope["checkpoints"]["card_count"] = len(cards_payload)
        envelope_path.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )
        return draft.model_copy(
            update={
                "card_count": len(cards_payload),
                "artifacts": [
                    _source_artifact(artifact.kind, Path(artifact.path))
                    for artifact in draft.artifacts
                ],
            }
        )


def _write_project(root: Path) -> None:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    fixtures = {
        "01_项目定位/position.md": "A long mystery about a missing witness.",
        "02_世界观设定/world.md": "The old port closes after midnight.",
        "03_人物设定/characters.md": "Lin refuses to sacrifice a witness.",
        "04_剧情大纲/plot.md": "Find the witness and expose the old fire.",
        "04_剧情大纲/CH001_控制卡.md": (
            "Goal: recover the witness. Cost: expose the investigator."
        ),
        "06_写作规范/style.md": "Keep dialogue restrained and concrete.",
        "07_运营数据/status.md": "Serialization is active.",
    }
    for relative_path, content in fixtures.items():
        target = root / Path(relative_path)
        target.write_text(content, encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir()
    (volume / "CH001_正文.md").write_text(
        "Lin finds a blood-stained key at the old port.",
        encoding="utf-8",
    )


def _wait_for_terminal(client: TestClient, run_id: str) -> dict[str, object]:
    for _ in range(1000):
        run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.01)
    raise AssertionError("Context E2E Run did not reach a terminal state")


def _publish_project_kb(
    client: TestClient,
    snapshot: dict[str, Any],
) -> dict[str, str]:
    project = snapshot["project"]
    project_id = project["project_id"]
    knowledge_response = client.get(
        f"/api/v1/projects/{project_id}/knowledge/cards"
    )
    assert knowledge_response.status_code == 200, knowledge_response.text
    knowledge = knowledge_response.json()
    rebuild_response = client.post(
        f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "expected_scope_state_revision": knowledge["scope"]["state_revision"],
            "expected_knowledge_fingerprint": knowledge["fingerprint"],
        },
    )
    assert rebuild_response.status_code == 200, rebuild_response.text
    record = rebuild_response.json()["record"]
    assert record["status"] == "current"
    manifest = record["manifest"]
    return {
        "project_kb_state_id": manifest["state_id"],
        "project_kb_manifest_hash": manifest["manifest_hash"],
    }


def _start_tool_run(
    client: TestClient,
    snapshot: dict[str, Any],
    *,
    objective: str,
    key_prefix: str,
    require_skills: bool = False,
    writer_mode: str = "repair_only",
) -> tuple[str, dict[str, Any]]:
    project_id = snapshot["project"]["project_id"]
    session_response = client.post(
        f"/api/v1/projects/{project_id}/agent/sessions",
        json={"title": "Tool Context E2E"},
    )
    assert session_response.status_code == 201, session_response.text
    assert "tool_calls" in session_response.json()["capabilities"]["names"]
    if require_skills:
        assert "skills" in session_response.json()["capabilities"]["names"]
    session_id = session_response.json()["session_id"]
    project_kb_binding = _publish_project_kb(client, snapshot)
    pack_response = client.post(
        f"/api/v1/agent/sessions/{session_id}/context-packs",
        headers={"Idempotency-Key": f"{key_prefix}-context"},
        json={
            "objective": objective,
            "profile": "rewrite",
            "active_chapter_id": "VOL001-CH001",
            "expected_project_revision": snapshot["project"]["revision"],
            "expected_project_fingerprint": snapshot["project"]["fingerprint"],
            **project_kb_binding,
        },
    )
    assert pack_response.status_code == 201, pack_response.text
    pack = pack_response.json()["pack"]
    run_response = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": f"{key_prefix}-turn"},
        json={
            "message": objective,
            "expected_project_revision": pack["project_revision"],
            "expected_project_fingerprint": pack["project_fingerprint"],
            "active_chapter_id": pack["active_chapter_id"],
            "writer_mode": writer_mode,
            "context_pack_id": pack["context_pack_id"],
            "expected_context_pack_hash": pack["pack_hash"],
        },
    )
    assert run_response.status_code == 202, run_response.text
    return run_response.json()["run"]["run_id"], pack


def test_reviewed_context_messages_match_provider_and_injection_receipt(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        project_id = snapshot["project"]["project_id"]
        session_response = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Context E2E"},
        )
        assert session_response.status_code == 201, session_response.text
        session_id = session_response.json()["session_id"]

        objective = "Tighten the chapter opening without changing the key clue."
        project_kb_binding = _publish_project_kb(client, snapshot)
        pack_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/context-packs",
            headers={"Idempotency-Key": "context-e2e-0001"},
            json={
                "objective": objective,
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        )
        assert pack_response.status_code == 201, pack_response.text
        reviewed = pack_response.json()
        assert reviewed["consumed_by_run_id"] is None
        pack = reviewed["pack"]
        reviewed_hash = pack["provider_messages_hash"]

        run_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "turn-context-e2e-0001"},
            json={
                "message": objective,
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": pack["active_chapter_id"],
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["run"]["run_id"]
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        by_run = client.get(f"/api/v1/agent/runs/{run_id}/context-pack")
        assert by_run.status_code == 200, by_run.text
        assert by_run.json()["consumed_by_run_id"] == run_id
        activities = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json()["activities"]
        route_state_response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        )
        assert route_state_response.status_code == 200, route_state_response.text
        route_state = route_state_response.json()
        prewrite_state_response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        )
        assert prewrite_state_response.status_code == 200, prewrite_state_response.text
        prewrite_state = prewrite_state_response.json()
        handoff_state_response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/3"
        )
        assert handoff_state_response.status_code == 200, handoff_state_response.text
        handoff_state = handoff_state_response.json()
        operation_package_response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/prewrite-operation-package"
        )
        assert operation_package_response.status_code == 200
        operation_package = operation_package_response.json()
        assert operation_package["readiness"] == "risk_draft_only"
        assert operation_package["missing_requirements"] == []
        assert any(
            "控制卡" in item["title"]
            for item in operation_package["must_obey_truth"]
        )

    exact_message_count = len(pack["exact_provider_messages"])
    provider_context_messages = tuple(
        ContextProviderMessage.model_validate(message)
        for message in provider.received_messages[:exact_message_count]
    )
    provider_hash = context_provider_messages_hash(provider_context_messages)
    assert provider.received_messages[:exact_message_count] == pack["exact_provider_messages"]
    assert provider_hash == reviewed_hash
    assert provider.received_messages[-8]["tool_calls"][0]["function"]["name"] == "skill_execute"
    provider_audit = json.loads(str(provider.received_messages[-7]["content"]))
    assert provider_audit["status"] == "succeeded"
    assert provider_audit["output"]["domain_status"] == "blocked"
    assert provider_audit["output"]["audit"]["health"] == "blocked"
    prewrite_arguments = json.loads(
        provider.received_messages[-6]["tool_calls"][0]["function"]["arguments"]
    )
    provider_prewrite = json.loads(str(provider.received_messages[-5]["content"]))
    assert prewrite_arguments["skill_id"] == "novel-creation-suite.prewrite-gate"
    assert prewrite_arguments["input"]["operation_package"]["package_hash"] == (
        operation_package["package_hash"]
    )
    assert prewrite_arguments["input"]["project_audit_execution_receipt_hash"] == (
        route_state["receipt"]["receipt_hash"]
    )
    assert prewrite_arguments["input"]["project_audit_skill_receipt_hash"] == (
        route_state["receipt"]["skill_receipt_hash"]
    )
    assert prewrite_arguments["input"]["project_audit_tool_result_hash"] == (
        route_state["receipt"]["tool_result_hash"]
    )
    assert provider_prewrite["status"] == "succeeded"
    assert provider_prewrite["output"]["prewrite_gate"]["gate_status"] == "blocked"
    assert provider_prewrite["output"]["prewrite_gate"]["formal_draft_allowed"] is False
    handoff_arguments = json.loads(
        provider.received_messages[-4]["tool_calls"][0]["function"]["arguments"]
    )
    provider_handoff = json.loads(str(provider.received_messages[-3]["content"]))
    assert handoff_arguments["skill_id"] == "novel-creation-suite.creation-handoff"
    assert handoff_arguments["input"]["workflow_plan_hash"]
    assert handoff_arguments["input"]["knowledge_fingerprint"] == pack[
        "knowledge_fingerprint"
    ]
    assert handoff_arguments["input"]["prewrite_gate_execution_receipt_hash"] == (
        prewrite_state["receipt"]["receipt_hash"]
    )
    assert provider_handoff["status"] == "succeeded"
    assert provider_handoff["output"]["creation_handoff"]["handoff_status"] == "blocked"
    assert provider_handoff["output"]["creation_handoff"]["formal_draft_allowed"] is False
    writer_dispatch = json.loads(str(provider.received_messages[-2]["content"]))
    assert writer_dispatch["schema"] == "zhimeng.writer-task-dispatch.v1"
    assert writer_dispatch["writer_task"]["mode"] == "repair_only"
    assert writer_dispatch["required_candidate_kind"] == "repair_plan"
    assert writer_dispatch["runtime_adds_writer_task_reference"] is True
    instruction_dispatch = json.loads(str(provider.received_messages[-1]["content"]))
    assert instruction_dispatch["schema"] == "zhimeng.creation-instruction-dispatch.v1"
    assert instruction_dispatch["runtime_adds_materialization_reference"] is True
    assert route_state["status"] == "succeeded"
    assert route_state["receipt"]["domain_status"] == "blocked"
    assert route_state["receipt"]["tool_result_hash"]
    assert prewrite_state["status"] == "succeeded"
    assert prewrite_state["receipt"]["domain_status"] == "blocked"
    assert prewrite_state["receipt"]["skill_receipt_hash"] == (
        provider_prewrite["output"]["receipt_hash"]
    )
    assert handoff_state["status"] == "succeeded"
    assert handoff_state["receipt"]["domain_status"] == "blocked"
    assert handoff_state["receipt"]["skill_receipt_hash"] == (
        provider_handoff["output"]["receipt_hash"]
    )

    with app.state.runtime.store.connection() as connection:
        receipt = connection.execute(
            """
            SELECT content_hash, metadata_json FROM artifact
            WHERE run_id = ? AND artifact_type = 'context_injection_receipt'
            """,
            (run_id,),
        ).fetchone()
    assert receipt is not None
    assert str(receipt["content_hash"]) == reviewed_hash
    assert json.loads(str(receipt["metadata_json"]))["provider_messages_hash"] == reviewed_hash
    assert any(
        item["kind"] == "artifact"
        and item["payload"]["artifact_type"] == "context_injection_receipt"
        and item["payload"]["content_hash"] == reviewed_hash
        for item in activities
    )


def test_context_tool_loop_creates_reviewable_changeset_then_apply_and_rollback(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original = chapter_path.read_text(encoding="utf-8")
    provider = ToolCallingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        objective = "Tighten the opening without losing the blood-stained key."
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective=objective,
            key_prefix="tool-e2e-0001",
            writer_mode="author_review_change_proposal",
        )
        runtime_propose_tool_id = app.state.agent_service._runtime_tool_call_id(
            run_id,
            "call_propose_change_0003",
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        writer_task_response = client.get(
            f"/api/v1/agent/runs/{run_id}/writer-task"
        )
        assert writer_task_response.status_code == 200, writer_task_response.text
        writer_task = writer_task_response.json()
        assert writer_task["mode"] == "author_review_change_proposal"
        assert writer_task["formal_draft_allowed"] is False
        assert writer_task["writes_project_truth"] is False
        assert len(writer_task["target_files"]) == 1

        writer_result_response = client.get(
            f"/api/v1/agent/runs/{run_id}/writer-candidate-result"
        )
        assert writer_result_response.status_code == 200, writer_result_response.text
        writer_result = writer_result_response.json()
        assert writer_result["writer_task_id"] == writer_task["writer_task_id"]
        assert writer_result["writer_task_hash"] == writer_task["task_hash"]
        assert writer_result["candidate_kind"] == "change_proposal"
        assert writer_result["formal_draft_allowed"] is False
        assert writer_result["writes_project_truth"] is False

        activities_response = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        )
        assert activities_response.status_code == 200, activities_response.text
        activities = activities_response.json()["activities"]
        tool_calls = [item for item in activities if item["kind"] == "tool_call"]
        tool_results = [item for item in activities if item["kind"] == "tool_result"]
        proposals = [item for item in activities if item["kind"] == "change_proposal"]
        assert [item["payload"]["tool_name"] for item in tool_calls] == [
            "skill_execute",
            "skill_execute",
            "skill_execute",
            "project_list_files",
            "project_read_file",
            "change_set_propose",
            "candidate_output_submit",
        ]
        assert [item["payload"]["status"] for item in tool_results] == [
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert len(proposals) == 1
        change_set_id = proposals[0]["payload"]["proposal_id"]
        assert chapter_path.read_text(encoding="utf-8") == original

        change_set_response = client.get(f"/api/v1/change-sets/{change_set_id}")
        assert change_set_response.status_code == 200, change_set_response.text
        change_set = change_set_response.json()
        assert change_set["origin"] == "agent"
        assert change_set["source_run_id"] == run_id
        assert change_set["source_tool_call_id"] == runtime_propose_tool_id
        assert change_set["status"] == "reviewing"
        hunks = [
            hunk for change in change_set["changes"] for hunk in change["hunks"]
        ]
        reviewed_response = client.patch(
            f"/api/v1/change-sets/{change_set_id}/decisions",
            json={
                "expected_review_revision": change_set["review_revision"],
                "decisions": [
                    {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                    for hunk in hunks
                ],
            },
        )
        assert reviewed_response.status_code == 200, reviewed_response.text
        reviewed = reviewed_response.json()
        gate_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/post-draft-gate",
            json={"expected_review_revision": reviewed["review_revision"]},
        )
        assert gate_response.status_code == 200, gate_response.text
        assert gate_response.json()["post_draft_gate"]["status"] == "passed"
        applied_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/apply",
            json={"expected_review_revision": reviewed["review_revision"]},
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert "before the old port closes" in chapter_path.read_text(encoding="utf-8")
        rolled_back_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rolled_back_response.status_code == 200, rolled_back_response.text
        assert rolled_back_response.json()["status"] == "rolled_back"
        assert chapter_path.read_text(encoding="utf-8") == original

    assert provider.tool_catalogs == [
        [
            "project_list_files",
            "project_read_file",
            "project_search",
            "change_set_propose",
            "candidate_output_submit",
        ]
    ] * 4
    with app.state.runtime.store.connection() as connection:
        provenance = connection.execute(
            "SELECT * FROM change_set_provenance WHERE change_set_id = ?",
            (change_set_id,),
        ).fetchone()
    assert provenance is not None
    assert str(provenance["run_id"]) == run_id
    assert str(provenance["tool_call_id"]) == runtime_propose_tool_id
    assert str(provenance["context_pack_id"]) == pack["context_pack_id"]
    assert str(provenance["project_fingerprint"]) == pack["project_fingerprint"]


def test_context_agent_executes_managed_skill_and_recovers_receipt_chain(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original = chapter_path.read_bytes()
    provider = SkillCallingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        run_id, _ = _start_tool_run(
            client,
            snapshot,
            objective="Audit the project before the next writing task.",
            key_prefix="skill-e2e-0001",
            require_skills=True,
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        recovered = client.get(f"/api/v1/agent/runs/{run_id}/activities")
        assert recovered.status_code == 200, recovered.text
        activities = recovered.json()["activities"]
        relevant = [
            item
            for item in activities
            if item["kind"] in {"tool_call", "skill_call", "artifact", "tool_result"}
            and (
                item["kind"] != "artifact"
                or item["payload"]["artifact_type"]
                in {
                    "skill_execution_receipt",
                    "project_audit",
                    "prewrite_gate",
                    "creation_handoff",
                }
            )
        ]
        assert [item["kind"] for item in relevant] == [
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "tool_call",
            "tool_result",
        ]
        causal = [
            item
            for item in activities
            if item in relevant
                or (
                    item["kind"] == "artifact"
                    and item["payload"]["artifact_type"]
                    in {
                        "writer_task",
                        "creation_instruction_materialization",
                        "creation_instruction_dispatch_receipt",
                    }
                )
            or (
                    item["kind"] == "run_state"
                    and (
                        item["payload"]["detail"].startswith("Workflow step ")
                        or item["payload"]["detail"]
                        == "Provider boundary and instruction dispatch receipt persisted"
                    )
            )
        ]
        assert [item["kind"] for item in causal] == [
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "run_state",
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "run_state",
            "tool_call",
            "skill_call",
            "artifact",
            "artifact",
            "tool_result",
            "run_state",
            "artifact",
            "artifact",
            "artifact",
            "run_state",
            "tool_call",
            "tool_result",
        ]
        for previous, current in zip(causal, causal[1:]):
            assert current["causation_id"] == previous["activity_id"]

        skill_call = relevant[1]["payload"]
        receipt = skill_call["output"]["receipt"]
        assert skill_call["skill_id"] == "novel-creation-suite.project-audit"
        assert skill_call["status"] == "succeeded"
        assert receipt["execution_status"] == "succeeded"
        assert receipt["domain_status"] == "blocked"
        assert receipt["package_fingerprint"]
        assert receipt["project_fingerprint"] == snapshot["project"]["fingerprint"]
        prewrite_call = relevant[6]["payload"]
        prewrite_receipt = prewrite_call["output"]["receipt"]
        assert prewrite_call["skill_id"] == "novel-creation-suite.prewrite-gate"
        assert prewrite_call["status"] == "succeeded"
        assert prewrite_receipt["execution_status"] == "succeeded"
        assert prewrite_receipt["domain_status"] == "blocked"
        assert prewrite_call["input"]["project_audit_skill_receipt_hash"] == (
            relevant[4]["payload"]["output"]["receipt_hash"]
        )
        assert prewrite_call["input"]["project_audit_tool_result_hash"]
        assert relevant[9]["payload"]["output"]["prewrite_gate"][
            "formal_draft_allowed"
        ] is False
        handoff_call = relevant[11]["payload"]
        handoff_receipt = handoff_call["output"]["receipt"]
        assert handoff_call["skill_id"] == "novel-creation-suite.creation-handoff"
        assert handoff_call["status"] == "succeeded"
        assert handoff_receipt["execution_status"] == "succeeded"
        assert handoff_receipt["domain_status"] == "blocked"
        assert handoff_call["input"]["prewrite_gate_skill_receipt_hash"] == (
            relevant[9]["payload"]["output"]["receipt_hash"]
        )
        assert handoff_call["input"]["prewrite_gate_formal_draft_allowed"] is False
        assert relevant[14]["payload"]["status"] == "succeeded"
        assert relevant[14]["payload"]["output"]["domain_status"] == "blocked"
        assert relevant[14]["payload"]["output"]["creation_handoff"][
            "formal_draft_allowed"
        ] is False
        assert relevant[15]["payload"]["tool_name"] == "candidate_output_submit"
        assert relevant[16]["payload"]["status"] == "succeeded"
        assert relevant[16]["payload"]["output"]["accepted"] is True

        refreshed = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["project"]["revision"] == snapshot["project"]["revision"]
        assert (
            refreshed.json()["project"]["fingerprint"]
            == snapshot["project"]["fingerprint"]
        )

    assert chapter_path.read_bytes() == original
    assert len(provider.requests) == 1
    assert provider.tool_catalogs == [
        [
            "project_list_files",
            "project_read_file",
            "project_search",
            "candidate_output_submit",
        ]
    ]
    with app.state.runtime.store.connection() as connection:
        artifact_rows = connection.execute(
            "SELECT artifact_type, content_hash FROM artifact WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    assert {str(row["artifact_type"]) for row in artifact_rows} >= {
        "context_injection_receipt",
        "skill_execution_receipt",
        "project_audit",
        "prewrite_gate",
        "creation_handoff",
    }
    assert all(len(str(row["content_hash"])) == 64 for row in artifact_rows)
    skill_runs = tmp_path / "skill-runs"
    assert not skill_runs.exists() or list(skill_runs.iterdir()) == []


def test_verified_source_to_prewrite_chain_recovers_without_source_adapter(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original = chapter_path.read_bytes()
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,),
        allowed_origins=(),
        sse_heartbeat_seconds=0.05,
    )
    provider = SkillCallingProvider()

    with TestClient(
        create_app(
            settings,
            source_import_adapter=FullChainTomatoAdapter(),
            agent_provider=provider,
        )
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "tomato",
                "query": "玄幻升级来源书",
                "candidate": {
                    "index": 1,
                    "book_id": "9001",
                    "title": "玄幻升级来源书",
                    "author": "测试作者",
                },
            },
        )
        assert created.status_code == 202, created.text
        source_import_id = created.json()["import_id"]
        awaiting_review = _wait_for_source_import_status(
            client,
            source_import_id,
            "awaiting_review",
        )
        assert awaiting_review["distillation_draft"]["card_count"] == 3

        source_receipt_response = client.get(
            f"/api/v1/source-imports/{source_import_id}/execution-receipt"
        )
        assert source_receipt_response.status_code == 200, source_receipt_response.text
        source_receipt_record = source_receipt_response.json()
        assert source_receipt_record["lineage_status"] == "verified"
        source_receipt = SourceImportExecutionReceipt.model_validate(
            source_receipt_record["receipt"]
        )

        candidate_batch = _import_candidate_batch(client, source_import_id)
        pending = next(
            item
            for item in candidate_batch["candidates"]
            if item["card_id"] == FULL_CHAIN_CARD_ID
        )
        immutable_origin = pending["origin"]
        assert pending["lineage_status"] == "verified"
        assert immutable_origin["source_receipt_id"] == source_receipt.receipt_id
        assert immutable_origin["source_receipt_hash"] == source_receipt.receipt_hash
        assert immutable_origin["source_packet_fingerprint"] == (
            source_receipt.source_packet_fingerprint
        )
        assert immutable_origin["cards_draft_hash"] == source_receipt.cards_draft_hash

        staged_card, candidate_batch = _review_and_promote_work_candidate(
            client,
            source_import_id,
            candidate_batch,
            card_id=FULL_CHAIN_CARD_ID,
            promise=(
                "玄幻升级章节必须完成阶段兑现，让选择产生可见代价，"
                "并把本章结果变成下一阶段更高压力。"
            ),
            replace_active=False,
        )
        promoted_candidate = next(
            item
            for item in candidate_batch["candidates"]
            if item["card_id"] == FULL_CHAIN_CARD_ID
        )
        assert promoted_candidate["status"] == "promoted"
        assert promoted_candidate["origin"] == immutable_origin
        staged_version = next(
            item
            for item in staged_card["versions"]
            if item["version_id"] == promoted_candidate["promoted_version_id"]
        )
        lineage = staged_version["lineage"]
        assert staged_version["lineage_status"] == "verified"
        assert staged_version["formalization_status"] == "awaiting_formalization"
        assert lineage["candidate_id"] == promoted_candidate["candidate_id"]
        assert lineage["candidate_origin"] == immutable_origin
        assert lineage["activation_mode"] == "initial"

        finalized_response = _finalize_source_batch(
            client,
            source_import_id,
            source_receipt,
            candidate_batch["candidate_set_hash"],
        )
        assert finalized_response.status_code == 200, finalized_response.text
        finalized_receipt = finalized_response.json()
        assert finalized_receipt["source_receipt_hash"] == source_receipt.receipt_hash
        promoted_decision = next(
            item
            for item in finalized_receipt["decisions"]
            if item["candidate_id"] == promoted_candidate["candidate_id"]
        )
        assert promoted_decision["decision"] == "promoted"
        assert promoted_decision["version_lineage_receipt_hash"] == lineage["receipt_hash"]

        completed_source = client.get(
            f"/api/v1/source-imports/{source_import_id}"
        ).json()
        assert completed_source["status"] == "completed"
        formalization_record = client.get(
            f"/api/v1/knowledge/source-imports/{source_import_id}/formalization"
        ).json()
        assert formalization_record["status"] == "verified"
        assert formalization_record["receipt"] == finalized_receipt
        cards_record = client.get("/api/v1/knowledge/cards").json()
        formal_card = next(
            item
            for item in cards_record["cards"]
            if item["card_id"] == FULL_CHAIN_CARD_ID
        )
        formal_version = formal_card["active_version"]
        assert formal_version["version_id"] == staged_version["version_id"]
        assert formal_version["formalization_status"] == "verified"
        assert formal_version["formalization"]["formalization_receipt_hash"] == (
            finalized_receipt["receipt_hash"]
        )

        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        objective = "玄幻升级章节要完成阶段兑现，并让选择留下更高层债务。"
        run_id, pack = _start_tool_run(
            client,
            inspection.json(),
            objective=objective,
            key_prefix="source-prewrite-chain-0001",
            require_skills=True,
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal
        assert terminal["error"] is None
        candidate_response = client.get(
            f"/api/v1/agent/runs/{run_id}/candidate-output"
        )
        assert candidate_response.status_code == 200, candidate_response.text
        assert candidate_response.json()["run_id"] == run_id

        pack_record = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
        ).json()
        route_receipt = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}/nlcl-route"
        ).json()
        operation_package = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/prewrite-operation-package"
        ).json()
        audit_execution = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        ).json()
        prewrite_execution = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        ).json()
        handoff_execution = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/3"
        ).json()
        run_record = client.get(f"/api/v1/agent/runs/{run_id}").json()
        activities_record = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json()

        native_fragment = next(
            item
            for item in pack_record["pack"]["included"]
            if item["source_id"] == f"knowledge-card:{FULL_CHAIN_CARD_ID}"
        )
        provenance = native_fragment["knowledge_provenance"]
        assert provenance == {
            "schema": "zhimeng.knowledge-provenance-ref.v1",
            "source_import_id": source_import_id,
            "source_receipt_id": source_receipt.receipt_id,
            "source_receipt_hash": source_receipt.receipt_hash,
            "source_packet_fingerprint": source_receipt.source_packet_fingerprint,
            "cards_draft_hash": source_receipt.cards_draft_hash,
            "candidate_id": promoted_candidate["candidate_id"],
            "candidate_origin_hash": immutable_origin["origin_hash"],
            "card_id": FULL_CHAIN_CARD_ID,
            "version_id": formal_version["version_id"],
            "version_content_hash": formal_version["content_hash"],
            "version_lineage_receipt_id": lineage["lineage_receipt_id"],
            "version_lineage_receipt_hash": lineage["receipt_hash"],
            "formalization_receipt_id": finalized_receipt[
                "formalization_receipt_id"
            ],
            "formalization_receipt_hash": finalized_receipt["receipt_hash"],
        }
        selected_card = next(
            item
            for item in route_receipt["selected_cards"]
            if item["card_id"] == FULL_CHAIN_CARD_ID
        )
        assert route_receipt["source"] == "native_knowledge"
        assert selected_card["knowledge_provenance"] == provenance
        package_evidence = next(
            item
            for item in operation_package["relevant_evidence_refs"]
            if item["source_id"] == f"knowledge-card:{FULL_CHAIN_CARD_ID}"
        )
        assert package_evidence["knowledge_provenance"] == provenance
        assert operation_package["style_output_contract"][
            "selected_card_provenance"
        ] == [provenance]
        assert operation_package["formal_draft_allowed"] is False

        assert audit_execution["status"] == "succeeded"
        assert audit_execution["receipt"]["skill_id"] == (
            "novel-creation-suite.project-audit"
        )
        assert audit_execution["receipt"]["skill_receipt_hash"]
        assert prewrite_execution["status"] == "succeeded"
        assert prewrite_execution["receipt"]["skill_id"] == (
            "novel-creation-suite.prewrite-gate"
        )
        assert prewrite_execution["receipt"]["skill_receipt_hash"]
        assert handoff_execution["status"] == "succeeded"
        assert handoff_execution["receipt"]["skill_id"] == (
            "novel-creation-suite.creation-handoff"
        )
        assert handoff_execution["receipt"]["skill_receipt_hash"]
        assert activities_record["last_sequence"] == run_record["run"][
            "last_activity_sequence"
        ]
        skill_calls = [
            item
            for item in activities_record["activities"]
            if item["kind"] == "skill_call"
        ]
        assert [item["payload"]["skill_id"] for item in skill_calls] == [
            "novel-creation-suite.project-audit",
            "novel-creation-suite.prewrite-gate",
            "novel-creation-suite.creation-handoff",
        ]

        source_list = client.get("/api/v1/source-imports").json()
        source_receipt_record = client.get(
            f"/api/v1/source-imports/{source_import_id}/execution-receipt"
        ).json()
        candidates_record = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": source_import_id},
        ).json()

    assert chapter_path.read_bytes() == original
    assert len(provider.requests) == 1

    with TestClient(
        create_app(settings, agent_provider=RecordingProvider())
    ) as restarted_client:
        assert restarted_client.get(
            f"/api/v1/source-imports/{source_import_id}"
        ).json() == completed_source
        assert restarted_client.get("/api/v1/source-imports").json() == source_list
        assert restarted_client.get(
            f"/api/v1/source-imports/{source_import_id}/execution-receipt"
        ).json() == source_receipt_record
        assert restarted_client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": source_import_id},
        ).json() == candidates_record
        assert restarted_client.get(
            f"/api/v1/knowledge/source-imports/{source_import_id}/formalization"
        ).json() == formalization_record
        assert restarted_client.get("/api/v1/knowledge/cards").json() == cards_record
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
        ).json() == pack_record
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}/nlcl-route"
        ).json() == route_receipt
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/prewrite-operation-package"
        ).json() == operation_package
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        ).json() == audit_execution
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        ).json() == prewrite_execution
        assert restarted_client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/3"
        ).json() == handoff_execution
        assert restarted_client.get(
            f"/api/v1/agent/runs/{run_id}"
        ).json() == run_record
        assert restarted_client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json() == activities_record


def test_route_execution_reconciles_after_first_receipt_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )
    route_store = app.state.context_service.store
    original_reconcile = route_store.reconcile_route_execution
    reconcile_calls = 0

    def fail_first_reconcile(*args, **kwargs):
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 1:
            raise ContextStoreError("simulated first receipt write failure")
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(route_store, "reconcile_route_execution", fail_first_reconcile)

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective="Audit and explain the current Creation blockers.",
            key_prefix="route-reconcile-0001",
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal
        route_state = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        ).json()
        prewrite_state = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        ).json()

    assert reconcile_calls == 4
    assert route_state["status"] == "succeeded"
    assert route_state["receipt"]["run_id"] == run_id
    assert route_state["receipt"]["domain_status"] == "blocked"
    audit_call_index, audit_result_index, audit_result = _provider_skill_result(
        provider.received_messages,
        "novel-creation-suite.project-audit",
    )
    prewrite_call_index, prewrite_result_index, prewrite_result = (
        _provider_skill_result(
            provider.received_messages,
            "novel-creation-suite.prewrite-gate",
        )
    )
    assert audit_call_index < audit_result_index < prewrite_call_index
    assert prewrite_call_index < prewrite_result_index
    assert audit_result["output"]["receipt_hash"] == route_state["receipt"][
        "skill_receipt_hash"
    ]
    assert prewrite_state["status"] == "succeeded"
    assert prewrite_state["receipt"]["domain_status"] == "blocked"
    assert prewrite_result["output"]["receipt_hash"] == prewrite_state["receipt"][
        "skill_receipt_hash"
    ]


def test_prewrite_skill_failure_preserves_second_execution_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        skill_executor = app.state.agent_service.tool_executor.skill_executor
        assert skill_executor is not None
        original_execute = skill_executor.execute

        async def fail_prewrite(**kwargs):
            outcome = await original_execute(**kwargs)
            if kwargs["skill_id"] != "novel-creation-suite.prewrite-gate":
                return outcome
            receipt_payload = outcome.receipt.model_dump(mode="python", by_alias=True)
            receipt_payload.update(
                {
                    "output_hash": canonical_json_hash({}),
                    "execution_status": "failed",
                    "domain_status": None,
                    "exit_code": 2,
                    "artifacts": [],
                    "error": {
                        "code": "fixture_prewrite_failure",
                        "message": "Simulated prewrite adapter failure.",
                        "retryable": False,
                    },
                }
            )
            failed_receipt = SkillExecutionReceipt.model_validate(
                receipt_payload,
                strict=True,
            )
            return SkillExecutionOutcome(
                receipt=failed_receipt,
                receipt_hash=canonical_json_hash(failed_receipt),
                result={},
                result_summary="Simulated prewrite adapter failure.",
                artifacts=(),
            )

        monkeypatch.setattr(skill_executor, "execute", fail_prewrite)
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective="Audit the project and preserve a failed prewrite receipt.",
            key_prefix="prewrite-failure-0001",
        )
        terminal = _wait_for_terminal(client, run_id)
        audit_state = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        ).json()
        prewrite_state = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        ).json()

    assert terminal["status"] == "failed"
    assert terminal["error"]["error"]["code"] == (
        "agent_skill_fixture_prewrite_failure"
    )
    assert audit_state["status"] == "succeeded"
    assert prewrite_state["status"] == "failed"
    assert prewrite_state["receipt"]["execution_status"] == "failed"
    assert prewrite_state["receipt"]["skill_receipt_id"]
    assert prewrite_state["receipt"]["skill_receipt_hash"]
    assert provider.received_messages == []
    with app.state.runtime.store.connection() as connection:
        receipt_artifact_count = connection.execute(
            """
            SELECT COUNT(*) FROM artifact
            WHERE run_id = ? AND artifact_type = 'skill_execution_receipt'
            """,
            (run_id,),
        ).fetchone()[0]
    assert receipt_artifact_count == 2


def test_missing_managed_skill_catalog_fails_before_provider_io(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            managed_skill_root=None,
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = snapshot["project"]["project_id"]
        session = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Missing managed catalog"},
        ).json()
        objective = "Audit the project before any prose work."
        project_kb_binding = _publish_project_kb(client, snapshot)
        pack = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/context-packs",
            headers={"Idempotency-Key": "route-catalog-missing-0001-context"},
            json={
                "objective": objective,
                "profile": "rewrite",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        ).json()["pack"]
        run_response = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/runs",
            headers={"Idempotency-Key": "route-catalog-missing-0001-turn"},
            json={
                "message": objective,
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": pack["active_chapter_id"],
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        runs = client.get(
            f"/api/v1/agent/sessions/{session['session_id']}/runs"
        ).json()["runs"]

    assert run_response.status_code == 503
    assert run_response.json()["error"]["code"] == "agent_unavailable"
    assert runs == []
    assert app.state.context_service.store.get_pack(
        pack["context_pack_id"]
    ).consumed_by_run_id is None
    assert provider.received_messages == []


def test_tampered_route_receipt_fails_before_provider_io(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = snapshot["project"]["project_id"]
        session = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Tampered route fixture"},
        ).json()
        objective = "Audit this project before Provider I/O."
        project_kb_binding = _publish_project_kb(client, snapshot)
        pack = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/context-packs",
            headers={"Idempotency-Key": "route-tamper-context-0001"},
            json={
                "objective": objective,
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        ).json()["pack"]
        with app.state.runtime.store.connection() as connection:
            connection.execute(
                "UPDATE nlcl_route_receipt SET payload_json = '{}' WHERE context_pack_id = ?",
                (pack["context_pack_id"],),
            )
        response = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/runs",
            headers={"Idempotency-Key": "route-tamper-turn-0001"},
            json={
                "message": objective,
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": pack["active_chapter_id"],
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        runs = client.get(
            f"/api/v1/agent/sessions/{session['session_id']}/runs"
        ).json()["runs"]

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "nlcl_route_integrity_failed"
    assert runs == []
    assert app.state.context_service.store.get_pack(
        pack["context_pack_id"]
    ).consumed_by_run_id is None
    assert provider.received_messages == []


def test_single_call_route_requires_recompile_before_provider_io(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = snapshot["project"]["project_id"]
        session = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Single-call route fixture"},
        ).json()
        objective = "Reject an incomplete managed prewrite chain."
        project_kb_binding = _publish_project_kb(client, snapshot)
        pack_response = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/context-packs",
            headers={"Idempotency-Key": "single-call-context-0001"},
            json={
                "objective": objective,
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        )
        assert pack_response.status_code == 201, pack_response.text
        pack_json = pack_response.json()["pack"]
        context_pack_id = pack_json["context_pack_id"]
        context_store = app.state.context_service.store
        pack = context_store.get_pack(context_pack_id).pack
        current_route = context_store.get_nlcl_route(context_pack_id)
        route_draft = current_route.model_copy(
            update={
                "chapter_call_list": current_route.chapter_call_list[:1],
                "receipt_hash": "0" * 64,
            }
        )
        legacy_route = NlclRouteReceipt.model_validate_json(
            json.dumps(
                route_draft.model_copy(
                    update={"receipt_hash": nlcl_route_receipt_hash(route_draft)}
                ).model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            ),
            strict=True,
        )
        legacy_package = build_prewrite_operation_package(pack, legacy_route)
        assert legacy_package.readiness == "risk_draft_only"
        with app.state.runtime.store.connection() as connection:
            connection.execute(
                """
                UPDATE nlcl_route_receipt
                SET receipt_hash = ?, payload_json = ?
                WHERE context_pack_id = ?
                """,
                (
                    legacy_route.receipt_hash,
                    json.dumps(
                        legacy_route.model_dump(mode="json", by_alias=True),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    context_pack_id,
                ),
            )
            connection.execute(
                """
                UPDATE prewrite_operation_package
                SET route_receipt_hash = ?, readiness = ?, package_hash = ?,
                    payload_json = ?
                WHERE context_pack_id = ?
                """,
                (
                    legacy_route.receipt_hash,
                    legacy_package.readiness,
                    legacy_package.package_hash,
                    json.dumps(
                        legacy_package.model_dump(mode="json", by_alias=True),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    context_pack_id,
                ),
            )
        run_response = client.post(
            f"/api/v1/agent/sessions/{session['session_id']}/runs",
            headers={"Idempotency-Key": "single-call-turn-0001"},
            json={
                "message": objective,
                "expected_project_revision": pack_json["project_revision"],
                "expected_project_fingerprint": pack_json["project_fingerprint"],
                "active_chapter_id": pack_json["active_chapter_id"],
                "context_pack_id": context_pack_id,
                "expected_context_pack_hash": pack_json["pack_hash"],
            },
        )
        runs = client.get(
            f"/api/v1/agent/sessions/{session['session_id']}/runs"
        ).json()["runs"]

    assert run_response.status_code == 409, run_response.text
    assert run_response.json()["error"]["code"] == (
        "nlcl_prewrite_route_recompile_required"
    )
    assert runs == []
    assert app.state.context_service.store.get_pack(
        context_pack_id
    ).consumed_by_run_id is None
    assert provider.received_messages == []


def test_tampered_prewrite_audit_hash_binding_is_rejected(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective="Audit the project before testing durable receipt binding.",
            key_prefix="prewrite-binding-tamper-0001",
        )
        assert _wait_for_terminal(client, run_id)["status"] == "succeeded"
        with app.state.runtime.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, input_json FROM tool_call
                WHERE run_id = ? AND tool_name = 'skill_execute'
                ORDER BY started_at, id
                """,
                (run_id,),
            ).fetchall()
            prewrite_row = next(
                row
                for row in rows
                if json.loads(str(row["input_json"]))["skill_id"]
                == "novel-creation-suite.prewrite-gate"
            )
            arguments = json.loads(str(prewrite_row["input_json"]))
            arguments["input"]["project_audit_execution_receipt_hash"] = "f" * 64
            connection.execute(
                "UPDATE tool_call SET input_json = ? WHERE id = ?",
                (
                    json.dumps(
                        arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    prewrite_row["id"],
                ),
            )

        response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/2"
        )

    assert response.status_code == 503
    assert "project-audit binding mismatch" in response.text


def test_cancelling_automatic_route_skill_closes_tool_and_run(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = RecordingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective="Audit the project, but allow operator cancellation.",
            key_prefix="route-cancel-0001",
        )
        for _ in range(200):
            activities = client.get(
                f"/api/v1/agent/runs/{run_id}/activities"
            ).json()["activities"]
            if any(
                item["kind"] == "tool_call"
                and item["payload"]["tool_name"] == "skill_execute"
                for item in activities
            ):
                break
            time.sleep(0.005)
        else:
            raise AssertionError("automatic NLCL route ToolCall did not start")
        cancelled = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        terminal = _wait_for_terminal(client, run_id)
        route_state = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}"
            "/nlcl-route/executions/1"
        ).json()
        tool_calls = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json()["activities"]

    assert terminal["status"] == "cancelled"
    assert provider.received_messages == []
    assert route_state["status"] == "cancelled"
    assert route_state["receipt"]["execution_status"] == "cancelled"
    assert any(
        item["kind"] == "tool_result"
        and item["payload"]["status"] == "cancelled"
        for item in tool_calls
    )
    skill_runs = tmp_path / "skill-runs"
    assert not skill_runs.exists() or list(skill_runs.iterdir()) == []


def test_changeset_proposal_is_reconciled_if_tool_result_persist_fails_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original = chapter_path.read_text(encoding="utf-8")
    provider = ToolCallingProvider()
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        distillation_adapter=StableKnowledgeAdapter(),
        agent_provider=provider,
    )
    original_finish = app.state.agent_service.store.finish_tool_call
    injected = False

    def fail_after_changeset_once(run_id: str, **kwargs):
        nonlocal injected
        if kwargs.get("change_proposal") is not None and not injected:
            injected = True
            raise RuntimeError("simulated crash after ChangeSet persistence")
        return original_finish(run_id, **kwargs)

    monkeypatch.setattr(
        app.state.agent_service.store,
        "finish_tool_call",
        fail_after_changeset_once,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        run_id, _ = _start_tool_run(
            client,
            snapshot,
            objective="Tighten the opening and preserve the key.",
            key_prefix="tool-reconcile-0001",
            writer_mode="author_review_change_proposal",
        )
        runtime_propose_tool_id = app.state.agent_service._runtime_tool_call_id(
            run_id,
            "call_propose_change_0003",
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "failed"
        assert terminal["error"]["error"]["code"] == "agent_execution_failed"

        activities = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json()["activities"]
        proposals = [item for item in activities if item["kind"] == "change_proposal"]
        assert len(proposals) == 1
        proposal_tool_result = next(
            item
            for item in activities
            if item["kind"] == "tool_result"
            and item["payload"]["tool_call_id"] == runtime_propose_tool_id
        )
        assert proposal_tool_result["payload"]["status"] == "succeeded"
        assert proposal_tool_result["payload"]["output"]["recovered"] is True
        change_set_id = proposals[0]["payload"]["proposal_id"]
        change_set = client.get(f"/api/v1/change-sets/{change_set_id}").json()
        assert change_set["status"] == "reviewing"
        assert change_set["source_run_id"] == run_id
        assert chapter_path.read_text(encoding="utf-8") == original

    assert injected is True
    assert len(provider.requests) == 3
