import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type ManagedSkillChainStatus =
  | 'prepared'
  | 'approved'
  | 'rejected'
  | 'disabled'
  | 'running'
  | 'succeeded'
  | 'failed'

export type ManagedSkillChainStepStatus =
  | 'pending'
  | 'prepared'
  | 'approved'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'

export interface ManagedSkillChainStepSpec {
  step_id: string
  title: string
  skill_id: string
  capability: string
  input_payload?: Record<string, unknown>
}

export interface ManagedSkillChainPrepareRequest {
  expected_project_revision: number
  expected_project_fingerprint: string
  chain_key: string
  steps: ManagedSkillChainStepSpec[]
}

export interface ManagedSkillChainDecisionRequest {
  expected_chain_id: string
}

export interface ManagedSkillChainStep {
  step_id: string
  ordinal: number
  title: string
  skill_id: string
  capability: string
  status: ManagedSkillChainStepStatus
  input_payload: Record<string, unknown>
  input_hash: string | null
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  domain_status: 'healthy' | 'degraded' | 'blocked' | null
  error_code: string | null
  error_message: string | null
}

export interface ManagedSkillChainSkillRisk {
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

export interface ManagedSkillChainRiskDossier {
  formal_draft_allowed: false
  network: false
  secrets: string[]
  permissions: {
    read: string[]
    write: string[]
    network: false
    secrets: string[]
  }
  skills: ManagedSkillChainSkillRisk[]
  risk_summary: string
  not_claiming: string[]
}

export interface ManagedSkillChainRunRecord {
  schema: 'zhimeng.managed-skill-chain-run.v1'
  chain_id: string
  chain_key: string
  status: ManagedSkillChainStatus
  project: {
    project_id: string
    revision: number
    fingerprint: string
    root_path: string
  }
  steps: ManagedSkillChainStep[]
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
  risk_dossier: ManagedSkillChainRiskDossier | null
  risk_approval_token: {
    token_id: string
    chain_id: string
    project_id: string
    project_revision: number
    project_fingerprint: string
    dossier: ManagedSkillChainRiskDossier
    approved_by: 'author'
    approved_at: string
    consumed: boolean
    token_hash: string
  } | null
  workflow_plan: {
    schema?: string
    workflow_plan_id: string
    plan_hash: string
    steps: Array<{
      workflow_step_id: string
      skill_id: string
      runner: 'managed_skill'
      formal_draft_allowed: false
    }>
    formal_draft_allowed: false
    not_claiming: string[]
  } | null
  workflow_checkpoints: Array<{
    checkpoint_id: string
    workflow_step_id: string
    step_ordinal: number
    skill_id: string
    previous_checkpoint_id: string | null
    skill_receipt_id: string | null
    execution_status: 'succeeded' | 'failed'
    formal_draft_allowed: false
  }>
  formal_draft_allowed: false
  current_step_ordinal: number
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface ManagedSkillChainResponse {
  schema: 'zhimeng.managed-skill-chain-response.v1'
  record: ManagedSkillChainRunRecord
  not_claiming: string[]
}

function basePath(projectId: string): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/managed-skill-chains`
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export function prepareManagedSkillChain(
  projectId: string,
  request: ManagedSkillChainPrepareRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/prepare`,
    options,
    jsonPost(request),
  )
}

export function getManagedSkillChain(
  projectId: string,
  chainId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/${encodeURIComponent(chainId)}`,
    options,
  )
}

export function approveManagedSkillChain(
  projectId: string,
  chainId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/${encodeURIComponent(chainId)}/approve`,
    options,
    jsonPost({ expected_chain_id: chainId }),
  )
}

export function rejectManagedSkillChain(
  projectId: string,
  chainId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/${encodeURIComponent(chainId)}/reject`,
    options,
    jsonPost({ expected_chain_id: chainId }),
  )
}

export function disableManagedSkillChain(
  projectId: string,
  chainId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/${encodeURIComponent(chainId)}/disable`,
    options,
    jsonPost({ expected_chain_id: chainId }),
  )
}

export function runManagedSkillChain(
  projectId: string,
  chainId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillChainResponse>(
    `${basePath(projectId)}/${encodeURIComponent(chainId)}/run`,
    { ...options, timeoutMs: options.timeoutMs ?? 180_000 },
    jsonPost({ expected_chain_id: chainId }),
  )
}

export const WORKBENCH_DUAL_PROJECT_AUDIT_STEPS: ManagedSkillChainStepSpec[] = [
  {
    step_id: 'audit-a',
    title: '第一步项目审计',
    skill_id: 'novel-creation-suite.project-audit',
    capability: 'project-audit',
    input_payload: { schema: 'zhimeng.creation-project-audit-request.v1' },
  },
  {
    step_id: 'audit-b',
    title: '第二步项目审计',
    skill_id: 'novel-creation-suite.project-audit',
    capability: 'project-audit',
    input_payload: { schema: 'zhimeng.creation-project-audit-request.v1' },
  },
]
