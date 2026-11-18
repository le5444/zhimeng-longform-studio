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
  const [{ runtime }, setState] = useState({ runtime: initialRuntimeSnapshot, failures: 0 })
  const pending = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null)

  const refresh = useCallback(() => {
    if (pending.current) return pending.current.promise
    const controller = new AbortController()
    const promise = fetchRuntimeSnapshot({ signal: controller.signal }).then((next) => {
      if (controller.signal.aborted) return
      setState((state) => {
        const current = state.runtime
        if (next.connection !== 'offline') {
          // 轮询每 10 秒一次；有意义字段都没变时保持原对象，
          // 避免未变化的轮询结果触发重渲染。
          const same =
            current.connection === next.connection
            && current.ready === next.ready
            && current.serviceStatus === next.serviceStatus
            && current.runStatus === next.runStatus
            && current.version === next.version
            && current.detail === next.detail
          return same && state.failures === 0 ? state : { runtime: same ? current : next, failures: 0 }
        }
        const failures = state.failures + 1
        if (
          current.ready
          && failures < OFFLINE_FAILURE_THRESHOLD
        ) {
          return {
            failures,
            runtime: {
              ...current,
              connection: 'degraded',
              detail: '本机服务响应慢，正在重试',
              checkedAt: next.checkedAt,
            },
          }
        }
        return { runtime: next, failures }
      })
    }).finally(() => {
      if (pending.current?.controller === controller) pending.current = null
    })
    pending.current = { controller, promise }
    return promise
  }, [])

  useEffect(() => {
    void refresh()
    const interval = window.setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => {
      window.clearInterval(interval)
      pending.current?.controller.abort()
      pending.current = null
    }
  }, [refresh])

  return { runtime, refresh }
}
