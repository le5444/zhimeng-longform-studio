import { useCallback, useEffect, useRef, useState } from 'react'
import { searchSessionMessages, type SessionSearchPage } from '../api/sessionSearch'
import { RuntimeApiError } from '../api/runtime'
import { authorFacingError } from '../lib/authorFacingError'
import { ConversationSources, SourceLink } from './chat/ConversationSources'
import { Btn, Spinner } from './ui'
import './conversationSearch.css'

/** Remount on query/scope change so old pages and their open source never cross searches. */
export function ConversationSearch(props: { query: string; scope: 'all' | 'project'; projectId: string | null }) {
  return <SearchResults key={JSON.stringify([props.query, props.scope, props.projectId])} {...props} />
}

function SearchResults({ query, scope, projectId }: { query: string; scope: 'all' | 'project'; projectId: string | null }) {
  const [page, setPage] = useState<SessionSearchPage | null>(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [restart, setRestart] = useState(false)
  const request = useRef<AbortController | null>(null)

  const load = useCallback(async (previous: SessionSearchPage | null) => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setBusy(true); setError(''); setRestart(false)
    try {
      const next = await searchSessionMessages(query, scope, projectId, previous?.next_before_message_id ?? undefined, { signal: controller.signal })
      if (controller.signal.aborted) return
      const seen = new Set(previous?.matches.map(hit => hit.message_id))
      setPage({ ...next, matches: [...(previous?.matches ?? []), ...next.matches.filter(hit => !seen.has(hit.message_id))] })
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(authorFacingError(reason, '正文搜索失败，请重试。'))
        setRestart(reason instanceof RuntimeApiError && reason.status === 400)
      }
    } finally { if (!controller.signal.aborted) setBusy(false) }
  }, [query, scope, projectId])

  useEffect(() => {
    const timer = setTimeout(() => void load(null), 250)
    return () => { clearTimeout(timer); request.current?.abort() }
  }, [load])

  return <ConversationSources scope={query}>
    <section className="zm-conversation-search" aria-label="正文搜索结果">
      <h3>正文{page ? <span>已找到 {page.matches.length} 条{page.has_more ? '，还有更多' : ''}</span> : null}
        {page ? <Btn size="sm" variant="ghost" disabled={busy} onClick={() => void load(null)}>刷新搜索</Btn> : null}
      </h3>
      {page?.matches.map(hit => <article key={hit.message_id}>
        <SourceLink target={{ sessionId: hit.session_id, messageId: hit.message_id }}>
          <span className="zm-search-hit">
            <strong>{hit.session_title || '新对话'}</strong>
            <small>{hit.project_name ?? '自由对话'} · {hit.role === 'user' ? '你' : 'Agent'} · <time dateTime={hit.created_at}>{new Date(hit.created_at).toLocaleDateString()}</time></small>
            <span className="zm-search-excerpt">{hit.content || '这条消息没有可显示的文字。'}</span>
            <span className="zm-search-source">查看原消息{hit.start_offset > 0 || hit.next_start_offset !== null ? ' · 部分原文' : ''}</span>
          </span>
        </SourceLink>
      </article>)}
      {busy ? <p role="status"><Spinner /> 正在搜索正文…</p> : null}
      {error ? <div role="alert"><p>{error}</p><Btn size="sm" onClick={() => void load(restart ? null : page)}>{restart ? '重新搜索' : '重试正文搜索'}</Btn></div> : null}
      {!busy && !error && page?.matches.length === 0 ? <p>没有包含这些文字的消息。</p> : null}
      {!busy && !error && page?.has_more ? <Btn size="sm" onClick={() => void load(page)}>加载更多消息</Btn> : null}
    </section>
  </ConversationSources>
}
