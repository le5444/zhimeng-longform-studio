import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

// A writeback scans the project, performs multiple atomic replacements, and
// rescans it before returning. Keep this timeout separate from short UI reads.
export const CHANGE_SET_WRITEBACK_TIMEOUT_MS = 120_000
export const POST_DRAFT_GATE_TIMEOUT_MS = 120_000

export type ChangeOperation = 'create' | 'update' | 'delete'
export type ChangeSetOrigin = 'manual' | 'agent' | 'skill_workflow'
export type HunkDecision = 'pending' | 'accepted' | 'rejected'
export type PostDraftGateStatus = 'passed' | 'blocked' | 'failed'
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

export interface WritebackEvidenceReceipt {
  schema: 'zhimeng.writeback-evidence-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back'
  writeback_manifest_hash: string
  base_revision: number
  base_fingerprint: string
  result_revision: number
  result_fingerprint: string
  context_pack_id: string | null
  context_pack_hash: string | null
  route_receipt_id: string | null
  route_receipt_hash: string | null
  knowledge_fingerprint: string | null
  selected_card_ids: string[]
  source_run_id: string | null
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface WritebackEvidenceResponse {
  schema: 'zhimeng.writeback-evidence-response.v1'
  change_set_id: string
  receipt: WritebackEvidenceReceipt | null
  history: WritebackEvidenceReceipt[]
}

export interface PostDraftGatePoint {
  line: number
  column: number
  offset: number
}

export interface PostDraftGateLocation {
  start: PostDraftGatePoint
  end: PostDraftGatePoint
}

export interface PostDraftGateFinding {
  code: string
  failure_signature: string
  severity: 'blocked'
  message: string
  evidence: string
  location: PostDraftGateLocation
}

export interface PostDraftGateCheck {
  code: string
  status: 'passed' | 'blocked'
  message: string
  evidence_refs: string[]
}

export interface PostDraftGateFileResult {
  path: string
  status: 'passed' | 'blocked'
  content_hash: string
  byte_length: number
  body_line_count: number
  findings: PostDraftGateFinding[]
}

export interface PostDraftGateResult {
  schema: 'zhimeng.creation-draft-gate-result.v1'
  status: 'passed' | 'blocked'
  summary: string
  candidate_id: string
  candidate_manifest_hash: string | null
  quality_assessed: false
  formal_draft_allowed: false
  global_findings: PostDraftGateFinding[]
  checks: PostDraftGateCheck[]
  files: PostDraftGateFileResult[]
  failure_signatures: string[]
}

export interface PostDraftGateReceipt {
  schema: 'zhimeng.post-draft-gate-receipt.v1'
  gate_receipt_id: string
  change_set_id: string
  project_id: string
  review_revision: number
  base_revision: number
  base_fingerprint: string
  candidate_hash: string
  validator_id: string
  validator_version: string
  validator_manifest_fingerprint: string
  validator_package_fingerprint: string
  context_pack_id: string | null
  context_pack_hash: string | null
  knowledge_fingerprint: string | null
  candidate_file_count: number
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  skill_output_hash: string | null
  execution_status: 'succeeded' | 'failed'
  status: PostDraftGateStatus
  result: PostDraftGateResult | null
  error_code: string | null
  error_message: string | null
  quality_assessed: false
  formal_draft_allowed: false
  created_at: string
  receipt_hash: string
}

export interface PostDraftGateResponse {
  schema: 'zhimeng.post-draft-gate-response.v1'
  change_set_id: string
  project_id: string
  gate_required: boolean
  review_revision: number
  receipt: PostDraftGateReceipt | null
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
  status: ChangeSetStatus
  review_revision: number
  changes: CandidateFileChange[]
  post_draft_gate: PostDraftGateReceipt | null
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

function postDraftGateOptions(options: RuntimeClientOptions): RuntimeClientOptions {
  return options.timeoutMs === undefined
    ? { ...options, timeoutMs: POST_DRAFT_GATE_TIMEOUT_MS }
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

export function runPostDraftGate(
  changeSetId: string,
  expectedReviewRevision: number,
  options: RuntimeClientOptions = {},
): Promise<ChangeSet> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/post-draft-gate`,
    postDraftGateOptions(options),
    jsonRequest('POST', { expected_review_revision: expectedReviewRevision }),
  )
}

export function getPostDraftGate(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<PostDraftGateResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/post-draft-gate`,
    options,
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

export function getWritebackEvidence(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<WritebackEvidenceResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/writeback-evidence`,
    options,
  )
}

export interface NlclNovelStateFileRef {
  relative_path: string
  source_hash: string
  domain: 'character' | 'plot' | 'foreshadowing' | 'project_status'
}

export interface NlclOriginalNovelStateBridgeReceipt {
  schema: 'zhimeng.nlcl-original-novel-state-bridge-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_chapter_ops_usage_export_receipt_id: string
  nlcl_original_chapter_ops_usage_export_receipt_hash: string
  nlcl_original_call_usage_export_receipt_id: string
  nlcl_original_call_usage_export_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  chapter_relative_paths: string[]
  character_state_refs: NlclNovelStateFileRef[]
  plot_state_refs: NlclNovelStateFileRef[]
  foreshadowing_state_refs: NlclNovelStateFileRef[]
  project_status_refs: NlclNovelStateFileRef[]
  source_run_id: string | null
  export_relative_path: string
  export_absolute_path: string
  export_file_sha256: string
  export_byte_offset: number
  export_row_sha256: string
  record_mode: 'product_bridge_novel_state_inventory_from_chapter_ops'
  original_nlcl_call_usage_recorded: true
  original_nlcl_chapter_ops_usage_synced: true
  original_nlcl_novel_state_bridge_recorded: true
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalNovelStateBridgeResponse {
  schema: 'zhimeng.nlcl-original-novel-state-bridge-response.v1'
  change_set_id: string
  receipt: NlclOriginalNovelStateBridgeReceipt | null
  history: NlclOriginalNovelStateBridgeReceipt[]
}

export function getOriginalNlclNovelStateBridge(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalNovelStateBridgeResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-novel-state-bridge`,
    options,
  )
}

export function exportOriginalNlclNovelStateBridge(
  changeSetId: string,
  expectedChapterOpsUsageExportReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalNovelStateBridgeResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-novel-state-bridge`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_chapter_ops_usage_export_receipt_hash:
        expectedChapterOpsUsageExportReceiptHash,
    }),
  )
}

export interface NlclOriginalNovelStateWrittenFile {
  relative_path: string
  source_hash: string
  domain:
    | 'character'
    | 'plot'
    | 'foreshadowing'
    | 'project_status'
    | 'truth_character_matrix'
    | 'truth_foreshadow_lifecycle'
    | 'truth_info_boundary'
  created: boolean
}

export interface NlclOriginalNovelStateSyncReceipt {
  schema: 'zhimeng.nlcl-original-novel-state-sync-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_novel_state_bridge_receipt_id: string
  nlcl_original_novel_state_bridge_receipt_hash: string
  nlcl_original_chapter_ops_usage_export_receipt_id: string
  nlcl_original_chapter_ops_usage_export_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  chapter_relative_paths: string[]
  written_files: NlclOriginalNovelStateWrittenFile[]
  state_root_relative_path: string
  record_mode: 'product_bridge_original_state_json_writeback'
  original_nlcl_call_usage_recorded: true
  original_nlcl_chapter_ops_usage_synced: true
  original_nlcl_novel_state_bridge_recorded: true
  original_nlcl_novel_state_synced: true
  original_nlcl_truth_matrix_synced: true
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalNovelStateSyncResponse {
  schema: 'zhimeng.nlcl-original-novel-state-sync-response.v1'
  change_set_id: string
  receipt: NlclOriginalNovelStateSyncReceipt | null
  history: NlclOriginalNovelStateSyncReceipt[]
}

export function getOriginalNlclNovelStateSync(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalNovelStateSyncResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-novel-state-sync`,
    options,
  )
}

export function exportOriginalNlclNovelStateSync(
  changeSetId: string,
  expectedNovelStateBridgeReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalNovelStateSyncResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-novel-state-sync`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_novel_state_bridge_receipt_hash:
        expectedNovelStateBridgeReceiptHash,
    }),
  )
}




export interface NlclOriginalPrewriteContextReportWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalPrewriteContextReportReceipt {
  schema: 'zhimeng.nlcl-original-prewrite-context-report-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_novel_state_sync_receipt_id: string
  nlcl_original_novel_state_sync_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  written_files: NlclOriginalPrewriteContextReportWrittenFile[]
  report_relative_path: string
  record_mode: 'product_bridge_prewrite_context_report'
  original_nlcl_novel_state_synced: true
  original_nlcl_truth_matrix_synced: true
  prewrite_context_report_synced: true
  bounded_library_activated: boolean
  nlcl_original_bounded_library_activation_receipt_id: string | null
  nlcl_original_bounded_library_activation_receipt_hash: string | null
  activated_source_count: number
  whole_library_load_forbidden: true
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalPrewriteContextReportResponse {
  schema: 'zhimeng.nlcl-original-prewrite-context-report-response.v1'
  change_set_id: string
  receipt: NlclOriginalPrewriteContextReportReceipt | null
  history: NlclOriginalPrewriteContextReportReceipt[]
}

export function getOriginalNlclPrewriteContextReport(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalPrewriteContextReportResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-prewrite-context-report`,
    options,
  )
}

export function exportOriginalNlclPrewriteContextReport(
  changeSetId: string,
  expectedNovelStateSyncReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalPrewriteContextReportResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-prewrite-context-report`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_novel_state_sync_receipt_hash:
        expectedNovelStateSyncReceiptHash,
    }),
  )
}

export interface NlclOriginalTruthContentExtractSourceFile {
  relative_path: string
  source_hash: string
  section_index: number
  extracted_item_count: number
}

export interface NlclOriginalTruthContentExtractWrittenFile {
  relative_path: string
  source_hash: string
  domain:
    | 'character'
    | 'plot'
    | 'foreshadowing'
    | 'truth_character_matrix'
    | 'truth_foreshadow_lifecycle'
    | 'truth_info_boundary'
  created: boolean
  item_count: number
}

export interface NlclOriginalTruthContentExtractReceipt {
  schema: 'zhimeng.nlcl-original-truth-content-extract-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_novel_state_sync_receipt_id: string
  nlcl_original_novel_state_sync_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  source_files: NlclOriginalTruthContentExtractSourceFile[]
  written_files: NlclOriginalTruthContentExtractWrittenFile[]
  extracted_character_count: number
  extracted_foreshadow_count: number
  extracted_info_boundary_count: number
  record_mode: 'product_bridge_truth_content_extract_from_01_09'
  original_nlcl_novel_state_synced: true
  original_nlcl_truth_matrix_synced: true
  truth_content_extracted: true
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalTruthContentExtractResponse {
  schema: 'zhimeng.nlcl-original-truth-content-extract-response.v1'
  change_set_id: string
  receipt: NlclOriginalTruthContentExtractReceipt | null
  history: NlclOriginalTruthContentExtractReceipt[]
}

export function getOriginalNlclTruthContentExtract(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalTruthContentExtractResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-truth-content-extract`,
    options,
  )
}

export function exportOriginalNlclTruthContentExtract(
  changeSetId: string,
  expectedNovelStateSyncReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalTruthContentExtractResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-truth-content-extract`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_novel_state_sync_receipt_hash:
        expectedNovelStateSyncReceiptHash,
    }),
  )
}

export interface NlclOriginalMandatoryUsagePlanItem {
  item_id: string
  kind: 'character' | 'foreshadow' | 'plot_thread' | 'info_boundary' | 'selected_card'
  title: string
  source_relative_path: string | null
  required_action: string
  status: 'pending' | 'used' | 'unused' | 'missed' | 'suppressed_for_project_truth'
}

export interface NlclOriginalMandatoryUsagePlanWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalMandatoryUsagePlanReceipt {
  schema: 'zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_truth_content_extract_receipt_id: string
  nlcl_original_truth_content_extract_receipt_hash: string
  nlcl_original_novel_state_sync_receipt_id: string
  nlcl_original_novel_state_sync_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  items: NlclOriginalMandatoryUsagePlanItem[]
  written_files: NlclOriginalMandatoryUsagePlanWrittenFile[]
  plan_relative_path: string
  record_mode: 'product_bridge_mandatory_usage_plan_scaffold'
  original_nlcl_novel_state_synced: true
  original_nlcl_truth_matrix_synced: true
  truth_content_extracted: true
  mandatory_usage_plan_synced: true
  mandatory_usage_closure_closed: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalMandatoryUsagePlanResponse {
  schema: 'zhimeng.nlcl-original-mandatory-usage-plan-response.v1'
  change_set_id: string
  receipt: NlclOriginalMandatoryUsagePlanReceipt | null
  history: NlclOriginalMandatoryUsagePlanReceipt[]
}

export function getOriginalNlclMandatoryUsagePlan(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalMandatoryUsagePlanResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-mandatory-usage-plan`,
    options,
  )
}

export function exportOriginalNlclMandatoryUsagePlan(
  changeSetId: string,
  expectedTruthContentExtractReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalMandatoryUsagePlanResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-mandatory-usage-plan`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_truth_content_extract_receipt_hash:
        expectedTruthContentExtractReceiptHash,
    }),
  )
}

export type NlclOriginalMandatoryUsageDecisionStatus =
  | 'used'
  | 'unused'
  | 'missed'
  | 'suppressed_for_project_truth'

export interface NlclOriginalMandatoryUsageClosureDecision {
  item_id: string
  status: NlclOriginalMandatoryUsageDecisionStatus
  note?: string
}

export interface NlclOriginalMandatoryUsageClosureItem {
  item_id: string
  kind: 'character' | 'foreshadow' | 'plot_thread' | 'info_boundary' | 'selected_card'
  title: string
  source_relative_path: string | null
  required_action: string
  status: NlclOriginalMandatoryUsageDecisionStatus
  note: string
}

export interface NlclOriginalMandatoryUsageClosureWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalMandatoryUsageClosureReceipt {
  schema: 'zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_mandatory_usage_plan_receipt_id: string
  nlcl_original_mandatory_usage_plan_receipt_hash: string
  nlcl_original_truth_content_extract_receipt_id: string
  nlcl_original_truth_content_extract_receipt_hash: string
  nlcl_original_novel_state_sync_receipt_id: string
  nlcl_original_novel_state_sync_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  items: NlclOriginalMandatoryUsageClosureItem[]
  written_files: NlclOriginalMandatoryUsageClosureWrittenFile[]
  closure_relative_path: string
  overall_status: 'closed'
  record_mode: 'product_bridge_mandatory_usage_closure_author_decided'
  original_nlcl_novel_state_synced: true
  original_nlcl_truth_matrix_synced: true
  truth_content_extracted: true
  mandatory_usage_plan_synced: true
  mandatory_usage_closure_closed: true
  route_validated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalMandatoryUsageClosureResponse {
  schema: 'zhimeng.nlcl-original-mandatory-usage-closure-response.v1'
  change_set_id: string
  receipt: NlclOriginalMandatoryUsageClosureReceipt | null
  history: NlclOriginalMandatoryUsageClosureReceipt[]
}

export function getOriginalNlclMandatoryUsageClosure(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalMandatoryUsageClosureResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-mandatory-usage-closure`,
    options,
  )
}

export function exportOriginalNlclMandatoryUsageClosure(
  changeSetId: string,
  expectedMandatoryUsagePlanReceiptHash: string,
  decisions: NlclOriginalMandatoryUsageClosureDecision[],
  finalize = true,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalMandatoryUsageClosureResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-mandatory-usage-closure`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_mandatory_usage_plan_receipt_hash:
        expectedMandatoryUsagePlanReceiptHash,
      decisions,
      finalize,
    }),
  )
}

export interface NlclOriginalOperationPackageLikeWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalOperationPackageLikeReceipt {
  schema: 'zhimeng.nlcl-original-operation-package-like-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_mandatory_usage_closure_receipt_id: string
  nlcl_original_mandatory_usage_closure_receipt_hash: string
  nlcl_original_mandatory_usage_plan_receipt_id: string
  nlcl_original_mandatory_usage_plan_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  operation_package_id: string
  package_hash: string
  readiness: 'risk_draft_only'
  written_files: NlclOriginalOperationPackageLikeWrittenFile[]
  package_relative_path: string
  record_mode: 'product_bridge_operation_package_like_scaffold'
  original_nlcl_novel_state_synced: true
  truth_content_extracted: true
  mandatory_usage_plan_synced: true
  mandatory_usage_closure_closed: true
  operation_package_like_synced: true
  route_validated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalOperationPackageLikeResponse {
  schema: 'zhimeng.nlcl-original-operation-package-like-response.v1'
  change_set_id: string
  receipt: NlclOriginalOperationPackageLikeReceipt | null
  history: NlclOriginalOperationPackageLikeReceipt[]
}

export function getOriginalNlclOperationPackageLike(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalOperationPackageLikeResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-operation-package-like`,
    options,
  )
}

export function exportOriginalNlclOperationPackageLike(
  changeSetId: string,
  expectedMandatoryUsageClosureReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalOperationPackageLikeResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-operation-package-like`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_mandatory_usage_closure_receipt_hash:
        expectedMandatoryUsageClosureReceiptHash,
    }),
  )
}

export interface NlclOriginalLibraryLayoutSlot {
  slot_id: string
  kind:
    | 'section_01'
    | 'section_02'
    | 'section_03'
    | 'section_04'
    | 'section_05'
    | 'section_06'
    | 'section_07'
    | 'section_08'
    | 'section_09'
    | 'state_file'
    | 'chapter_context'
  label: string
  relative_path: string | null
  present: boolean
  file_count: number
  note: string
}

export interface NlclOriginalLibraryLayoutInventoryWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalLibraryLayoutInventoryReceipt {
  schema: 'zhimeng.nlcl-original-library-layout-inventory-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_operation_package_like_receipt_id: string
  nlcl_original_operation_package_like_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  slots: NlclOriginalLibraryLayoutSlot[]
  present_slot_count: number
  missing_slot_count: number
  written_files: NlclOriginalLibraryLayoutInventoryWrittenFile[]
  inventory_relative_path: string
  record_mode: 'product_bridge_library_layout_inventory'
  operation_package_like_synced: true
  library_layout_inventoried: true
  library_layout_complete: false
  whole_library_load_forbidden: true
  route_validated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalLibraryLayoutInventoryResponse {
  schema: 'zhimeng.nlcl-original-library-layout-inventory-response.v1'
  change_set_id: string
  receipt: NlclOriginalLibraryLayoutInventoryReceipt | null
  history: NlclOriginalLibraryLayoutInventoryReceipt[]
}

export function getOriginalNlclLibraryLayoutInventory(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalLibraryLayoutInventoryResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-library-layout-inventory`,
    options,
  )
}

export function exportOriginalNlclLibraryLayoutInventory(
  changeSetId: string,
  expectedOperationPackageLikeReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalLibraryLayoutInventoryResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-library-layout-inventory`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_operation_package_like_receipt_hash:
        expectedOperationPackageLikeReceiptHash,
    }),
  )
}

export interface NlclOriginalLibraryLayoutCompletenessWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalLibraryLayoutCompletenessReceipt {
  schema: 'zhimeng.nlcl-original-library-layout-completeness-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_library_layout_inventory_receipt_id: string
  nlcl_original_library_layout_inventory_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  slots: NlclOriginalLibraryLayoutSlot[]
  required_slot_count: number
  present_required_slot_count: number
  missing_required_slot_count: number
  completeness_status: 'complete' | 'incomplete'
  written_files: NlclOriginalLibraryLayoutCompletenessWrittenFile[]
  assessment_relative_path: string
  record_mode: 'product_bridge_library_layout_completeness'
  library_layout_inventoried: true
  library_layout_assessed: true
  library_layout_complete: boolean
  whole_library_load_forbidden: true
  route_validated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalLibraryLayoutCompletenessResponse {
  schema: 'zhimeng.nlcl-original-library-layout-completeness-response.v1'
  change_set_id: string
  receipt: NlclOriginalLibraryLayoutCompletenessReceipt | null
  history: NlclOriginalLibraryLayoutCompletenessReceipt[]
}

export function getOriginalNlclLibraryLayoutCompleteness(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalLibraryLayoutCompletenessResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-library-layout-completeness`,
    options,
  )
}

export function exportOriginalNlclLibraryLayoutCompleteness(
  changeSetId: string,
  expectedInventoryReceiptHash: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalLibraryLayoutCompletenessResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-library-layout-completeness`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_library_layout_inventory_receipt_hash:
        expectedInventoryReceiptHash,
    }),
  )
}

export interface NlclOriginalBoundedLibraryActivationSource {
  source_id: string
  slot_id: string
  kind:
    | 'section_01'
    | 'section_02'
    | 'section_03'
    | 'section_04'
    | 'section_05'
    | 'section_06'
    | 'section_07'
    | 'section_08'
    | 'section_09'
    | 'state_file'
    | 'chapter_context'
  relative_path: string
  character_count: number
  token_estimate: number
}

export interface NlclOriginalBoundedLibraryActivationWrittenFile {
  relative_path: string
  source_hash: string
  kind: 'json_latest' | 'md_latest' | 'json_stage' | 'md_stage'
  created: boolean
}

export interface NlclOriginalBoundedLibraryActivationReceipt {
  schema: 'zhimeng.nlcl-original-bounded-library-activation-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  nlcl_original_library_layout_completeness_receipt_id: string
  nlcl_original_library_layout_completeness_receipt_hash: string
  outcome: 'applied' | 'rolled_back'
  chapter_id: string
  canonical_chapter_key: string
  activated_sources: NlclOriginalBoundedLibraryActivationSource[]
  activated_source_ids: string[]
  max_sources: number
  activated_count: number
  candidate_present_count: number
  max_characters: number
  used_characters: number
  max_tokens: number
  used_tokens: number
  written_files: NlclOriginalBoundedLibraryActivationWrittenFile[]
  activation_relative_path: string
  record_mode: 'product_bridge_bounded_library_activation'
  library_layout_inventoried: true
  library_layout_assessed: true
  library_layout_complete: boolean
  bounded_library_activated: true
  whole_library_load: false
  whole_library_load_forbidden: true
  route_validated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: true
  created_at: string
  receipt_hash: string
}

export interface NlclOriginalBoundedLibraryActivationResponse {
  schema: 'zhimeng.nlcl-original-bounded-library-activation-response.v1'
  change_set_id: string
  receipt: NlclOriginalBoundedLibraryActivationReceipt | null
  history: NlclOriginalBoundedLibraryActivationReceipt[]
}

export function getOriginalNlclBoundedLibraryActivation(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalBoundedLibraryActivationResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-bounded-library-activation`,
    options,
  )
}

export function exportOriginalNlclBoundedLibraryActivation(
  changeSetId: string,
  expectedCompletenessReceiptHash: string,
  maxSources = 16,
  options: RuntimeClientOptions = {},
): Promise<NlclOriginalBoundedLibraryActivationResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/nlcl-original-bounded-library-activation`,
    writebackOptions(options),
    jsonRequest('POST', {
      expected_nlcl_original_library_layout_completeness_receipt_hash:
        expectedCompletenessReceiptHash,
      max_sources: maxSources,
    }),
  )
}

export type ReaderFindingKind = 'fact_risk' | 'continuity_risk' | 'subjective_note'
export type ReaderFindingSeverity = 'info' | 'watch' | 'concern'
export type ReaderFindingDecisionValue = 'adopted' | 'rejected' | 'deferred'

export interface ReaderFinding {
  finding_id: string
  kind: ReaderFindingKind
  severity: ReaderFindingSeverity
  summary: string
  evidence_quote: string
  relative_path: string | null
  overrides_gate: false
}

export interface ReaderReviewReceipt {
  schema: 'zhimeng.reader-review-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back'
  result_revision: number
  result_fingerprint: string
  review_mode: 'deterministic_cold_read_v1'
  allowed_input_classes: string[]
  excluded_input_classes: string[]
  candidate_relative_paths: string[]
  findings: ReaderFinding[]
  finding_counts: Record<string, number>
  reader_opinion_recorded: true
  overrides_gate: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface ReaderReviewResponse {
  schema: 'zhimeng.reader-review-response.v1'
  change_set_id: string
  receipt: ReaderReviewReceipt | null
  history: ReaderReviewReceipt[]
}

export interface ReaderFindingDecisionInput {
  finding_id: string
  decision: ReaderFindingDecisionValue
  note?: string
}

export interface ReaderReviewDecisionReceipt {
  schema: 'zhimeng.reader-review-decision-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  reader_review_receipt_id: string
  reader_review_receipt_hash: string
  result_revision: number
  result_fingerprint: string
  decisions: ReaderFindingDecisionInput[]
  adopted_finding_ids: string[]
  rejected_finding_ids: string[]
  deferred_finding_ids: string[]
  finalized: boolean
  follow_up_change_set_id: string | null
  parent_reader_valid_for_new_candidate: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface ReaderReviewDecisionResponse {
  schema: 'zhimeng.reader-review-decision-response.v1'
  change_set_id: string
  receipt: ReaderReviewDecisionReceipt | null
  history: ReaderReviewDecisionReceipt[]
  follow_up_change_set: ChangeSet | null
}

export type FeedbackPromotionStatus =
  | 'not_promoted'
  | 'source_verified'
  | 'conflict_checked'
  | 'regression_evaluated'
  | 'author_approved'
  | 'experience_recorded'
  | 'rejected'
  | 'rolled_back'

export type FeedbackRegressionResult =
  | 'not_run'
  | 'pass_with_watch'
  | 'fail_blocked'
  | 'skipped_rejected'

export type FeedbackPromotionAction =
  | 'verify_source'
  | 'check_conflicts'
  | 'evaluate_regression'
  | 'approve'
  | 'record_experience'
  | 'reject'
  | 'rollback_experience'

export interface FeedbackCandidate {
  candidate_id: string
  source_kind: 'reader_review_decision'
  source_decision: ReaderFindingDecisionValue
  finding_id: string
  finding_kind: ReaderFindingKind
  summary: string
  evidence_quote: string
  relative_path: string | null
  status: 'candidate' | 'rejected' | 'experience'
  promotion_status: FeedbackPromotionStatus
  confidence: 'author_explicit'
  regression_result: FeedbackRegressionResult
  experience_relative_path: string | null
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
}

export interface FeedbackCandidateBundle {
  schema: 'zhimeng.feedback-candidate-bundle.v1'
  bundle_id: string
  change_set_id: string
  project_id: string
  reader_review_decision_receipt_id: string
  reader_review_decision_receipt_hash: string
  follow_up_change_set_id: string | null
  candidates: FeedbackCandidate[]
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface FeedbackCandidateResponse {
  schema: 'zhimeng.feedback-candidate-response.v1'
  change_set_id: string
  bundle: FeedbackCandidateBundle | null
  history: FeedbackCandidateBundle[]
}

export function getReaderReview(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<ReaderReviewResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/reader-review`,
    options,
  )
}

export function createReaderReview(
  changeSetId: string,
  expectedResultFingerprint?: string | null,
  options: RuntimeClientOptions = {},
): Promise<ReaderReviewResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/reader-review`,
    options,
    jsonRequest('POST', {
      expected_result_fingerprint: expectedResultFingerprint ?? null,
    }),
  )
}

export function getReaderReviewDecision(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<ReaderReviewDecisionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/reader-review-decision`,
    options,
  )
}

export function decideReaderReview(
  changeSetId: string,
  expectedReaderReviewReceiptHash: string,
  decisions: ReaderFindingDecisionInput[],
  finalize = true,
  options: RuntimeClientOptions = {},
): Promise<ReaderReviewDecisionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/reader-review-decision`,
    options,
    jsonRequest('POST', {
      expected_reader_review_receipt_hash: expectedReaderReviewReceiptHash,
      decisions,
      finalize,
    }),
  )
}

export function getFeedbackCandidates(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<FeedbackCandidateResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/feedback-candidates`,
    options,
  )
}

export interface FeedbackCandidatePromotionDecision {
  candidate_id: string
  action: FeedbackPromotionAction
}

export interface FeedbackCandidatePromotionReceipt {
  schema: 'zhimeng.feedback-candidate-promotion-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  feedback_candidate_bundle_id: string
  feedback_candidate_bundle_hash: string
  decisions: FeedbackCandidatePromotionDecision[]
  resulting_candidates: FeedbackCandidate[]
  experience_relative_path: string | null
  experience_rows_appended: number
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
  quality_assessed: false
  novel_state_synced: false
  created_at: string
  receipt_hash: string
}

export interface FeedbackCandidatePromotionResponse {
  schema: 'zhimeng.feedback-candidate-promotion-response.v1'
  change_set_id: string
  receipt: FeedbackCandidatePromotionReceipt | null
  history: FeedbackCandidatePromotionReceipt[]
  bundle: FeedbackCandidateBundle | null
}

export function getFeedbackCandidatePromotion(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<FeedbackCandidatePromotionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/feedback-candidate-promotion`,
    options,
  )
}

export function promoteFeedbackCandidates(
  changeSetId: string,
  expectedBundleHash: string,
  decisions: FeedbackCandidatePromotionDecision[],
  options: RuntimeClientOptions = {},
): Promise<FeedbackCandidatePromotionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/feedback-candidate-promotion`,
    options,
    jsonRequest('POST', {
      expected_feedback_candidate_bundle_hash: expectedBundleHash,
      decisions,
    }),
  )
}


export type FormalKnowledgePromotionAction =
  | 'stage_for_review'
  | 'materialize_for_review'
  | 'author_request_formalization'
  | 'author_withhold_formalization'
  | 'commit_author_attested_formal_knowledge'
  | 'rollback_author_attested_formal_knowledge'
  | 'reject'
  | 'rollback_stage'

export interface FormalKnowledgePromotionDecision {
  candidate_id: string
  action: FormalKnowledgePromotionAction
}

export interface FormalKnowledgePromotionItem {
  candidate_id: string
  finding_id: string
  finding_kind: ReaderFindingKind
  summary: string
  evidence_quote: string
  relative_path: string | null
  experience_relative_path: string | null
  promotion_status:
    | 'experience_recorded'
    | 'formal_promotion_staged'
    | 'formal_promotion_materialized'
    | 'formal_promotion_author_requested'
    | 'formal_promotion_author_withheld'
    | 'formal_promotion_author_committed'
    | 'formal_promotion_rejected'
    | 'formal_promotion_rolled_back'
  regression_result: string
  staged_package_id: string | null
  materialized_source_import_id: string | null
  materialized_knowledge_candidate_id: string | null
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
  writes_formal_knowledge: boolean
  committed_knowledge_card_id?: string | null
  committed_knowledge_version_id?: string | null
}

export interface FormalKnowledgePromotionPackage {
  schema: 'zhimeng.formal-knowledge-promotion-package.v1'
  package_id: string
  change_set_id: string
  project_id: string
  candidate_id: string
  finding_id: string
  finding_kind: ReaderFindingKind
  summary: string
  evidence_quote: string
  relative_path: string | null
  proposed_card_type: 'validator'
  proposed_card_title: string
  proposed_card_body: string
  source_kind: 'feedback_experience'
  experience_relative_path: string
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
  writes_formal_knowledge: false
  quality_assessed: false
  staged_at: string
  materialized_source_import_id: string | null
  materialized_knowledge_candidate_id: string | null
  package_hash: string
}

export interface FormalKnowledgePromotionReceipt {
  schema: 'zhimeng.formal-knowledge-promotion-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  feedback_candidate_bundle_id: string | null
  feedback_candidate_bundle_hash: string | null
  status: 'ready' | 'incomplete' | 'blocked' | 'staged' | 'materialized' | 'author_formalization_requested' | 'author_formalization_withheld' | 'author_formalization_committed' | 'rejected' | 'rolled_back'
  authorization_input_ready: boolean
  author_formalization_intent: 'none' | 'request' | 'withhold'
  author_formalization_effective: boolean
  formal_knowledge_contaminated: false
  formal_draft_allowed: false
  writes_formal_knowledge: boolean
  quality_assessed: false
  checks: Array<{ code: string; required: boolean; present: boolean; detail: string; candidate_id?: string | null }>
  missing_required: string[]
  items: FormalKnowledgePromotionItem[]
  packages: FormalKnowledgePromotionPackage[]
  queue_relative_path: string | null
  queue_rows_appended: number
  policy_version: 'local-v1-formal-knowledge-promotion-1'
  evaluated_at: string
  receipt_hash: string
}

export interface FormalKnowledgePromotionResponse {
  schema: 'zhimeng.formal-knowledge-promotion-response.v1'
  change_set_id: string
  receipt: FormalKnowledgePromotionReceipt | null
  history: FormalKnowledgePromotionReceipt[]
  not_claiming: string[]
}

export function getFormalKnowledgePromotion(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<FormalKnowledgePromotionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/formal-knowledge-promotion`,
    options,
  )
}

export function evaluateFormalKnowledgePromotion(
  changeSetId: string,
  expectedBundleHash?: string | null,
  candidateIds: string[] = [],
  options: RuntimeClientOptions = {},
): Promise<FormalKnowledgePromotionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/formal-knowledge-promotion/evaluate`,
    options,
    jsonRequest('POST', {
      expected_feedback_candidate_bundle_hash: expectedBundleHash ?? null,
      candidate_ids: candidateIds,
    }),
  )
}

export function actFormalKnowledgePromotion(
  changeSetId: string,
  expectedBundleHash: string,
  decisions: FormalKnowledgePromotionDecision[],
  options: RuntimeClientOptions = {},
): Promise<FormalKnowledgePromotionResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/formal-knowledge-promotion`,
    options,
    jsonRequest('POST', {
      expected_feedback_candidate_bundle_hash: expectedBundleHash,
      decisions,
    }),
  )
}

export interface EvalCorpusArtifactRef {
  kind: string
  present: boolean
  relative_artifact: string | null
  content_sha256: string | null
  desensitized: true
}

export interface EvalCorpusExportReceipt {
  schema: 'zhimeng.eval-corpus-export-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back' | 'ready_candidate'
  export_relative_path: string
  export_file_sha256: string
  artifact_count: number
  artifacts: EvalCorpusArtifactRef[]
  redaction_policy: 'local-v1-eval-corpus-redact-1'
  formal_draft_allowed: false
  quality_assessed: false
  formal_knowledge_contaminated: false
  training_ready: false
  created_at: string
  receipt_hash: string
}

export interface EvalCorpusExportResponse {
  schema: 'zhimeng.eval-corpus-export-response.v1'
  change_set_id: string
  receipt: EvalCorpusExportReceipt | null
  history: EvalCorpusExportReceipt[]
  package: Record<string, unknown> | null
  not_claiming: string[]
}

export function getEvalCorpusExport(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<EvalCorpusExportResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/eval-corpus-export`,
    options,
  )
}

export function exportEvalCorpus(
  changeSetId: string,
  options: RuntimeClientOptions = {},
): Promise<EvalCorpusExportResponse> {
  return fetchRuntimeJson(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/eval-corpus-export`,
    options,
    jsonRequest('POST', {
      include_feedback_experience: true,
      include_formal_knowledge_promotion: true,
      include_gates: true,
      include_writeback: true,
    }),
  )
}

