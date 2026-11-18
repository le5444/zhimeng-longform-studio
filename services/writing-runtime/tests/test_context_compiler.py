from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from zhimeng_runtime.context_compiler import CONTEXT_COMPILER_VERSION, compile_context_pack
from zhimeng_runtime.context_models import (
    CompletedHistoryMessage,
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextPack,
    ContextReadiness,
    canonical_json,
    canonical_json_hash,
)


PROJECT_FINGERPRINT = canonical_json_hash({"project": "revision-7"})
PROJECT_KB_STATE_ID = f"project-kb-state-{'a' * 32}"
PROJECT_KB_MANIFEST_HASH = "b" * 64


def request() -> ContextCreateRequest:
    return ContextCreateRequest(
        objective="收紧本章对话，同时保持人物既有立场。",
        profile="rewrite",
        active_chapter_id="VOL001-CH007",
        expected_project_revision=7,
        expected_project_fingerprint=PROJECT_FINGERPRINT,
        project_kb_state_id=PROJECT_KB_STATE_ID,
        project_kb_manifest_hash=PROJECT_KB_MANIFEST_HASH,
    )


def candidate(
    source_id: str,
    *,
    authority: ContextAuthority = ContextAuthority.PROJECT_TRUTH,
    content: str = "人物在上一章拒绝了交易。",
    priority: int = 100,
    mandatory: bool = False,
    eligible: bool = True,
    exclusion_reason: str | None = None,
) -> ContextCandidateFragment:
    return ContextCandidateFragment(
        source_id=source_id,
        source_type="chapter_fact",
        title=f"Context {source_id}",
        authority=authority,
        version="revision-7",
        source_hash=canonical_json_hash({"source": source_id}),
        content=content,
        priority=priority,
        mandatory=mandatory,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
    )


def history() -> tuple[CompletedHistoryMessage, ...]:
    return (
        CompletedHistoryMessage(ordinal=3, role="user", content="先检查人物动机。"),
        CompletedHistoryMessage(
            ordinal=4,
            role="assistant",
            content="人物动机与上一章一致。",
        ),
    )


def compile_pack(
    *,
    context_pack_id: str = "context-pack-1",
    created_at: datetime = datetime(2026, 7, 20, tzinfo=timezone.utc),
    candidates: tuple[ContextCandidateFragment, ...] = (),
    max_characters: int = 100_000,
    max_tokens: int = 100_000,
    completed_history: tuple[CompletedHistoryMessage, ...] | None = None,
    knowledge_fingerprint: str | None = None,
) -> ContextPack:
    return compile_context_pack(
        context_pack_id=context_pack_id,
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=request(),
        history=history() if completed_history is None else completed_history,
        candidates=candidates,
        max_characters=max_characters,
        max_tokens=max_tokens,
        created_at=created_at,
        knowledge_fingerprint=knowledge_fingerprint,
    )


def test_compilation_is_deterministic_and_provider_messages_are_exact() -> None:
    mandatory = candidate("project-character", mandatory=True, priority=1)
    high_priority = candidate("project-chapter", priority=500)

    first = compile_pack(candidates=(high_priority, mandatory))
    second = compile_pack(
        context_pack_id="context-pack-2",
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        candidates=(mandatory, high_priority),
        completed_history=tuple(reversed(history())),
    )

    assert [item.source_id for item in first.included] == [
        "project-character",
        "project-chapter",
    ]
    assert first.pack_hash == second.pack_hash
    assert first.injection_hash == second.injection_hash
    assert first.provider_messages_hash == second.provider_messages_hash
    assert first.context_pack_id != second.context_pack_id
    assert first.created_at != second.created_at
    assert [message.role for message in first.exact_provider_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert first.exact_provider_messages[-1].content == request().objective
    assert first.readiness is ContextReadiness.READY


def test_v5_chat_pack_keeps_response_guidance_but_current_version_has_none() -> None:
    """v5 及以前的聊天资料包带【回答要求】，逐字节校验要求它们一个字不变；
    当前版本（v6 起）不再带：作者 2026-09-20 把「口吻规矩」全删了。"""
    chat_request = request().model_copy(update={"profile": "chat"})

    def _pack(version: str, pack_id: str):
        return compile_context_pack(
            context_pack_id=pack_id,
            session_id="session-context-1",
            project_id="project-context-1",
            history_through_ordinal=4,
            request=chat_request,
            history=history(),
            candidates=(),
            max_characters=100_000,
            max_tokens=100_000,
            created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            compiler_version=version,
        )

    legacy = _pack("context-compiler.v5", "context-pack-chat-v5").exact_provider_messages[0].content
    assert "【回答要求】" in legacy
    assert "简体中文" in legacy
    assert "思考与操作记录" in legacy
    assert "工程术语" in legacy

    current = _pack(CONTEXT_COMPILER_VERSION, "context-pack-chat").exact_provider_messages[0].content
    assert "【回答要求】" not in current
    assert "工程术语" not in current
    assert "【上下文说明】" in current
    assert "【回答要求】" not in compile_pack().exact_provider_messages[0].content


def test_chat_pack_created_by_v1_remains_readable_after_response_guidance_upgrade() -> None:
    chat_request = request().model_copy(update={"profile": "chat"})
    old_pack = compile_context_pack(
        context_pack_id="context-pack-chat-v1",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=chat_request,
        history=history(),
        candidates=(),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        compiler_version="context-compiler.v1",
    )

    restored = ContextPack.model_validate_json(old_pack.model_dump_json())
    system_payload = json.loads(restored.exact_provider_messages[0].content)

    assert restored.compiler_version == "context-compiler.v1"
    assert "response_guidance" not in system_payload


def test_chat_pack_created_by_v2_keeps_its_original_response_guidance() -> None:
    chat_request = request().model_copy(update={"profile": "chat"})
    old_pack = compile_context_pack(
        context_pack_id="context-pack-chat-v2",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=chat_request,
        history=history(),
        candidates=(),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        compiler_version="context-compiler.v2",
    )

    restored = ContextPack.model_validate(old_pack.model_dump(mode="json"))
    system_payload = json.loads(restored.exact_provider_messages[0].content)

    assert restored.compiler_version == "context-compiler.v2"
    assert "思考过程" in system_payload["response_guidance"]
    assert "工程术语" not in system_payload["response_guidance"]


def test_pack_created_by_v3_keeps_its_original_json_system_context() -> None:
    old_pack = compile_context_pack(
        context_pack_id="context-pack-v3",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=request().model_copy(update={"profile": "chat"}),
        history=history(),
        candidates=(
            candidate("project-character", mandatory=True, priority=1),
            candidate(
                "knowledge-card-1",
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                content="开篇先给主角一个具体动作。",
            ),
        ),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        compiler_version="context-compiler.v3",
        knowledge_fingerprint=canonical_json_hash({"knowledge": "v1"}),
    )

    restored = ContextPack.model_validate_json(old_pack.model_dump_json())
    system_content = restored.exact_provider_messages[0].content
    system_payload = json.loads(system_content)

    assert restored.compiler_version == "context-compiler.v3"
    assert system_payload["schema"] == "zhimeng.provider-system-context.v1"
    assert "工程术语" in system_payload["response_guidance"]
    assert system_content == canonical_json(system_payload)
    fragment_payloads = system_payload["fragments"]
    assert [item["source_id"] for item in fragment_payloads] == [
        "project-character",
        "knowledge-card-1",
    ]
    for key in (
        "source_hash",
        "fragment_hash",
        "version",
        "priority",
        "mandatory",
    ):
        assert key in fragment_payloads[0]


def test_v4_system_context_is_chinese_sections_without_engineering_fields() -> None:
    knowledge = candidate(
        "knowledge-card-1",
        authority=ContextAuthority.FORMAL_KNOWLEDGE,
        content="开篇先给主角一个具体动作。",
        priority=900,
    )
    reference = candidate(
        "reference-note",
        authority=ContextAuthority.REFERENCE,
        content="作者提过想试试倒叙。",
        priority=800,
    )
    project = candidate(
        "project-character",
        content="人物在上一章拒绝了交易。",
        priority=100,
    )

    first = compile_pack(
        candidates=(project, reference, knowledge),
        knowledge_fingerprint=canonical_json_hash({"knowledge": "v1"}),
    )
    second = compile_pack(
        context_pack_id="context-pack-2",
        created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        candidates=(knowledge, project, reference),
        knowledge_fingerprint=canonical_json_hash({"knowledge": "v1"}),
    )
    restored = ContextPack.model_validate_json(first.model_dump_json())
    system_content = restored.exact_provider_messages[0].content

    assert restored.compiler_version == "context-compiler.v6"
    assert system_content == second.exact_provider_messages[0].content
    assert first.pack_hash == second.pack_hash
    assert not system_content.startswith("{")
    assert "【上下文说明】" in system_content
    assert "【作品】" in system_content
    assert "当前章节：VOL001-CH007" in system_content
    assert "【写作知识版本】" in system_content
    assert (
        "【作品内容】\n■ Context project-character\n人物在上一章拒绝了交易。"
        in system_content
    )
    assert (
        "【写作知识】\n■ Context knowledge-card-1\n开篇先给主角一个具体动作。"
        in system_content
    )
    assert (
        "【参考资料】\n■ Context reference-note\n作者提过想试试倒叙。"
        in system_content
    )
    assert system_content.index("【作品内容】") < system_content.index("【写作知识】")
    assert system_content.index("【写作知识】") < system_content.index("【参考资料】")
    for noise in (
        "source_hash",
        "fragment_hash",
        "priority",
        "mandatory",
        "revision-7",
        PROJECT_FINGERPRINT,
        "project_truth",
        "formal_knowledge",
        '"fragments"',
    ):
        assert noise not in system_content


def test_v4_system_context_omits_chapter_line_when_no_active_chapter() -> None:
    chapter_request = request().model_copy(update={"active_chapter_id": None})
    pack = compile_context_pack(
        context_pack_id="context-pack-no-chapter",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=chapter_request,
        history=history(),
        candidates=(),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    system_content = pack.exact_provider_messages[0].content
    assert "当前章节" not in system_content
    assert "【作品内容】" not in system_content


def test_budget_excludes_optional_and_blocks_missing_mandatory_fragment() -> None:
    mandatory = candidate(
        "project-required",
        content="必须事实。" * 20,
        mandatory=True,
    )
    optional = candidate(
        "knowledge-optional",
        authority=ContextAuthority.FORMAL_KNOWLEDGE,
        content="可选知识。" * 300,
        priority=1000,
    )
    mandatory_only = compile_pack(candidates=(mandatory,))
    selected = compile_pack(
        candidates=(optional, mandatory),
        max_characters=mandatory_only.budget.used_characters,
    )

    assert [item.source_id for item in selected.included] == ["project-required"]
    assert selected.excluded[0].source_id == "knowledge-optional"
    assert selected.excluded[0].exclusion_reason == "budget_exceeded_characters"
    assert selected.readiness is ContextReadiness.DEGRADED
    assert selected.budget.overflow_characters == 0

    base = compile_pack(candidates=())
    blocked = compile_pack(
        candidates=(mandatory,),
        max_characters=base.budget.used_characters,
    )
    assert blocked.included == ()
    assert blocked.excluded[0].exclusion_reason == (
        "mandatory_budget_exceeded_characters"
    )
    assert blocked.readiness is ContextReadiness.BLOCKED
    assert any(gate.code == "mandatory_fragment_over_budget" for gate in blocked.gates)


def test_token_budget_excludes_fragment_even_when_character_budget_fits() -> None:
    source = candidate(
        "knowledge-token-heavy",
        authority=ContextAuthority.FORMAL_KNOWLEDGE,
        content="汉字" * 200,
    )
    base = compile_pack(candidates=())
    pack = compile_pack(
        candidates=(source,),
        max_characters=100_000,
        max_tokens=base.budget.used_tokens,
    )

    assert pack.included == ()
    assert pack.excluded[0].exclusion_reason == "budget_exceeded_tokens"
    assert pack.readiness is ContextReadiness.DEGRADED
    assert pack.budget.used_characters < pack.budget.max_characters
    assert pack.budget.used_tokens <= pack.budget.max_tokens


def test_risk_and_unavailable_knowledge_are_excluded_without_blocking_project() -> None:
    project = candidate("project-truth", mandatory=True)
    risk = candidate(
        "knowledge-risk",
        authority=ContextAuthority.RISK,
        content="未经验证的风格结论。",
        priority=999,
    )
    unavailable = candidate(
        "knowledge-unavailable",
        authority=ContextAuthority.FORMAL_KNOWLEDGE,
        eligible=False,
        exclusion_reason="library_unavailable",
    )

    pack = compile_pack(candidates=(unavailable, risk, project))

    assert [item.source_id for item in pack.included] == ["project-truth"]
    assert {item.source_id: item.exclusion_reason for item in pack.excluded} == {
        "knowledge-risk": "risk_authority",
        "knowledge-unavailable": "library_unavailable",
    }
    assert pack.readiness is ContextReadiness.DEGRADED
    assert all(gate.severity.value == "warning" for gate in pack.gates)
    assert all("content" not in item.model_dump() for item in pack.excluded)
    assert "project-truth" in pack.exact_provider_messages[0].content
    assert "knowledge-risk" not in pack.exact_provider_messages[0].content


def test_hash_tampering_and_model_invariant_violations_are_rejected() -> None:
    pack = compile_pack(candidates=(candidate("project-truth", mandatory=True),))

    changed_fragment = pack.model_dump(mode="json")
    changed_fragment["included"][0]["content"] = "tampered"
    with pytest.raises(ValidationError, match="fragment_hash"):
        ContextPack.model_validate(changed_fragment)

    changed_messages = pack.model_dump(mode="json")
    changed_messages["exact_provider_messages"][-1]["content"] = "other objective"
    with pytest.raises(ValidationError, match="final provider message"):
        ContextPack.model_validate(changed_messages)

    changed_hash = pack.model_dump(mode="json")
    changed_hash["pack_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="pack_hash"):
        ContextPack.model_validate(changed_hash)

    with pytest.raises(ValidationError, match="frozen"):
        pack.objective = "cannot mutate"


def test_request_candidates_and_completed_history_are_strict() -> None:
    payload = request().model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ContextCreateRequest.model_validate(payload)

    manual_payload = request().model_dump(mode="json")
    manual_payload["manual_file_ids"] = ["file-b", "file-a"]
    manual_request = ContextCreateRequest.model_validate(manual_payload)
    assert manual_request.manual_file_ids == ("file-a", "file-b")

    manual_payload["manual_file_ids"] = ["file-a", "file-a"]
    with pytest.raises(ValidationError, match="manual_file_ids must be unique"):
        ContextCreateRequest.model_validate(manual_payload)

    with pytest.raises(ValidationError, match="require an exclusion_reason"):
        candidate("knowledge-missing", eligible=False)

    incomplete_history = (
        CompletedHistoryMessage(ordinal=1, role="user", content="unfinished"),
    )
    with pytest.raises(ValueError, match="end with an assistant"):
        compile_pack(completed_history=incomplete_history)

    duplicate = candidate("project-duplicate")
    with pytest.raises(ValueError, match="source_ids must be unique"):
        compile_pack(candidates=(duplicate, duplicate))

    with pytest.raises(ValueError, match="candidate count exceeds max_fragments"):
        compile_context_pack(
            context_pack_id="context-pack-limited",
            session_id="session-context-1",
            project_id="project-context-1",
            history_through_ordinal=4,
            request=request(),
            history=history(),
            candidates=(candidate("project-one"), candidate("project-two")),
            max_characters=100_000,
            max_tokens=100_000,
            max_fragments=1,
            created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

    with pytest.raises(ValidationError, match="knowledge_fingerprint"):
        compile_pack(knowledge_fingerprint="not-a-sha256")

    with pytest.raises(ValidationError, match="included formal knowledge"):
        compile_pack(
            candidates=(
                candidate(
                    "knowledge-formal-without-fingerprint",
                    authority=ContextAuthority.FORMAL_KNOWLEDGE,
                ),
            )
        )


def test_pack_hash_excludes_only_pack_id_and_creation_time() -> None:
    source = candidate("project-truth", mandatory=True)
    first = compile_pack(candidates=(source,))
    metadata_only = compile_pack(
        context_pack_id="context-pack-other",
        created_at=first.created_at + timedelta(days=1),
        candidates=(source,),
    )
    changed_policy = compile_context_pack(
        context_pack_id="context-pack-policy",
        session_id="session-context-1",
        project_id="project-context-1",
        history_through_ordinal=4,
        request=request(),
        history=history(),
        candidates=(source,),
        max_characters=100_000,
        max_tokens=100_000,
        created_at=first.created_at,
        policy_version="context-selection.v2",
    )

    assert first.pack_hash == metadata_only.pack_hash
    assert first.pack_hash != changed_policy.pack_hash
    assert first.model_dump(mode="json", by_alias=True)["schema"] == (
        "zhimeng.context-pack.v1"
    )


def test_knowledge_fingerprint_binds_messages_injection_and_pack_hash() -> None:
    source = candidate("project-truth", mandatory=True)
    first = compile_pack(
        candidates=(source,),
        knowledge_fingerprint=canonical_json_hash({"knowledge": "v1"}),
    )
    second = compile_pack(
        candidates=(source,),
        knowledge_fingerprint=canonical_json_hash({"knowledge": "v2"}),
    )

    assert first.provider_messages_hash != second.provider_messages_hash
    assert first.injection_hash != second.injection_hash
    assert first.pack_hash != second.pack_hash


def test_consecutive_assistant_history_is_merged_not_rejected() -> None:
    """输出截断续写后，同一轮回答会落成两条相邻的 assistant 消息。

    以前历史必须严格一问一答，撞上就 503；现在合并成一条（内容都在、
    ordinal 取靠后的），下一轮资料包照常编译。
    """
    history = (
        CompletedHistoryMessage(ordinal=1, role="user", content="把开头写紧凑一点。"),
        CompletedHistoryMessage(ordinal=2, role="assistant", content="故事从旧港开始，"),
        CompletedHistoryMessage(ordinal=3, role="assistant", content="然后风向变了。"),
        CompletedHistoryMessage(ordinal=4, role="user", content="很好，继续第三章。"),
        CompletedHistoryMessage(ordinal=5, role="assistant", content="第三章开始。"),
    )
    def merged_pack(*, history, through):
        return compile_context_pack(
            context_pack_id="context-pack-merged",
            session_id="session-context-1",
            project_id="project-context-1",
            history_through_ordinal=through,
            request=request(),
            history=history,
            candidates=(),
            max_characters=100_000,
            max_tokens=100_000,
            created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

    history = (
        CompletedHistoryMessage(ordinal=1, role="user", content="把开头写紧凑一点。"),
        CompletedHistoryMessage(ordinal=2, role="assistant", content="故事从旧港开始，"),
        CompletedHistoryMessage(ordinal=3, role="assistant", content="然后风向变了。"),
        CompletedHistoryMessage(ordinal=4, role="user", content="很好，继续第三章。"),
        CompletedHistoryMessage(ordinal=5, role="assistant", content="第三章开始。"),
    )
    first = merged_pack(history=history, through=5)
    second = merged_pack(
        history=(
            CompletedHistoryMessage(ordinal=1, role="user", content="把开头写紧凑一点。"),
            CompletedHistoryMessage(
                ordinal=3,
                role="assistant",
                content="故事从旧港开始，\n\n然后风向变了。",
            ),
            CompletedHistoryMessage(ordinal=4, role="user", content="很好，继续第三章。"),
            CompletedHistoryMessage(ordinal=5, role="assistant", content="第三章开始。"),
        ),
        through=5,
    )
    # 合并正确性：两条相邻 assistant 与「本来就该是一条消息」的历史，
    # 编译出完全相同的请求（内容不丢、请求形态合规）
    assert first.provider_messages_hash == second.provider_messages_hash
