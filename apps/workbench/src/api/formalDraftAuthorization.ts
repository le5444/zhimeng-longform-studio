import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface FormalDraftEvaluateRequest {
  expected_project_revision?: number
  expected_project_fingerprint?: string
  context_pack_id?: string | null
  run_id?: string | null
  change_set_id?: string | null
}

export interface FormalDraftPrerequisite {
  code: string
  satisfied: boolean
  detail: string
}

export interface FormalDraftDenial {
  code: string
  message: string
}

export interface FormalDraftAuthorizationReceipt {
  schema: 'zhimeng.formal-draft-authorization-receipt.v1'
  receipt_id: string
  project_id: string
  project_revision: number | null
  project_fingerprint: string | null
  context_pack_id: string | null
  run_id: string | null
  change_set_id: string | null
  decision: 'denied'
  formal_draft_allowed: false
  writes_project_truth: false
  prerequisites: FormalDraftPrerequisite[]
  denials: FormalDraftDenial[]
  policy_version: 'local-v1-locked-1'
  evaluated_at: string
  receipt_hash: string
}

export interface FormalDraftAuthorizationResponse {
  schema: 'zhimeng.formal-draft-authorization-response.v1'
  receipt: FormalDraftAuthorizationReceipt
  not_claiming: string[]
}

export function evaluateFormalDraftAuthorization(
  projectId: string,
  request: FormalDraftEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FormalDraftAuthorizationResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/formal-draft-authorization/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}


export type FormalDraftAuthorIntent =
  | 'request_authorization'
  | 'withhold_authorization'
  | 'revoke_authorization'

export interface FormalDraftAuthorDecisionRequest {
  intent: FormalDraftAuthorIntent
  expected_project_revision?: number
  expected_project_fingerprint?: string
  change_set_id?: string | null
  note?: string | null
}

export interface FormalDraftAuthorDecisionReceipt {
  schema: 'zhimeng.formal-draft-author-decision-receipt.v1'
  receipt_id: string
  project_id: string
  project_revision: number | null
  project_fingerprint: string | null
  change_set_id: string | null
  intent: FormalDraftAuthorIntent
  note: string | null
  authorization_effective: false
  formal_draft_allowed: false
  writes_project_truth: false
  policy_version: 'local-v1-locked-1'
  decided_at: string
  receipt_hash: string
}

export interface FormalDraftAuthorDecisionResponse {
  schema: 'zhimeng.formal-draft-author-decision-response.v1'
  receipt: FormalDraftAuthorDecisionReceipt | null
  history: FormalDraftAuthorDecisionReceipt[]
  not_claiming: string[]
}

export function getFormalDraftAuthorDecision(
  projectId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FormalDraftAuthorDecisionResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/formal-draft-authorization/author-decision`,
    options,
  )
}

export function recordFormalDraftAuthorDecision(
  projectId: string,
  request: FormalDraftAuthorDecisionRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FormalDraftAuthorDecisionResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/formal-draft-authorization/author-decision`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}


export interface FormalDraftProvenanceEvaluateRequest {
  expected_project_revision?: number
  expected_project_fingerprint?: string
  change_set_id?: string | null
}

export interface FormalDraftProvenanceCheck {
  code: string
  required: boolean
  present: boolean
  detail: string
}

export interface FormalDraftProvenanceReceipt {
  schema: 'zhimeng.formal-draft-provenance-receipt.v1'
  receipt_id: string
  project_id: string
  project_revision: number | null
  project_fingerprint: string | null
  change_set_id: string | null
  status: 'complete' | 'incomplete' | 'scope_limited'
  authorization_input_ready: boolean
  authorization_unlock: false
  formal_draft_allowed: false
  writes_project_truth: false
  checks: FormalDraftProvenanceCheck[]
  missing_required: string[]
  policy_version: 'local-v1-provenance-1'
  evaluated_at: string
  receipt_hash: string
}

export interface FormalDraftProvenanceResponse {
  schema: 'zhimeng.formal-draft-provenance-response.v1'
  receipt: FormalDraftProvenanceReceipt
  not_claiming: string[]
}

export function evaluateFormalDraftProvenance(
  projectId: string,
  request: FormalDraftProvenanceEvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FormalDraftProvenanceResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/formal-draft-authorization/provenance/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}


export interface FormalDraftContinuousO2EvaluateRequest {
  expected_project_revision?: number
  expected_project_fingerprint?: string
  adjacent_chain_id?: string | null
  managed_skill_chain_id?: string | null
}

export interface FormalDraftContinuousO2Check {
  code: string
  required: boolean
  present: boolean
  detail: string
  evidence_id?: string | null
}

export interface FormalDraftContinuousO2Receipt {
  schema: 'zhimeng.formal-draft-continuous-o2-receipt.v1'
  receipt_id: string
  project_id: string
  project_revision: number | null
  project_fingerprint: string | null
  status: 'complete' | 'incomplete'
  authorization_input_ready: boolean
  authorization_unlock: false
  formal_draft_allowed: false
  writes_project_truth: false
  four_skills_generic_exactly_once: false
  checks: FormalDraftContinuousO2Check[]
  missing_required: string[]
  adjacent_chain_id: string | null
  managed_skill_chain_id: string | null
  creation_suite_chain_id: string | null
  policy_version: 'local-v1-continuous-o2-1'
  evaluated_at: string
  receipt_hash: string
}

export interface FormalDraftContinuousO2Response {
  schema: 'zhimeng.formal-draft-continuous-o2-response.v1'
  receipt: FormalDraftContinuousO2Receipt
  not_claiming: string[]
}

export function evaluateFormalDraftContinuousO2(
  projectId: string,
  request: FormalDraftContinuousO2EvaluateRequest = {},
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<FormalDraftContinuousO2Response>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/formal-draft-authorization/continuous-o2/evaluate`,
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}

