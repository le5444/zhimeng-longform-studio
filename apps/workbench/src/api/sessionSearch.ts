import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import type { SessionSourcePage } from './sessionSource'

export interface SessionSearchPage {
  query: string
  scope: 'all' | 'project'
  project_id: string | null
  matches: SessionSourcePage['message'][]
  has_more: boolean
  next_before_message_id: string | null
}

export function searchSessionMessages(query: string, scope: 'all' | 'project', projectId: string | null,
  before?: string, options: RuntimeClientOptions = {}): Promise<SessionSearchPage> {
  const params = new URLSearchParams({ query, scope, limit: '20' })
  if (scope === 'project' && projectId) params.set('project_id', projectId)
  if (before) params.set('before_message_id', before)
  return fetchRuntimeJson(`/api/v1/agent/history/search?${params}`, options)
}
