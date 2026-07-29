from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from zhimeng_runtime.agent_provider import AgentProviderCancelled, AgentProviderError
from zhimeng_runtime.agent_store import (
    AgentStore,
    AgentStoreError,
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
    StoredToolCall,
)
from zhimeng_runtime.agent_tools import (
    CREATION_HANDOFF_SKILL_ID,
    CREATION_PREWRITE_GATE_SKILL_ID,
    CREATION_PROJECT_AUDIT_SKILL_ID,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreError,
    ContextStoreIntegrity,
)
from zhimeng_runtime.execution_protocol import Run
from zhimeng_runtime.managed_skill_service import ManagedSkillError, ManagedSkillVersion
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_workflow_plan,
    build_workflow_step,
)


PROJECT_AUDIT_INPUT_CONTRACT = "zhimeng.creation-project-audit-request.v1"
PREWRITE_GATE_INPUT_CONTRACT = "zhimeng.creation-prewrite-gate-request.v1"
CREATION_HANDOFF_INPUT_CONTRACT = "zhimeng.creation-handoff-request.v1"
PROJECT_AUDIT_INPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-project-audit-request.schema.json"
)
PROJECT_AUDIT_OUTPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-project-audit-result.schema.json"
)
PREWRITE_GATE_INPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-prewrite-gate-request.schema.json"
)
PREWRITE_GATE_OUTPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-prewrite-gate-result.schema.json"
)
CREATION_HANDOFF_INPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-handoff-request.schema.json"
)
CREATION_HANDOFF_OUTPUT_SCHEMA = (
    "https://zhimeng.local/contracts/creation-handoff-result.schema.json"
)
INPUT_BUILDER_VERSION = "1"


class ToolExecutionResultLike(Protocol):
    tool_call: StoredToolCall
    replayed: bool


ExecuteToolCall = Callable[..., Awaitable[ToolExecutionResultLike]]


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    plan: WorkflowPlan
    checkpoints: tuple[WorkflowStepCheckpoint, ...]
    provider_messages: tuple[dict[str, object], ...]
    tool_call_count: int


@dataclass(frozen=True, slots=True)
class WorkflowStartupRecovery:
    plan: WorkflowPlan | None
    checkpoints: tuple[WorkflowStepCheckpoint, ...]
    reconciled_count: int


def workflow_provider_tool_call_id(route_receipt_id: str, step_ordinal: int) -> str:
    """Preserve the M3d/M3e synthetic Provider identity for one route step."""

    digest = hashlib.sha256(
        f"{route_receipt_id}\0{step_ordinal}".encode("utf-8")
    ).hexdigest()
    return f"nlcl_call_{digest[:24]}"


def workflow_runtime_tool_call_id(run_id: str, provider_tool_call_id: str) -> str:
    """Scope a synthetic Provider identity to one persisted Agent Run."""

    digest = hashlib.sha256(
        f"{run_id}\0{provider_tool_call_id}".encode("utf-8")
    ).hexdigest()
    return f"tool_{digest[:32]}"


class WorkflowRunner:
    """Durable pre-Provider execution for the managed Creation workflow."""

    def __init__(
        self,
        store: AgentStore,
        context_store: ContextStore,
        skill_executor: SkillExecutor,
    ) -> None:
        self.store = store
        self.context_store = context_store
        self.skill_executor = skill_executor

    async def execute_pre_provider(
        self,
        run: Run,
        *,
        cancel_event: asyncio.Event,
        execute_tool_call: ExecuteToolCall,
    ) -> WorkflowExecutionResult:
        plan = await asyncio.to_thread(self._compile_and_persist_plan, run)
        await asyncio.to_thread(self._reconcile_terminal_steps, plan)
        chain = await asyncio.to_thread(
            self.store.list_workflow_checkpoints,
            run.run_id,
        )
        if any(isinstance(item, ProviderIoBoundary) for item in chain):
            raise AgentProviderError(
                "Provider I/O already began for this workflow",
                code="workflow_provider_boundary_exists",
                retryable=False,
            )
        checkpoints = self._step_checkpoints(chain)
        messages: list[dict[str, object]] = []
        audit_checkpoint: WorkflowStepCheckpoint | None = None
        audit_call: StoredToolCall | None = None
        prewrite_checkpoint: WorkflowStepCheckpoint | None = None
        prewrite_call: StoredToolCall | None = None

        for step in plan.steps:
            existing = self._checkpoint_for_step(checkpoints, step.ordinal)
            if existing is not None:
                stored_call = await asyncio.to_thread(
                    self._validated_stored_call,
                    plan,
                    existing,
                )
                self._raise_for_terminal_call(stored_call)
                messages.extend(self._provider_messages(existing, stored_call))
                if step.ordinal == 1:
                    audit_checkpoint = existing
                    audit_call = stored_call
                elif step.ordinal == 2:
                    prewrite_checkpoint = existing
                    prewrite_call = stored_call
                continue

            arguments = self._step_arguments(
                plan,
                step.ordinal,
                audit_checkpoint=audit_checkpoint,
                audit_call=audit_call,
                prewrite_checkpoint=prewrite_checkpoint,
                prewrite_call=prewrite_call,
            )
            provider_tool_call_id = workflow_provider_tool_call_id(
                plan.route_receipt_id,
                step.ordinal,
            )
            previous = checkpoints[-1] if checkpoints else None
            try:
                executed = await execute_tool_call(
                    run,
                    provider_tool_call_id=provider_tool_call_id,
                    tool_name="skill_execute",
                    arguments=arguments,
                    cancel_event=cancel_event,
                )
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(
                        asyncio.to_thread(
                            self._reconcile_one_step,
                            plan,
                            step.ordinal,
                            provider_tool_call_id,
                            previous,
                        )
                    )
                except (AgentStoreError, ContextStoreError, AgentProviderError):
                    # Startup reconciliation owns any terminal ToolCall whose
                    # receipt could not be coordinated during cancellation.
                    pass
                raise

            checkpoint = await self._reconcile_with_retry(
                plan,
                step.ordinal,
                provider_tool_call_id,
                previous,
            )
            checkpoints = (*checkpoints, checkpoint)
            stored_call = executed.tool_call
            if checkpoint.tool_call_id != stored_call.tool_call_id:
                raise AgentProviderError(
                    "Workflow checkpoint does not match the executed ToolCall",
                    code="workflow_checkpoint_tool_mismatch",
                    retryable=False,
                )
            self._raise_for_terminal_call(stored_call)
            messages.extend(self._provider_messages(checkpoint, stored_call))
            if step.ordinal == 1:
                audit_checkpoint = checkpoint
                audit_call = stored_call
            elif step.ordinal == 2:
                prewrite_checkpoint = checkpoint
                prewrite_call = stored_call

        return WorkflowExecutionResult(
            plan=plan,
            checkpoints=checkpoints,
            provider_messages=tuple(messages),
            tool_call_count=len(checkpoints),
        )

    def reconcile_startup(self, run_id: str) -> WorkflowStartupRecovery:
        """Repair terminal pre-Provider evidence without executing a Skill or Provider."""

        try:
            plan = self.store.get_workflow_plan(run_id)
        except AgentStoreNotFound:
            return WorkflowStartupRecovery(plan=None, checkpoints=(), reconciled_count=0)
        before = self._step_checkpoints(self.store.list_workflow_checkpoints(run_id))
        after = self._reconcile_terminal_steps(plan)
        return WorkflowStartupRecovery(
            plan=plan,
            checkpoints=after,
            reconciled_count=len(after) - len(before),
        )

    def _compile_and_persist_plan(self, run: Run) -> WorkflowPlan:
        try:
            existing = self.store.get_workflow_plan(run.run_id)
        except AgentStoreNotFound:
            existing = None
        except AgentWorkflowCheckpointConflict as exc:
            raise AgentProviderError(
                str(exc),
                code=exc.code,
                retryable=False,
            ) from exc
        if existing is not None:
            self.validate_plan_dependencies(existing)
            return existing

        plan = self.build_plan(run)
        try:
            return self.store.create_workflow_plan(plan)
        except AgentWorkflowCheckpointConflict as exc:
            raise AgentProviderError(
                str(exc),
                code=exc.code,
                retryable=False,
            ) from exc

    def build_plan(
        self,
        run: Run,
        *,
        context_pack_id: str | None = None,
    ) -> WorkflowPlan:
        """Compile a Plan that can be committed atomically with a new Run."""

        try:
            stored_pack = (
                self.context_store.get_run_pack(run.run_id)
                if context_pack_id is None
                else self.context_store.get_pack(context_pack_id)
            )
            pack = stored_pack.pack
            route = self.context_store.get_nlcl_route(pack.context_pack_id)
            operation_package = self.context_store.get_prewrite_operation_package(
                pack.context_pack_id
            )
        except ContextStoreIntegrity as exc:
            raise AgentProviderError(
                str(exc),
                code="nlcl_route_integrity_failed",
                retryable=False,
            ) from exc
        except ContextStoreError as exc:
            raise AgentProviderError(
                str(exc),
                code="nlcl_route_unavailable",
                retryable=True,
            ) from exc

        expected_calls = (
            (1, CREATION_PROJECT_AUDIT_SKILL_ID, PROJECT_AUDIT_INPUT_CONTRACT),
            (2, CREATION_PREWRITE_GATE_SKILL_ID, PREWRITE_GATE_INPUT_CONTRACT),
            (3, CREATION_HANDOFF_SKILL_ID, CREATION_HANDOFF_INPUT_CONTRACT),
        )
        actual_calls = tuple(
            (call.ordinal, call.skill_id, call.input_contract)
            for call in route.chapter_call_list
        )
        if actual_calls != expected_calls:
            raise AgentProviderError(
                "NLCL route requires recompilation for the managed prewrite workflow",
                code="nlcl_prewrite_route_recompile_required",
                retryable=False,
            )
        if (
            "migrated_prewrite_package_requires_recompile"
            in operation_package.missing_requirements
        ):
            raise AgentProviderError(
                "migrated prewrite operation package must be recompiled",
                code="nlcl_prewrite_package_recompile_required",
                retryable=False,
            )

        contract_pairs = (
            (PROJECT_AUDIT_INPUT_SCHEMA, PROJECT_AUDIT_OUTPUT_SCHEMA),
            (PREWRITE_GATE_INPUT_SCHEMA, PREWRITE_GATE_OUTPUT_SCHEMA),
            (CREATION_HANDOFF_INPUT_SCHEMA, CREATION_HANDOFF_OUTPUT_SCHEMA),
        )
        builder_ids = (
            "zhimeng.workflow-input.project-audit",
            "zhimeng.workflow-input.prewrite-gate",
            "zhimeng.workflow-input.creation-handoff",
        )
        steps = []
        for call, (input_schema, output_schema), builder_id in zip(
            route.chapter_call_list,
            contract_pairs,
            builder_ids,
            strict=True,
        ):
            skill = self._active_skill(pack.project_id, call.skill_id)
            if (
                skill.manifest.input_schema != input_schema
                or skill.manifest.output_schema != output_schema
            ):
                raise AgentProviderError(
                    "managed Skill contracts do not match the workflow step",
                    code="workflow_skill_contract_mismatch",
                    retryable=False,
                )
            steps.append(
                build_workflow_step(
                    ordinal=call.ordinal,
                    skill_id=call.skill_id,
                    expected_skill_version_id=skill.skill_version_id,
                    expected_skill_version=skill.manifest.version,
                    expected_manifest_fingerprint=skill.manifest_fingerprint,
                    expected_package_fingerprint=skill.package_fingerprint,
                    input_contract=input_schema,
                    output_contract=output_schema,
                    input_builder_id=builder_id,
                    input_builder_version=INPUT_BUILDER_VERSION,
                    max_attempts=1,
                )
            )

        return build_workflow_plan(
            run_id=run.run_id,
            project_id=pack.project_id,
            project_revision=pack.project_revision,
            project_fingerprint=pack.project_fingerprint,
            context_pack_id=pack.context_pack_id,
            context_pack_hash=pack.pack_hash,
            knowledge_fingerprint=pack.knowledge_fingerprint,
            route_receipt_id=route.route_receipt_id,
            route_receipt_hash=route.receipt_hash,
            operation_package_id=operation_package.operation_package_id,
            operation_package_hash=operation_package.package_hash,
            steps=tuple(steps),
            created_at=run.created_at,
        )

    def validate_plan_dependencies(self, plan: WorkflowPlan) -> None:
        """Fail closed if a persisted Plan's managed Skill dependencies drift."""

        for step in plan.steps:
            skill = self._active_skill(plan.project_id, step.skill_id)
            if (
                skill.skill_version_id != step.expected_skill_version_id
                or skill.manifest.version != step.expected_skill_version
                or skill.manifest.input_schema != step.input_contract
                or skill.manifest.output_schema != step.output_contract
                or skill.manifest_fingerprint
                != step.expected_manifest_fingerprint
                or skill.package_fingerprint != step.expected_package_fingerprint
            ):
                raise AgentProviderError(
                    "Workflow Plan managed Skill dependency changed",
                    code="workflow_plan_skill_changed",
                    retryable=False,
                )

    def _active_skill(
        self,
        project_id: str,
        skill_id: str,
    ) -> ManagedSkillVersion:
        try:
            return self.skill_executor.active_skill(project_id, skill_id)
        except ManagedSkillError as exc:
            raise AgentProviderError(
                str(exc),
                code=exc.code,
                retryable=exc.retryable,
            ) from exc

    def _reconcile_terminal_steps(
        self,
        plan: WorkflowPlan,
    ) -> tuple[WorkflowStepCheckpoint, ...]:
        chain = self.store.list_workflow_checkpoints(plan.run_id)
        checkpoints = self._step_checkpoints(chain)
        if any(isinstance(item, ProviderIoBoundary) for item in chain):
            return checkpoints
        if checkpoints and checkpoints[-1].execution_status != "succeeded":
            return checkpoints

        while len(checkpoints) < len(plan.steps):
            ordinal = len(checkpoints) + 1
            provider_tool_call_id = workflow_provider_tool_call_id(
                plan.route_receipt_id,
                ordinal,
            )
            runtime_tool_call_id = workflow_runtime_tool_call_id(
                plan.run_id,
                provider_tool_call_id,
            )
            try:
                tool_call = self.store.get_tool_call(runtime_tool_call_id)
            except AgentStoreNotFound:
                break
            if tool_call.status not in {"succeeded", "failed", "cancelled"}:
                break
            previous = checkpoints[-1] if checkpoints else None
            checkpoint = self._reconcile_one_step(
                plan,
                ordinal,
                provider_tool_call_id,
                previous,
            )
            checkpoints = (*checkpoints, checkpoint)
            if checkpoint.execution_status != "succeeded":
                break
        return checkpoints

    async def _reconcile_with_retry(
        self,
        plan: WorkflowPlan,
        step_ordinal: int,
        provider_tool_call_id: str,
        previous: WorkflowStepCheckpoint | None,
    ) -> WorkflowStepCheckpoint:
        last_error: ContextStoreError | None = None
        for attempt in range(2):
            try:
                return await asyncio.to_thread(
                    self._reconcile_one_step,
                    plan,
                    step_ordinal,
                    provider_tool_call_id,
                    previous,
                )
            except ContextStoreIntegrity:
                raise
            except ContextStoreError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0)
        raise AgentProviderError(
            str(last_error or "Workflow execution receipt was not persisted"),
            code="workflow_checkpoint_reconciliation_failed",
            retryable=True,
        ) from last_error

    def _reconcile_one_step(
        self,
        plan: WorkflowPlan,
        step_ordinal: int,
        provider_tool_call_id: str,
        previous: WorkflowStepCheckpoint | None,
    ) -> WorkflowStepCheckpoint:
        runtime_tool_call_id = workflow_runtime_tool_call_id(
            plan.run_id,
            provider_tool_call_id,
        )
        try:
            self.context_store.reconcile_route_execution(
                plan.context_pack_id,
                call_ordinal=step_ordinal,
                run_id=plan.run_id,
                tool_call_id=runtime_tool_call_id,
            )
            checkpoint = self.store.build_workflow_step_checkpoint_from_execution(
                plan,
                step_ordinal=step_ordinal,
                provider_tool_call_id=provider_tool_call_id,
                previous_checkpoint=previous,
            )
            return self.store.append_workflow_step_checkpoint(
                checkpoint,
                expected_previous_checkpoint_hash=(
                    None if previous is None else previous.checkpoint_hash
                ),
            )
        except ContextStoreIntegrity as exc:
            raise AgentProviderError(
                str(exc),
                code="workflow_checkpoint_integrity_failed",
                retryable=False,
            ) from exc

    def _validated_stored_call(
        self,
        plan: WorkflowPlan,
        checkpoint: WorkflowStepCheckpoint,
    ) -> StoredToolCall:
        if checkpoint.tool_call_id is None or checkpoint.provider_tool_call_id is None:
            raise AgentProviderError(
                "Successful workflow checkpoint has no ToolCall identity",
                code="workflow_checkpoint_integrity_failed",
                retryable=False,
            )
        expected_provider_id = workflow_provider_tool_call_id(
            plan.route_receipt_id,
            checkpoint.step_ordinal,
        )
        expected_runtime_id = workflow_runtime_tool_call_id(
            plan.run_id,
            expected_provider_id,
        )
        if (
            checkpoint.provider_tool_call_id != expected_provider_id
            or checkpoint.tool_call_id != expected_runtime_id
        ):
            raise AgentProviderError(
                "Workflow checkpoint ToolCall identity is invalid",
                code="workflow_checkpoint_integrity_failed",
                retryable=False,
            )
        return self.store.get_tool_call(checkpoint.tool_call_id)

    def _step_arguments(
        self,
        plan: WorkflowPlan,
        step_ordinal: int,
        *,
        audit_checkpoint: WorkflowStepCheckpoint | None,
        audit_call: StoredToolCall | None,
        prewrite_checkpoint: WorkflowStepCheckpoint | None,
        prewrite_call: StoredToolCall | None,
    ) -> dict[str, object]:
        if step_ordinal == 1:
            return {
                "skill_id": CREATION_PROJECT_AUDIT_SKILL_ID,
                "input": {"schema": PROJECT_AUDIT_INPUT_CONTRACT},
            }
        audit_receipt, finding_count = self._audit_binding(
            plan,
            audit_checkpoint,
            audit_call,
        )
        if step_ordinal == 2:
            operation_package = self.context_store.get_prewrite_operation_package(
                plan.context_pack_id
            )
            return {
                "skill_id": CREATION_PREWRITE_GATE_SKILL_ID,
                "input": {
                    "schema": PREWRITE_GATE_INPUT_CONTRACT,
                    "operation_package": operation_package.model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                    "project_audit_execution_receipt_hash": audit_receipt.receipt_hash,
                    "project_audit_skill_receipt_hash": audit_receipt.skill_receipt_hash,
                    "project_audit_tool_result_hash": audit_receipt.tool_result_hash,
                    "project_audit_domain_status": audit_receipt.domain_status,
                    "project_audit_finding_count": finding_count,
                },
            }
        if step_ordinal != 3:
            raise AgentProviderError(
                "Workflow step has no input builder",
                code="workflow_step_input_builder_missing",
                retryable=False,
            )
        prewrite_receipt, prewrite_gate = self._prewrite_binding(
            plan,
            prewrite_checkpoint,
            prewrite_call,
        )
        operation_package = self.context_store.get_prewrite_operation_package(
            plan.context_pack_id
        )
        return {
            "skill_id": CREATION_HANDOFF_SKILL_ID,
            "input": {
                "schema": CREATION_HANDOFF_INPUT_CONTRACT,
                "workflow_plan_id": plan.workflow_plan_id,
                "workflow_plan_hash": plan.plan_hash,
                "knowledge_fingerprint": plan.knowledge_fingerprint,
                "operation_package": operation_package.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                "project_audit_execution_receipt_hash": audit_receipt.receipt_hash,
                "project_audit_skill_receipt_hash": audit_receipt.skill_receipt_hash,
                "project_audit_tool_result_hash": audit_receipt.tool_result_hash,
                "project_audit_checkpoint_hash": audit_checkpoint.checkpoint_hash,
                "project_audit_domain_status": audit_receipt.domain_status,
                "project_audit_finding_count": finding_count,
                "prewrite_gate_execution_receipt_hash": prewrite_receipt.receipt_hash,
                "prewrite_gate_skill_receipt_hash": prewrite_receipt.skill_receipt_hash,
                "prewrite_gate_tool_result_hash": prewrite_receipt.tool_result_hash,
                "prewrite_gate_checkpoint_hash": prewrite_checkpoint.checkpoint_hash,
                "prewrite_gate_domain_status": prewrite_receipt.domain_status,
                "prewrite_gate_status": prewrite_gate["gate_status"],
                "prewrite_gate_formal_draft_allowed": prewrite_gate[
                    "formal_draft_allowed"
                ],
                "prewrite_gate_failure_signatures": prewrite_gate[
                    "failure_signatures"
                ],
            },
        }

    def _audit_binding(
        self,
        plan: WorkflowPlan,
        audit_checkpoint: WorkflowStepCheckpoint | None,
        audit_call: StoredToolCall | None,
    ) -> tuple[object, int]:
        if audit_checkpoint is None or audit_call is None:
            raise AgentProviderError(
                "prewrite gate requires the succeeded project-audit checkpoint",
                code="workflow_prewrite_audit_binding_missing",
                retryable=False,
            )
        audit_receipt = self.context_store.get_route_execution(
            plan.context_pack_id,
            call_ordinal=1,
        )
        if (
            audit_receipt is None
            or audit_receipt.execution_status != "succeeded"
            or audit_receipt.skill_receipt_hash is None
            or audit_receipt.domain_status is None
            or not isinstance(audit_call.output, dict)
        ):
            raise AgentProviderError(
                "prewrite gate requires the succeeded project-audit execution receipt",
                code="workflow_prewrite_audit_binding_missing",
                retryable=False,
            )
        audit_summary = audit_call.output.get("audit")
        finding_count = (
            audit_summary.get("finding_count")
            if isinstance(audit_summary, dict)
            else None
        )
        if isinstance(finding_count, bool) or not isinstance(finding_count, int):
            raise AgentProviderError(
                "project-audit ToolResult has no valid finding count",
                code="workflow_prewrite_audit_result_invalid",
                retryable=False,
            )
        return audit_receipt, finding_count

    def _prewrite_binding(
        self,
        plan: WorkflowPlan,
        prewrite_checkpoint: WorkflowStepCheckpoint | None,
        prewrite_call: StoredToolCall | None,
    ) -> tuple[object, dict[str, object]]:
        if prewrite_checkpoint is None or prewrite_call is None:
            raise AgentProviderError(
                "creation handoff requires the succeeded prewrite-gate checkpoint",
                code="workflow_handoff_prewrite_binding_missing",
                retryable=False,
            )
        prewrite_receipt = self.context_store.get_route_execution(
            plan.context_pack_id,
            call_ordinal=2,
        )
        if (
            prewrite_receipt is None
            or prewrite_receipt.execution_status != "succeeded"
            or prewrite_receipt.skill_receipt_hash is None
            or prewrite_receipt.domain_status is None
            or not isinstance(prewrite_call.output, dict)
        ):
            raise AgentProviderError(
                "creation handoff requires the succeeded prewrite-gate execution receipt",
                code="workflow_handoff_prewrite_binding_missing",
                retryable=False,
            )
        prewrite_gate = prewrite_call.output.get("prewrite_gate")
        if not isinstance(prewrite_gate, dict):
            raise AgentProviderError(
                "prewrite-gate ToolResult has no durable gate payload",
                code="workflow_handoff_prewrite_result_invalid",
                retryable=False,
            )
        if prewrite_gate.get("formal_draft_allowed") is not False:
            raise AgentProviderError(
                "creation handoff cannot bind a formally authorized prewrite gate",
                code="workflow_handoff_formal_draft_forbidden",
                retryable=False,
            )
        if prewrite_gate.get("gate_status") not in {"blocked", "risk_draft_only"}:
            raise AgentProviderError(
                "prewrite-gate ToolResult has no valid gate status",
                code="workflow_handoff_prewrite_result_invalid",
                retryable=False,
            )
        signatures = prewrite_gate.get("failure_signatures")
        if not isinstance(signatures, list) or any(
            not isinstance(item, str) for item in signatures
        ):
            raise AgentProviderError(
                "prewrite-gate ToolResult has no stable failure signatures",
                code="workflow_handoff_prewrite_result_invalid",
                retryable=False,
            )
        return prewrite_receipt, prewrite_gate

    @staticmethod
    def _checkpoint_for_step(
        checkpoints: tuple[WorkflowStepCheckpoint, ...],
        ordinal: int,
    ) -> WorkflowStepCheckpoint | None:
        matches = [item for item in checkpoints if item.step_ordinal == ordinal]
        if len(matches) > 1:
            raise AgentProviderError(
                "Workflow contains duplicate step checkpoints",
                code="workflow_checkpoint_integrity_failed",
                retryable=False,
            )
        return matches[0] if matches else None

    @staticmethod
    def _step_checkpoints(
        chain: list[WorkflowStepCheckpoint | ProviderIoBoundary]
        | tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
    ) -> tuple[WorkflowStepCheckpoint, ...]:
        return tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )

    @staticmethod
    def _provider_messages(
        checkpoint: WorkflowStepCheckpoint,
        tool_call: StoredToolCall,
    ) -> tuple[dict[str, object], dict[str, object]]:
        provider_tool_call_id = checkpoint.provider_tool_call_id
        if provider_tool_call_id is None:
            raise AgentProviderError(
                "Workflow checkpoint has no Provider ToolCall identity",
                code="workflow_checkpoint_integrity_failed",
                retryable=False,
            )
        assistant_message: dict[str, object] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": provider_tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "skill_execute",
                        "arguments": json.dumps(
                            tool_call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
        }
        payload = {
            "status": tool_call.status,
            "output": tool_call.output or {},
            "error": (
                tool_call.error.model_dump(mode="json", by_alias=True)
                if tool_call.error is not None
                else None
            ),
        }
        tool_message: dict[str, object] = {
            "role": "tool",
            "tool_call_id": provider_tool_call_id,
            "content": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return assistant_message, tool_message

    @staticmethod
    def _raise_for_terminal_call(tool_call: StoredToolCall) -> None:
        if tool_call.status == "cancelled":
            raise AgentProviderCancelled()
        if tool_call.status != "failed":
            return
        if tool_call.error is None:
            raise AgentProviderError(
                "Workflow step failed without a persisted error",
                code="workflow_step_failed",
                retryable=False,
            )
        raise AgentProviderError(
            tool_call.error.error.message,
            code=tool_call.error.error.code,
            retryable=tool_call.error.error.retryable,
        )


__all__ = [
    "ExecuteToolCall",
    "WorkflowExecutionResult",
    "WorkflowRunner",
    "WorkflowStartupRecovery",
    "workflow_provider_tool_call_id",
    "workflow_runtime_tool_call_id",
]
