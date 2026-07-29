import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import { fetchRuntimeSse } from './sse'
import { canonicalJsonHash } from '../lib/canonicalJson'

export type AgentAvailability = 'checking' | 'available' | 'unavailable' | 'error'
export type WriterMode = 'repair_only' | 'author_review_change_proposal'
export type AgentRunStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'awaiting_approval'
  | 'paused'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface AgentStatus {
  schema: 'zhimeng.agent-status.v1'
  available: boolean
  detail: string
  executor: Record<string, unknown> | null
  capabilities: Record<string, unknown> | null
}

export interface ProjectRevision {
  project_id: string
  revision: number
  fingerprint: string
  observed_at: string
}

export interface AgentSession {
  schema: 'zhimeng.agent-session.v1'
  session_id: string
  project_revision: ProjectRevision
  title: string
  status: 'active' | 'idle' | 'closing' | 'closed' | 'interrupted'
  revision: number
  executor: Record<string, unknown>
  capabilities: Record<string, unknown>
  correlation_id: string
  last_run_ordinal: number
  last_message_ordinal: number
  created_at: string
  updated_at: string
}

export interface AgentMessage {
  schema: 'zhimeng.agent-message.v1'
  message_id: string
  session_id: string
  run_id: string
  ordinal: number
  role: 'system' | 'user' | 'assistant' | 'tool'
  format: 'plain' | 'markdown'
  content: string
  created_at: string
}

export type AgentCandidateKind =
  | 'repair_plan'
  | 'risk_draft'
  | 'change_proposal'
  | 'next_step_advice'
export type AgentCandidateReferenceKind =
  | 'workflow_checkpoint'
  | 'runtime_identity'
  | 'artifact'
  | 'change_set'

export interface AgentCandidateContent {
  title: string
  summary: string
  body: string
  format: 'markdown'
}

export interface AgentCandidateReference {
  reference_kind: AgentCandidateReferenceKind
  reference_id: string
  reference_hash: string
}

export interface AgentCandidateOutputBase {
  candidate_output_id: string
  run_id: string
  workflow_plan_id: string
  workflow_plan_hash: string
  final_checkpoint_id: string
  final_checkpoint_hash: string
  provider_boundary_id: string
  provider_boundary_hash: string
  provider_request_hash: string
  context_pack_id: string
  context_pack_hash: string
  operation_package_id: string
  operation_package_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  handoff_status: 'blocked' | 'risk_draft_only'
  allowed_output: 'repair_plan_or_risk_draft_only'
  failure_signatures: string[]
  kind: AgentCandidateKind
  content: AgentCandidateContent
  content_hash: string
  references: AgentCandidateReference[]
  references_hash: string
  formal_draft_allowed: false
  requires_author_review: true
  created_at: string
  envelope_hash: string
}

export interface ProviderTurnReceiptBinding {
  turn_ordinal: number
  receipt_id: string
  receipt_hash: string
}

export type ProviderModelIdentityStatus =
  | 'exact_match'
  | 'accepted_alias'
  | 'unreported'
  | 'rejected_mismatch'
export type ProviderIdentityAggregateStatus =
  | 'pending'
  | 'verified'
  | 'unreported'
  | 'rejected_mismatch'

export interface ProviderMessageUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface ProviderTurnReceipt {
  schema: 'zhimeng.provider-turn-receipt.v1'
  receipt_id: string
  run_id: string
  provider_boundary_id: string
  provider_boundary_hash: string
  turn_ordinal: number
  request_hash: string
  requested_model: string | null
  reported_model: string | null
  accepted_reported_models: string[]
  model_identity_status: ProviderModelIdentityStatus
  finish_reason: string
  usage: ProviderMessageUsage | null
  created_at: string
  receipt_hash: string
}

export interface ProviderTurnReceiptListResponse {
  schema: 'zhimeng.provider-turn-receipt-list.v1'
  run_id: string
  provider_boundary_id: string
  provider_boundary_hash: string
  requested_model: string | null
  provider_turn_receipts: ProviderTurnReceipt[]
  provider_turn_receipts_hash: string
  provider_identity_status: ProviderIdentityAggregateStatus
}

export interface AgentCandidateOutputV1 extends AgentCandidateOutputBase {
  schema: 'zhimeng.agent-candidate-output.v1'
}

export interface AgentCandidateOutputV2 extends AgentCandidateOutputBase {
  schema: 'zhimeng.agent-candidate-output.v2'
  provider_turn_receipts: ProviderTurnReceiptBinding[]
  provider_turn_receipts_hash: string
  provider_identity_status: 'verified' | 'unreported'
}

export type AgentCandidateOutputV3 = Omit<AgentCandidateOutputV2, 'schema'> & {
  schema: 'zhimeng.agent-candidate-output.v3'
  writer_task_id: string
  writer_task_hash: string
  instruction_materialization_id: string
  instruction_materialization_hash: string
  instruction_dispatch_receipt_id: string
  instruction_dispatch_receipt_hash: string
}

export type AgentCandidateOutput =
  | AgentCandidateOutputV1
  | AgentCandidateOutputV2
  | AgentCandidateOutputV3

export interface WriterTargetFile {
  file_id: string
  relative_path: string
  content_hash: string
  content_role: 'prose_body'
}

export interface WriterTask {
  schema: 'zhimeng.writer-task.v1'
  writer_task_id: string
  mode: WriterMode
  run_id: string
  workflow_plan_id: string
  workflow_plan_hash: string
  final_checkpoint_id: string
  final_checkpoint_hash: string
  creation_handoff_hash: string
  creation_handoff_status: 'blocked' | 'risk_draft_only'
  context_pack_id: string
  context_pack_hash: string
  route_receipt_id: string
  route_receipt_hash: string
  operation_package_id: string
  operation_package_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  active_chapter_id: string | null
  task_goal: string
  failure_signatures: string[]
  target_files: WriterTargetFile[]
  target_files_hash: string
  knowledge_fingerprint: string | null
  selected_card_ids: string[]
  selected_card_hashes: string[]
  formal_draft_allowed: false
  writes_project_truth: false
  requires_author_review: true
  created_at: string
  task_hash: string
}

export interface WriterCandidateResult {
  schema: 'zhimeng.writer-candidate-result.v1'
  writer_candidate_result_id: string
  mode: WriterMode
  run_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  writer_task_id: string
  writer_task_hash: string
  candidate_output_id: string
  candidate_output_hash: string
  candidate_kind: 'repair_plan' | 'change_proposal'
  change_set_id: string | null
  change_set_hash: string | null
  target_files: WriterTargetFile[]
  target_files_hash: string
  formal_draft_allowed: false
  writes_project_truth: false
  requires_author_review: true
  created_at: string
  result_hash: string
}

export interface CreationInstructionActivationScope {
  scope_type: 'application' | 'project'
  scope_key: string
  configured_state: 'unconfigured' | 'inherited' | 'enabled' | 'disabled'
  state_revision: number
  activation_receipt_id: string | null
  activation_receipt_hash: string | null
  configured_version_id: string | null
}

export interface CreationInstructionActivationBinding {
  schema: 'zhimeng.creation-instruction-activation.v1'
  activation_binding_id: string
  run_id: string
  project_id: string
  workflow_plan_id: string
  workflow_plan_hash: string
  skill_id: 'novel-creation-suite.writer-instruction'
  scope_chain: CreationInstructionActivationScope[]
  effective_scope: 'application' | 'project'
  skill_version_id: string
  skill_version: string
  manifest_fingerprint: string
  package_fingerprint: string
  instruction_map_path: 'instruction-map.json'
  instruction_map_hash: string
  created_at: string
  binding_hash: string
}

export interface CreationInstructionBlock {
  block_id: string
  title: string
  source_path: string
  source_line_start: number
  source_line_end: number
  source_byte_start: number
  source_byte_end: number
  utf8_bytes: number
  content_hash: string
  included: boolean
  content: string | null
  exclusion_reason: string | null
}

export interface CreationInstructionMaterialization {
  schema: 'zhimeng.creation-instruction-materialization.v1'
  materialization_id: string
  run_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  workflow_plan_id: string
  workflow_plan_hash: string
  final_checkpoint_id: string
  final_checkpoint_hash: string
  writer_task_id: string
  writer_task_hash: string
  activation_binding_id: string
  activation_binding_hash: string
  skill_version_id: string
  manifest_fingerprint: string
  package_fingerprint: string
  instruction_map_path: 'instruction-map.json'
  instruction_map_hash: string
  source_path: string
  source_hash: string
  blocks: CreationInstructionBlock[]
  blocks_hash: string
  included_block_ids: string[]
  provider_instruction: string
  provider_instruction_hash: string
  provider_instruction_utf8_bytes: number
  max_included_utf8_bytes: number
  formal_draft_allowed: false
  writes_project_truth: false
  requires_author_review: true
  created_at: string
  materialization_hash: string
}

export interface CreationInstructionDispatchReceipt {
  schema: 'zhimeng.creation-instruction-dispatch-receipt.v1'
  dispatch_receipt_id: string
  run_id: string
  workflow_plan_id: string
  workflow_plan_hash: string
  final_checkpoint_id: string
  final_checkpoint_hash: string
  writer_task_id: string
  writer_task_hash: string
  materialization_id: string
  materialization_hash: string
  provider_boundary_id: string
  provider_boundary_hash: string
  provider_request_hash: string
  provider_messages_hash: string
  tool_definitions_hash: string
  provider_dispatch_config_fingerprint: string
  automatic_provider_replay_count: 0
  status: 'committed_before_dispatch'
  formal_draft_allowed: false
  writes_project_truth: false
  created_at: string
  receipt_hash: string
}

export type ProviderRecoveryAction =
  | 'adopt_persisted_candidate'
  | 'abandon_and_recompile'
export type ProviderRecoveryCandidateState = 'persisted' | 'absent' | 'invalid'

export interface ProviderRecoveryDossier {
  schema: 'zhimeng.provider-recovery-dossier.v1'
  dossier_id: string
  run_id: string
  workflow_plan_id: string
  workflow_plan_hash: string
  final_checkpoint_id: string
  final_checkpoint_hash: string
  provider_boundary_id: string
  provider_boundary_hash: string
  provider_request_hash: string
  instruction_dispatch_receipt_id: string
  instruction_dispatch_receipt_hash: string
  context_pack_id: string
  context_pack_hash: string
  operation_package_id: string
  operation_package_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  run_status_at_capture: AgentRunStatus
  candidate_state: ProviderRecoveryCandidateState
  candidate_output_id: string | null
  candidate_output_hash: string | null
  remote_result: 'unknown'
  provider_replay_count: 0
  dossier_revision: number
  created_at: string
  dossier_hash: string
}

export interface ProviderRecoveryDecisionRequest {
  schema: 'zhimeng.provider-recovery-decision-request.v1'
  expected_dossier_revision: number
  expected_dossier_hash: string
  action: ProviderRecoveryAction
}

export interface ProviderRecoveryDecision {
  schema: 'zhimeng.provider-recovery-decision.v1'
  decision_id: string
  dossier_id: string
  run_id: string
  previous_dossier_hash: string
  instruction_dispatch_receipt_id: string
  instruction_dispatch_receipt_hash: string
  action: ProviderRecoveryAction
  previous_status: 'pending'
  resulting_run_status: AgentRunStatus
  previous_dossier_revision: number
  new_dossier_revision: number
  created_at: string
  decision_hash: string
}

export interface ProviderRecoveryResponse {
  schema: 'zhimeng.provider-recovery.v1'
  dossier: ProviderRecoveryDossier
  decision: ProviderRecoveryDecision | null
}

export interface AgentRun {
  schema: 'zhimeng.execution-run.v1'
  run_id: string
  session_id: string
  ordinal: number
  status: AgentRunStatus
  objective: string
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  executor: Record<string, unknown>
  capabilities: Record<string, unknown>
  project_revision: ProjectRevision
  correlation_id: string
  causation_id: string | null
  last_activity_sequence: number
  checkpoint_id: string | null
  error: AgentErrorEnvelope | null
}

export type AgentWorkflowDomainStatus = 'healthy' | 'degraded' | 'blocked'
export type AgentWorkflowExecutionStatus = 'succeeded' | 'failed' | 'cancelled'
export type AgentWorkflowResumeState =
  | 'running'
  | 'resumable'
  | 'blocked'
  | 'provider_dispatch_uncertain'
  | 'terminal'

export interface AgentWorkflowStep {
  step_id: string
  ordinal: number
  runner: 'managed_skill'
  skill_id: string
  expected_skill_version_id: string
  expected_skill_version: string
  expected_manifest_fingerprint: string
  expected_package_fingerprint: string
  input_contract: string
  output_contract: string
  input_builder_id: string
  input_builder_version: string
  max_attempts: 1
  continue_on_domain_status: AgentWorkflowDomainStatus[]
  formal_draft_allowed: false
  step_hash: string
}

export interface AgentWorkflowPlan {
  schema: 'zhimeng.workflow-plan.v1'
  workflow_plan_id: string
  run_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  context_pack_id: string
  context_pack_hash: string
  knowledge_fingerprint: string | null
  route_receipt_id: string
  route_receipt_hash: string
  operation_package_id: string
  operation_package_hash: string
  steps: AgentWorkflowStep[]
  formal_draft_allowed: false
  created_at: string
  plan_hash: string
}

export interface AgentWorkflowStepCheckpoint {
  schema: 'zhimeng.workflow-step-checkpoint.v1'
  checkpoint_id: string
  workflow_plan_id: string
  plan_hash: string
  run_id: string
  step_id: string
  step_hash: string
  step_ordinal: number
  runner: 'managed_skill'
  skill_id: string
  expected_skill_version_id: string
  expected_skill_version: string
  expected_manifest_fingerprint: string
  expected_package_fingerprint: string
  attempt_ordinal: 1
  previous_checkpoint_id: string | null
  previous_checkpoint_hash: string | null
  tool_call_id: string | null
  provider_tool_call_id: string | null
  skill_call_id: string | null
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  route_execution_receipt_id: string | null
  route_execution_receipt_hash: string | null
  input_hash: string | null
  output_hash: string | null
  tool_result_hash: string | null
  execution_status: AgentWorkflowExecutionStatus
  domain_status: AgentWorkflowDomainStatus | null
  error_code: string | null
  error_message: string | null
  formal_draft_allowed: false
  created_at: string
  checkpoint_hash: string
}

export interface AgentExecutorIdentity {
  executor_id: string
  kind: 'embedded' | 'process' | 'remote'
  implementation: string
  version: string
  protocol_version: string
  instance_id: string | null
}

export interface AgentProviderIoBoundary {
  schema: 'zhimeng.provider-io-boundary.v1'
  boundary_id: string
  workflow_plan_id: string
  plan_hash: string
  run_id: string
  request_hash: string
  provider_model: string | null
  provider_dispatch_config_fingerprint: string
  executor: AgentExecutorIdentity
  last_step_checkpoint_id: string
  last_step_checkpoint_hash: string
  status: 'dispatching'
  formal_draft_allowed: false
  created_at: string
  boundary_hash: string
}

export interface AgentWorkflowState {
  schema: 'zhimeng.agent-workflow-state.v1'
  run_id: string
  run_status: AgentRunStatus
  plan: AgentWorkflowPlan
  checkpoints: AgentWorkflowStepCheckpoint[]
  provider_boundary: AgentProviderIoBoundary | null
  last_checkpoint_id: string | null
  last_checkpoint_hash: string | null
  next_step_ordinal: number | null
  resume_state: AgentWorkflowResumeState
  resume_allowed: boolean
  reason_code: string
  formal_draft_allowed: false
}

export interface WriterTraceResponse {
  schema: 'zhimeng.writer-trace.v1'
  run_id: string
  workflow: AgentWorkflowState
  writer_task: WriterTask | null
  activation_binding: CreationInstructionActivationBinding | null
  materialization: CreationInstructionMaterialization | null
  dispatch_receipt: CreationInstructionDispatchReceipt | null
  candidate_output: AgentCandidateOutput | null
  writer_candidate_result: WriterCandidateResult | null
  provider_recovery: ProviderRecoveryResponse | null
}

export interface AgentRunResumeRequest {
  schema: 'zhimeng.agent-run-resume-request.v1'
  expected_workflow_plan_id: string
  expected_plan_hash: string
  expected_checkpoint_id: string | null
  expected_checkpoint_hash: string | null
}

export interface AgentRunResumeResponse {
  schema: 'zhimeng.agent-run-resume.v1'
  run: AgentRun
  accepted: true
  workflow_plan_id: string
  resumed_from_checkpoint_id: string | null
  resumed_from_checkpoint_hash: string | null
  next_action: 'workflow_step' | 'provider_io'
}

export class AgentContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AgentContractError'
  }
}

export type AgentJsonValue =
  | null
  | boolean
  | number
  | string
  | AgentJsonValue[]
  | { [key: string]: AgentJsonValue }

export interface AgentErrorEnvelope {
  schema?: string
  error: {
    code: string
    message: string
    request_id?: string | null
    retryable: boolean
  }
}

export interface AgentMessagePayload {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  name: string | null
  message_id: string | null
  phase: 'delta' | 'completed'
  delta: string | null
  format: 'plain' | 'markdown'
  finish_reason: string | null
  usage: Record<string, number> | null
}

export interface AgentRunStatePayload {
  status: AgentRunStatus
  previous_status: AgentRunStatus | null
  detail: string | null
}

export interface AgentToolCallPayload {
  tool_call_id: string
  tool_name: string
  arguments: AgentJsonValue
}

export interface AgentToolResultPayload {
  tool_call_id: string
  status: 'succeeded' | 'failed' | 'cancelled'
  output: AgentJsonValue
  error: AgentErrorEnvelope | null
  duration_ms: number | null
}

export interface AgentSkillPermissionSnapshot {
  read: ['bound_project_snapshot']
  write: ['run_staging']
  network: false
  secrets: []
}

export interface AgentSkillReceiptArtifact {
  artifact_type: string
  relative_path: string | null
  content_hash: string
}

export interface AgentSkillReceiptError {
  code: string
  message: string
  retryable: boolean
}

export interface AgentSkillExecutionReceipt {
  schema: 'zhimeng.skill-execution-receipt.v1'
  receipt_id: string
  run_id: string
  tool_call_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  skill_id: string
  skill_version: string
  skill_kind: 'deterministic_tool' | 'instruction_source'
  skill_version_id: string
  manifest_fingerprint: string
  package_fingerprint: string
  permissions: AgentSkillPermissionSnapshot
  permissions_hash: string
  input_hash: string
  output_hash: string
  execution_status: 'succeeded' | 'failed' | 'cancelled'
  domain_status: 'healthy' | 'degraded' | 'blocked' | null
  exit_code: number | null
  started_at: string
  finished_at: string
  duration_ms: number
  artifacts: AgentSkillReceiptArtifact[]
  error: AgentSkillReceiptError | null
}

export interface AgentSkillCallOutput {
  receipt: AgentSkillExecutionReceipt
  result_summary: string | null
}

export interface AgentSkillCallPayload {
  skill_call_id: string
  skill_id: string
  skill_version: string | null
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  input: AgentJsonValue
  output: AgentSkillCallOutput | null
}

export interface AgentChangeProposalPayload {
  proposal_id: string
  title: string
  summary: string
  base_revision: number
  base_fingerprint: string
  files: Array<{
    file_id: string | null
    path: string
    operation: 'create' | 'update' | 'delete'
    content_hash_before: string | null
    content_hash_after: string | null
  }>
}

export interface AgentApprovalRequestPayload {
  approval_id: string
  subject_kind: 'tool_call' | 'command' | 'change_proposal' | 'skill_call' | 'subagent'
  subject_id: string
  prompt: string
  options: Array<{
    option_id: string
    label: string
    effect: 'approve' | 'reject' | 'cancel' | 'custom'
  }>
  status: 'pending' | 'resolved' | 'expired' | 'cancelled'
  selected_option_id: string | null
}

export interface AgentArtifactPayload {
  artifact_id: string
  artifact_type: string
  name: string
  uri: string
  media_type: string | null
  content_hash: string | null
  size_bytes: number | null
}

export interface AgentCommandPayload {
  command_id: string
  command: string
  cwd: string | null
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  exit_code: number | null
  output_artifact_id: string | null
}

export interface AgentErrorPayload {
  error: AgentErrorEnvelope
  fatal: boolean
  scope: 'activity' | 'run' | 'session' | 'runtime'
}

interface AgentActivityBase {
  schema: 'zhimeng.activity-item.v1'
  activity_id: string
  session_id: string
  run_id: string
  sequence: number
  created_at: string
  project_revision: ProjectRevision
  correlation_id: string
  causation_id: string | null
}

export type AgentActivity = AgentActivityBase & (
  | { kind: 'message'; payload: AgentMessagePayload }
  | { kind: 'run_state'; payload: AgentRunStatePayload }
  | { kind: 'tool_call'; payload: AgentToolCallPayload }
  | { kind: 'tool_result'; payload: AgentToolResultPayload }
  | { kind: 'skill_call'; payload: AgentSkillCallPayload }
  | { kind: 'change_proposal'; payload: AgentChangeProposalPayload }
  | { kind: 'approval_request'; payload: AgentApprovalRequestPayload }
  | { kind: 'artifact'; payload: AgentArtifactPayload }
  | { kind: 'command'; payload: AgentCommandPayload }
  | { kind: 'error'; payload: AgentErrorPayload }
  | {
      kind: 'reasoning' | 'subagent' | 'compaction'
      payload: Record<string, unknown>
    }
)

export interface AgentRunEvent {
  schema: 'zhimeng.run-event.v1'
  event_id: string
  event_type: 'activity' | 'done'
  emitted_at: string
  data: { activity?: AgentActivity; run?: AgentRun }
}

interface SessionListResponse {
  sessions: AgentSession[]
}

export interface AgentMessagePage {
  messages: AgentMessage[]
  last_ordinal: number
}

interface RunListResponse {
  runs: AgentRun[]
}

export interface AgentActivityPage {
  schema: 'zhimeng.agent-activity-list.v1'
  run_id: string
  activities: AgentActivity[]
  last_sequence: number
}

interface RunCreateResponse {
  run: AgentRun
  user_message: AgentMessage
  replayed: boolean
}

function jsonRequest(body: unknown, method = 'POST', signal?: AbortSignal): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  }
}

function contractRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function invalidContract(detail: string): never {
  throw new AgentContractError(`Writing Agent 契约无效：${detail}`)
}

const sha256Pattern = /^[0-9a-f]{64}$/
const stableIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/
const candidateIdPattern = /^agent-candidate-[0-9a-f]{32}$/
const workflowPlanIdPattern = /^workflow-plan-[0-9a-f]{32}$/
const checkpointIdPattern = /^workflow-checkpoint-[0-9a-f]{32}$/
const providerBoundaryIdPattern = /^provider-io-[0-9a-f]{32}$/
const operationPackageIdPattern = /^prewrite-package-[0-9a-f]{32}$/
const providerRecoveryIdPattern = /^provider-recovery-[0-9a-f]{32}$/
const providerRecoveryDecisionIdPattern = /^provider-recovery-decision-[0-9a-f]{32}$/
const writerTaskIdPattern = /^writer-task-[0-9a-f]{32}$/
const writerCandidateResultIdPattern = /^writer-candidate-result-[0-9a-f]{32}$/
const changeSetIdPattern = /^changeset-[0-9a-f]{32}$/
const instructionActivationIdPattern = /^creation-instruction-activation-[0-9a-f]{32}$/
const instructionMaterializationIdPattern = /^creation-instruction-materialization-[0-9a-f]{32}$/
const instructionDispatchIdPattern = /^creation-instruction-dispatch-[0-9a-f]{32}$/
const skillVersionIdPattern = /^skill-version-[0-9a-f]{32}$/
const skillActivationIdPattern = /^skill-activation-[0-9a-f]{32}$/
const agentRunStatuses: readonly AgentRunStatus[] = [
  'queued', 'starting', 'running', 'awaiting_approval', 'paused', 'cancelling',
  'succeeded', 'failed', 'cancelled', 'interrupted',
]

function hasExactKeys(record: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(record).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function nonBlankString(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= maxLength
}

function sha256(value: unknown): value is string {
  return typeof value === 'string' && sha256Pattern.test(value)
}

function stableId(value: unknown): value is string {
  return typeof value === 'string' && stableIdPattern.test(value)
}

async function utf8Sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

const candidateOutputV1Keys = [
  'schema', 'candidate_output_id', 'run_id', 'workflow_plan_id', 'workflow_plan_hash',
  'final_checkpoint_id', 'final_checkpoint_hash', 'provider_boundary_id',
  'provider_boundary_hash', 'provider_request_hash', 'context_pack_id', 'context_pack_hash',
  'operation_package_id', 'operation_package_hash', 'project_id', 'project_revision',
  'project_fingerprint', 'handoff_status', 'allowed_output', 'failure_signatures', 'kind',
  'content', 'content_hash', 'references', 'references_hash', 'formal_draft_allowed',
  'requires_author_review', 'created_at', 'envelope_hash',
] as const
const candidateOutputV2Keys = [
  ...candidateOutputV1Keys,
  'provider_turn_receipts', 'provider_turn_receipts_hash', 'provider_identity_status',
] as const
const candidateOutputV3Keys = [
  ...candidateOutputV2Keys,
  'writer_task_id', 'writer_task_hash', 'instruction_materialization_id',
  'instruction_materialization_hash', 'instruction_dispatch_receipt_id',
  'instruction_dispatch_receipt_hash',
] as const
const candidateContentKeys = ['title', 'summary', 'body', 'format'] as const
const candidateReferenceKeys = ['reference_kind', 'reference_id', 'reference_hash'] as const
const providerTurnReceiptBindingKeys = ['turn_ordinal', 'receipt_id', 'receipt_hash'] as const
const providerTurnReceiptListKeys = [
  'schema', 'run_id', 'provider_boundary_id', 'provider_boundary_hash',
  'requested_model', 'provider_turn_receipts', 'provider_turn_receipts_hash',
  'provider_identity_status',
] as const
const providerTurnReceiptKeys = [
  'schema', 'receipt_id', 'run_id', 'provider_boundary_id', 'provider_boundary_hash',
  'turn_ordinal', 'request_hash', 'requested_model', 'reported_model',
  'accepted_reported_models', 'model_identity_status', 'finish_reason', 'usage',
  'created_at', 'receipt_hash',
] as const
const providerMessageUsageKeys = ['input_tokens', 'output_tokens', 'total_tokens'] as const
const providerRecoveryResponseKeys = ['schema', 'dossier', 'decision'] as const
const providerRecoveryDossierKeys = [
  'schema', 'dossier_id', 'run_id', 'workflow_plan_id', 'workflow_plan_hash',
  'final_checkpoint_id', 'final_checkpoint_hash', 'provider_boundary_id',
  'provider_boundary_hash', 'provider_request_hash', 'instruction_dispatch_receipt_id',
  'instruction_dispatch_receipt_hash', 'context_pack_id', 'context_pack_hash',
  'operation_package_id', 'operation_package_hash', 'project_id', 'project_revision',
  'project_fingerprint', 'run_status_at_capture', 'candidate_state', 'candidate_output_id',
  'candidate_output_hash', 'remote_result', 'provider_replay_count', 'dossier_revision',
  'created_at', 'dossier_hash',
] as const
const providerRecoveryDecisionKeys = [
  'schema', 'decision_id', 'dossier_id', 'run_id', 'previous_dossier_hash',
  'instruction_dispatch_receipt_id', 'instruction_dispatch_receipt_hash',
  'action', 'previous_status',
  'resulting_run_status', 'previous_dossier_revision', 'new_dossier_revision',
  'created_at', 'decision_hash',
] as const
const writerTargetFileKeys = [
  'file_id', 'relative_path', 'content_hash', 'content_role',
] as const
const writerTaskKeys = [
  'schema', 'writer_task_id', 'mode', 'run_id', 'workflow_plan_id',
  'workflow_plan_hash', 'final_checkpoint_id', 'final_checkpoint_hash',
  'creation_handoff_hash', 'creation_handoff_status', 'context_pack_id',
  'context_pack_hash', 'route_receipt_id', 'route_receipt_hash',
  'operation_package_id', 'operation_package_hash', 'project_id',
  'project_revision', 'project_fingerprint', 'active_chapter_id', 'task_goal',
  'failure_signatures', 'target_files', 'target_files_hash',
  'knowledge_fingerprint', 'selected_card_ids', 'selected_card_hashes',
  'formal_draft_allowed', 'writes_project_truth', 'requires_author_review',
  'created_at', 'task_hash',
] as const
const writerCandidateResultKeys = [
  'schema', 'writer_candidate_result_id', 'mode', 'run_id', 'project_id',
  'project_revision', 'project_fingerprint', 'writer_task_id', 'writer_task_hash',
  'candidate_output_id', 'candidate_output_hash', 'candidate_kind',
  'change_set_id', 'change_set_hash', 'target_files', 'target_files_hash',
  'formal_draft_allowed', 'writes_project_truth', 'requires_author_review',
  'created_at', 'result_hash',
] as const
const instructionActivationScopeKeys = [
  'scope_type', 'scope_key', 'configured_state', 'state_revision',
  'activation_receipt_id', 'activation_receipt_hash', 'configured_version_id',
] as const
const instructionActivationKeys = [
  'schema', 'activation_binding_id', 'run_id', 'project_id', 'workflow_plan_id',
  'workflow_plan_hash', 'skill_id', 'scope_chain', 'effective_scope',
  'skill_version_id', 'skill_version', 'manifest_fingerprint', 'package_fingerprint',
  'instruction_map_path', 'instruction_map_hash', 'created_at', 'binding_hash',
] as const
const instructionBlockKeys = [
  'block_id', 'title', 'source_path', 'source_line_start', 'source_line_end',
  'source_byte_start', 'source_byte_end', 'utf8_bytes', 'content_hash', 'included',
  'content', 'exclusion_reason',
] as const
const instructionMaterializationKeys = [
  'schema', 'materialization_id', 'run_id', 'project_id', 'project_revision',
  'project_fingerprint', 'workflow_plan_id', 'workflow_plan_hash',
  'final_checkpoint_id', 'final_checkpoint_hash', 'writer_task_id', 'writer_task_hash',
  'activation_binding_id', 'activation_binding_hash', 'skill_version_id',
  'manifest_fingerprint', 'package_fingerprint', 'instruction_map_path',
  'instruction_map_hash', 'source_path', 'source_hash', 'blocks', 'blocks_hash',
  'included_block_ids', 'provider_instruction', 'provider_instruction_hash',
  'provider_instruction_utf8_bytes', 'max_included_utf8_bytes',
  'formal_draft_allowed', 'writes_project_truth', 'requires_author_review',
  'created_at', 'materialization_hash',
] as const
const instructionDispatchKeys = [
  'schema', 'dispatch_receipt_id', 'run_id', 'workflow_plan_id',
  'workflow_plan_hash', 'final_checkpoint_id', 'final_checkpoint_hash',
  'writer_task_id', 'writer_task_hash', 'materialization_id', 'materialization_hash',
  'provider_boundary_id', 'provider_boundary_hash', 'provider_request_hash',
  'provider_messages_hash', 'tool_definitions_hash',
  'provider_dispatch_config_fingerprint', 'automatic_provider_replay_count', 'status',
  'formal_draft_allowed', 'writes_project_truth', 'created_at', 'receipt_hash',
] as const
const writerTraceKeys = [
  'schema', 'run_id', 'workflow', 'writer_task', 'activation_binding',
  'materialization', 'dispatch_receipt', 'candidate_output',
  'writer_candidate_result', 'provider_recovery',
] as const

async function validateAgentCandidateOutput(
  payload: unknown,
  requestedRunId: string,
): Promise<AgentCandidateOutput> {
  const output = contractRecord(payload)
  const candidateSchema = output?.schema
  const expectedKeys = candidateSchema === 'zhimeng.agent-candidate-output.v3'
    ? candidateOutputV3Keys
    : candidateSchema === 'zhimeng.agent-candidate-output.v2'
      ? candidateOutputV2Keys
    : candidateSchema === 'zhimeng.agent-candidate-output.v1'
      ? candidateOutputV1Keys
      : null
  if (!output || !expectedKeys || !hasExactKeys(output, expectedKeys)) {
    invalidContract('Candidate Output schema 或字段不匹配')
  }
  if (output.run_id !== requestedRunId || !stableId(output.run_id)) {
    invalidContract('Candidate Output 未绑定请求的 Run')
  }
  if (!candidateIdPattern.test(String(output.candidate_output_id))
    || !workflowPlanIdPattern.test(String(output.workflow_plan_id))
    || !checkpointIdPattern.test(String(output.final_checkpoint_id))
    || !providerBoundaryIdPattern.test(String(output.provider_boundary_id))
    || !operationPackageIdPattern.test(String(output.operation_package_id))) {
    invalidContract('Candidate Output Runtime 身份格式无效')
  }
  for (const [name, value] of Object.entries(output)) {
    if (name.endsWith('_hash') || name === 'content_hash' || name === 'references_hash' || name === 'envelope_hash') {
      if (!sha256(value)) invalidContract(`Candidate Output ${name} 不是有效 SHA-256`)
    }
  }
  if (!stableId(output.context_pack_id) || !stableId(output.project_id)
    || typeof output.project_revision !== 'number'
    || !Number.isInteger(output.project_revision) || output.project_revision < 1
    || !sha256(output.project_fingerprint)) {
    invalidContract('Candidate Output 项目或上下文来源绑定无效')
  }
  if (output.handoff_status !== 'blocked' && output.handoff_status !== 'risk_draft_only'
    || output.allowed_output !== 'repair_plan_or_risk_draft_only'
    || output.formal_draft_allowed !== false
    || output.requires_author_review !== true) {
    invalidContract('Candidate Output 正文授权或作者审查策略无效')
  }
  if (!Array.isArray(output.failure_signatures) || output.failure_signatures.length > 64
    || output.failure_signatures.some((value) => !nonBlankString(value, 256))
    || new Set(output.failure_signatures).size !== output.failure_signatures.length
    || (output.handoff_status === 'blocked' && output.failure_signatures.length === 0)) {
    invalidContract('Candidate Output 失败签名无效')
  }
  if (!['repair_plan', 'risk_draft', 'change_proposal', 'next_step_advice'].includes(String(output.kind))) {
    invalidContract('Candidate Output kind 无效')
  }
  const content = contractRecord(output.content)
  if (!content || !hasExactKeys(content, candidateContentKeys)
    || !nonBlankString(content.title, 240)
    || !nonBlankString(content.summary, 8_000)
    || !nonBlankString(content.body, 500_000)
    || content.format !== 'markdown') {
    invalidContract('Candidate Output 内容无效')
  }
  if (!Array.isArray(output.references) || output.references.length < 1 || output.references.length > 64) {
    invalidContract('Candidate Output 引用列表无效')
  }
  const referenceIdentities = new Set<string>()
  for (const value of output.references) {
    const reference = contractRecord(value)
    if (!reference || !hasExactKeys(reference, candidateReferenceKeys)
      || !['workflow_checkpoint', 'runtime_identity', 'artifact', 'change_set'].includes(String(reference.reference_kind))
      || !nonBlankString(reference.reference_id, 512)
      || !/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(reference.reference_id)
      || !sha256(reference.reference_hash)) {
      invalidContract('Candidate Output 引用字段无效')
    }
    const identity = `${reference.reference_kind}:${reference.reference_id}`
    if (referenceIdentities.has(identity)) invalidContract('Candidate Output 引用重复')
    referenceIdentities.add(identity)
    if (reference.reference_kind === 'workflow_checkpoint'
      && reference.reference_id === output.final_checkpoint_id
      && reference.reference_hash !== output.final_checkpoint_hash) {
      invalidContract('Candidate Output 最终 Checkpoint 引用 Hash 不匹配')
    }
    if (reference.reference_kind === 'runtime_identity') {
      const runtimeHashes = new Map([
        [output.context_pack_id, output.context_pack_hash],
        [output.operation_package_id, output.operation_package_hash],
      ])
      const envelopeIdentityMatches = (
        runtimeHashes.get(reference.reference_id) === reference.reference_hash
      )
      const writerTaskIdentity = writerTaskIdPattern.test(String(reference.reference_id))
      if (!envelopeIdentityMatches && !writerTaskIdentity) {
        invalidContract('Candidate Output Runtime 身份引用不匹配')
      }
    }
  }
  if (typeof output.created_at !== 'string'
    || Number.isNaN(Date.parse(output.created_at))
    || !/(?:Z|[+-]\d{2}:\d{2})$/.test(output.created_at)) {
    invalidContract('Candidate Output created_at 必须带时区')
  }

  if (output.schema === 'zhimeng.agent-candidate-output.v2'
    || output.schema === 'zhimeng.agent-candidate-output.v3') {
    if (!Array.isArray(output.provider_turn_receipts)
      || output.provider_turn_receipts.length < 1
      || output.provider_turn_receipts.length > 8) {
      invalidContract('Candidate Output Provider Receipt 数量无效')
    }
    const receiptIds = new Set<string>()
    for (const [index, value] of output.provider_turn_receipts.entries()) {
      const binding = contractRecord(value)
      if (!binding || !hasExactKeys(binding, providerTurnReceiptBindingKeys)
        || binding.turn_ordinal !== index + 1
        || !Number.isInteger(binding.turn_ordinal)
        || !/^provider-turn-receipt-[0-9a-f]{32}$/.test(String(binding.receipt_id))
        || !sha256(binding.receipt_hash)
        || receiptIds.has(String(binding.receipt_id))) {
        invalidContract('Candidate Output Provider Receipt 顺序、身份或 Hash 无效')
      }
      receiptIds.add(String(binding.receipt_id))
    }
    if (output.provider_turn_receipts_hash
      !== await canonicalJsonHash(output.provider_turn_receipts)) {
      invalidContract('Candidate Output Provider Receipt 链 Hash 不匹配')
    }
    if (output.provider_identity_status !== 'verified'
      && output.provider_identity_status !== 'unreported') {
      invalidContract('Candidate Output Provider 身份状态无效')
    }
  }

  if (output.schema === 'zhimeng.agent-candidate-output.v3') {
    if (!writerTaskIdPattern.test(String(output.writer_task_id))
      || !sha256(output.writer_task_hash)
      || !instructionMaterializationIdPattern.test(String(output.instruction_materialization_id))
      || !sha256(output.instruction_materialization_hash)
      || !instructionDispatchIdPattern.test(String(output.instruction_dispatch_receipt_id))
      || !sha256(output.instruction_dispatch_receipt_hash)) {
      invalidContract('Candidate Output v3 Writer 或指令调度绑定无效')
    }
    const writerReferences = output.references.filter((value) => {
      const reference = contractRecord(value)
      return reference?.reference_kind === 'runtime_identity'
    })
    const instructionReferences = output.references.filter((value) => {
      const reference = contractRecord(value)
      return reference?.reference_kind === 'artifact'
    })
    if (writerReferences.length !== 1
      || writerReferences[0]?.reference_id !== output.writer_task_id
      || writerReferences[0]?.reference_hash !== output.writer_task_hash
      || instructionReferences.length !== 1
      || instructionReferences[0]?.reference_id !== output.instruction_materialization_id
      || instructionReferences[0]?.reference_hash !== output.instruction_materialization_hash) {
      invalidContract('Candidate Output v3 缺少唯一 Writer Task 或指令物化引用')
    }
  }

  const expectedCandidateId = `agent-candidate-${(await canonicalJsonHash({
    schema: output.schema,
    run_id: output.run_id,
    provider_boundary_id: output.provider_boundary_id,
  })).slice(0, 32)}`
  if (output.candidate_output_id !== expectedCandidateId) {
    invalidContract('Candidate Output 身份不匹配 Provider boundary')
  }
  if (output.content_hash !== await canonicalJsonHash(content)) {
    invalidContract('Candidate Output content_hash 不匹配内容')
  }
  if (output.references_hash !== await canonicalJsonHash(output.references)) {
    invalidContract('Candidate Output references_hash 不匹配引用')
  }
  const { envelope_hash: _envelopeHash, ...envelopeMaterial } = output
  if (output.envelope_hash !== await canonicalJsonHash(envelopeMaterial)) {
    invalidContract('Candidate Output envelope_hash 不匹配 Envelope')
  }
  return output as unknown as AgentCandidateOutput
}

function providerModelName(value: unknown): value is string {
  return nonBlankString(value, 512) && value === value.trim()
}

function validateProviderMessageUsage(value: unknown): ProviderMessageUsage | null {
  if (value === null) return null
  const usage = contractRecord(value)
  if (!usage || !hasExactKeys(usage, providerMessageUsageKeys)) {
    invalidContract('Provider Receipt usage 字段无效')
  }
  const inputTokens = usage.input_tokens
  const outputTokens = usage.output_tokens
  const totalTokens = usage.total_tokens
  if (typeof inputTokens !== 'number' || !Number.isInteger(inputTokens) || inputTokens < 0
    || typeof outputTokens !== 'number' || !Number.isInteger(outputTokens) || outputTokens < 0
    || typeof totalTokens !== 'number' || !Number.isInteger(totalTokens)
    || totalTokens < inputTokens + outputTokens) {
    invalidContract('Provider Receipt token 用量无效')
  }
  return usage as unknown as ProviderMessageUsage
}

function expectedProviderIdentityStatus(
  requestedModel: string | null,
  reportedModel: string | null,
  acceptedReportedModels: string[],
): ProviderModelIdentityStatus {
  if (reportedModel === null) return 'unreported'
  if (requestedModel !== null && reportedModel === requestedModel) return 'exact_match'
  if (acceptedReportedModels.includes(reportedModel)) return 'accepted_alias'
  return 'rejected_mismatch'
}

async function validateProviderTurnReceipt(
  value: unknown,
  requestedRunId: string,
  providerBoundaryId: string,
  providerBoundaryHash: string,
  requestedModel: string | null,
  expectedOrdinal: number,
): Promise<ProviderTurnReceipt> {
  const receipt = contractRecord(value)
  if (!receipt || !hasExactKeys(receipt, providerTurnReceiptKeys)
    || receipt.schema !== 'zhimeng.provider-turn-receipt.v1'
    || receipt.run_id !== requestedRunId
    || receipt.provider_boundary_id !== providerBoundaryId
    || receipt.provider_boundary_hash !== providerBoundaryHash
    || receipt.turn_ordinal !== expectedOrdinal
    || !Number.isInteger(receipt.turn_ordinal)
    || !/^provider-turn-receipt-[0-9a-f]{32}$/.test(String(receipt.receipt_id))
    || !sha256(receipt.provider_boundary_hash)
    || !sha256(receipt.request_hash)
    || !sha256(receipt.receipt_hash)) {
    invalidContract('Provider Receipt schema、回合或 Boundary 绑定无效')
  }
  if ((receipt.requested_model !== null && !providerModelName(receipt.requested_model))
    || receipt.requested_model !== requestedModel
    || (receipt.reported_model !== null && !providerModelName(receipt.reported_model))) {
    invalidContract('Provider Receipt 请求或报告模型无效')
  }
  if (!Array.isArray(receipt.accepted_reported_models)
    || receipt.accepted_reported_models.length > 64
    || receipt.accepted_reported_models.some((model) => !providerModelName(model))) {
    invalidContract('Provider Receipt 模型别名列表无效')
  }
  const aliases = receipt.accepted_reported_models as string[]
  const sortedAliases = [...new Set(aliases)].sort()
  if (aliases.length !== sortedAliases.length
    || aliases.some((alias, index) => alias !== sortedAliases[index])) {
    invalidContract('Provider Receipt 模型别名必须排序且唯一')
  }
  const identityStatus = expectedProviderIdentityStatus(
    requestedModel,
    receipt.reported_model as string | null,
    aliases,
  )
  if (receipt.model_identity_status !== identityStatus) {
    invalidContract('Provider Receipt 模型身份状态与证据不一致')
  }
  if (!nonBlankString(receipt.finish_reason, 128)
    || receipt.finish_reason !== receipt.finish_reason.trim()
    || !timezoneDateTime(receipt.created_at)) {
    invalidContract('Provider Receipt 完成原因或时间无效')
  }
  validateProviderMessageUsage(receipt.usage)
  const expectedReceiptId = `provider-turn-receipt-${(await canonicalJsonHash({
    schema: 'zhimeng.provider-turn-receipt.v1',
    run_id: receipt.run_id,
    provider_boundary_id: receipt.provider_boundary_id,
    turn_ordinal: receipt.turn_ordinal,
    request_hash: receipt.request_hash,
  })).slice(0, 32)}`
  if (receipt.receipt_id !== expectedReceiptId) {
    invalidContract('Provider Receipt 身份与不可变输入不匹配')
  }
  const { receipt_hash: _receiptHash, ...receiptMaterial } = receipt
  if (receipt.receipt_hash !== await canonicalJsonHash(receiptMaterial)) {
    invalidContract('Provider Receipt Hash 不匹配')
  }
  return receipt as unknown as ProviderTurnReceipt
}

async function validateProviderTurnReceiptList(
  payload: unknown,
  requestedRunId: string,
): Promise<ProviderTurnReceiptListResponse> {
  const response = contractRecord(payload)
  if (!response || !hasExactKeys(response, providerTurnReceiptListKeys)
    || response.schema !== 'zhimeng.provider-turn-receipt-list.v1'
    || response.run_id !== requestedRunId || !stableId(response.run_id)
    || !providerBoundaryIdPattern.test(String(response.provider_boundary_id))
    || !sha256(response.provider_boundary_hash)
    || !sha256(response.provider_turn_receipts_hash)
    || (response.requested_model !== null && !providerModelName(response.requested_model))
    || !Array.isArray(response.provider_turn_receipts)
    || response.provider_turn_receipts.length > 8) {
    invalidContract('Provider Receipt List schema、Run 或 Boundary 无效')
  }
  const requestedModel = response.requested_model as string | null
  const receipts: ProviderTurnReceipt[] = []
  for (const [index, value] of response.provider_turn_receipts.entries()) {
    receipts.push(await validateProviderTurnReceipt(
      value,
      requestedRunId,
      String(response.provider_boundary_id),
      String(response.provider_boundary_hash),
      requestedModel,
      index + 1,
    ))
  }
  const bindings = receipts.map((receipt) => ({
    turn_ordinal: receipt.turn_ordinal,
    receipt_id: receipt.receipt_id,
    receipt_hash: receipt.receipt_hash,
  }))
  if (response.provider_turn_receipts_hash !== await canonicalJsonHash(bindings)) {
    invalidContract('Provider Receipt List 链 Hash 不匹配')
  }
  const aggregateStatus: ProviderIdentityAggregateStatus = receipts.length === 0
    ? 'pending'
    : receipts.some((receipt) => receipt.model_identity_status === 'rejected_mismatch')
      ? 'rejected_mismatch'
      : receipts.some((receipt) => receipt.model_identity_status === 'unreported')
        ? 'unreported'
        : 'verified'
  if (response.provider_identity_status !== aggregateStatus) {
    invalidContract('Provider Receipt List 聚合身份状态不匹配')
  }
  return response as unknown as ProviderTurnReceiptListResponse
}

export function assertProviderTurnReceiptBindings(
  candidate: AgentCandidateOutput,
  evidence: ProviderTurnReceiptListResponse,
): void {
  if (candidate.schema === 'zhimeng.agent-candidate-output.v1') {
    invalidContract('历史 Candidate v1 没有可交叉校验的 Provider Receipt binding')
  }
  const bindingsMatch = candidate.provider_turn_receipts.length
      === evidence.provider_turn_receipts.length
    && candidate.provider_turn_receipts.every((binding, index) => {
      const receipt = evidence.provider_turn_receipts[index]
      return receipt?.turn_ordinal === binding.turn_ordinal
        && receipt.receipt_id === binding.receipt_id
        && receipt.receipt_hash === binding.receipt_hash
    })
  if (evidence.run_id !== candidate.run_id
    || evidence.provider_boundary_id !== candidate.provider_boundary_id
    || evidence.provider_boundary_hash !== candidate.provider_boundary_hash
    || evidence.provider_turn_receipts_hash !== candidate.provider_turn_receipts_hash
    || evidence.provider_identity_status !== candidate.provider_identity_status
    || evidence.provider_turn_receipts[0]?.request_hash !== candidate.provider_request_hash
    || !bindingsMatch) {
    invalidContract('Provider Receipt List 未绑定同一 Candidate、Boundary 与首轮请求')
  }
}

function timezoneDateTime(value: unknown): value is string {
  return typeof value === 'string'
    && !Number.isNaN(Date.parse(value))
    && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
}

async function validateWriterTargetFiles(value: unknown): Promise<WriterTargetFile[]> {
  if (!Array.isArray(value) || value.length > 32) {
    invalidContract('Writer target_files 数量无效')
  }
  const targets: WriterTargetFile[] = []
  for (const item of value) {
    const target = contractRecord(item)
    if (!target || !hasExactKeys(target, writerTargetFileKeys)
      || !stableId(target.file_id)
      || !nonBlankString(target.relative_path, 512)
      || !String(target.relative_path).startsWith('05_正文内容/')
      || !/\.(?:md|txt)$/i.test(String(target.relative_path))
      || !sha256(target.content_hash)
      || target.content_role !== 'prose_body') {
      invalidContract('Writer prose target 无效或越过章节目录')
    }
    targets.push(target as unknown as WriterTargetFile)
  }
  const canonical = [...targets].sort((left, right) => {
    const leftPath = left.relative_path.toLowerCase()
    const rightPath = right.relative_path.toLowerCase()
    return leftPath < rightPath ? -1 : leftPath > rightPath ? 1 : 0
  })
  if (targets.some((target, index) => target.relative_path !== canonical[index]?.relative_path)
    || new Set(targets.map((target) => target.file_id)).size !== targets.length
    || new Set(targets.map((target) => target.relative_path.toLowerCase())).size !== targets.length) {
    invalidContract('Writer prose target 未按规范排序或存在重复')
  }
  return targets
}

async function validateWriterTask(
  payload: unknown,
  requestedRunId: string,
): Promise<WriterTask> {
  const task = contractRecord(payload)
  if (!task || !hasExactKeys(task, writerTaskKeys)
    || task.schema !== 'zhimeng.writer-task.v1'
    || task.run_id !== requestedRunId || !stableId(task.run_id)
    || !writerTaskIdPattern.test(String(task.writer_task_id))
    || !workflowPlanIdPattern.test(String(task.workflow_plan_id))
    || !checkpointIdPattern.test(String(task.final_checkpoint_id))
    || !/^nlcl-route-[0-9a-f]{32}$/.test(String(task.route_receipt_id))
    || !operationPackageIdPattern.test(String(task.operation_package_id))
    || !stableId(task.context_pack_id) || !stableId(task.project_id)) {
    invalidContract('Writer Task schema 或 Runtime 身份无效')
  }
  for (const name of [
    'workflow_plan_hash', 'final_checkpoint_hash', 'creation_handoff_hash',
    'context_pack_hash', 'route_receipt_hash', 'operation_package_hash',
    'project_fingerprint', 'target_files_hash', 'task_hash',
  ]) {
    if (!sha256(task[name])) invalidContract(`Writer Task ${name} 无效`)
  }
  if (task.knowledge_fingerprint !== null && !sha256(task.knowledge_fingerprint)) {
    invalidContract('Writer Task knowledge_fingerprint 无效')
  }
  if (!['repair_only', 'author_review_change_proposal'].includes(String(task.mode))
    || !['blocked', 'risk_draft_only'].includes(String(task.creation_handoff_status))
    || typeof task.project_revision !== 'number' || !Number.isInteger(task.project_revision)
    || task.project_revision < 1
    || (task.active_chapter_id !== null && !nonBlankString(task.active_chapter_id, 256))
    || !nonBlankString(task.task_goal, 100_000)
    || task.formal_draft_allowed !== false || task.writes_project_truth !== false
    || task.requires_author_review !== true || !timezoneDateTime(task.created_at)) {
    invalidContract('Writer Task 模式、项目版本或审阅策略无效')
  }
  if (!Array.isArray(task.failure_signatures) || task.failure_signatures.length > 64
    || task.failure_signatures.some((item) => !nonBlankString(item, 256))
    || new Set(task.failure_signatures).size !== task.failure_signatures.length
    || !Array.isArray(task.selected_card_ids) || task.selected_card_ids.length > 24
    || task.selected_card_ids.some((item) => !nonBlankString(item, 256))
    || !Array.isArray(task.selected_card_hashes)
    || task.selected_card_hashes.some((item) => !sha256(item))
    || task.selected_card_ids.length !== task.selected_card_hashes.length
    || new Set(task.selected_card_ids).size !== task.selected_card_ids.length
    || task.selected_card_ids.some((item, index) => item !== [...(task.selected_card_ids as unknown[])].sort()[index])) {
    invalidContract('Writer Task 失败签名或知识卡绑定无效')
  }
  if ((task.selected_card_ids.length > 0 && task.knowledge_fingerprint === null)
    || (task.knowledge_fingerprint === null && task.selected_card_hashes.length > 0)) {
    invalidContract('Writer Task 知识卡缺少知识指纹')
  }
  const targets = await validateWriterTargetFiles(task.target_files)
  if (task.target_files_hash !== await canonicalJsonHash(targets)) {
    invalidContract('Writer Task target_files_hash 不匹配')
  }
  if (task.mode === 'author_review_change_proposal'
    && (task.active_chapter_id === null || targets.length === 0)) {
    invalidContract('正文候选 Writer Task 缺少活动章节或 prose target')
  }
  const expectedId = `writer-task-${(await canonicalJsonHash({
    schema: 'zhimeng.writer-task.v1',
    run_id: task.run_id,
    final_checkpoint_id: task.final_checkpoint_id,
    creation_handoff_hash: task.creation_handoff_hash,
    mode: task.mode,
    target_files_hash: task.target_files_hash,
  })).slice(0, 32)}`
  if (task.writer_task_id !== expectedId) invalidContract('Writer Task 身份不匹配')
  const { task_hash: _taskHash, ...taskMaterial } = task
  if (task.task_hash !== await canonicalJsonHash(taskMaterial)) {
    invalidContract('Writer Task task_hash 不匹配')
  }
  return task as unknown as WriterTask
}

async function validateWriterCandidateResult(
  payload: unknown,
  requestedRunId: string,
): Promise<WriterCandidateResult> {
  const result = contractRecord(payload)
  if (!result || !hasExactKeys(result, writerCandidateResultKeys)
    || result.schema !== 'zhimeng.writer-candidate-result.v1'
    || result.run_id !== requestedRunId || !stableId(result.run_id)
    || !writerCandidateResultIdPattern.test(String(result.writer_candidate_result_id))
    || !writerTaskIdPattern.test(String(result.writer_task_id))
    || !candidateIdPattern.test(String(result.candidate_output_id))
    || !stableId(result.project_id)) {
    invalidContract('Writer Candidate Result schema 或 Runtime 身份无效')
  }
  for (const name of [
    'project_fingerprint', 'writer_task_hash', 'candidate_output_hash',
    'target_files_hash', 'result_hash',
  ]) {
    if (!sha256(result[name])) invalidContract(`Writer Candidate Result ${name} 无效`)
  }
  if (result.change_set_hash !== null && !sha256(result.change_set_hash)) {
    invalidContract('Writer Candidate Result change_set_hash 无效')
  }
  if (!['repair_only', 'author_review_change_proposal'].includes(String(result.mode))
    || !['repair_plan', 'change_proposal'].includes(String(result.candidate_kind))
    || typeof result.project_revision !== 'number' || !Number.isInteger(result.project_revision)
    || result.project_revision < 1
    || result.formal_draft_allowed !== false || result.writes_project_truth !== false
    || result.requires_author_review !== true || !timezoneDateTime(result.created_at)
    || (result.change_set_id === null) !== (result.change_set_hash === null)
    || (result.change_set_id !== null && !changeSetIdPattern.test(String(result.change_set_id)))) {
    invalidContract('Writer Candidate Result 模式、ChangeSet 或审阅策略无效')
  }
  const targets = await validateWriterTargetFiles(result.target_files)
  if (result.target_files_hash !== await canonicalJsonHash(targets)) {
    invalidContract('Writer Candidate Result target_files_hash 不匹配')
  }
  if (result.mode === 'repair_only'
    ? result.candidate_kind !== 'repair_plan' || result.change_set_id !== null || targets.length !== 0
    : result.candidate_kind !== 'change_proposal' || result.change_set_id === null || targets.length === 0) {
    invalidContract('Writer Candidate Result 与 Writer 模式不一致')
  }
  const expectedId = `writer-candidate-result-${(await canonicalJsonHash({
    schema: 'zhimeng.writer-candidate-result.v1',
    writer_task_id: result.writer_task_id,
    candidate_output_id: result.candidate_output_id,
    change_set_id: result.change_set_id,
  })).slice(0, 32)}`
  if (result.writer_candidate_result_id !== expectedId) {
    invalidContract('Writer Candidate Result 身份不匹配')
  }
  const { result_hash: _resultHash, ...resultMaterial } = result
  if (result.result_hash !== await canonicalJsonHash(resultMaterial)) {
    invalidContract('Writer Candidate Result result_hash 不匹配')
  }
  return result as unknown as WriterCandidateResult
}

function relativePackagePath(value: unknown): value is string {
  if (!nonBlankString(value, 1_024) || value.includes('\\') || value.startsWith('/')) return false
  return value.split('/').every((part) => part !== '' && part !== '.' && part !== '..')
}

function validateInstructionActivationScope(value: unknown): CreationInstructionActivationScope {
  const scope = contractRecord(value)
  if (!scope || !hasExactKeys(scope, instructionActivationScopeKeys)
    || !['application', 'project'].includes(String(scope.scope_type))
    || !nonBlankString(scope.scope_key, 512)
    || !['unconfigured', 'inherited', 'enabled', 'disabled'].includes(String(scope.configured_state))
    || typeof scope.state_revision !== 'number' || !Number.isInteger(scope.state_revision)
    || scope.state_revision < 0) {
    invalidContract('Creation Instruction activation scope 无效')
  }
  if ((scope.scope_type === 'application' && scope.scope_key !== 'application')
    || (scope.scope_type === 'project' && !String(scope.scope_key).startsWith('project:'))) {
    invalidContract('Creation Instruction activation scope key 无效')
  }
  const receiptPaired = (scope.activation_receipt_id === null)
    === (scope.activation_receipt_hash === null)
  if (!receiptPaired
    || (scope.activation_receipt_id !== null
      && (!skillActivationIdPattern.test(String(scope.activation_receipt_id))
        || !sha256(scope.activation_receipt_hash)))
    || (scope.configured_version_id !== null
      && !skillVersionIdPattern.test(String(scope.configured_version_id)))) {
    invalidContract('Creation Instruction activation scope receipt 无效')
  }
  if (scope.configured_state === 'unconfigured') {
    if (scope.state_revision !== 0 || scope.activation_receipt_id !== null
      || scope.configured_version_id !== null) {
      invalidContract('未配置的 Creation Instruction scope 不能声明激活证据')
    }
  } else if (scope.state_revision < 1 || scope.activation_receipt_id === null) {
    invalidContract('已配置的 Creation Instruction scope 缺少决议收据')
  }
  if ((scope.configured_state === 'enabled') !== (scope.configured_version_id !== null)
    || (scope.scope_type === 'application'
      && ['unconfigured', 'inherited'].includes(String(scope.configured_state)))) {
    invalidContract('Creation Instruction activation scope 状态与版本不一致')
  }
  return scope as unknown as CreationInstructionActivationScope
}

async function validateCreationInstructionActivation(
  value: unknown,
  requestedRunId: string,
): Promise<CreationInstructionActivationBinding> {
  const binding = contractRecord(value)
  if (!binding || !hasExactKeys(binding, instructionActivationKeys)
    || binding.schema !== 'zhimeng.creation-instruction-activation.v1'
    || binding.run_id !== requestedRunId || !stableId(binding.run_id)
    || !stableId(binding.project_id)
    || !instructionActivationIdPattern.test(String(binding.activation_binding_id))
    || !workflowPlanIdPattern.test(String(binding.workflow_plan_id))
    || binding.skill_id !== 'novel-creation-suite.writer-instruction'
    || !['application', 'project'].includes(String(binding.effective_scope))
    || !skillVersionIdPattern.test(String(binding.skill_version_id))
    || !/^[0-9A-Za-z][0-9A-Za-z._+-]*$/.test(String(binding.skill_version))
    || !nonBlankString(binding.skill_version, 128)
    || binding.instruction_map_path !== 'instruction-map.json'
    || !timezoneDateTime(binding.created_at)) {
    invalidContract('Creation Instruction Activation schema 或身份无效')
  }
  for (const name of [
    'workflow_plan_hash', 'manifest_fingerprint', 'package_fingerprint',
    'instruction_map_hash', 'binding_hash',
  ]) {
    if (!sha256(binding[name])) invalidContract(`Creation Instruction Activation ${name} 无效`)
  }
  if (!Array.isArray(binding.scope_chain)
    || binding.scope_chain.length < 1 || binding.scope_chain.length > 2) {
    invalidContract('Creation Instruction Activation scope chain 数量无效')
  }
  const scopeChain = binding.scope_chain.map(validateInstructionActivationScope)
  const effective = scopeChain.at(-1)
  if (!effective
    || scopeChain[0]?.scope_type !== 'project'
    || effective.scope_type !== binding.effective_scope
    || effective.configured_state !== 'enabled'
    || effective.configured_version_id !== binding.skill_version_id
    || (binding.effective_scope === 'project' && scopeChain.length !== 1)
    || (binding.effective_scope === 'application'
      && (scopeChain.length !== 2 || scopeChain[1]?.scope_type !== 'application'
        || !['unconfigured', 'inherited'].includes(scopeChain[0]?.configured_state ?? '')))) {
    invalidContract('Creation Instruction Activation scope chain 无效')
  }
  const expectedId = `creation-instruction-activation-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-activation.v1',
    run_id: binding.run_id,
    workflow_plan_id: binding.workflow_plan_id,
    workflow_plan_hash: binding.workflow_plan_hash,
    scope_chain: scopeChain,
    skill_version_id: binding.skill_version_id,
    package_fingerprint: binding.package_fingerprint,
    instruction_map_hash: binding.instruction_map_hash,
  })).slice(0, 32)}`
  if (binding.activation_binding_id !== expectedId) {
    invalidContract('Creation Instruction Activation identity 不匹配')
  }
  const { binding_hash: _bindingHash, ...bindingMaterial } = binding
  if (binding.binding_hash !== await canonicalJsonHash(bindingMaterial)) {
    invalidContract('Creation Instruction Activation binding_hash 不匹配')
  }
  return { ...binding, scope_chain: scopeChain } as unknown as CreationInstructionActivationBinding
}

async function validateCreationInstructionBlock(value: unknown): Promise<CreationInstructionBlock> {
  const block = contractRecord(value)
  if (!block || !hasExactKeys(block, instructionBlockKeys)
    || !/^[a-z0-9][a-z0-9._-]*[a-z0-9]$/.test(String(block.block_id))
    || !nonBlankString(block.title, 256) || !relativePackagePath(block.source_path)
    || typeof block.source_line_start !== 'number' || !Number.isInteger(block.source_line_start)
    || block.source_line_start < 1
    || typeof block.source_line_end !== 'number' || !Number.isInteger(block.source_line_end)
    || block.source_line_end < block.source_line_start
    || typeof block.source_byte_start !== 'number' || !Number.isInteger(block.source_byte_start)
    || block.source_byte_start < 0
    || typeof block.source_byte_end !== 'number' || !Number.isInteger(block.source_byte_end)
    || block.source_byte_end <= block.source_byte_start
    || typeof block.utf8_bytes !== 'number' || !Number.isInteger(block.utf8_bytes)
    || block.utf8_bytes < 1 || block.source_byte_end - block.source_byte_start !== block.utf8_bytes
    || !sha256(block.content_hash) || typeof block.included !== 'boolean') {
    invalidContract('Creation Instruction materialized block 无效')
  }
  if (block.included) {
    if (typeof block.content !== 'string' || block.content.length === 0
      || block.exclusion_reason !== null) {
      invalidContract('包含的 Creation Instruction block 缺少正文')
    }
    const bytes = new TextEncoder().encode(block.content)
    if (bytes.length !== block.utf8_bytes
      || block.content_hash !== await utf8Sha256(block.content)) {
      invalidContract('Creation Instruction block 内容字节数或 Hash 不匹配')
    }
  } else if (block.content !== null || !nonBlankString(block.exclusion_reason, 512)) {
    invalidContract('排除的 Creation Instruction block 缺少原因')
  }
  return block as unknown as CreationInstructionBlock
}

async function validateCreationInstructionMaterialization(
  value: unknown,
  requestedRunId: string,
): Promise<CreationInstructionMaterialization> {
  const materialization = contractRecord(value)
  if (!materialization || !hasExactKeys(materialization, instructionMaterializationKeys)
    || materialization.schema !== 'zhimeng.creation-instruction-materialization.v1'
    || materialization.run_id !== requestedRunId || !stableId(materialization.run_id)
    || !stableId(materialization.project_id)
    || !instructionMaterializationIdPattern.test(String(materialization.materialization_id))
    || !workflowPlanIdPattern.test(String(materialization.workflow_plan_id))
    || !checkpointIdPattern.test(String(materialization.final_checkpoint_id))
    || !writerTaskIdPattern.test(String(materialization.writer_task_id))
    || !instructionActivationIdPattern.test(String(materialization.activation_binding_id))
    || !skillVersionIdPattern.test(String(materialization.skill_version_id))
    || materialization.instruction_map_path !== 'instruction-map.json'
    || !relativePackagePath(materialization.source_path)
    || typeof materialization.project_revision !== 'number'
    || !Number.isInteger(materialization.project_revision) || materialization.project_revision < 1
    || materialization.formal_draft_allowed !== false
    || materialization.writes_project_truth !== false
    || materialization.requires_author_review !== true
    || !timezoneDateTime(materialization.created_at)) {
    invalidContract('Creation Instruction Materialization schema 或身份无效')
  }
  for (const name of [
    'project_fingerprint', 'workflow_plan_hash', 'final_checkpoint_hash',
    'writer_task_hash', 'activation_binding_hash', 'manifest_fingerprint',
    'package_fingerprint', 'instruction_map_hash', 'source_hash', 'blocks_hash',
    'provider_instruction_hash', 'materialization_hash',
  ]) {
    if (!sha256(materialization[name])) {
      invalidContract(`Creation Instruction Materialization ${name} 无效`)
    }
  }
  if (!Array.isArray(materialization.blocks)
    || materialization.blocks.length < 2 || materialization.blocks.length > 64) {
    invalidContract('Creation Instruction Materialization blocks 数量无效')
  }
  const blocks: CreationInstructionBlock[] = []
  for (const value of materialization.blocks) {
    blocks.push(await validateCreationInstructionBlock(value))
  }
  if (new Set(blocks.map((block) => block.block_id)).size !== blocks.length
    || materialization.blocks_hash !== await canonicalJsonHash(blocks)) {
    invalidContract('Creation Instruction Materialization blocks 重复或 Hash 不匹配')
  }
  const includedIds = blocks.filter((block) => block.included).map((block) => block.block_id)
  if (!Array.isArray(materialization.included_block_ids)
    || materialization.included_block_ids.length < 1
    || materialization.included_block_ids.length > 32
    || materialization.included_block_ids.some((value, index) => value !== includedIds[index])
    || materialization.included_block_ids.length !== includedIds.length) {
    invalidContract('Creation Instruction Materialization included blocks 不匹配')
  }
  const providerInstruction = blocks
    .filter((block) => block.included)
    .map((block) => block.content ?? '')
    .join('')
  const providerBytes = new TextEncoder().encode(providerInstruction)
  if (materialization.provider_instruction !== providerInstruction
    || typeof materialization.provider_instruction_utf8_bytes !== 'number'
    || !Number.isInteger(materialization.provider_instruction_utf8_bytes)
    || materialization.provider_instruction_utf8_bytes !== providerBytes.length
    || typeof materialization.max_included_utf8_bytes !== 'number'
    || !Number.isInteger(materialization.max_included_utf8_bytes)
    || materialization.max_included_utf8_bytes < 1
    || materialization.max_included_utf8_bytes > 200_000
    || providerBytes.length > materialization.max_included_utf8_bytes
    || materialization.provider_instruction_hash !== await utf8Sha256(providerInstruction)) {
    invalidContract('Creation Instruction Materialization Provider instruction 无效')
  }
  const expectedId = `creation-instruction-materialization-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-materialization.v1',
    run_id: materialization.run_id,
    writer_task_id: materialization.writer_task_id,
    activation_binding_id: materialization.activation_binding_id,
    blocks_hash: materialization.blocks_hash,
    provider_instruction_hash: materialization.provider_instruction_hash,
  })).slice(0, 32)}`
  if (materialization.materialization_id !== expectedId) {
    invalidContract('Creation Instruction Materialization identity 不匹配')
  }
  const { materialization_hash: _materializationHash, ...materializationMaterial } = materialization
  if (materialization.materialization_hash !== await canonicalJsonHash(materializationMaterial)) {
    invalidContract('Creation Instruction Materialization hash 不匹配')
  }
  return { ...materialization, blocks } as unknown as CreationInstructionMaterialization
}

async function validateCreationInstructionDispatch(
  value: unknown,
  requestedRunId: string,
): Promise<CreationInstructionDispatchReceipt> {
  const receipt = contractRecord(value)
  if (!receipt || !hasExactKeys(receipt, instructionDispatchKeys)
    || receipt.schema !== 'zhimeng.creation-instruction-dispatch-receipt.v1'
    || receipt.run_id !== requestedRunId || !stableId(receipt.run_id)
    || !instructionDispatchIdPattern.test(String(receipt.dispatch_receipt_id))
    || !workflowPlanIdPattern.test(String(receipt.workflow_plan_id))
    || !checkpointIdPattern.test(String(receipt.final_checkpoint_id))
    || !writerTaskIdPattern.test(String(receipt.writer_task_id))
    || !instructionMaterializationIdPattern.test(String(receipt.materialization_id))
    || !providerBoundaryIdPattern.test(String(receipt.provider_boundary_id))
    || receipt.automatic_provider_replay_count !== 0
    || receipt.status !== 'committed_before_dispatch'
    || receipt.formal_draft_allowed !== false || receipt.writes_project_truth !== false
    || !timezoneDateTime(receipt.created_at)) {
    invalidContract('Creation Instruction Dispatch Receipt schema 或身份无效')
  }
  for (const name of [
    'workflow_plan_hash', 'final_checkpoint_hash', 'writer_task_hash',
    'materialization_hash', 'provider_boundary_hash', 'provider_request_hash',
    'provider_messages_hash', 'tool_definitions_hash',
    'provider_dispatch_config_fingerprint', 'receipt_hash',
  ]) {
    if (!sha256(receipt[name])) {
      invalidContract(`Creation Instruction Dispatch Receipt ${name} 无效`)
    }
  }
  const expectedId = `creation-instruction-dispatch-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-dispatch-receipt.v1',
    run_id: receipt.run_id,
    provider_boundary_id: receipt.provider_boundary_id,
    materialization_id: receipt.materialization_id,
  })).slice(0, 32)}`
  if (receipt.dispatch_receipt_id !== expectedId) {
    invalidContract('Creation Instruction Dispatch Receipt identity 不匹配')
  }
  const { receipt_hash: _receiptHash, ...receiptMaterial } = receipt
  if (receipt.receipt_hash !== await canonicalJsonHash(receiptMaterial)) {
    invalidContract('Creation Instruction Dispatch Receipt hash 不匹配')
  }
  return receipt as unknown as CreationInstructionDispatchReceipt
}

async function validateProviderRecoveryDossier(
  value: unknown,
  requestedRunId: string,
): Promise<ProviderRecoveryDossier> {
  const dossier = contractRecord(value)
  if (!dossier || !hasExactKeys(dossier, providerRecoveryDossierKeys)
    || dossier.schema !== 'zhimeng.provider-recovery-dossier.v1'
    || dossier.run_id !== requestedRunId || !stableId(dossier.run_id)) {
    invalidContract('Provider Recovery Dossier schema 或 Run 绑定无效')
  }
  if (!providerRecoveryIdPattern.test(String(dossier.dossier_id))
    || !workflowPlanIdPattern.test(String(dossier.workflow_plan_id))
    || !checkpointIdPattern.test(String(dossier.final_checkpoint_id))
    || !providerBoundaryIdPattern.test(String(dossier.provider_boundary_id))
    || !instructionDispatchIdPattern.test(String(dossier.instruction_dispatch_receipt_id))
    || !operationPackageIdPattern.test(String(dossier.operation_package_id))
    || !stableId(dossier.context_pack_id) || !stableId(dossier.project_id)) {
    invalidContract('Provider Recovery Dossier 来源身份格式无效')
  }
  for (const name of [
    'workflow_plan_hash', 'final_checkpoint_hash', 'provider_boundary_hash',
    'provider_request_hash', 'instruction_dispatch_receipt_hash', 'context_pack_hash',
    'operation_package_hash', 'project_fingerprint', 'dossier_hash',
  ]) {
    if (!sha256(dossier[name])) invalidContract(`Provider Recovery Dossier ${name} 无效`)
  }
  if (!agentRunStatuses.includes(dossier.run_status_at_capture as AgentRunStatus)
    || typeof dossier.project_revision !== 'number' || !Number.isInteger(dossier.project_revision)
    || dossier.project_revision < 1
    || typeof dossier.dossier_revision !== 'number' || !Number.isInteger(dossier.dossier_revision)
    || dossier.dossier_revision < 1
    || dossier.remote_result !== 'unknown' || dossier.provider_replay_count !== 0
    || !timezoneDateTime(dossier.created_at)) {
    invalidContract('Provider Recovery Dossier 状态或版本无效')
  }
  if (!['persisted', 'absent', 'invalid'].includes(String(dossier.candidate_state))) {
    invalidContract('Provider Recovery Dossier 候选状态无效')
  }
  if (dossier.candidate_state === 'persisted') {
    if (!candidateIdPattern.test(String(dossier.candidate_output_id))
      || !sha256(dossier.candidate_output_hash)) {
      invalidContract('Provider Recovery Dossier 缺少持久化候选身份')
    }
  } else if (dossier.candidate_output_id !== null || dossier.candidate_output_hash !== null) {
    invalidContract('Provider Recovery Dossier 暴露了不可采用的候选身份')
  }
  const expectedDossierId = `provider-recovery-${(await canonicalJsonHash({
    schema: 'zhimeng.provider-recovery-dossier.v1',
    run_id: dossier.run_id,
    provider_boundary_id: dossier.provider_boundary_id,
  })).slice(0, 32)}`
  if (dossier.dossier_id !== expectedDossierId) {
    invalidContract('Provider Recovery Dossier 身份不匹配 Provider boundary')
  }
  const { dossier_hash: _dossierHash, ...dossierMaterial } = dossier
  if (dossier.dossier_hash !== await canonicalJsonHash(dossierMaterial)) {
    invalidContract('Provider Recovery Dossier hash 不匹配')
  }
  return dossier as unknown as ProviderRecoveryDossier
}

async function validateProviderRecoveryDecision(
  value: unknown,
  dossier: ProviderRecoveryDossier,
): Promise<ProviderRecoveryDecision> {
  const decision = contractRecord(value)
  if (!decision || !hasExactKeys(decision, providerRecoveryDecisionKeys)
    || decision.schema !== 'zhimeng.provider-recovery-decision.v1'
    || decision.run_id !== dossier.run_id || decision.dossier_id !== dossier.dossier_id
    || !providerRecoveryDecisionIdPattern.test(String(decision.decision_id))
    || decision.previous_dossier_hash !== dossier.dossier_hash
    || !sha256(decision.previous_dossier_hash)
    || decision.instruction_dispatch_receipt_id !== dossier.instruction_dispatch_receipt_id
    || !instructionDispatchIdPattern.test(String(decision.instruction_dispatch_receipt_id))
    || decision.instruction_dispatch_receipt_hash !== dossier.instruction_dispatch_receipt_hash
    || !sha256(decision.instruction_dispatch_receipt_hash)
    || !['adopt_persisted_candidate', 'abandon_and_recompile'].includes(String(decision.action))
    || decision.previous_status !== 'pending'
    || !agentRunStatuses.includes(decision.resulting_run_status as AgentRunStatus)
    || typeof decision.previous_dossier_revision !== 'number'
    || !Number.isInteger(decision.previous_dossier_revision)
    || decision.previous_dossier_revision !== dossier.dossier_revision
    || typeof decision.new_dossier_revision !== 'number'
    || decision.new_dossier_revision !== decision.previous_dossier_revision + 1
    || !timezoneDateTime(decision.created_at)
    || Date.parse(String(decision.created_at)) < Date.parse(dossier.created_at)
    || !sha256(decision.decision_hash)
    || (decision.action === 'adopt_persisted_candidate'
      ? decision.resulting_run_status !== 'succeeded'
      : decision.resulting_run_status !== 'failed')) {
    invalidContract('Provider Recovery Decision 身份、状态或版本无效')
  }
  const expectedDecisionId = `provider-recovery-decision-${(await canonicalJsonHash({
    schema: 'zhimeng.provider-recovery-decision.v1',
    dossier_id: decision.dossier_id,
    previous_dossier_hash: decision.previous_dossier_hash,
    instruction_dispatch_receipt_id: decision.instruction_dispatch_receipt_id,
    instruction_dispatch_receipt_hash: decision.instruction_dispatch_receipt_hash,
    action: decision.action,
    previous_dossier_revision: decision.previous_dossier_revision,
  })).slice(0, 32)}`
  if (decision.decision_id !== expectedDecisionId) {
    invalidContract('Provider Recovery Decision 身份不匹配 CAS 输入')
  }
  const { decision_hash: _decisionHash, ...decisionMaterial } = decision
  if (decision.decision_hash !== await canonicalJsonHash(decisionMaterial)) {
    invalidContract('Provider Recovery Decision hash 不匹配')
  }
  return decision as unknown as ProviderRecoveryDecision
}

async function validateProviderRecoveryResponse(
  payload: unknown,
  requestedRunId: string,
): Promise<ProviderRecoveryResponse> {
  const response = contractRecord(payload)
  if (!response || !hasExactKeys(response, providerRecoveryResponseKeys)
    || response.schema !== 'zhimeng.provider-recovery.v1') {
    invalidContract('Provider Recovery 响应 schema 或字段无效')
  }
  const dossier = await validateProviderRecoveryDossier(response.dossier, requestedRunId)
  const decision = response.decision === null
    ? null
    : await validateProviderRecoveryDecision(response.decision, dossier)
  return { schema: 'zhimeng.provider-recovery.v1', dossier, decision }
}

function assertWriterTraceEvidenceBindings(trace: WriterTraceResponse): void {
  const { workflow, writer_task: task, activation_binding: activation } = trace
  const { materialization, dispatch_receipt: dispatch, candidate_output: candidate } = trace
  const { writer_candidate_result: result, provider_recovery: recovery } = trace
  const plan = workflow.plan
  const boundary = workflow.provider_boundary
  const finalCheckpoint = workflow.checkpoints.at(-1) ?? null

  if (task && (task.run_id !== trace.run_id
    || task.workflow_plan_id !== plan.workflow_plan_id
    || task.workflow_plan_hash !== plan.plan_hash
    || task.final_checkpoint_id !== finalCheckpoint?.checkpoint_id
    || task.final_checkpoint_hash !== finalCheckpoint?.checkpoint_hash
    || task.context_pack_id !== plan.context_pack_id
    || task.context_pack_hash !== plan.context_pack_hash
    || task.operation_package_id !== plan.operation_package_id
    || task.operation_package_hash !== plan.operation_package_hash
    || task.project_id !== plan.project_id
    || task.project_revision !== plan.project_revision
    || task.project_fingerprint !== plan.project_fingerprint)) {
    invalidContract('Writer Task 未绑定聚合 Workflow 与项目快照')
  }
  if (activation && (activation.run_id !== trace.run_id
    || activation.project_id !== plan.project_id
    || activation.workflow_plan_id !== plan.workflow_plan_id
    || activation.workflow_plan_hash !== plan.plan_hash)) {
    invalidContract('Creation Instruction Activation 未绑定聚合 Workflow')
  }
  if (materialization && (!task || !activation
    || materialization.run_id !== trace.run_id
    || materialization.project_id !== plan.project_id
    || materialization.project_revision !== plan.project_revision
    || materialization.project_fingerprint !== plan.project_fingerprint
    || materialization.workflow_plan_id !== plan.workflow_plan_id
    || materialization.workflow_plan_hash !== plan.plan_hash
    || materialization.final_checkpoint_id !== finalCheckpoint?.checkpoint_id
    || materialization.final_checkpoint_hash !== finalCheckpoint?.checkpoint_hash
    || materialization.writer_task_id !== task.writer_task_id
    || materialization.writer_task_hash !== task.task_hash
    || materialization.activation_binding_id !== activation.activation_binding_id
    || materialization.activation_binding_hash !== activation.binding_hash
    || materialization.skill_version_id !== activation.skill_version_id
    || materialization.manifest_fingerprint !== activation.manifest_fingerprint
    || materialization.package_fingerprint !== activation.package_fingerprint
    || materialization.instruction_map_hash !== activation.instruction_map_hash)) {
    invalidContract('Creation Instruction Materialization 未绑定激活决议、Writer Task 或项目快照')
  }
  if (dispatch && (!task || !materialization || !boundary
    || dispatch.run_id !== trace.run_id
    || dispatch.workflow_plan_id !== plan.workflow_plan_id
    || dispatch.workflow_plan_hash !== plan.plan_hash
    || dispatch.final_checkpoint_id !== finalCheckpoint?.checkpoint_id
    || dispatch.final_checkpoint_hash !== finalCheckpoint?.checkpoint_hash
    || dispatch.writer_task_id !== task.writer_task_id
    || dispatch.writer_task_hash !== task.task_hash
    || dispatch.materialization_id !== materialization.materialization_id
    || dispatch.materialization_hash !== materialization.materialization_hash
    || dispatch.provider_boundary_id !== boundary.boundary_id
    || dispatch.provider_boundary_hash !== boundary.boundary_hash
    || dispatch.provider_request_hash !== boundary.request_hash
    || dispatch.provider_dispatch_config_fingerprint
      !== boundary.provider_dispatch_config_fingerprint)) {
    invalidContract('Creation Instruction Dispatch 未绑定物化指令或 Provider boundary')
  }
  if (candidate && (!boundary
    || candidate.run_id !== trace.run_id
    || candidate.workflow_plan_id !== plan.workflow_plan_id
    || candidate.workflow_plan_hash !== plan.plan_hash
    || candidate.final_checkpoint_id !== finalCheckpoint?.checkpoint_id
    || candidate.final_checkpoint_hash !== finalCheckpoint?.checkpoint_hash
    || candidate.provider_boundary_id !== boundary.boundary_id
    || candidate.provider_boundary_hash !== boundary.boundary_hash
    || candidate.provider_request_hash !== boundary.request_hash
    || candidate.context_pack_id !== plan.context_pack_id
    || candidate.context_pack_hash !== plan.context_pack_hash
    || candidate.operation_package_id !== plan.operation_package_id
    || candidate.operation_package_hash !== plan.operation_package_hash
    || candidate.project_id !== plan.project_id
    || candidate.project_revision !== plan.project_revision
    || candidate.project_fingerprint !== plan.project_fingerprint)) {
    invalidContract('Candidate Output 未绑定聚合 Workflow、Provider boundary 或项目快照')
  }
  if (candidate?.schema === 'zhimeng.agent-candidate-output.v3'
    && (!task || !materialization || !dispatch
      || candidate.writer_task_id !== task.writer_task_id
      || candidate.writer_task_hash !== task.task_hash
      || candidate.instruction_materialization_id !== materialization.materialization_id
      || candidate.instruction_materialization_hash !== materialization.materialization_hash
      || candidate.instruction_dispatch_receipt_id !== dispatch.dispatch_receipt_id
      || candidate.instruction_dispatch_receipt_hash !== dispatch.receipt_hash)) {
    invalidContract('Candidate Output v3 未绑定同一 Writer 指令调度链')
  }
  const dossier = recovery?.dossier
  if (dossier && (!dispatch || !boundary
    || dossier.workflow_plan_id !== plan.workflow_plan_id
    || dossier.workflow_plan_hash !== plan.plan_hash
    || dossier.final_checkpoint_id !== finalCheckpoint?.checkpoint_id
    || dossier.final_checkpoint_hash !== finalCheckpoint?.checkpoint_hash
    || dossier.provider_boundary_id !== boundary.boundary_id
    || dossier.provider_boundary_hash !== boundary.boundary_hash
    || dossier.provider_request_hash !== boundary.request_hash
    || dossier.instruction_dispatch_receipt_id !== dispatch.dispatch_receipt_id
    || dossier.instruction_dispatch_receipt_hash !== dispatch.receipt_hash
    || dossier.context_pack_id !== plan.context_pack_id
    || dossier.context_pack_hash !== plan.context_pack_hash
    || dossier.operation_package_id !== plan.operation_package_id
    || dossier.operation_package_hash !== plan.operation_package_hash
    || dossier.project_id !== plan.project_id
    || dossier.project_revision !== plan.project_revision
    || dossier.project_fingerprint !== plan.project_fingerprint)) {
    invalidContract('Provider Recovery 未绑定同一 Workflow、调度收据或项目快照')
  }
  if (result && (!task || !candidate)) {
    invalidContract('Writer Result 缺少同一聚合响应中的 Writer Task 或 Candidate')
  }
  if (task && result) assertWriterTraceBindings(task, result, candidate)
}

async function validateWriterTraceResponse(
  payload: unknown,
  requestedRunId: string,
): Promise<WriterTraceResponse> {
  const response = contractRecord(payload)
  if (!response || !hasExactKeys(response, writerTraceKeys)
    || response.schema !== 'zhimeng.writer-trace.v1'
    || response.run_id !== requestedRunId || !stableId(response.run_id)) {
    invalidContract('Writer Trace 聚合响应 schema、字段或 Run 无效')
  }
  const workflow = validateWorkflowState(response.workflow, requestedRunId)
  const writerTask = response.writer_task === null
    ? null
    : await validateWriterTask(response.writer_task, requestedRunId)
  const activationBinding = response.activation_binding === null
    ? null
    : await validateCreationInstructionActivation(response.activation_binding, requestedRunId)
  const materialization = response.materialization === null
    ? null
    : await validateCreationInstructionMaterialization(response.materialization, requestedRunId)
  const dispatchReceipt = response.dispatch_receipt === null
    ? null
    : await validateCreationInstructionDispatch(response.dispatch_receipt, requestedRunId)
  const candidateOutput = response.candidate_output === null
    ? null
    : await validateAgentCandidateOutput(response.candidate_output, requestedRunId)
  const writerCandidateResult = response.writer_candidate_result === null
    ? null
    : await validateWriterCandidateResult(response.writer_candidate_result, requestedRunId)
  const providerRecovery = response.provider_recovery === null
    ? null
    : await validateProviderRecoveryResponse(response.provider_recovery, requestedRunId)
  const trace: WriterTraceResponse = {
    schema: 'zhimeng.writer-trace.v1',
    run_id: requestedRunId,
    workflow,
    writer_task: writerTask,
    activation_binding: activationBinding,
    materialization,
    dispatch_receipt: dispatchReceipt,
    candidate_output: candidateOutput,
    writer_candidate_result: writerCandidateResult,
    provider_recovery: providerRecovery,
  }
  assertWriterTraceEvidenceBindings(trace)
  return trace
}

function validateWorkflowState(payload: unknown, requestedRunId: string): AgentWorkflowState {
  const state = contractRecord(payload)
  if (!state || state.schema !== 'zhimeng.agent-workflow-state.v1') {
    invalidContract('Workflow 响应 schema 不匹配')
  }
  const plan = contractRecord(state.plan)
  if (!plan || plan.schema !== 'zhimeng.workflow-plan.v1') {
    invalidContract('Workflow Plan 缺失')
  }
  if (state.run_id !== requestedRunId || plan.run_id !== requestedRunId) {
    invalidContract('Workflow 绑定了其他 Run')
  }
  const workflowPlanId = plan.workflow_plan_id
  const planHash = plan.plan_hash
  if (typeof workflowPlanId !== 'string' || typeof planHash !== 'string') {
    invalidContract('Workflow Plan 身份缺失')
  }
  if (!Array.isArray(state.checkpoints)) {
    invalidContract('Workflow Checkpoint 列表缺失')
  }
  const checkpoints = state.checkpoints.map((value) => {
    const checkpoint = contractRecord(value)
    if (!checkpoint
      || checkpoint.schema !== 'zhimeng.workflow-step-checkpoint.v1'
      || checkpoint.run_id !== requestedRunId
      || checkpoint.workflow_plan_id !== workflowPlanId
      || checkpoint.plan_hash !== planHash
      || typeof checkpoint.checkpoint_id !== 'string'
      || typeof checkpoint.checkpoint_hash !== 'string') {
      invalidContract('Workflow Checkpoint 身份不匹配')
    }
    return checkpoint
  })
  const lastCheckpoint = checkpoints.at(-1) ?? null
  const expectedCheckpointId = lastCheckpoint?.checkpoint_id ?? null
  const expectedCheckpointHash = lastCheckpoint?.checkpoint_hash ?? null
  if (state.last_checkpoint_id !== expectedCheckpointId
    || state.last_checkpoint_hash !== expectedCheckpointHash) {
    invalidContract('Workflow Checkpoint 游标不匹配')
  }
  if (state.provider_boundary !== null) {
    const boundary = contractRecord(state.provider_boundary)
    const executor = contractRecord(boundary?.executor)
    if (!boundary
      || boundary.schema !== 'zhimeng.provider-io-boundary.v1'
      || boundary.run_id !== requestedRunId
      || boundary.workflow_plan_id !== workflowPlanId
      || boundary.plan_hash !== planHash
      || boundary.last_step_checkpoint_id !== expectedCheckpointId
      || boundary.last_step_checkpoint_hash !== expectedCheckpointHash
      || typeof boundary.provider_dispatch_config_fingerprint !== 'string'
      || executor?.instance_id !== `provider-config-${boundary.provider_dispatch_config_fingerprint}`) {
      invalidContract('Provider boundary 身份不匹配')
    }
  }
  if (state.resume_allowed === true && (
    state.run_status !== 'paused'
    || state.resume_state !== 'resumable'
    || state.provider_boundary !== null
  )) {
    invalidContract('Workflow Resume 条件自相矛盾')
  }
  return payload as AgentWorkflowState
}

function validateRunResumeResponse(
  payload: unknown,
  requestedRunId: string,
  request: AgentRunResumeRequest,
): AgentRunResumeResponse {
  const response = contractRecord(payload)
  const run = contractRecord(response?.run)
  if (!response
    || response.schema !== 'zhimeng.agent-run-resume.v1'
    || response.accepted !== true
    || !run
    || run.run_id !== requestedRunId
    || run.status !== 'running'
    || response.workflow_plan_id !== request.expected_workflow_plan_id
    || response.resumed_from_checkpoint_id !== request.expected_checkpoint_id
    || response.resumed_from_checkpoint_hash !== request.expected_checkpoint_hash
    || !['workflow_step', 'provider_io'].includes(String(response.next_action))) {
    invalidContract('Resume 响应未确认提交的 Run、Plan 或 Checkpoint')
  }
  return payload as AgentRunResumeResponse
}

export function getAgentStatus(
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentStatus> {
  return fetchRuntimeJson('/api/v1/agent/status', options, { signal })
}

export async function listAgentSessions(
  projectId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession[]> {
  const payload = await fetchRuntimeJson<SessionListResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
    options,
    { signal },
  )
  return payload.sessions
}

export function createAgentSession(
  projectId: string,
  title: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
    options,
    jsonRequest({ title }, 'POST', signal),
  )
}

export function getAgentSession(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}`,
    options,
    { signal },
  )
}

export async function listAgentMessages(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentMessagePage> {
  return fetchRuntimeJson<AgentMessagePage>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/messages?limit=500`,
    options,
    { signal },
  )
}

export async function listAgentRuns(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRun[]> {
  const payload = await fetchRuntimeJson<RunListResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
    options,
    { signal },
  )
  return payload.runs
}

export function listAgentActivities(
  runId: string,
  afterSequence = 0,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentActivityPage> {
  return fetchRuntimeJson(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/activities?after_sequence=${afterSequence}`,
    options,
    { signal },
  )
}

export function createAgentRun(
  sessionId: string,
  request: {
    message: string
    expected_project_revision: number
    expected_project_fingerprint: string
    active_chapter_id: string | null
    writer_mode: WriterMode
    context_pack_id: string
    expected_context_pack_hash: string
  },
  idempotencyKey: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<RunCreateResponse> {
  const init = jsonRequest(request, 'POST', signal)
  const headers = new Headers(init.headers)
  headers.set('Idempotency-Key', idempotencyKey)
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs`,
    options,
    { ...init, headers },
  )
}

export async function getAgentRun(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRun> {
  const payload = await fetchRuntimeJson<{ run: AgentRun }>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}`,
    options,
    { signal },
  )
  return payload.run
}

export async function getAgentWorkflowState(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentWorkflowState> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/workflow`,
    options,
    { signal },
  )
  return validateWorkflowState(payload, runId)
}

export async function getAgentCandidateOutput(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentCandidateOutput> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/candidate-output`,
    options,
    { signal },
  )
  return validateAgentCandidateOutput(payload, runId)
}

export async function getProviderTurnReceipts(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ProviderTurnReceiptListResponse> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/provider-turn-receipts`,
    options,
    { signal },
  )
  return validateProviderTurnReceiptList(payload, runId)
}

export async function getWriterTask(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<WriterTask> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/writer-task`,
    options,
    { signal },
  )
  return validateWriterTask(payload, runId)
}

export async function getWriterCandidateResult(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<WriterCandidateResult> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/writer-candidate-result`,
    options,
    { signal },
  )
  return validateWriterCandidateResult(payload, runId)
}

export async function getWriterTrace(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<WriterTraceResponse> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/writer-trace`,
    options,
    { signal },
  )
  return validateWriterTraceResponse(payload, runId)
}

export function assertWriterTraceBindings(
  task: WriterTask,
  result: WriterCandidateResult,
  candidate: AgentCandidateOutput | null = null,
): void {
  if (result.run_id !== task.run_id
    || result.mode !== task.mode
    || result.project_id !== task.project_id
    || result.project_revision !== task.project_revision
    || result.project_fingerprint !== task.project_fingerprint
    || result.writer_task_id !== task.writer_task_id
    || result.writer_task_hash !== task.task_hash
    || result.target_files_hash !== task.target_files_hash) {
    invalidContract('Writer Result 未绑定同一 Writer Task 与项目快照')
  }
  if (!candidate) return
  const taskReferences = candidate.references.filter((reference) => (
    reference.reference_kind === 'runtime_identity'
    && reference.reference_id === task.writer_task_id
  ))
  const changeSetReferences = candidate.references.filter((reference) => (
    reference.reference_kind === 'change_set'
  ))
  if (result.candidate_output_id !== candidate.candidate_output_id
    || result.candidate_output_hash !== candidate.envelope_hash
    || result.candidate_kind !== candidate.kind
    || candidate.run_id !== task.run_id
    || candidate.workflow_plan_id !== task.workflow_plan_id
    || candidate.workflow_plan_hash !== task.workflow_plan_hash
    || candidate.final_checkpoint_id !== task.final_checkpoint_id
    || candidate.final_checkpoint_hash !== task.final_checkpoint_hash
    || candidate.context_pack_id !== task.context_pack_id
    || candidate.context_pack_hash !== task.context_pack_hash
    || candidate.operation_package_id !== task.operation_package_id
    || candidate.operation_package_hash !== task.operation_package_hash
    || candidate.project_id !== task.project_id
    || candidate.project_revision !== task.project_revision
    || candidate.project_fingerprint !== task.project_fingerprint
    || candidate.handoff_status !== task.creation_handoff_status
    || taskReferences.length !== 1
    || taskReferences[0]?.reference_hash !== task.task_hash
    || (task.mode === 'repair_only'
      ? changeSetReferences.length !== 0
      : changeSetReferences.length !== 1
        || changeSetReferences[0]?.reference_id !== result.change_set_id
        || changeSetReferences[0]?.reference_hash !== result.change_set_hash)) {
    invalidContract('Writer Result、Candidate Output 与 Writer Task 来源链不一致')
  }
}

export async function getProviderRecovery(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ProviderRecoveryResponse> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/provider-recovery`,
    options,
    { signal },
  )
  return validateProviderRecoveryResponse(payload, runId)
}

export async function decideProviderRecovery(
  runId: string,
  request: ProviderRecoveryDecisionRequest,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ProviderRecoveryResponse> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/provider-recovery/decision`,
    options,
    jsonRequest(request, 'POST', signal),
  )
  return validateProviderRecoveryResponse(payload, runId)
}

export async function resumeAgentRun(
  runId: string,
  request: AgentRunResumeRequest,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRunResumeResponse> {
  const payload = await fetchRuntimeJson<unknown>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/resume`,
    options,
    jsonRequest(request, 'POST', signal),
  )
  return validateRunResumeResponse(payload, runId, request)
}

export async function cancelAgentRun(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRun> {
  const payload = await fetchRuntimeJson<{ run: AgentRun }>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/cancel`,
    options,
    { method: 'POST', signal },
  )
  return payload.run
}

export function streamAgentRunEvents(
  runId: string,
  options: RuntimeClientOptions & {
    signal: AbortSignal
    lastEventId?: string | null
    onEvent: (event: AgentRunEvent) => void
  },
): Promise<void> {
  return fetchRuntimeSse(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/events`,
    {
      ...options,
      onEvent: (event) => {
        const payload = JSON.parse(event.data) as AgentRunEvent
        if (
          payload.schema !== 'zhimeng.run-event.v1' ||
          !['activity', 'done'].includes(payload.event_type)
        ) {
          throw new Error('Runtime emitted an invalid Agent event')
        }
        options.onEvent(payload)
      },
    },
  )
}

export function isTerminalAgentRun(status: AgentRunStatus): boolean {
  return ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(status)
}
