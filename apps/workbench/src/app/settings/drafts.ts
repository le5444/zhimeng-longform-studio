import { createContext, useContext, useEffect } from 'react'

export const SettingsDraftContext = createContext<(section: string, dirty: boolean, description?: string) => void>(() => undefined)

export function useSettingsDraft(section: string, dirty: boolean, description?: string) {
  const update = useContext(SettingsDraftContext)
  useEffect(() => {
    if (description === undefined) update(section, dirty)
    else update(section, dirty, description)
    return () => update(section, false)
  }, [dirty, section, update, description])
}
