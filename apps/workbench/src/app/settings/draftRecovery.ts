import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState } from 'react'

const PREFIX = 'zhimeng.settings-draft.v1:'
export const recoveryKey = (section: string) => `${PREFIX}${section}`
export const DraftRecoveryContext = createContext<(key: string, discard: () => boolean) => () => void>(() => () => undefined)

export function recoveredSettingsSections(): Array<'models' | 'mcp'> {
  try { return (['models', 'mcp'] as const).filter(key => sessionStorage.getItem(recoveryKey(key)) !== null) }
  catch { return [] }
}

export function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
export const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string')

/** A tab-local draft, never a saved configuration. Synchronous writes finish
 * before the next navigation; cleanup does not replay old component state. */
export function useDraftRecovery<T>(section: string, parse: (value: unknown) => T) {
  const key = recoveryKey(section)
  const [loaded] = useState(() => {
    let raw: string | null = null
    try {
      raw = sessionStorage.getItem(key)
      if (raw === null) return { value: null, raw, error: '', blocked: false }
      const envelope: unknown = JSON.parse(raw)
      if (!record(envelope) || envelope.version !== 1) throw new Error('unsupported draft')
      return { value: parse(envelope.value), raw, error: '', blocked: false }
    } catch {
      return { value: null, raw, error: raw === null ? '无法读取此标签页的草稿备份。' : '旧草稿格式无法读取，原备份已保留。', blocked: true }
    }
  })
  const [error, setError] = useState(loaded.error)
  const [unreadable, setUnreadable] = useState(loaded.blocked)
  const [retained, setRetained] = useState(loaded.value !== null)
  const previous = useRef(loaded.raw)
  const blocked = useRef(loaded.blocked)
  const desired = useRef<T | null>(loaded.value)
  const register = useContext(DraftRecoveryContext)
  const persist = useCallback((value: T | null) => {
    desired.current = value
    if (blocked.current) return false
    const next = value === null ? null : JSON.stringify({ version: 1, value })
    try {
      if (sessionStorage.getItem(key) !== previous.current) {
        blocked.current = true
        setUnreadable(true)
        throw new Error('draft changed')
      }
      if (next !== previous.current) {
        if (next === null) sessionStorage.removeItem(key)
        else sessionStorage.setItem(key, next)
        previous.current = next
      }
      setRetained(next !== null)
      setError('')
      return true
    } catch {
      setError('草稿备份未能更新。当前输入仍在页面中，刷新后可能丢失输入或恢复旧草稿。')
      return false
    }
  }, [key])
  const discard = useCallback(() => {
    try {
      sessionStorage.removeItem(key)
      previous.current = null
      blocked.current = false
      setUnreadable(false)
      setError('')
      setRetained(false)
      return true
    } catch {
      setError('无法移除此标签页的草稿备份，尚未放弃修改，请重试。')
      return false
    }
  }, [key])
  useLayoutEffect(() => register(section, discard), [discard, register, section])
  return { initial: loaded.value, error, unreadable, retained, persist, discard,
    recovered: loaded.value !== null,
    retry: () => persist(desired.current),
    removeUnreadable: () => {
      if (!discard()) return
      blocked.current = false
      setUnreadable(false)
      persist(desired.current)
    },
  }
}
