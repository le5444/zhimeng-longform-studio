import { useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

interface Props {
  pane: 'side' | 'panel'
  label: string
  value: number
  min: number
  max: number
  defaultValue: number
  onChange: (value: number, persist: boolean) => void
  onCancel: () => void
}

export function PaneResizeHandle({ pane, label, value, min, max, defaultValue, onChange, onCancel }: Props) {
  const drag = useRef<{ id: number; x: number; width: number; next: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const direction = pane === 'side' ? 1 : -1
  const clamp = (width: number) => Math.round(Math.max(min, Math.min(max, width)))
  const finish = (node: HTMLDivElement, cancel = false) => {
    const current = drag.current
    if (!current) return
    drag.current = null
    setDragging(false)
    if (cancel || current.next === current.width) onCancel()
    else if (current.next !== current.width) onChange(current.next, true)
    if (node.hasPointerCapture(current.id)) node.releasePointerCapture(current.id)
  }
  const move = (event: PointerEvent<HTMLDivElement>) => {
    const current = drag.current
    if (!current || current.id !== event.pointerId) return
    current.next = clamp(current.width + (event.clientX - current.x) * direction)
    onChange(current.next, false)
  }
  const keyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && drag.current) {
      event.preventDefault()
      finish(event.currentTarget, true)
      return
    }
    const next = event.key === 'ArrowLeft' ? value - 16 * direction
      : event.key === 'ArrowRight' ? value + 16 * direction
      : event.key === 'Home' ? min : event.key === 'End' ? max : null
    if (next === null) return
    event.preventDefault()
    onChange(clamp(next), true)
  }
  return <div
    className={`zm-pane-resize is-${pane}${dragging ? ' is-dragging' : ''}`}
    role="separator" aria-label={label} aria-orientation="vertical"
    aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(value)}
    tabIndex={0} title={label}
    onPointerDown={event => {
      if (event.button !== 0) return
      event.preventDefault()
      event.currentTarget.focus({ preventScroll: true })
      drag.current = { id: event.pointerId, x: event.clientX, width: value, next: value }
      event.currentTarget.setPointerCapture(event.pointerId)
      setDragging(true)
    }}
    onPointerMove={move}
    onPointerUp={event => { if (drag.current?.id === event.pointerId) { move(event); finish(event.currentTarget) } }}
    onPointerCancel={event => finish(event.currentTarget, true)}
    onLostPointerCapture={event => finish(event.currentTarget, true)}
    onKeyDown={keyDown}
    onDoubleClick={() => onChange(defaultValue, true)}
  />
}
