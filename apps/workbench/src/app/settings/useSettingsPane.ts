import { createContext, useContext, useEffect, useState } from 'react'

export const SettingsTargetContext = createContext<string | null>(null)

/** Resolve a search destination before the shell scrolls and focuses its field. */
export function useSettingsPane<K extends string>(initial: K, destinations: Record<string, K>) {
  const [pane, setPane] = useState<K>(initial)
  const target = useContext(SettingsTargetContext)
  useEffect(() => {
    if (target && destinations[target]) setPane(destinations[target])
  }, [target, destinations])
  return [pane, setPane] as const
}
