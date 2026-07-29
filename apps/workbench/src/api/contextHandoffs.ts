import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type ContextHandoffStatus =
  | 'awaiting_upstream'
  | 'prepared'
  | 'approved'
  | 'rejected'
  | 'disabled'
  | 'running'
  | 'succeeded'
  | 'failed'

export interface ContextHandoffIndexEntry {
  card_id: string
  card_type: 'work' | 'voice' | 'validator'
  version_id: string
  content_hash: string
  title: string
  route_terms: string[]
  tags: string[]
  safety_summary: string
}

export interface ContextHandoffRecord {
  schema: 'zhimeng.context-handoff-run.v1'
  context_handoff_id: string
  handoff_key: 'novel-kb-import-index-to-assemble-context'
  status: ContextHandoffStatus
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
    source_handoff_id: string
    source_handoff_key: 'novel-distillation-to-kb-manager'
    source_skill_receipt_id: string
    source_skill_receipt_hash: string
    source_result_hash: string
    distillation_index_hash: string
    distillation_index_artifact_path: string
    distillation_index_artifact_file_hash: string
    source_project_revision: number
    source_project_fingerprint: string
    upstream_activation_skill_version_id: string
    entries: [ContextHandoffIndexEntry, ContextHandoffIndexEntry, ContextHandoffIndexEntry]
    no_full_card_text_copied: true
  }
  downstream: {
    target_skill_id: 'novel-kb-manager'
    capability: 'assemble-context'
    contract: string
  }
  route: {
    profile: string
    stage: string
    chapter_id: string
    task_intent: string
    route_terms: string[]
  }
  activation: null | {
    managed_skill_id: 'novel-kb.context-handoff'
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
  constraints: Record<string, unknown>
  formal_draft_allowed: false
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  result_hash: string | null
  artifact_hash: string | null
  output_artifact_path: string | null
  context_pack_id: string | null
  context_pack_hash: string | null
  route_receipt_id: string | null
  route_receipt_hash: string | null
  result: null | {
    schema?: string
    status?: string
    source_handoff_id?: string
    distillation_index_hash?: string
    downstream_target?: string
    context_pack_id?: string
    context_pack_hash?: string
    route_receipt_id?: string
    route_receipt_hash?: string
    route_health?: string
    route_evidence_status?: string
    context_health?: string
    readiness?: string
    profile?: string
    stage?: string
    chapter_id?: string
    task_intent?: string
    route_terms?: string[]
    selected_entries?: ContextHandoffIndexEntry[]
    selection_reasons?: string[]
    constraints?: Record<string, unknown>
  }
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface ContextHandoffResponse {
  schema: 'zhimeng.context-handoff-response.v1'
  record: ContextHandoffRecord
}

export interface ContextHandoffPrepareRequest {
  expected_project_revision: number
  expected_project_fingerprint: string
  profile?: string
  stage?: string
  chapter_id?: string
  task_intent?: string
}

export interface ContextHandoffDecisionRequest {
  expected_context_handoff_id: string
}

function pathFor(projectId: string, suffix = ''): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/context-handoffs/novel-kb-assemble${suffix}`
}

export function getContextHandoff(projectId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<ContextHandoffResponse>(pathFor(projectId), options)
}

export function prepareContextHandoff(
  projectId: string,
  request: ContextHandoffPrepareRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ContextHandoffResponse>(pathFor(projectId, '/prepare'), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function approveContextHandoff(projectId: string, contextHandoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/approve', contextHandoffId, options)
}

export function rejectContextHandoff(projectId: string, contextHandoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/reject', contextHandoffId, options)
}

export function disableContextHandoff(projectId: string, contextHandoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/disable', contextHandoffId, options)
}

export function runContextHandoff(projectId: string, contextHandoffId: string, options: RuntimeClientOptions = {}) {
  return postDecision(projectId, '/run', contextHandoffId, { ...options, timeoutMs: Math.max(options.timeoutMs ?? 0, 90_000) })
}

function postDecision(
  projectId: string,
  suffix: '/approve' | '/reject' | '/disable' | '/run',
  contextHandoffId: string,
  options: RuntimeClientOptions,
) {
  return fetchRuntimeJson<ContextHandoffResponse>(pathFor(projectId, suffix), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_context_handoff_id: contextHandoffId } satisfies ContextHandoffDecisionRequest),
  })
}
