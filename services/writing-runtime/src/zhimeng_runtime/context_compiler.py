from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from zhimeng_runtime.context_models import (
    human_readable_for,
    include_response_guidance_for,
    response_guidance_version_for,
    CompletedHistoryMessage,
    ContextAuthority,
    ContextBudget,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextExcludedFragment,
    ContextGate,
    ContextGateSeverity,
    ContextIncludedFragment,
    ContextPack,
    ContextProviderMessage,
    conservative_token_estimate,
    context_fragment_hash,
    context_injection_hash,
    context_pack_hash,
    context_provider_messages_hash,
    fragment_sort_key,
    provider_message_characters,
    provider_message_tokens,
    readiness_from_gates,
    render_context_fragment,
    render_system_context,
)


# v5（2026-09-13）：【回答要求】升到第 3 版（点名禁掉「窗口 / 工具次数 / 回填」这类内部说法，
# 记账文件里的英文状态词要翻成人话）。渲染格式与 v4 相同（人话版），只是这一段文字不同。
# 存量 v4 Pack 按 v4 的文字逐字节重渲染，不受影响。
# v6（2026-09-20）：不再往资料包里编【回答要求】——作者把「口吻规矩」全删了。
CONTEXT_COMPILER_VERSION = "context-compiler.v6"
CONTEXT_POLICY_VERSION = "context-selection.v1"
_PLACEHOLDER_HASH = "0" * 64


def _validate_history(
    history: Sequence[CompletedHistoryMessage],
    *,
    history_through_ordinal: int,
) -> tuple[CompletedHistoryMessage, ...]:
    """校验并规整已完成历史。

    规整规则：连续同角色消息合并成一条（内容拼接、ordinal 取靠后的那条）。
    合并的起因是「输出被 token 上限截断后自动续写」——同一轮回答会落成两条
    相邻的 assistant 消息；而 Anthropic 协议要求请求里 user/assistant 严格
    交替，不合并的话下一轮编资料包必然 503。
    """
    if isinstance(history_through_ordinal, bool) or history_through_ordinal < 0:
        raise ValueError("history_through_ordinal must be a non-negative integer")
    ordered = sorted(history, key=lambda message: message.ordinal)
    ordinals = [message.ordinal for message in ordered]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("completed history ordinals must be unique")
    if ordinals and ordinals[-1] > history_through_ordinal:
        raise ValueError("completed history exceeds history_through_ordinal")
    merged: list[CompletedHistoryMessage] = []
    for message in ordered:
        if merged and merged[-1].role == message.role:
            previous = merged[-1]
            merged[-1] = previous.model_copy(
                update={
                    "ordinal": message.ordinal,
                    "content": previous.content + "\n\n" + message.content,
                }
            )
            continue
        merged.append(message)
    if merged and merged[-1].role != "assistant":
        raise ValueError("completed history must end with an assistant message")
    return tuple(merged)


def _validate_candidates(
    candidates: Sequence[ContextCandidateFragment],
    *,
    max_fragments: int,
) -> tuple[ContextCandidateFragment, ...]:
    if isinstance(max_fragments, bool) or max_fragments < 1:
        raise ValueError("max_fragments must be a positive integer")
    if max_fragments > 256:
        raise ValueError("max_fragments cannot exceed the hard limit of 256")
    if len(candidates) > max_fragments:
        raise ValueError(f"candidate count exceeds max_fragments={max_fragments}")
    ordered = tuple(sorted(candidates, key=fragment_sort_key))
    source_ids = [candidate.source_id for candidate in ordered]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("candidate source_ids must be unique")
    return ordered


def _fragment_metrics(
    candidate: ContextCandidateFragment,
) -> tuple[str, int, int]:
    rendered = render_context_fragment(candidate)
    return (
        context_fragment_hash(candidate.content),
        len(rendered),
        conservative_token_estimate(rendered),
    )


def _included(candidate: ContextCandidateFragment) -> ContextIncludedFragment:
    values = candidate.model_dump(exclude={"eligible", "exclusion_reason"})
    fragment_hash, estimated_characters, estimated_tokens = _fragment_metrics(
        candidate
    )
    return ContextIncludedFragment(
        **values,
        fragment_hash=fragment_hash,
        estimated_characters=estimated_characters,
        estimated_tokens=estimated_tokens,
        selection_reason="mandatory" if candidate.mandatory else "priority",
    )


def _excluded(
    candidate: ContextCandidateFragment,
    *,
    reason: str,
) -> ContextExcludedFragment:
    fragment_hash, estimated_characters, estimated_tokens = _fragment_metrics(
        candidate
    )
    values = candidate.model_dump(exclude={"content", "exclusion_reason"})
    provisional = ContextExcludedFragment(
        **values,
        candidate_exclusion_reason=candidate.exclusion_reason,
        fragment_hash=fragment_hash,
        estimated_characters=estimated_characters,
        estimated_tokens=estimated_tokens,
        exclusion_reason=reason,
    )
    return provisional


def _gate_for_exclusion(
    fragment: ContextExcludedFragment,
    *,
    code: str,
) -> ContextGate:
    severity = (
        ContextGateSeverity.BLOCKING
        if fragment.mandatory
        else ContextGateSeverity.WARNING
    )
    return ContextGate(
        code=code,
        severity=severity,
        source_id=fragment.source_id,
        message=f"Context fragment {fragment.source_id} was excluded: {fragment.exclusion_reason}",
    )


def _provider_messages(
    *,
    request: ContextCreateRequest,
    session_history: tuple[CompletedHistoryMessage, ...],
    project_id: str,
    knowledge_fingerprint: str | None,
    included: tuple[ContextIncludedFragment, ...],
    compiler_version: str,
) -> tuple[ContextProviderMessage, ...]:
    system = ContextProviderMessage(
        role="system",
        content=render_system_context(
            project_id=project_id,
            project_revision=request.expected_project_revision,
            project_fingerprint=request.expected_project_fingerprint,
            project_kb_state_id=request.project_kb_state_id,
            project_kb_manifest_hash=request.project_kb_manifest_hash,
            knowledge_fingerprint=knowledge_fingerprint,
            profile=request.profile,
            active_chapter_id=request.active_chapter_id,
            included=included,
            include_response_guidance=include_response_guidance_for(compiler_version),
            response_guidance_version=response_guidance_version_for(compiler_version),
            human_readable=human_readable_for(compiler_version),
        ),
    )
    history_messages = tuple(
        ContextProviderMessage(role=message.role, content=message.content)
        for message in session_history
    )
    objective = ContextProviderMessage(role="user", content=request.objective)
    return (system, *history_messages, objective)


def compile_context_pack(
    *,
    context_pack_id: str,
    session_id: str,
    project_id: str,
    history_through_ordinal: int,
    request: ContextCreateRequest,
    history: Sequence[CompletedHistoryMessage],
    candidates: Sequence[ContextCandidateFragment],
    max_characters: int,
    created_at: datetime,
    max_tokens: int | None = None,
    knowledge_fingerprint: str | None = None,
    max_fragments: int = 256,
    compiler_version: str = CONTEXT_COMPILER_VERSION,
    policy_version: str = CONTEXT_POLICY_VERSION,
) -> ContextPack:
    """Compile a pure, immutable Context Pack without I/O or ambient state."""

    if isinstance(max_characters, bool) or max_characters < 1:
        raise ValueError("max_characters must be a positive integer")
    effective_max_tokens = max_characters if max_tokens is None else max_tokens
    if isinstance(effective_max_tokens, bool) or effective_max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    session_history = _validate_history(
        history,
        history_through_ordinal=history_through_ordinal,
    )
    ordered_candidates = _validate_candidates(
        candidates,
        max_fragments=max_fragments,
    )

    included: list[ContextIncludedFragment] = []
    excluded: list[ContextExcludedFragment] = []
    gates: list[ContextGate] = []

    base_messages = _provider_messages(
        request=request,
        session_history=session_history,
        project_id=project_id,
        knowledge_fingerprint=knowledge_fingerprint,
        included=(),
        compiler_version=compiler_version,
    )
    base_character_overflow = provider_message_characters(base_messages) > max_characters
    base_token_overflow = (
        provider_message_tokens(base_messages) > effective_max_tokens
    )
    if base_character_overflow or base_token_overflow:
        dimensions = _budget_dimensions(
            character_overflow=base_character_overflow,
            token_overflow=base_token_overflow,
        )
        gates.append(
            ContextGate(
                code="base_messages_over_budget",
                severity=ContextGateSeverity.BLOCKING,
                message=(
                    "System context, completed history, and objective exceed "
                    f"the {dimensions} budget."
                ),
                source_id=None,
            )
        )

    for candidate in ordered_candidates:
        if not candidate.eligible:
            fragment = _excluded(
                candidate,
                reason=candidate.exclusion_reason or "ineligible",
            )
            excluded.append(fragment)
            gates.append(
                _gate_for_exclusion(
                    fragment,
                    code=(
                        "mandatory_fragment_ineligible"
                        if candidate.mandatory
                        else "fragment_ineligible"
                    ),
                )
            )
            continue

        if candidate.authority is ContextAuthority.RISK:
            fragment = _excluded(candidate, reason="risk_authority")
            excluded.append(fragment)
            gates.append(
                _gate_for_exclusion(
                    fragment,
                    code=(
                        "mandatory_risk_fragment_excluded"
                        if candidate.mandatory
                        else "risk_fragment_excluded"
                    ),
                )
            )
            continue

        fragment = _included(candidate)
        proposed = tuple((*included, fragment))
        proposed_messages = _provider_messages(
            request=request,
            session_history=session_history,
            project_id=project_id,
            knowledge_fingerprint=knowledge_fingerprint,
            included=proposed,
            compiler_version=compiler_version,
        )
        character_overflow = (
            provider_message_characters(proposed_messages) > max_characters
        )
        token_overflow = (
            provider_message_tokens(proposed_messages) > effective_max_tokens
        )
        if not character_overflow and not token_overflow:
            included.append(fragment)
            continue

        dimensions = _budget_dimensions(
            character_overflow=character_overflow,
            token_overflow=token_overflow,
        )
        reason = (
            f"{'mandatory_' if candidate.mandatory else ''}"
            f"budget_exceeded_{dimensions}"
        )
        excluded_fragment = _excluded(candidate, reason=reason)
        excluded.append(excluded_fragment)
        gates.append(
            _gate_for_exclusion(
                excluded_fragment,
                code=(
                    "mandatory_fragment_over_budget"
                    if candidate.mandatory
                    else "fragment_over_budget"
                ),
            )
        )

    included_tuple = tuple(included)
    excluded_tuple = tuple(sorted(excluded, key=fragment_sort_key))
    gates_tuple = tuple(gates)
    messages = _provider_messages(
        request=request,
        session_history=session_history,
        project_id=project_id,
        knowledge_fingerprint=knowledge_fingerprint,
        included=included_tuple,
        compiler_version=compiler_version,
    )
    used_characters = provider_message_characters(messages)
    used_tokens = provider_message_tokens(messages)
    budget = ContextBudget(
        max_characters=max_characters,
        used_characters=used_characters,
        remaining_characters=max(0, max_characters - used_characters),
        overflow_characters=max(0, used_characters - max_characters),
        max_tokens=effective_max_tokens,
        used_tokens=used_tokens,
        remaining_tokens=max(0, effective_max_tokens - used_tokens),
        overflow_tokens=max(0, used_tokens - effective_max_tokens),
    )
    provider_messages_hash = context_provider_messages_hash(messages)

    pack_values: dict[str, object] = {
        "context_pack_id": context_pack_id,
        "session_id": session_id,
        "project_id": project_id,
        "history_through_ordinal": history_through_ordinal,
        "history_message_count": len(session_history),
        "include_session_history": request.include_session_history,
        "objective": request.objective,
        "profile": request.profile,
        "active_chapter_id": request.active_chapter_id,
        "project_revision": request.expected_project_revision,
        "project_fingerprint": request.expected_project_fingerprint,
        "project_kb_state_id": request.project_kb_state_id,
        "project_kb_manifest_hash": request.project_kb_manifest_hash,
        "knowledge_fingerprint": knowledge_fingerprint,
        "formal_knowledge_selector": request.formal_knowledge_selector,
        "manual_file_ids": request.manual_file_ids,
        "compiler_version": compiler_version,
        "policy_version": policy_version,
        "budget": budget,
        "included": included_tuple,
        "excluded": excluded_tuple,
        "gates": gates_tuple,
        "exact_provider_messages": messages,
        "pack_hash": _PLACEHOLDER_HASH,
        "injection_hash": _PLACEHOLDER_HASH,
        "provider_messages_hash": provider_messages_hash,
        "readiness": readiness_from_gates(gates_tuple),
        "created_at": created_at,
    }
    provisional = ContextPack.model_construct(**pack_values)
    pack_values["injection_hash"] = context_injection_hash(provisional)
    provisional = ContextPack.model_construct(**pack_values)
    pack_values["pack_hash"] = context_pack_hash(provisional)
    return ContextPack.model_validate(pack_values)


def _budget_dimensions(
    *,
    character_overflow: bool,
    token_overflow: bool,
) -> str:
    if character_overflow and token_overflow:
        return "characters_and_tokens"
    if character_overflow:
        return "characters"
    if token_overflow:
        return "tokens"
    raise ValueError("at least one budget dimension must overflow")


__all__ = [
    "CONTEXT_COMPILER_VERSION",
    "CONTEXT_POLICY_VERSION",
    "compile_context_pack",
]
