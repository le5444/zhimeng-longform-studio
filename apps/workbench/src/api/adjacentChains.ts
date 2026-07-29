import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type AdjacentChainStatus =
  | 'awaiting_upstream'
  | 'prepared'
  | 'approved'
  | 'rejected'
  | 'disabled'
  | 'running'
  | 'succeeded'
  | 'failed'

export type AdjacentChainStepStatus =
  | 'pending'
  | 'prepared'
  | 'approved'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'

export interface AdjacentChainStep {
  step_id: string
  ordinal: number
  title: string
  skill_id: string
  capability: string
  status: AdjacentChainStepStatus
  child_id: string | null
  result_hash: string | null
  error_code: string | null
  error_message: string | null
}

export interface AdjacentChainSkillRisk {
  step_id: string
  skill_id: string
  capability: string
  skill_version_id: string
  skill_version: string
  manifest_fingerprint: string
  package_fingerprint: string
  effective_scope: 'application' | 'project'
  input_contract: string
  output_contract: string
}

export interface AdjacentChainRiskDossier {
  formal_draft_allowed: false
  network: false
  secrets: string[]
  permissions: {
    read: string[]
    write: string[]
    network: false
    secrets: string[]
  }
  skills: [AdjacentChainSkillRisk, AdjacentChainSkillRisk]
  risk_summary: string
  not_claiming: string[]
}

export interface AdjacentChainRiskApprovalToken {
  token_id: string
  chain_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  dossier: AdjacentChainRiskDossier
  approved_by: 'author'
  approved_at: string
  consumed: boolean
  token_hash: string
}


export interface AdjacentWorkflowPlanStep {
  workflow_step_id: string
  step_hash: string
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
  formal_draft_allowed: false
}

export interface AdjacentWorkflowPlan {
  schema: 'zhimeng.adjacent-workflow-plan.v1'
  workflow_plan_id: string
  chain_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  risk_token_id: string
  risk_token_hash: string
  steps: [AdjacentWorkflowPlanStep, AdjacentWorkflowPlanStep]
  formal_draft_allowed: false
  not_claiming: string[]
  created_at: string
  plan_hash: string
}

export interface AdjacentWorkflowCheckpoint {
  schema: 'zhimeng.adjacent-workflow-checkpoint.v1'
  checkpoint_id: string
  workflow_plan_id: string
  plan_hash: string
  chain_id: string
  workflow_step_id: string
  step_hash: string
  step_ordinal: number
  skill_id: string
  expected_skill_version_id: string
  expected_package_fingerprint: string
  previous_checkpoint_id: string | null
  child_handoff_id: string | null
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  execution_status: 'succeeded' | 'failed'
  formal_draft_allowed: false
  created_at: string
  checkpoint_hash: string
}

export interface AdjacentChainRecord {
  schema: 'zhimeng.adjacent-skill-chain-run.v1'
  chain_id: string
  chain_key: 'novel-distillation-kb-to-context'
  status: AdjacentChainStatus
  project: {
    project_id: string
    revision: number
    fingerprint: string
  }
  route: {
    profile: string
    stage: string
    chapter_id: string
    task_intent: string
  }
  steps: [AdjacentChainStep, AdjacentChainStep]
  approval: {
    approved: boolean
    approved_by: 'author' | null
    approved_at: string | null
    risk_acknowledged: boolean
  }
  permissions: {
    read: string[]
    write: string[]
    network: false
    secrets: string[]
  }
  risk_dossier: AdjacentChainRiskDossier | null
  risk_approval_token: AdjacentChainRiskApprovalToken | null
  workflow_plan: AdjacentWorkflowPlan | null
  workflow_checkpoints: AdjacentWorkflowCheckpoint[]
  formal_draft_allowed: false
  source_import_id: string | null
  skill_handoff_id: string | null
  context_handoff_id: string | null
  context_pack_id: string | null
  context_pack_hash: string | null
  route_receipt_id: string | null
  route_receipt_hash: string | null
  current_step_ordinal: number
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface AdjacentChainResponse {
  schema: 'zhimeng.adjacent-skill-chain-response.v1'
  record: AdjacentChainRecord
  not_claiming: string[]
}

export interface AdjacentChainPrepareRequest {
  expected_project_revision: number
  expected_project_fingerprint: string
  source_import_id?: string | null
  profile?: string
  stage?: string
  chapter_id?: string
  task_intent?: string
}

export interface AdjacentChainDecisionRequest {
  expected_chain_id: string
}

function pathFor(projectId: string, suffix = ''): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/adjacent-skill-chains/novel-distillation-kb-to-context${suffix}`
}

export function getAdjacentChain(projectId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<AdjacentChainResponse>(pathFor(projectId), options)
}

export function prepareAdjacentChain(
  projectId: string,
  request: AdjacentChainPrepareRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<AdjacentChainResponse>(pathFor(projectId, '/prepare'), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function approveAdjacentChain(projectId: string, chainId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/approve', chainId, options)
}

export function rejectAdjacentChain(projectId: string, chainId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/reject', chainId, options)
}

export function disableAdjacentChain(projectId: string, chainId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/disable', chainId, options)
}

export function runAdjacentChain(projectId: string, chainId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/run', chainId, {
    ...options,
    timeoutMs: Math.max(options.timeoutMs ?? 0, 180_000),
  })
}

function postDecision(
  projectId: string,
  suffix: '/approve' | '/reject' | '/disable' | '/run',
  chainId: string,
  options: RuntimeClientOptions,
) {
  return fetchRuntimeJson<AdjacentChainResponse>(pathFor(projectId, suffix), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_chain_id: chainId } satisfies AdjacentChainDecisionRequest),
  })
}
