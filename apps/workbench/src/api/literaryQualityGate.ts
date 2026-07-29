import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface LiteraryQualityEvaluateRequest {
  expected_result_fingerprint?: string
  expected_review_revision?: number
}

export interface LiteraryQualityDimension {
  code: string
  status: 'pass' | 'watch' | 'fail' | 'not_scored'
  detail: string
}

export interface LiteraryQualityFinding {
  finding_id: string
  kind: 'structural' | 'hard_fact_signal' | 'subjective_note' | 'rubric_note'
  severity: 'info' | 'watch' | 'concern'
  summary: string
  evidence_quote: string
  relative_path: string | null
}

export interface LiteraryRubricScore {
  code:
    | 'dialogue_balance'
    | 'sensory_density'
    | 'pacing_variation'
    | 'hook_opening'
    | 'repetition_risk'
  score: number
  weight: number
  weighted_score: number
  detail: string
}

export interface LiteraryQualityGateReceipt {
  schema: 'zhimeng.literary-quality-gate-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back' | 'ready_candidate'
  result_revision: number | null
  result_fingerprint: string | null
  review_revision: number
  gate_mode: 'deterministic_structural_v1' | 'deterministic_structural_rubric_v2'
  decision: 'advisory_only'
  authorization_unlock: false
  formal_draft_allowed: false
  quality_assessed: false
  structural_checks_run: true
  deterministic_rubric_scored: true
  dimensions: LiteraryQualityDimension[]
  findings: LiteraryQualityFinding[]
  finding_counts: Record<string, number>
  rubric_scores: LiteraryRubricScore[]
  rubric_total_score: number
  policy_version: 'local-v1-advisory-1' | 'local-v1-advisory-2'
  evaluated_at: string
  receipt_hash: string
}

export interface LiteraryQualityGateResponse {
  schema: 'zhimeng.literary-quality-gate-response.v1'
  change_set_id: string
  receipt: LiteraryQualityGateReceipt | null
  history: LiteraryQualityGateReceipt[]
  not_claiming: string[]
}

export function getLiteraryQualityGate(
  changeSetId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<LiteraryQualityGateResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/literary-quality-gate`,
    options,
  )
}

export function evaluateLiteraryQualityGate(
  changeSetId: string,
  request: LiteraryQualityEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<LiteraryQualityGateResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/literary-quality-gate/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}
