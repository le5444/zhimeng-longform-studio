import type { DistillationInvocationSnapshot } from './distillation'
import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

// The Skill may spend up to 180s in PowerShell search and another 60s
// resolving structured candidates from the downloader service.
export const SOURCE_IMPORT_SEARCH_TIMEOUT_MS = 300_000

export type TomatoSearchResolution =
  | 'auto_selected'
  | 'selection_required'
  | 'not_found'

export type SourceImportStatus =
  | 'queued'
  | 'downloading'
  | 'source_ready'
  | 'drafting'
  | 'awaiting_review'
  | 'formalizing'
  | 'completed'
  | 'cancel_requested'
  | 'cancelled'
  | 'failed'
  | 'interrupted'

export type LocalSourceFormat = 'txt' | 'epub'

export interface TomatoSearchCandidate {
  index: number
  book_id: string
  title: string
  author: string
}

export interface LocalFileSourceCandidate {
  index: 1
  source_path: string
  source_format: LocalSourceFormat
  title: string
  author: string
  source_size_bytes: number
  source_sha256: string
  source_locator_hash: string
}

export interface TomatoSearchResponse {
  schema: 'zhimeng.tomato-search.v1'
  query: string
  resolution: TomatoSearchResolution
  candidates: TomatoSearchCandidate[]
  selected_candidate: TomatoSearchCandidate | null
}

export interface SourceArtifact {
  kind: string
  path: string
  size_bytes: number | null
  sha256: string | null
}

export interface PreparedSourcePacket {
  title: string
  author: string
  download_scope: string
  packet_dir: string
  packet_manifest_path: string
  epub_path: string
  downloaded_epub_path: string
  text_path: string
  metadata_path: string
  char_count: number
  chapter_marker_count: number
  artifacts: SourceArtifact[]
}

export interface PreparedLocalSourcePacket {
  source_backend: 'generic_local_file'
  source_format: LocalSourceFormat
  title: string
  author: string
  download_scope: 'full'
  packet_dir: string
  packet_manifest_path: string
  packet_source_path: string
  text_path: string
  metadata_path: string
  char_count: number
  chapter_marker_count: number
  source_file_size_bytes: number
  source_file_sha256: string
  packet_source_sha256: string
  text_sha256: string
  artifacts: SourceArtifact[]
}

export interface DistillationDraft {
  status: 'draft_ready'
  output_dir: string
  cards_path: string
  report_path: string
  validation_report_path: string
  envelope_path: string
  card_count: number
  requires_review: true
  artifacts: SourceArtifact[]
}

export interface SourceImportProgress {
  stage: string
  message: string
  saved_chapters: number | null
  total_chapters: number | null
  percent: number | null
}

export interface TomatoSourceImportJob {
  schema: 'zhimeng.source-import.v1'
  import_id: string
  provider: 'tomato'
  query: string
  candidate: TomatoSearchCandidate
  status: SourceImportStatus
  retry_of: string | null
  progress: SourceImportProgress
  source_packet: PreparedSourcePacket | null
  distillation_draft: DistillationDraft | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface LocalSourceImportJob {
  schema: 'zhimeng.source-import.v2'
  import_id: string
  provider: 'local_file'
  query: string
  candidate: LocalFileSourceCandidate
  status: SourceImportStatus
  retry_of: string | null
  progress: SourceImportProgress
  source_packet: PreparedLocalSourcePacket | null
  distillation_draft: DistillationDraft | null
  error: string | null
  created_at: string
  updated_at: string
}

export type SourceImportJob = TomatoSourceImportJob | LocalSourceImportJob

export type SourceImportLineageStatus = 'verified' | 'legacy_unverified'

export interface SourceImportRequestEvidence {
  query: string
  candidate_index: number
  book_id: string
  title: string
  author: string
}

export interface LocalSourceImportRequestEvidence {
  source_format: LocalSourceFormat
  title: string
  author: string
  source_size_bytes: number
  source_sha256: string
  source_locator_hash: string
}

export interface SourceImportToolEvidence {
  tool_id: string
  tool_version: string
  tool_fingerprint: string
}

export interface SourceImportArtifactEvidence {
  kind: string
  relative_path: string
  size_bytes: number
  sha256: string
}

export interface SourceImportExecutionReceipt {
  schema: 'zhimeng.source-import-execution-receipt.v1'
  receipt_id: string
  source_import_id: string
  run_id: string
  revision: 1
  idempotency_key: string
  lineage_status: 'verified'
  provider: 'tomato'
  request: SourceImportRequestEvidence
  request_hash: string
  retry_of: string | null
  prepare_invocation_id: string
  prepare_tool: SourceImportToolEvidence
  prepare_input_hash: string
  prepare_output_hash: string
  download_scope: 'full'
  source_packet_fingerprint: string
  packet_manifest_hash: string
  packet_text_hash: string
  packet_metadata_hash: string
  source_artifacts: SourceImportArtifactEvidence[]
  distillation_invocation_id: string
  distillation_tool: SourceImportToolEvidence
  distillation_input_hash: string
  distillation_output_hash: string
  draft_fingerprint: string
  cards_draft_hash: string
  envelope_hash: string
  envelope_schema: 'novel_distillation.source_packet_draft.v1'
  envelope_status: 'draft_ready'
  card_count: number
  draft_artifacts: SourceImportArtifactEvidence[]
  created_at: string
  receipt_hash: string
}

export interface LocalSourceImportExecutionReceipt {
  schema: 'zhimeng.source-import-execution-receipt.v2'
  receipt_id: string
  source_import_id: string
  run_id: string
  revision: 1
  idempotency_key: string
  lineage_status: 'verified'
  provider: 'local_file'
  request: LocalSourceImportRequestEvidence
  request_hash: string
  retry_of: string | null
  prepare_invocation_id: string
  prepare_tool: SourceImportToolEvidence
  prepare_input_hash: string
  prepare_output_hash: string
  download_scope: 'full'
  source_format: LocalSourceFormat
  source_locator_hash: string
  source_file_size_bytes: number
  source_file_sha256: string
  source_packet_fingerprint: string
  packet_manifest_hash: string
  packet_source_hash: string
  packet_text_hash: string
  packet_metadata_hash: string
  packet_source_size_bytes: number | null
  packet_text_size_bytes: number | null
  source_artifacts: SourceImportArtifactEvidence[]
  distillation_invocation_id: string
  distillation_tool: SourceImportToolEvidence
  distillation_input_hash: string
  distillation_output_hash: string
  draft_fingerprint: string
  cards_draft_hash: string
  envelope_hash: string
  envelope_schema: 'novel_distillation.source_packet_draft.v1'
  envelope_status: 'draft_ready'
  card_count: number
  draft_artifacts: SourceImportArtifactEvidence[]
  created_at: string
  receipt_hash: string
}

export interface SourceImportExecutionReceiptRecord {
  schema: 'zhimeng.source-import-execution-receipt-record.v1'
  source_import_id: string
  lineage_status: SourceImportLineageStatus
  receipt: SourceImportExecutionReceipt | LocalSourceImportExecutionReceipt | null
}

interface SourceImportListResponse {
  schema: 'zhimeng.source-import-list.v1'
  imports: SourceImportJob[]
}

export interface SourceImportDistillationPrepareResult {
  schema: 'zhimeng.source-import-distillation-prepare-result.v1'
  snapshot: DistillationInvocationSnapshot
  anchor_catalog: Record<string, unknown>
  text_by_anchor_id: Record<string, string>
}

export interface SourceImportDistillationPrepareRequest {
  project_id: string
  expected_project_revision?: number | null
  expected_project_fingerprint?: string | null
  idempotency_key?: string | null
}

function jsonRequest(body: unknown, signal?: AbortSignal): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  }
}

function withDefaultTimeout(
  options: RuntimeClientOptions,
  timeoutMs: number,
): RuntimeClientOptions {
  return options.timeoutMs === undefined
    ? { ...options, timeoutMs }
    : options
}

export function searchTomatoNovels(
  query: string,
  options: RuntimeClientOptions = {},
): Promise<TomatoSearchResponse> {
  return fetchRuntimeJson(
    '/api/v1/source-imports/tomato/search',
    withDefaultTimeout(options, SOURCE_IMPORT_SEARCH_TIMEOUT_MS),
    jsonRequest({ query }),
  )
}

export function createSourceImport(
  query: string,
  candidate: TomatoSearchCandidate,
  options: RuntimeClientOptions = {},
): Promise<SourceImportJob> {
  return fetchRuntimeJson(
    '/api/v1/source-imports',
    options,
    jsonRequest({ provider: 'tomato', query, candidate }),
  )
}

export function inspectLocalSource(
  sourcePath: string,
  options: RuntimeClientOptions = {},
): Promise<LocalFileSourceCandidate> {
  return fetchRuntimeJson(
    '/api/v1/source-imports/local/inspect',
    options,
    jsonRequest({ source_path: sourcePath }),
  )
}

export function createLocalSourceImport(
  candidate: LocalFileSourceCandidate,
  options: RuntimeClientOptions = {},
): Promise<LocalSourceImportJob> {
  return fetchRuntimeJson(
    '/api/v1/source-imports',
    options,
    jsonRequest({
      provider: 'local_file',
      query: candidate.source_path,
      candidate,
    }),
  )
}

export async function listSourceImports(
  options: RuntimeClientOptions = {},
): Promise<SourceImportJob[]> {
  const payload = await fetchRuntimeJson<SourceImportListResponse>(
    '/api/v1/source-imports',
    options,
  )
  return payload.imports
}

export function getSourceImport(
  importId: string,
  options: RuntimeClientOptions = {},
): Promise<SourceImportJob> {
  return fetchRuntimeJson(
    `/api/v1/source-imports/${encodeURIComponent(importId)}`,
    options,
  )
}

export function getSourceImportExecutionReceipt(
  importId: string,
  options: RuntimeClientOptions = {},
): Promise<SourceImportExecutionReceiptRecord> {
  return fetchRuntimeJson(
    `/api/v1/source-imports/${encodeURIComponent(importId)}/execution-receipt`,
    options,
  )
}

export function prepareSourceImportDistillationInvocation(
  importId: string,
  request: SourceImportDistillationPrepareRequest,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<SourceImportDistillationPrepareResult> {
  return fetchRuntimeJson(
    `/api/v1/source-imports/${encodeURIComponent(importId)}/distillation-invocation/prepare`,
    options,
    jsonRequest(request, signal),
  )
}

export function cancelSourceImport(
  importId: string,
  options: RuntimeClientOptions = {},
): Promise<SourceImportJob> {
  return fetchRuntimeJson(
    `/api/v1/source-imports/${encodeURIComponent(importId)}/cancel`,
    options,
    jsonRequest({}),
  )
}

export function retrySourceImport(
  importId: string,
  options: RuntimeClientOptions = {},
): Promise<SourceImportJob> {
  return fetchRuntimeJson(
    `/api/v1/source-imports/${encodeURIComponent(importId)}/retry`,
    options,
    jsonRequest({}),
  )
}
