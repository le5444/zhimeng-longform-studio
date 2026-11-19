import { memo, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { ArrowDown, History } from 'lucide-react'
import type { AgentMessage } from '../../api/agent'
import type { EarlierMessagesState } from './useConversationScroll'
import { firstMessageAfter } from './messageGeometry'
import './conversationRail.css'

export const ConversationRail = memo(function ConversationRail({ messages, scrollRef, hasEarlier, earlier, onLoadEarlier, onLatest }: {
  messages: readonly AgentMessage[]
  scrollRef: RefObject<HTMLDivElement | null>
  hasEarlier: boolean
  earlier: EarlierMessagesState
  onLoadEarlier: () => Promise<void>
  onLatest: () => void
}) {
  const entries = useMemo(() => {
    const items: { id: string; prompt: string; answer: string }[] = []
    for (const message of messages) {
      if (message.role === 'user') items.push({ id: message.message_id, prompt: message.content || '图片消息', answer: '' })
      else if (message.role === 'assistant' && items.length) items[items.length - 1].answer = message.content
    }
    return items
  }, [messages])
  const [active, setActive] = useState<string | null>(null)
  const [layout, setLayout] = useState({ top: 72, height: 400 })
  const [preview, setPreview] = useState<{ id: string; left: number; top: number } | null>(null)
  const rail = useRef<HTMLElement | null>(null)
  const messageNodes = useRef(new Map<string, HTMLElement>())
  useEffect(() => {
    const scroller = scrollRef.current
    if (!scroller) return
    let scheduled = 0
    const entryIds = new Set(entries.map((entry) => entry.id))
    const nodes = [...scroller.querySelectorAll<HTMLElement>('[data-message-id]')]
      .filter((node) => entryIds.has(node.dataset.messageId!))
    messageNodes.current = new Map(nodes.map((node) => [node.dataset.messageId!, node]))
    const measure = () => {
      scheduled = 0
      const next = { top: scroller.offsetTop + 8, height: Math.max(80, scroller.clientHeight - 16) }
      setLayout((current) => current.top === next.top && current.height === next.height ? current : next)
      const top = scroller.getBoundingClientRect().top + 36
      const index = Math.max(0, firstMessageAfter(nodes, 'top', top) - 1)
      const selected = nodes[index]?.dataset.messageId ?? entries[0]?.id ?? null
      setActive(selected)
    }
    const schedule = () => { if (!scheduled) scheduled = requestAnimationFrame(measure) }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    observer?.observe(scroller)
    if (scroller.firstElementChild) observer?.observe(scroller.firstElementChild)
    scroller.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    return () => { observer?.disconnect(); scroller.removeEventListener('scroll', schedule); window.removeEventListener('resize', schedule); cancelAnimationFrame(scheduled) }
  }, [entries, scrollRef])
  const sessionId = messages[0]?.session_id
  useEffect(() => { setPreview(null) }, [sessionId])
  if (entries.length < 2 && !hasEarlier) return null
  const shown = entries.find((entry) => entry.id === preview?.id)
  const show = (id: string, button: HTMLButtonElement) => {
    const rect = button.getBoundingClientRect()
    setPreview({ id, left: rect.right + 12, top: Math.max(12, Math.min(rect.top - 20, window.innerHeight - 210)) })
  }
  const jump = (id: string) => {
    const scroller = scrollRef.current
    const node = messageNodes.current.get(id)
    if (!scroller || !node) return
    scroller.scrollTo({ top: scroller.scrollTop + node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 16, behavior: 'instant' })
    setActive(id)
    setPreview(null)
  }
  return <>
    <nav ref={rail} className="zm-history-rail" aria-label="对话历史定位" style={layout} onMouseLeave={() => setPreview(null)}>
      {hasEarlier ? <button className="zm-history-control" type="button" disabled={earlier === 'loading'} title={earlier === 'failed' ? '重试加载更早的消息' : '加载更早的消息'} aria-label={earlier === 'failed' ? '重试加载更早的消息' : '加载更早的消息'} onClick={() => void onLoadEarlier()}><History aria-hidden /></button> : null}
      <div className="zm-history-marks">
        {entries.map((entry, index) => <button key={entry.id} type="button" className={`zm-history-mark${entry.id === active ? ' is-current' : ''}`}
          aria-label={`跳到第 ${index + 1} 条消息：${entry.prompt.slice(0, 60)}`} aria-current={entry.id === active ? 'location' : undefined}
          onMouseEnter={(event) => show(entry.id, event.currentTarget)} onFocus={(event) => show(entry.id, event.currentTarget)} onBlur={() => setPreview(null)}
          onKeyDown={(event) => { if (event.key === 'Escape') setPreview(null) }} onClick={() => jump(entry.id)}><span /></button>)}
      </div>
      <button type="button" className="zm-history-control" aria-label="回到最新消息" title="回到最新消息" onClick={() => { setPreview(null); onLatest() }}><ArrowDown aria-hidden /></button>
    </nav>
    {shown && preview ? createPortal(<div className="zm-history-preview" role="tooltip" style={{ left: preview.left, top: preview.top }}>
      <strong>{shown.prompt}</strong><p>{shown.answer ? shown.answer.replace(/[#*`]/g, '').slice(0, 320) : '尚无回复'}</p>
    </div>, rail.current?.closest('.zm') ?? document.body) : null}
  </>
})
