import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchDistillationHealth,
  previewDistillationCoverage,
  type DistillationCoverage,
  type DistillationHealth,
  type DistillationProfile,
} from '../api/distillation'
import { RuntimeApiError } from '../api/runtime'

export type PreviewLoadState = 'idle' | 'loading' | 'ready' | 'error'

let sharedHealthRequest: Promise<DistillationHealth> | null = null

function loadDistillationHealth(): Promise<DistillationHealth> {
  if (sharedHealthRequest) return sharedHealthRequest
  const request = fetchDistillationHealth()
  sharedHealthRequest = request
  const clear = () => {
    if (sharedHealthRequest === request) sharedHealthRequest = null
  }
  void request.then(clear, clear)
  return request
}

function previewErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return '覆盖检查超时，请重试'
  }
  return error instanceof Error ? error.message : '蒸馏覆盖检查失败'
}

export function useDistillationPreview(enabled = true) {
  const [health, setHealth] = useState<DistillationHealth | null>(null)
  const [healthState, setHealthState] = useState<PreviewLoadState>('idle')
  const [healthError, setHealthError] = useState<string | null>(null)
  const [query, setQueryValue] = useState('')
  const [profile, setProfileValue] = useState<DistillationProfile>('draft_assist')
  const [coverage, setCoverage] = useState<DistillationCoverage | null>(null)
  const [coverageState, setCoverageState] = useState<PreviewLoadState>('idle')
  const [coverageError, setCoverageError] = useState<string | null>(null)
  const healthEpoch = useRef(0)
  const coverageEpoch = useRef(0)

  const refreshHealth = useCallback(async () => {
    if (!enabled) return
    const epoch = ++healthEpoch.current
    setHealthState('loading')
    setHealthError(null)
    setCoverage(null)
    setCoverageState('idle')
    setCoverageError(null)
    try {
      const result = await loadDistillationHealth()
      if (healthEpoch.current !== epoch) return
      setHealth(result)
      setHealthState('ready')
    } catch (error) {
      if (healthEpoch.current !== epoch) return
      setHealth(null)
      setHealthError(previewErrorMessage(error))
      setHealthState('error')
    }
  }, [enabled])

  useEffect(() => {
    if (!enabled) {
      healthEpoch.current += 1
      coverageEpoch.current += 1
      setHealth(null)
      setHealthState('idle')
      setHealthError(null)
      setCoverage(null)
      setCoverageState('idle')
      setCoverageError(null)
      return
    }
    void refreshHealth()
    return () => {
      healthEpoch.current += 1
      coverageEpoch.current += 1
    }
  }, [enabled, refreshHealth])

  const setQuery = useCallback((value: string) => {
    coverageEpoch.current += 1
    setQueryValue(value)
    setCoverage(null)
    setCoverageState('idle')
    setCoverageError(null)
  }, [])

  const setProfile = useCallback((value: DistillationProfile) => {
    coverageEpoch.current += 1
    setProfileValue(value)
    setCoverage(null)
    setCoverageState('idle')
    setCoverageError(null)
  }, [])

  const runCoverage = useCallback(async () => {
    const trimmed = query.trim()
    if (!enabled || !trimmed || healthState !== 'ready' || coverageState === 'loading') return
    const epoch = ++coverageEpoch.current
    setCoverageState('loading')
    setCoverageError(null)
    setCoverage(null)
    try {
      const result = await previewDistillationCoverage(trimmed, profile)
      if (coverageEpoch.current !== epoch) return
      if (
        health
        && (
          result.library_fingerprint !== health.library_fingerprint
          || result.tool_fingerprint !== health.tool_fingerprint
        )
      ) {
        const message = '蒸馏库或工具版本已变化，请刷新后重新检查'
        setHealth(null)
        setHealthState('error')
        setHealthError(message)
        setCoverage(null)
        setCoverageError(message)
        setCoverageState('error')
        return
      }
      setCoverage(result)
      setCoverageState('ready')
    } catch (error) {
      if (coverageEpoch.current !== epoch) return
      setCoverage(null)
      setCoverageError(previewErrorMessage(error))
      setCoverageState('error')
    }
  }, [coverageState, enabled, health, healthState, profile, query])

  return {
    health,
    healthState,
    healthError,
    refreshHealth,
    query,
    setQuery,
    profile,
    setProfile,
    coverage,
    coverageState,
    coverageError,
    runCoverage,
  }
}
