import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

// A writeback scans the project, performs multiple atomic replacements, and
// rescans it before returning. Keep this timeout separate from short UI reads.
export const CHANGE_SET_WRITEBACK_TIMEOUT_MS = 120_000

export type ChangeOperation = 'create' | 'update' | 'delete' | 'move'
export type ChangeSetOrigin = 'manual' | 'model' | 'agent' | 'skill_workflow'
export type HunkDecision = 'pending' | 'accepted' | 'rejected'
export type ChangeSetStatus =
  | 'reviewing'
  | 'ready'
  | 'applying'
  | 'rolling_back'
  | 'applied'
  | 'rolled_back'
  | 'discarded'
  | 'conflicted'
  | 'failed'

export interface ChangeHunk {
  hunk_id: string
  index: number
  before_start: number
  before_end: number
  after_start: number
  after_end: number
  before_text: string
  after_text: string
  before_sha256: string
  after_sha256: string
  decision: HunkDecision
}

export interface CandidateFileChange {
  change_id: string
  operation: ChangeOperation
  file_id: string | null
  relative_path: string
  /** 移动/改名的目标路径；非 move 时为 null（旧数据没有这个键）。 */
  target_relative_path?: string | null
  rationale: string
  before_sha256: string | null
  proposed_after_sha256: string | null
  preserve_utf8_bom: boolean
  post_draft_gate_scope: 'none' | 'prose_body'
  hunks: ChangeHunk[]
}

export interface WritebackFileResult {
  change_id: string
  operation: ChangeOperation
  relative_path: string
  target_relative_path?: string | null
  before_sha256: string | null
  after_sha256: string | null
  result_file_id: string | null
  accepted_hunk_ids: string[]
  rejected_hunk_ids: string[]
  backup_ref: string | null
}

export interface WritebackManifest {
  schema: 'zhimeng.writeback-manifest.v1'
  change_set_id: string
  project_id: string
  base_revision: number
  base_fingerprint: string
  result_revision: number
  result_fingerprint: string
  outcome: 'applied' | 'rolled_back'
  files: WritebackFileResult[]
  created_directories: string[]
  created_at: string
}

/**
 * 后端挂在 ChangeSet 上的写法提示证据（AiSmellReport.as_dict() + baseline/chronic/purpose）。
 * 卡片从这里渲染；旧记录没有这个字段时前端退回解析摘要字符串。
 */
export interface ChangeSetAiSmellEvidence {
  score: number
  level: string
  purpose?: 'draft' | 'review'
  baseline?: number | null
  chronic?: string[]
  sample_characters?: number
  voice_reference?: 'personal' | 'global' | 'none'
  worst_sentences?: Array<{ sentence?: string; reasons?: string[] }>
  deai_drift?: { risk_level?: string } | null
  top_components?: Array<{ name?: string; value?: number }>
}

export interface ChangeSet {
  schema: 'zhimeng.change-set.v1'
  change_set_id: string
  project_id: string
  base_revision: number
  base_fingerprint: string
  title: string
  summary: string
  origin: ChangeSetOrigin
  source_run_id: string | null
  source_tool_call_id: string | null
  source_request_hash: string | null
  source_skill_ids: string[]
  gate_required: boolean
  ai_smell?: ChangeSetAiSmellEvidence | null
  status: ChangeSetStatus
  review_revision: number
  changes: CandidateFileChange[]
  writeback: WritebackManifest | null
  rollback: WritebackManifest | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface ChangeSetSummary {
  change_set_id: string
  project_id: string
  title: string
  origin: ChangeSetOrigin
  source_run_id: string | null
  source_tool_call_id: string | null
  gate_required: boolean
  status: ChangeSetStatus
  base_revision: number
  base_fingerprint: string
  review_revision: number
  file_count: number
  hunk_count: number
  pending_hunk_count: number
  accepted_hunk_count: number
  rejected_hunk_count: number
  created_at: string
  updated_at: string
}

export interface UpdateFileChangeInput {
  operation: 'update'
  file_id: string
  after_content: string
  rationale: string
}

export interface CreateFileChangeInput {
  operation: 'create'
  relative_path: string
  after_content: string
  rationale: string
}

export interface DeleteFileChangeInput {
  operation: 'delete'
  file_id: string
  rationale: string
}

export type ChangeSetCreateChangeInput =
  | UpdateFileChangeInput
  | CreateFileChangeInput
  | DeleteFileChangeInput

export interface ChangeSetCreateRequest {
  schema?: 'zhimeng.change-set-create.v1'
  base_revision: number
  base_fingerprint: string
  title: string
  summary: string
  origin?: ChangeSetOrigin
  source_run_id?: string | null
  source_tool_call_id?: string | null
  source_request_hash?: string | null
  source_skill_ids?: string[]
  gate_required?: boolean
  ai_smell?: Record<string, unknown> | null
  changes: ChangeSetCreateChangeInput[]
}

interface ChangeSetListResponse {
  schema: 'zhimeng.change-set-list.v1'
  change_sets: ChangeSetSummary[]
}

export interface HunkDecisionUpdate {
  hunk_id: string
  decision: Exclude<HunkDecision, 'pending'>
}

function jsonRequest(method: 'PATCH' | 'POST', body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function writebackOptions(options: RuntimeClientOptions): RuntimeClientOptions {
  return options.timeoutMs === undefined
    ? { ...options, timeoutMs: CHANGE_SET_WRITEBACK_TIMEOUT_MS }
    : options
}

export function createProjectChangeSet(
  projectId: string,
  request: ChangeSetCreateRequest,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/change-sets`,
    writebackOptions(options),
    jsonRequest('POST', {
      schema: 'zhimeng.change-set-create.v1',
      origin: 'manual',
      gate_required: false,
      source_run_id: null,
      source_tool_call_id: null,
      source_request_hash: null,
      source_skill_ids: [],
      ...request,
    }),
  )
}

export async function listProjectChangeSets(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<ChangeSetSummary[]> {
  const payload = await fetchRuntimeJson<ChangeSetListResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/change-sets`,
    options,
  )
  return payload.change_sets
}

export function summarizeChangeSet(record: ChangeSet): ChangeSetSummary {
  const decisions = record.changes.flatMap((change) => (
    change.hunks.map((hunk) => hunk.decision)
  ))
  return {
    change_set_id: record.change_set_id,
    project_id: record.project_id,
    title: record.title,
    origin: record.origin,
    source_run_id: record.source_run_id,
    source_tool_call_id: record.source_tool_call_id,
    gate_required: record.gate_required,
    status: record.status,
    base_revision: record.base_revision,
    base_fingerprint: record.base_fingerprint,
    review_revision: record.review_revision,
    file_count: record.changes.length,
    hunk_count: decisions.length,
    pending_hunk_count: decisions.filter((decision) => decision === 'pending').length,
    accepted_hunk_count: decisions.filter((decision) => decision === 'accepted').length,
    rejected_hunk_count: decisions.filter((decision) => decision === 'rejected').length,
    created_at: record.created_at,
    updated_at: record.updated_at,
  }
}

export function getChangeSet(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}`,
    options,
  )
}

export function updateHunkDecisions(
  changeSetId: string,
  expectedReviewRevision: number,
  decisions: HunkDecisionUpdate[],
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/decisions`,
    options,
    jsonRequest('PATCH', {
      expected_review_revision: expectedReviewRevision,
      decisions,
    }),
  )
}

export function editChangeSetHunk(
  changeSetId: string,
  hunkId: string,
  expectedReviewRevision: number,
  afterText: string,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/hunks/${encodeURIComponent(hunkId)}`,
    options,
    jsonRequest('PATCH', {
      expected_review_revision: expectedReviewRevision,
      after_text: afterText,
    }),
  )
}

export function applyChangeSet(
  changeSetId: string,
  expectedReviewRevision: number,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/apply`,
    writebackOptions(options),
    jsonRequest('POST', { expected_review_revision: expectedReviewRevision }),
  )
}

export function applyChangeSetDirect(
  changeSetId: string,
  expectedReviewRevision: number,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/apply-direct`,
    writebackOptions(options),
    jsonRequest('POST', { expected_review_revision: expectedReviewRevision }),
  )
}

export function discardChangeSet(
  changeSetId: string,
  expectedReviewRevision: number,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/discard`,
    options,
    jsonRequest('POST', { expected_review_revision: expectedReviewRevision }),
  )
}

export function rollbackChangeSet(
  changeSetId: string,
  expectedReviewRevision: number,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/rollback`,
    writebackOptions(options),
    jsonRequest('POST', { expected_review_revision: expectedReviewRevision }),
  )
}
