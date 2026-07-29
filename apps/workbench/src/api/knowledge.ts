import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type KnowledgeCandidateStatus = 'pending' | 'promoted' | 'deferred' | 'rejected'
export type KnowledgeCardStatus = 'active' | 'disabled'
export type KnowledgeLineageStatus = 'verified' | 'legacy_unverified'
export type KnowledgeVersionFormalizationStatus =
  | 'verified'
  | 'awaiting_formalization'
  | 'abandoned'
  | 'legacy_unverified'
export type KnowledgeFormalizationStatus =
  | 'verified'
  | 'awaiting_review'
  | 'legacy_unverified'

export type KnowledgeScopeType = 'application' | 'project'

export interface KnowledgeScopeRef {
  schema: 'zhimeng.knowledge-scope-ref.v1'
  scope_id: string
  scope_type: KnowledgeScopeType
  project_binding_id: string | null
  state_revision: number
}

export interface KnowledgeScopeRecord {
  schema: 'zhimeng.knowledge-scope-record.v1'
  scope_id: string
  scope_type: KnowledgeScopeType
  project_binding_id: string | null
  state_revision: number
  created_at: string
  updated_at: string
}

export interface KnowledgeReviewIssue {
  code: string
  field: string
  message: string
}

export interface KnowledgeCandidateOrigin {
  schema: 'zhimeng.knowledge-candidate-origin.v1'
  source_receipt_id: string
  source_receipt_hash: string
  source_packet_fingerprint: string
  draft_fingerprint: string
  cards_draft_hash: string
  source_card_index: number
  source_card_hash: string
  origin_hash: string
}

export interface KnowledgeCandidate {
  schema: 'zhimeng.knowledge-candidate.v2'
  candidate_id: string
  source_import_id: string
  source_card_index: number
  source_draft_hash: string
  lineage_status: KnowledgeLineageStatus
  origin: KnowledgeCandidateOrigin | null
  runtime_distillation_invocation_id?: string | null
  card_id: string
  card_type: string
  status: KnowledgeCandidateStatus
  revision: number
  content_hash: string
  card: Record<string, unknown>
  issues: KnowledgeReviewIssue[]
  promoted_version_id: string | null
  created_at: string
  updated_at: string
}

export interface KnowledgeCardVersion {
  schema: 'zhimeng.knowledge-card-version.v2'
  version_id: string
  card_id: string
  version_number: number
  content_hash: string
  card: Record<string, unknown>
  source_candidate_id: string
  lineage_status: KnowledgeLineageStatus
  lineage: KnowledgeVersionLineageReceipt | null
  formalization_status: KnowledgeVersionFormalizationStatus
  formalization: KnowledgeFormalizationRef | null
  created_at: string
}

export interface KnowledgeVersionLineageReceipt {
  schema: 'zhimeng.knowledge-version-lineage-receipt.v1' | 'zhimeng.knowledge-version-lineage-receipt.v2'
  lineage_receipt_id: string
  version_id: string
  card_id: string
  version_content_hash: string
  candidate_id: string
  approved_candidate_revision: number
  approved_candidate_content_hash: string
  candidate_origin: KnowledgeCandidateOrigin
  source_import_id: string
  activation_mode: 'initial' | 'replace'
  replacement_authorized: boolean
  activation_expected_version_id: string
  activation_expected_state_revision: number
  created_at: string
  receipt_hash: string
  scope?: KnowledgeScopeRef
}

export interface KnowledgeFormalizationRef {
  formalization_receipt_id: string
  formalization_receipt_hash: string
}

export interface KnowledgeCard {
  schema: 'zhimeng.knowledge-card.v2'
  card_id: string
  status: KnowledgeCardStatus
  state_revision: number
  active_version_id: string
  active_version: KnowledgeCardVersion
  versions: KnowledgeCardVersion[]
  created_at: string
  updated_at: string
}

export interface KnowledgeCandidateList {
  schema: 'zhimeng.knowledge-candidate-list.v2'
  candidates: KnowledgeCandidate[]
  candidate_set_hash: string | null
}

export interface KnowledgeCardList {
  schema: 'zhimeng.knowledge-card-list.v2'
  cards: KnowledgeCard[]
  fingerprint: string
}

export interface KnowledgeScopedCardList {
  schema: 'zhimeng.knowledge-scoped-card-list.v1'
  scope: KnowledgeScopeRecord
  includes_application_scope: boolean
  cards: KnowledgeCard[]
  inherited_card_ids: string[]
  fingerprint: string
}

export interface KnowledgeFormalizationDecision {
  candidate_id: string
  source_card_index: number
  origin_hash: string
  decision: 'promoted' | 'rejected' | 'deferred'
  revision: number
  content_hash: string
  promoted_version_id: string | null
  version_lineage_receipt_id: string | null
  version_lineage_receipt_hash: string | null
}

export interface KnowledgeFormalizationReceipt {
  schema: 'zhimeng.knowledge-formalization-receipt.v1' | 'zhimeng.knowledge-formalization-receipt.v2'
  formalization_receipt_id: string
  source_import_id: string
  source_receipt_id: string
  source_receipt_hash: string
  source_packet_fingerprint: string
  draft_fingerprint: string
  cards_draft_hash: string
  candidate_set_hash: string
  decisions: KnowledgeFormalizationDecision[]
  knowledge_state_hash: string
  created_at: string
  receipt_hash: string
  scope?: KnowledgeScopeRef
}

export interface KnowledgeFormalizationRecord {
  schema: 'zhimeng.knowledge-formalization-record.v1'
  source_import_id: string
  status: KnowledgeFormalizationStatus
  receipt: KnowledgeFormalizationReceipt | null
}

export interface KnowledgeFormalizationRequest {
  expected_source_receipt_hash: string
  expected_draft_hash: string
  expected_candidate_set_hash: string
}

export type ProjectKbStatus = 'missing' | 'current' | 'stale' | 'tampered'

export interface ProjectKbCounts {
  candidates: number
  cards: number
  active_cards: number
  formalization_receipts: number
  active_index_entries: number
}

export interface ProjectKbManifest {
  schema: 'zhimeng.project-kb-state.v1'
  state_id: string
  state_hash: string
  project: {
    schema: 'zhimeng.project-kb-project-ref.v1'
    project_id: string
    revision: number
    fingerprint: string
  }
  knowledge_scope: KnowledgeScopeRef
  knowledge_fingerprint: string
  inherited_card_ids: string[]
  source_of_truth: {
    schema: 'zhimeng.project-kb-source-of-truth.v1'
    project_files: 'work_truth'
    runtime_sqlite: 'run_truth'
    derived: true
    rebuildable: true
  }
  counts: ProjectKbCounts
  artifacts: Array<{
    logical_path: string
    size_bytes: number
    sha256: string
  }>
  manifest_hash: string
}

export interface ProjectKbStateRecord {
  schema: 'zhimeng.project-kb-state-record.v1'
  project_id: string
  status: ProjectKbStatus
  runtime_relative_root: string
  pointer: {
    schema: 'zhimeng.project-kb-current-pointer.v1'
    project_id: string
    state_id: string
    state_hash: string
    manifest_hash: string
    manifest_file_sha256: string
    pointer_hash: string
  } | null
  manifest: ProjectKbManifest | null
  reasons: string[]
}

export interface ProjectKbBinding {
  project_kb_state_id: string
  project_kb_manifest_hash: string
}

export interface ProjectKbBindingExpectation {
  projectId: string
  projectRevision?: number | null
  projectFingerprint?: string | null
  knowledgeScopeStateRevision?: number | null
  knowledgeFingerprint?: string | null
}

function nonBlank(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function sha256(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{64}$/i.test(value)
}

export function currentProjectKbBinding(
  record: ProjectKbStateRecord | null | undefined,
  expectation: ProjectKbBindingExpectation,
): ProjectKbBinding | null {
  if (record?.status !== 'current' || !record.pointer || !record.manifest) return null
  const { pointer, manifest } = record
  if (
    !nonBlank(pointer.state_id)
    || !sha256(pointer.state_hash)
    || !sha256(pointer.manifest_hash)
    || !sha256(pointer.manifest_file_sha256)
    || !sha256(pointer.pointer_hash)
    || !nonBlank(manifest.state_id)
    || !sha256(manifest.state_hash)
    || !sha256(manifest.manifest_hash)
    || !sha256(manifest.project.fingerprint)
    || !sha256(manifest.knowledge_fingerprint)
    || record.project_id !== expectation.projectId
    || pointer.project_id !== expectation.projectId
    || manifest.project.project_id !== expectation.projectId
    || pointer.state_id !== manifest.state_id
    || pointer.state_hash !== manifest.state_hash
    || pointer.manifest_hash !== manifest.manifest_hash
  ) return null
  if (
    expectation.projectRevision !== null
    && expectation.projectRevision !== undefined
    && manifest.project.revision !== expectation.projectRevision
  ) return null
  if (
    expectation.projectFingerprint
    && manifest.project.fingerprint !== expectation.projectFingerprint
  ) return null
  if (
    expectation.knowledgeScopeStateRevision !== null
    && expectation.knowledgeScopeStateRevision !== undefined
    && manifest.knowledge_scope.state_revision !== expectation.knowledgeScopeStateRevision
  ) return null
  if (
    expectation.knowledgeFingerprint
    && manifest.knowledge_fingerprint !== expectation.knowledgeFingerprint
  ) return null
  return {
    project_kb_state_id: manifest.state_id,
    project_kb_manifest_hash: manifest.manifest_hash,
  }
}

export interface ProjectKbRebuildRequest {
  expected_project_revision: number
  expected_project_fingerprint: string
  expected_scope_state_revision: number
  expected_knowledge_fingerprint: string
}

export interface ProjectKbRebuildResult {
  schema: 'zhimeng.project-kb-rebuild-result.v1'
  reused_snapshot: boolean
  record: ProjectKbStateRecord
}

function jsonRequest(method: 'POST' | 'PUT', body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function projectKnowledgePath(projectId: string, suffix: string): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/knowledge${suffix}`
}

export async function importKnowledgeCandidates(
  sourceImportId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidateList> {
  return fetchRuntimeJson<KnowledgeCandidateList>(
    `/api/v1/knowledge/source-imports/${encodeURIComponent(sourceImportId)}/candidates`,
    options,
    { method: 'POST' },
  )
}

export async function listKnowledgeCandidates(
  sourceImportId?: string | null,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidateList> {
  const query = sourceImportId
    ? `?source_import_id=${encodeURIComponent(sourceImportId)}`
    : ''
  return fetchRuntimeJson<KnowledgeCandidateList>(
    `/api/v1/knowledge/candidates${query}`,
    options,
  )
}

export function getKnowledgeSourceFormalization(
  sourceImportId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeFormalizationRecord> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/source-imports/${encodeURIComponent(sourceImportId)}/formalization`,
    options,
  )
}

export function updateKnowledgeCandidate(
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  card: Record<string, unknown>,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidate> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/candidates/${encodeURIComponent(candidate.candidate_id)}`,
    options,
    jsonRequest('PUT', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
      card,
    }),
  )
}

export function promoteKnowledgeCandidate(
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  replaceActive: boolean,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/candidates/${encodeURIComponent(candidate.candidate_id)}/promote`,
    options,
    jsonRequest('POST', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
      replace_active: replaceActive,
    }),
  )
}

export function rejectKnowledgeCandidate(
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidate> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/candidates/${encodeURIComponent(candidate.candidate_id)}/reject`,
    options,
    jsonRequest('POST', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
    }),
  )
}

export function finalizeKnowledgeSourceImport(
  sourceImportId: string,
  request: KnowledgeFormalizationRequest,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeFormalizationReceipt> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/source-imports/${encodeURIComponent(sourceImportId)}/finalize`,
    options,
    jsonRequest('POST', request),
  )
}

export function listKnowledgeCards(
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCardList> {
  return fetchRuntimeJson('/api/v1/knowledge/cards', options)
}

export function setKnowledgeCardStatus(
  card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
  status: KnowledgeCardStatus,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  const action = status === 'active' ? 'enable' : 'disable'
  return fetchRuntimeJson(
    `/api/v1/knowledge/cards/${encodeURIComponent(card.card_id)}/${action}`,
    options,
    jsonRequest('POST', {
      expected_state_revision: card.state_revision,
      expected_active_version_id: card.active_version_id,
    }),
  )
}

export function rollbackKnowledgeCard(
  card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
  targetVersionId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  return fetchRuntimeJson(
    `/api/v1/knowledge/cards/${encodeURIComponent(card.card_id)}/rollback`,
    options,
    jsonRequest('POST', {
      expected_state_revision: card.state_revision,
      expected_active_version_id: card.active_version_id,
      target_version_id: targetVersionId,
    }),
  )
}

// Project-scoped endpoints are explicit wrappers so the legacy application
// scope remains unchanged for existing callers and fixtures.
export function getProjectKnowledgeScope(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeScopeRecord> {
  return fetchRuntimeJson(
    projectKnowledgePath(projectId, '/scope'),
    options,
  )
}

export function getProjectKbState(
  projectId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ProjectKbStateRecord> {
  return fetchRuntimeJson(
    projectKnowledgePath(projectId, '/kb-state'),
    options,
    { signal },
  )
}

export function rebuildProjectKbState(
  projectId: string,
  request: ProjectKbRebuildRequest,
  options: RuntimeClientOptions = {},
): Promise<ProjectKbRebuildResult> {
  return fetchRuntimeJson(
    projectKnowledgePath(projectId, '/kb-state/rebuild'),
    options,
    jsonRequest('POST', request),
  )
}

export function importProjectKnowledgeCandidates(
  projectId: string,
  sourceImportId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidateList> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/source-imports/${encodeURIComponent(sourceImportId)}/candidates`,
    ),
    options,
    { method: 'POST' },
  )
}

export function listProjectKnowledgeCandidates(
  projectId: string,
  sourceImportId?: string | null,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidateList> {
  const query = sourceImportId
    ? `?source_import_id=${encodeURIComponent(sourceImportId)}`
    : ''
  return fetchRuntimeJson(
    projectKnowledgePath(projectId, `/candidates${query}`),
    options,
  )
}

export function getProjectKnowledgeSourceFormalization(
  projectId: string,
  sourceImportId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeFormalizationRecord> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/source-imports/${encodeURIComponent(sourceImportId)}/formalization`,
    ),
    options,
  )
}

export function updateProjectKnowledgeCandidate(
  projectId: string,
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  card: Record<string, unknown>,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidate> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/candidates/${encodeURIComponent(candidate.candidate_id)}`,
    ),
    options,
    jsonRequest('PUT', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
      card,
    }),
  )
}

export function promoteProjectKnowledgeCandidate(
  projectId: string,
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  replaceActive: boolean,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/candidates/${encodeURIComponent(candidate.candidate_id)}/promote`,
    ),
    options,
    jsonRequest('POST', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
      replace_active: replaceActive,
    }),
  )
}

export function rejectProjectKnowledgeCandidate(
  projectId: string,
  candidate: Pick<KnowledgeCandidate, 'candidate_id' | 'revision' | 'content_hash'>,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCandidate> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/candidates/${encodeURIComponent(candidate.candidate_id)}/reject`,
    ),
    options,
    jsonRequest('POST', {
      expected_revision: candidate.revision,
      expected_content_hash: candidate.content_hash,
    }),
  )
}

export function finalizeProjectKnowledgeSourceImport(
  projectId: string,
  sourceImportId: string,
  request: KnowledgeFormalizationRequest,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeFormalizationReceipt> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/source-imports/${encodeURIComponent(sourceImportId)}/formalization/finalize`,
    ),
    options,
    jsonRequest('POST', request),
  )
}

export function listProjectKnowledgeCards(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeScopedCardList> {
  return fetchRuntimeJson(
    projectKnowledgePath(projectId, '/cards'),
    options,
  )
}

export function setProjectKnowledgeCardStatus(
  projectId: string,
  card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
  status: KnowledgeCardStatus,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  const action = status === 'active' ? 'enable' : 'disable'
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/cards/${encodeURIComponent(card.card_id)}/${action}`,
    ),
    options,
    jsonRequest('POST', {
      expected_state_revision: card.state_revision,
      expected_active_version_id: card.active_version_id,
    }),
  )
}

export function rollbackProjectKnowledgeCard(
  projectId: string,
  card: Pick<KnowledgeCard, 'card_id' | 'state_revision' | 'active_version_id'>,
  targetVersionId: string,
  options: RuntimeClientOptions = {},
): Promise<KnowledgeCard> {
  return fetchRuntimeJson(
    projectKnowledgePath(
      projectId,
      `/cards/${encodeURIComponent(card.card_id)}/rollback`,
    ),
    options,
    jsonRequest('POST', {
      expected_state_revision: card.state_revision,
      expected_active_version_id: card.active_version_id,
      target_version_id: targetVersionId,
    }),
  )
}
