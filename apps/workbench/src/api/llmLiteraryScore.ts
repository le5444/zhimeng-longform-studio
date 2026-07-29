import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface LlmLiteraryScoreEvaluateRequest {
  expected_result_fingerprint?: string
  expected_review_revision?: number
  prefer_provider?: boolean
}

export interface LlmLiteraryScoreAxis {
  code:
    | 'immersion'
    | 'character_voice'
    | 'emotional_arc'
    | 'prose_craft'
    | 'suspense_pull'
  score: number
  weight: number
  weighted_score: number
  rationale: string
}

export interface LlmLiteraryScoreReceipt {
  schema: 'zhimeng.llm-literary-score-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back' | 'ready_candidate'
  result_revision: number | null
  result_fingerprint: string | null
  review_revision: number
  scorer_mode: 'offline_stub' | 'provider_unavailable' | 'provider_live'
  advisory_only: true
  llm_score_recorded: boolean
  quality_assessed: false
  subjective_quality_unlocked: false
  formal_draft_allowed: false
  authorization_unlock: false
  provider_model: string | null
  literary_quality_receipt_id: string | null
  axes: LlmLiteraryScoreAxis[]
  total_score: number
  detail: string
  policy_version: 'local-v1-llm-literary-score-1'
  evaluated_at: string
  receipt_hash: string
}

export interface LlmLiteraryScoreResponse {
  schema: 'zhimeng.llm-literary-score-response.v1'
  change_set_id: string
  receipt: LlmLiteraryScoreReceipt | null
  history: LlmLiteraryScoreReceipt[]
  not_claiming: string[]
}

export function getLlmLiteraryScore(
  changeSetId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<LlmLiteraryScoreResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/llm-literary-scores`,
    options,
  )
}

export function evaluateLlmLiteraryScore(
  changeSetId: string,
  request: LlmLiteraryScoreEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<LlmLiteraryScoreResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/llm-literary-scores/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}
