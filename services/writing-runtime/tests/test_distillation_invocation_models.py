from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.distillation_anchor_models import (
    DistillationAnchorLevel,
    DistillationAnchorReadRequest,
    build_distillation_anchor,
    build_distillation_anchor_catalog,
    build_distillation_anchor_read_result,
    distillation_anchor_binding,
)
from zhimeng_runtime.distillation_invocation_models import (
    DISTILLATION_ALLOWED_TOOL_NAMES,
    DISTILLATION_INSTRUCTION_SKILL_ID,
    DistillationCandidateCard,
    DistillationCandidateSet,
    DistillationCandidateState,
    DistillationCardType,
    DistillationInstructionBinding,
    DistillationInvocation,
    DistillationInvocationStatus,
    DistillationKnowledgeBinding,
    DistillationOutputSubmission,
    DistillationProjectBinding,
    DistillationProviderBinding,
    DistillationRecoveryAction,
    DistillationSourceBinding,
    DistillationSubmittedCard,
    distillation_candidate_set_hash,
    distillation_invocation_head_hash,
    distillation_invocation_id,
    distillation_output_submission_hash,
)
from zhimeng_runtime.distillation_receipt_models import (
    DistillationCandidateOutputPayload,
    DistillationFinalCandidateBinding,
    DistillationFinalResultPayload,
    DistillationPreparedPayload,
    DistillationProviderDispatchPayload,
    DistillationProviderToolCall,
    DistillationProviderTurnResultPayload,
    DistillationRecoveryDecisionPayload,
    DistillationRecoveryDossierPayload,
    DistillationToolResultBinding,
    build_distillation_receipt,
    distillation_anchor_binding_set_hash,
    distillation_provider_tool_calls_hash,
    distillation_provider_turn_chain_hash,
    distillation_receipt_hash,
    distillation_tool_result_binding_hash,
    validate_distillation_receipt_chain,
)
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)


NOW = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)
SOURCE_IMPORT_ID = f"source-import-{'1' * 32}"
SOURCE_RECEIPT_ID = f"source-import-receipt-{'2' * 32}"
PROJECT_ID = f"project-{'3' * 24}"
IDEMPOTENCY_KEY = f"distillation-idempotency-{'4' * 32}"


def _bindings() -> tuple[
    DistillationSourceBinding,
    DistillationProjectBinding,
    DistillationKnowledgeBinding,
    DistillationInstructionBinding,
    DistillationProviderBinding,
]:
    source = DistillationSourceBinding(
        source_import_id=SOURCE_IMPORT_ID,
        source_receipt_id=SOURCE_RECEIPT_ID,
        source_receipt_hash="5" * 64,
        source_packet_fingerprint="6" * 64,
        packet_text_hash=hashlib.sha256(b"alphabeta").hexdigest(),
    )
    project = DistillationProjectBinding(
        project_id=PROJECT_ID,
        project_revision=7,
        project_fingerprint="7" * 64,
    )
    knowledge = DistillationKnowledgeBinding(
        scope_id=f"knowledge-scope-{'8' * 32}",
        scope_state_revision=3,
        knowledge_fingerprint="8" * 64,
        project_kb_state_id=f"project-kb-state-{'9' * 32}",
        project_kb_manifest_hash="9" * 64,
    )
    instruction = DistillationInstructionBinding(
        skill_id=DISTILLATION_INSTRUCTION_SKILL_ID,
        skill_version_id=f"skill-version-{'a' * 32}",
        skill_version="1.0.0",
        manifest_fingerprint="a" * 64,
        package_fingerprint="b" * 64,
        activation_receipt_id=f"skill-activation-{'b' * 32}",
        activation_receipt_hash="c" * 64,
        activation_state_revision=2,
        effective_scope="project",
        instruction_materialization_id=(
            f"distillation-instruction-materialization-{'c' * 32}"
        ),
        instruction_materialization_hash="d" * 64,
        instruction_hash="e" * 64,
    )
    provider = DistillationProviderBinding(
        executor=ExecutorIdentity(
            executor_id="executor.openai-compatible",
            kind=ExecutorKind.REMOTE,
            implementation="openai-compatible-chat-completions",
            version="0.1.0",
            protocol_version="1",
            instance_id=f"provider-config-{'d' * 64}",
        ),
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        ),
        requested_model="test-model",
        accepted_reported_models=("gateway-model",),
        dispatch_config_fingerprint="f" * 64,
        credential_reference="env:ZHIMENG_AGENT_PROVIDER_API_KEY",
        tool_definitions_hash="0" * 64,
    )
    return source, project, knowledge, instruction, provider


def _catalog():
    source, *_ = _bindings()
    first = build_distillation_anchor(
        source_receipt_id=source.source_receipt_id,
        packet_text_hash=source.packet_text_hash,
        level=DistillationAnchorLevel.SMALL,
        ordinal=1,
        char_start=0,
        text="alpha",
        chapter_start=1,
        chapter_end=1,
    )
    second = build_distillation_anchor(
        source_receipt_id=source.source_receipt_id,
        packet_text_hash=source.packet_text_hash,
        level=DistillationAnchorLevel.SMALL,
        ordinal=2,
        char_start=5,
        text="beta",
        chapter_start=2,
        chapter_end=2,
    )
    invocation_id = distillation_invocation_id(
        source_receipt_id=source.source_receipt_id,
        project_id=PROJECT_ID,
        idempotency_key=IDEMPOTENCY_KEY,
    )
    return build_distillation_anchor_catalog(
        invocation_id=invocation_id,
        source_import_id=source.source_import_id,
        source_receipt_id=source.source_receipt_id,
        source_receipt_hash=source.source_receipt_hash,
        packet_text_hash=source.packet_text_hash,
        packet_char_count=9,
        anchors=(first, second),
        created_at=NOW,
    )


def _submission(anchor_id: str) -> DistillationOutputSubmission:
    return DistillationOutputSubmission(
        cards=tuple(
            DistillationSubmittedCard(
                card_type=DistillationCardType(card_type),
                card={"title": f"{card_type} evidence", "summary": "bounded mechanism"},
                anchor_ids=(anchor_id,),
            )
            for card_type in ("work", "voice", "validator")
        )
    )


def _candidate_set(
    invocation_id: str,
    submission: DistillationOutputSubmission,
    anchor,
) -> DistillationCandidateSet:
    cards = []
    for index, submitted in enumerate(submission.cards):
        card_id = f"distilled:{submitted.card_type.value}:source"
        card = {
            **submitted.card,
            "card_id": card_id,
            "card_type": submitted.card_type.value,
            "draft_candidate": True,
            "requires_author_review": True,
        }
        cards.append(
            DistillationCandidateCard(
                candidate_id=f"knowledge-candidate-{index + 1:032x}",
                card_id=card_id,
                output_index=index,
                card_type=submitted.card_type,
                card=card,
                card_hash=canonical_json_hash(card),
                anchors=(anchor,),
            )
        )
    card_tuple = tuple(cards)
    return DistillationCandidateSet(
        invocation_id=invocation_id,
        cards=card_tuple,
        candidate_set_hash=distillation_candidate_set_hash(card_tuple),
    )


def _tool_call(tool_name: str, arguments: dict[str, object], suffix: str):
    return DistillationProviderToolCall(
        provider_tool_call_id=f"provider-call-{suffix}",
        tool_name=tool_name,
        arguments=arguments,
        arguments_hash=canonical_json_hash(arguments),
    )


def _turn_result(
    *,
    turn_ordinal: int,
    dispatch,
    tool_call: DistillationProviderToolCall,
) -> DistillationProviderTurnResultPayload:
    calls = (tool_call,)
    return DistillationProviderTurnResultPayload(
        turn_ordinal=turn_ordinal,
        dispatch_receipt_id=dispatch.receipt_id,
        dispatch_receipt_hash=dispatch.receipt_hash,
        requested_model="test-model",
        reported_model="gateway-model",
        accepted_reported_models=("gateway-model",),
        model_identity_status="accepted_alias",
        assistant_text=None,
        assistant_text_hash=None,
        tool_calls=calls,
        tool_calls_hash=distillation_provider_tool_calls_hash(calls),
        finish_reason="tool_calls",
        usage=None,
    )


def _normal_chain():
    source, project, knowledge, instruction, provider = _bindings()
    catalog = _catalog()
    invocation_id = catalog.invocation_id
    prepared_payload = DistillationPreparedPayload(
        source=source,
        project=project,
        knowledge=knowledge,
        instruction=instruction,
        provider=provider,
        anchor_catalog_id=catalog.catalog_id,
        anchor_catalog_hash=catalog.catalog_hash,
        anchor_count=catalog.anchor_count,
        output_contract_hash="1" * 64,
        allowed_tools=DISTILLATION_ALLOWED_TOOL_NAMES,
        request_hash="2" * 64,
        retry_of=None,
    )
    prepared = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=1,
        parent_receipt_id=None,
        expected_invocation_revision=0,
        payload=prepared_payload,
        created_at=NOW,
    )
    dispatch1_payload = DistillationProviderDispatchPayload(
        turn_ordinal=1,
        prepared_receipt_id=prepared.receipt_id,
        prepared_receipt_hash=prepared.receipt_hash,
        request_hash="3" * 64,
        provider_messages_hash="4" * 64,
        tool_definitions_hash=provider.tool_definitions_hash,
        provider_replay_count=0,
    )
    dispatch1 = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=2,
        parent_receipt_id=prepared.receipt_id,
        expected_invocation_revision=1,
        payload=dispatch1_payload,
        created_at=NOW,
    )
    read_request = DistillationAnchorReadRequest(
        anchor_ids=(catalog.anchors[0].anchor_id,)
    )
    read_call = _tool_call(
        "distillation_anchor_read",
        read_request.model_dump(mode="json", by_alias=True),
        "read-0001",
    )
    result1_payload = _turn_result(
        turn_ordinal=1,
        dispatch=dispatch1,
        tool_call=read_call,
    )
    result1 = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=3,
        parent_receipt_id=dispatch1.receipt_id,
        expected_invocation_revision=2,
        payload=result1_payload,
        created_at=NOW,
    )
    read_result = build_distillation_anchor_read_result(
        catalog=catalog,
        request=read_request,
        text_by_anchor_id={catalog.anchors[0].anchor_id: "alpha"},
    )
    read_binding = distillation_anchor_binding(catalog.anchors[0])
    tool_result_material = {
        "provider_tool_call_id": read_call.provider_tool_call_id,
        "tool_name": "distillation_anchor_read",
        "result_hash": read_result.result_hash,
        "anchor_catalog_id": catalog.catalog_id,
        "anchor_catalog_hash": catalog.catalog_hash,
        "read_anchors": (read_binding,),
    }
    tool_result_draft = DistillationToolResultBinding.model_construct(
        **tool_result_material,
        binding_hash="0" * 64,
    )
    tool_result = DistillationToolResultBinding(
        **tool_result_material,
        binding_hash=distillation_tool_result_binding_hash(tool_result_draft),
    )
    dispatch2_payload = DistillationProviderDispatchPayload(
        turn_ordinal=2,
        prepared_receipt_id=prepared.receipt_id,
        prepared_receipt_hash=prepared.receipt_hash,
        request_hash="5" * 64,
        provider_messages_hash="6" * 64,
        tool_definitions_hash=provider.tool_definitions_hash,
        prior_turn_result_receipt_id=result1.receipt_id,
        prior_turn_result_receipt_hash=result1.receipt_hash,
        prior_tool_result=tool_result,
        provider_replay_count=0,
    )
    dispatch2 = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=4,
        parent_receipt_id=result1.receipt_id,
        expected_invocation_revision=3,
        payload=dispatch2_payload,
        created_at=NOW,
    )
    submission = _submission(read_binding.anchor_id)
    submit_call = _tool_call(
        "distillation_output_submit",
        submission.model_dump(mode="json", by_alias=True),
        "submit-0002",
    )
    result2_payload = _turn_result(
        turn_ordinal=2,
        dispatch=dispatch2,
        tool_call=submit_call,
    )
    result2 = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=5,
        parent_receipt_id=dispatch2.receipt_id,
        expected_invocation_revision=4,
        payload=result2_payload,
        created_at=NOW,
    )
    result_receipts = (result1, result2)
    candidate_set = _candidate_set(invocation_id, submission, read_binding)
    candidate_payload = DistillationCandidateOutputPayload(
        final_turn_result_receipt_id=result2.receipt_id,
        final_turn_result_receipt_hash=result2.receipt_hash,
        provider_turn_count=2,
        provider_turn_chain_hash=distillation_provider_turn_chain_hash(result_receipts),
        source_receipt_id=source.source_receipt_id,
        source_receipt_hash=source.source_receipt_hash,
        instruction_materialization_id=instruction.instruction_materialization_id,
        instruction_materialization_hash=instruction.instruction_materialization_hash,
        anchor_catalog_id=catalog.catalog_id,
        anchor_catalog_hash=catalog.catalog_hash,
        read_anchors=(read_binding,),
        read_anchors_hash=distillation_anchor_binding_set_hash((read_binding,)),
        submission=submission,
        submission_hash=distillation_output_submission_hash(submission),
        candidate_set=candidate_set,
    )
    candidate = build_distillation_receipt(
        invocation_id=invocation_id,
        ordinal=6,
        parent_receipt_id=result2.receipt_id,
        expected_invocation_revision=5,
        payload=candidate_payload,
        created_at=NOW,
    )
    return {
        "catalog": catalog,
        "prepared": prepared,
        "dispatch1": dispatch1,
        "result1": result1,
        "dispatch2": dispatch2,
        "result2": result2,
        "candidate": candidate,
        "candidate_set": candidate_set,
        "read_binding": read_binding,
        "receipts": (prepared, dispatch1, result1, dispatch2, result2, candidate),
    }


def _success_final(chain: dict[str, object], *, ordinal: int, parent):
    candidate = chain["candidate"]
    candidate_set = chain["candidate_set"]
    result_receipts = (chain["result1"], chain["result2"])
    bindings = tuple(
        DistillationFinalCandidateBinding(
            candidate_id=card.candidate_id,
            card_type=card.card_type.value,
            content_hash=card.card_hash,
        )
        for card in candidate_set.cards
    )
    payload = DistillationFinalResultPayload(
        outcome="succeeded",
        provider_turn_count=2,
        provider_turn_chain_hash=distillation_provider_turn_chain_hash(result_receipts),
        candidate_output_receipt_id=candidate.receipt_id,
        candidate_output_receipt_hash=candidate.receipt_hash,
        candidates=bindings,
        candidate_set_hash=candidate_set.candidate_set_hash,
        work_count=1,
        voice_count=1,
        validator_count=1,
        failure_signature=None,
    )
    return build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=ordinal,
        parent_receipt_id=parent.receipt_id,
        expected_invocation_revision=ordinal - 1,
        payload=payload,
        created_at=NOW,
    )


def test_anchor_catalog_and_read_are_deterministic_and_tamper_evident() -> None:
    catalog = _catalog()
    request = DistillationAnchorReadRequest(
        anchor_ids=(catalog.anchors[0].anchor_id, catalog.anchors[1].anchor_id)
    )
    result = build_distillation_anchor_read_result(
        catalog=catalog,
        request=request,
        text_by_anchor_id={
            catalog.anchors[0].anchor_id: "alpha",
            catalog.anchors[1].anchor_id: "beta",
        },
    )

    assert catalog.anchor_count == 2
    assert result.total_char_count == 9
    assert tuple(item.text for item in result.items) == ("alpha", "beta")

    tampered = catalog.model_dump(mode="python")
    tampered["packet_char_count"] = 8
    with pytest.raises(ValidationError, match="exceeds packet text bounds"):
        type(catalog).model_validate(tampered)

    with pytest.raises(ValidationError, match="text hash"):
        build_distillation_anchor_read_result(
            catalog=catalog,
            request=DistillationAnchorReadRequest(
                anchor_ids=(catalog.anchors[0].anchor_id,)
            ),
            text_by_anchor_id={catalog.anchors[0].anchor_id: "ALPHA"},
        )


def test_output_submit_requires_exact_three_types_and_forbids_runtime_fields() -> None:
    anchor_id = _catalog().anchors[0].anchor_id
    valid = _submission(anchor_id)
    assert tuple(card.card_type.value for card in valid.cards) == (
        "work",
        "voice",
        "validator",
    )

    duplicate = valid.model_dump(mode="python")
    duplicate["cards"][1]["card_type"] = DistillationCardType.WORK
    with pytest.raises(ValidationError, match="exactly once"):
        DistillationOutputSubmission.model_validate(duplicate)

    with pytest.raises(ValidationError, match="Runtime-owned"):
        DistillationSubmittedCard(
            card_type=DistillationCardType.WORK,
            card={"title": "bad", "card_id": "provider-owned"},
            anchor_ids=(anchor_id,),
        )


def test_normal_provider_chain_requires_read_evidence_and_exact_final_batch() -> None:
    chain = _normal_chain()
    final = _success_final(chain, ordinal=7, parent=chain["candidate"])
    receipts = (*chain["receipts"], final)

    validate_distillation_receipt_chain(receipts)
    assert final.resulting_status is DistillationInvocationStatus.SUCCEEDED
    assert final.payload.work_count == 1
    assert final.payload.voice_count == 1
    assert final.payload.validator_count == 1

    tampered = final.model_dump(mode="python", by_alias=True)
    tampered["receipt_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="receipt hash"):
        type(final).model_validate(tampered)

    candidate = chain["candidate"]
    candidate_payload = candidate.payload
    unread_anchors = (distillation_anchor_binding(chain["catalog"].anchors[1]),)
    unread = candidate_payload.model_copy(
        update={
            "read_anchors": unread_anchors,
            "read_anchors_hash": distillation_anchor_binding_set_hash(unread_anchors),
        }
    )
    with pytest.raises(ValidationError, match="unread anchor"):
        DistillationCandidateOutputPayload.model_validate(
            unread.model_dump(mode="python")
        )


def test_persisted_candidate_recovery_requires_cas_adopt_before_success() -> None:
    chain = _normal_chain()
    candidate = chain["candidate"]
    dossier_payload = DistillationRecoveryDossierPayload(
        prepared_receipt_id=chain["prepared"].receipt_id,
        prepared_receipt_hash=chain["prepared"].receipt_hash,
        last_dispatch_receipt_id=chain["dispatch2"].receipt_id,
        last_dispatch_receipt_hash=chain["dispatch2"].receipt_hash,
        last_turn_result_receipt_id=chain["result2"].receipt_id,
        last_turn_result_receipt_hash=chain["result2"].receipt_hash,
        candidate_state=DistillationCandidateState.PERSISTED,
        candidate_output_receipt_id=candidate.receipt_id,
        candidate_output_receipt_hash=candidate.receipt_hash,
        failure_signature="a" * 64,
    )
    dossier = build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=7,
        parent_receipt_id=candidate.receipt_id,
        expected_invocation_revision=6,
        payload=dossier_payload,
        created_at=NOW,
    )
    decision_payload = DistillationRecoveryDecisionPayload(
        dossier_receipt_id=dossier.receipt_id,
        dossier_receipt_hash=dossier.receipt_hash,
        previous_dossier_revision=1,
        new_dossier_revision=2,
        action=DistillationRecoveryAction.ADOPT_PERSISTED_CANDIDATE,
        candidate_output_receipt_id=candidate.receipt_id,
        candidate_output_receipt_hash=candidate.receipt_hash,
    )
    decision = build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=8,
        parent_receipt_id=dossier.receipt_id,
        expected_invocation_revision=7,
        payload=decision_payload,
        created_at=NOW,
    )
    final = _success_final(chain, ordinal=9, parent=decision)

    validate_distillation_receipt_chain((*chain["receipts"], dossier, decision, final))

    with pytest.raises(ValidationError, match="only adopt recovery"):
        DistillationRecoveryDecisionPayload(
            dossier_receipt_id=dossier.receipt_id,
            dossier_receipt_hash=dossier.receipt_hash,
            previous_dossier_revision=1,
            new_dossier_revision=2,
            action=DistillationRecoveryAction.ADOPT_PERSISTED_CANDIDATE,
        )


def test_unknown_remote_result_can_only_be_abandoned_without_provider_replay() -> None:
    chain = _normal_chain()
    base = (chain["prepared"], chain["dispatch1"])
    dossier_payload = DistillationRecoveryDossierPayload(
        prepared_receipt_id=chain["prepared"].receipt_id,
        prepared_receipt_hash=chain["prepared"].receipt_hash,
        last_dispatch_receipt_id=chain["dispatch1"].receipt_id,
        last_dispatch_receipt_hash=chain["dispatch1"].receipt_hash,
        candidate_state=DistillationCandidateState.ABSENT,
        failure_signature="b" * 64,
    )
    dossier = build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=3,
        parent_receipt_id=chain["dispatch1"].receipt_id,
        expected_invocation_revision=2,
        payload=dossier_payload,
        created_at=NOW,
    )
    decision_payload = DistillationRecoveryDecisionPayload(
        dossier_receipt_id=dossier.receipt_id,
        dossier_receipt_hash=dossier.receipt_hash,
        previous_dossier_revision=1,
        new_dossier_revision=2,
        action=DistillationRecoveryAction.ABANDON_AND_RECOMPILE,
    )
    decision = build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=4,
        parent_receipt_id=dossier.receipt_id,
        expected_invocation_revision=3,
        payload=decision_payload,
        created_at=NOW,
    )
    final_payload = DistillationFinalResultPayload(
        outcome="abandoned",
        provider_turn_count=0,
        candidates=(),
        work_count=0,
        voice_count=0,
        validator_count=0,
        failure_signature="c" * 64,
        provider_replay_count=0,
    )
    final = build_distillation_receipt(
        invocation_id=chain["catalog"].invocation_id,
        ordinal=5,
        parent_receipt_id=decision.receipt_id,
        expected_invocation_revision=4,
        payload=final_payload,
        created_at=NOW,
    )

    validate_distillation_receipt_chain((*base, dossier, decision, final))
    assert dossier.payload.remote_result == "unknown"
    assert dossier.payload.provider_replay_count == 0
    assert final.payload.provider_replay_count == 0


def test_invocation_head_enforces_status_cutpoints_and_exact_success_counts() -> None:
    chain = _normal_chain()
    final = _success_final(chain, ordinal=7, parent=chain["candidate"])
    source, project, knowledge, instruction, provider = _bindings()
    catalog = chain["catalog"]
    material = {
        "invocation_id": catalog.invocation_id,
        "source": source,
        "project": project,
        "knowledge": knowledge,
        "instruction": instruction,
        "provider": provider,
        "anchor_catalog_id": catalog.catalog_id,
        "anchor_catalog_hash": catalog.catalog_hash,
        "retry_of": None,
        "request_hash": chain["prepared"].payload.request_hash,
        "idempotency_key": IDEMPOTENCY_KEY,
        "status": DistillationInvocationStatus.SUCCEEDED,
        "state_revision": 7,
        "provider_replay_count": 0,
        "last_receipt_ordinal": 7,
        "last_receipt_hash": final.receipt_hash,
        "last_dispatch_receipt_id": chain["dispatch2"].receipt_id,
        "last_dispatch_receipt_hash": chain["dispatch2"].receipt_hash,
        "last_dispatch_turn_ordinal": 2,
        "last_turn_result_receipt_id": chain["result2"].receipt_id,
        "last_turn_result_receipt_hash": chain["result2"].receipt_hash,
        "provider_turn_count": 2,
        "provider_turn_chain_hash": final.payload.provider_turn_chain_hash,
        "candidate_output_receipt_id": chain["candidate"].receipt_id,
        "candidate_output_receipt_hash": chain["candidate"].receipt_hash,
        "final_result_receipt_id": final.receipt_id,
        "final_result_receipt_hash": final.receipt_hash,
        "candidate_set_hash": chain["candidate_set"].candidate_set_hash,
        "work_count": 1,
        "voice_count": 1,
        "validator_count": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    draft = DistillationInvocation.model_construct(**material, head_hash="0" * 64)
    head = DistillationInvocation(
        **material,
        head_hash=distillation_invocation_head_hash(draft),
    )
    assert head.status is DistillationInvocationStatus.SUCCEEDED

    invalid = head.model_dump(mode="python")
    invalid["voice_count"] = 0
    invalid["head_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="exactly one card"):
        DistillationInvocation.model_validate(invalid)


def test_credential_reference_rejects_secret_shaped_values() -> None:
    *_, provider = _bindings()
    payload = provider.model_dump(mode="python")
    payload["credential_reference"] = "sk-not-a-reference"
    with pytest.raises(ValidationError, match="must not contain"):
        DistillationProviderBinding.model_validate(payload)


def test_receipt_hash_is_stable_for_equivalent_typed_payload() -> None:
    chain = _normal_chain()
    prepared = chain["prepared"]
    reparsed = type(prepared).model_validate(
        prepared.model_dump(mode="python", by_alias=True)
    )
    assert reparsed == prepared
    assert distillation_receipt_hash(reparsed) == prepared.receipt_hash
