/**
 * 新界面的基础件：按钮、图标按钮、弹出菜单、开关、输入框、确认框、提示条、转圈。
 * 样式在 app.css（zm- 前缀，令牌 --zm-*）。别的地方不再各写一套按钮。
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type Ref,
  type TextareaHTMLAttributes,
} from 'react'
import { createPortal } from 'react-dom'

// ============================================================
// 按钮
// ============================================================

export interface BtnProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  children?: ReactNode
  icon?: ReactNode
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  busy?: boolean
  ref?: Ref<HTMLButtonElement>
}

/** 带字的按钮。主操作用 primary（墨色），次要的 secondary（描边），不抢眼的 ghost。 */
export function Btn({ children, icon, variant = 'secondary', size = 'md', busy = false, className, disabled, type = 'button', ref, ...rest }: BtnProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={`zm-btn is-${variant} is-${size}${className ? ` ${className}` : ''}`}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Spinner /> : icon}
      {children !== undefined ? <span>{children}</span> : null}
    </button>
  )
}

export interface IconBtnProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children' | 'aria-label'> {
  /** 图标按钮没有字，这句既是读屏的名字，也是鼠标停上去的提示。 */
  label: string
  icon: ReactNode
  size?: 'sm' | 'md'
  active?: boolean
  ref?: Ref<HTMLButtonElement>
}

export function IconBtn({ label, icon, size = 'md', active = false, className, type = 'button', title, ref, ...rest }: IconBtnProps) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={title ?? label}
      className={`zm-icon-btn is-${size}${active ? ' is-active' : ''}${className ? ` ${className}` : ''}`}
      {...rest}
    >
      {icon}
    </button>
  )
}

export function Spinner({ label }: { label?: string }) {
  return <span className="zm-spinner" role={label ? 'status' : undefined} aria-label={label} aria-hidden={label ? undefined : true} />
}

// ============================================================
// 弹出菜单
// ============================================================

export interface MenuItem {
  key: string
  label: string
  icon?: ReactNode
  hint?: string
  danger?: boolean
  disabled?: boolean
  onSelect: () => void
}

/**
 * 菜单固定定位到应用根节点：绕开左栏滚动区，同时继承主题和字号。
 * 点别处、按 Esc、选了一项都会关；上下键在项之间走。
 */
export function Menu({ trigger, items, align = 'start', label }: {
  /** 渲染触发按钮：拿到 open 状态和开关函数、ref。 */
  trigger: (props: { open: boolean; toggle: () => void; ref: Ref<HTMLButtonElement>; menuId: string }) => ReactNode
  items: MenuItem[]
  align?: 'start' | 'end'
  label: string
}) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const menuId = useId()
  const close = useCallback(() => setOpen(false), [])

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return
    const rect = triggerRef.current.getBoundingClientRect()
    const width = menuRef.current?.offsetWidth ?? 200
    const height = menuRef.current?.offsetHeight ?? 0
    const left = align === 'end' ? rect.right - width : rect.left
    const below = rect.bottom + 4
    const top = below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 4) : below
    setPosition({ top, left: Math.min(Math.max(8, left), window.innerWidth - width - 8) })
  }, [align, open])

  useEffect(() => {
    if (!open) return undefined
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      close()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        close()
        triggerRef.current?.focus()
      }
    }
    window.addEventListener('pointerdown', onPointer, true)
    window.addEventListener('keydown', onKey)
    const onScroll = (event: Event) => { if (!menuRef.current?.contains(event.target as Node)) close() }
    window.addEventListener('resize', close)
    window.addEventListener('scroll', onScroll, true)
    // 打开时焦点落到第一项，键盘能直接选。
    requestAnimationFrame(() => menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus())
    return () => {
      window.removeEventListener('pointerdown', onPointer, true)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [close, open])

  const onMenuKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const buttons = [...(menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const next = event.key === 'ArrowDown' ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length
    buttons[next]?.focus()
  }

  return (
    <>
      {trigger({ open, toggle: () => setOpen((value) => !value), ref: triggerRef, menuId })}
      {open
        ? createPortal(
          <div
            ref={menuRef}
            id={menuId}
            role="menu"
            aria-label={label}
            className="zm-menu"
            style={position ? { top: position.top, left: position.left } : { visibility: 'hidden', top: 0, left: 0 }}
            onKeyDown={onMenuKey}
          >
            {items.map((item) => (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                className={`zm-menu-item${item.danger ? ' is-danger' : ''}`}
                disabled={item.disabled}
                title={item.hint}
                onClick={() => { close(); item.onSelect() }}
              >
                {item.icon ? <span className="zm-menu-icon" aria-hidden>{item.icon}</span> : null}
                <span>{item.label}</span>
              </button>
            ))}
          </div>,
          triggerRef.current?.closest('.zm') ?? document.body,
        )
        : null}
    </>
  )
}

// ============================================================
// 开关、输入框
// ============================================================

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`zm-switch${checked ? ' is-on' : ''}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="zm-switch-knob" aria-hidden />
    </button>
  )
}

export function TextInput({ className, ref, ...props }: InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement> }) {
  return <input ref={ref} {...props} className={`zm-input${className ? ` ${className}` : ''}`} />
}

export function TextArea({ className, ref, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement> & { ref?: Ref<HTMLTextAreaElement> }) {
  return <textarea ref={ref} {...props} className={`zm-textarea${className ? ` ${className}` : ''}`} />
}

// ============================================================
// 确认框
// ============================================================

export interface ConfirmRequest {
  title: ReactNode
  body?: ReactNode
  confirmText?: string
  danger?: boolean
  onConfirm: () => void | Promise<void>
}
export type AskConfirm = (request: ConfirmRequest) => void

export function ConfirmDialog({ request, busy, onConfirm, onCancel, cancelText }: {
  request: ConfirmRequest
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
  cancelText: string
}) {
  const confirmRef = useRef<HTMLButtonElement | null>(null)
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const titleId = useId()
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    if (dialog && typeof dialog.showModal === 'function') dialog.showModal()
    else dialog?.setAttribute('open', '')
    confirmRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onCancel(); return }
      if (event.key !== 'Tab' || !dialog) return
      const items = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), [tabindex="0"]'))
      const first = items[0]
      const last = items.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey); previous?.focus() }
  }, [onCancel])
  return (
    <dialog ref={dialogRef} className="zm-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); onCancel() }}
      onClick={(event) => { if (event.target === event.currentTarget) onCancel() }}>
      <h2 id={titleId}>{request.title}</h2>
      {request.body ? <div className="zm-dialog-body">{request.body}</div> : null}
      <div className="zm-dialog-actions">
        <Btn variant="ghost" onClick={onCancel} disabled={busy}>{cancelText}</Btn>
        <Btn ref={confirmRef} variant={request.danger ? 'danger' : 'primary'} busy={busy} onClick={onConfirm}>
          {request.confirmText}
        </Btn>
      </div>
    </dialog>
  )
}

// ============================================================
// 右下角的提示条
// ============================================================

export type ToastKind = 'ok' | 'warn' | 'danger' | 'info'
export type Notify = (kind: ToastKind, text: string) => void
export interface Toast {
  id: number
  kind: ToastKind
  text: string
}

export function useToasts(): { toasts: Toast[]; notify: Notify; dismiss: (id: number) => void } {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(0)
  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((item) => item.id !== id)), [])
  const notify = useCallback<Notify>((kind, text) => {
    const id = ++nextId.current
    setToasts((current) => [...current.slice(-3), { id, kind, text }])
    window.setTimeout(() => dismiss(id), kind === 'danger' ? 9000 : 4500)
  }, [dismiss])
  return { toasts, notify, dismiss }
}

export function Toasts({ toasts, onDismiss, closeLabel }: { toasts: Toast[]; onDismiss: (id: number) => void; closeLabel: string }) {
  if (toasts.length === 0) return null
  return (
    <div className="zm-toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`zm-toast is-${toast.kind}`}>
          <span>{toast.text}</span>
          <button type="button" className="zm-toast-close" aria-label={closeLabel} onClick={() => onDismiss(toast.id)}>×</button>
        </div>
      ))}
    </div>
  )
}
