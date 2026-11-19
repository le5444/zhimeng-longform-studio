import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface MemoryChangeRecord {
  action: 'add' | 'replace' | 'remove'
  text: string
  old_text: string | null
  source_message_id: string | null
  evidence: string | null
  origin: string
  created_at: string
}

export interface MemoryChangesView {
  session_id: string
  changes: Record<string, MemoryChangeRecord[]>
  pending: boolean
}

export async function readMemoryChanges(sessionId: string, runIds: string[], options: RuntimeClientOptions = {}): Promise<MemoryChangesView> {
  const changes: MemoryChangesView['changes'] = {}
  let pending = false
  for (let start = 0; start < runIds.length; start += 500) {
    const ids = runIds.slice(start, start + 500)
    const view = await fetchRuntimeJson<MemoryChangesView>(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/memory-changes`, options, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_ids: ids }),
    })
    if (view.session_id !== sessionId || Object.keys(view.changes).some((id) => !ids.includes(id))) throw new Error('记忆变更不属于当前对话。')
    Object.assign(changes, view.changes)
    pending ||= view.pending
  }
  return { session_id: sessionId, changes, pending }
}
