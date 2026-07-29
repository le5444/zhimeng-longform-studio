from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateOutput,
    AgentCandidateOutputRecord,
)
from zhimeng_runtime.agent_models import AgentWorkflowStateResponse
from zhimeng_runtime.creation_instruction_models import (
    CreationInstructionActivationBinding,
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
)
from zhimeng_runtime.execution_protocol import StableId
from zhimeng_runtime.provider_recovery_models import ProviderRecoveryResponse
from zhimeng_runtime.writer_models import WriterCandidateResult, WriterTask


class WriterTraceResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_id: Literal["zhimeng.writer-trace.v1"] = Field(
        default="zhimeng.writer-trace.v1",
        alias="schema",
    )
    run_id: StableId
    workflow: AgentWorkflowStateResponse
    writer_task: WriterTask | None = None
    activation_binding: CreationInstructionActivationBinding | None = None
    materialization: CreationInstructionMaterialization | None = None
    dispatch_receipt: CreationInstructionDispatchReceipt | None = None
    candidate_output: AgentCandidateOutputRecord | None = None
    writer_candidate_result: WriterCandidateResult | None = None
    provider_recovery: ProviderRecoveryResponse | None = None

    @model_validator(mode="after")
    def validate_trace(self) -> "WriterTraceResponse":
        if self.workflow.run_id != self.run_id:
            raise ValueError("Writer trace Workflow belongs to another Run")
        evidence_run_ids = (
            self.writer_task.run_id if self.writer_task else None,
            self.activation_binding.run_id if self.activation_binding else None,
            self.materialization.run_id if self.materialization else None,
            self.dispatch_receipt.run_id if self.dispatch_receipt else None,
            self.candidate_output.run_id if self.candidate_output else None,
            self.writer_candidate_result.run_id
            if self.writer_candidate_result
            else None,
            self.provider_recovery.dossier.run_id if self.provider_recovery else None,
        )
        if any(run_id not in {None, self.run_id} for run_id in evidence_run_ids):
            raise ValueError("Writer trace contains evidence from another Run")
        if self.materialization is not None and (
            self.writer_task is None or self.activation_binding is None
        ):
            raise ValueError("Writer trace materialization requires task and activation")
        if self.dispatch_receipt is not None and self.materialization is None:
            raise ValueError("Writer trace dispatch requires instruction materialization")
        if isinstance(self.candidate_output, AgentCandidateOutput) and (
            self.writer_task is None
            or self.materialization is None
            or self.dispatch_receipt is None
        ):
            raise ValueError("Current Writer candidate requires its dispatch evidence")
        if self.writer_candidate_result is not None and (
            self.writer_task is None or self.candidate_output is None
        ):
            raise ValueError("Writer result requires task and candidate evidence")
        if self.provider_recovery is not None and self.dispatch_receipt is None:
            raise ValueError("Provider recovery requires dispatch evidence")
        return self


__all__ = ["WriterTraceResponse"]
