import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

/** 沿用 ui.Menu 的主题根节点 portal 与固定定位，不被输入区滚动/裁剪。 */
export function ComposerPopover({ anchor, title, onClose, children }: {
  anchor: RefObject<HTMLButtonElement | null>
  title: string
  onClose: (restoreFocus: boolean) => void
  children: ReactNode
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const heading = useId()
  const [position, setPosition] = useState({ top: 0, left: 0, ready: false })
  const close = useRef(onClose)
  close.current = onClose
  useLayoutEffect(() => {
    const place = () => {
      const rect = anchor.current?.getBoundingClientRect()
      if (!rect || !ref.current) return
      const width = ref.current.offsetWidth
      const height = ref.current.offsetHeight
      const top = rect.top - height - 8
      setPosition({ left: Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - width - 12)),
        top: Math.max(12, top >= 12 ? top : Math.min(rect.bottom + 8, window.innerHeight - height - 12)), ready: true })
    }
    place()
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(place) : null
    if (ref.current) observer?.observe(ref.current)
    window.addEventListener('resize', place)
    return () => { observer?.disconnect(); window.removeEventListener('resize', place) }
  }, [anchor])
  useEffect(() => {
    const focus = requestAnimationFrame(() => (ref.current?.querySelector<HTMLElement>('input,select')
      ?? ref.current?.querySelector<HTMLElement>('button:not([data-close-popup])'))?.focus())
    const outside = (event: PointerEvent) => {
      const target = event.target as Node
      if (!ref.current?.contains(target) && !anchor.current?.contains(target)) close.current(false)
    }
    const key = (event: KeyboardEvent) => {
      if (event.isComposing) return
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(true); return }
      if (event.key !== 'Tab' || !ref.current?.contains(document.activeElement)) return
      const controls = [...ref.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]')]
      const first = controls[0], last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    window.addEventListener('pointerdown', outside, true)
    window.addEventListener('keydown', key, true)
    return () => { cancelAnimationFrame(focus); window.removeEventListener('pointerdown', outside, true); window.removeEventListener('keydown', key, true) }
  }, [anchor])
  return createPortal(<div ref={ref} className="zm-composer-popover" role="dialog" aria-labelledby={heading}
    style={{ top: position.top, left: position.left, visibility: position.ready ? undefined : 'hidden' }}>
    <header><strong id={heading}>{title}</strong><button type="button" data-close-popup aria-label={`关闭${title}`} onClick={() => close.current(true)}><X aria-hidden /></button></header>
    {children}
  </div>, anchor.current?.closest('.zm') ?? document.body)
}
