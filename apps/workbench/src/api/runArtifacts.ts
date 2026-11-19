import type { AgentRunStatus } from './agent'
import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface RunArtifact {
  receipt_id: string
  source: 'file_change' | 'change_set'
  record_state: string
  operation: string
  path: string
  expected_state: 'file' | 'directory' | 'missing' | 'unknown'
  expected_sha256: string | null
  current: { state: 'file' | 'directory' | 'missing' | 'unverified'; bytes: number | null; sha256: string | null; reason: string | null }
  matches_recorded_state: boolean | null
}

export interface RunArtifactsPage {
  session_id: string
  run_id: string
  run_status: AgentRunStatus
  revision: string
  observed_at: string
  total_paths: number
  observations: RunArtifact[]
  next_offset: number | null
  scope: 'run_receipt_paths_only'
}

export function readRunArtifacts(sessionId: string, runId: string, offset = 0, revision?: string,
  options: RuntimeClientOptions = {}): Promise<RunArtifactsPage> {
  const params = new URLSearchParams({ offset: String(offset), limit: '20' })
  if (revision) params.set('revision', revision)
  return fetchRuntimeJson(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/artifacts?${params}`, options)
}
