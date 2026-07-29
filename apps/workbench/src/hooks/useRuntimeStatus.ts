import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchRuntimeSnapshot,
  initialRuntimeSnapshot,
  type RuntimeSnapshot,
} from '../api/runtime'

const POLL_INTERVAL_MS = 10_000
const OFFLINE_FAILURE_THRESHOLD = 3

export function useRuntimeStatus(): {
  runtime: RuntimeSnapshot
  refresh: () => Promise<void>
} {
  const [runtime, setRuntime] = useState(initialRuntimeSnapshot)
  const consecutiveOfflineRef = useRef(0)

  const refresh = useCallback(async () => {
    setRuntime((current) =>
      current.connection === 'checking'
        ? current
        : { ...current, detail: '正在重新检查 Runtime' },
    )
    const next = await fetchRuntimeSnapshot()
    setRuntime((current) => {
      if (next.connection !== 'offline') {
        consecutiveOfflineRef.current = 0
        return next
      }
      consecutiveOfflineRef.current += 1
      if (
        current.ready
        && consecutiveOfflineRef.current < OFFLINE_FAILURE_THRESHOLD
      ) {
        return {
          ...current,
          connection: 'degraded',
          detail: 'Runtime 响应超时，正在重试',
          checkedAt: next.checkedAt,
        }
      }
      return next
    })
  }, [])

  useEffect(() => {
    void refresh()
    const interval = window.setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => window.clearInterval(interval)
  }, [refresh])

  return { runtime, refresh }
}
