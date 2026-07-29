from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from test_workflow_checkpoint_store import (
    StartupTrapProvider,
    WorkflowFixture,
    append_full_chain,
    app_settings,
    build_fixture,
    executor_identity,
)
from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateContent,
    AgentCandidateOutput,
    AgentCandidateReference,
    AgentCandidateSubmission,
    agent_candidate_content_hash,
    agent_candidate_output_envelope_hash,
    agent_candidate_references_hash,
)
from zhimeng_runtime.agent_store import (
    AgentCandidateOutputConflict,
    AgentContextPackConflict,
    AgentStore,
    AgentStoreInvalidState,
    AgentStoreNotFound,
    ProviderRecoveryConflict,
)
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_tools import CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.provider_recovery_models import ProviderRecoveryDecisionRequest
from zhimeng_runtime.project_kb_service import ProjectKbConflictError
from zhimeng_runtime.provider_turn_models import build_provider_turn_receipt
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
    build_workflow_plan,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "packages" / "contracts" / "agent-candidate-output.schema.json"


def _accept_current_project_kb(target, _connection) -> None:
    assert target.project_id is not None
    assert target.project_kb_state_id is not None
    assert target.project_kb_manifest_hash is not None


class CurrentProjectKbVerifier:
    def require_current_binding(
        self,
        _project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> None:
        del expected_knowledge_fingerprint
        assert project_kb_state_id
        assert project_kb_manifest_hash

    @contextmanager
    def current_binding_lease(self, project_id: str, **kwargs):
        yield self.require_current_binding(project_id, **kwargs)


def _recovery_event_count(fixture: WorkflowFixture, event_type: str) -> int:
    with fixture.database.connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM run_event WHERE run_id = ? AND event_type = ?",
            (fixture.run.run_id, event_type),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _completed_workflow(
    tmp_path: Path,
    *,
    persist_provider_receipt: bool = True,
) -> tuple[
    WorkflowFixture,
    tuple[WorkflowStepCheckpoint, ...],
    ProviderIoBoundary,
]:
    fixture = build_fixture(tmp_path)
    checkpoints = append_full_chain(fixture)
    writer_task = fixture.store.ensure_writer_task(
        fixture.run.run_id,
        target_files=(),
    )
    materialization = fixture.store.ensure_creation_instruction_materialization(
        fixture.run.run_id
    )
    assert materialization.writer_task_id == writer_task.writer_task_id
    boundary = build_provider_io_boundary(
        fixture.plan,
        checkpoints,
        request_hash="1" * 64,
        provider_model="candidate-fixture-model",
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    stored_boundary = fixture.store.create_provider_io_boundary(
        boundary,
        expected_previous_checkpoint_hash=checkpoints[-1].checkpoint_hash,
        provider_messages_hash="2" * 64,
        tool_definitions_hash="3" * 64,
    )
    if persist_provider_receipt:
        fixture.store.create_provider_turn_receipt(
            build_provider_turn_receipt(
                stored_boundary,
                turn_ordinal=1,
                request_hash=stored_boundary.request_hash,
                reported_model=stored_boundary.provider_model,
                accepted_reported_models=(),
                finish_reason="tool_calls",
                usage=None,
                created_at=datetime.now(UTC),
            )
        )
    return fixture, checkpoints, stored_boundary


def _submission(
    checkpoint: WorkflowStepCheckpoint,
    *,
    body: str = "Repair the missing chapter control evidence before formal drafting.",
) -> AgentCandidateSubmission:
    del checkpoint
    return AgentCandidateSubmission(
        kind="repair_plan",
        content=AgentCandidateContent(
            title="Repair the prewrite evidence",
            summary="The workflow remains bounded to repair planning.",
            body=body,
        ),
    )


def _candidate(
    fixture: WorkflowFixture,
    checkpoint: WorkflowStepCheckpoint,
    *,
    body: str = "Repair the missing chapter control evidence before formal drafting.",
    created_at: datetime | None = None,
) -> AgentCandidateOutput:
    return fixture.store.build_agent_candidate_output(
        fixture.run.run_id,
        _submission(
            checkpoint,
            body=body,
        ),
        created_at=created_at or datetime.now(UTC),
    )


def _persist_candidate_cutpoint(
    fixture: WorkflowFixture,
    checkpoint: WorkflowStepCheckpoint,
    *,
    candidate: AgentCandidateOutput | None = None,
    tool_call_id: str = "tool_candidate_recovery_cutpoint",
) -> AgentCandidateOutput:
    submission = _submission(checkpoint)
    candidate = candidate or fixture.store.build_agent_candidate_output(
        fixture.run.run_id,
        submission,
    )
    output = {
        "schema": "zhimeng.tool-result.candidate-output-submit.v1",
        "accepted": True,
        "candidate_kind": submission.kind,
        "requires_author_review": True,
        "formal_draft_allowed": False,
    }
    pending, replayed = fixture.store.begin_tool_call(
        fixture.run.run_id,
        tool_call_id=tool_call_id,
        tool_name=CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
        tool_version="2",
        arguments=submission.model_dump(mode="json", by_alias=True),
        request_hash="7" * 64,
        idempotency_key=tool_call_id,
    )
    assert replayed is False
    assert pending.status == "running"
    return fixture.store.persist_candidate_recovery_cutpoint(
        candidate,
        candidate_tool_call_id=tool_call_id,
        candidate_tool_output=output,
        candidate_tool_duration_ms=12,
    )


def test_candidate_builder_binds_the_real_three_step_workflow(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, boundary = _completed_workflow(tmp_path)

    candidate = _candidate(fixture, checkpoints[-1])

    assert candidate.schema_id == "zhimeng.agent-candidate-output.v3"
    assert candidate.provider_identity_status == "verified"
    assert [item.turn_ordinal for item in candidate.provider_turn_receipts] == [1]
    assert candidate.run_id == fixture.run.run_id
    assert candidate.workflow_plan_id == fixture.plan.workflow_plan_id
    assert candidate.workflow_plan_hash == fixture.plan.plan_hash
    assert candidate.final_checkpoint_id == checkpoints[-1].checkpoint_id
    assert candidate.final_checkpoint_hash == checkpoints[-1].checkpoint_hash
    assert candidate.provider_boundary_id == boundary.boundary_id
    assert candidate.provider_boundary_hash == boundary.boundary_hash
    assert candidate.provider_request_hash == boundary.request_hash
    writer_task = fixture.store.get_writer_task(fixture.run.run_id)
    materialization = fixture.store.get_creation_instruction_materialization(
        fixture.run.run_id
    )
    dispatch = fixture.store.get_creation_instruction_dispatch_receipt(
        fixture.run.run_id
    )
    assert candidate.writer_task_id == writer_task.writer_task_id
    assert candidate.writer_task_hash == writer_task.task_hash
    assert candidate.instruction_materialization_id == materialization.materialization_id
    assert candidate.instruction_materialization_hash == materialization.materialization_hash
    assert candidate.instruction_dispatch_receipt_id == dispatch.dispatch_receipt_id
    assert candidate.instruction_dispatch_receipt_hash == dispatch.receipt_hash
    assert [item.reference_kind for item in candidate.references] == [
        "runtime_identity",
        "artifact",
    ]
    assert candidate.context_pack_id == fixture.pack.context_pack_id
    assert candidate.context_pack_hash == fixture.pack.pack_hash
    assert candidate.operation_package_id == fixture.operation_package.operation_package_id
    assert candidate.operation_package_hash == fixture.operation_package.package_hash
    assert candidate.project_revision == fixture.run.project_revision.revision
    assert candidate.project_fingerprint == fixture.run.project_revision.fingerprint
    assert candidate.handoff_status in {"blocked", "risk_draft_only"}
    assert candidate.allowed_output == "repair_plan_or_risk_draft_only"
    if candidate.handoff_status == "blocked":
        assert candidate.failure_signatures
    assert candidate.formal_draft_allowed is False
    assert candidate.requires_author_review is True
    assert candidate.content_hash == agent_candidate_content_hash(candidate.content)
    assert candidate.references_hash == agent_candidate_references_hash(
        candidate.references
    )
    assert candidate.envelope_hash == agent_candidate_output_envelope_hash(candidate)


def test_candidate_builder_requires_persisted_provider_turn_evidence(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(
        tmp_path,
        persist_provider_receipt=False,
    )

    with pytest.raises(ValueError, match="requires Provider turn receipts"):
        _candidate(fixture, checkpoints[-1])


def test_candidate_contract_is_strict_and_matches_the_exported_schema() -> None:
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == (
        "https://zhimeng.local/contracts/agent-candidate-output.schema.json"
    )
    assert stored == AgentCandidateOutput.model_json_schema(by_alias=True)
    assert stored["additionalProperties"] is False
    assert stored["properties"]["formal_draft_allowed"]["const"] is False
    assert stored["properties"]["requires_author_review"]["const"] is True
    assert set(stored["properties"]["kind"]["enum"]) == {
        "repair_plan",
        "risk_draft",
        "change_proposal",
        "next_step_advice",
    }


def test_candidate_models_reject_formal_drafts_and_tampered_hashes(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _candidate(fixture, checkpoints[-1])

    with pytest.raises(ValidationError, match="kind"):
        AgentCandidateSubmission(
            kind="formal_draft",  # type: ignore[arg-type]
            content=candidate.content,
        )

    payload = candidate.model_dump(mode="python", by_alias=True)
    payload["formal_draft_allowed"] = True
    with pytest.raises(ValidationError, match="formal_draft_allowed"):
        AgentCandidateOutput.model_validate(payload)

    payload = candidate.model_dump(mode="python", by_alias=True)
    payload["content_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="content hash"):
        AgentCandidateOutput.model_validate(payload)

    payload = candidate.model_dump(mode="python", by_alias=True)
    payload["unexpected"] = "forbidden"
    with pytest.raises(ValidationError, match="unexpected"):
        AgentCandidateOutput.model_validate(payload)


def test_store_is_idempotent_for_same_content_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    created_at = datetime.now(UTC)
    first = _candidate(
        fixture,
        checkpoints[-1],
        created_at=created_at,
    )
    equivalent_retry = _candidate(
        fixture,
        checkpoints[-1],
        created_at=created_at + timedelta(seconds=5),
    )

    assert first.envelope_hash != equivalent_retry.envelope_hash
    assert fixture.store.create_agent_candidate_output(first) == first
    assert fixture.store.create_agent_candidate_output(equivalent_retry) == first
    assert fixture.store.get_agent_candidate_output(fixture.run.run_id) == first

    conflicting = _candidate(
        fixture,
        checkpoints[-1],
        body="Use a different repair plan with conflicting immutable content.",
    )
    with pytest.raises(AgentCandidateOutputConflict) as conflict:
        fixture.store.create_agent_candidate_output(conflicting)
    assert conflict.value.code == "agent_candidate_output_conflict"

    with fixture.database.connection() as connection:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_candidate_output WHERE run_id = ?",
                (fixture.run.run_id,),
            ).fetchone()[0]
        )
    assert count == 1


def test_submission_forbids_provider_references_and_store_rejects_forged_binding(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _candidate(fixture, checkpoints[-1])

    with pytest.raises(ValidationError, match="references"):
        AgentCandidateSubmission.model_validate(
            {
                "kind": "repair_plan",
                "content": candidate.content.model_dump(mode="json"),
                "references": [
                    {
                        "reference_kind": "artifact",
                        "reference_id": "artifact_untrusted",
                        "reference_hash": "2" * 64,
                    }
                ],
            }
        )

    forged_references = (
        AgentCandidateReference(
            reference_kind="runtime_identity",
            reference_id=candidate.writer_task_id,
            reference_hash="0" * 64,
        ),
        candidate.references[1],
    )
    forged_draft = candidate.model_copy(
        update={
            "writer_task_hash": "0" * 64,
            "references": forged_references,
            "references_hash": agent_candidate_references_hash(forged_references),
        }
    )
    forged = AgentCandidateOutput.model_validate(
        forged_draft.model_copy(
            update={
                "envelope_hash": agent_candidate_output_envelope_hash(forged_draft)
            }
        ).model_dump(mode="python", by_alias=True)
    )
    with pytest.raises(AgentCandidateOutputConflict) as conflict:
        fixture.store.create_agent_candidate_output(forged)
    assert conflict.value.code == "agent_candidate_output_writer_binding_mismatch"


def test_builder_adds_only_canonical_runtime_owned_references(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _candidate(fixture, checkpoints[-1])

    assert len(candidate.references) == 2
    assert candidate.references[0].reference_id == candidate.writer_task_id
    assert (
        candidate.references[1].reference_id
        == candidate.instruction_materialization_id
    )
    assert fixture.store.create_agent_candidate_output(candidate) == candidate


def test_candidate_cutpoint_survives_phase_two_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    submission = _submission(checkpoints[-1])
    candidate = fixture.store.build_agent_candidate_output(
        fixture.run.run_id,
        submission,
    )
    candidate_tool_call_id = "tool_candidate_atomic_completion"
    candidate_tool_output = {
        "schema": "zhimeng.tool-result.candidate-output-submit.v1",
        "accepted": True,
        "candidate_kind": submission.kind,
        "requires_author_review": True,
        "formal_draft_allowed": False,
    }
    pending_call, replayed = fixture.store.begin_tool_call(
        fixture.run.run_id,
        tool_call_id=candidate_tool_call_id,
        tool_name=CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
        tool_version="2",
        arguments=submission.model_dump(mode="json", by_alias=True),
        request_hash="7" * 64,
        idempotency_key=candidate_tool_call_id,
    )
    assert replayed is False
    assert pending_call.status == "running"

    original_complete = fixture.store._complete_run_in_connection

    def fail_after_writer_result(*args, **kwargs):
        del args, kwargs
        raise AgentStoreInvalidState("injected phase-two failure")

    monkeypatch.setattr(
        fixture.store,
        "_complete_run_in_connection",
        fail_after_writer_result,
    )
    with pytest.raises(AgentStoreInvalidState, match="phase-two failure"):
        fixture.store.complete_run_with_candidate_output(
            candidate,
            candidate_tool_call_id=candidate_tool_call_id,
            candidate_tool_output=candidate_tool_output,
            candidate_tool_duration_ms=12,
            message_id="message_candidate_rollback",
            usage=None,
        )
    assert fixture.store.get_agent_candidate_output(fixture.run.run_id) == candidate
    persisted_call = fixture.store.get_tool_call(candidate_tool_call_id)
    assert persisted_call.status == "succeeded"
    assert persisted_call.output == candidate_tool_output
    with pytest.raises(AgentStoreNotFound):
        fixture.store.get_writer_candidate_result(fixture.run.run_id)
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.RUNNING
    monkeypatch.setattr(
        fixture.store,
        "_complete_run_in_connection",
        original_complete,
    )

    failed = fixture.store.fail_run(
        fixture.run.run_id,
        code="candidate_terminal_commit_failed",
        message="injected phase-two failure",
        retryable=False,
    )
    assert failed.status is RunStatus.FAILED
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert pending.dossier.candidate_state == "persisted"
    with pytest.raises(AgentStoreInvalidState, match="running Run"):
        fixture.store.finalize_persisted_candidate(
            fixture.run.run_id,
            expected_candidate_output_id=candidate.candidate_output_id,
            expected_candidate_output_hash=candidate.envelope_hash,
            message_id="message_candidate_forbidden_auto_finalize",
            usage=None,
        )
    decided = fixture.store.decide_provider_recovery(
        fixture.run.run_id,
        ProviderRecoveryDecisionRequest(
            expected_dossier_revision=pending.dossier.dossier_revision,
            expected_dossier_hash=pending.dossier.dossier_hash,
            action="adopt_persisted_candidate",
        ),
        current_project_kb_verifier=_accept_current_project_kb,
    )
    assert decided.decision is not None
    assert decided.decision.action == "adopt_persisted_candidate"
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.SUCCEEDED
    completed_call = fixture.store.get_tool_call(candidate_tool_call_id)
    assert completed_call.status == "succeeded"
    assert completed_call.output == candidate_tool_output
    assert fixture.store.get_agent_candidate_output(fixture.run.run_id) == candidate
    writer_result = fixture.store.get_writer_candidate_result(fixture.run.run_id)
    assert writer_result.candidate_output_id == candidate.candidate_output_id


def test_non_handoff_managed_workflow_can_complete_without_candidate(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path, create_plan=False)
    legacy_plan = build_workflow_plan(
        run_id=fixture.run.run_id,
        project_id=fixture.pack.project_id,
        project_revision=fixture.pack.project_revision,
        project_fingerprint=fixture.pack.project_fingerprint,
        context_pack_id=fixture.pack.context_pack_id,
        context_pack_hash=fixture.pack.pack_hash,
        knowledge_fingerprint=fixture.pack.knowledge_fingerprint,
        route_receipt_id=fixture.route.route_receipt_id,
        route_receipt_hash=fixture.route.receipt_hash,
        operation_package_id=fixture.operation_package.operation_package_id,
        operation_package_hash=fixture.operation_package.package_hash,
        steps=(fixture.plan.steps[0],),
        created_at=datetime.now(UTC),
    )
    # Older runtimes could persist other managed plans. Insert that historical
    # shape directly because today's NLCL compiler always emits three steps.
    with fixture.database.connection() as connection:
        connection.execute(
            """
            INSERT INTO checkpoint(
                id, run_id, sequence, checkpoint_type, state_json, created_at
            ) VALUES (?, ?, 0, 'workflow_plan', ?, ?)
            """,
            (
                legacy_plan.workflow_plan_id,
                legacy_plan.run_id,
                json.dumps(
                    legacy_plan.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                legacy_plan.created_at.isoformat(),
            ),
        )

    run, message = fixture.store.complete_run(
        fixture.run.run_id,
        message_id="message_legacy_workflow_completed",
        content="Legacy managed workflow result.",
        finish_reason="stop",
        usage=None,
    )

    assert run.status is RunStatus.SUCCEEDED
    assert message.content == "Legacy managed workflow result."
    with pytest.raises(AgentStoreNotFound):
        fixture.store.get_agent_candidate_output(fixture.run.run_id)


def test_candidate_tool_call_uses_the_advertised_definition_version(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    definition = fixture.tool_executor.candidate_output_definition.model_copy(
        update={"version": "7"}
    )
    fixture.tool_executor.candidate_output_definition = definition
    fixture.tool_executor._definitions_by_name[
        CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
    ] = definition
    service = AgentService(
        fixture.store,
        None,
        tool_executor=fixture.tool_executor,
    )

    result = asyncio.run(
        service._execute_tool_call(
            fixture.store.get_run(fixture.run.run_id),
            provider_tool_call_id="candidate_version_call_0001",
            tool_name=CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
            arguments=_submission(checkpoints[-1]).model_dump(mode="json"),
            cancel_event=asyncio.Event(),
        )
    )

    assert result.tool_call.tool_version == definition.version
    assert result.candidate_submission is not None


def test_provider_recovery_adopts_a_persisted_candidate_with_cas(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    reopened_database = SqliteStore(fixture.database.path)
    reopened_database.initialize()
    reopened = AgentStore(reopened_database)
    assert reopened.get_agent_candidate_output(fixture.run.run_id) == candidate
    reopened.mark_interrupted(
        fixture.run.run_id,
        detail="test interrupted after Provider boundary",
    )

    pending = reopened.get_provider_recovery(fixture.run.run_id)
    assert pending.dossier.candidate_state == "persisted"
    assert pending.dossier.remote_result == "unknown"
    assert pending.dossier.provider_replay_count == 0
    assert pending.decision is None

    decided = reopened.decide_provider_recovery(
        fixture.run.run_id,
        ProviderRecoveryDecisionRequest(
            expected_dossier_revision=pending.dossier.dossier_revision,
            expected_dossier_hash=pending.dossier.dossier_hash,
            action="adopt_persisted_candidate",
        ),
        current_project_kb_verifier=_accept_current_project_kb,
    )
    assert decided.decision is not None
    assert decided.decision.action == "adopt_persisted_candidate"
    assert decided.decision.resulting_run_status is RunStatus.SUCCEEDED
    assert reopened.get_run(fixture.run.run_id).status is RunStatus.SUCCEEDED
    assert (
        reopened.get_provider_recovery(fixture.run.run_id).decision
        == decided.decision
    )

    with pytest.raises(ProviderRecoveryConflict) as stale:
        reopened.decide_provider_recovery(
            fixture.run.run_id,
            ProviderRecoveryDecisionRequest(
                expected_dossier_revision=pending.dossier.dossier_revision,
                expected_dossier_hash=pending.dossier.dossier_hash,
                action="abandon_and_recompile",
            ),
        )
    assert stale.value.code == "provider_recovery_already_decided"


def test_provider_recovery_rejects_candidate_replacement_after_dossier_capture(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    original = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="capture recovery dossier before candidate replacement",
    )
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    replacement = _candidate(
        fixture,
        checkpoints[-1],
        body="A different internally valid repair plan.",
        created_at=original.created_at,
    )
    assert replacement.candidate_output_id == original.candidate_output_id
    assert replacement.envelope_hash != original.envelope_hash
    replacement_submission = _submission(
        checkpoints[-1],
        body=replacement.content.body,
    )
    with fixture.database.connection() as connection:
        connection.execute(
            """
            UPDATE agent_candidate_output
            SET content_hash = ?, envelope_hash = ?, payload_json = ?
            WHERE run_id = ?
            """,
            (
                replacement.content_hash,
                replacement.envelope_hash,
                json.dumps(
                    replacement.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                fixture.run.run_id,
            ),
        )
        connection.execute(
            """
            UPDATE tool_call SET input_json = ?
            WHERE run_id = ? AND tool_name = ?
            """,
            (
                json.dumps(
                    replacement_submission.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                fixture.run.run_id,
                CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
            ),
        )

    with pytest.raises(ProviderRecoveryConflict) as changed:
        fixture.store.decide_provider_recovery(
            fixture.run.run_id,
            ProviderRecoveryDecisionRequest(
                expected_dossier_revision=pending.dossier.dossier_revision,
                expected_dossier_hash=pending.dossier.dossier_hash,
                action="adopt_persisted_candidate",
            ),
            current_project_kb_verifier=_accept_current_project_kb,
        )

    assert changed.value.code == "provider_recovery_candidate_changed"
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED
    assert fixture.store.get_provider_recovery(fixture.run.run_id).decision is None
    assert _recovery_event_count(fixture, "provider_recovery_decision") == 0


def test_provider_recovery_rechecks_project_kb_inside_decision_transaction(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    _persist_candidate_cutpoint(fixture, checkpoints[-1])
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="project KB changes after recovery precheck",
    )
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)

    class RebuiltProjectKbVerifier:
        def __init__(self) -> None:
            self.calls = 0

        def require_current_binding(self, *_args, **_kwargs) -> None:
            self.calls += 1
            if self.calls == 2:
                raise ProjectKbConflictError(
                    "project KB was rebuilt after recovery precheck",
                    details=[{"code": "project_kb_binding_changed"}],
                    retryable=True,
                )

        @contextmanager
        def current_binding_lease(self, project_id: str, **kwargs):
            yield self.require_current_binding(project_id, **kwargs)

    verifier = RebuiltProjectKbVerifier()
    service = AgentService(
        fixture.store,
        None,
        project_kb_service=verifier,  # type: ignore[arg-type]
    )
    request = ProviderRecoveryDecisionRequest(
        expected_dossier_revision=pending.dossier.dossier_revision,
        expected_dossier_hash=pending.dossier.dossier_hash,
        action="adopt_persisted_candidate",
    )

    with pytest.raises(AgentContextPackConflict) as changed:
        asyncio.run(service.decide_provider_recovery(fixture.run.run_id, request))

    assert verifier.calls == 2
    assert changed.value.code == "project_kb_binding_changed"
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED
    assert fixture.store.get_provider_recovery(fixture.run.run_id).decision is None
    assert _recovery_event_count(fixture, "provider_recovery_decision") == 0


def test_app_startup_recovers_a_natural_candidate_cutpoint_without_provider_replay(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    provider = StartupTrapProvider()
    app = create_app(app_settings(fixture), agent_provider=provider)
    app.state.agent_service.project_kb_service = CurrentProjectKbVerifier()

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        run_response = client.get(f"/api/v1/agent/runs/{fixture.run.run_id}")
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["run"]["status"] == "interrupted"

        recovery_response = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery"
        )
        assert recovery_response.status_code == 200, recovery_response.text
        recovery = recovery_response.json()
        dossier = recovery["dossier"]
        assert recovery["decision"] is None
        assert dossier["candidate_state"] == "persisted"
        assert dossier["candidate_output_id"] == candidate.candidate_output_id
        assert dossier["candidate_output_hash"] == candidate.envelope_hash
        assert dossier["remote_result"] == "unknown"
        assert dossier["provider_replay_count"] == 0
        assert provider.call_count == 0

        decision_response = client.post(
            f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery/decision",
            json={
                "schema": "zhimeng.provider-recovery-decision-request.v1",
                "expected_dossier_revision": dossier["dossier_revision"],
                "expected_dossier_hash": dossier["dossier_hash"],
                "action": "adopt_persisted_candidate",
            },
        )
        assert decision_response.status_code == 200, decision_response.text
        assert decision_response.json()["decision"]["action"] == (
            "adopt_persisted_candidate"
        )
        terminal_response = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}"
        )
        assert terminal_response.status_code == 200, terminal_response.text
        assert terminal_response.json()["run"]["status"] == "succeeded"
        assert provider.call_count == 0

    assert fixture.store.list_nonterminal_workflow_run_ids() == []
    writer_result = fixture.store.get_writer_candidate_result(fixture.run.run_id)
    assert writer_result.candidate_output_id == candidate.candidate_output_id


def test_provider_recovery_rejects_a_bare_candidate_without_submit_receipt(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _candidate(fixture, checkpoints[-1])
    fixture.store.create_agent_candidate_output(candidate)
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="test bare candidate is not recoverable",
    )

    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert pending.dossier.candidate_state == "invalid"
    assert pending.dossier.candidate_output_id is None
    assert pending.dossier.candidate_output_hash is None
    with pytest.raises(ProviderRecoveryConflict) as conflict:
        fixture.store.decide_provider_recovery(
            fixture.run.run_id,
            ProviderRecoveryDecisionRequest(
                expected_dossier_revision=pending.dossier.dossier_revision,
                expected_dossier_hash=pending.dossier.dossier_hash,
                action="adopt_persisted_candidate",
            ),
        )
    assert conflict.value.code == "provider_recovery_candidate_missing"


def test_live_finalizer_refuses_a_pending_recovery_dossier(
    tmp_path: Path,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    dossier = fixture.store.ensure_provider_recovery_dossier(fixture.run.run_id)

    assert dossier.candidate_state == "persisted"
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.RUNNING
    with pytest.raises(AgentStoreInvalidState, match="author decision"):
        fixture.store.finalize_persisted_candidate(
            fixture.run.run_id,
            expected_candidate_output_id=candidate.candidate_output_id,
            expected_candidate_output_hash=candidate.envelope_hash,
            message_id="message_candidate_dossier_forbidden",
            usage=None,
        )


def test_recovery_decision_rollback_keeps_candidate_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, checkpoints, _ = _completed_workflow(tmp_path)
    candidate = _persist_candidate_cutpoint(fixture, checkpoints[-1])
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="test recovery decision rollback",
    )
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    request = ProviderRecoveryDecisionRequest(
        expected_dossier_revision=pending.dossier.dossier_revision,
        expected_dossier_hash=pending.dossier.dossier_hash,
        action="adopt_persisted_candidate",
    )
    original_insert = fixture.store._insert_provider_recovery_event

    def fail_after_decision_insert(*args, **kwargs):
        original_insert(*args, **kwargs)
        if kwargs["event_type"] == "provider_recovery_decision":
            raise RuntimeError("injected recovery decision failure")

    monkeypatch.setattr(
        fixture.store,
        "_insert_provider_recovery_event",
        fail_after_decision_insert,
    )
    with pytest.raises(RuntimeError, match="decision failure"):
        fixture.store.decide_provider_recovery(
            fixture.run.run_id,
            request,
            current_project_kb_verifier=_accept_current_project_kb,
        )

    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED
    assert fixture.store.get_agent_candidate_output(fixture.run.run_id) == candidate
    with pytest.raises(AgentStoreNotFound):
        fixture.store.get_writer_candidate_result(fixture.run.run_id)
    rolled_back = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert rolled_back.dossier == pending.dossier
    assert rolled_back.decision is None

    monkeypatch.setattr(
        fixture.store,
        "_insert_provider_recovery_event",
        original_insert,
    )
    decided = fixture.store.decide_provider_recovery(
        fixture.run.run_id,
        request,
        current_project_kb_verifier=_accept_current_project_kb,
    )
    assert decided.decision is not None
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.SUCCEEDED


def test_provider_recovery_abandon_is_explicit_and_never_replays(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="unknown remote result",
    )
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    decided = fixture.store.decide_provider_recovery(
        fixture.run.run_id,
        ProviderRecoveryDecisionRequest(
            expected_dossier_revision=pending.dossier.dossier_revision,
            expected_dossier_hash=pending.dossier.dossier_hash,
            action="abandon_and_recompile",
        ),
    )
    assert decided.decision is not None
    assert decided.decision.action == "abandon_and_recompile"
    assert decided.decision.resulting_run_status is RunStatus.FAILED
    run = fixture.store.get_run(fixture.run.run_id)
    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert run.error.error.code == "provider_result_abandoned"
    recovery = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert recovery.dossier.remote_result == "unknown"
    assert recovery.dossier.provider_replay_count == 0


def test_provider_failure_after_boundary_creates_one_no_replay_dossier(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)

    failed = fixture.store.fail_run(
        fixture.run.run_id,
        code="agent_provider_incomplete_stream",
        message="Provider stream ended after dispatch",
        retryable=True,
    )
    repeated = fixture.store.fail_run(
        fixture.run.run_id,
        code="agent_provider_incomplete_stream",
        message="Provider stream ended after dispatch",
        retryable=True,
    )

    recovery = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert failed.status is RunStatus.FAILED
    assert repeated == failed
    assert recovery.dossier.run_status_at_capture is RunStatus.RUNNING
    assert recovery.dossier.remote_result == "unknown"
    assert recovery.dossier.provider_replay_count == 0
    assert _recovery_event_count(fixture, "provider_recovery_dossier") == 1


def test_provider_recovery_rejects_a_tampered_dossier_payload(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="unknown remote result",
    )
    with fixture.database.connection() as connection:
        row = connection.execute(
            """
            SELECT sequence, payload_json FROM run_event
            WHERE run_id = ? AND event_type = 'provider_recovery_dossier'
            """,
            (fixture.run.run_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        payload["dossier_hash"] = "0" * 64
        connection.execute(
            "UPDATE run_event SET payload_json = ? WHERE run_id = ? AND sequence = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                fixture.run.run_id,
                int(row["sequence"]),
            ),
        )

    with pytest.raises(ProviderRecoveryConflict) as conflict:
        fixture.store.get_provider_recovery(fixture.run.run_id)
    assert conflict.value.code == "provider_recovery_integrity_failed"


def test_provider_recovery_rejects_a_valid_dossier_from_another_run(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path / "first")
    other, _, _ = _completed_workflow(tmp_path / "other")
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="unknown remote result",
    )
    other.store.mark_interrupted(
        other.run.run_id,
        detail="other unknown remote result",
    )
    other_dossier = other.store.get_provider_recovery(other.run.run_id).dossier
    with fixture.database.connection() as connection:
        connection.execute(
            """
            UPDATE run_event SET payload_json = ?
            WHERE run_id = ? AND event_type = 'provider_recovery_dossier'
            """,
            (
                json.dumps(
                    other_dossier.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                fixture.run.run_id,
            ),
        )

    with pytest.raises(ProviderRecoveryConflict) as conflict:
        fixture.store.get_provider_recovery(fixture.run.run_id)
    assert conflict.value.code == "provider_recovery_integrity_failed"


def test_graceful_shutdown_after_boundary_creates_one_no_replay_dossier(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)
    service = AgentService(fixture.store, None)

    asyncio.run(service.shutdown())
    asyncio.run(service.shutdown())

    recovery = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert fixture.store.get_run(fixture.run.run_id).status is RunStatus.INTERRUPTED
    assert recovery.dossier.remote_result == "unknown"
    assert recovery.dossier.provider_replay_count == 0
    assert _recovery_event_count(fixture, "provider_recovery_dossier") == 1


def test_concurrent_provider_recovery_decisions_commit_once_with_cas(
    tmp_path: Path,
) -> None:
    fixture, _, _ = _completed_workflow(tmp_path)
    fixture.store.mark_interrupted(
        fixture.run.run_id,
        detail="unknown remote result",
    )
    pending = fixture.store.get_provider_recovery(fixture.run.run_id)
    request = ProviderRecoveryDecisionRequest(
        expected_dossier_revision=pending.dossier.dossier_revision,
        expected_dossier_hash=pending.dossier.dossier_hash,
        action="abandon_and_recompile",
    )
    barrier = Barrier(2)

    def decide() -> object:
        barrier.wait(timeout=5)
        try:
            return fixture.store.decide_provider_recovery(
                fixture.run.run_id,
                request,
            )
        except ProviderRecoveryConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: decide(), range(2)))

    conflicts = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, ProviderRecoveryConflict)
    ]
    successes = [
        outcome
        for outcome in outcomes
        if not isinstance(outcome, ProviderRecoveryConflict)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "provider_recovery_already_decided"
    recovery = fixture.store.get_provider_recovery(fixture.run.run_id)
    assert recovery.decision is not None
    assert recovery.decision.action == "abandon_and_recompile"
    assert recovery.dossier.provider_replay_count == 0
    assert _recovery_event_count(fixture, "provider_recovery_dossier") == 1
    assert _recovery_event_count(fixture, "provider_recovery_decision") == 1


def test_v16_database_migrates_candidate_output_to_current_schema(
    tmp_path: Path,
) -> None:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    with database.connection() as connection:
        connection.execute("DROP TABLE agent_candidate_output")
        connection.execute("PRAGMA user_version = 16")

    health = database.initialize()

    # The source-import vertical may add a later migration, but this fixture
    # must still prove that a v16 database reaches the runtime's current
    # schema and recreates the candidate table contract.
    assert SCHEMA_VERSION >= 18
    assert health.ready is True
    assert health.schema_version == SCHEMA_VERSION
    with database.connection() as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(agent_candidate_output)"
            ).fetchall()
        }
    assert {
        "handoff_status",
        "allowed_output",
        "failure_signatures_json",
        "formal_draft_allowed",
        "requires_author_review",
        "content_hash",
        "references_hash",
        "envelope_hash",
    }.issubset(columns)
