import type { SessionCheckpointRecord } from './agent'
import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface SessionContextState {
  schema: 'zhimeng.session-context-state.v1'
  session_id: string
  message_count: number
  recent_message_count: number
  compacted_message_count: number
  compacting: boolean
  background_pending: boolean
  latest_checkpoint: SessionCheckpointRecord | null
  run_compaction?: {
    run_id: string
    state: 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
    folded_results: number
    tokens_before: number | null
    tokens_after: number | null
  } | null
}

export function readSessionContextState(sessionId: string, options: RuntimeClientOptions = {}): Promise<SessionContextState> {
  return fetchRuntimeJson(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/context-state`, options)
}
