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
    def __init__(self, *, candidate_preamble: str | None = None) -> None:
        self.received_messages: list[dict[str, str]] = []
        self.candidate_preamble = candidate_preamble

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
            if self.candidate_preamble is not None:
                yield ProviderTextDelta(self.candidate_preamble)
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


class AnswerOnlyProvider:
    """无论通告了哪些工具，都只流式给出文本回答，不调用任何工具。"""

    def __init__(self, *, answer: str = "Reviewed context received.") -> None:
        self.received_messages: list[dict[str, str]] = []
        self.answer = answer

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "e" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.answer-only-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="answer-only-e2e",
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
        yield ProviderTextDelta(self.answer)
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
        assert previous["status"] == "succeeded", previous
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


def test_managed_author_review_run_completes_when_model_only_answers(
    tmp_path: Path,
) -> None:
    """统一 Agent 边界：编辑模式不再强制候选信封。

    作者的普通一句话（哪怕发生在打开章节的改稿语境里）如果让模型判断为
    「只需要回答」，Run 就以文本回答成功收尾——没有 ChangeSet、没有候选、
    正文一个字节不动。作者审阅制不受影响：apply 永远要人点。
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original = chapter_path.read_text(encoding="utf-8")
    provider = AnswerOnlyProvider()
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
        run_id, _pack = _start_tool_run(
            client,
            snapshot,
            objective="What is the opening of this chapter doing right now?",
            key_prefix="answer-only-e2e-0001",
            writer_mode="author_review_change_proposal",
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", json.dumps(
            {k: terminal.get(k) for k in ("status", "failure", "error")},
            ensure_ascii=False,
        )

        candidate_response = client.get(
            f"/api/v1/agent/runs/{run_id}/candidate-output"
        )
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", json.dumps(
            {k: terminal.get(k) for k in ("status", "failure", "error")},
            ensure_ascii=False,
        )

        candidate_response = client.get(
            f"/api/v1/agent/runs/{run_id}/candidate-output"
        )
        assert candidate_response.status_code == 404, candidate_response.text

        messages = [
            item
            for item in client.get(
                f"/api/v1/agent/runs/{run_id}/activities"
            ).json()["activities"]
            if item["kind"] == "message" and item["payload"]["role"] == "assistant"
        ]
        completed_messages = [
            item for item in messages if item["payload"]["phase"] == "completed"
        ]
        assert len(completed_messages) == 1
        assert "Reviewed context received." in str(
            completed_messages[0]["payload"]["content"]
        )

        change_sets = [
            item for item in client.get(
                f"/api/v1/agent/runs/{run_id}/activities"
            ).json()["activities"]
            if item["kind"] == "change_proposal"
        ]
        assert change_sets == []
        assert chapter_path.read_text(encoding="utf-8") == original


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
        assert terminal["status"] == "succeeded", json.dumps(
            {k: terminal.get(k) for k in ("status", "failure", "error")},
            ensure_ascii=False,
        )

        activities_response = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        )
        assert activities_response.status_code == 200, activities_response.text
        activities = activities_response.json()["activities"]
        tool_calls = [item for item in activities if item["kind"] == "tool_call"]
        tool_results = [item for item in activities if item["kind"] == "tool_result"]
        proposals = [item for item in activities if item["kind"] == "change_proposal"]
        # 受管流水线（project-audit / prewrite / handoff 三次 skill_execute + candidate_output_submit）
        # 已退役：普通对话只有模型自己发的三次工具调用。
        assert [item["payload"]["tool_name"] for item in tool_calls] == [
            "project_list_files",
            "project_read_file",
            "change_set_propose",
        ]
        assert [item["payload"]["status"] for item in tool_results] == [
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
        # Agent 提的修改不再强制过写前门禁（2026-09-24）。
        assert change_set["gate_required"] is False
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

    # 四轮都是普通工具目录：没有受管链的 candidate_output_submit / skill_execute。
    assert len(provider.tool_catalogs) == 4
    for catalog in provider.tool_catalogs:
        assert {"list_files", "read_file", "change_set_propose"} <= set(catalog)
        assert "candidate_output_submit" not in catalog
        assert "skill_execute" not in catalog
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


class DirectWriteProvider:
    """聊天链里直接写文件的 Provider：第一轮建文件，第二轮收尾回答。"""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_catalogs: list[list[str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "f" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.direct-write-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="direct-write-e2e",
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
        if turn == 1:
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_write_file_0001",
                tool_name="write_file",
                arguments={
                    "relative_path": "08_素材库/灵感.md",
                    "content": "# 灵感\n\n一句题记。\n",
                    "note": "作者让建一个灵感文件",
                },
            )
            yield ProviderCompleted("tool_calls", None)
            return
        previous = json.loads(str(request[-1]["content"]))
        assert previous["status"] == "succeeded", previous
        assert previous["output"]["applied"] is True
        yield ProviderTextDelta("已经建好并保存，可以在修改记录里撤回。")
        yield ProviderCompleted("stop", None)


def test_chat_run_direct_write_applies_to_disk_and_rolls_back(
    tmp_path: Path,
) -> None:
    """回归：聊天链里 project_write_file 落盘后 Run 必须成功收尾。

    直接落盘会把 Run 的版本钉子推到新版本，此前服务层仍按旧基准记
    change_proposal 活动，ActivityItem 校验失败把整轮对话判死
    （真机 2026-09-15 抓到）。现在直接落盘不记提案活动，只留工具结果。
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    note_path = project / "08_素材库" / "灵感.md"
    provider = DirectWriteProvider()
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
        project_kb_binding = _publish_project_kb(client, snapshot)
        session_response = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Direct write chat"},
        )
        assert session_response.status_code == 201, session_response.text
        session_id = session_response.json()["session_id"]
        message = "帮我在素材库建一个灵感文件。"
        pack_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/context-packs",
            headers={"Idempotency-Key": "direct-write-e2e-context"},
            json={
                "objective": message,
                "profile": "chat",
                "active_chapter_id": None,
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        )
        assert pack_response.status_code == 201, pack_response.text
        pack = pack_response.json()["pack"]
        run_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "direct-write-e2e-turn"},
            json={
                "message": message,
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": None,
                "writer_mode": "author_review_change_proposal",
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["run"]["run_id"]
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        # 聊天链（动手改挡位）必须通告直接写工具。
        assert "write_file" in provider.tool_catalogs[0]
        # 文件真的落盘。
        assert note_path.read_text(encoding="utf-8") == "# 灵感\n\n一句题记。\n"
        # 没有「等待审阅的提案」活动——直接落盘只有工具结果。
        activities = client.get(
            f"/api/v1/agent/runs/{run_id}/activities"
        ).json()["activities"]
        assert [
            item for item in activities if item["kind"] == "change_proposal"
        ] == []
        # 修改记录里这条是 applied，可撤回。
        listing = client.get(f"/api/v1/projects/{project_id}/change-sets").json()
        mine = next(
            item
            for item in listing["change_sets"]
            if item.get("source_run_id") == run_id
        )
        assert mine["status"] == "applied"
        detail = client.get(
            f"/api/v1/change-sets/{mine['change_set_id']}"
        ).json()
        change_set = detail.get("change_set") or detail
        rolled_back = client.post(
            f"/api/v1/change-sets/{mine['change_set_id']}/rollback",
            json={"expected_review_revision": change_set["review_revision"]},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert not note_path.exists()


class DirectMoveProvider:
    """聊天链里直接改名的 Provider：第一轮挪文件，第二轮收尾回答。"""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_catalogs: list[list[str]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "e" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.direct-move-e2e",
            kind=ExecutorKind.EMBEDDED,
            implementation="direct-move-e2e",
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
        if len(self.requests) == 1:
            yield ProviderToolCall(
                index=0,
                tool_call_id="call_move_file_0001",
                tool_name="move_file",
                arguments={
                    "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                    "target_relative_path": "05_正文内容/第一卷/第 003 章 试剑.md",
                },
            )
            yield ProviderCompleted("tool_calls", None)
            return
        previous = json.loads(str(request[-1]["content"]))
        assert previous["status"] == "succeeded", previous
        assert previous["output"]["applied"] is True
        yield ProviderTextDelta("名字换好了，修改记录里可以撤回。")
        yield ProviderCompleted("stop", None)


def test_chat_run_direct_move_renames_and_rolls_back(tmp_path: Path) -> None:
    """对话里「把第 3 章改名成 第 003 章 试剑」：一条 move 落盘，撤回恢复原文件名。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    moved_path = project / "05_正文内容" / "第一卷" / "第 003 章 试剑.md"
    original = chapter_path.read_text(encoding="utf-8")
    provider = DirectMoveProvider()
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
        project_kb_binding = _publish_project_kb(client, snapshot)
        session_response = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Rename chat"},
        )
        assert session_response.status_code == 201, session_response.text
        session_id = session_response.json()["session_id"]
        message = "把第 3 章改名成 第 003 章 试剑。"
        pack_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/context-packs",
            headers={"Idempotency-Key": "direct-move-e2e-context"},
            json={
                "objective": message,
                "profile": "chat",
                "active_chapter_id": None,
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        )
        assert pack_response.status_code == 201, pack_response.text
        pack = pack_response.json()["pack"]
        run_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "direct-move-e2e-turn"},
            json={
                "message": message,
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": None,
                "writer_mode": "author_review_change_proposal",
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["run"]["run_id"]
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        # 聊天链（动手改挡位）必须通告改名工具。
        assert "move_file" in provider.tool_catalogs[0]
        # 文件真的挪走：新名字在、旧名字不在、字节原封不动。
        assert not chapter_path.exists()
        assert moved_path.read_text(encoding="utf-8") == original
        # 修改记录里这条是 applied 的 move，可撤回。
        listing = client.get(f"/api/v1/projects/{project_id}/change-sets").json()
        mine = next(
            item
            for item in listing["change_sets"]
            if item.get("source_run_id") == run_id
        )
        assert mine["status"] == "applied"
        detail = client.get(f"/api/v1/change-sets/{mine['change_set_id']}").json()
        change_set = detail.get("change_set") or detail
        assert change_set["changes"][0]["operation"] == "move"
        assert (
            change_set["changes"][0]["target_relative_path"]
            == "05_正文内容/第一卷/第 003 章 试剑.md"
        )
        rolled_back = client.post(
            f"/api/v1/change-sets/{mine['change_set_id']}/rollback",
            json={"expected_review_revision": change_set["review_revision"]},
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["status"] == "rolled_back"
        assert chapter_path.read_text(encoding="utf-8") == original
        assert not moved_path.exists()


class ReadOnlyCatalogProvider(AnswerOnlyProvider):
    """只回答、不调用任何工具的 Provider，顺带把每轮收到的工具目录记下来。"""

    def __init__(self) -> None:
        super().__init__()
        self.tool_catalogs: list[list[str]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        self.tool_catalogs.append([tool.name for tool in tools])
        async for event in super().stream_chat(
            messages,
            cancel_event=cancel_event,
            tools=tools,
        ):
            yield event


def test_readonly_gear_keeps_web_fetch_but_strips_write_tools(tmp_path: Path) -> None:
    """「只问不改」挡位：写工具与命令全摘掉，上网与只读工具照常在目录里。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    provider = ReadOnlyCatalogProvider()
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
        project_kb_binding = _publish_project_kb(client, snapshot)
        session_response = client.post(
            f"/api/v1/projects/{project_id}/agent/sessions",
            json={"title": "Read-only chat"},
        )
        assert session_response.status_code == 201, session_response.text
        session_id = session_response.json()["session_id"]
        pack_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/context-packs",
            headers={"Idempotency-Key": "readonly-web-e2e-context"},
            json={
                "objective": "只读一问。",
                "profile": "chat",
                "active_chapter_id": None,
                "expected_project_revision": snapshot["project"]["revision"],
                "expected_project_fingerprint": snapshot["project"]["fingerprint"],
                **project_kb_binding,
            },
        )
        assert pack_response.status_code == 201, pack_response.text
        pack = pack_response.json()["pack"]
        run_response = client.post(
            f"/api/v1/agent/sessions/{session_id}/runs",
            headers={"Idempotency-Key": "readonly-web-e2e-turn"},
            json={
                "message": "只读一问。",
                "expected_project_revision": pack["project_revision"],
                "expected_project_fingerprint": pack["project_fingerprint"],
                "active_chapter_id": None,
                "writer_mode": "repair_only",
                "context_pack_id": pack["context_pack_id"],
                "expected_context_pack_hash": pack["pack_hash"],
            },
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["run"]["run_id"]
        terminal = _wait_for_terminal(client, run_id)
        assert terminal["status"] == "succeeded", terminal

        assert provider.tool_catalogs
        catalog = provider.tool_catalogs[0]
        assert "web_fetch" in catalog
        assert "search_web" in catalog
        assert "read_file" in catalog and "list_files" in catalog
        assert "write_file" not in catalog
        assert "delete_file" not in catalog
        assert "move_file" not in catalog
        assert "make_folder" not in catalog
        assert "computer_run_command" not in catalog
