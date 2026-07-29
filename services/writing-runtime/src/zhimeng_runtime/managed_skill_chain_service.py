from __future__ import annotations

import hashlib
import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.managed_skill_chain_models import (
    ManagedSkillChainApproval,
    ManagedSkillChainCheckpoint,
    ManagedSkillChainDecisionRequest,
    ManagedSkillChainPermissions,
    ManagedSkillChainPlan,
    ManagedSkillChainPlanStep,
    ManagedSkillChainPrepareRequest,
    ManagedSkillChainProjectRef,
    ManagedSkillChainResponse,
    ManagedSkillChainRiskApprovalToken,
    ManagedSkillChainRiskDossier,
    ManagedSkillChainRunRecord,
    ManagedSkillChainSkillRisk,
    ManagedSkillChainStep,
)
from zhimeng_runtime.managed_skill_service import ManagedSkillError
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.workflow_models import build_workflow_step


INPUT_BUILDER_ID = "author-supplied-input"
INPUT_BUILDER_VERSION = "1"
MAX_INPUT_BYTES = 256 * 1024


class ManagedSkillChainError(RuntimeError):
    code = "managed_skill_chain_error"
    status_code = 409
    retryable = False

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class ManagedSkillChainNotFound(ManagedSkillChainError):
    code = "managed_skill_chain_not_found"
    status_code = 404


class ManagedSkillChainUnavailable(ManagedSkillChainError):
    code = "managed_skill_chain_unavailable"
    status_code = 503
    retryable = True


class ManagedSkillChainInvalid(ManagedSkillChainError):
    code = "managed_skill_chain_invalid"
    status_code = 422


class ManagedSkillChainConflict(ManagedSkillChainError):
    code = "managed_skill_chain_conflict"
    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

class ManagedSkillChainService:
    """Risk-approved N-step runner over active managed deterministic skills.

    This is not the original four-skills exactly-once orchestrator, and it never
    executes raw SKILL.md instruction packages.
    """

    def __init__(
        self,
        store: SqliteStore,
        skill_executor: SkillExecutor | None,
        *,
        runtime_state_root: Path,
    ) -> None:
        self.store = store
        self.skill_executor = skill_executor
        self.chain_root = Path(runtime_state_root) / "managed-skill-chains"
        self.chain_root.mkdir(parents=True, exist_ok=True)

    def get(self, project_id: str, chain_id: str) -> ManagedSkillChainResponse:
        return ManagedSkillChainResponse(record=self._require_record(project_id, chain_id))

    def prepare(
        self,
        project_id: str,
        request: ManagedSkillChainPrepareRequest,
    ) -> ManagedSkillChainResponse:
        if self.skill_executor is None:
            raise ManagedSkillChainUnavailable("Managed Skill executor is unavailable")
        binding = self._require_fresh_binding(
            project_id,
            request.expected_project_revision,
            request.expected_project_fingerprint,
        )
        now = _utcnow()
        runtime_steps: list[ManagedSkillChainStep] = []
        risk_skills: list[ManagedSkillChainSkillRisk] = []
        read_perms: set[str] = set()
        write_perms: set[str] = set()

        for ordinal, spec in enumerate(request.steps, start=1):
            skill = self._require_active_deterministic(project_id, spec.skill_id)
            input_hash = self._input_hash(spec.input_payload)
            if skill.manifest.permissions.network:
                raise ManagedSkillChainInvalid(
                    f"{spec.skill_id} 声明了 network 权限，通用 managed chain 拒绝。"
                )
            if skill.manifest.permissions.secrets:
                raise ManagedSkillChainInvalid(
                    f"{spec.skill_id} 声明了 secrets 权限，通用 managed chain 拒绝。"
                )
            read_perms.update(skill.manifest.permissions.read)
            write_perms.update(skill.manifest.permissions.write)
            if not skill.manifest.input_schema or not skill.manifest.output_schema:
                raise ManagedSkillChainInvalid(
                    f"{spec.skill_id} 缺少本地 input/output contract，不能进入 managed chain。"
                )
            declared_capabilities = tuple(skill.manifest.capabilities or ())
            if declared_capabilities and spec.capability not in declared_capabilities:
                raise ManagedSkillChainInvalid(
                    f"{spec.skill_id} 未声明 capability={spec.capability}"
                )
            risk_skills.append(
                ManagedSkillChainSkillRisk(
                    step_id=spec.step_id,
                    skill_id=spec.skill_id,
                    capability=spec.capability,
                    skill_version_id=skill.skill_version_id,
                    skill_version=skill.manifest.version,
                    manifest_fingerprint=skill.manifest_fingerprint,
                    package_fingerprint=skill.package_fingerprint,
                    effective_scope="project",
                    input_contract=skill.manifest.input_schema,
                    output_contract=skill.manifest.output_schema,
                )
            )
            runtime_steps.append(
                ManagedSkillChainStep(
                    step_id=spec.step_id,
                    ordinal=ordinal,
                    title=spec.title,
                    skill_id=spec.skill_id,
                    capability=spec.capability,
                    status="prepared",
                    input_payload=spec.input_payload,
                    input_hash=input_hash,
                )
            )

        permissions = ManagedSkillChainPermissions(
            read=tuple(sorted(read_perms)),
            write=tuple(sorted(write_perms)),
            network=False,
            secrets=(),
        )
        risk_dossier = ManagedSkillChainRiskDossier(
            formal_draft_allowed=False,
            network=False,
            secrets=(),
            permissions=permissions,
            skills=tuple(risk_skills),
            risk_summary=self._risk_summary(risk_skills),
        )
        chain_id = _digest_id(
            "managed-skill-chain-",
            project_id,
            request.chain_key,
            binding.revision,
            binding.fingerprint,
            *(skill.package_fingerprint for skill in risk_skills),
            *(step.input_hash for step in runtime_steps),
        )
        record = ManagedSkillChainRunRecord(
            chain_id=chain_id,
            chain_key=request.chain_key,
            status="prepared",
            project=ManagedSkillChainProjectRef(
                project_id=project_id,
                revision=binding.revision,
                fingerprint=binding.fingerprint,
                root_path=binding.root_path,
            ),
            steps=tuple(runtime_steps),
            approval=ManagedSkillChainApproval(approved=False),
            permissions=permissions,
            risk_dossier=risk_dossier,
            risk_approval_token=None,
            workflow_plan=None,
            workflow_checkpoints=(),
            formal_draft_allowed=False,
            current_step_ordinal=0,
            created_at=now,
            updated_at=now,
        )
        self._save_record(record)
        return ManagedSkillChainResponse(record=record)

    def approve(
        self,
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status in {"disabled", "running", "succeeded"}:
            raise ManagedSkillChainConflict("当前 managed skill chain 状态不能批准。")
        if record.status not in {"prepared", "approved", "failed", "rejected"}:
            raise ManagedSkillChainConflict("当前 managed skill chain 状态不能批准。")
        self._assert_not_stale(record)
        if record.risk_dossier is None:
            raise ManagedSkillChainInvalid("缺少风险档案，需要重新准备。")
        self._assert_risk_dossier_current(project_id, record.risk_dossier)
        now = _utcnow()
        token = self._mint_risk_token(
            record=record,
            dossier=record.risk_dossier,
            approved_at=now,
        )
        plan = self._compile_plan(
            record=record,
            dossier=record.risk_dossier,
            token=token,
            created_at=now,
        )
        steps = tuple(
            step.model_copy(update={"status": "approved"}) for step in record.steps
        )
        record = record.model_copy(
            update={
                "status": "approved",
                "approval": ManagedSkillChainApproval(
                    approved=True,
                    approved_by="author",
                    approved_at=now,
                    risk_acknowledged=True,
                ),
                "risk_approval_token": token,
                "workflow_plan": plan,
                "workflow_checkpoints": (),
                "steps": steps,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
        )
        self._save_record(record)
        return ManagedSkillChainResponse(record=record)

    def reject(
        self,
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status in {"running", "succeeded"}:
            raise ManagedSkillChainConflict("运行中或已成功的 managed skill chain 不能拒绝。")
        record = record.model_copy(
            update={
                "status": "rejected",
                "approval": ManagedSkillChainApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._save_record(record)
        return ManagedSkillChainResponse(record=record)

    def disable(
        self,
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status == "running":
            raise ManagedSkillChainConflict("运行中的 managed skill chain 不能禁用。")
        record = record.model_copy(
            update={
                "status": "disabled",
                "approval": ManagedSkillChainApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._save_record(record)
        return ManagedSkillChainResponse(record=record)

    async def run(
        self,
        project_id: str,
        request: ManagedSkillChainDecisionRequest,
    ) -> ManagedSkillChainResponse:
        if self.skill_executor is None:
            raise ManagedSkillChainUnavailable("Managed Skill executor is unavailable")
        record = self._require_record(project_id, request.expected_chain_id)
        if record.status == "succeeded":
            return ManagedSkillChainResponse(record=record)
        if record.status == "disabled":
            raise ManagedSkillChainConflict("作者已禁用该 managed skill chain，不能运行。")
        if (
            record.status != "approved"
            or not record.approval.approved
            or not record.approval.risk_acknowledged
        ):
            raise ManagedSkillChainConflict(
                "必须先由作者批准并确认风险后才能运行 managed skill chain。"
            )
        if record.workflow_plan is None or record.risk_dossier is None:
            raise ManagedSkillChainInvalid("缺少 workflow plan 或风险档案，需要重新批准。")
        self._assert_not_stale(record)
        token = self._require_usable_risk_token(record)
        self._assert_risk_dossier_current(project_id, record.risk_dossier)

        now = _utcnow()
        consumed = token.model_copy(update={"consumed": True})
        steps = list(record.steps)
        steps[0] = steps[0].model_copy(update={"status": "running"})
        record = record.model_copy(
            update={
                "status": "running",
                "current_step_ordinal": 1,
                "risk_approval_token": consumed,
                "steps": tuple(steps),
                "workflow_checkpoints": (),
                "updated_at": now,
            }
        )
        self._save_record(record)

        checkpoints: list[ManagedSkillChainCheckpoint] = []
        try:
            for index, step in enumerate(record.steps):
                ordinal = index + 1
                steps = list(record.steps)
                steps[index] = step.model_copy(update={"status": "running"})
                record = record.model_copy(
                    update={
                        "current_step_ordinal": ordinal,
                        "steps": tuple(steps),
                        "updated_at": _utcnow(),
                    }
                )
                self._save_record(record)

                workspace = self.chain_root / record.chain_id / f"step-{ordinal:02d}"
                snapshot = workspace / "project-snapshot"
                staging = workspace / "run-staging"
                self._reset_dir(workspace)
                self._materialize_project_snapshot(record.project.root_path, snapshot)
                staging.mkdir(parents=True, exist_ok=True)

                plan_step = record.workflow_plan.steps[index]
                if plan_step.input_hash != step.input_hash:
                    raise ManagedSkillChainConflict(
                        f"step {ordinal} input hash 与 plan 不一致。"
                    )
                tool_call_id = _digest_id(
                    "tool_",
                    record.chain_id,
                    plan_step.workflow_step_id,
                    step.input_hash or "",
                )
                outcome = await self.skill_executor.execute(
                    project_id=project_id,
                    project_revision=record.project.revision,
                    project_fingerprint=record.project.fingerprint,
                    run_id=record.workflow_plan.workflow_plan_id,
                    tool_call_id=tool_call_id,
                    skill_id=step.skill_id,
                    input_payload=step.input_payload,
                    project_snapshot=snapshot,
                    run_staging=staging,
                )
                result_hash = (
                    canonical_json_hash(outcome.result)
                    if outcome.result is not None
                    else None
                )
                domain_status = outcome.receipt.domain_status
                previous_id = checkpoints[-1].checkpoint_id if checkpoints else None
                if (
                    outcome.receipt.execution_status != "succeeded"
                    or domain_status == "blocked"
                ):
                    error_code = (
                        outcome.receipt.error.code
                        if outcome.receipt.error is not None
                        else "domain_blocked"
                    )
                    error_message = (
                        outcome.receipt.error.message
                        if outcome.receipt.error is not None
                        else "Skill returned a blocked domain result"
                    )
                    failed_step = step.model_copy(
                        update={
                            "status": "failed",
                            "skill_receipt_id": outcome.receipt.receipt_id,
                            "skill_receipt_hash": outcome.receipt_hash,
                            "result_hash": result_hash,
                            "domain_status": domain_status,
                            "error_code": error_code[:128],
                            "error_message": error_message[:4096],
                        }
                    )
                    steps = list(record.steps)
                    steps[index] = failed_step
                    checkpoint = self._build_checkpoint(
                        record=record,
                        step_ordinal=ordinal,
                        skill_receipt_id=outcome.receipt.receipt_id,
                        skill_receipt_hash=outcome.receipt_hash,
                        input_hash=step.input_hash,
                        result_hash=result_hash,
                        execution_status="failed",
                        domain_status=domain_status,
                        previous_checkpoint_id=previous_id,
                        created_at=_utcnow(),
                    )
                    checkpoints.append(checkpoint)
                    record = record.model_copy(
                        update={
                            "status": "failed",
                            "steps": tuple(steps),
                            "workflow_checkpoints": tuple(checkpoints),
                            "error_code": error_code[:128],
                            "error_message": error_message[:4096],
                            "updated_at": _utcnow(),
                        }
                    )
                    self._save_record(record)
                    raise ManagedSkillChainConflict(error_message)

                succeeded_step = step.model_copy(
                    update={
                        "status": "succeeded",
                        "skill_receipt_id": outcome.receipt.receipt_id,
                        "skill_receipt_hash": outcome.receipt_hash,
                        "result_hash": result_hash,
                        "domain_status": domain_status,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                steps = list(record.steps)
                steps[index] = succeeded_step
                checkpoint = self._build_checkpoint(
                    record=record,
                    step_ordinal=ordinal,
                    skill_receipt_id=outcome.receipt.receipt_id,
                    skill_receipt_hash=outcome.receipt_hash,
                    input_hash=step.input_hash,
                    result_hash=result_hash,
                    execution_status="succeeded",
                    domain_status=domain_status,
                    previous_checkpoint_id=previous_id,
                    created_at=_utcnow(),
                )
                checkpoints.append(checkpoint)
                record = record.model_copy(
                    update={
                        "steps": tuple(steps),
                        "workflow_checkpoints": tuple(checkpoints),
                        "updated_at": _utcnow(),
                    }
                )
                self._save_record(record)

            record = record.model_copy(
                update={
                    "status": "succeeded",
                    "current_step_ordinal": len(record.steps),
                    "error_code": None,
                    "error_message": None,
                    "updated_at": _utcnow(),
                }
            )
            self._save_record(record)
            return ManagedSkillChainResponse(record=record)
        except ManagedSkillChainError:
            raise
        except ManagedSkillError as exc:
            failed = self._failed_record(record, exc.code, str(exc))
            self._save_record(failed)
            raise ManagedSkillChainConflict(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            failed = self._failed_record(record, "execution_failed", str(exc))
            self._save_record(failed)
            raise ManagedSkillChainConflict(str(exc)) from exc

    def _require_active_deterministic(self, project_id: str, skill_id: str):
        assert self.skill_executor is not None
        try:
            skill = self.skill_executor.active_skill_declaration(project_id, skill_id)
        except ManagedSkillError as exc:
            raise ManagedSkillChainConflict(
                f"managed skill 未启用或不可路由：{skill_id}"
            ) from exc
        if skill.manifest.kind != "deterministic_tool":
            raise ManagedSkillChainInvalid(
                f"{skill_id} 不是 deterministic_tool；raw/instruction Skill 不能进入 managed chain。"
            )
        return skill

    def _compile_plan(
        self,
        *,
        record: ManagedSkillChainRunRecord,
        dossier: ManagedSkillChainRiskDossier,
        token: ManagedSkillChainRiskApprovalToken,
        created_at: datetime,
    ) -> ManagedSkillChainPlan:
        plan_steps: list[ManagedSkillChainPlanStep] = []
        for skill_risk, runtime_step in zip(dossier.skills, record.steps, strict=True):
            built = build_workflow_step(
                ordinal=runtime_step.ordinal,
                skill_id=skill_risk.skill_id,
                expected_skill_version_id=skill_risk.skill_version_id,
                expected_skill_version=skill_risk.skill_version,
                expected_manifest_fingerprint=skill_risk.manifest_fingerprint,
                expected_package_fingerprint=skill_risk.package_fingerprint,
                input_contract=skill_risk.input_contract,
                output_contract=skill_risk.output_contract,
                input_builder_id=INPUT_BUILDER_ID,
                input_builder_version=INPUT_BUILDER_VERSION,
                continue_on_domain_status=("healthy", "degraded"),
            )
            if runtime_step.input_hash is None:
                raise ManagedSkillChainInvalid("step input hash missing")
            plan_steps.append(
                ManagedSkillChainPlanStep(
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
                    input_builder_id="author-supplied-input",
                    input_builder_version="1",
                    input_hash=runtime_step.input_hash,
                    formal_draft_allowed=False,
                )
            )
        provisional = ManagedSkillChainPlan(
            workflow_plan_id=_digest_id(
                "managed-skill-chain-plan-",
                record.chain_id,
                token.token_id,
                *(step.workflow_step_id for step in plan_steps),
            ),
            chain_id=record.chain_id,
            chain_key=record.chain_key,
            project_id=record.project.project_id,
            project_revision=record.project.revision,
            project_fingerprint=record.project.fingerprint,
            risk_token_id=token.token_id,
            risk_token_hash=token.token_hash,
            steps=tuple(plan_steps),
            formal_draft_allowed=False,
            created_at=created_at,
            plan_hash="0" * 64,
        )
        material = provisional.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        return provisional.model_copy(update={"plan_hash": canonical_json_hash(material)})

    def _build_checkpoint(
        self,
        *,
        record: ManagedSkillChainRunRecord,
        step_ordinal: int,
        skill_receipt_id: str | None,
        skill_receipt_hash: str | None,
        input_hash: str | None,
        result_hash: str | None,
        execution_status: str,
        domain_status: str | None,
        previous_checkpoint_id: str | None,
        created_at: datetime,
    ) -> ManagedSkillChainCheckpoint:
        plan = record.workflow_plan
        if plan is None:
            raise ManagedSkillChainConflict("缺少 workflow plan，不能写 checkpoint。")
        step = next((item for item in plan.steps if item.ordinal == step_ordinal), None)
        if step is None:
            raise ManagedSkillChainConflict(f"workflow plan 缺少 step {step_ordinal}")
        provisional = ManagedSkillChainCheckpoint(
            checkpoint_id=_digest_id(
                "managed-skill-chain-checkpoint-",
                plan.workflow_plan_id,
                step.workflow_step_id,
                skill_receipt_id or "none",
                result_hash or "none",
                execution_status,
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
            skill_receipt_id=skill_receipt_id,
            skill_receipt_hash=skill_receipt_hash,
            input_hash=input_hash,
            result_hash=result_hash,
            execution_status=execution_status,  # type: ignore[arg-type]
            domain_status=domain_status,  # type: ignore[arg-type]
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

    def _mint_risk_token(
        self,
        *,
        record: ManagedSkillChainRunRecord,
        dossier: ManagedSkillChainRiskDossier,
        approved_at: datetime,
    ) -> ManagedSkillChainRiskApprovalToken:
        token_id = _digest_id(
            "risk-token-",
            record.chain_id,
            record.project.revision,
            record.project.fingerprint,
            *(skill.package_fingerprint for skill in dossier.skills),
            approved_at.isoformat(),
        )
        provisional = ManagedSkillChainRiskApprovalToken(
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
        return provisional.model_copy(update={"token_hash": canonical_json_hash(material)})

    def _require_usable_risk_token(
        self,
        record: ManagedSkillChainRunRecord,
    ) -> ManagedSkillChainRiskApprovalToken:
        token = record.risk_approval_token
        if token is None:
            raise ManagedSkillChainConflict("缺少风险审批 token，需要重新批准。")
        if token.consumed:
            raise ManagedSkillChainConflict(
                "风险审批 token 已消费，不能重复运行；请重新准备并批准。"
            )
        if token.chain_id != record.chain_id:
            raise ManagedSkillChainConflict("风险审批 token 与 chain 不匹配。")
        if (
            token.project_id != record.project.project_id
            or token.project_revision != record.project.revision
            or token.project_fingerprint != record.project.fingerprint
        ):
            raise ManagedSkillChainConflict("风险审批 token 绑定的项目快照已陈旧。")
        material = token.model_dump(mode="json", exclude={"token_hash"})
        material["consumed"] = False
        if token.token_hash != canonical_json_hash(material):
            raise ManagedSkillChainConflict("风险审批 token 被篡改或哈希不匹配。")
        return token

    def _assert_risk_dossier_current(
        self,
        project_id: str,
        dossier: ManagedSkillChainRiskDossier,
    ) -> None:
        for expected in dossier.skills:
            current = self._require_active_deterministic(project_id, expected.skill_id)
            if (
                current.skill_version_id != expected.skill_version_id
                or current.manifest_fingerprint != expected.manifest_fingerprint
                or current.package_fingerprint != expected.package_fingerprint
                or current.manifest.input_schema != expected.input_contract
                or current.manifest.output_schema != expected.output_contract
            ):
                raise ManagedSkillChainConflict(
                    "managed skill 版本/合同已变化，旧风险批准失效；请重新准备并重新批准。"
                )

    def _risk_summary(self, skills: list[ManagedSkillChainSkillRisk]) -> str:
        path = " -> ".join(f"{item.skill_id}@{item.skill_version}" for item in skills)
        return (
            f"一次作者风险批准覆盖 {len(skills)} 步 managed skill chain：{path}。"
            "只执行已适配且激活的 deterministic managed package；"
            "formal_draft_allowed=false；不执行 raw SKILL.md，也不声称通用四 Skills exactly-once。"
        )

    def _input_hash(self, payload: dict) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            raise ManagedSkillChainInvalid(
                f"step input_payload 超过 {MAX_INPUT_BYTES} 字节上限。"
            )
        return hashlib.sha256(encoded).hexdigest()

    def _materialize_project_snapshot(self, root_path: str, destination: Path) -> None:
        source = Path(root_path)
        if not source.is_absolute():
            raise ManagedSkillChainInvalid("项目 root_path 必须是绝对路径。")
        try:
            source_stat = source.lstat()
        except OSError as exc:
            raise ManagedSkillChainInvalid("项目 root_path 不可访问。") from exc
        if not stat.S_ISDIR(source_stat.st_mode):
            raise ManagedSkillChainInvalid("项目 root_path 必须是目录。")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".venv",
                "node_modules",
                "*.pyc",
            ),
        )

    def _reset_dir(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.store.get_project_binding(project_id)
        if binding is None:
            raise ManagedSkillChainNotFound(f"project binding not found: {project_id}")
        return binding

    def _require_fresh_binding(
        self,
        project_id: str,
        revision: int,
        fingerprint: str,
    ) -> ProjectBindingRecord:
        binding = self._require_binding(project_id)
        if binding.revision != revision or binding.fingerprint != fingerprint:
            raise ManagedSkillChainConflict(
                "项目 revision/fingerprint 已变化，需要重新准备 managed skill chain。"
            )
        return binding

    def _assert_not_stale(self, record: ManagedSkillChainRunRecord) -> None:
        binding = self._require_binding(record.project.project_id)
        if (
            binding.revision != record.project.revision
            or binding.fingerprint != record.project.fingerprint
        ):
            raise ManagedSkillChainConflict(
                "项目快照已变化，managed skill chain 已陈旧，需要重新准备。"
            )

    def _failed_record(
        self,
        record: ManagedSkillChainRunRecord,
        code: str,
        message: str,
    ) -> ManagedSkillChainRunRecord:
        steps = list(record.steps)
        ordinal = max(record.current_step_ordinal, 1)
        index = min(ordinal, len(steps)) - 1
        steps[index] = steps[index].model_copy(
            update={
                "status": "failed",
                "error_code": code[:128],
                "error_message": message[:4096],
            }
        )
        return record.model_copy(
            update={
                "status": "failed",
                "steps": tuple(steps),
                "error_code": code[:128],
                "error_message": message[:4096],
                "updated_at": _utcnow(),
            }
        )

    def _chain_path(self, chain_id: str) -> Path:
        return self.chain_root / f"{chain_id}.json"

    def _save_record(self, record: ManagedSkillChainRunRecord) -> None:
        path = self._chain_path(record.chain_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                record.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _load_record(self, chain_id: str) -> ManagedSkillChainRunRecord | None:
        path = self._chain_path(chain_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ManagedSkillChainRunRecord.model_validate(payload)

    def _require_record(
        self,
        project_id: str,
        chain_id: str,
    ) -> ManagedSkillChainRunRecord:
        record = self._load_record(chain_id)
        if record is None or record.project.project_id != project_id:
            raise ManagedSkillChainNotFound("managed skill chain 不存在。")
        return record


def raise_managed_skill_chain_error(exc: ManagedSkillChainError) -> None:
    raise ApiError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
        details=exc.details,
        retryable=exc.retryable,
    ) from exc


__all__ = [
    "ManagedSkillChainError",
    "ManagedSkillChainService",
    "raise_managed_skill_chain_error",
]
