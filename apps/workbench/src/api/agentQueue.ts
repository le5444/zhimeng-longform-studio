import type { AgentMessage, AgentRun, WriterMode } from './agent'
import { fetchRuntimeJson } from './runtime'

export interface QueueInput {
  item_id: string
  message: string
  project_id: string | null
  writer_mode: WriterMode
  manual_file_ids: string[]
  manual_skill_names: string[]
  open_file_path: string | null
  image_ids?: string[]
}
export interface QueueItem extends QueueInput {
  sending: boolean
  delivery?: 'queued' | 'steer_pending'
  steering_run_id?: string | null
}
export interface QueueState {
  session_id: string
  revision: number
  pause_reason: 'user' | 'failed' | 'cancelled' | 'interrupted' | 'send_failed' | null
  active_run_id: string | null
  items: QueueItem[]
}
export interface QueueUpdate {
  action: 'pause' | 'resume' | 'edit' | 'remove' | 'move'
  item_id?: string
  message?: string
  direction?: -1 | 1
}
export interface QueueDispatch {
  queue: QueueState
  accepted: { run: AgentRun; user_message: AgentMessage; replayed: boolean }
}
export interface QueueSteer { queue: QueueState }
const path = (sessionId: string) => `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/queue`
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

export function readMessageQueue(sessionId: string): Promise<QueueState> {
  return fetchRuntimeJson(path(sessionId))
}
export function enqueueMessage(sessionId: string, input: QueueInput): Promise<QueueState> {
  return fetchRuntimeJson(path(sessionId), {}, json('POST', input))
}
export function updateMessageQueue(sessionId: string, revision: number, update: QueueUpdate): Promise<QueueState> {
  return fetchRuntimeJson(path(sessionId), {}, json('PATCH', { ...update, revision }))
}
export function dispatchQueuedMessage(sessionId: string, itemId: string): Promise<QueueDispatch> {
  return fetchRuntimeJson(`${path(sessionId)}/${encodeURIComponent(itemId)}/dispatch`, { timeoutMs: 60_000 }, { method: 'POST' })
}
export function steerQueuedMessage(sessionId: string, itemId: string, expectedRunId: string, input?: QueueInput): Promise<QueueSteer> {
  return fetchRuntimeJson(`${path(sessionId)}/${encodeURIComponent(itemId)}/steer`, {}, json('POST', { expected_run_id: expectedRunId, ...(input ? { input } : {}) }))
}
