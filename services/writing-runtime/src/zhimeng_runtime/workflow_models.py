from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.execution_protocol import ExecutorIdentity


DomainStatus = Literal["healthy", "degraded", "blocked"]
ExecutionStatus = Literal["succeeded", "failed", "cancelled"]


class WorkflowModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class WorkflowStep(WorkflowModel):
    step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    ordinal: int = Field(ge=1, le=32, strict=True)
    runner: Literal["managed_skill"]
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    expected_skill_version_id: str = Field(
        pattern=r"^skill-version-[0-9a-f]{32}$"
    )
    expected_skill_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    expected_manifest_fingerprint: Sha256
    expected_package_fingerprint: Sha256
    input_contract: str = Field(
        max_length=2048,
        pattern=(
            r"^https://zhimeng\.local/contracts/"
            r"[A-Za-z0-9._/-]+\.schema\.json$"
        ),
    )
    output_contract: str = Field(
        max_length=2048,
        pattern=(
            r"^https://zhimeng\.local/contracts/"
            r"[A-Za-z0-9._/-]+\.schema\.json$"
        ),
    )
    input_builder_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    input_builder_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    max_attempts: Literal[1]
    continue_on_domain_status: tuple[DomainStatus, ...] = Field(
        min_length=1,
        max_length=3,
    )
    formal_draft_allowed: Literal[False]
    step_hash: Sha256

    @field_validator("input_contract", "output_contract")
    @classmethod
    def validate_contract_uri(cls, value: str) -> str:
        suffix = value.removeprefix("https://zhimeng.local/contracts/")
        path = PurePosixPath(suffix)
        if (
            "\\" in suffix
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("workflow contract URIs must remain inside the contract root")
        return value

    @model_validator(mode="after")
    def validate_step(self) -> "WorkflowStep":
        status_order = {"healthy": 0, "degraded": 1, "blocked": 2}
        if len(self.continue_on_domain_status) != len(
            set(self.continue_on_domain_status)
        ):
            raise ValueError("workflow continuation domain statuses must be unique")
        if tuple(
            sorted(self.continue_on_domain_status, key=status_order.__getitem__)
        ) != self.continue_on_domain_status:
            raise ValueError(
                "workflow continuation domain statuses must use canonical order"
            )
        if self.step_hash != workflow_step_hash(self):
            raise ValueError("workflow step hash does not match immutable material")
        return self


class WorkflowPlan(WorkflowModel):
    schema_id: Literal["zhimeng.workflow-plan.v1"] = Field(
        alias="schema",
    )
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    run_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    project_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    context_pack_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    context_pack_hash: Sha256
    knowledge_fingerprint: Sha256 | None
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: Sha256
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=32)
    formal_draft_allowed: Literal[False]
    created_at: datetime
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_plan(self) -> "WorkflowPlan":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("workflow plan created_at must be timezone-aware")
        ordinals = tuple(step.ordinal for step in self.steps)
        if ordinals != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("workflow steps must use contiguous ordered ordinals")
        step_ids = tuple(step.step_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow step IDs must be unique")
        if self.plan_hash != workflow_plan_hash(self):
            raise ValueError("workflow plan hash does not match immutable material")
        return self


class WorkflowStepCheckpoint(WorkflowModel):
    schema_id: Literal["zhimeng.workflow-step-checkpoint.v1"] = Field(
        alias="schema",
    )
    checkpoint_id: str = Field(pattern=r"^workflow-checkpoint-[0-9a-f]{32}$")
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    plan_hash: Sha256
    run_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    step_id: str = Field(pattern=r"^workflow-step-[0-9a-f]{32}$")
    step_hash: Sha256
    step_ordinal: int = Field(ge=1, le=32, strict=True)
    runner: Literal["managed_skill"]
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    expected_skill_version_id: str = Field(
        pattern=r"^skill-version-[0-9a-f]{32}$"
    )
    expected_skill_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    expected_manifest_fingerprint: Sha256
    expected_package_fingerprint: Sha256
    attempt_ordinal: Literal[1]
    previous_checkpoint_id: str | None = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$",
    )
    previous_checkpoint_hash: Sha256 | None
    tool_call_id: str | None = Field(min_length=1, max_length=256)
    provider_tool_call_id: str | None = Field(min_length=1, max_length=256)
    skill_call_id: str | None = Field(min_length=1, max_length=256)
    skill_receipt_id: str | None = Field(
        pattern=r"^skill-receipt-[0-9a-f]{32}$",
    )
    skill_receipt_hash: Sha256 | None
    route_execution_receipt_id: str | None = Field(
        pattern=r"^nlcl-route-exec-[0-9a-f]{32}$",
    )
    route_execution_receipt_hash: Sha256 | None
    input_hash: Sha256 | None
    output_hash: Sha256 | None
    tool_result_hash: Sha256 | None
    execution_status: ExecutionStatus
    domain_status: DomainStatus | None
    error_code: str | None = Field(
        min_length=2,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    error_message: str | None = Field(min_length=1, max_length=4096)
    formal_draft_allowed: Literal[False]
    created_at: datetime
    checkpoint_hash: Sha256

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "WorkflowStepCheckpoint":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("workflow checkpoint created_at must be timezone-aware")
        if (self.previous_checkpoint_id is None) != (
            self.previous_checkpoint_hash is None
        ):
            raise ValueError("previous workflow checkpoint identity and hash must pair")
        if (self.step_ordinal == 1) != (self.previous_checkpoint_id is None):
            raise ValueError(
                "only the first workflow step may omit the previous checkpoint"
            )
        call_ids = (
            self.tool_call_id,
            self.provider_tool_call_id,
            self.skill_call_id,
        )
        if any(value is None for value in call_ids) and any(
            value is not None for value in call_ids
        ):
            raise ValueError(
                "workflow ToolCall, Provider ToolCall and SkillCall identities must pair"
            )
        if (self.skill_receipt_id is None) != (self.skill_receipt_hash is None):
            raise ValueError("workflow Skill receipt identity and hash must pair")
        if (self.route_execution_receipt_id is None) != (
            self.route_execution_receipt_hash is None
        ):
            raise ValueError("workflow route receipt identity and hash must pair")
        if self.route_execution_receipt_id is not None and self.skill_receipt_id is None:
            raise ValueError("workflow route receipt requires a Skill receipt")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("workflow checkpoint error code and message must pair")

        if self.tool_call_id is None:
            forbidden_without_call = (
                self.skill_receipt_id,
                self.skill_receipt_hash,
                self.route_execution_receipt_id,
                self.route_execution_receipt_hash,
                self.output_hash,
                self.tool_result_hash,
            )
            if any(value is not None for value in forbidden_without_call):
                raise ValueError(
                    "pre-dispatch workflow checkpoints cannot carry execution evidence"
                )
        elif self.input_hash is None:
            raise ValueError("workflow ToolCalls require an input hash")

        if self.skill_receipt_id is None:
            if self.output_hash is not None:
                raise ValueError("workflow output hashes require a Skill receipt")
            if self.route_execution_receipt_id is not None:
                raise ValueError("workflow route receipts require a Skill receipt")
        elif self.output_hash is None:
            raise ValueError("workflow Skill receipts require an output hash")

        if self.tool_result_hash is not None and self.tool_call_id is None:
            raise ValueError("workflow ToolResult hashes require a ToolCall")
        if (
            self.route_execution_receipt_id is not None
            and self.tool_result_hash is None
        ):
            raise ValueError("workflow route receipts require a ToolResult hash")

        if self.execution_status == "succeeded":
            required = (
                self.tool_call_id,
                self.provider_tool_call_id,
                self.skill_call_id,
                self.skill_receipt_id,
                self.skill_receipt_hash,
                self.route_execution_receipt_id,
                self.route_execution_receipt_hash,
                self.input_hash,
                self.output_hash,
                self.tool_result_hash,
                self.domain_status,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "successful workflow checkpoints require complete execution receipts"
                )
            if self.error_code is not None:
                raise ValueError("successful workflow checkpoints cannot carry an error")
        else:
            if self.domain_status is not None:
                raise ValueError(
                    "failed or cancelled workflow checkpoints cannot claim domain status"
                )
            if self.error_code is None:
                raise ValueError(
                    "failed or cancelled workflow checkpoints require an error"
                )
        if self.checkpoint_hash != workflow_step_checkpoint_hash(self):
            raise ValueError("workflow checkpoint hash does not match immutable material")
        return self


class ProviderIoBoundary(WorkflowModel):
    schema_id: Literal["zhimeng.provider-io-boundary.v1"] = Field(
        alias="schema",
    )
    boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    plan_hash: Sha256
    run_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    request_hash: Sha256
    provider_model: str | None = Field(min_length=1, max_length=512)
    provider_dispatch_config_fingerprint: Sha256
    executor: ExecutorIdentity
    last_step_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    last_step_checkpoint_hash: Sha256
    status: Literal["dispatching"]
    formal_draft_allowed: Literal[False]
    created_at: datetime
    boundary_hash: Sha256

    @field_validator("provider_model")
    @classmethod
    def normalize_provider_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider model must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_boundary(self) -> "ProviderIoBoundary":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("provider I/O boundary created_at must be timezone-aware")
        if self.executor.instance_id != (
            f"provider-config-{self.provider_dispatch_config_fingerprint}"
        ):
            raise ValueError(
                "provider I/O boundary executor must bind the dispatch configuration"
            )
        if self.boundary_hash != provider_io_boundary_hash(self):
            raise ValueError("provider I/O boundary hash does not match immutable material")
        return self


def workflow_step_hash(step: WorkflowStep) -> str:
    return canonical_json_hash(
        step.model_dump(mode="json", by_alias=True, exclude={"step_hash"})
    )


def workflow_plan_hash(plan: WorkflowPlan) -> str:
    return canonical_json_hash(
        plan.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
    )


def workflow_step_checkpoint_hash(checkpoint: WorkflowStepCheckpoint) -> str:
    return canonical_json_hash(
        checkpoint.model_dump(
            mode="json",
            by_alias=True,
            exclude={"checkpoint_hash"},
        )
    )


def provider_io_boundary_hash(boundary: ProviderIoBoundary) -> str:
    return canonical_json_hash(
        boundary.model_dump(mode="json", by_alias=True, exclude={"boundary_hash"})
    )


def _derived_id(prefix: str, payload: object) -> str:
    digest = canonical_json_hash(payload)
    return f"{prefix}-{digest[:32]}"


def build_workflow_step(
    *,
    ordinal: int,
    skill_id: str,
    expected_skill_version_id: str,
    expected_skill_version: str,
    expected_manifest_fingerprint: str,
    expected_package_fingerprint: str,
    input_contract: str,
    output_contract: str,
    input_builder_id: str,
    input_builder_version: str,
    max_attempts: Literal[1] = 1,
    continue_on_domain_status: tuple[DomainStatus, ...] = (
        "healthy",
        "degraded",
        "blocked",
    ),
) -> WorkflowStep:
    seed = {
        "schema": "zhimeng.workflow-step.v1",
        "ordinal": ordinal,
        "runner": "managed_skill",
        "skill_id": skill_id,
        "expected_skill_version_id": expected_skill_version_id,
        "expected_skill_version": expected_skill_version,
        "expected_manifest_fingerprint": expected_manifest_fingerprint,
        "expected_package_fingerprint": expected_package_fingerprint,
        "input_contract": input_contract,
        "output_contract": output_contract,
        "input_builder_id": input_builder_id,
        "input_builder_version": input_builder_version,
        "max_attempts": max_attempts,
        "continue_on_domain_status": continue_on_domain_status,
        "formal_draft_allowed": False,
    }
    material = {
        "step_id": _derived_id("workflow-step", seed),
        **{key: value for key, value in seed.items() if key != "schema"},
    }
    draft = WorkflowStep.model_construct(**material, step_hash="0" * 64)
    return WorkflowStep(**material, step_hash=workflow_step_hash(draft))


def build_workflow_plan(
    *,
    run_id: str,
    project_id: str,
    project_revision: int,
    project_fingerprint: str,
    context_pack_id: str,
    context_pack_hash: str,
    knowledge_fingerprint: str | None,
    route_receipt_id: str,
    route_receipt_hash: str,
    operation_package_id: str,
    operation_package_hash: str,
    steps: tuple[WorkflowStep, ...],
    created_at: datetime,
) -> WorkflowPlan:
    identity = {
        "schema": "zhimeng.workflow-plan.v1",
        "run_id": run_id,
        "project_id": project_id,
        "project_revision": project_revision,
        "project_fingerprint": project_fingerprint,
        "context_pack_id": context_pack_id,
        "context_pack_hash": context_pack_hash,
        "knowledge_fingerprint": knowledge_fingerprint,
        "route_receipt_id": route_receipt_id,
        "route_receipt_hash": route_receipt_hash,
        "operation_package_id": operation_package_id,
        "operation_package_hash": operation_package_hash,
        "step_hashes": [step.step_hash for step in steps],
        "created_at": created_at.isoformat(),
    }
    material = {
        "schema_id": "zhimeng.workflow-plan.v1",
        "workflow_plan_id": _derived_id("workflow-plan", identity),
        "run_id": run_id,
        "project_id": project_id,
        "project_revision": project_revision,
        "project_fingerprint": project_fingerprint,
        "context_pack_id": context_pack_id,
        "context_pack_hash": context_pack_hash,
        "knowledge_fingerprint": knowledge_fingerprint,
        "route_receipt_id": route_receipt_id,
        "route_receipt_hash": route_receipt_hash,
        "operation_package_id": operation_package_id,
        "operation_package_hash": operation_package_hash,
        "steps": steps,
        "formal_draft_allowed": False,
        "created_at": created_at,
    }
    draft = WorkflowPlan.model_construct(**material, plan_hash="0" * 64)
    return WorkflowPlan(**material, plan_hash=workflow_plan_hash(draft))


def build_workflow_step_checkpoint(
    plan: WorkflowPlan,
    step: WorkflowStep,
    *,
    attempt_ordinal: Literal[1],
    previous_checkpoint: WorkflowStepCheckpoint | None,
    tool_call_id: str | None,
    provider_tool_call_id: str | None,
    skill_call_id: str | None,
    skill_receipt_id: str | None,
    skill_receipt_hash: str | None,
    route_execution_receipt_id: str | None,
    route_execution_receipt_hash: str | None,
    input_hash: str | None,
    output_hash: str | None,
    tool_result_hash: str | None,
    execution_status: ExecutionStatus,
    domain_status: DomainStatus | None,
    error_code: str | None,
    error_message: str | None,
    created_at: datetime,
) -> WorkflowStepCheckpoint:
    if step not in plan.steps:
        raise ValueError("workflow checkpoint step is not part of the supplied plan")
    previous_id = None if previous_checkpoint is None else previous_checkpoint.checkpoint_id
    previous_hash = (
        None if previous_checkpoint is None else previous_checkpoint.checkpoint_hash
    )
    identity = {
        "schema": "zhimeng.workflow-step-checkpoint.v1",
        "workflow_plan_id": plan.workflow_plan_id,
        "plan_hash": plan.plan_hash,
        "step_id": step.step_id,
        "step_hash": step.step_hash,
        "attempt_ordinal": attempt_ordinal,
        "previous_checkpoint_hash": previous_hash,
    }
    material = {
        "schema_id": "zhimeng.workflow-step-checkpoint.v1",
        "checkpoint_id": _derived_id("workflow-checkpoint", identity),
        "workflow_plan_id": plan.workflow_plan_id,
        "plan_hash": plan.plan_hash,
        "run_id": plan.run_id,
        "step_id": step.step_id,
        "step_hash": step.step_hash,
        "step_ordinal": step.ordinal,
        "runner": step.runner,
        "skill_id": step.skill_id,
        "expected_skill_version_id": step.expected_skill_version_id,
        "expected_skill_version": step.expected_skill_version,
        "expected_manifest_fingerprint": step.expected_manifest_fingerprint,
        "expected_package_fingerprint": step.expected_package_fingerprint,
        "attempt_ordinal": attempt_ordinal,
        "previous_checkpoint_id": previous_id,
        "previous_checkpoint_hash": previous_hash,
        "tool_call_id": tool_call_id,
        "provider_tool_call_id": provider_tool_call_id,
        "skill_call_id": skill_call_id,
        "skill_receipt_id": skill_receipt_id,
        "skill_receipt_hash": skill_receipt_hash,
        "route_execution_receipt_id": route_execution_receipt_id,
        "route_execution_receipt_hash": route_execution_receipt_hash,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "tool_result_hash": tool_result_hash,
        "execution_status": execution_status,
        "domain_status": domain_status,
        "error_code": error_code,
        "error_message": error_message,
        "formal_draft_allowed": False,
        "created_at": created_at,
    }
    draft = WorkflowStepCheckpoint.model_construct(
        **material,
        checkpoint_hash="0" * 64,
    )
    checkpoint = WorkflowStepCheckpoint(
        **material,
        checkpoint_hash=workflow_step_checkpoint_hash(draft),
    )
    validate_workflow_step_checkpoint(plan, checkpoint)
    return checkpoint


def build_provider_io_boundary(
    plan: WorkflowPlan,
    checkpoints: tuple[WorkflowStepCheckpoint, ...],
    *,
    request_hash: str,
    provider_model: str | None,
    provider_dispatch_config_fingerprint: str,
    executor: ExecutorIdentity,
    created_at: datetime,
) -> ProviderIoBoundary:
    validate_workflow_checkpoint_chain(plan, checkpoints)
    if not checkpoints:
        raise ValueError("provider I/O requires a completed workflow checkpoint chain")
    last_checkpoint = checkpoints[-1]
    if last_checkpoint.step_ordinal != len(plan.steps):
        raise ValueError("provider I/O requires the final workflow step checkpoint")
    if last_checkpoint.execution_status != "succeeded":
        raise ValueError("provider I/O requires a successful workflow checkpoint")
    final_step = plan.steps[-1]
    if last_checkpoint.domain_status not in final_step.continue_on_domain_status:
        raise ValueError("workflow domain status does not permit Provider dispatch")
    normalized_provider_model = (
        None if provider_model is None else provider_model.strip()
    )
    if normalized_provider_model == "":
        raise ValueError("provider model must not be blank")
    identity = {
        "schema": "zhimeng.provider-io-boundary.v1",
        "workflow_plan_id": plan.workflow_plan_id,
        "plan_hash": plan.plan_hash,
        "run_id": plan.run_id,
        "request_hash": request_hash,
        "provider_model": normalized_provider_model,
        "provider_dispatch_config_fingerprint": provider_dispatch_config_fingerprint,
        "executor": executor.model_dump(mode="json"),
        "last_step_checkpoint_id": last_checkpoint.checkpoint_id,
        "last_step_checkpoint_hash": last_checkpoint.checkpoint_hash,
    }
    material = {
        "schema_id": "zhimeng.provider-io-boundary.v1",
        "boundary_id": _derived_id("provider-io", identity),
        "workflow_plan_id": plan.workflow_plan_id,
        "plan_hash": plan.plan_hash,
        "run_id": plan.run_id,
        "request_hash": request_hash,
        "provider_model": normalized_provider_model,
        "provider_dispatch_config_fingerprint": provider_dispatch_config_fingerprint,
        "executor": executor,
        "last_step_checkpoint_id": last_checkpoint.checkpoint_id,
        "last_step_checkpoint_hash": last_checkpoint.checkpoint_hash,
        "status": "dispatching",
        "formal_draft_allowed": False,
        "created_at": created_at,
    }
    draft = ProviderIoBoundary.model_construct(
        **material,
        boundary_hash="0" * 64,
    )
    return ProviderIoBoundary(
        **material,
        boundary_hash=provider_io_boundary_hash(draft),
    )


def validate_workflow_step_checkpoint(
    plan: WorkflowPlan,
    checkpoint: WorkflowStepCheckpoint,
) -> None:
    if workflow_plan_hash(plan) != plan.plan_hash:
        raise ValueError("workflow plan hash does not match immutable material")
    if workflow_step_checkpoint_hash(checkpoint) != checkpoint.checkpoint_hash:
        raise ValueError("workflow checkpoint hash does not match immutable material")
    if (
        checkpoint.workflow_plan_id != plan.workflow_plan_id
        or checkpoint.plan_hash != plan.plan_hash
        or checkpoint.run_id != plan.run_id
    ):
        raise ValueError("workflow checkpoint does not bind the supplied plan")
    if checkpoint.step_ordinal > len(plan.steps):
        raise ValueError("workflow checkpoint references an unknown step ordinal")
    step = plan.steps[checkpoint.step_ordinal - 1]
    if workflow_step_hash(step) != step.step_hash:
        raise ValueError("workflow step hash does not match immutable material")
    expected = (
        checkpoint.step_id == step.step_id
        and checkpoint.step_hash == step.step_hash
        and checkpoint.runner == step.runner
        and checkpoint.skill_id == step.skill_id
        and checkpoint.expected_skill_version_id == step.expected_skill_version_id
        and checkpoint.expected_skill_version == step.expected_skill_version
        and checkpoint.expected_manifest_fingerprint
        == step.expected_manifest_fingerprint
        and checkpoint.expected_package_fingerprint == step.expected_package_fingerprint
    )
    if not expected:
        raise ValueError("workflow checkpoint does not bind its immutable step")
    if checkpoint.attempt_ordinal != 1 or step.max_attempts != 1:
        raise ValueError("workflow v1 allows exactly one attempt per step")


def validate_workflow_checkpoint_chain(
    plan: WorkflowPlan,
    checkpoints: tuple[WorkflowStepCheckpoint, ...],
) -> None:
    if not checkpoints:
        return
    seen_ids: set[str] = set()
    previous: WorkflowStepCheckpoint | None = None
    for checkpoint in checkpoints:
        validate_workflow_step_checkpoint(plan, checkpoint)
        if checkpoint.checkpoint_id in seen_ids:
            raise ValueError("workflow checkpoint IDs must be unique")
        seen_ids.add(checkpoint.checkpoint_id)
        if previous is None:
            if checkpoint.step_ordinal != 1 or checkpoint.attempt_ordinal != 1:
                raise ValueError("workflow checkpoint chain must start at step 1 attempt 1")
            if checkpoint.previous_checkpoint_id is not None:
                raise ValueError("first workflow checkpoint cannot reference a predecessor")
        else:
            if (
                checkpoint.previous_checkpoint_id != previous.checkpoint_id
                or checkpoint.previous_checkpoint_hash != previous.checkpoint_hash
            ):
                raise ValueError("workflow checkpoint predecessor chain is broken")
            if checkpoint.step_ordinal == previous.step_ordinal:
                raise ValueError("workflow v1 does not support step retries")
            if checkpoint.step_ordinal == previous.step_ordinal + 1:
                previous_step = plan.steps[previous.step_ordinal - 1]
                if (
                    previous.execution_status != "succeeded"
                    or previous.domain_status
                    not in previous_step.continue_on_domain_status
                ):
                    raise ValueError(
                        "workflow cannot advance from the previous checkpoint outcome"
                    )
            else:
                raise ValueError("workflow checkpoint steps must remain sequential")
        previous = checkpoint


__all__ = [
    "DomainStatus",
    "ExecutionStatus",
    "ProviderIoBoundary",
    "WorkflowPlan",
    "WorkflowStep",
    "WorkflowStepCheckpoint",
    "build_provider_io_boundary",
    "build_workflow_plan",
    "build_workflow_step",
    "build_workflow_step_checkpoint",
    "provider_io_boundary_hash",
    "validate_workflow_checkpoint_chain",
    "validate_workflow_step_checkpoint",
    "workflow_plan_hash",
    "workflow_step_checkpoint_hash",
    "workflow_step_hash",
]
