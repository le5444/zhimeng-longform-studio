import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getDistillationInvocation,
  isRuntimeDistillationInvocationId,
  type DistillationInvocationSnapshot,
} from '../api/distillation'
import { RuntimeApiError } from '../api/runtime'

export type DistillationInvocationLoadState =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'missing'
  | 'error'

export interface DistillationInvocationState {
  snapshot: DistillationInvocationSnapshot | null
  state: DistillationInvocationLoadState
  error: string | null
  refresh: () => void
}

function invocationErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof Error) return error.message
  return 'Runtime 深蒸馏 Invocation 读取失败'
}

export function useDistillationInvocation(
  invocationId: string | null | undefined,
  enabled = true,
): DistillationInvocationState {
  const [snapshot, setSnapshot] = useState<DistillationInvocationSnapshot | null>(null)
  const [state, setState] = useState<DistillationInvocationLoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const epoch = useRef(0)

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  useEffect(() => {
    const requestEpoch = ++epoch.current
    if (!enabled || !isRuntimeDistillationInvocationId(invocationId)) {
      setSnapshot(null)
      setState('idle')
      setError(null)
      return
    }

    const controller = new AbortController()
    setSnapshot(null)
    setState('loading')
    setError(null)
    void getDistillationInvocation(invocationId, {}, controller.signal)
      .then((nextSnapshot) => {
        if (controller.signal.aborted || epoch.current !== requestEpoch) return
        if (nextSnapshot.invocation.invocation_id !== invocationId) {
          setSnapshot(null)
          setState('error')
          setError(
            `Runtime 返回的 Invocation 身份与当前来源不一致：${nextSnapshot.invocation.invocation_id} != ${invocationId}`,
          )
          return
        }
        setSnapshot(nextSnapshot)
        setState('ready')
      })
      .catch((nextError: unknown) => {
        if (controller.signal.aborted || epoch.current !== requestEpoch) return
        if (nextError instanceof RuntimeApiError && nextError.status === 404) {
          setSnapshot(null)
          setState('missing')
          setError(null)
          return
        }
        setSnapshot(null)
        setState('error')
        setError(invocationErrorMessage(nextError))
      })
    return () => controller.abort()
  }, [enabled, invocationId, refreshKey])

  return {
    snapshot,
    state,
    error,
    refresh,
  }
}
