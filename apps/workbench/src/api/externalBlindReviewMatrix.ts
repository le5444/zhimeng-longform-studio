import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface ExternalBlindReviewMatrixEvaluateRequest {
  suite_id?: string
  evidence_path?: string
  evidence?: Record<string, unknown>
  write_receipt_to?: string
}

export interface ExternalBlindReviewMatrixCell {
  reviewer_id: string
  sample_id: string
  required: boolean
  status: 'passed' | 'failed' | 'missing' | 'skipped'
  present: boolean
  detail: string
  score: number | null
  evidence_ref: string | null
}

export interface ExternalBlindReviewMatrixReceipt {
  schema: 'zhimeng.external-blind-review-matrix-receipt.v1'
  receipt_id: string
  policy_version: string
  suite_id: string | null
  evidence_id: string | null
  matrix_ready: boolean
  required_cell_count: number
  passed_cell_count: number
  missing_required: string[]
  cells: ExternalBlindReviewMatrixCell[]
  quality_assessed: false
  formal_draft_allowed: false
  llm_literary_score_claim_allowed: false
  local_v1_exe_claim_allowed: false
  detail: string
  evaluated_at: string
  receipt_hash: string
}

export interface ExternalBlindReviewMatrixResponse {
  schema: 'zhimeng.external-blind-review-matrix-response.v1'
  receipt: ExternalBlindReviewMatrixReceipt
  not_claiming: string[]
}

export function evaluateExternalBlindReviewMatrix(
  request: ExternalBlindReviewMatrixEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ExternalBlindReviewMatrixResponse>(
    '/api/v1/external-blind-review-matrix/evaluate',
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}
