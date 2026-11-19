import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface SessionFileChangeItem {
  sequence: number
  operation: 'create' | 'update' | 'delete' | 'move' | 'mkdir'
  path: string
  target_path: string | null
  undone_at: string | null
}

export interface SessionFileChange {
  change_id: string
  session_id: string
  run_id: string
  title: string
  created_at: string
  undone_at: string | null
  status: 'applied' | 'failed' | 'unverified' | 'partially_undone' | 'undone'
  files: SessionFileChangeItem[]
}

export interface SessionFileChangePreview extends SessionFileChangeItem {
  before_text: string | null
  after_text: string | null
  before_characters?: number | null
  after_characters?: number | null
  preview_note: string | null
  masked_secrets: number
}

export interface SessionFileChangeDetail extends SessionFileChange {
  previews: SessionFileChangePreview[]
  undo_available: boolean
  undo_reason: string | null
}

function route(sessionId: string, changeId?: string) {
  return `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/file-changes${changeId ? `/${encodeURIComponent(changeId)}` : ''}`
}

export function undoSessionFileChange(sessionId: string, changeId: string, options: RuntimeClientOptions = {}): Promise<SessionFileChangeDetail> {
  return fetchRuntimeJson(`${route(sessionId, changeId)}/undo`, { timeoutMs: 120_000, ...options }, { method: 'POST' })
}
