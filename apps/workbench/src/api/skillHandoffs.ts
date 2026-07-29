import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type SkillHandoffStatus =
  | 'awaiting_upstream'
  | 'prepared'
  | 'approved'
  | 'rejected'
  | 'disabled'
  | 'running'
  | 'succeeded'
  | 'failed'

export interface SkillHandoffCardRef {
  card_id: string
  card_type: 'work' | 'voice' | 'validator'
  version_id: string
  content_hash: string
  title: string
  summary: string
  route_terms: string[]
  tags: string[]
  safety_summary: string
}

export interface SkillHandoffRecord {
  schema: 'zhimeng.skill-handoff-run.v1'
  handoff_id: string
  handoff_key: 'novel-distillation-to-kb-manager'
  status: SkillHandoffStatus
  project: {
    project_id: string
    revision: number
    fingerprint: string
  }
  workflow: null | {
    workflow_plan_id: string
    workflow_plan_hash: string
    checkpoint_id: string
    checkpoint_hash: string
    tool_call_id: string
    skill_call_id: string
  }
  upstream: null | {
    source_skill_id: 'novel-distillation'
    source_import_id: string
    formalization_receipt_id: string
    formalization_receipt_hash: string
    knowledge_fingerprint: string
    cards_hash: string
    cards: [SkillHandoffCardRef, SkillHandoffCardRef, SkillHandoffCardRef]
  }
  downstream: {
    target_skill_id: 'novel-kb-manager'
    capability: 'import-distillation-index'
    contract: string
  }
  activation: null | {
    managed_skill_id: 'novel-distillation.kb-handoff'
    skill_version_id: string
    skill_version: string
    manifest_fingerprint: string
    package_fingerprint: string
    effective_scope: 'application' | 'project'
  }
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
  formal_draft_allowed: false
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  artifact_hash: string | null
  output_artifact_path: string | null
  result: null | {
    schema?: string
    status?: string
    distillation_index_hash?: string
    downstream_target?: string
    counts?: Record<string, number>
    risk_summary?: string
    next_action?: string
  }
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface SkillHandoffResponse {
  schema: 'zhimeng.skill-handoff-response.v1'
  record: SkillHandoffRecord
}

export interface SkillHandoffPrepareRequest {
  expected_project_revision: number
  expected_project_fingerprint: string
  source_import_id?: string | null
}

export interface SkillHandoffDecisionRequest {
  expected_handoff_id: string
}

function pathFor(projectId: string, suffix = ''): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/skill-handoffs/novel-distillation-kb${suffix}`
}

export function getSkillHandoff(projectId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<SkillHandoffResponse>(pathFor(projectId), options)
}

export function prepareSkillHandoff(
  projectId: string,
  request: SkillHandoffPrepareRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<SkillHandoffResponse>(pathFor(projectId, '/prepare'), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function approveSkillHandoff(projectId: string, handoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/approve', handoffId, options)
}

export function rejectSkillHandoff(projectId: string, handoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/reject', handoffId, options)
}

export function disableSkillHandoff(projectId: string, handoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/disable', handoffId, options)
}

export function runSkillHandoff(projectId: string, handoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/run', handoffId, { ...options, timeoutMs: Math.max(options.timeoutMs ?? 0, 90_000) })
}

function postDecision(
  projectId: string,
  suffix: '/approve' | '/reject' | '/disable' | '/run',
  handoffId: string,
  options: RuntimeClientOptions,
) {
  return fetchRuntimeJson<SkillHandoffResponse>(pathFor(projectId, suffix), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_handoff_id: handoffId } satisfies SkillHandoffDecisionRequest),
  })
}
