import { useLayoutEffect, type RefObject } from 'react'

export function measureMessageLayout(scroller: HTMLDivElement) {
  if (typeof CSS === 'undefined' || !CSS.supports?.('content-visibility', 'auto')
    || !CSS.supports('contain-intrinsic-block-size', 'auto 1px') || !scroller.clientWidth) return
  const measured = [...scroller.querySelectorAll<HTMLElement>('[data-message-id]:not(.is-measured)')]
    .map((node) => ({ node, height: node.getBoundingClientRect().height }))
  // Read all boxes before writing styles; interleaving forces a layout per message.
  for (const { node, height } of measured) {
    if (!height) continue
    node.style.setProperty('--zm-turn-height', `${height}px`)
    node.classList.add('is-measured')
  }
}

/** Keep every message mounted and searchable, but let the browser skip offscreen layout.
 * Measure before enabling containment so an unseen message never gets a guessed height.
 */
export function useMessageLayout(scrollRef: RefObject<HTMLDivElement | null>, contentVersion: unknown) {
  useLayoutEffect(() => {
    const scroller = scrollRef.current
    if (!scroller || typeof CSS === 'undefined' || !CSS.supports?.('content-visibility', 'auto')
      || !CSS.supports('contain-intrinsic-block-size', 'auto 1px')) return
    let frame = 0
    const measure = () => {
      frame = 0
      // A settings page can temporarily hide the conversation. Wait for its real layout.
      measureMessageLayout(scroller)
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure) }
    schedule()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    observer?.observe(scroller)
    return () => { observer?.disconnect(); cancelAnimationFrame(frame) }
  }, [scrollRef, contentVersion])
}
