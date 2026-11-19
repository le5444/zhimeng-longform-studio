import { useEffect, useState } from 'react'

export interface ChatPreferences {
  expandThinking: boolean
  groupTools: boolean
}
const KEY = 'zhimeng.app.chat-preferences'
const EVENT = 'zhimeng:chat-preferences'
const DEFAULTS: ChatPreferences = { expandThinking: true, groupTools: true }

export function readChatPreferences(): ChatPreferences {
  try {
    const saved = JSON.parse(window.localStorage.getItem(KEY) ?? '{}')
    return {
      expandThinking: saved?.expandThinking !== false,
      groupTools: saved?.groupTools !== false,
    }
  } catch { return DEFAULTS }
}

export function writeChatPreferences(patch: Partial<ChatPreferences>): boolean {
  const next = { ...readChatPreferences(), ...patch }
  try { window.localStorage.setItem(KEY, JSON.stringify(next)) } catch { return false }
  window.dispatchEvent(new CustomEvent(EVENT, { detail: next }))
  return true
}

export function useChatPreferences(): ChatPreferences {
  const [value, setValue] = useState(readChatPreferences)
  useEffect(() => {
    const local = (event: Event) => setValue((event as CustomEvent<ChatPreferences>).detail)
    const storage = (event: StorageEvent) => { if (event.key === KEY || event.key === null) setValue(readChatPreferences()) }
    window.addEventListener(EVENT, local)
    window.addEventListener('storage', storage)
    return () => { window.removeEventListener(EVENT, local); window.removeEventListener('storage', storage) }
  }, [])
  return value
}
