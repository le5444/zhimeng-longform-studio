import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface SourceTarget { sessionId: string; messageId: string }
export interface SourceExcerpt extends SourceTarget { label: string; content: string }

export interface SessionSourcePage {
  message: {
    session_id: string; session_title: string; project_id: string | null; project_name: string | null
    message_id: string; ordinal: number; role: 'user' | 'assistant'; created_at: string
    run_id: string; run_status: string; origin_kind: string; content: string
    start_offset: number; end_offset: number; total_characters: number; next_start_offset: number | null
    masked_secrets: number; offset_basis: 'masked_text'
  }
  content_hash: string
  previous_message_id: string | null
  next_message_id: string | null
}

export async function readSessionSource(target: SourceTarget, offset = 0, hash?: string, options: RuntimeClientOptions = {}): Promise<SessionSourcePage> {
  const query = new URLSearchParams({ start_offset: String(offset) })
  if (hash) query.set('expected_hash', hash)
  const page = await fetchRuntimeJson<SessionSourcePage>(`/api/v1/agent/sessions/${encodeURIComponent(target.sessionId)}/messages/${encodeURIComponent(target.messageId)}/source?${query}`, options)
  if (page.message.session_id !== target.sessionId || page.message.message_id !== target.messageId
    || page.message.start_offset !== offset || (hash && page.content_hash !== hash)) throw new Error('原消息已变化，请重新读取。')
  return page
}
