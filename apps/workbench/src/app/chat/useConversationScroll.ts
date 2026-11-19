import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { firstMessageAfter } from './messageGeometry'

export type EarlierMessagesState = 'idle' | 'loading' | 'failed'

interface ReadingPosition {
  messageId: string | undefined
  offset: number
  fromBottom: number
}

function readingPosition(node: HTMLDivElement): ReadingPosition {
  const top = node.getBoundingClientRect().top
  const messages = [...node.querySelectorAll<HTMLElement>('[data-message-id]')]
  const message = messages[firstMessageAfter(messages, 'bottom', top)]
  return { messageId: message?.dataset.messageId, offset: message ? message.getBoundingClientRect().top - top : 0,
    fromBottom: node.scrollHeight - node.scrollTop }
}

export function useConversationScroll(scope: string, contentVersion: unknown, fetchEarlier: () => Promise<void>) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stick = useRef(true)
  const followed = useRef<{ top: number; height: number } | null>(null)
  const lifetime = useRef({ active: false, frame: 0, followFrame: 0 })
  const pending = useRef<{ position: ReadingPosition | null; restoring: boolean; latest: boolean } | null>(null)
  const [atBottom, setAtBottom] = useState(true)
  const [history, setHistory] = useState<{ scope: string; state: EarlierMessagesState }>({ scope, state: 'idle' })

  useLayoutEffect(() => {
    const current = { active: true, frame: 0, followFrame: 0 }
    lifetime.current = current
    pending.current = null
    followed.current = null
    stick.current = true
    setAtBottom(true)
    setHistory({ scope, state: 'idle' })
    return () => {
      current.active = false
      cancelAnimationFrame(current.frame)
      cancelAnimationFrame(current.followFrame)
    }
  }, [scope])

  const followLatest = useCallback(() => {
    const owner = lifetime.current
    if (owner.followFrame || !stick.current || pending.current) return
    // Several stream commits can share a paint. Measure the final layout once,
    // and recheck the reader's position: they may have scrolled or switched away.
    owner.followFrame = requestAnimationFrame(() => {
      owner.followFrame = 0
      const node = scrollRef.current
      if (owner.active && node && stick.current && !pending.current) {
        const height = node.scrollHeight
        node.scrollTop = height
        followed.current = { top: node.scrollTop, height }
      }
    })
  }, [])

  useLayoutEffect(followLatest, [scope, contentVersion, followLatest])

  const onScroll = useCallback(() => {
    const node = scrollRef.current
    if (!node) return
    if (pending.current) {
      if (!pending.current.restoring) pending.current.position = readingPosition(node)
      return
    }
    const height = node.scrollHeight
    const top = node.scrollTop
    const previous = followed.current
    // Revealing offscreen messages can grow the page after our scroll. A downward
    // layout adjustment is not the reader scrolling up; finish following the tail.
    if (stick.current && previous && height !== previous.height && top >= previous.top) {
      followed.current = { top, height }
      followLatest()
      return
    }
    const bottom = height - top - node.clientHeight < 80
    stick.current = bottom
    followed.current = bottom ? { top, height } : null
    setAtBottom(bottom)
  }, [followLatest])

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const owner = lifetime.current
    const observer = new ResizeObserver(() => {
      if (!owner.active || pending.current) return
      if (stick.current) followLatest()
      else onScroll()
    })
    observer.observe(node)
    if (node.firstElementChild) observer.observe(node.firstElementChild)
    return () => observer.disconnect()
  }, [scope, followLatest, onScroll])

  const toBottom = useCallback(() => {
    if (pending.current) pending.current.latest = true
    // A long animated jump lays out every message it passes and its destination
    // can move while offscreen content is revealed. Go straight to the latest.
    const node = scrollRef.current
    node?.scrollTo({ top: node.scrollHeight, behavior: 'instant' })
    stick.current = true
    followed.current = node ? { top: node.scrollTop, height: node.scrollHeight } : null
    setAtBottom(true)
    followLatest()
  }, [followLatest])

  const loadEarlier = useCallback(async () => {
    if (pending.current) return
    const owner = lifetime.current
    const node = scrollRef.current
    const request = { position: node ? readingPosition(node) : null, restoring: false, latest: false }
    const current = () => owner.active && pending.current === request
    pending.current = request
    setHistory({ scope, state: 'loading' })
    try {
      await fetchEarlier()
      if (!current()) return
      request.restoring = true
      // The DOM needs the prepended messages before restoring the reading position.
      owner.frame = requestAnimationFrame(() => {
        if (!current()) return
        if (node && scrollRef.current === node) {
          const position = request.position
          const message = position?.messageId ? [...node.querySelectorAll<HTMLElement>('[data-message-id]')]
            .find((item) => item.dataset.messageId === position.messageId) : null
          if (request.latest) node.scrollTop = node.scrollHeight
          else if (message && position) node.scrollTop += message.getBoundingClientRect().top - node.getBoundingClientRect().top - position.offset
          else if (position) node.scrollTop = node.scrollHeight - position.fromBottom
        }
        pending.current = null
        setHistory({ scope, state: 'idle' })
        onScroll()
      })
    } catch {
      if (!current()) return
      pending.current = null
      setHistory({ scope, state: 'failed' })
      onScroll()
    }
  }, [fetchEarlier, onScroll, scope])

  return { scrollRef, atBottom, onScroll, toBottom, loadEarlier, earlier: history.scope === scope ? history.state : 'idle' }
}
