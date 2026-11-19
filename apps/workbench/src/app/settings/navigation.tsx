import type { ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'

/** Keep drafts mounted while optional controls are collapsed. */
export function SettingsDisclosure({ id, title, open, onToggle, children }: {
  id: string; title: string; open: boolean; onToggle: () => void; children: ReactNode
}) {
  return <section className="zm-settings-disclosure">
    <button type="button" className="zm-settings-disclosure-toggle" aria-expanded={open} aria-controls={`${id}-content`} onClick={onToggle}>
      <ChevronRight aria-hidden className={open ? 'is-open' : ''} />{title}
    </button>
    <div id={`${id}-content`} className="zm-settings-disclosure-content" hidden={!open}>{children}</div>
  </section>
}

export function SettingsTabs<K extends string>({ id, label, value, onChange, items }: {
  id: string; label: string; value: K; onChange: (value: K) => void
  items: ReadonlyArray<{ key: K; label: string; count?: number }>
}) {
  return <div className="zm-settings-tabs" role="tablist" aria-label={label}>
    {items.map((item, index) => <button key={item.key} type="button" role="tab"
      id={`${id}-${item.key}`} aria-controls={`${id}-${item.key}-panel`}
      aria-selected={value === item.key} tabIndex={value === item.key ? 0 : -1}
      onClick={() => onChange(item.key)} onKeyDown={(event) => {
        const next = event.key === 'ArrowRight' ? (index + 1) % items.length
          : event.key === 'ArrowLeft' ? (index + items.length - 1) % items.length
            : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : -1
        if (next < 0) return
        event.preventDefault()
        onChange(items[next].key)
        document.getElementById(`${id}-${items[next].key}`)?.focus()
      }}><span>{item.label}</span>{item.count !== undefined ? <small>{item.count}</small> : null}</button>)}
  </div>
}

export function SettingsPane({ id, name, active, children }: { id: string; name: string; active: string; children: ReactNode }) {
  return <div className="zm-settings-pane" id={`${id}-${name}-panel`} role="tabpanel"
    aria-labelledby={`${id}-${name}`} hidden={active !== name}>{children}</div>
}
