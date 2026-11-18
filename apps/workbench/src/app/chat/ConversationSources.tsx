import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, ArrowRight, Copy, ExternalLink, RefreshCw, X } from 'lucide-react'
import { readSessionSource, type SessionSourcePage, type SourceTarget } from '../../api/sessionSource'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { Btn, IconBtn, Spinner } from '../ui'
import './conversationSources.css'

const OpenSource = createContext<((target: SourceTarget) => void) | null>(null)

export function ConversationSources({ scope, children }: { scope: string | null; children: ReactNode }) {
  const [opened, setOpened] = useState<{ scope: string | null; target: SourceTarget } | null>(null)
  useEffect(() => { setOpened(null) }, [scope])
  const open = useCallback((target: SourceTarget) => setOpened({ scope, target }), [scope])
  return <OpenSource.Provider value={open}>
    {children}
    {opened && opened.scope === scope ? <SourceDialog target={opened.target} onClose={() => setOpened(null)} /> : null}
  </OpenSource.Provider>
}

export function SourceLink({ target, children = '查看原消息' }: { target: SourceTarget; children?: ReactNode }) {
  const open = useContext(OpenSource)
  return open ? <button type="button" className="zm-source-link" onClick={() => open(target)}><ExternalLink aria-hidden />{children}</button> : null
}

function SourceDialog({ target, onClose }: { target: SourceTarget; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [selected, setSelected] = useState(target)
  const [page, setPage] = useState<SessionSourcePage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const [revision, setRevision] = useState(0)
  const request = useRef<AbortController | null>(null)
  const body = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const element = dialog.current
    element?.showModal()
    return () => { request.current?.abort(); element?.close(); previous?.focus({ preventScroll: true }) }
  }, [])
  useEffect(() => { setSelected(target) }, [target])
  useEffect(() => {
    const controller = new AbortController()
    request.current?.abort()
    request.current = controller
    setPage(null); setError(''); setBusy(true); setCopied(false)
    body.current?.scrollTo?.({ top: 0 })
    void readSessionSource(selected, 0, undefined, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setPage(value)
    }).catch(reason => {
      if (!controller.signal.aborted) setError(authorFacingError(reason, '原消息读取失败'))
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [selected, revision])

  const more = async () => {
    if (!page || page.message.next_start_offset === null || busy) return
    const previous = page
    const controller = new AbortController()
    request.current?.abort(); request.current = controller
    setBusy(true); setError(''); setCopied(false)
    try {
      const next = await readSessionSource(selected, previous.message.next_start_offset!, previous.content_hash, { signal: controller.signal })
      if (!controller.signal.aborted) setPage({ ...next, message: { ...next.message, start_offset: 0, content: previous.message.content + next.message.content } })
    } catch (reason) {
      if (!controller.signal.aborted) {
        if (reason instanceof RuntimeApiError && [404, 409].includes(reason.status)) setPage(null)
        setError(authorFacingError(reason, '后续原文读取失败'))
      }
    } finally { if (!controller.signal.aborted) setBusy(false) }
  }
  const copy = async () => {
    const current = request.current
    try { await navigator.clipboard.writeText(page?.message.content ?? ''); if (request.current === current && !current?.signal.aborted) setCopied(true) }
    catch { if (request.current === current && !current?.signal.aborted) setError('复制失败，请重试。') }
  }
  const message = page?.message
  return <dialog ref={dialog} className="zm-source-dialog" aria-labelledby={titleId} onCancel={event => { event.preventDefault(); onClose() }}>
    <header><div><h2 id={titleId}>对话原文</h2>{message ? <p>{message.session_title || '未命名对话'} · {message.project_name ?? '自由对话'}</p> : null}</div><IconBtn label="关闭原文" icon={<X />} onClick={onClose} /></header>
    <div className="zm-source-toolbar">
      <IconBtn label="上一条消息" icon={<ArrowLeft />} disabled={busy || !page?.previous_message_id} onClick={() => page?.previous_message_id && setSelected({ ...selected, messageId: page.previous_message_id })} />
      <IconBtn label="下一条消息" icon={<ArrowRight />} disabled={busy || !page?.next_message_id} onClick={() => page?.next_message_id && setSelected({ ...selected, messageId: page.next_message_id })} />
      <span>{message ? `${message.role === 'user' ? '用户' : 'Agent'} · 第 ${message.ordinal} 条` : ''}</span>
      <IconBtn label="重新读取原文" icon={<RefreshCw />} disabled={busy} onClick={() => setRevision(value => value + 1)} />
      <IconBtn label={copied ? '已复制' : message?.next_start_offset != null ? '复制已加载原文' : '复制原文'} icon={<Copy />} disabled={!message || busy} onClick={() => void copy()} />
    </div>
    <div className="zm-source-body" ref={body}>
      {message ? <><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString()}</time><pre>{message.content || '这条消息没有文字内容。'}</pre></> : null}
      {busy ? <p role="status"><Spinner /> 正在读取原文</p> : null}
      {error ? <p role="alert" className="zm-error-line">{error}</p> : null}
      {message?.next_start_offset != null ? <Btn size="sm" busy={busy} onClick={() => void more()}>继续加载原文</Btn> : null}
    </div>
    <footer>{message ? `已加载 ${message.end_offset} / ${message.total_characters} 字符${message.masked_secrets ? ' · 敏感信息已遮蔽' : ''}` : '对话来源'}</footer>
  </dialog>
}
