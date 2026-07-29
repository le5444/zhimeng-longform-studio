from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from zhimeng_runtime.adjacent_chain_models import (
    AdjacentChainApproval,
    AdjacentChainDecisionRequest,
    AdjacentChainPermissions,
    AdjacentChainPrepareRequest,
    AdjacentChainProjectRef,
    AdjacentChainResponse,
    AdjacentChainRiskApprovalToken,
    AdjacentChainRiskDossier,
    AdjacentChainRoute,
    AdjacentChainRunRecord,
    AdjacentChainSkillRisk,
    AdjacentChainStep,
    AdjacentWorkflowCheckpoint,
    AdjacentWorkflowPlan,
    AdjacentWorkflowPlanStep,
)
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.managed_skill_service import ManagedSkillError
from zhimeng_runtime.workflow_models import build_workflow_step
from zhimeng_runtime.context_handoff_models import (
    ContextHandoffDecisionRequest,
    ContextHandoffPrepareRequest,
)
from zhimeng_runtime.context_handoff_service import (
    ContextHandoffConflict,
    ContextHandoffError,
    ContextHandoffInvalid,
    ContextHandoffNotFound,
    ContextHandoffService,
    ContextHandoffUnavailable,
)
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.skill_handoff_models import SkillHandoffDecisionRequest, SkillHandoffPrepareRequest
from zhimeng_runtime.skill_handoff_service import (
    SkillHandoffConflict,
    SkillHandoffError,
    SkillHandoffInvalid,
    SkillHandoffNotFound,
    SkillHandoffService,
    SkillHandoffUnavailable,
)

CHAIN_KEY = "novel-distillation-kb-to-context"
STEP1_ID = "o2d-kb-handoff"
STEP2_ID = "o2e-context-handoff"
O2D_SKILL_ID = "novel-distillation.kb-handoff"
O2E_SKILL_ID = "novel-kb.context-handoff"
O2D_INPUT_CONTRACT = "https://zhimeng.local/contracts/novel-kb-handoff-request.schema.json"
O2D_OUTPUT_CONTRACT = "https://zhimeng.local/contracts/novel-kb-handoff-result.schema.json"
O2E_INPUT_CONTRACT = "https://zhimeng.local/contracts/novel-kb-context-handoff-request.schema.json"
O2E_OUTPUT_CONTRACT = "https://zhimeng.local/contracts/novel-kb-context-handoff-result.schema.json"


class AdjacentChainError(RuntimeError):
    code = "adjacent_chain_error"
    status_code = 409
    retryable = False

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class AdjacentChainNotFound(AdjacentChainError):
    code = "adjacent_chain_not_found"
    status_code = 404


class AdjacentChainUnavailable(AdjacentChainError):
    code = "adjacent_chain_unavailable"
    status_code = 503
    retryable = True


class AdjacentChainInvalid(AdjacentChainError):
    code = "adjacent_chain_invalid"
    status_code = 422


class AdjacentChainConflict(AdjacentChainError):
    code = "adjacent_chain_conflict"
    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class AdjacentChainService:
    """Author-visible adjacent skill chain: O2-D then O2-E with one risk approval.

    This is a fixed two-step chain over existing managed handoffs. It does not
    claim generic four-skills exactly-once orchestration.
    """

    def __init__(
        self,
        store: SqliteStore,
        skill_handoff_service: SkillHandoffService,
        context_handoff_service: ContextHandoffService,
        *,
        runtime_state_root: Path,
    ) -> None:
        self.store = store
        self.skill_handoff_service = skill_handoff_service
        self.context_handoff_service = context_handoff_service
        self.chain_root = Path(runtime_state_root) / "adjacent-chains"
        self.chain_root.mkdir(parents=True, exist_ok=True)

    def get(self, project_id: str) -> AdjacentChainResponse:
        record = self._latest_record(project_id)
        if record is None:
            binding = self._require_binding(project_id)
            now = _utcnow()
            record = AdjacentChainRunRecord(
                chain_id=_digest_id("adjacent-chain-", project_id, CHAIN_KEY, "awaiting"),
                chain_key=CHAIN_KEY,
                status="awaiting_upstream",
                project=AdjacentChainProjectRef(
                    project_id=project_id,
                    revision=binding.revision,
                    fingerprint=binding.fingerprint,
                ),
                route=AdjacentChainRoute(
                    profile="chapter",
                    stage="draft",
                    chapter_id="CH001",
                    task_intent="把 O2-D distillation_index 连续装配成写前 Context Pack；不写正文。",
                ),
                steps=self._empty_steps(),
                approval=AdjacentChainApproval(approved=False),
                permissions=self._permissions(),
                formal_draft_allowed=False,
                current_step_ordinal=0,
                created_at=now,
                updated_at=now,
            )
        return AdjacentChainResponse(record=record)

    def prepare(
        self,
        project_id: str,
        request: AdjacentChainPrepareRequest,
    ) -> AdjacentChainResponse:
        binding = self._require_fresh_binding(
            project_id,
            request.expected_project_revision,
            request.expected_project_fingerprint,
        )
        try:
            skill_prepared = self.skill_handoff_service.prepare(
                project_id,
                SkillHandoffPrepareRequest(
                    expected_project_revision=binding.revision,
                    expected_project_fingerprint=binding.fingerprint,
                    source_import_id=request.source_import_id,
                ),
            ).record
        except SkillHandoffInvalid as exc:
            raise AdjacentChainInvalid(str(exc)) from exc
        except SkillHandoffUnavailable as exc:
            raise AdjacentChainUnavailable(str(exc)) from exc
        except SkillHandoffError as exc:
            raise AdjacentChainConflict(str(exc)) from exc

        chain_id = _digest_id(
            "adjacent-chain-",
            project_id,
            CHAIN_KEY,
            skill_prepared.handoff_id,
            skill_prepared.upstream.formalization_receipt_hash if skill_prepared.upstream else "none",
        )
        now = _utcnow()
        steps = (
            AdjacentChainStep(
                step_id=STEP1_ID,
                ordinal=1,
                title="O2-D distillation -> KB handoff",
                skill_id="novel-distillation.kb-handoff",
                capability="import-distillation-index",
                status="prepared",
                child_id=skill_prepared.handoff_id,
            ),
            AdjacentChainStep(
                step_id=STEP2_ID,
                ordinal=2,
                title="O2-E KB index -> Context Pack",
                skill_id="novel-kb.context-handoff",
                capability="assemble-context",
                status="pending",
            ),
        )
        risk_dossier = self._build_risk_dossier(project_id, permissions=self._permissions())
        record = AdjacentChainRunRecord(
            chain_id=chain_id,
            chain_key=CHAIN_KEY,
            status="prepared",
            project=AdjacentChainProjectRef(
                project_id=project_id,
                revision=binding.revision,
                fingerprint=binding.fingerprint,
            ),
            route=AdjacentChainRoute(
                profile=request.profile,
                stage=request.stage,
                chapter_id=request.chapter_id,
                task_intent=request.task_intent,
            ),
            steps=steps,
            approval=AdjacentChainApproval(approved=False),
            permissions=self._permissions(),
            risk_dossier=risk_dossier,
            risk_approval_token=None,
            formal_draft_allowed=False,
            source_import_id=skill_prepared.upstream.source_import_id if skill_prepared.upstream else None,
            skill_handoff_id=skill_prepared.handoff_id,
            current_step_ordinal=1,
            created_at=now,
            updated_at=now,
        )
        self._save_record(record)
        return AdjacentChainResponse(record=record)

    def approve(
        self,
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status in {"disabled", "running", "succeeded"}:
            raise AdjacentChainConflict("当前相邻链状态不能批准。")
        self._assert_not_stale(record)
        if not record.skill_handoff_id:
            raise AdjacentChainInvalid("相邻链缺少 O2-D handoff，需要重新准备。")
        try:
            self.skill_handoff_service.approve(
                project_id,
                SkillHandoffDecisionRequest(expected_handoff_id=record.skill_handoff_id),
            )
        except SkillHandoffError as exc:
            raise AdjacentChainConflict(str(exc)) from exc

        now = _utcnow()
        step1, step2 = record.steps
        step1 = step1.model_copy(update={"status": "approved"})
        dossier = record.risk_dossier or self._build_risk_dossier(
            project_id,
            permissions=record.permissions,
        )
        self._assert_risk_dossier_current(project_id, dossier)
        token = self._mint_risk_token(record=record, dossier=dossier, approved_at=now)
        workflow_plan = self._compile_adjacent_workflow_plan(
            record=record,
            dossier=dossier,
            token=token,
            created_at=now,
        )
        record = record.model_copy(
            update={
                "status": "approved",
                "approval": AdjacentChainApproval(
                    approved=True,
                    approved_by="author",
                    approved_at=now,
                    risk_acknowledged=True,
                ),
                "risk_dossier": dossier,
                "risk_approval_token": token,
                "workflow_plan": workflow_plan,
                "workflow_checkpoints": (),
                "steps": (step1, step2),
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
        )
        self._save_record(record)
        return AdjacentChainResponse(record=record)

    def reject(
        self,
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status in {"running", "succeeded"}:
            raise AdjacentChainConflict("运行中或已成功的相邻链不能改成拒绝。")
        if record.skill_handoff_id and record.status not in {"failed"}:
            try:
                self.skill_handoff_service.reject(
                    project_id,
                    SkillHandoffDecisionRequest(expected_handoff_id=record.skill_handoff_id),
                )
            except SkillHandoffError:
                pass
        record = record.model_copy(
            update={
                "status": "rejected",
                "approval": AdjacentChainApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._save_record(record)
        return AdjacentChainResponse(record=record)

    def disable(
        self,
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status == "running":
            raise AdjacentChainConflict("运行中的相邻链不能禁用。")
        if record.skill_handoff_id:
            try:
                self.skill_handoff_service.disable(
                    project_id,
                    SkillHandoffDecisionRequest(expected_handoff_id=record.skill_handoff_id),
                )
            except SkillHandoffError:
                pass
        if record.context_handoff_id:
            try:
                self.context_handoff_service.disable(
                    project_id,
                    ContextHandoffDecisionRequest(
                        expected_context_handoff_id=record.context_handoff_id
                    ),
                )
            except ContextHandoffError:
                pass
        record = record.model_copy(
            update={
                "status": "disabled",
                "approval": AdjacentChainApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._save_record(record)
        return AdjacentChainResponse(record=record)

    async def run(
        self,
        project_id: str,
        request: AdjacentChainDecisionRequest,
    ) -> AdjacentChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status == "succeeded":
            return AdjacentChainResponse(record=record)
        if record.status == "disabled":
            raise AdjacentChainConflict("作者已禁用该相邻链，不能运行。")
        if record.status != "approved" or not record.approval.approved or not record.approval.risk_acknowledged:
            raise AdjacentChainConflict("必须先由作者批准并确认风险后才能运行相邻 Skill 链。")
        if not record.skill_handoff_id:
            raise AdjacentChainInvalid("相邻链缺少 O2-D handoff。")
        self._assert_not_stale(record)
        token = self._require_usable_risk_token(record)
        if record.risk_dossier is not None:
            self._assert_risk_dossier_current(project_id, record.risk_dossier)

        step1, step2 = record.steps
        now = _utcnow()
        consumed = token.model_copy(update={"consumed": True})
        record = record.model_copy(
            update={
                "status": "running",
                "current_step_ordinal": 1,
                "risk_approval_token": consumed,
                "steps": (
                    step1.model_copy(update={"status": "running"}),
                    step2,
                ),
                "updated_at": now,
            }
        )
        self._save_record(record)

        try:
            skill_result = await self.skill_handoff_service.run(
                project_id,
                SkillHandoffDecisionRequest(expected_handoff_id=record.skill_handoff_id),
            )
            skill_record = skill_result.record
            if skill_record.status != "succeeded":
                raise AdjacentChainConflict(
                    skill_record.error_message or "O2-D 相邻交接未成功。"
                )
            step1 = step1.model_copy(
                update={
                    "status": "succeeded",
                    "child_id": skill_record.handoff_id,
                    "result_hash": skill_record.result_hash,
                    "error_code": None,
                    "error_message": None,
                }
            )
            checkpoint1 = self._build_workflow_checkpoint(
                record=record,
                step_ordinal=1,
                child_handoff_id=skill_record.handoff_id,
                skill_receipt_id=skill_record.skill_receipt_id,
                skill_receipt_hash=skill_record.skill_receipt_hash,
                result_hash=skill_record.result_hash,
                previous_checkpoint_id=None,
                created_at=_utcnow(),
            )
            record = record.model_copy(
                update={
                    "skill_handoff_id": skill_record.handoff_id,
                    "source_import_id": (
                        skill_record.upstream.source_import_id if skill_record.upstream else record.source_import_id
                    ),
                    "steps": (step1, step2.model_copy(update={"status": "prepared"})),
                    "workflow_checkpoints": (checkpoint1,),
                    "current_step_ordinal": 2,
                    "updated_at": _utcnow(),
                }
            )
            self._save_record(record)

            binding = self._require_binding(project_id)
            context_prepared = self.context_handoff_service.prepare(
                project_id,
                ContextHandoffPrepareRequest(
                    expected_project_revision=binding.revision,
                    expected_project_fingerprint=binding.fingerprint,
                    profile=record.route.profile,
                    stage=record.route.stage,
                    chapter_id=record.route.chapter_id,
                    task_intent=record.route.task_intent,
                ),
            ).record
            step2 = step2.model_copy(
                update={
                    "status": "running",
                    "child_id": context_prepared.context_handoff_id,
                }
            )
            record = record.model_copy(
                update={
                    "context_handoff_id": context_prepared.context_handoff_id,
                    "steps": (step1, step2),
                    "updated_at": _utcnow(),
                }
            )
            self._save_record(record)

            self.context_handoff_service.approve(
                project_id,
                ContextHandoffDecisionRequest(
                    expected_context_handoff_id=context_prepared.context_handoff_id
                ),
            )
            context_result = await self.context_handoff_service.run(
                project_id,
                ContextHandoffDecisionRequest(
                    expected_context_handoff_id=context_prepared.context_handoff_id
                ),
            )
            context_record = context_result.record
            if context_record.status != "succeeded":
                raise AdjacentChainConflict(
                    context_record.error_message or "O2-E Context handoff 未成功。"
                )

            step2 = step2.model_copy(
                update={
                    "status": "succeeded",
                    "child_id": context_record.context_handoff_id,
                    "result_hash": context_record.result_hash,
                    "error_code": None,
                    "error_message": None,
                }
            )
            previous = record.workflow_checkpoints[0] if record.workflow_checkpoints else None
            checkpoint2 = self._build_workflow_checkpoint(
                record=record,
                step_ordinal=2,
                child_handoff_id=context_record.context_handoff_id,
                skill_receipt_id=context_record.skill_receipt_id,
                skill_receipt_hash=context_record.skill_receipt_hash,
                result_hash=context_record.result_hash,
                previous_checkpoint_id=previous.checkpoint_id if previous else None,
                created_at=_utcnow(),
            )
            checkpoints = (
                (previous, checkpoint2) if previous is not None else (checkpoint2,)
            )
            record = record.model_copy(
                update={
                    "status": "succeeded",
                    "context_handoff_id": context_record.context_handoff_id,
                    "context_pack_id": context_record.context_pack_id,
                    "context_pack_hash": context_record.context_pack_hash,
                    "route_receipt_id": context_record.route_receipt_id,
                    "route_receipt_hash": context_record.route_receipt_hash,
                    "steps": (step1, step2),
                    "workflow_checkpoints": checkpoints,
                    "current_step_ordinal": 2,
                    "error_code": None,
                    "error_message": None,
                    "updated_at": _utcnow(),
                }
            )
            self._save_record(record)
            return AdjacentChainResponse(record=record)
        except (
            SkillHandoffError,
            ContextHandoffError,
            AdjacentChainError,
        ) as exc:
            failed = self._failed_record(record, type(exc).__name__.lower(), str(exc))
            self._save_record(failed)
            if isinstance(exc, AdjacentChainError):
                raise
            if isinstance(exc, (SkillHandoffUnavailable, ContextHandoffUnavailable)):
                raise AdjacentChainUnavailable(str(exc)) from exc
            if isinstance(exc, (SkillHandoffInvalid, ContextHandoffInvalid)):
                raise AdjacentChainInvalid(str(exc)) from exc
            if isinstance(exc, (SkillHandoffNotFound, ContextHandoffNotFound)):
                raise AdjacentChainNotFound(str(exc)) from exc
            raise AdjacentChainConflict(str(exc)) from exc



    def _compile_adjacent_workflow_plan(
        self,
        *,
        record: AdjacentChainRunRecord,
        dossier: AdjacentChainRiskDossier,
        token: AdjacentChainRiskApprovalToken,
        created_at: datetime,
    ) -> AdjacentWorkflowPlan:
        plan_steps: list[AdjacentWorkflowPlanStep] = []
        for index, skill in enumerate(dossier.skills, start=1):
            built = build_workflow_step(
                ordinal=index,
                skill_id=skill.skill_id,
                expected_skill_version_id=skill.skill_version_id,
                expected_skill_version=skill.skill_version,
                expected_manifest_fingerprint=skill.manifest_fingerprint,
                expected_package_fingerprint=skill.package_fingerprint,
                input_contract=skill.input_contract,
                output_contract=skill.output_contract,
                input_builder_id=f"zhimeng.adjacent-input.{skill.step_id}",
                input_builder_version="1",
                continue_on_domain_status=("healthy", "degraded", "blocked"),
            )
            plan_steps.append(
                AdjacentWorkflowPlanStep(
                    workflow_step_id=built.step_id,
                    step_hash=built.step_hash,
                    ordinal=built.ordinal,
                    runner="managed_skill",
                    skill_id=built.skill_id,
                    expected_skill_version_id=built.expected_skill_version_id,
                    expected_skill_version=built.expected_skill_version,
                    expected_manifest_fingerprint=built.expected_manifest_fingerprint,
                    expected_package_fingerprint=built.expected_package_fingerprint,
                    input_contract=built.input_contract,
                    output_contract=built.output_contract,
                    input_builder_id=built.input_builder_id,
                    input_builder_version=built.input_builder_version,
                    formal_draft_allowed=False,
                )
            )
        if len(plan_steps) != 2:
            raise AdjacentChainInvalid("adjacent workflow plan requires exactly two steps")
        provisional = AdjacentWorkflowPlan(
            workflow_plan_id=_digest_id(
                "adjacent-workflow-plan-",
                record.chain_id,
                token.token_id,
                plan_steps[0].workflow_step_id,
                plan_steps[1].workflow_step_id,
            ),
            chain_id=record.chain_id,
            project_id=record.project.project_id,
            project_revision=record.project.revision,
            project_fingerprint=record.project.fingerprint,
            risk_token_id=token.token_id,
            risk_token_hash=token.token_hash,
            steps=(plan_steps[0], plan_steps[1]),
            formal_draft_allowed=False,
            created_at=created_at,
            plan_hash="0" * 64,
        )
        material = provisional.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        return provisional.model_copy(update={"plan_hash": canonical_json_hash(material)})

    def _build_workflow_checkpoint(
        self,
        *,
        record: AdjacentChainRunRecord,
        step_ordinal: int,
        child_handoff_id: str | None,
        skill_receipt_id: str | None,
        skill_receipt_hash: str | None,
        result_hash: str | None,
        previous_checkpoint_id: str | None,
        created_at: datetime,
    ) -> AdjacentWorkflowCheckpoint:
        plan = record.workflow_plan
        if plan is None:
            raise AdjacentChainConflict("相邻链缺少 workflow plan，不能写 checkpoint。")
        step = next((item for item in plan.steps if item.ordinal == step_ordinal), None)
        if step is None:
            raise AdjacentChainConflict(f"workflow plan 缺少 step {step_ordinal}")
        provisional = AdjacentWorkflowCheckpoint(
            checkpoint_id=_digest_id(
                "adjacent-workflow-checkpoint-",
                plan.workflow_plan_id,
                step.workflow_step_id,
                child_handoff_id or "none",
                skill_receipt_id or "none",
                result_hash or "none",
            ),
            workflow_plan_id=plan.workflow_plan_id,
            plan_hash=plan.plan_hash,
            chain_id=record.chain_id,
            workflow_step_id=step.workflow_step_id,
            step_hash=step.step_hash,
            step_ordinal=step.ordinal,
            skill_id=step.skill_id,
            expected_skill_version_id=step.expected_skill_version_id,
            expected_package_fingerprint=step.expected_package_fingerprint,
            previous_checkpoint_id=previous_checkpoint_id,
            child_handoff_id=child_handoff_id,
            skill_receipt_id=skill_receipt_id,
            skill_receipt_hash=skill_receipt_hash,
            result_hash=result_hash,
            execution_status="succeeded",
            formal_draft_allowed=False,
            created_at=created_at,
            checkpoint_hash="0" * 64,
        )
        material = provisional.model_dump(
            mode="json",
            by_alias=True,
            exclude={"checkpoint_hash"},
        )
        return provisional.model_copy(
            update={"checkpoint_hash": canonical_json_hash(material)}
        )

    def _build_risk_dossier(
        self,
        project_id: str,
        *,
        permissions: AdjacentChainPermissions,
    ) -> AdjacentChainRiskDossier:
        o2d = self._skill_risk(
            project_id,
            step_id=STEP1_ID,
            skill_id=O2D_SKILL_ID,
            capability="import-distillation-index",
            input_contract=O2D_INPUT_CONTRACT,
            output_contract=O2D_OUTPUT_CONTRACT,
        )
        o2e = self._skill_risk(
            project_id,
            step_id=STEP2_ID,
            skill_id=O2E_SKILL_ID,
            capability="assemble-context",
            input_contract=O2E_INPUT_CONTRACT,
            output_contract=O2E_OUTPUT_CONTRACT,
        )
        summary = (
            "一次作者风险批准覆盖固定两步相邻链："
            f"{o2d.skill_id}@{o2d.skill_version} -> {o2e.skill_id}@{o2e.skill_version}。"
            "只读项目快照与 staging，可写 staging 与 Context store；无网络、无 secrets；"
            "formal_draft_allowed 保持 false，不写正式正文，也不声称通用四 Skills exactly-once。"
        )
        return AdjacentChainRiskDossier(
            formal_draft_allowed=False,
            network=False,
            secrets=(),
            permissions=permissions,
            skills=(o2d, o2e),
            risk_summary=summary,
        )

    def _skill_risk(
        self,
        project_id: str,
        *,
        step_id: str,
        skill_id: str,
        capability: str,
        input_contract: str,
        output_contract: str,
    ) -> AdjacentChainSkillRisk:
        executor = self.skill_handoff_service.skill_executor
        if executor is None:
            raise AdjacentChainUnavailable("Managed Skill executor is unavailable")
        try:
            skill = executor.active_skill_declaration(project_id, skill_id)
        except ManagedSkillError as exc:
            raise AdjacentChainConflict(
                f"相邻 Skill 未启用或不可路由：{skill_id}"
            ) from exc
        if skill.manifest.input_schema != input_contract:
            raise AdjacentChainConflict(
                f"{skill_id} input contract drifted from adjacent-chain binding"
            )
        if skill.manifest.output_schema != output_contract:
            raise AdjacentChainConflict(
                f"{skill_id} output contract drifted from adjacent-chain binding"
            )
        return AdjacentChainSkillRisk(
            step_id=step_id,
            skill_id=skill_id,
            capability=capability,
            skill_version_id=skill.skill_version_id,
            skill_version=skill.manifest.version,
            manifest_fingerprint=skill.manifest_fingerprint,
            package_fingerprint=skill.package_fingerprint,
            effective_scope="project",
            input_contract=input_contract,
            output_contract=output_contract,
        )

    def _mint_risk_token(
        self,
        *,
        record: AdjacentChainRunRecord,
        dossier: AdjacentChainRiskDossier,
        approved_at: datetime,
    ) -> AdjacentChainRiskApprovalToken:
        token_id = _digest_id(
            "risk-token-",
            record.chain_id,
            record.project.revision,
            record.project.fingerprint,
            dossier.skills[0].package_fingerprint,
            dossier.skills[1].package_fingerprint,
            approved_at.isoformat(),
        )
        provisional = AdjacentChainRiskApprovalToken(
            token_id=token_id,
            chain_id=record.chain_id,
            project_id=record.project.project_id,
            project_revision=record.project.revision,
            project_fingerprint=record.project.fingerprint,
            dossier=dossier,
            approved_by="author",
            approved_at=approved_at,
            consumed=False,
            token_hash="0" * 64,
        )
        material = provisional.model_dump(mode="json", exclude={"token_hash"})
        token_hash = canonical_json_hash(material)
        return provisional.model_copy(update={"token_hash": token_hash})

    def _require_usable_risk_token(
        self,
        record: AdjacentChainRunRecord,
    ) -> AdjacentChainRiskApprovalToken:
        token = record.risk_approval_token
        if token is None:
            raise AdjacentChainConflict("相邻链缺少风险审批 token，需要重新批准。")
        if token.consumed:
            raise AdjacentChainConflict("风险审批 token 已消费，不能重复运行；请重新准备并批准。")
        if token.chain_id != record.chain_id:
            raise AdjacentChainConflict("风险审批 token 与相邻链不匹配。")
        if (
            token.project_id != record.project.project_id
            or token.project_revision != record.project.revision
            or token.project_fingerprint != record.project.fingerprint
        ):
            raise AdjacentChainConflict("风险审批 token 绑定的项目快照已陈旧。")
        material = token.model_dump(mode="json", exclude={"token_hash"})
        material["consumed"] = False
        expected = canonical_json_hash(material)
        if token.token_hash != expected:
            raise AdjacentChainConflict("风险审批 token 被篡改或哈希不匹配。")
        return token

    def _assert_risk_dossier_current(
        self,
        project_id: str,
        dossier: AdjacentChainRiskDossier,
    ) -> None:
        current = self._build_risk_dossier(project_id, permissions=dossier.permissions)
        for expected, actual in zip(dossier.skills, current.skills, strict=True):
            if (
                expected.skill_version_id != actual.skill_version_id
                or expected.manifest_fingerprint != actual.manifest_fingerprint
                or expected.package_fingerprint != actual.package_fingerprint
                or expected.input_contract != actual.input_contract
                or expected.output_contract != actual.output_contract
            ):
                raise AdjacentChainConflict(
                    "相邻 Skill 版本/合同已变化，旧风险批准失效；请重新准备并重新批准。"
                )

    def _empty_steps(self) -> tuple[AdjacentChainStep, AdjacentChainStep]:
        return (
            AdjacentChainStep(
                step_id=STEP1_ID,
                ordinal=1,
                title="O2-D distillation -> KB handoff",
                skill_id="novel-distillation.kb-handoff",
                capability="import-distillation-index",
                status="pending",
            ),
            AdjacentChainStep(
                step_id=STEP2_ID,
                ordinal=2,
                title="O2-E KB index -> Context Pack",
                skill_id="novel-kb.context-handoff",
                capability="assemble-context",
                status="pending",
            ),
        )

    def _permissions(self) -> AdjacentChainPermissions:
        return AdjacentChainPermissions(
            read=("bound_project_snapshot", "run_staging"),
            write=("run_staging", "runtime_context_store"),
            network=False,
            secrets=(),
        )

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.store.get_project_binding(project_id)
        if binding is None:
            raise AdjacentChainNotFound(f"project binding not found: {project_id}")
        return binding

    def _require_fresh_binding(
        self,
        project_id: str,
        revision: int,
        fingerprint: str,
    ) -> ProjectBindingRecord:
        binding = self._require_binding(project_id)
        if binding.revision != revision or binding.fingerprint != fingerprint:
            raise AdjacentChainConflict("项目 revision/fingerprint 已变化，需要重新准备相邻链。")
        return binding

    def _assert_not_stale(self, record: AdjacentChainRunRecord) -> None:
        binding = self._require_binding(record.project.project_id)
        if (
            binding.revision != record.project.revision
            or binding.fingerprint != record.project.fingerprint
        ):
            raise AdjacentChainConflict("项目快照已变化，相邻链已陈旧，需要重新准备。")

    def _failed_record(
        self,
        record: AdjacentChainRunRecord,
        code: str,
        message: str,
    ) -> AdjacentChainRunRecord:
        step1, step2 = record.steps
        ordinal = record.current_step_ordinal or 1
        if ordinal <= 1:
            step1 = step1.model_copy(
                update={
                    "status": "failed",
                    "error_code": code[:128],
                    "error_message": message[:4096],
                }
            )
        else:
            step2 = step2.model_copy(
                update={
                    "status": "failed",
                    "error_code": code[:128],
                    "error_message": message[:4096],
                }
            )
        return record.model_copy(
            update={
                "status": "failed",
                "steps": (step1, step2),
                "error_code": code[:128],
                "error_message": message[:4096],
                "updated_at": _utcnow(),
            }
        )

    def _project_index_path(self, project_id: str) -> Path:
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
        return self.chain_root / f"project-{digest}.json"

    def _chain_path(self, chain_id: str) -> Path:
        return self.chain_root / f"{chain_id}.json"

    def _save_record(self, record: AdjacentChainRunRecord) -> None:
        payload = record.model_dump(mode="json", by_alias=True)
        path = self._chain_path(record.chain_id)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        index = {
            "project_id": record.project.project_id,
            "latest_chain_id": record.chain_id,
            "updated_at": record.updated_at.isoformat(),
        }
        self._project_index_path(record.project.project_id).write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _load_record(self, chain_id: str) -> AdjacentChainRunRecord | None:
        path = self._chain_path(chain_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AdjacentChainRunRecord.model_validate(payload)

    def _latest_record(self, project_id: str) -> AdjacentChainRunRecord | None:
        index_path = self._project_index_path(project_id)
        if not index_path.is_file():
            return None
        index = json.loads(index_path.read_text(encoding="utf-8"))
        chain_id = index.get("latest_chain_id")
        if not isinstance(chain_id, str):
            return None
        return self._load_record(chain_id)

    def _require_record(self, project_id: str, chain_id: str) -> AdjacentChainRunRecord:
        record = self._load_record(chain_id)
        if record is None or record.project.project_id != project_id:
            raise AdjacentChainNotFound("相邻 Skill 链不存在。")
        return record


def raise_adjacent_chain_error(exc: AdjacentChainError) -> None:
    raise ApiError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
        details=exc.details,
        retryable=exc.retryable,
    ) from exc


__all__ = [
    "AdjacentChainService",
    "AdjacentChainError",
    "raise_adjacent_chain_error",
]
