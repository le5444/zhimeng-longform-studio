from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from zhimeng_runtime.context_models import canonical_json_hash


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
BatchId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-raw-adaptation-[0-9a-f]{32}$")
]


class FourSkillsRawAdaptationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FourSkillsRawFamily(StrEnum):
    SOURCE_INGEST = "source_ingest"
    DISTILLATION = "distillation"
    KNOWLEDGE_CONTEXT = "knowledge_context"
    CREATION_SUITE = "creation_suite"


class FourSkillsRawAdaptationStatus(StrEnum):
    PLANNED = "planned"
    MATERIALIZED = "materialized"
    INCOMPLETE = "incomplete"


class FourSkillsRawMemberRequest(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    source_path: NonEmptyText = Field(max_length=4096)


class FourSkillsRawAdaptationPlanRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-raw-adaptation-plan-request.v1"] = Field(
        default="zhimeng.four-skills-raw-adaptation-plan-request.v1",
        alias="schema",
    )
    members: list[FourSkillsRawMemberRequest] = Field(min_length=4, max_length=4)


class FourSkillsRawAdaptationMaterializeRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-raw-adaptation-materialize-request.v1"] = Field(
        default="zhimeng.four-skills-raw-adaptation-materialize-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256
    import_after_materialize: bool = True


class FourSkillsRawMemberRecord(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    source_path: NonEmptyText = Field(max_length=4096)
    plan_id: str | None = Field(default=None, max_length=160)
    plan_hash: Sha256 | None = None
    materialization_id: str | None = Field(default=None, max_length=160)
    skill_id: str | None = Field(default=None, max_length=128)
    skill_version_id: str | None = Field(default=None, max_length=160)
    package_fingerprint: Sha256 | None = None
    present: bool
    linked: bool
    executable: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsRawAdaptationReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-raw-adaptation-receipt.v1"] = Field(
        default="zhimeng.four-skills-raw-adaptation-receipt.v1",
        alias="schema",
    )
    batch_id: BatchId
    status: FourSkillsRawAdaptationStatus
    members: list[FourSkillsRawMemberRecord] = Field(min_length=4, max_length=4)
    member_count: int = Field(ge=4, le=4)
    all_present: bool
    all_linked: bool
    authorization_input_ready: bool
    authorization_unlock: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-raw-adaptation-1"] = (
        "local-v1-four-skills-raw-adaptation-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    updated_at: datetime
    batch_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsRawAdaptationReceipt":
        if self.authorization_unlock is not False:
            raise ValueError("raw adaptation cannot unlock formal draft")
        if self.formal_draft_allowed is not False:
            raise ValueError("raw adaptation cannot allow formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("raw adaptation cannot write project truth")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("raw adaptation cannot claim generic exactly-once")
        if self.raw_skill_md_execution is not False:
            raise ValueError("raw adaptation cannot claim raw SKILL.md execution")
        if self.member_count != len(self.members):
            raise ValueError("member_count must match members")
        families = [item.family for item in self.members]
        if len(set(families)) != 4:
            raise ValueError("raw adaptation requires four distinct families")
        expected = four_skills_raw_adaptation_receipt_hash(self)
        if self.batch_hash != expected:
            raise ValueError("raw adaptation batch hash mismatch")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return self


class FourSkillsRawAdaptationResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-raw-adaptation-response.v1"] = Field(
        default="zhimeng.four-skills-raw-adaptation-response.v1",
        alias="schema",
    )
    receipt: FourSkillsRawAdaptationReceipt
    history: list[FourSkillsRawAdaptationReceipt] = Field(default_factory=list, max_length=32)
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "deterministic_tool_auto_generation",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_raw_adaptation_receipt_hash(
    receipt: FourSkillsRawAdaptationReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-raw-adaptation-receipt-hash.v1",
        "batch_id": receipt.batch_id,
        "status": receipt.status.value if hasattr(receipt.status, "value") else receipt.status,
        "members": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "source_path": item.source_path,
                "plan_id": item.plan_id,
                "plan_hash": item.plan_hash,
                "materialization_id": item.materialization_id,
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "present": item.present,
                "linked": item.linked,
                "executable": False,
                "formal_draft_allowed": False,
                "detail": item.detail,
            }
            for item in receipt.members
        ],
        "member_count": receipt.member_count,
        "all_present": receipt.all_present,
        "all_linked": receipt.all_linked,
        "authorization_input_ready": receipt.authorization_input_ready,
        "authorization_unlock": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "four_skills_generic_exactly_once": False,
        "raw_skill_md_execution": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "updated_at": receipt.updated_at.isoformat(),
    }
    return canonical_json_hash(material)


__all__ = [
    "FourSkillsRawAdaptationMaterializeRequest",
    "FourSkillsRawAdaptationPlanRequest",
    "FourSkillsRawAdaptationReceipt",
    "FourSkillsRawAdaptationResponse",
    "FourSkillsRawAdaptationStatus",
    "FourSkillsRawFamily",
    "FourSkillsRawMemberRecord",
    "FourSkillsRawMemberRequest",
    "four_skills_raw_adaptation_receipt_hash",
]


BindingId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-workflow-binding-[0-9a-f]{32}$")
]


class FourSkillsWorkflowBindingRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-binding-request.v1"] = Field(
        default="zhimeng.four-skills-workflow-binding-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256


class FourSkillsWorkflowBindingStep(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    skill_id: NonEmptyText = Field(max_length=128)
    skill_version_id: NonEmptyText = Field(max_length=160)
    package_fingerprint: Sha256
    runner: Literal["instruction_source"] = "instruction_source"
    executable: Literal[False] = False
    bound: bool
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsWorkflowBindingReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-binding-receipt.v1"] = Field(
        default="zhimeng.four-skills-workflow-binding-receipt.v1",
        alias="schema",
    )
    binding_id: BindingId
    batch_id: BatchId
    batch_hash: Sha256
    status: Literal["bound", "incomplete"]
    workflow_surface: Literal["generic_instruction_workflow"] = (
        "generic_instruction_workflow"
    )
    steps: list[FourSkillsWorkflowBindingStep] = Field(min_length=4, max_length=4)
    step_count: int = Field(ge=4, le=4)
    all_bound: bool
    workflow_ready: bool
    executable: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-workflow-binding-1"] = (
        "local-v1-four-skills-workflow-binding-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    bound_at: datetime
    binding_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsWorkflowBindingReceipt":
        if self.executable is not False:
            raise ValueError("workflow binding cannot mark packages executable")
        if self.raw_skill_md_execution is not False:
            raise ValueError("workflow binding cannot claim raw SKILL.md execution")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("workflow binding cannot claim generic exactly-once")
        if self.formal_draft_allowed is not False:
            raise ValueError("workflow binding cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("workflow binding cannot write project truth")
        if self.step_count != len(self.steps):
            raise ValueError("step_count must match steps")
        if self.all_bound is not all(item.bound for item in self.steps):
            raise ValueError("all_bound must match steps")
        if self.workflow_ready is not (self.status == "bound" and self.all_bound):
            raise ValueError("workflow_ready must match bound status")
        if self.bound_at.tzinfo is None or self.bound_at.utcoffset() is None:
            raise ValueError("bound_at must be timezone-aware")
        expected = four_skills_workflow_binding_receipt_hash(self)
        if self.binding_hash != expected:
            raise ValueError("workflow binding hash mismatch")
        return self


class FourSkillsWorkflowBindingResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-binding-response.v1"] = Field(
        default="zhimeng.four-skills-workflow-binding-response.v1",
        alias="schema",
    )
    receipt: FourSkillsWorkflowBindingReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "deterministic_tool_auto_generation",
        "managed_skill_execution",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_workflow_binding_receipt_hash(
    receipt: FourSkillsWorkflowBindingReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-workflow-binding-receipt-hash.v1",
        "binding_id": receipt.binding_id,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "status": receipt.status,
        "workflow_surface": receipt.workflow_surface,
        "steps": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "runner": item.runner,
                "executable": False,
                "bound": item.bound,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "step_count": receipt.step_count,
        "all_bound": receipt.all_bound,
        "workflow_ready": receipt.workflow_ready,
        "executable": False,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "bound_at": receipt.bound_at.isoformat(),
    }
    return canonical_json_hash(material)


OrchestrationId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-workflow-orchestration-[0-9a-f]{32}$")
]


class FourSkillsWorkflowOrchestrationRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-orchestration-request.v1"] = Field(
        default="zhimeng.four-skills-workflow-orchestration-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256
    expected_binding_id: BindingId
    expected_binding_hash: Sha256


class FourSkillsWorkflowOrchestrationStep(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    skill_id: NonEmptyText = Field(max_length=128)
    skill_version_id: NonEmptyText = Field(max_length=160)
    package_fingerprint: Sha256
    runner: Literal["instruction_orchestrator"] = "instruction_orchestrator"
    status: Literal["completed", "skipped", "failed"]
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsWorkflowOrchestrationReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-orchestration-receipt.v1"] = Field(
        default="zhimeng.four-skills-workflow-orchestration-receipt.v1",
        alias="schema",
    )
    orchestration_id: OrchestrationId
    batch_id: BatchId
    batch_hash: Sha256
    binding_id: BindingId
    binding_hash: Sha256
    status: Literal["completed", "incomplete", "failed"]
    workflow_surface: Literal["generic_instruction_workflow"] = (
        "generic_instruction_workflow"
    )
    steps: list[FourSkillsWorkflowOrchestrationStep] = Field(min_length=4, max_length=4)
    step_count: int = Field(ge=4, le=4)
    completed_step_count: int = Field(ge=0, le=4)
    orchestration_ready: bool
    runnable: bool
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-workflow-orchestration-1"] = (
        "local-v1-four-skills-workflow-orchestration-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    orchestrated_at: datetime
    orchestration_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsWorkflowOrchestrationReceipt":
        if self.managed_skill_execution is not False:
            raise ValueError("orchestration cannot claim managed skill execution")
        if self.raw_skill_md_execution is not False:
            raise ValueError("orchestration cannot claim raw SKILL.md execution")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("orchestration cannot claim generic exactly-once")
        if self.formal_draft_allowed is not False:
            raise ValueError("orchestration cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("orchestration cannot write project truth")
        if self.step_count != len(self.steps):
            raise ValueError("step_count must match steps")
        completed = sum(1 for item in self.steps if item.status == "completed")
        if self.completed_step_count != completed:
            raise ValueError("completed_step_count must match completed steps")
        expected_ready = self.status == "completed" and completed == 4
        if self.orchestration_ready is not expected_ready:
            raise ValueError("orchestration_ready must match completed status")
        if self.runnable is not (self.status in {"completed", "incomplete"} and completed > 0):
            raise ValueError("runnable must reflect whether orchestration advanced")
        if self.orchestrated_at.tzinfo is None or self.orchestrated_at.utcoffset() is None:
            raise ValueError("orchestrated_at must be timezone-aware")
        expected = four_skills_workflow_orchestration_receipt_hash(self)
        if self.orchestration_hash != expected:
            raise ValueError("workflow orchestration hash mismatch")
        return self


class FourSkillsWorkflowOrchestrationResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-workflow-orchestration-response.v1"] = Field(
        default="zhimeng.four-skills-workflow-orchestration-response.v1",
        alias="schema",
    )
    receipt: FourSkillsWorkflowOrchestrationReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "managed_skill_execution",
        "deterministic_tool_auto_generation",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_workflow_orchestration_receipt_hash(
    receipt: FourSkillsWorkflowOrchestrationReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-workflow-orchestration-receipt-hash.v1",
        "orchestration_id": receipt.orchestration_id,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "binding_id": receipt.binding_id,
        "binding_hash": receipt.binding_hash,
        "status": receipt.status,
        "workflow_surface": receipt.workflow_surface,
        "steps": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "runner": item.runner,
                "status": item.status,
                "managed_skill_execution": False,
                "raw_skill_md_execution": False,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "step_count": receipt.step_count,
        "completed_step_count": receipt.completed_step_count,
        "orchestration_ready": receipt.orchestration_ready,
        "runnable": receipt.runnable,
        "managed_skill_execution": False,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "orchestrated_at": receipt.orchestrated_at.isoformat(),
    }
    return canonical_json_hash(material)

HandoffId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-instruction-chain-handoff-[0-9a-f]{32}$")
]


class FourSkillsInstructionChainHandoffRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-instruction-chain-handoff-request.v1"] = Field(
        default="zhimeng.four-skills-instruction-chain-handoff-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256
    expected_binding_id: BindingId
    expected_binding_hash: Sha256
    expected_orchestration_id: OrchestrationId
    expected_orchestration_hash: Sha256


class FourSkillsInstructionChainHandoffStep(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    skill_id: NonEmptyText = Field(max_length=128)
    skill_version_id: NonEmptyText = Field(max_length=160)
    package_fingerprint: Sha256
    runner: Literal["instruction_chain_handoff"] = "instruction_chain_handoff"
    status: Literal["prepared", "skipped", "failed"]
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsInstructionChainHandoffReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-instruction-chain-handoff-receipt.v1"] = Field(
        default="zhimeng.four-skills-instruction-chain-handoff-receipt.v1",
        alias="schema",
    )
    handoff_id: HandoffId
    batch_id: BatchId
    batch_hash: Sha256
    binding_id: BindingId
    binding_hash: Sha256
    orchestration_id: OrchestrationId
    orchestration_hash: Sha256
    status: Literal["ready", "incomplete", "failed"]
    workflow_surface: Literal["generic_instruction_workflow"] = (
        "generic_instruction_workflow"
    )
    steps: list[FourSkillsInstructionChainHandoffStep] = Field(min_length=4, max_length=4)
    step_count: int = Field(ge=4, le=4)
    prepared_step_count: int = Field(ge=0, le=4)
    instruction_chain_ready: bool
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-instruction-chain-handoff-1"] = (
        "local-v1-four-skills-instruction-chain-handoff-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    handed_off_at: datetime
    handoff_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsInstructionChainHandoffReceipt":
        if self.managed_skill_execution is not False:
            raise ValueError("instruction chain handoff cannot claim managed skill execution")
        if self.raw_skill_md_execution is not False:
            raise ValueError("instruction chain handoff cannot claim raw SKILL.md execution")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("instruction chain handoff cannot claim generic exactly-once")
        if self.formal_draft_allowed is not False:
            raise ValueError("instruction chain handoff cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("instruction chain handoff cannot write project truth")
        if self.step_count != len(self.steps):
            raise ValueError("step_count must match steps")
        prepared = sum(1 for item in self.steps if item.status == "prepared")
        if self.prepared_step_count != prepared:
            raise ValueError("prepared_step_count must match prepared steps")
        expected_ready = self.status == "ready" and prepared == 4
        if self.instruction_chain_ready is not expected_ready:
            raise ValueError("instruction_chain_ready must match ready status")
        if self.handed_off_at.tzinfo is None or self.handed_off_at.utcoffset() is None:
            raise ValueError("handed_off_at must be timezone-aware")
        expected = four_skills_instruction_chain_handoff_receipt_hash(self)
        if self.handoff_hash != expected:
            raise ValueError("instruction chain handoff hash mismatch")
        return self


class FourSkillsInstructionChainHandoffResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-instruction-chain-handoff-response.v1"] = Field(
        default="zhimeng.four-skills-instruction-chain-handoff-response.v1",
        alias="schema",
    )
    receipt: FourSkillsInstructionChainHandoffReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "managed_skill_execution",
        "deterministic_tool_auto_generation",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_instruction_chain_handoff_receipt_hash(
    receipt: FourSkillsInstructionChainHandoffReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-instruction-chain-handoff-receipt-hash.v1",
        "handoff_id": receipt.handoff_id,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "binding_id": receipt.binding_id,
        "binding_hash": receipt.binding_hash,
        "orchestration_id": receipt.orchestration_id,
        "orchestration_hash": receipt.orchestration_hash,
        "status": receipt.status,
        "workflow_surface": receipt.workflow_surface,
        "steps": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "runner": item.runner,
                "status": item.status,
                "managed_skill_execution": False,
                "raw_skill_md_execution": False,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "step_count": receipt.step_count,
        "prepared_step_count": receipt.prepared_step_count,
        "instruction_chain_ready": receipt.instruction_chain_ready,
        "managed_skill_execution": False,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "handed_off_at": receipt.handed_off_at.isoformat(),
    }
    return canonical_json_hash(material)


ProjectBindingId = Annotated[
    str, StringConstraints(pattern=r"^project-instruction-chain-handoff-[0-9a-f]{32}$")
]
ProjectId = Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]


class ProjectInstructionChainHandoffBindRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.project-instruction-chain-handoff-bind-request.v1"] = Field(
        default="zhimeng.project-instruction-chain-handoff-bind-request.v1",
        alias="schema",
    )
    expected_project_revision: int = Field(ge=1)
    expected_project_fingerprint: Sha256
    batch_id: BatchId
    batch_hash: Sha256
    handoff_id: HandoffId
    handoff_hash: Sha256


class ProjectInstructionChainHandoffBindingReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.project-instruction-chain-handoff-binding-receipt.v1"] = Field(
        default="zhimeng.project-instruction-chain-handoff-binding-receipt.v1",
        alias="schema",
    )
    binding_id: ProjectBindingId
    project_id: ProjectId
    project_revision: int = Field(ge=1)
    project_fingerprint: Sha256
    batch_id: BatchId
    batch_hash: Sha256
    handoff_id: HandoffId
    handoff_hash: Sha256
    prepared_step_count: int = Field(ge=0, le=4)
    instruction_chain_ready: bool
    status: Literal["bound"] = "bound"
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-project-instruction-chain-handoff-1"] = (
        "local-v1-project-instruction-chain-handoff-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    bound_at: datetime
    binding_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "ProjectInstructionChainHandoffBindingReceipt":
        if self.managed_skill_execution is not False:
            raise ValueError("project handoff binding cannot claim managed skill execution")
        if self.raw_skill_md_execution is not False:
            raise ValueError("project handoff binding cannot claim raw SKILL.md execution")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("project handoff binding cannot claim generic exactly-once")
        if self.formal_draft_allowed is not False:
            raise ValueError("project handoff binding cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("project handoff binding cannot write project truth")
        if self.status != "bound":
            raise ValueError("project handoff binding status must be bound")
        if self.instruction_chain_ready is not True:
            raise ValueError("project handoff binding requires instruction_chain_ready")
        if self.prepared_step_count != 4:
            raise ValueError("project handoff binding requires four prepared steps")
        if self.bound_at.tzinfo is None or self.bound_at.utcoffset() is None:
            raise ValueError("bound_at must be timezone-aware")
        expected = project_instruction_chain_handoff_binding_receipt_hash(self)
        if self.binding_hash != expected:
            raise ValueError("project handoff binding hash mismatch")
        return self


class ProjectInstructionChainHandoffBindingResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.project-instruction-chain-handoff-binding-response.v1"] = Field(
        default="zhimeng.project-instruction-chain-handoff-binding-response.v1",
        alias="schema",
    )
    receipt: ProjectInstructionChainHandoffBindingReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "managed_skill_execution",
        "deterministic_tool_auto_generation",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def project_instruction_chain_handoff_binding_receipt_hash(
    receipt: ProjectInstructionChainHandoffBindingReceipt,
) -> str:
    material = {
        "schema": "zhimeng.project-instruction-chain-handoff-binding-receipt-hash.v1",
        "binding_id": receipt.binding_id,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "handoff_id": receipt.handoff_id,
        "handoff_hash": receipt.handoff_hash,
        "prepared_step_count": receipt.prepared_step_count,
        "instruction_chain_ready": True,
        "status": receipt.status,
        "managed_skill_execution": False,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "bound_at": receipt.bound_at.isoformat(),
    }
    return canonical_json_hash(material)


ProbeId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-generic-execution-probe-[0-9a-f]{32}$")
]


class FourSkillsGenericExecutionProbeRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-execution-probe-request.v1"] = Field(
        default="zhimeng.four-skills-generic-execution-probe-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256
    expected_handoff_id: HandoffId
    expected_handoff_hash: Sha256


class FourSkillsGenericExecutionProbeStep(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    skill_id: NonEmptyText = Field(max_length=128)
    skill_version_id: NonEmptyText = Field(max_length=160)
    package_fingerprint: Sha256
    package_kind: Literal["instruction_source", "deterministic_tool", "unknown"]
    catalog_present: bool
    fingerprint_matched: bool
    executor_would_accept: bool
    denial_code: Literal[
        "skill_not_executable",
        "skill_version_missing",
        "package_fingerprint_mismatch",
        "none",
    ]
    status: Literal["denied", "would_accept", "failed"]
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsGenericExecutionProbeReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-execution-probe-receipt.v1"] = Field(
        default="zhimeng.four-skills-generic-execution-probe-receipt.v1",
        alias="schema",
    )
    probe_id: ProbeId
    batch_id: BatchId
    batch_hash: Sha256
    handoff_id: HandoffId
    handoff_hash: Sha256
    status: Literal["complete", "incomplete", "failed"]
    steps: list[FourSkillsGenericExecutionProbeStep] = Field(min_length=4, max_length=4)
    step_count: int = Field(ge=4, le=4)
    assessed_step_count: int = Field(ge=0, le=4)
    denied_step_count: int = Field(ge=0, le=4)
    would_accept_step_count: int = Field(ge=0, le=4)
    probe_complete: bool
    generic_execution_ready: Literal[False] = False
    managed_skill_execution: Literal[False] = False
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-generic-execution-probe-1"] = (
        "local-v1-four-skills-generic-execution-probe-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    probed_at: datetime
    probe_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsGenericExecutionProbeReceipt":
        if self.generic_execution_ready is not False:
            raise ValueError("generic execution probe cannot claim generic_execution_ready")
        if self.managed_skill_execution is not False:
            raise ValueError("generic execution probe cannot claim managed skill execution")
        if self.raw_skill_md_execution is not False:
            raise ValueError("generic execution probe cannot claim raw SKILL.md execution")
        if self.four_skills_generic_exactly_once is not False:
            raise ValueError("generic execution probe cannot claim generic exactly-once")
        if self.formal_draft_allowed is not False:
            raise ValueError("generic execution probe cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("generic execution probe cannot write project truth")
        if self.step_count != len(self.steps):
            raise ValueError("step_count must match steps")
        assessed = sum(1 for item in self.steps if item.status in {"denied", "would_accept"})
        denied = sum(1 for item in self.steps if item.status == "denied")
        would_accept = sum(1 for item in self.steps if item.status == "would_accept")
        if self.assessed_step_count != assessed:
            raise ValueError("assessed_step_count must match assessed steps")
        if self.denied_step_count != denied:
            raise ValueError("denied_step_count must match denied steps")
        if self.would_accept_step_count != would_accept:
            raise ValueError("would_accept_step_count must match would_accept steps")
        expected_complete = self.status == "complete" and assessed == 4
        if self.probe_complete is not expected_complete:
            raise ValueError("probe_complete must match complete status")
        if any(item.executor_would_accept and item.status != "would_accept" for item in self.steps):
            raise ValueError("executor_would_accept requires would_accept status")
        if any(item.status == "would_accept" and not item.executor_would_accept for item in self.steps):
            raise ValueError("would_accept status requires executor_would_accept")
        if self.probed_at.tzinfo is None or self.probed_at.utcoffset() is None:
            raise ValueError("probed_at must be timezone-aware")
        expected = four_skills_generic_execution_probe_receipt_hash(self)
        if self.probe_hash != expected:
            raise ValueError("generic execution probe hash mismatch")
        return self


class FourSkillsGenericExecutionProbeResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-execution-probe-response.v1"] = Field(
        default="zhimeng.four-skills-generic-execution-probe-response.v1",
        alias="schema",
    )
    receipt: FourSkillsGenericExecutionProbeReceipt
    not_claiming: tuple[str, ...] = (
        "four_skills_generic_exactly_once",
        "raw_skill_md_execution",
        "managed_skill_execution",
        "generic_execution_ready",
        "deterministic_tool_auto_generation",
        "formal_draft_authorization_unlock",
        "local_v1_exe",
    )


def four_skills_generic_execution_probe_receipt_hash(
    receipt: FourSkillsGenericExecutionProbeReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-generic-execution-probe-receipt-hash.v1",
        "probe_id": receipt.probe_id,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "handoff_id": receipt.handoff_id,
        "handoff_hash": receipt.handoff_hash,
        "status": receipt.status,
        "steps": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "skill_id": item.skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "package_kind": item.package_kind,
                "catalog_present": item.catalog_present,
                "fingerprint_matched": item.fingerprint_matched,
                "executor_would_accept": item.executor_would_accept,
                "denial_code": item.denial_code,
                "status": item.status,
                "managed_skill_execution": False,
                "raw_skill_md_execution": False,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "step_count": receipt.step_count,
        "assessed_step_count": receipt.assessed_step_count,
        "denied_step_count": receipt.denied_step_count,
        "would_accept_step_count": receipt.would_accept_step_count,
        "probe_complete": receipt.probe_complete,
        "generic_execution_ready": False,
        "managed_skill_execution": False,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": False,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "probed_at": receipt.probed_at.isoformat(),
    }
    return canonical_json_hash(material)


ManagedExecutionId = Annotated[
    str, StringConstraints(pattern=r"^four-skills-generic-managed-execution-[0-9a-f]{32}$")
]


FAMILY_MANAGED_SKILL_IDS: dict[str, str] = {
    "source_ingest": "four-skills.family-source-ingest",
    "distillation": "four-skills.family-distillation",
    "knowledge_context": "four-skills.family-knowledge-context",
    "creation_suite": "four-skills.family-creation-suite",
}


class FourSkillsGenericManagedExecutionRequest(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-managed-execution-request.v1"] = Field(
        default="zhimeng.four-skills-generic-managed-execution-request.v1",
        alias="schema",
    )
    expected_batch_id: BatchId
    expected_batch_hash: Sha256
    expected_probe_id: ProbeId
    expected_probe_hash: Sha256
    project_id: Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
    expected_project_revision: int = Field(ge=0)
    expected_project_fingerprint: Sha256


class FourSkillsGenericManagedExecutionStep(FourSkillsRawAdaptationModel):
    family: FourSkillsRawFamily
    ordinal: int = Field(ge=1, le=4)
    managed_skill_id: NonEmptyText = Field(max_length=128)
    skill_version_id: str | None = Field(default=None, max_length=160)
    package_fingerprint: Sha256 | None = None
    status: Literal["succeeded", "failed", "skipped", "denied"]
    managed_skill_execution: bool
    raw_skill_md_execution: Literal[False] = False
    skill_receipt_id: str | None = Field(default=None, max_length=160)
    skill_receipt_hash: Sha256 | None = None
    denial_code: Literal[
        "none",
        "probe_incomplete",
        "skill_not_active",
        "skill_not_executable",
        "execution_failed",
        "project_mismatch",
    ] = "none"
    detail: NonEmptyText = Field(max_length=1024)


class FourSkillsGenericManagedExecutionReceipt(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-managed-execution-receipt.v1"] = Field(
        default="zhimeng.four-skills-generic-managed-execution-receipt.v1",
        alias="schema",
    )
    execution_id: ManagedExecutionId
    batch_id: BatchId
    batch_hash: Sha256
    probe_id: ProbeId
    probe_hash: Sha256
    project_id: Annotated[str, StringConstraints(pattern=r"^project-[0-9a-f]{24}$")]
    project_revision: int = Field(ge=0)
    project_fingerprint: Sha256
    status: Literal["complete", "incomplete", "failed"]
    steps: list[FourSkillsGenericManagedExecutionStep] = Field(min_length=4, max_length=4)
    step_count: int = Field(ge=4, le=4)
    succeeded_step_count: int = Field(ge=0, le=4)
    failed_step_count: int = Field(ge=0, le=4)
    managed_skill_execution: bool
    raw_skill_md_execution: Literal[False] = False
    four_skills_generic_exactly_once: bool
    formal_draft_allowed: Literal[False] = False
    writes_project_truth: Literal[False] = False
    policy_version: Literal["local-v1-four-skills-generic-managed-execution-1"] = (
        "local-v1-four-skills-generic-managed-execution-1"
    )
    detail: NonEmptyText = Field(max_length=1024)
    executed_at: datetime
    execution_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "FourSkillsGenericManagedExecutionReceipt":
        if self.raw_skill_md_execution is not False:
            raise ValueError("generic managed execution cannot claim raw SKILL.md execution")
        if self.formal_draft_allowed is not False:
            raise ValueError("generic managed execution cannot unlock formal draft")
        if self.writes_project_truth is not False:
            raise ValueError("generic managed execution cannot write project truth")
        if self.step_count != len(self.steps):
            raise ValueError("step_count must match steps")
        succeeded = sum(1 for item in self.steps if item.status == "succeeded")
        failed = sum(1 for item in self.steps if item.status == "failed")
        if self.succeeded_step_count != succeeded:
            raise ValueError("succeeded_step_count mismatch")
        if self.failed_step_count != failed:
            raise ValueError("failed_step_count mismatch")
        any_managed = any(item.managed_skill_execution for item in self.steps)
        if self.managed_skill_execution is not any_managed:
            raise ValueError("managed_skill_execution must match step executions")
        expected_once = succeeded == 4 and failed == 0 and all(
            item.managed_skill_execution and item.raw_skill_md_execution is False
            for item in self.steps
        )
        if self.four_skills_generic_exactly_once is not expected_once:
            raise ValueError(
                "four_skills_generic_exactly_once requires all four managed steps succeeded once"
            )
        if self.four_skills_generic_exactly_once and self.status != "complete":
            raise ValueError("exactly-once claim requires complete status")
        if self.executed_at.tzinfo is None or self.executed_at.utcoffset() is None:
            raise ValueError("executed_at must be timezone-aware")
        expected = four_skills_generic_managed_execution_receipt_hash(self)
        if self.execution_hash != expected:
            raise ValueError("generic managed execution hash mismatch")
        return self


class FourSkillsGenericManagedExecutionResponse(FourSkillsRawAdaptationModel):
    schema_id: Literal["zhimeng.four-skills-generic-managed-execution-response.v1"] = Field(
        default="zhimeng.four-skills-generic-managed-execution-response.v1",
        alias="schema",
    )
    receipt: FourSkillsGenericManagedExecutionReceipt
    not_claiming: tuple[str, ...] = (
        "raw_skill_md_execution",
        "formal_draft_authorization_unlock",
        "formal_prose_writeback",
        "local_v1_exe",
    )


def four_skills_generic_managed_execution_receipt_hash(
    receipt: FourSkillsGenericManagedExecutionReceipt,
) -> str:
    material = {
        "schema": "zhimeng.four-skills-generic-managed-execution-receipt-hash.v1",
        "execution_id": receipt.execution_id,
        "batch_id": receipt.batch_id,
        "batch_hash": receipt.batch_hash,
        "probe_id": receipt.probe_id,
        "probe_hash": receipt.probe_hash,
        "project_id": receipt.project_id,
        "project_revision": receipt.project_revision,
        "project_fingerprint": receipt.project_fingerprint,
        "status": receipt.status,
        "steps": [
            {
                "family": item.family.value if hasattr(item.family, "value") else item.family,
                "ordinal": item.ordinal,
                "managed_skill_id": item.managed_skill_id,
                "skill_version_id": item.skill_version_id,
                "package_fingerprint": item.package_fingerprint,
                "status": item.status,
                "managed_skill_execution": item.managed_skill_execution,
                "raw_skill_md_execution": False,
                "skill_receipt_id": item.skill_receipt_id,
                "skill_receipt_hash": item.skill_receipt_hash,
                "denial_code": item.denial_code,
                "detail": item.detail,
            }
            for item in receipt.steps
        ],
        "step_count": receipt.step_count,
        "succeeded_step_count": receipt.succeeded_step_count,
        "failed_step_count": receipt.failed_step_count,
        "managed_skill_execution": receipt.managed_skill_execution,
        "raw_skill_md_execution": False,
        "four_skills_generic_exactly_once": receipt.four_skills_generic_exactly_once,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "policy_version": receipt.policy_version,
        "detail": receipt.detail,
        "executed_at": receipt.executed_at.isoformat(),
    }
    return canonical_json_hash(material)

