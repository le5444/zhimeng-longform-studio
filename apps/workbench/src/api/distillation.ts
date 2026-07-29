import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

// Runtime allows an operator-configured Skill timeout up to 600 seconds. Keep
// the client deadline later so its typed 504 response wins the race.
export const DISTILLATION_PREVIEW_TIMEOUT_MS = 610_000

export type DistillationHealthState = 'healthy' | 'degraded' | 'blocked'

export type DistillationCardType =
  | 'work'
  | 'author'
  | 'genre'
  | 'scene'
  | 'voice'
  | 'trope'
  | 'platform'
  | 'packaging'
  | 'benchmark'
  | 'validator'

export type DistillationProfile =
  | 'scene'
  | 'chapter'
  | 'draft_assist'
  | 'mechanism_transfer'
  | 'review'
  | 'rewrite'
  | 'volume'
  | 'deai'
  | 'style_calibration'
  | 'story_unit'
  | 'debt_recall'
  | 'packaging'
  | 'benchmark'

export interface ValidationLadderDebt {
  risk_ready_cards: number
  warn_steps: number
}

export interface DistillationHealth {
  schema: 'zhimeng.distillation-health.v1'
  read_only: true
  health: DistillationHealthState
  structural_health: DistillationHealthState
  validation_maturity: 'validated' | 'risk_ready' | 'degraded'
  card_count: number
  counts: Partial<Record<DistillationCardType, number>>
  validation_ladder_debt: ValidationLadderDebt
  hard_rule_eligible_counts: Partial<Record<DistillationCardType, number>>
  library_fingerprint: string
  tool_fingerprint: string
  observed_at: string
}

export interface DistillationCoverageCard {
  card_id: string
  card_type: DistillationCardType
  title: string
  summary: string
  usage_mode: 'hard_rule' | 'risk_ready'
  hard_rule_eligible: boolean
  safety_risk: number
  tags: string[]
}

export interface DistillationCoverage {
  schema: 'zhimeng.distillation-coverage.v1'
  read_only: true
  query: string
  profile: DistillationProfile
  target_health: DistillationHealthState
  writing_gate:
    | 'proceed_with_distillation_context'
    | 'pause_and_request_benchmark_sources'
    | 'validate_existing_cards_or_risk_draft'
    | 'pause_and_request_sources'
    | 'proceed_with_risk_only'
    | 'ask_user_or_proceed_with_risk'
  recommended_next_step:
    | 'run_context_and_write'
    | 'validate_existing_cards_or_run_risk_context'
    | 'run_risk_context_only'
    | 'ask_user_for_same_type_sources'
    | 'ask_user_or_run_context_with_risk'
  risk_preview_allowed: boolean
  formal_injection_allowed: boolean
  source_coverage_ratio: number
  formal_coverage_ratio: number
  matched_cards: number
  available_matched_cards: number
  selected_type_counts: Partial<Record<DistillationCardType, number>>
  selected_hard_rule_type_counts: Partial<Record<DistillationCardType, number>>
  selected_risk_ready_type_counts: Partial<Record<DistillationCardType, number>>
  source_quota_gaps: Partial<Record<DistillationCardType, number>>
  formal_quota_gaps: Partial<Record<DistillationCardType, number>>
  missing_types: DistillationCardType[]
  formal_missing_types: DistillationCardType[]
  selected_cards: DistillationCoverageCard[]
  feed_request: string
  library_fingerprint: string
  tool_fingerprint: string
  observed_at: string
}

function jsonRequest(
  body: unknown,
  method = 'POST',
  signal?: AbortSignal,
): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  }
}

function withPreviewTimeout(options: RuntimeClientOptions): RuntimeClientOptions {
  return options.timeoutMs === undefined
    ? { ...options, timeoutMs: DISTILLATION_PREVIEW_TIMEOUT_MS }
    : options
}

export function fetchDistillationHealth(
  options: RuntimeClientOptions = {},
): Promise<DistillationHealth> {
  return fetchRuntimeJson(
    '/api/v1/distillation/health',
    withPreviewTimeout(options),
  )
}

export function previewDistillationCoverage(
  query: string,
  profile: DistillationProfile,
  options: RuntimeClientOptions = {},
): Promise<DistillationCoverage> {
  return fetchRuntimeJson(
    '/api/v1/distillation/coverage',
    withPreviewTimeout(options),
    jsonRequest({ query, profile }),
  )
}

export type DistillationInvocationStatus =
  | 'prepared'
  | 'running'
  | 'output_persisted'
  | 'interrupted'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'abandoned'

export type DistillationReceiptKind =
  | 'prepared'
  | 'provider_dispatch'
  | 'provider_turn_result'
  | 'candidate_output'
  | 'recovery_dossier'
  | 'recovery_decision'
  | 'final_result'

export type RuntimeDistillationCardType = 'work' | 'voice' | 'validator'

export interface DistillationInvocation {
  schema: 'zhimeng.distillation-invocation.v1'
  invocation_id: string
  source: Record<string, unknown>
  project: Record<string, unknown>
  knowledge: Record<string, unknown>
  instruction: Record<string, unknown>
  provider: Record<string, unknown>
  anchor_catalog_id: string
  anchor_catalog_hash: string
  retry_of: string | null
  request_hash: string
  idempotency_key: string
  status: DistillationInvocationStatus
  state_revision: number
  provider_replay_count: 0
  last_receipt_ordinal: number
  last_receipt_hash: string
  last_dispatch_receipt_id: string | null
  last_dispatch_receipt_hash: string | null
  last_dispatch_turn_ordinal: number | null
  last_turn_result_receipt_id: string | null
  last_turn_result_receipt_hash: string | null
  provider_turn_count: number
  provider_turn_chain_hash: string | null
  candidate_output_receipt_id: string | null
  candidate_output_receipt_hash: string | null
  recovery_dossier_receipt_id: string | null
  recovery_dossier_revision: number | null
  recovery_dossier_receipt_hash: string | null
  recovery_decision_receipt_id: string | null
  recovery_decision_receipt_hash: string | null
  final_result_receipt_id: string | null
  final_result_receipt_hash: string | null
  candidate_set_hash: string | null
  work_count: number | null
  voice_count: number | null
  validator_count: number | null
  created_at: string
  updated_at: string
  head_hash: string
}

export interface DistillationAnchorBinding {
  anchor_id: string
  anchor_hash: string
  [key: string]: unknown
}

export interface DistillationCandidateCard {
  candidate_id: string
  card_id: string
  output_index: number
  card_type: RuntimeDistillationCardType
  card: Record<string, unknown>
  card_hash: string
  anchors: DistillationAnchorBinding[]
  status: 'pending'
  requires_author_review: true
}

export interface DistillationCandidateSet {
  invocation_id: string
  cards: DistillationCandidateCard[]
  candidate_set_hash: string
}

export interface DistillationCandidateOutputPayload {
  schema: 'zhimeng.distillation-candidate-output.v1'
  provider_turn_count: number
  provider_turn_chain_hash: string
  source_receipt_id: string
  source_receipt_hash: string
  candidate_set: DistillationCandidateSet
  formal_draft_allowed: false
  requires_author_review: true
  [key: string]: unknown
}

export type DistillationReceiptPayload =
  | DistillationCandidateOutputPayload
  | (Record<string, unknown> & { schema?: string })

export interface DistillationInvocationReceipt {
  schema: 'zhimeng.distillation-invocation-receipt.v1'
  receipt_id: string
  invocation_id: string
  ordinal: number
  kind: DistillationReceiptKind
  turn_ordinal: number | null
  parent_receipt_id: string | null
  expected_invocation_revision: number
  resulting_invocation_revision: number
  resulting_status: DistillationInvocationStatus
  idempotency_key: string
  payload: DistillationReceiptPayload
  payload_hash: string
  created_at: string
  receipt_hash: string
}

export interface DistillationInvocationSnapshot {
  schema: 'zhimeng.distillation-invocation-snapshot.v1'
  invocation: DistillationInvocation
  last_receipt_id: string
  receipts: DistillationInvocationReceipt[]
}

export interface DistillationInvocationReceiptList {
  schema: 'zhimeng.distillation-invocation-receipt-list.v1'
  invocation_id: string
  receipts: DistillationInvocationReceipt[]
}

export interface DistillationInvocationRunRequest {
  anchor_catalog: Record<string, unknown>
  text_by_anchor_id: Record<string, string>
}

export interface DistillationInvocationRunResult extends DistillationInvocationSnapshot {
  terminal_reason: string
}

export function isRuntimeDistillationInvocationId(
  value: string | null | undefined,
): value is string {
  return /^distillation-invocation-[0-9a-f]{32}$/.test(value ?? '')
}

export function getDistillationInvocation(
  invocationId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<DistillationInvocationSnapshot> {
  return fetchRuntimeJson(
    `/api/v1/distillation/invocations/${encodeURIComponent(invocationId)}`,
    options,
    { signal },
  )
}

export function listDistillationInvocationReceipts(
  invocationId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<DistillationInvocationReceiptList> {
  return fetchRuntimeJson(
    `/api/v1/distillation/invocations/${encodeURIComponent(invocationId)}/receipts`,
    options,
    { signal },
  )
}

export function runDistillationInvocation(
  invocationId: string,
  request: DistillationInvocationRunRequest,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<DistillationInvocationRunResult> {
  return fetchRuntimeJson(
    `/api/v1/distillation/invocations/${encodeURIComponent(invocationId)}/run`,
    options,
    jsonRequest(request, 'POST', signal),
  )
}
