import { useCallback, useState } from 'react'
import {
  getProjectAssets,
  rebuildProjectAssets,
  type ProjectAssetIndexResponse,
} from '../api/projectAssets'
import { RuntimeApiError } from '../api/runtime'

export type ProjectAssetsLoadState = 'idle' | 'loading' | 'ready' | 'error'

function assetsErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
    return `资产索引失败（HTTP ${error.status}）`
  }
  return error instanceof Error ? error.message : '项目资产索引失败'
}

export function useProjectAssets(projectId: string | null, enabled: boolean) {
  const [state, setState] = useState<ProjectAssetsLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [payload, setPayload] = useState<ProjectAssetIndexResponse | null>(null)

  const rebuild = useCallback(async () => {
    if (!projectId || !enabled) return
    setState('loading')
    setError(null)
    try {
      const next = await rebuildProjectAssets(projectId)
      setPayload(next)
      setState('ready')
    } catch (err) {
      setError(assetsErrorMessage(err))
      setState('error')
    }
  }, [enabled, projectId])

  const refresh = useCallback(async () => {
    if (!projectId || !enabled) return
    setState('loading')
    setError(null)
    try {
      const next = await getProjectAssets(projectId)
      setPayload(next)
      setState('ready')
    } catch {
      // first open may need rebuild
      try {
        const next = await rebuildProjectAssets(projectId)
        setPayload(next)
        setState('ready')
      } catch (rebuildErr) {
        setError(assetsErrorMessage(rebuildErr))
        setState('error')
      }
    }
  }, [enabled, projectId])

  return {
    state,
    error,
    payload,
    rebuild,
    refresh,
    busy: state === 'loading',
  }
}

export type ProjectAssetsState = ReturnType<typeof useProjectAssets>
