/**
 * Shared settings controls. Persistence and draft ownership live in each section.
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, Ref, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'
import { Btn, Spinner } from '../ui'

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  children: ReactNode
  icon?: ReactNode
  variant?: 'normal' | 'primary' | 'quiet' | 'danger'
  small?: boolean
  block?: boolean
  active?: boolean
  busy?: boolean
  ref?: Ref<HTMLButtonElement>
}

export function Button({ variant = 'normal', small = false, block = false, active = false, className, ...rest }: ButtonProps) {
  const mapped = variant === 'normal' ? 'secondary' : variant === 'quiet' ? 'ghost' : variant
  const classes = [block ? 'is-block' : '', active ? 'is-active' : '', className ?? ''].filter(Boolean).join(' ')
  return <Btn variant={mapped} size={small ? 'sm' : 'md'} className={classes || undefined} {...rest} />
}

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="zm-field">
      <span className="zm-field-label">{label}</span>
      {children}
      {hint ? <small className="zm-field-hint">{hint}</small> : null}
    </label>
  )
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`zm-input${props.className ? ` ${props.className}` : ''}`} />
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`zm-textarea${props.className ? ` ${props.className}` : ''}`} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`zm-select${props.className ? ` ${props.className}` : ''}`} />
}

export function Notice({ kind = 'plain', children, action, role }: { kind?: 'plain' | 'info' | 'ok' | 'warn' | 'danger'; children: ReactNode; action?: ReactNode; role?: 'status' | 'alert' }) {
  return (
    <div className={`zm-notice is-${kind}`} role={role ?? (kind === 'danger' ? 'alert' : undefined)}>
      <div className="zm-notice-text">{children}</div>
      {action ? <div className="zm-notice-action">{action}</div> : null}
    </div>
  )
}

export function Working({ children }: { children: ReactNode }) {
  return <span className="zm-working-inline"><Spinner />{children}</span>
}

export function Panel({ title, children, actions, className }: { title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <section className={`zm-card${className ? ` ${className}` : ''}`}>
      {title || actions ? (
        <div className="zm-card-title">
          {title ? <h3>{title}</h3> : null}
          <span className="zm-spacer" />
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  )
}

export function SectionTitle({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="zm-card-title">
      <h3>{children}</h3>
      <span className="zm-spacer" />
      {actions}
    </div>
  )
}

export function Empty({ title, children, actions }: { title?: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="zm-empty-block">
      {title ? <strong>{title}</strong> : null}
      {children ? <p>{children}</p> : null}
      {actions ? <div className="zm-btn-row">{actions}</div> : null}
    </div>
  )
}
