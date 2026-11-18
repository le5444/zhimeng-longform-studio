import type { AgentRun } from './agent'
import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface CommandCompletionEvent {
  sequence: number
  event_id: string
  command_session_id: string
  owner_session_id: string
  owner_run_id: string
  status: 'exited' | 'cancelled' | 'interrupted'
  exit_code: number | null
  ended_at: number | string | null
  delivery_state: 'pending' | 'delivered' | 'suppressed'
  delivered_run_id: string | null
  payload: { command?: string; cwd?: string; duration_ms?: number; [key: string]: unknown }
}

export interface BackgroundEventsView {
  events: CommandCompletionEvent[]
  next_cursor: number
  active_run: AgentRun | null
  latest_run: AgentRun | null
  has_running_commands: boolean
  pending_count: number
}

export function readBackgroundEvents(sessionId: string, options: RuntimeClientOptions = {}) {
  // Delivery state may change after the completion's stable sequence was assigned.
  return fetchRuntimeJson<BackgroundEventsView>(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/background-events?after=0`, options)
}
