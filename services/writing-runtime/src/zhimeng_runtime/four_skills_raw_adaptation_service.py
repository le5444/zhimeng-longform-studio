from __future__ import annotations

from datetime import UTC, datetime
import asyncio
from pathlib import Path
import hashlib
import json

from zhimeng_runtime.four_skills_raw_adaptation_models import (
    FAMILY_MANAGED_SKILL_IDS,
    FourSkillsInstructionChainHandoffReceipt,
    FourSkillsInstructionChainHandoffRequest,
    FourSkillsInstructionChainHandoffResponse,
    FourSkillsInstructionChainHandoffStep,
    FourSkillsRawAdaptationMaterializeRequest,
    FourSkillsRawAdaptationPlanRequest,
    FourSkillsRawAdaptationReceipt,
    FourSkillsRawAdaptationResponse,
    FourSkillsRawAdaptationStatus,
    FourSkillsRawFamily,
    FourSkillsRawMemberRecord,
    FourSkillsWorkflowBindingReceipt,
    FourSkillsWorkflowBindingRequest,
    FourSkillsWorkflowBindingResponse,
    FourSkillsWorkflowBindingStep,
    FourSkillsWorkflowOrchestrationReceipt,
    FourSkillsWorkflowOrchestrationRequest,
    FourSkillsWorkflowOrchestrationResponse,
    FourSkillsWorkflowOrchestrationStep,
    four_skills_instruction_chain_handoff_receipt_hash,
    four_skills_raw_adaptation_receipt_hash,
    four_skills_workflow_binding_receipt_hash,
    four_skills_workflow_orchestration_receipt_hash,
    FourSkillsGenericExecutionProbeRequest,
    FourSkillsGenericExecutionProbeResponse,
    FourSkillsGenericExecutionProbeReceipt,
    FourSkillsGenericExecutionProbeStep,
    four_skills_generic_execution_probe_receipt_hash,
    FourSkillsGenericManagedExecutionRequest,
    FourSkillsGenericManagedExecutionResponse,
    FourSkillsGenericManagedExecutionReceipt,
    FourSkillsGenericManagedExecutionStep,
    four_skills_generic_managed_execution_receipt_hash,
)
from zhimeng_runtime.skill_adaptation_models import (
    SkillAdaptationMaterializeRequest,
    SkillAdaptationPlanRequest,
)
from zhimeng_runtime.skill_adaptation_service import (
    SkillAdaptationError,
    SkillAdaptationService,
)


FAMILY_ORDER = (
    FourSkillsRawFamily.SOURCE_INGEST,
    FourSkillsRawFamily.DISTILLATION,
    FourSkillsRawFamily.KNOWLEDGE_CONTEXT,
    FourSkillsRawFamily.CREATION_SUITE,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class FourSkillsRawAdaptationError(RuntimeError):
    code = "four_skills_raw_adaptation_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class FourSkillsRawAdaptationNotFound(FourSkillsRawAdaptationError):
    code = "four_skills_raw_adaptation_not_found"


class FourSkillsRawAdaptationInvalid(FourSkillsRawAdaptationError):
    code = "four_skills_raw_adaptation_invalid"


class FourSkillsRawAdaptationConflict(FourSkillsRawAdaptationError):
    code = "four_skills_raw_adaptation_conflict"


class FourSkillsRawAdaptationUnavailable(FourSkillsRawAdaptationError):
    code = "four_skills_raw_adaptation_unavailable"
    retryable = True


class FourSkillsRawAdaptationService:
    """Plan/materialize four raw SKILL.md families into linked inert packages.

    Never executes raw SKILL.md, never claims generic exactly-once, and never
    unlocks formal draft. Each family is adapted through SkillAdaptationService.
    """

    def __init__(
        self,
        skill_adaptation_service: SkillAdaptationService | None,
        *,
        state_root: Path,
        skill_executor=None,
        store=None,
    ) -> None:
        self.skill_adaptation_service = skill_adaptation_service
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.skill_executor = skill_executor
        self.store = store

    def get(self, batch_id: str) -> FourSkillsRawAdaptationResponse:
        receipt = self._read_receipt(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        return FourSkillsRawAdaptationResponse(
            receipt=receipt,
            history=self._read_history(batch_id),
        )

    def plan(
        self,
        request: FourSkillsRawAdaptationPlanRequest,
    ) -> FourSkillsRawAdaptationResponse:
        if self.skill_adaptation_service is None:
            raise FourSkillsRawAdaptationUnavailable(
                "skill adaptation service is unavailable"
            )
        if len(request.members) != 4:
            raise FourSkillsRawAdaptationInvalid(
                "four skills raw adaptation requires exactly four members"
            )
        by_family = {item.family: item for item in request.members}
        if set(by_family) != set(FAMILY_ORDER):
            raise FourSkillsRawAdaptationInvalid(
                "four skills raw adaptation requires source_ingest, distillation, "
                "knowledge_context, and creation_suite exactly once"
            )

        members: list[FourSkillsRawMemberRecord] = []
        for ordinal, family in enumerate(FAMILY_ORDER, start=1):
            member = by_family[family]
            try:
                planned = self.skill_adaptation_service.plan(
                    SkillAdaptationPlanRequest(source_path=member.source_path)
                )
            except SkillAdaptationError as exc:
                raise FourSkillsRawAdaptationInvalid(
                    f"{family.value}: {exc}"
                ) from exc
            plan = planned.plan
            members.append(
                FourSkillsRawMemberRecord(
                    family=family,
                    ordinal=ordinal,
                    source_path=member.source_path,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    materialization_id=None,
                    skill_id=plan.target.skill_id,
                    skill_version_id=None,
                    package_fingerprint=None,
                    present=True,
                    linked=True,
                    executable=False,
                    formal_draft_allowed=False,
                    detail=(
                        f"raw SKILL.md planned as inert instruction_source "
                        f"{plan.target.skill_id}"
                    ),
                )
            )

        batch_id = self._batch_id(members)
        existing = self._read_receipt(batch_id)
        if existing is not None and existing.status is FourSkillsRawAdaptationStatus.PLANNED:
            return FourSkillsRawAdaptationResponse(
                receipt=existing,
                history=self._read_history(batch_id),
            )

        receipt = self._build_receipt(
            batch_id=batch_id,
            status=FourSkillsRawAdaptationStatus.PLANNED,
            members=members,
            detail=(
                "Four raw skill families planned into linked inert adaptation plans; "
                "no raw SKILL.md execution and no formal draft unlock."
            ),
        )
        return self._persist(receipt, history=[])

    def materialize(
        self,
        request: FourSkillsRawAdaptationMaterializeRequest,
    ) -> FourSkillsRawAdaptationResponse:
        if self.skill_adaptation_service is None:
            raise FourSkillsRawAdaptationUnavailable(
                "skill adaptation service is unavailable"
            )
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        if current.status is FourSkillsRawAdaptationStatus.MATERIALIZED:
            return FourSkillsRawAdaptationResponse(
                receipt=current,
                history=self._read_history(current.batch_id),
            )
        if current.status is not FourSkillsRawAdaptationStatus.PLANNED:
            raise FourSkillsRawAdaptationInvalid(
                "only planned raw adaptation batches can be materialized"
            )

        members: list[FourSkillsRawMemberRecord] = []
        for item in current.members:
            if item.plan_id is None or item.plan_hash is None:
                raise FourSkillsRawAdaptationInvalid(
                    f"{item.family.value}: missing adaptation plan identity"
                )
            try:
                materialized = self.skill_adaptation_service.materialize(
                    SkillAdaptationMaterializeRequest(
                        expected_plan_id=item.plan_id,
                        expected_plan_hash=item.plan_hash,
                        import_after_materialize=request.import_after_materialize,
                    )
                )
            except SkillAdaptationError as exc:
                raise FourSkillsRawAdaptationInvalid(
                    f"{item.family.value}: {exc}"
                ) from exc
            mat = materialized.materialization
            if mat.formal_draft_allowed is not False:
                raise FourSkillsRawAdaptationConflict(
                    f"{item.family.value}: materialization unlocked formal draft"
                )
            if mat.kind != "instruction_source":
                raise FourSkillsRawAdaptationConflict(
                    f"{item.family.value}: materialization is not instruction_source"
                )
            members.append(
                FourSkillsRawMemberRecord(
                    family=item.family,
                    ordinal=item.ordinal,
                    source_path=item.source_path,
                    plan_id=item.plan_id,
                    plan_hash=item.plan_hash,
                    materialization_id=mat.materialization_id,
                    skill_id=mat.skill_id,
                    skill_version_id=mat.skill_version_id,
                    package_fingerprint=mat.package_fingerprint,
                    present=True,
                    linked=True,
                    executable=False,
                    formal_draft_allowed=False,
                    detail=(
                        f"raw SKILL.md materialized as inert package "
                        f"{mat.skill_id}; imported={mat.imported}"
                    ),
                )
            )

        receipt = self._build_receipt(
            batch_id=current.batch_id,
            status=FourSkillsRawAdaptationStatus.MATERIALIZED,
            members=members,
            detail=(
                "Four raw skill families materialized into linked inert packages; "
                "raw SKILL.md remains non-executable and formal draft stays locked."
            ),
        )
        return self._persist(receipt, history=self._read_history(current.batch_id))


    def bind_workflow(
        self,
        request: FourSkillsWorkflowBindingRequest,
    ) -> FourSkillsWorkflowBindingResponse:
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        if current.status is not FourSkillsRawAdaptationStatus.MATERIALIZED:
            raise FourSkillsRawAdaptationInvalid(
                "workflow binding requires a materialized raw adaptation batch"
            )
        if not current.authorization_input_ready:
            raise FourSkillsRawAdaptationInvalid(
                "workflow binding requires authorization_input_ready raw adaptation"
            )

        existing = self._read_workflow_binding(current.batch_id)
        if existing is not None and existing.batch_hash == current.batch_hash:
            return FourSkillsWorkflowBindingResponse(receipt=existing)

        steps: list[FourSkillsWorkflowBindingStep] = []
        for item in current.members:
            bound = bool(
                item.present
                and item.linked
                and item.skill_id
                and item.skill_version_id
                and item.package_fingerprint
                and item.executable is False
            )
            steps.append(
                FourSkillsWorkflowBindingStep(
                    family=item.family,
                    ordinal=item.ordinal,
                    skill_id=str(item.skill_id or "missing"),
                    skill_version_id=str(item.skill_version_id or "missing"),
                    package_fingerprint=item.package_fingerprint or ("0" * 64),
                    runner="instruction_source",
                    executable=False,
                    bound=bound,
                    detail=(
                        "instruction_source package bound into generic workflow surface"
                        if bound
                        else "member missing materialized instruction_source identity"
                    ),
                )
            )
        all_bound = all(item.bound for item in steps)
        status = "bound" if all_bound else "incomplete"
        bound_at = _utcnow()
        material_for_id = {
            "batch_id": current.batch_id,
            "batch_hash": current.batch_hash,
            "steps": [
                {
                    "family": item.family.value,
                    "skill_version_id": item.skill_version_id,
                    "package_fingerprint": item.package_fingerprint,
                }
                for item in steps
            ],
        }
        binding_id = "four-skills-workflow-binding-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Four raw-adapted instruction_source packages bound into the generic "
            "workflow surface; packages remain non-executable and raw SKILL.md is not run."
            if all_bound
            else "Workflow binding incomplete: one or more instruction_source packages missing."
        )
        temp = FourSkillsWorkflowBindingReceipt.model_construct(
            schema_id="zhimeng.four-skills-workflow-binding-receipt.v1",
            binding_id=binding_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            all_bound=all_bound,
            workflow_ready=all_bound and status == "bound",
            executable=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-workflow-binding-1",
            detail=detail,
            bound_at=bound_at,
            binding_hash="0" * 64,
        )
        binding_hash = four_skills_workflow_binding_receipt_hash(temp)
        receipt = FourSkillsWorkflowBindingReceipt(
            binding_id=binding_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            all_bound=all_bound,
            workflow_ready=all_bound and status == "bound",
            executable=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-workflow-binding-1",
            detail=detail,
            bound_at=bound_at,
            binding_hash=binding_hash,
        )
        _atomic_write_json(self._workflow_binding_path(current.batch_id), receipt.model_dump(mode="json", by_alias=True))
        return FourSkillsWorkflowBindingResponse(receipt=receipt)

    def get_workflow_binding(self, batch_id: str) -> FourSkillsWorkflowBindingResponse:
        receipt = self._read_workflow_binding(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills workflow binding was not found"
            )
        return FourSkillsWorkflowBindingResponse(receipt=receipt)


    def orchestrate_workflow(
        self,
        request: FourSkillsWorkflowOrchestrationRequest,
    ) -> FourSkillsWorkflowOrchestrationResponse:
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        binding = self._read_workflow_binding(current.batch_id)
        if binding is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills workflow binding was not found"
            )
        if binding.binding_id != request.expected_binding_id:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow binding id does not match"
            )
        if binding.binding_hash != request.expected_binding_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow binding hash does not match"
            )
        if not binding.workflow_ready or binding.status != "bound":
            raise FourSkillsRawAdaptationInvalid(
                "workflow orchestration requires a ready generic instruction workflow binding"
            )

        existing = self._read_workflow_orchestration(current.batch_id)
        if (
            existing is not None
            and existing.batch_hash == current.batch_hash
            and existing.binding_hash == binding.binding_hash
        ):
            return FourSkillsWorkflowOrchestrationResponse(receipt=existing)

        steps: list[FourSkillsWorkflowOrchestrationStep] = []
        for item in binding.steps:
            ok = bool(
                item.bound
                and item.skill_id
                and item.skill_version_id
                and item.package_fingerprint
                and item.executable is False
                and item.runner == "instruction_source"
            )
            steps.append(
                FourSkillsWorkflowOrchestrationStep(
                    family=item.family,
                    ordinal=item.ordinal,
                    skill_id=item.skill_id,
                    skill_version_id=item.skill_version_id,
                    package_fingerprint=item.package_fingerprint,
                    runner="instruction_orchestrator",
                    status="completed" if ok else "failed",
                    managed_skill_execution=False,
                    raw_skill_md_execution=False,
                    detail=(
                        "instruction_source step advanced by generic instruction orchestrator"
                        if ok
                        else "instruction_source step missing bound package identity"
                    ),
                )
            )
        completed = sum(1 for item in steps if item.status == "completed")
        if completed == 4:
            status = "completed"
        elif completed == 0:
            status = "failed"
        else:
            status = "incomplete"
        orchestrated_at = _utcnow()
        material_for_id = {
            "batch_id": current.batch_id,
            "batch_hash": current.batch_hash,
            "binding_id": binding.binding_id,
            "binding_hash": binding.binding_hash,
            "steps": [
                {
                    "family": item.family.value,
                    "skill_version_id": item.skill_version_id,
                    "package_fingerprint": item.package_fingerprint,
                    "status": item.status,
                }
                for item in steps
            ],
        }
        orchestration_id = "four-skills-workflow-orchestration-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Generic instruction workflow orchestration completed over four bound "
            "instruction_source packages; raw SKILL.md was not executed and managed "
            "skill execution is not claimed."
            if status == "completed"
            else "Generic instruction workflow orchestration did not complete all steps."
        )
        temp = FourSkillsWorkflowOrchestrationReceipt.model_construct(
            schema_id="zhimeng.four-skills-workflow-orchestration-receipt.v1",
            orchestration_id=orchestration_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            binding_id=binding.binding_id,
            binding_hash=binding.binding_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            completed_step_count=completed,
            orchestration_ready=status == "completed",
            runnable=completed > 0,
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-workflow-orchestration-1",
            detail=detail,
            orchestrated_at=orchestrated_at,
            orchestration_hash="0" * 64,
        )
        orchestration_hash = four_skills_workflow_orchestration_receipt_hash(temp)
        receipt = FourSkillsWorkflowOrchestrationReceipt(
            orchestration_id=orchestration_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            binding_id=binding.binding_id,
            binding_hash=binding.binding_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            completed_step_count=completed,
            orchestration_ready=status == "completed",
            runnable=completed > 0,
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-workflow-orchestration-1",
            detail=detail,
            orchestrated_at=orchestrated_at,
            orchestration_hash=orchestration_hash,
        )
        _atomic_write_json(
            self._workflow_orchestration_path(current.batch_id),
            receipt.model_dump(mode="json", by_alias=True),
        )
        return FourSkillsWorkflowOrchestrationResponse(receipt=receipt)

    def get_workflow_orchestration(
        self, batch_id: str
    ) -> FourSkillsWorkflowOrchestrationResponse:
        receipt = self._read_workflow_orchestration(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills workflow orchestration was not found"
            )
        return FourSkillsWorkflowOrchestrationResponse(receipt=receipt)


    def handoff_instruction_chain(
        self,
        request: FourSkillsInstructionChainHandoffRequest,
    ) -> FourSkillsInstructionChainHandoffResponse:
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        binding = self._read_workflow_binding(current.batch_id)
        if binding is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills workflow binding was not found"
            )
        if binding.binding_id != request.expected_binding_id:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow binding id does not match"
            )
        if binding.binding_hash != request.expected_binding_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow binding hash does not match"
            )
        orchestration = self._read_workflow_orchestration(current.batch_id)
        if orchestration is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills workflow orchestration was not found"
            )
        if orchestration.orchestration_id != request.expected_orchestration_id:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow orchestration id does not match"
            )
        if orchestration.orchestration_hash != request.expected_orchestration_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills workflow orchestration hash does not match"
            )
        if (
            not orchestration.orchestration_ready
            or orchestration.status != "completed"
            or orchestration.completed_step_count != 4
        ):
            raise FourSkillsRawAdaptationInvalid(
                "instruction chain handoff requires a completed generic instruction workflow orchestration"
            )

        existing = self._read_instruction_chain_handoff(current.batch_id)
        if (
            existing is not None
            and existing.batch_hash == current.batch_hash
            and existing.binding_hash == binding.binding_hash
            and existing.orchestration_hash == orchestration.orchestration_hash
        ):
            return FourSkillsInstructionChainHandoffResponse(receipt=existing)

        steps: list[FourSkillsInstructionChainHandoffStep] = []
        for item in orchestration.steps:
            ok = bool(
                item.status == "completed"
                and item.skill_id
                and item.skill_version_id
                and item.package_fingerprint
                and item.managed_skill_execution is False
                and item.raw_skill_md_execution is False
                and item.runner == "instruction_orchestrator"
            )
            steps.append(
                FourSkillsInstructionChainHandoffStep(
                    family=item.family,
                    ordinal=item.ordinal,
                    skill_id=item.skill_id,
                    skill_version_id=item.skill_version_id,
                    package_fingerprint=item.package_fingerprint,
                    runner="instruction_chain_handoff",
                    status="prepared" if ok else "failed",
                    managed_skill_execution=False,
                    raw_skill_md_execution=False,
                    detail=(
                        "orchestrated instruction_source step prepared for closed handoff"
                        if ok
                        else "orchestrated step missing completed instruction identity"
                    ),
                )
            )
        prepared = sum(1 for item in steps if item.status == "prepared")
        if prepared == 4:
            status = "ready"
        elif prepared == 0:
            status = "failed"
        else:
            status = "incomplete"
        handed_off_at = _utcnow()
        material_for_id = {
            "batch_id": current.batch_id,
            "batch_hash": current.batch_hash,
            "binding_id": binding.binding_id,
            "binding_hash": binding.binding_hash,
            "orchestration_id": orchestration.orchestration_id,
            "orchestration_hash": orchestration.orchestration_hash,
            "steps": [
                {
                    "family": item.family.value,
                    "skill_version_id": item.skill_version_id,
                    "package_fingerprint": item.package_fingerprint,
                    "status": item.status,
                }
                for item in steps
            ],
        }
        handoff_id = "four-skills-instruction-chain-handoff-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Instruction chain handoff prepared over four orchestrated instruction_source "
            "steps; raw SKILL.md was not executed, managed skill execution is not claimed, "
            "and formal draft remains denied."
            if status == "ready"
            else "Instruction chain handoff did not prepare all four steps."
        )
        temp = FourSkillsInstructionChainHandoffReceipt.model_construct(
            schema_id="zhimeng.four-skills-instruction-chain-handoff-receipt.v1",
            handoff_id=handoff_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            binding_id=binding.binding_id,
            binding_hash=binding.binding_hash,
            orchestration_id=orchestration.orchestration_id,
            orchestration_hash=orchestration.orchestration_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            prepared_step_count=prepared,
            instruction_chain_ready=status == "ready",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-instruction-chain-handoff-1",
            detail=detail,
            handed_off_at=handed_off_at,
            handoff_hash="0" * 64,
        )
        handoff_hash = four_skills_instruction_chain_handoff_receipt_hash(temp)
        receipt = FourSkillsInstructionChainHandoffReceipt(
            handoff_id=handoff_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            binding_id=binding.binding_id,
            binding_hash=binding.binding_hash,
            orchestration_id=orchestration.orchestration_id,
            orchestration_hash=orchestration.orchestration_hash,
            status=status,
            workflow_surface="generic_instruction_workflow",
            steps=steps,
            step_count=len(steps),
            prepared_step_count=prepared,
            instruction_chain_ready=status == "ready",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-instruction-chain-handoff-1",
            detail=detail,
            handed_off_at=handed_off_at,
            handoff_hash=handoff_hash,
        )
        _atomic_write_json(
            self._instruction_chain_handoff_path(current.batch_id),
            receipt.model_dump(mode="json", by_alias=True),
        )
        return FourSkillsInstructionChainHandoffResponse(receipt=receipt)


    def probe_generic_execution(
        self,
        request: FourSkillsGenericExecutionProbeRequest,
    ) -> FourSkillsGenericExecutionProbeResponse:
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        handoff = self._read_instruction_chain_handoff(current.batch_id)
        if handoff is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills instruction chain handoff was not found"
            )
        if handoff.handoff_id != request.expected_handoff_id:
            raise FourSkillsRawAdaptationConflict(
                "four skills instruction chain handoff id does not match"
            )
        if handoff.handoff_hash != request.expected_handoff_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills instruction chain handoff hash does not match"
            )
        if not handoff.instruction_chain_ready or handoff.status != "ready":
            raise FourSkillsRawAdaptationInvalid(
                "generic execution probe requires a ready instruction chain handoff"
            )

        existing = self._read_generic_execution_probe(current.batch_id)
        if (
            existing is not None
            and existing.batch_hash == current.batch_hash
            and existing.handoff_hash == handoff.handoff_hash
        ):
            return FourSkillsGenericExecutionProbeResponse(receipt=existing)

        managed = None
        if self.skill_adaptation_service is not None:
            managed = getattr(self.skill_adaptation_service, "managed_skill_service", None)

        steps: list[FourSkillsGenericExecutionProbeStep] = []
        for item in handoff.steps:
            package_kind = "instruction_source"
            catalog_present = False
            fingerprint_matched = False
            detail = "instruction_source package assessed as non-executable by SkillExecutor policy"
            denial_code = "skill_not_executable"
            status = "denied"
            executor_would_accept = False

            if managed is not None and item.skill_version_id:
                try:
                    version = managed._load_version(item.skill_version_id)
                except Exception:
                    version = None
                if version is None:
                    package_kind = "unknown"
                    catalog_present = False
                    fingerprint_matched = False
                    denial_code = "skill_version_missing"
                    status = "failed"
                    detail = "managed catalog does not contain the handed-off skill version"
                else:
                    catalog_present = True
                    package_kind = (
                        "instruction_source"
                        if version.manifest.kind == "instruction_source"
                        else (
                            "deterministic_tool"
                            if version.manifest.kind == "deterministic_tool"
                            else "unknown"
                        )
                    )
                    fingerprint_matched = (
                        version.package_fingerprint == item.package_fingerprint
                        and version.manifest.id == item.skill_id
                    )
                    if not fingerprint_matched:
                        denial_code = "package_fingerprint_mismatch"
                        status = "failed"
                        detail = "managed package fingerprint or skill id does not match handoff"
                    elif package_kind == "deterministic_tool":
                        # Probe only assesses readiness; it still does not execute.
                        executor_would_accept = True
                        denial_code = "none"
                        status = "would_accept"
                        detail = (
                            "deterministic_tool would be accepted by SkillExecutor, "
                            "but this probe does not execute managed skills"
                        )
                    else:
                        denial_code = "skill_not_executable"
                        status = "denied"
                        detail = (
                            "instruction sources are immutable materialization inputs and "
                            "cannot be executed as managed tools"
                        )
            else:
                # Without catalog access, instruction-chain packages remain non-executable.
                package_kind = "instruction_source"
                catalog_present = False
                fingerprint_matched = False
                denial_code = "skill_not_executable"
                status = "denied"
                detail = (
                    "instruction_source package assessed without catalog load; "
                    "SkillExecutor rejects non-deterministic_tool kinds"
                )

            steps.append(
                FourSkillsGenericExecutionProbeStep(
                    family=item.family,
                    ordinal=item.ordinal,
                    skill_id=item.skill_id,
                    skill_version_id=item.skill_version_id,
                    package_fingerprint=item.package_fingerprint,
                    package_kind=package_kind,
                    catalog_present=catalog_present,
                    fingerprint_matched=fingerprint_matched,
                    executor_would_accept=executor_would_accept,
                    denial_code=denial_code,
                    status=status,
                    managed_skill_execution=False,
                    raw_skill_md_execution=False,
                    detail=detail,
                )
            )

        assessed = sum(1 for item in steps if item.status in {"denied", "would_accept"})
        denied = sum(1 for item in steps if item.status == "denied")
        would_accept = sum(1 for item in steps if item.status == "would_accept")
        failed = sum(1 for item in steps if item.status == "failed")
        if assessed == 4 and failed == 0:
            status_value = "complete"
        elif assessed == 0 and failed == 4:
            status_value = "failed"
        else:
            status_value = "incomplete"

        probed_at = _utcnow()
        material_for_id = {
            "batch_id": current.batch_id,
            "batch_hash": current.batch_hash,
            "handoff_id": handoff.handoff_id,
            "handoff_hash": handoff.handoff_hash,
            "steps": [
                {
                    "family": item.family.value,
                    "skill_version_id": item.skill_version_id,
                    "package_fingerprint": item.package_fingerprint,
                    "status": item.status,
                    "denial_code": item.denial_code,
                }
                for item in steps
            ],
        }
        probe_id = "four-skills-generic-execution-probe-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Generic execution probe completed over four instruction-chain packages; "
            "SkillExecutor would reject instruction_source kinds, managed/raw execution "
            "is not claimed, and formal draft remains denied."
            if status_value == "complete" and would_accept == 0
            else (
                "Generic execution probe assessed packages without claiming execution; "
                "generic exactly-once remains false and formal draft stays locked."
                if status_value == "complete"
                else "Generic execution probe did not fully assess all four packages."
            )
        )
        temp = FourSkillsGenericExecutionProbeReceipt.model_construct(
            schema_id="zhimeng.four-skills-generic-execution-probe-receipt.v1",
            probe_id=probe_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            handoff_id=handoff.handoff_id,
            handoff_hash=handoff.handoff_hash,
            status=status_value,
            steps=steps,
            step_count=len(steps),
            assessed_step_count=assessed,
            denied_step_count=denied,
            would_accept_step_count=would_accept,
            probe_complete=status_value == "complete",
            generic_execution_ready=False,
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-generic-execution-probe-1",
            detail=detail,
            probed_at=probed_at,
            probe_hash="0" * 64,
        )
        probe_hash = four_skills_generic_execution_probe_receipt_hash(temp)
        receipt = FourSkillsGenericExecutionProbeReceipt(
            probe_id=probe_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            handoff_id=handoff.handoff_id,
            handoff_hash=handoff.handoff_hash,
            status=status_value,
            steps=steps,
            step_count=len(steps),
            assessed_step_count=assessed,
            denied_step_count=denied,
            would_accept_step_count=would_accept,
            probe_complete=status_value == "complete",
            generic_execution_ready=False,
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-generic-execution-probe-1",
            detail=detail,
            probed_at=probed_at,
            probe_hash=probe_hash,
        )
        _atomic_write_json(
            self._generic_execution_probe_path(current.batch_id),
            receipt.model_dump(mode="json", by_alias=True),
        )
        return FourSkillsGenericExecutionProbeResponse(receipt=receipt)

    def get_generic_execution_probe(
        self, batch_id: str
    ) -> FourSkillsGenericExecutionProbeResponse:
        receipt = self._read_generic_execution_probe(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills generic execution probe was not found"
            )
        return FourSkillsGenericExecutionProbeResponse(receipt=receipt)

    def _generic_execution_probe_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "generic-execution-probe.json"

    def _read_generic_execution_probe(
        self, batch_id: str
    ) -> FourSkillsGenericExecutionProbeReceipt | None:
        path = self._generic_execution_probe_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsGenericExecutionProbeReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def execute_generic_managed(
        self,
        request: FourSkillsGenericManagedExecutionRequest,
    ) -> FourSkillsGenericManagedExecutionResponse:
        current = self._read_receipt(request.expected_batch_id)
        if current is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills raw adaptation batch was not found"
            )
        if current.batch_hash != request.expected_batch_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills raw adaptation batch hash does not match"
            )
        probe = self._read_generic_execution_probe(current.batch_id)
        if probe is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills generic execution probe was not found"
            )
        if probe.probe_id != request.expected_probe_id:
            raise FourSkillsRawAdaptationConflict(
                "four skills generic execution probe id does not match"
            )
        if probe.probe_hash != request.expected_probe_hash:
            raise FourSkillsRawAdaptationConflict(
                "four skills generic execution probe hash does not match"
            )
        if not probe.probe_complete or probe.status != "complete":
            raise FourSkillsRawAdaptationInvalid(
                "generic managed execution requires a complete generic execution probe"
            )
        if self.skill_executor is None or self.store is None:
            raise FourSkillsRawAdaptationUnavailable(
                "managed skill executor or project store is unavailable"
            )

        existing = self._read_generic_managed_execution(current.batch_id)
        if (
            existing is not None
            and existing.batch_hash == current.batch_hash
            and existing.probe_hash == probe.probe_hash
            and existing.project_id == request.project_id
            and existing.project_revision == request.expected_project_revision
            and existing.project_fingerprint == request.expected_project_fingerprint
        ):
            return FourSkillsGenericManagedExecutionResponse(receipt=existing)

        binding = self.store.get_project_binding(request.project_id)
        if binding is None:
            raise FourSkillsRawAdaptationNotFound("project binding was not found")
        if binding.revision != request.expected_project_revision:
            raise FourSkillsRawAdaptationConflict("project revision does not match request")
        if (binding.fingerprint or "") != request.expected_project_fingerprint:
            raise FourSkillsRawAdaptationConflict(
                "project fingerprint does not match request"
            )

        managed = getattr(self.skill_adaptation_service, "managed_skill_service", None)
        steps: list[FourSkillsGenericManagedExecutionStep] = []
        for ordinal, family in enumerate(FAMILY_ORDER, start=1):
            skill_id = FAMILY_MANAGED_SKILL_IDS[family.value]
            skill_version_id = None
            package_fingerprint = None
            status = "failed"
            managed_exec = False
            skill_receipt_id = None
            skill_receipt_hash = None
            denial_code = "execution_failed"
            detail = "managed family execution failed"

            try:
                version = self.skill_executor.active_skill(request.project_id, skill_id)
            except Exception as exc:  # noqa: BLE001 - map any activation miss honestly
                status = "denied"
                denial_code = "skill_not_active"
                detail = f"managed family skill not active: {exc}"
                steps.append(
                    FourSkillsGenericManagedExecutionStep(
                        family=family,
                        ordinal=ordinal,
                        managed_skill_id=skill_id,
                        skill_version_id=skill_version_id,
                        package_fingerprint=package_fingerprint,
                        status=status,
                        managed_skill_execution=False,
                        raw_skill_md_execution=False,
                        skill_receipt_id=None,
                        skill_receipt_hash=None,
                        denial_code=denial_code,
                        detail=detail[:1024],
                    )
                )
                continue

            skill_version_id = version.skill_version_id
            package_fingerprint = version.package_fingerprint
            if version.manifest.kind != "deterministic_tool":
                status = "denied"
                denial_code = "skill_not_executable"
                detail = "mapped family package is not deterministic_tool"
                steps.append(
                    FourSkillsGenericManagedExecutionStep(
                        family=family,
                        ordinal=ordinal,
                        managed_skill_id=skill_id,
                        skill_version_id=skill_version_id,
                        package_fingerprint=package_fingerprint,
                        status=status,
                        managed_skill_execution=False,
                        raw_skill_md_execution=False,
                        skill_receipt_id=None,
                        skill_receipt_hash=None,
                        denial_code=denial_code,
                        detail=detail,
                    )
                )
                continue

            workspace = (
                self.state_root
                / current.batch_id
                / "generic-managed-execution"
                / f"step-{ordinal:02d}-{family.value}"
            )
            if workspace.exists():
                import shutil

                shutil.rmtree(workspace)
            snapshot = workspace / "project-snapshot"
            staging = workspace / "run-staging"
            snapshot.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True, exist_ok=True)
            # materialize a shallow project snapshot for sandbox execution
            project_root = Path(binding.root_path)
            for child in project_root.iterdir():
                target = snapshot / child.name
                if child.is_dir():
                    # copytree for directories; ignore heavy caches
                    import shutil

                    shutil.copytree(child, target, dirs_exist_ok=True)
                elif child.is_file():
                    target.write_bytes(child.read_bytes())

            input_payload = {
                "schema": "zhimeng.four-skills-family-step-request.v1",
                "family": family.value,
                "batch_id": current.batch_id,
                "probe_id": probe.probe_id,
            }
            tool_call_id = "tool_" + hashlib.sha256(
                f"{current.batch_id}\0{probe.probe_id}\0{family.value}\0{skill_id}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            run_id = "run_" + hashlib.sha256(
                f"{current.batch_id}\0{probe.probe_id}\0generic-managed".encode("utf-8")
            ).hexdigest()[:24]
            try:
                outcome = asyncio.run(
                    self.skill_executor.execute(
                        project_id=request.project_id,
                        project_revision=binding.revision,
                        project_fingerprint=binding.fingerprint or "",
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                        skill_id=skill_id,
                        input_payload=input_payload,
                        project_snapshot=snapshot,
                        run_staging=staging,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                denial_code = "execution_failed"
                detail = f"SkillExecutor raised: {exc}"
                steps.append(
                    FourSkillsGenericManagedExecutionStep(
                        family=family,
                        ordinal=ordinal,
                        managed_skill_id=skill_id,
                        skill_version_id=skill_version_id,
                        package_fingerprint=package_fingerprint,
                        status=status,
                        managed_skill_execution=False,
                        raw_skill_md_execution=False,
                        skill_receipt_id=None,
                        skill_receipt_hash=None,
                        denial_code=denial_code,
                        detail=detail[:1024],
                    )
                )
                continue

            if outcome.receipt.execution_status == "succeeded":
                status = "succeeded"
                managed_exec = True
                denial_code = "none"
                skill_receipt_id = outcome.receipt.receipt_id
                skill_receipt_hash = outcome.receipt_hash
                detail = (
                    f"managed deterministic family skill executed once for {family.value}"
                )
            else:
                status = "failed"
                managed_exec = False
                denial_code = "execution_failed"
                skill_receipt_id = outcome.receipt.receipt_id
                skill_receipt_hash = outcome.receipt_hash
                err = (
                    outcome.receipt.error.message
                    if outcome.receipt.error is not None
                    else outcome.receipt.execution_status
                )
                detail = f"managed family execution failed: {err}"

            steps.append(
                FourSkillsGenericManagedExecutionStep(
                    family=family,
                    ordinal=ordinal,
                    managed_skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    package_fingerprint=package_fingerprint,
                    status=status,
                    managed_skill_execution=managed_exec,
                    raw_skill_md_execution=False,
                    skill_receipt_id=skill_receipt_id,
                    skill_receipt_hash=skill_receipt_hash,
                    denial_code=denial_code,
                    detail=detail[:1024],
                )
            )

        succeeded = sum(1 for item in steps if item.status == "succeeded")
        failed = sum(1 for item in steps if item.status == "failed")
        if succeeded == 4 and failed == 0:
            status_value = "complete"
        elif succeeded == 0:
            status_value = "failed"
        else:
            status_value = "incomplete"
        managed_skill_execution = any(item.managed_skill_execution for item in steps)
        four_once = succeeded == 4 and failed == 0 and managed_skill_execution
        executed_at = _utcnow()
        material_for_id = {
            "batch_id": current.batch_id,
            "batch_hash": current.batch_hash,
            "probe_id": probe.probe_id,
            "probe_hash": probe.probe_hash,
            "project_id": request.project_id,
            "project_revision": binding.revision,
            "steps": [
                {
                    "family": item.family.value,
                    "status": item.status,
                    "skill_receipt_id": item.skill_receipt_id,
                }
                for item in steps
            ],
        }
        execution_id = "four-skills-generic-managed-execution-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Four family-mapped managed deterministic skills executed exactly once; "
            "raw SKILL.md remains unexecuted and formal draft stays locked."
            if four_once
            else (
                "Generic managed execution recorded partial or failed family steps; "
                "raw SKILL.md remains unexecuted and formal draft stays locked."
            )
        )
        temp = FourSkillsGenericManagedExecutionReceipt.model_construct(
            schema_id="zhimeng.four-skills-generic-managed-execution-receipt.v1",
            execution_id=execution_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            probe_id=probe.probe_id,
            probe_hash=probe.probe_hash,
            project_id=request.project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or "",
            status=status_value,
            steps=steps,
            step_count=len(steps),
            succeeded_step_count=succeeded,
            failed_step_count=failed,
            managed_skill_execution=managed_skill_execution,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=four_once,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-generic-managed-execution-1",
            detail=detail,
            executed_at=executed_at,
            execution_hash="0" * 64,
        )
        execution_hash = four_skills_generic_managed_execution_receipt_hash(temp)
        receipt = FourSkillsGenericManagedExecutionReceipt(
            execution_id=execution_id,
            batch_id=current.batch_id,
            batch_hash=current.batch_hash,
            probe_id=probe.probe_id,
            probe_hash=probe.probe_hash,
            project_id=request.project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or "",
            status=status_value,
            steps=steps,
            step_count=len(steps),
            succeeded_step_count=succeeded,
            failed_step_count=failed,
            managed_skill_execution=managed_skill_execution,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=four_once,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-four-skills-generic-managed-execution-1",
            detail=detail,
            executed_at=executed_at,
            execution_hash=execution_hash,
        )
        _atomic_write_json(
            self._generic_managed_execution_path(current.batch_id),
            receipt.model_dump(mode="json", by_alias=True),
        )
        return FourSkillsGenericManagedExecutionResponse(receipt=receipt)

    def get_generic_managed_execution(
        self, batch_id: str
    ) -> FourSkillsGenericManagedExecutionResponse:
        receipt = self._read_generic_managed_execution(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills generic managed execution was not found"
            )
        return FourSkillsGenericManagedExecutionResponse(receipt=receipt)

    def _generic_managed_execution_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "generic-managed-execution.json"

    def _read_generic_managed_execution(
        self, batch_id: str
    ) -> FourSkillsGenericManagedExecutionReceipt | None:
        path = self._generic_managed_execution_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsGenericManagedExecutionReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def get_instruction_chain_handoff(
        self, batch_id: str
    ) -> FourSkillsInstructionChainHandoffResponse:
        receipt = self._read_instruction_chain_handoff(batch_id)
        if receipt is None:
            raise FourSkillsRawAdaptationNotFound(
                "four skills instruction chain handoff was not found"
            )
        return FourSkillsInstructionChainHandoffResponse(receipt=receipt)

    def _instruction_chain_handoff_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "instruction-chain-handoff.json"

    def _read_instruction_chain_handoff(
        self, batch_id: str
    ) -> FourSkillsInstructionChainHandoffReceipt | None:
        path = self._instruction_chain_handoff_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsInstructionChainHandoffReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _workflow_orchestration_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "workflow-orchestration.json"

    def _read_workflow_orchestration(
        self, batch_id: str
    ) -> FourSkillsWorkflowOrchestrationReceipt | None:
        path = self._workflow_orchestration_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsWorkflowOrchestrationReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _workflow_binding_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "workflow-binding.json"

    def _read_workflow_binding(self, batch_id: str) -> FourSkillsWorkflowBindingReceipt | None:
        path = self._workflow_binding_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsWorkflowBindingReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _build_receipt(
        self,
        *,
        batch_id: str,
        status: FourSkillsRawAdaptationStatus,
        members: list[FourSkillsRawMemberRecord],
        detail: str,
    ) -> FourSkillsRawAdaptationReceipt:
        all_present = all(item.present for item in members)
        all_linked = all(item.linked for item in members) and all_present
        ready = (
            status is FourSkillsRawAdaptationStatus.MATERIALIZED
            and all_present
            and all_linked
        )
        updated_at = _utcnow()
        provisional = FourSkillsRawAdaptationReceipt.model_construct(
            schema_id="zhimeng.four-skills-raw-adaptation-receipt.v1",
            batch_id=batch_id,
            status=status,
            members=members,
            member_count=len(members),
            all_present=all_present,
            all_linked=all_linked,
            authorization_input_ready=ready,
            authorization_unlock=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            four_skills_generic_exactly_once=False,
            raw_skill_md_execution=False,
            policy_version="local-v1-four-skills-raw-adaptation-1",
            detail=detail,
            updated_at=updated_at,
            batch_hash="0" * 64,
        )
        batch_hash = four_skills_raw_adaptation_receipt_hash(provisional)
        return FourSkillsRawAdaptationReceipt(
            batch_id=batch_id,
            status=status,
            members=members,
            member_count=len(members),
            all_present=all_present,
            all_linked=all_linked,
            authorization_input_ready=ready,
            authorization_unlock=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            four_skills_generic_exactly_once=False,
            raw_skill_md_execution=False,
            policy_version="local-v1-four-skills-raw-adaptation-1",
            detail=detail,
            updated_at=updated_at,
            batch_hash=batch_hash,
        )

    def _persist(
        self,
        receipt: FourSkillsRawAdaptationReceipt,
        *,
        history: list[FourSkillsRawAdaptationReceipt],
    ) -> FourSkillsRawAdaptationResponse:
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(self._receipt_path(receipt.batch_id), payload)
        next_history = [
            item for item in history if item.batch_hash != receipt.batch_hash
        ]
        next_history.append(receipt)
        next_history = next_history[-32:]
        _atomic_write_json(
            self._history_path(receipt.batch_id),
            [item.model_dump(mode="json", by_alias=True) for item in next_history],
        )
        return FourSkillsRawAdaptationResponse(receipt=receipt, history=next_history)

    def _batch_id(self, members: list[FourSkillsRawMemberRecord]) -> str:
        material = {
            "schema": "zhimeng.four-skills-raw-adaptation-id.v1",
            "members": [
                {
                    "family": item.family.value,
                    "source_path": item.source_path,
                    "plan_id": item.plan_id,
                    "plan_hash": item.plan_hash,
                }
                for item in members
            ],
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"four-skills-raw-adaptation-{digest[:32]}"

    def _receipt_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "receipt.json"

    def _history_path(self, batch_id: str) -> Path:
        return self.state_root / batch_id / "history.json"

    def _read_receipt(self, batch_id: str) -> FourSkillsRawAdaptationReceipt | None:
        path = self._receipt_path(batch_id)
        if not path.is_file():
            return None
        return FourSkillsRawAdaptationReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _read_history(self, batch_id: str) -> list[FourSkillsRawAdaptationReceipt]:
        path = self._history_path(batch_id)
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [FourSkillsRawAdaptationReceipt.model_validate(item) for item in payload]


__all__ = [
    "FourSkillsRawAdaptationConflict",
    "FourSkillsRawAdaptationError",
    "FourSkillsRawAdaptationInvalid",
    "FourSkillsRawAdaptationNotFound",
    "FourSkillsRawAdaptationService",
    "FourSkillsRawAdaptationUnavailable",
]
