import type { ReactNode } from 'react'

/** Shared row layout for project and session file-change history. */
export function ChangeRow({
  title,
  detail,
  meta,
  dot,
  onClick,
  active = false,
}: {
  title: string
  detail?: string | null
  meta: ReactNode
  dot: string
  onClick: () => void
  active?: boolean
}) {
  return (
    <button
      type="button"
      className={`zm-change-row${active ? ' is-active' : ''}`}
      aria-current={active ? 'true' : undefined}
      onClick={onClick}
    >
      <span className={`zm-dot is-${dot}`} aria-hidden />
      <span className="zm-change-row-title" title={title}>{title}</span>
      {detail ? <span className="zm-change-row-detail" title={detail}>{detail}</span> : null}
      <span className="zm-change-row-meta">{meta}</span>
    </button>
  )
}
