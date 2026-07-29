import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

// Context compilation may include the operator-configured distillation command,
// whose supported timeout is up to 600 seconds.
export const CONTEXT_PACK_COMPILE_TIMEOUT_MS = 610_000

export type ContextAuthority =
  | 'project_truth'
  | 'formal_knowledge'
  | 'reference'
  | 'risk'

export type ContextReadiness = 'ready' | 'degraded' | 'blocked'

export interface FormalKnowledgeCardSelector {
  card_id: string
  version_id: string
  content_hash: string
}

export interface FormalKnowledgeSelector {
  schema: 'zhimeng.formal-knowledge-selector.v1'
  source_import_id: string
  formalization_receipt_id: string
  formalization_receipt_hash: string
  expected_knowledge_fingerprint: string
  cards: FormalKnowledgeCardSelector[]
}

export interface ContextCreateRequest {
  objective: string
  profile: string
  active_chapter_id: string | null
  expected_project_revision: number
  expected_project_fingerprint: string
  project_kb_state_id: string
  project_kb_manifest_hash: string
  formal_knowledge_selector?: FormalKnowledgeSelector | null
  manual_file_ids?: string[]
}

export interface ProjectKbContractBinding {
  project_kb_state_id: string | null
  project_kb_manifest_hash: string | null
}

export type ProjectKbContractBindingStatus = 'checking' | 'current' | 'legacy' | 'mismatch'

export interface KnowledgeProvenanceRef {
  schema: 'zhimeng.knowledge-provenance-ref.v1'
  source_import_id: string
  source_receipt_id: string
  source_receipt_hash: string
  source_packet_fingerprint: string
  cards_draft_hash: string
  candidate_id: string
  candidate_origin_hash: string
  card_id: string
  version_id: string
  version_content_hash: string
  version_lineage_receipt_id: string
  version_lineage_receipt_hash: string
  formalization_receipt_id: string
  formalization_receipt_hash: string
}

export interface ContextFragmentIdentity {
  source_id: string
  source_type: string
  title: string
  authority: ContextAuthority
  version: string
  source_hash: string
  priority: number
  mandatory: boolean
  knowledge_provenance?: KnowledgeProvenanceRef | null
}

export interface ContextIncludedFragment extends ContextFragmentIdentity {
  content: string
  fragment_hash: string
  estimated_characters: number
  estimated_tokens: number
  selection_reason: 'mandatory' | 'priority'
}

export interface ContextExcludedFragment extends ContextFragmentIdentity {
  eligible: boolean
  candidate_exclusion_reason: string | null
  fragment_hash: string
  estimated_characters: number
  estimated_tokens: number
  exclusion_reason: string
}

export interface ContextGate {
  code: string
  severity: 'warning' | 'blocking'
  message: string
  source_id: string | null
}

export interface ContextBudget {
  character_estimator: 'unicode_codepoints_v1'
  token_estimator: 'conservative_utf8_bytes_div3_v1'
  max_characters: number
  used_characters: number
  remaining_characters: number
  overflow_characters: number
  max_tokens: number
  used_tokens: number
  remaining_tokens: number
  overflow_tokens: number
}

export interface ContextProviderMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface ContextPack {
  schema: 'zhimeng.context-pack.v1'
  context_pack_id: string
  session_id: string
  project_id: string
  history_through_ordinal: number
  history_message_count: number
  objective: string
  profile: string
  active_chapter_id: string | null
  project_revision: number
  project_fingerprint: string
  knowledge_fingerprint: string | null
  project_kb_state_id: string | null
  project_kb_manifest_hash: string | null
  formal_knowledge_selector?: FormalKnowledgeSelector | null
  manual_file_ids?: string[]
  compiler_version: string
  policy_version: string
  budget: ContextBudget
  included: ContextIncludedFragment[]
  excluded: ContextExcludedFragment[]
  gates: ContextGate[]
  exact_provider_messages: ContextProviderMessage[]
  pack_hash: string
  injection_hash: string
  provider_messages_hash: string
  readiness: ContextReadiness
  created_at: string
}

export interface ContextPackRecord {
  schema: 'zhimeng.context-pack-record.v1'
  pack: ContextPack
  consumed_by_run_id: string | null
}

export interface ContextPackListResponse {
  schema: 'zhimeng.context-pack-list.v1'
  session_id: string
  context_packs: ContextPackRecord[]
}

export type NlclRouteSource =
  | 'native_knowledge'
  | 'external_distillation'
  | 'unavailable'

export type NlclRouteStatus = 'ready' | 'risk_ready' | 'unavailable'

export interface NlclSelectedCard {
  card_id: string
  card_type: string
  title: string
  version_id: string
  content_hash: string
  route_terms: string[]
  recommended_profiles: string[]
  negative_routes: string[]
  safety_summary: string | null
  feedback_layer: string | null
  knowledge_provenance?: KnowledgeProvenanceRef | null
}

export interface NlclChapterCall {
  ordinal: number
  skill_id: string
  purpose: string
  execution_status: 'requires_runtime_catalog_check'
  input_contract: string
  context_pack_id: string
  selected_card_ids: string[]
}

export interface NlclRouteReceipt {
  schema: 'zhimeng.nlcl-route-receipt.v1'
  route_receipt_id: string
  context_pack_id: string
  context_pack_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  active_chapter_id: string | null
  profile: string
  objective_hash: string
  knowledge_fingerprint: string | null
  project_kb_state_id: string | null
  project_kb_manifest_hash: string | null
  source: NlclRouteSource
  status: NlclRouteStatus
  selected_cards: NlclSelectedCard[]
  chapter_call_list: NlclChapterCall[]
  whole_library_load_forbidden: true
  project_truth_precedence: true
  formal_draft_allowed: false
  allowed_output: 'context_and_creation_audit_only'
  warnings: string[]
  strict_failures: string[]
  created_at: string
  receipt_hash: string
}

export type NlclRouteExecutionStatus =
  | 'awaiting_execution'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface NlclRouteExecutionReceipt {
  schema: 'zhimeng.nlcl-route-execution-receipt.v1'
  execution_receipt_id: string
  route_receipt_id: string
  route_receipt_hash: string
  context_pack_id: string
  context_pack_hash: string
  project_kb_state_id: string | null
  project_kb_manifest_hash: string | null
  call_ordinal: number
  skill_id: string
  input_contract: string
  run_id: string
  tool_call_id: string
  skill_call_id: string | null
  skill_receipt_id: string | null
  skill_receipt_hash: string | null
  execution_status: 'succeeded' | 'failed' | 'cancelled'
  domain_status: 'healthy' | 'degraded' | 'blocked' | null
  output_hash: string
  tool_result_hash: string
  created_at: string
  receipt_hash: string
}

export interface NlclRouteExecutionState {
  schema: 'zhimeng.nlcl-route-execution-state.v1'
  route_receipt_id: string
  context_pack_id: string
  call_ordinal: number
  status: NlclRouteExecutionStatus
  run_id: string | null
  tool_call_id: string | null
  receipt: NlclRouteExecutionReceipt | null
  error_code: string | null
  error_message: string | null
}

function bindingKind(binding: ProjectKbContractBinding): 'current' | 'legacy' | 'mismatch' {
  const hasState = typeof binding.project_kb_state_id === 'string'
    && binding.project_kb_state_id.trim().length > 0
  const hasManifest = typeof binding.project_kb_manifest_hash === 'string'
    && binding.project_kb_manifest_hash.trim().length > 0
  if (hasState && hasManifest) return 'current'
  if (!hasState && !hasManifest) return 'legacy'
  return 'mismatch'
}

function sameBinding(
  expected: ProjectKbContractBinding,
  actual: ProjectKbContractBinding,
): boolean {
  return expected.project_kb_state_id === actual.project_kb_state_id
    && expected.project_kb_manifest_hash === actual.project_kb_manifest_hash
}

export function projectKbContractBindingStatus(
  record: ContextPackRecord,
  route: NlclRouteReceipt | null,
  executions: NlclRouteExecutionState[] = [],
): ProjectKbContractBindingStatus {
  const pack = record.pack
  const packKind = bindingKind(pack)
  if (packKind === 'mismatch') return 'mismatch'
  if (!route) return 'checking'
  if (bindingKind(route) !== packKind || !sameBinding(pack, route)) return 'mismatch'
  for (const execution of executions) {
    if (!execution.receipt) continue
    if (bindingKind(execution.receipt) !== packKind || !sameBinding(pack, execution.receipt)) {
      return 'mismatch'
    }
  }
  return packKind
}

export interface PrewriteEvidenceRef {
  source_id: string
  source_type: string
  title: string
  authority: ContextAuthority
  version: string
  source_hash: string
  fragment_hash: string
  content_excerpt: string
  knowledge_provenance?: KnowledgeProvenanceRef | null
}

export interface PrewriteOperationPackage {
  schema: 'zhimeng.prewrite-operation-package.v1'
  operation_package_id: string
  context_pack_id: string
  context_pack_hash: string
  route_receipt_id: string
  route_receipt_hash: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  active_chapter_id: string | null
  task_goal: string
  must_obey_truth: PrewriteEvidenceRef[]
  current_character_state: PrewriteEvidenceRef[]
  relevant_evidence_refs: PrewriteEvidenceRef[]
  constraints_and_taboos: string[]
  scene_contract: {
    active_chapter_id: string | null
    task_goal: string
    evidence_source_ids: string[]
    status: 'bound' | 'unbound'
  }
  style_output_contract: {
    selected_card_ids: string[]
    selected_card_hashes: string[]
    selected_card_provenance?: KnowledgeProvenanceRef[]
    whole_library_load_forbidden: true
    status: 'bound' | 'unbound'
  }
  acceptance_gate: {
    required_checks: string[]
    minimum_review_score: number
    author_approval_required: true
    writeback_receipt_required: true
  }
  failure_routing: Array<{
    failure_signature: string
    rewrite_from: string
  }>
  library_activation_budget: {
    max_characters: number
    used_characters: number
    max_tokens: number
    used_tokens: number
    activated_source_ids: string[]
  }
  whole_library_load_forbidden: true
  readiness: 'blocked' | 'risk_draft_only'
  formal_draft_allowed: false
  missing_requirements: string[]
  warnings: string[]
  created_at: string
  package_hash: string
}

function jsonRequest(
  body: unknown,
  idempotencyKey: string,
  signal?: AbortSignal,
): RequestInit {
  return {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify(body),
    signal,
  }
}

export function createContextPack(
  sessionId: string,
  request: ContextCreateRequest,
  idempotencyKey: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ContextPackRecord> {
  const compileOptions = options.timeoutMs === undefined
    ? { ...options, timeoutMs: CONTEXT_PACK_COMPILE_TIMEOUT_MS }
    : options
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/context-packs`,
    compileOptions,
    jsonRequest(request, idempotencyKey, signal),
  )
}

export function getContextPack(
  contextPackId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ContextPackRecord> {
  return fetchRuntimeJson(
    `/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`,
    options,
    { signal },
  )
}

export function getNlclRoute(
  contextPackId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<NlclRouteReceipt> {
  return fetchRuntimeJson(
    `/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}/nlcl-route`,
    options,
    { signal },
  )
}

export function getNlclRouteExecution(
  contextPackId: string,
  callOrdinal = 1,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<NlclRouteExecutionState> {
  return fetchRuntimeJson<unknown>(
    `/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`
      + `/nlcl-route/executions/${callOrdinal}`,
    options,
    { signal },
  ).then((payload) => {
    if (
      !payload
      || typeof payload !== 'object'
      || typeof (payload as { status?: unknown }).status !== 'string'
      || typeof (payload as { call_ordinal?: unknown }).call_ordinal !== 'number'
    ) {
      throw new Error('Runtime 返回了无效的 NLCL 执行状态')
    }
    return payload as NlclRouteExecutionState
  })
}

export function getPrewriteOperationPackage(
  contextPackId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<PrewriteOperationPackage> {
  return fetchRuntimeJson(
    `/api/v1/agent/context-packs/${encodeURIComponent(contextPackId)}`
      + '/prewrite-operation-package',
    options,
    { signal },
  )
}

export async function listSessionContextPacks(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ContextPackRecord[]> {
  const payload = await fetchRuntimeJson<ContextPackListResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/context-packs?limit=100`,
    options,
    { signal },
  )
  return payload.context_packs
}

export function getRunContextPack(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<ContextPackRecord> {
  return fetchRuntimeJson(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/context-pack`,
    options,
    { signal },
  )
}
