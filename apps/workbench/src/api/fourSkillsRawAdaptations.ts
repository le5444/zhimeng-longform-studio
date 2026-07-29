import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type FourSkillsFamily =
  | 'source_ingest'
  | 'distillation'
  | 'knowledge_context'
  | 'creation_suite'

export interface FourSkillsMemberRequest {
  family: FourSkillsFamily
  source_path: string
}

export interface FourSkillsRawMemberRecord {
  family: FourSkillsFamily
  ordinal: number
  source_path: string
  plan_id: string | null
  plan_hash: string | null
  materialization_id: string | null
  skill_id: string | null
  skill_version_id: string | null
  package_fingerprint: string | null
  present: boolean
  linked: boolean
  executable: false
  formal_draft_allowed: false
  detail: string
}

export interface FourSkillsRawAdaptationReceipt {
  schema: 'zhimeng.four-skills-raw-adaptation-receipt.v1'
  batch_id: string
  status: 'planned' | 'materialized' | 'incomplete'
  members: FourSkillsRawMemberRecord[]
  member_count: number
  all_present: boolean
  all_linked: boolean
  authorization_input_ready: boolean
  authorization_unlock: false
  formal_draft_allowed: false
  writes_project_truth: false
  four_skills_generic_exactly_once: false
  raw_skill_md_execution: false
  policy_version: string
  detail: string
  updated_at: string
  batch_hash: string
}

export interface FourSkillsRawAdaptationResponse {
  schema: 'zhimeng.four-skills-raw-adaptation-response.v1'
  receipt: FourSkillsRawAdaptationReceipt
  not_claiming: string[]
}

export interface FourSkillsWorkflowBindingStep {
  family: FourSkillsFamily
  ordinal: number
  skill_id: string
  skill_version_id: string
  package_fingerprint: string
  runner: 'instruction_source'
  executable: false
  bound: boolean
  detail: string
}

export interface FourSkillsWorkflowBindingReceipt {
  schema: 'zhimeng.four-skills-workflow-binding-receipt.v1'
  binding_id: string
  batch_id: string
  batch_hash: string
  status: 'bound' | 'incomplete'
  workflow_surface: 'generic_instruction_workflow'
  steps: FourSkillsWorkflowBindingStep[]
  step_count: number
  all_bound: boolean
  workflow_ready: boolean
  executable: false
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  bound_at: string
  binding_hash: string
}

export interface FourSkillsWorkflowBindingResponse {
  schema: 'zhimeng.four-skills-workflow-binding-response.v1'
  receipt: FourSkillsWorkflowBindingReceipt
  not_claiming: string[]
}

export interface FourSkillsWorkflowOrchestrationStep {
  family: FourSkillsFamily
  ordinal: number
  skill_id: string
  skill_version_id: string
  package_fingerprint: string
  runner: 'instruction_orchestrator'
  status: 'completed' | 'skipped' | 'failed'
  managed_skill_execution: false
  raw_skill_md_execution: false
  detail: string
}

export interface FourSkillsWorkflowOrchestrationReceipt {
  schema: 'zhimeng.four-skills-workflow-orchestration-receipt.v1'
  orchestration_id: string
  batch_id: string
  batch_hash: string
  binding_id: string
  binding_hash: string
  status: 'completed' | 'incomplete' | 'failed'
  workflow_surface: 'generic_instruction_workflow'
  steps: FourSkillsWorkflowOrchestrationStep[]
  step_count: number
  completed_step_count: number
  orchestration_ready: boolean
  runnable: boolean
  managed_skill_execution: false
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  orchestrated_at: string
  orchestration_hash: string
}

export interface FourSkillsWorkflowOrchestrationResponse {
  schema: 'zhimeng.four-skills-workflow-orchestration-response.v1'
  receipt: FourSkillsWorkflowOrchestrationReceipt
  not_claiming: string[]
}

export interface FourSkillsInstructionChainHandoffStep {
  family: FourSkillsFamily
  ordinal: number
  skill_id: string
  skill_version_id: string
  package_fingerprint: string
  runner: 'instruction_chain_handoff'
  status: 'prepared' | 'skipped' | 'failed'
  managed_skill_execution: false
  raw_skill_md_execution: false
  detail: string
}

export interface FourSkillsInstructionChainHandoffReceipt {
  schema: 'zhimeng.four-skills-instruction-chain-handoff-receipt.v1'
  handoff_id: string
  batch_id: string
  batch_hash: string
  binding_id: string
  binding_hash: string
  orchestration_id: string
  orchestration_hash: string
  status: 'ready' | 'incomplete' | 'failed'
  workflow_surface: 'generic_instruction_workflow'
  steps: FourSkillsInstructionChainHandoffStep[]
  step_count: number
  prepared_step_count: number
  instruction_chain_ready: boolean
  managed_skill_execution: false
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  handed_off_at: string
  handoff_hash: string
}

export interface FourSkillsInstructionChainHandoffResponse {
  schema: 'zhimeng.four-skills-instruction-chain-handoff-response.v1'
  receipt: FourSkillsInstructionChainHandoffReceipt
  not_claiming: string[]
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export function planFourSkillsRawAdaptation(
  members: FourSkillsMemberRequest[],
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsRawAdaptationResponse>(
    '/api/v1/four-skills-raw-adaptations/plan',
    options,
    jsonPost({ members }),
  )
}

export function materializeFourSkillsRawAdaptation(
  input: {
    expected_batch_id: string
    expected_batch_hash: string
    import_after_materialize?: boolean
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsRawAdaptationResponse>(
    '/api/v1/four-skills-raw-adaptations/materialize',
    options,
    jsonPost(input),
  )
}

export function bindFourSkillsWorkflow(
  batchId: string,
  input: {
    expected_batch_id: string
    expected_batch_hash: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsWorkflowBindingResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/workflow-bindings`,
    options,
    jsonPost(input),
  )
}

export function orchestrateFourSkillsWorkflow(
  batchId: string,
  input: {
    expected_batch_id: string
    expected_batch_hash: string
    expected_binding_id: string
    expected_binding_hash: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsWorkflowOrchestrationResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/workflow-orchestrations`,
    options,
    jsonPost(input),
  )
}

export function handoffFourSkillsInstructionChain(
  batchId: string,
  input: {
    expected_batch_id: string
    expected_batch_hash: string
    expected_binding_id: string
    expected_binding_hash: string
    expected_orchestration_id: string
    expected_orchestration_hash: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsInstructionChainHandoffResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/instruction-chain-handoffs`,
    options,
    jsonPost(input),
  )
}

export function getFourSkillsInstructionChainHandoff(
  batchId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsInstructionChainHandoffResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/instruction-chain-handoffs`,
    options,
  )
}

export interface FourSkillsGenericExecutionProbeStep {
  family: FourSkillsFamily
  ordinal: number
  skill_id: string
  skill_version_id: string
  package_fingerprint: string
  package_kind: 'instruction_source' | 'deterministic_tool' | 'unknown'
  catalog_present: boolean
  fingerprint_matched: boolean
  executor_would_accept: boolean
  denial_code:
    | 'skill_not_executable'
    | 'skill_version_missing'
    | 'package_fingerprint_mismatch'
    | 'none'
  status: 'denied' | 'would_accept' | 'failed'
  managed_skill_execution: false
  raw_skill_md_execution: false
  detail: string
}

export interface FourSkillsGenericExecutionProbeReceipt {
  schema: 'zhimeng.four-skills-generic-execution-probe-receipt.v1'
  probe_id: string
  batch_id: string
  batch_hash: string
  handoff_id: string
  handoff_hash: string
  status: 'complete' | 'incomplete' | 'failed'
  steps: FourSkillsGenericExecutionProbeStep[]
  step_count: number
  assessed_step_count: number
  denied_step_count: number
  would_accept_step_count: number
  probe_complete: boolean
  generic_execution_ready: false
  managed_skill_execution: false
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  probed_at: string
  probe_hash: string
}

export interface FourSkillsGenericExecutionProbeResponse {
  schema: 'zhimeng.four-skills-generic-execution-probe-response.v1'
  receipt: FourSkillsGenericExecutionProbeReceipt
  not_claiming: string[]
}

export function probeFourSkillsGenericExecution(
  batchId: string,
  request: {
    expected_batch_id: string
    expected_batch_hash: string
    expected_handoff_id: string
    expected_handoff_hash: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsGenericExecutionProbeResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/generic-execution-probes`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}

export function getFourSkillsGenericExecutionProbe(
  batchId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsGenericExecutionProbeResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/generic-execution-probes`,
    options,
  )
}




export interface FourSkillsGenericManagedExecutionStep {
  family: FourSkillsFamily
  ordinal: number
  managed_skill_id: string
  skill_version_id: string | null
  package_fingerprint: string | null
  status: 'succeeded' | 'failed' | 'skipped' | 'denied'
  managed_skill_execution: boolean
  raw_skill_md_execution: false
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  denial_code:
    | 'none'
    | 'probe_incomplete'
    | 'skill_not_active'
    | 'skill_not_executable'
    | 'execution_failed'
    | 'project_mismatch'
  detail: string
}

export interface FourSkillsGenericManagedExecutionReceipt {
  schema: 'zhimeng.four-skills-generic-managed-execution-receipt.v1'
  execution_id: string
  batch_id: string
  batch_hash: string
  probe_id: string
  probe_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  status: 'complete' | 'incomplete' | 'failed'
  steps: FourSkillsGenericManagedExecutionStep[]
  step_count: number
  succeeded_step_count: number
  failed_step_count: number
  managed_skill_execution: boolean
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: boolean
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  executed_at: string
  execution_hash: string
}

export interface FourSkillsGenericManagedExecutionResponse {
  schema: 'zhimeng.four-skills-generic-managed-execution-response.v1'
  receipt: FourSkillsGenericManagedExecutionReceipt
  not_claiming: string[]
}

export function executeFourSkillsGenericManaged(
  batchId: string,
  request: {
    expected_batch_id: string
    expected_batch_hash: string
    expected_probe_id: string
    expected_probe_hash: string
    project_id: string
    expected_project_revision: number
    expected_project_fingerprint: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsGenericManagedExecutionResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/generic-managed-executions`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}

export function getFourSkillsGenericManagedExecution(
  batchId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FourSkillsGenericManagedExecutionResponse>(
    `/api/v1/four-skills-raw-adaptations/${encodeURIComponent(batchId)}/generic-managed-executions`,
    options,
  )
}

export interface ProjectInstructionChainHandoffBindingReceipt {
  schema: 'zhimeng.project-instruction-chain-handoff-binding-receipt.v1'
  binding_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  batch_id: string
  batch_hash: string
  handoff_id: string
  handoff_hash: string
  prepared_step_count: number
  instruction_chain_ready: boolean
  status: 'bound'
  managed_skill_execution: false
  raw_skill_md_execution: false
  four_skills_generic_exactly_once: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: string
  detail: string
  bound_at: string
  binding_hash: string
}

export interface ProjectInstructionChainHandoffBindingResponse {
  schema: 'zhimeng.project-instruction-chain-handoff-binding-response.v1'
  receipt: ProjectInstructionChainHandoffBindingReceipt
  not_claiming: string[]
}

export function bindProjectInstructionChainHandoff(
  projectId: string,
  input: {
    expected_project_revision: number
    expected_project_fingerprint: string
    batch_id: string
    batch_hash: string
    handoff_id: string
    handoff_hash: string
  },
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ProjectInstructionChainHandoffBindingResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/instruction-chain-handoff-bindings`,
    options,
    jsonPost(input),
  )
}

