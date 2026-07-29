from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from scripts.export_workflow_contracts import MODELS, render_schema
from zhimeng_runtime.execution_protocol import ExecutorIdentity, ExecutorKind
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStep,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
    build_workflow_plan,
    build_workflow_step,
    build_workflow_step_checkpoint,
    provider_io_boundary_hash,
    validate_workflow_checkpoint_chain,
    validate_workflow_step_checkpoint,
    workflow_plan_hash,
    workflow_step_checkpoint_hash,
    workflow_step_hash,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
CREATED_AT = datetime(2026, 7, 21, 12, 30, tzinfo=UTC)


def _sha(character: str) -> str:
    return character * 64


def _step(
    ordinal: int,
    *,
    max_attempts: int = 1,
    continue_on_domain_status: tuple[str, ...] = (
        "healthy",
        "degraded",
        "blocked",
    ),
) -> WorkflowStep:
    suffix = format(ordinal, "x")
    skill_name = (
        "novel-creation-suite.project-audit"
        if ordinal == 1
        else "novel-creation-suite.prewrite-gate"
    )
    return build_workflow_step(
        ordinal=ordinal,
        skill_id=skill_name,
        expected_skill_version_id=f"skill-version-{suffix * 32}",
        expected_skill_version=f"2026.07.21.{ordinal}",
        expected_manifest_fingerprint=_sha(suffix),
        expected_package_fingerprint=_sha(format(ordinal + 2, "x")),
        input_contract=(
            "https://zhimeng.local/contracts/"
            "creation-project-audit-request.schema.json"
            if ordinal == 1
            else (
                "https://zhimeng.local/contracts/"
                "creation-prewrite-gate-request.schema.json"
            )
        ),
        output_contract=(
            "https://zhimeng.local/contracts/"
            "creation-project-audit-result.schema.json"
            if ordinal == 1
            else (
                "https://zhimeng.local/contracts/"
                "creation-prewrite-gate-result.schema.json"
            )
        ),
        input_builder_id=(
            "project-audit-request" if ordinal == 1 else "prewrite-gate-request"
        ),
        input_builder_version="1",
        max_attempts=max_attempts,  # type: ignore[arg-type]
        continue_on_domain_status=continue_on_domain_status,  # type: ignore[arg-type]
    )


def _plan(
    *,
    steps: tuple[WorkflowStep, ...] | None = None,
    knowledge_fingerprint: str | None = None,
) -> WorkflowPlan:
    return build_workflow_plan(
        run_id="run_workflow_contract_0001",
        project_id="project-" + "1" * 24,
        project_revision=7,
        project_fingerprint=_sha("a"),
        context_pack_id="context-pack-workflow-0001",
        context_pack_hash=_sha("b"),
        knowledge_fingerprint=knowledge_fingerprint,
        route_receipt_id="nlcl-route-" + "c" * 32,
        route_receipt_hash=_sha("d"),
        operation_package_id="prewrite-package-" + "e" * 32,
        operation_package_hash=_sha("f"),
        steps=steps or (_step(1), _step(2)),
        created_at=CREATED_AT,
    )


def _successful_checkpoint(
    plan: WorkflowPlan,
    step: WorkflowStep,
    *,
    attempt_ordinal: int = 1,
    previous_checkpoint: WorkflowStepCheckpoint | None = None,
    domain_status: str = "blocked",
) -> WorkflowStepCheckpoint:
    marker = format(step.ordinal + attempt_ordinal, "x")
    return build_workflow_step_checkpoint(
        plan,
        step,
        attempt_ordinal=attempt_ordinal,
        previous_checkpoint=previous_checkpoint,
        tool_call_id=f"tool_workflow_{step.ordinal}_{attempt_ordinal}",
        provider_tool_call_id=(
            f"provider_tool_workflow_{step.ordinal}_{attempt_ordinal}"
        ),
        skill_call_id=f"skill_call_workflow_{step.ordinal}_{attempt_ordinal}",
        skill_receipt_id="skill-receipt-" + marker * 32,
        skill_receipt_hash=_sha(marker),
        route_execution_receipt_id="nlcl-route-exec-" + marker * 32,
        route_execution_receipt_hash=_sha(format(step.ordinal + 4, "x")),
        input_hash=_sha(format(step.ordinal + 5, "x")),
        output_hash=_sha(format(step.ordinal + 6, "x")),
        tool_result_hash=_sha(format(step.ordinal + 7, "x")),
        execution_status="succeeded",
        domain_status=domain_status,  # type: ignore[arg-type]
        error_code=None,
        error_message=None,
        created_at=CREATED_AT + timedelta(seconds=step.ordinal + attempt_ordinal),
    )


def _failed_checkpoint(
    plan: WorkflowPlan,
    step: WorkflowStep,
    *,
    attempt_ordinal: int,
    previous_checkpoint: WorkflowStepCheckpoint | None,
) -> WorkflowStepCheckpoint:
    return build_workflow_step_checkpoint(
        plan,
        step,
        attempt_ordinal=attempt_ordinal,
        previous_checkpoint=previous_checkpoint,
        tool_call_id=None,
        provider_tool_call_id=None,
        skill_call_id=None,
        skill_receipt_id=None,
        skill_receipt_hash=None,
        route_execution_receipt_id=None,
        route_execution_receipt_hash=None,
        input_hash=None,
        output_hash=None,
        tool_result_hash=None,
        execution_status="failed",
        domain_status=None,
        error_code="input_builder_failed",
        error_message="Fixture input builder failed before Skill dispatch.",
        created_at=CREATED_AT + timedelta(seconds=attempt_ordinal),
    )


def _executor() -> ExecutorIdentity:
    return ExecutorIdentity(
        executor_id="executor.openai-compatible",
        kind=ExecutorKind.REMOTE,
        implementation="openai-compatible-chat-completions",
        version="0.1.0",
        protocol_version="1",
        instance_id=f"provider-config-{'a' * 64}",
    )


def _completed_chain(
    *,
    knowledge_fingerprint: str | None = None,
) -> tuple[WorkflowPlan, WorkflowStepCheckpoint, WorkflowStepCheckpoint]:
    plan = _plan(knowledge_fingerprint=knowledge_fingerprint)
    first = _successful_checkpoint(plan, plan.steps[0])
    second = _successful_checkpoint(
        plan,
        plan.steps[1],
        previous_checkpoint=first,
    )
    validate_workflow_checkpoint_chain(plan, (first, second))
    return plan, first, second


def _boundary(
    *,
    provider_model: str | None = "gpt-fixture",
) -> ProviderIoBoundary:
    plan, first, final = _completed_chain()
    return build_provider_io_boundary(
        plan,
        (first, final),
        request_hash=_sha("1"),
        provider_model=provider_model,
        provider_dispatch_config_fingerprint=_sha("a"),
        executor=_executor(),
        created_at=CREATED_AT,
    )


def _rehash_checkpoint(payload: dict[str, object]) -> WorkflowStepCheckpoint:
    draft = WorkflowStepCheckpoint.model_construct(
        **{
            key: value
            for key, value in payload.items()
            if key != "checkpoint_hash"
        },
        checkpoint_hash="0" * 64,
    )
    payload["checkpoint_hash"] = workflow_step_checkpoint_hash(draft)
    return WorkflowStepCheckpoint.model_validate(payload)


def _assert_rejected_by_model_and_schema(
    model: type,
    schema_name: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))
    schema = json.loads(render_schema(schema_name, model))
    assert list(Draft202012Validator(schema).iter_errors(payload))


def test_workflow_plan_is_linear_hash_bound_and_formal_draft_forbidden() -> None:
    plan = _plan(knowledge_fingerprint=_sha("9"))

    assert [step.ordinal for step in plan.steps] == [1, 2]
    assert {step.runner for step in plan.steps} == {"managed_skill"}
    assert plan.knowledge_fingerprint == _sha("9")
    assert plan.formal_draft_allowed is False
    assert all(step.formal_draft_allowed is False for step in plan.steps)
    assert workflow_plan_hash(plan) == plan.plan_hash
    assert all(workflow_step_hash(step) == step.step_hash for step in plan.steps)

    same = _plan(knowledge_fingerprint=_sha("9"))
    assert same.workflow_plan_id == plan.workflow_plan_id
    assert same.plan_hash == plan.plan_hash

    later = build_workflow_plan(
        run_id=plan.run_id,
        project_id=plan.project_id,
        project_revision=plan.project_revision,
        project_fingerprint=plan.project_fingerprint,
        context_pack_id=plan.context_pack_id,
        context_pack_hash=plan.context_pack_hash,
        knowledge_fingerprint=plan.knowledge_fingerprint,
        route_receipt_id=plan.route_receipt_id,
        route_receipt_hash=plan.route_receipt_hash,
        operation_package_id=plan.operation_package_id,
        operation_package_hash=plan.operation_package_hash,
        steps=plan.steps,
        created_at=plan.created_at + timedelta(seconds=1),
    )
    assert later.workflow_plan_id != plan.workflow_plan_id
    assert later.plan_hash != plan.plan_hash


def test_workflow_steps_require_builder_identity_and_pinned_skill_artifacts() -> None:
    step = _step(1)
    payload = step.model_dump(mode="python")

    for field in (
        "input_builder_id",
        "input_builder_version",
        "expected_skill_version_id",
        "expected_skill_version",
        "expected_manifest_fingerprint",
        "expected_package_fingerprint",
        "input_contract",
        "output_contract",
    ):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError):
            WorkflowStep.model_validate(missing)

    changed = dict(payload)
    changed["input_builder_version"] = "2"
    with pytest.raises(ValidationError, match="step hash"):
        WorkflowStep.model_validate(changed)


@pytest.mark.parametrize(
    "contract",
    [
        "zhimeng.creation-project-audit-request.v1",
        "https://evil.invalid/contracts/input.schema.json",
        "https://zhimeng.local/contracts/../input.schema.json",
        "https://zhimeng.local/contracts/input.json",
    ],
)
def test_workflow_steps_require_managed_contract_uris(contract: str) -> None:
    with pytest.raises(ValidationError):
        build_workflow_step(
            ordinal=1,
            skill_id="novel-creation-suite.project-audit",
            expected_skill_version_id="skill-version-" + "1" * 32,
            expected_skill_version="1",
            expected_manifest_fingerprint=_sha("2"),
            expected_package_fingerprint=_sha("3"),
            input_contract=contract,
            output_contract=contract,
            input_builder_id="project-audit-request",
            input_builder_version="1",
        )


@pytest.mark.parametrize("extra_field", ["depends_on", "when", "expression", "dag"])
def test_v1_rejects_dag_and_expression_fields(extra_field: str) -> None:
    plan_payload = _plan().model_dump(mode="python", by_alias=True)
    plan_payload[extra_field] = []
    with pytest.raises(ValidationError):
        WorkflowPlan.model_validate(plan_payload)

    step_payload = _step(1).model_dump(mode="python")
    step_payload[extra_field] = "step[0].output"
    with pytest.raises(ValidationError):
        WorkflowStep.model_validate(step_payload)


def test_v1_rejects_non_managed_runner_and_noncanonical_domain_statuses() -> None:
    payload = _step(1).model_dump(mode="python")
    payload["runner"] = "shell"
    with pytest.raises(ValidationError):
        WorkflowStep.model_validate(payload)

    with pytest.raises(ValidationError, match="canonical order"):
        _step(1, continue_on_domain_status=("blocked", "healthy"))
    with pytest.raises(ValidationError, match="must be unique"):
        _step(1, continue_on_domain_status=("healthy", "healthy"))
    with pytest.raises(ValidationError):
        _step(1, max_attempts=2)


def test_plan_rejects_gaps_reordering_duplicate_ids_and_hash_tampering() -> None:
    with pytest.raises(ValidationError, match="contiguous ordered ordinals"):
        _plan(steps=(_step(2), _step(1)))

    first = _step(1)
    second_payload = _step(2).model_dump(mode="python")
    second_payload["step_id"] = first.step_id
    draft = WorkflowStep.model_construct(
        **{key: value for key, value in second_payload.items() if key != "step_hash"},
        step_hash="0" * 64,
    )
    second_payload["step_hash"] = workflow_step_hash(draft)
    second = WorkflowStep.model_validate(second_payload)
    with pytest.raises(ValidationError, match="step IDs must be unique"):
        _plan(steps=(first, second))

    payload = _plan().model_dump(mode="python", by_alias=True)
    payload["project_revision"] = 8
    with pytest.raises(ValidationError, match="plan hash"):
        WorkflowPlan.model_validate(payload)


def test_checkpoint_binds_plan_step_attempt_receipts_and_hash() -> None:
    plan, first, second = _completed_chain(knowledge_fingerprint=_sha("9"))

    assert first.previous_checkpoint_id is None
    assert second.previous_checkpoint_id == first.checkpoint_id
    assert second.previous_checkpoint_hash == first.checkpoint_hash
    assert second.expected_package_fingerprint == plan.steps[1].expected_package_fingerprint
    assert second.execution_status == "succeeded"
    assert second.domain_status == "blocked"
    assert second.formal_draft_allowed is False
    assert workflow_step_checkpoint_hash(second) == second.checkpoint_hash

    payload = second.model_dump(mode="python", by_alias=True)
    payload["output_hash"] = _sha("0")
    with pytest.raises(ValidationError, match="checkpoint hash"):
        WorkflowStepCheckpoint.model_validate(payload)


def test_checkpoint_requires_complete_success_and_explicit_failure() -> None:
    plan = _plan()
    first = _successful_checkpoint(plan, plan.steps[0])
    payload = first.model_dump(mode="python", by_alias=True)
    payload["skill_receipt_id"] = None
    payload["skill_receipt_hash"] = None
    with pytest.raises(ValidationError, match="requires"):
        WorkflowStepCheckpoint.model_validate(payload)

    payload = first.model_dump(mode="python", by_alias=True)
    payload["execution_status"] = "failed"
    payload["domain_status"] = None
    with pytest.raises(ValidationError, match="require an error"):
        WorkflowStepCheckpoint.model_validate(payload)

    failure = _failed_checkpoint(
        plan,
        plan.steps[0],
        attempt_ordinal=1,
        previous_checkpoint=None,
    )
    assert failure.execution_status == "failed"
    assert failure.error_code == "input_builder_failed"


def test_checkpoint_evidence_progression_has_no_orphan_hashes() -> None:
    plan = _plan(steps=(_step(1),))
    step = plan.steps[0]
    common = {
        "attempt_ordinal": 1,
        "previous_checkpoint": None,
        "skill_receipt_id": None,
        "skill_receipt_hash": None,
        "route_execution_receipt_id": None,
        "route_execution_receipt_hash": None,
        "execution_status": "failed",
        "domain_status": None,
        "error_code": "fixture_failed",
        "error_message": "Fixture failure.",
        "created_at": CREATED_AT,
    }

    with pytest.raises(ValidationError, match="pre-dispatch"):
        build_workflow_step_checkpoint(
            plan,
            step,
            tool_call_id=None,
            provider_tool_call_id=None,
            skill_call_id=None,
            input_hash=None,
            output_hash=None,
            tool_result_hash=_sha("1"),
            **common,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError, match="identities must pair"):
        build_workflow_step_checkpoint(
            plan,
            step,
            tool_call_id="tool-workflow-evidence",
            provider_tool_call_id=None,
            skill_call_id="skill-call-workflow-evidence",
            input_hash=_sha("2"),
            output_hash=None,
            tool_result_hash=None,
            **common,  # type: ignore[arg-type]
        )

    tool_failure = build_workflow_step_checkpoint(
        plan,
        step,
        tool_call_id="tool-workflow-evidence",
        provider_tool_call_id="provider-tool-workflow-evidence",
        skill_call_id="skill-call-workflow-evidence",
        input_hash=_sha("2"),
        output_hash=None,
        tool_result_hash=_sha("3"),
        **common,  # type: ignore[arg-type]
    )
    assert tool_failure.tool_result_hash == _sha("3")


def test_v1_has_one_attempt_and_only_step_one_omits_its_predecessor() -> None:
    plan = _plan()
    first = _successful_checkpoint(plan, plan.steps[0])

    with pytest.raises(ValidationError):
        _successful_checkpoint(
            plan,
            plan.steps[0],
            attempt_ordinal=2,
            previous_checkpoint=first,
        )

    with pytest.raises(ValidationError, match="only the first workflow step"):
        build_workflow_step_checkpoint(
            plan,
            plan.steps[1],
            attempt_ordinal=1,
            previous_checkpoint=None,
            tool_call_id=None,
            provider_tool_call_id=None,
            skill_call_id=None,
            skill_receipt_id=None,
            skill_receipt_hash=None,
            route_execution_receipt_id=None,
            route_execution_receipt_hash=None,
            input_hash=None,
            output_hash=None,
            tool_result_hash=None,
            execution_status="failed",
            domain_status=None,
            error_code="missing_predecessor",
            error_message="Missing predecessor.",
            created_at=CREATED_AT,
        )


def test_checkpoint_chain_rejects_forks_skips_retries_after_success_and_drift() -> None:
    plan, first, second = _completed_chain()

    fork_payload = second.model_dump(mode="python", by_alias=True)
    fork_payload["previous_checkpoint_hash"] = _sha("0")
    forked = _rehash_checkpoint(fork_payload)
    with pytest.raises(ValueError, match="predecessor chain is broken"):
        validate_workflow_checkpoint_chain(plan, (first, forked))

    drift_payload = second.model_dump(mode="python", by_alias=True)
    drift_payload["expected_package_fingerprint"] = _sha("0")
    drifted = _rehash_checkpoint(drift_payload)
    with pytest.raises(ValueError, match="immutable step"):
        validate_workflow_step_checkpoint(plan, drifted)


def test_checkpoint_chain_cannot_advance_from_disallowed_domain_status() -> None:
    first_step = _step(1, continue_on_domain_status=("healthy",))
    plan = _plan(steps=(first_step, _step(2)))
    first = _successful_checkpoint(plan, first_step, domain_status="blocked")
    second = _successful_checkpoint(
        plan,
        plan.steps[1],
        previous_checkpoint=first,
    )
    with pytest.raises(ValueError, match="cannot advance"):
        validate_workflow_checkpoint_chain(plan, (first, second))


def test_provider_boundary_is_pre_dispatch_only_and_hash_bound() -> None:
    plan, first, final = _completed_chain()
    boundary = build_provider_io_boundary(
        plan,
        (first, final),
        request_hash=_sha("1"),
        provider_model="gpt-fixture",
        provider_dispatch_config_fingerprint=_sha("a"),
        executor=_executor(),
        created_at=CREATED_AT + timedelta(minutes=1),
    )

    assert boundary.status == "dispatching"
    assert boundary.provider_model == "gpt-fixture"
    assert boundary.provider_dispatch_config_fingerprint == _sha("a")
    assert boundary.executor.instance_id == f"provider-config-{_sha('a')}"
    assert boundary.last_step_checkpoint_id == final.checkpoint_id
    assert boundary.formal_draft_allowed is False
    assert provider_io_boundary_hash(boundary) == boundary.boundary_hash

    payload = boundary.model_dump(mode="python", by_alias=True)
    payload["request_hash"] = _sha("2")
    with pytest.raises(ValidationError, match="boundary hash"):
        ProviderIoBoundary.model_validate(payload)

    payload = boundary.model_dump(mode="python", by_alias=True)
    payload["response_hash"] = _sha("3")
    with pytest.raises(ValidationError):
        ProviderIoBoundary.model_validate(payload)

    with pytest.raises(ValidationError, match="executor must bind"):
        build_provider_io_boundary(
            plan,
            (first, final),
            request_hash=_sha("1"),
            provider_model="gpt-fixture",
            provider_dispatch_config_fingerprint=_sha("a"),
            executor=_executor().model_copy(
                update={"instance_id": f"provider-config-{_sha('b')}"}
            ),
            created_at=CREATED_AT,
        )


def test_provider_model_is_optional_but_executor_identity_is_required() -> None:
    plan, first, final = _completed_chain()
    boundary = build_provider_io_boundary(
        plan,
        (first, final),
        request_hash=_sha("1"),
        provider_model=None,
        provider_dispatch_config_fingerprint=_sha("a"),
        executor=_executor(),
        created_at=CREATED_AT,
    )
    assert boundary.provider_model is None

    payload = boundary.model_dump(mode="python", by_alias=True)
    payload.pop("executor")
    with pytest.raises(ValidationError):
        ProviderIoBoundary.model_validate(payload)


def test_provider_boundary_requires_final_successful_permitted_checkpoint() -> None:
    plan = _plan()
    first = _successful_checkpoint(plan, plan.steps[0])
    with pytest.raises(ValueError, match="final workflow step"):
        build_provider_io_boundary(
            plan,
            (first,),
            request_hash=_sha("1"),
            provider_model=None,
            provider_dispatch_config_fingerprint=_sha("a"),
            executor=_executor(),
            created_at=CREATED_AT,
        )

    second = _successful_checkpoint(
        plan,
        plan.steps[1],
        previous_checkpoint=first,
    )
    fork_payload = second.model_dump(mode="python", by_alias=True)
    fork_payload["previous_checkpoint_id"] = "workflow-checkpoint-" + "0" * 32
    fork_payload["previous_checkpoint_hash"] = _sha("0")
    forked = _rehash_checkpoint(fork_payload)
    with pytest.raises(ValueError, match="predecessor chain is broken"):
        build_provider_io_boundary(
            plan,
            (first, forked),
            request_hash=_sha("1"),
            provider_model=None,
            provider_dispatch_config_fingerprint=_sha("a"),
            executor=_executor(),
            created_at=CREATED_AT,
        )

    failure = _failed_checkpoint(
        plan,
        plan.steps[1],
        attempt_ordinal=1,
        previous_checkpoint=first,
    )
    with pytest.raises(ValueError, match="successful workflow checkpoint"):
        build_provider_io_boundary(
            plan,
            (first, failure),
            request_hash=_sha("1"),
            provider_model=None,
            provider_dispatch_config_fingerprint=_sha("a"),
            executor=_executor(),
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _plan().model_dump(mode="json", by_alias=True),
        lambda: _completed_chain()[1].model_dump(mode="json", by_alias=True),
        lambda: _boundary(provider_model=None).model_dump(mode="json", by_alias=True),
    ],
)
def test_all_top_level_contracts_reject_formal_draft_authorization(factory) -> None:
    payload = factory()
    payload["formal_draft_allowed"] = True
    model = {
        "zhimeng.workflow-plan.v1": WorkflowPlan,
        "zhimeng.workflow-step-checkpoint.v1": WorkflowStepCheckpoint,
        "zhimeng.provider-io-boundary.v1": ProviderIoBoundary,
    }[payload["schema"]]
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("schema_name", "model", "factory"),
    [
        (
            "workflow-plan.schema.json",
            WorkflowPlan,
            lambda: _plan().model_dump(mode="json", by_alias=True),
        ),
        (
            "workflow-step-checkpoint.schema.json",
            WorkflowStepCheckpoint,
            lambda: _completed_chain()[2].model_dump(mode="json", by_alias=True),
        ),
        (
            "provider-io-boundary.schema.json",
            ProviderIoBoundary,
            lambda: _boundary(provider_model=None).model_dump(
                mode="json",
                by_alias=True,
            ),
        ),
    ],
)
def test_every_canonical_top_level_field_is_explicitly_required(
    schema_name: str,
    model: type,
    factory,
) -> None:
    payload = factory()
    schema = json.loads(render_schema(schema_name, model))
    assert set(schema["required"]) == set(schema["properties"])
    for field_name in tuple(payload):
        missing = dict(payload)
        missing.pop(field_name)
        _assert_rejected_by_model_and_schema(model, schema_name, missing)


def test_every_workflow_step_field_is_explicitly_required() -> None:
    payload = _plan().model_dump(mode="json", by_alias=True)
    schema = json.loads(render_schema("workflow-plan.schema.json", WorkflowPlan))
    step_schema = schema["$defs"]["WorkflowStep"]
    assert set(step_schema["required"]) == set(step_schema["properties"])
    for field_name in tuple(payload["steps"][0]):
        missing = json.loads(json.dumps(payload))
        missing["steps"][0].pop(field_name)
        _assert_rejected_by_model_and_schema(
            WorkflowPlan,
            "workflow-plan.schema.json",
            missing,
        )


def test_json_schema_and_pydantic_reject_linear_plan_violations() -> None:
    reversed_steps = _plan().model_dump(mode="json", by_alias=True)
    reversed_steps["steps"] = list(reversed(reversed_steps["steps"]))
    _assert_rejected_by_model_and_schema(
        WorkflowPlan,
        "workflow-plan.schema.json",
        reversed_steps,
    )

    invalid_contract = _plan().model_dump(mode="json", by_alias=True)
    invalid_contract["steps"][0]["input_contract"] = "not a contract URI"
    _assert_rejected_by_model_and_schema(
        WorkflowPlan,
        "workflow-plan.schema.json",
        invalid_contract,
    )

    status_order = _plan().model_dump(mode="json", by_alias=True)
    status_order["steps"][0]["continue_on_domain_status"] = [
        "blocked",
        "healthy",
    ]
    _assert_rejected_by_model_and_schema(
        WorkflowPlan,
        "workflow-plan.schema.json",
        status_order,
    )


def test_json_schema_and_pydantic_reject_checkpoint_evidence_violations() -> None:
    plan, _, final = _completed_chain()
    schema_name = "workflow-step-checkpoint.schema.json"

    missing_provider_call = final.model_dump(mode="json", by_alias=True)
    missing_provider_call["provider_tool_call_id"] = None
    _assert_rejected_by_model_and_schema(
        WorkflowStepCheckpoint,
        schema_name,
        missing_provider_call,
    )

    missing_output = final.model_dump(mode="json", by_alias=True)
    missing_output["output_hash"] = None
    _assert_rejected_by_model_and_schema(
        WorkflowStepCheckpoint,
        schema_name,
        missing_output,
    )

    invalid_failure = final.model_dump(mode="json", by_alias=True)
    invalid_failure["execution_status"] = "failed"
    _assert_rejected_by_model_and_schema(
        WorkflowStepCheckpoint,
        schema_name,
        invalid_failure,
    )

    pre_dispatch = _failed_checkpoint(
        plan,
        plan.steps[0],
        attempt_ordinal=1,
        previous_checkpoint=None,
    ).model_dump(mode="json", by_alias=True)
    pre_dispatch["tool_result_hash"] = _sha("1")
    _assert_rejected_by_model_and_schema(
        WorkflowStepCheckpoint,
        schema_name,
        pre_dispatch,
    )


def test_exported_contracts_round_trip_and_have_no_schema_drift() -> None:
    plan, first, final = _completed_chain()
    boundary = build_provider_io_boundary(
        plan,
        (first, final),
        request_hash=_sha("1"),
        provider_model="gpt-fixture",
        provider_dispatch_config_fingerprint=_sha("a"),
        executor=_executor(),
        created_at=CREATED_AT,
    )
    examples = {
        "workflow-plan.schema.json": plan.model_dump(mode="json", by_alias=True),
        "workflow-step-checkpoint.schema.json": final.model_dump(
            mode="json",
            by_alias=True,
        ),
        "provider-io-boundary.schema.json": boundary.model_dump(
            mode="json",
            by_alias=True,
        ),
    }

    assert set(MODELS) == set(examples)
    for file_name, model in MODELS.items():
        rendered = render_schema(file_name, model)
        target = CONTRACTS / file_name
        assert target.read_text(encoding="utf-8") == rendered
        schema = json.loads(rendered)
        Draft202012Validator.check_schema(schema)
        assert list(Draft202012Validator(schema).iter_errors(examples[file_name])) == []
        assert (
            model.model_validate_json(json.dumps(examples[file_name])).model_dump(
                mode="json",
                by_alias=True,
            )
            == examples[file_name]
        )
        assert schema["additionalProperties"] is False

    plan_schema = json.loads(
        (CONTRACTS / "workflow-plan.schema.json").read_text(encoding="utf-8")
    )
    assert plan_schema["$defs"]["WorkflowStep"]["additionalProperties"] is False
    assert plan_schema["$defs"]["WorkflowStep"]["properties"]["runner"]["const"] == (
        "managed_skill"
    )

    boundary_schema = json.loads(
        (CONTRACTS / "provider-io-boundary.schema.json").read_text(encoding="utf-8")
    )
    assert set(boundary_schema["properties"]) == {
        "schema",
        "boundary_id",
        "workflow_plan_id",
        "plan_hash",
        "run_id",
            "request_hash",
            "provider_model",
            "provider_dispatch_config_fingerprint",
            "executor",
        "last_step_checkpoint_id",
        "last_step_checkpoint_hash",
        "status",
        "formal_draft_allowed",
        "created_at",
        "boundary_hash",
    }
    assert boundary_schema["properties"]["status"]["const"] == "dispatching"
    assert "response_hash" not in rendered


def test_hash_bearing_models_require_timezone_aware_created_at() -> None:
    plan = _plan()
    payload = plan.model_dump(mode="python", by_alias=True)
    payload["created_at"] = datetime(2026, 7, 21, 12, 30)
    with pytest.raises(ValidationError, match="timezone-aware"):
        WorkflowPlan.model_validate(payload)
