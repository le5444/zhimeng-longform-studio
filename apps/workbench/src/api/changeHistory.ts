import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import { rollbackChangeSet } from './changeSets'
import { undoSessionFileChange } from './fileChanges'

export type HistoryScope = 'session' | 'project'
export interface HistoryFile { path: string; target_path: string | null; operation: string }
export interface HistoryChange {
  id: string; source: 'change_set' | 'file_change'; session_id: string | null; project_id: string | null
  run_id: string | null; title: string; status: string; created_at: string; files: HistoryFile[]
}
export interface ChangeHistoryPage {
  session_id: string; scope: HistoryScope; project_id: string | null; revision: string
  total: number; changes: HistoryChange[]; next_offset: number | null
}
export interface HistoryPreview extends HistoryFile {
  current_state: 'file' | 'directory' | 'missing' | 'unverified'; open_path: string | null
  hunks: { before: string; after: string }[]; note: string | null
  before_characters?: number | null; after_characters?: number | null
}
export interface ChangeHistoryDetail extends HistoryChange {
  previews: HistoryPreview[]; undo_available: boolean; undo_reason: string | null; review_revision: number | null
}
const route = (session: string) => `/api/v1/agent/sessions/${encodeURIComponent(session)}/changes`
export async function readChangeHistory(session: string, scope: HistoryScope, offset = 0, revision?: string, options: RuntimeClientOptions = {}) {
  const params = new URLSearchParams({ scope, offset: String(offset) })
  if (revision) params.set('revision', revision)
  const page = await fetchRuntimeJson<ChangeHistoryPage>(`${route(session)}?${params}`, options)
  if (page.session_id !== session || page.scope !== scope) throw new Error('修改记录与当前范围不一致，请重新读取。')
  return page
}
export async function readHistoryDetail(session: string, id: string, scope: HistoryScope, options: RuntimeClientOptions = {}) {
  const detail = await fetchRuntimeJson<ChangeHistoryDetail>(`${route(session)}/${encodeURIComponent(id)}?scope=${scope}`, options)
  if (detail.id !== id || scope === 'session' && detail.session_id !== session) throw new Error('修改详情与当前记录不一致，请重新读取。')
  return detail
}
export async function undoHistoryChange(change: ChangeHistoryDetail): Promise<void> {
  if (change.source === 'change_set' && change.review_revision !== null) {
    const result = await rollbackChangeSet(change.id, change.review_revision)
    if (result.change_set_id !== change.id || result.project_id !== change.project_id) throw new Error('撤回结果与这条记录不一致，请刷新核对。')
    return
  }
  if (change.source === 'file_change' && change.session_id) {
    const result = await undoSessionFileChange(change.session_id, change.id)
    if (result.change_id !== change.id || result.session_id !== change.session_id) throw new Error('撤回结果与这条记录不一致，请刷新核对。')
    return
  }
  throw new Error('修改记录缺少版本信息，请刷新。')
}
