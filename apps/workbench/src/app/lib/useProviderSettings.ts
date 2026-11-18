import { useCallback, useEffect, useRef, useState } from 'react'
import { readProviderCatalog, readProviderSettings, type ProviderCatalogView, type ProviderSettingsView } from '../../api/providerSettings'
import { subscribeSettingsChanges } from '../../lib/settingsChanges'

/** 当前在用的模型服务（只读视图，密钥永远不回传）。设置页保存后调 reload。 */
export function useProviderSettings(enabled: boolean) {
  const [settings, setSettings] = useState<ProviderSettingsView | null>(null)
  const [catalog, setCatalog] = useState<ProviderCatalogView | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const request = useRef(0)
  const invalidate = useCallback(() => { request.current++ }, [])

  const reload = useCallback(async (options: { throwOnError?: boolean; background?: boolean } = {}) => {
    const sequence = ++request.current
    if (!options.background) setState('loading')
    try {
      const profiles = await readProviderCatalog()
      // New runtimes return the default from the same snapshot as the catalog.
      // The separate read only supports older runtimes during an upgrade.
      const next = profiles.active_settings ?? await readProviderSettings()
      if (sequence === request.current) {
        setSettings(profiles.active_settings ?? next)
        setCatalog(profiles)
        setState('ready')
      }
      return profiles.active_settings ?? next
    } catch (error) {
      if (sequence === request.current) setState('error')
      if (options.throwOnError) throw error
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    void reload()
    const refresh = () => { void reload({ background: true }) }
    const unsubscribe = subscribeSettingsChanges('providers', refresh)
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') refresh() }, 15_000)
    return () => { unsubscribe(); window.clearInterval(timer); invalidate() }
  }, [enabled, invalidate, reload])

  return { settings, catalog, state, reload }
}

export type ProviderSettingsApi = ReturnType<typeof useProviderSettings>
