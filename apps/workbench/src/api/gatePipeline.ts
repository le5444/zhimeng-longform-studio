import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface GatePipelineEvaluateRequest {
  expected_result_fingerprint?: string
  expected_review_revision?: number
}

export interface GatePipelineStep {
  code: 'post_draft_gate' | 'literary_quality_gate' | 'llm_literary_score' | 'external_blind_review_matrix' | 'hard_fact_gate' | string
  ordinal: number
  required: boolean
  present: boolean
  status: string
  receipt_id: string | null
  detail: string
}

export interface GatePipelineReceipt {
  schema: 'zhimeng.gate-pipeline-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back' | 'ready_candidate'
  result_revision: number | null
  result_fingerprint: string | null
  review_revision: number
  status: 'complete' | 'blocked' | 'incomplete'
  steps: GatePipelineStep[]
  hard_fact_receipt_id: string | null
  literary_quality_receipt_id: string | null
  llm_literary_score_receipt_id?: string | null
  external_blind_review_matrix_ready?: boolean
  post_draft_gate_status: 'missing' | 'passed' | 'blocked' | 'stale'
  authorization_input_ready: boolean
  authorization_unlock: false
  formal_draft_allowed: false
  quality_assessed: false
  policy_version: 'local-v1-gate-pipeline-1' | 'local-v1-gate-pipeline-2'
  evaluated_at: string
  receipt_hash: string
}

export interface GatePipelineResponse {
  schema: 'zhimeng.gate-pipeline-response.v1'
  change_set_id: string
  receipt: GatePipelineReceipt | null
  history: GatePipelineReceipt[]
  not_claiming: string[]
}

export function getGatePipeline(
  changeSetId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<GatePipelineResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/gate-pipeline`,
    options,
  )
}

export function evaluateGatePipeline(
  changeSetId: string,
  request: GatePipelineEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<GatePipelineResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/gate-pipeline/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}
