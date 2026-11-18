import { useCallback, useEffect, useState } from 'react'

/** 亮色 / 暗色 / 跟随系统（默认跟系统，和 ChatGPT、Codex 的设置一样）。记在本机。 */
export type ThemeChoice = 'system' | 'light' | 'dark'
export type Theme = 'light' | 'dark'
export const THEME_CHOICES: readonly ThemeChoice[] = ['system', 'light', 'dark']

// 旧外壳就用这个名字：作者以前选过的亮 / 暗照旧生效。
const KEY = 'zhimeng.desk.theme'
const DARK = '(prefers-color-scheme: dark)'

function readChoice(): ThemeChoice {
  try {
    const raw = window.localStorage.getItem(KEY)
    if (raw === 'system' || raw === 'light' || raw === 'dark') return raw
  } catch {
    // 读不到就跟系统
  }
  return 'system'
}

function systemTheme(): Theme {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK).matches ? 'dark' : 'light'
}

export function useTheme() {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice)
  const [system, setSystem] = useState<Theme>(systemTheme)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined
    const query = window.matchMedia(DARK)
    const onChange = () => setSystem(query.matches ? 'dark' : 'light')
    query.addEventListener?.('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }, [])
  const setChoice = useCallback((next: ThemeChoice) => {
    try {
      window.localStorage.setItem(KEY, next)
    } catch {
      return false
    }
    setChoiceState(next)
    return true
  }, [])
  return { choice, theme: choice === 'system' ? system : choice, setChoice }
}
