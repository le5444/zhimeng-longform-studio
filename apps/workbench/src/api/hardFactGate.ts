import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface HardFactEvaluateRequest {
  expected_result_fingerprint?: string
  expected_review_revision?: number
}

export interface HardFactSignal {
  code: string
  severity: 'info' | 'watch' | 'block'
  summary: string
  evidence_quote: string
  relative_path: string | null
  source: string
}

export interface HardFactGateReceipt {
  schema: 'zhimeng.hard-fact-gate-receipt.v1'
  receipt_id: string
  change_set_id: string
  project_id: string
  outcome: 'applied' | 'rolled_back' | 'ready_candidate'
  result_revision: number | null
  result_fingerprint: string | null
  review_revision: number
  gate_mode: 'deterministic_hard_fact_v1'
  status: 'passed' | 'failed' | 'missing_inputs'
  authorization_input_ready: boolean
  authorization_unlock: false
  formal_draft_allowed: false
  quality_assessed: false
  post_draft_gate_status: 'missing' | 'passed' | 'blocked' | 'stale'
  literary_quality_receipt_id: string | null
  signals: HardFactSignal[]
  signal_counts: Record<string, number>
  policy_version: 'local-v1-hard-fact-1'
  evaluated_at: string
  receipt_hash: string
}

export interface HardFactGateResponse {
  schema: 'zhimeng.hard-fact-gate-response.v1'
  change_set_id: string
  receipt: HardFactGateReceipt | null
  history: HardFactGateReceipt[]
  not_claiming: string[]
}

export function getHardFactGate(
  changeSetId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<HardFactGateResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/hard-fact-gate`,
    options,
  )
}

export function evaluateHardFactGate(
  changeSetId: string,
  request: HardFactEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<HardFactGateResponse>(
    `/api/v1/change-sets/${encodeURIComponent(changeSetId)}/hard-fact-gate/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}
