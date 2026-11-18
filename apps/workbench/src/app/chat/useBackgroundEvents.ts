import { useCallback, useEffect, useRef, useState } from 'react'
import { readBackgroundEvents, type BackgroundEventsView } from '../../api/backgroundEvents'
import type { AgentRun } from '../../api/agent'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'

const NO_EVENTS: BackgroundEventsView['events'] = []

/** Driven by the existing session queue poll; this hook creates no timer. */
export function useBackgroundEvents(sessionId: string | null, onRun?: (run: AgentRun) => void | Promise<void>) {
  const [record, setRecord] = useState<{ sessionId: string; view: BackgroundEventsView; error: string | null } | null>(null)
  const [failure, setFailure] = useState<{ sessionId: string; message: string } | null>(null)
  const current = useRef(sessionId); current.current = sessionId
  const callback = useRef(onRun); callback.current = onRun
  const reading = useRef<{ sessionId: string; controller: AbortController } | null>(null)
  const unavailable = useRef<string | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    unavailable.current = null
    return () => { mounted.current = false; reading.current?.controller.abort(); reading.current = null }
  }, [sessionId])
  const reload = useCallback(async () => {
    if (!sessionId || unavailable.current === sessionId || reading.current?.sessionId === sessionId) return
    reading.current?.controller.abort()
    const controller = new AbortController()
    reading.current = { sessionId, controller }
    try {
      const view = await readBackgroundEvents(sessionId, { signal: controller.signal })
      if (!mounted.current || current.current !== sessionId || controller.signal.aborted) return
      if (view.events.some((event) => event.owner_session_id !== sessionId)
        || (view.active_run && view.active_run.session_id !== sessionId)
        || (view.latest_run && view.latest_run.session_id !== sessionId)) throw new Error('后台任务返回的不是当前对话。')
      setRecord({ sessionId, view, error: null })
      setFailure(null)
      const run = view.active_run ?? view.latest_run
      if (run) await callback.current?.(run)
    } catch (error) {
      if (!mounted.current || current.current !== sessionId || controller.signal.aborted) return
      if (error instanceof RuntimeApiError && error.status === 404) { unavailable.current = sessionId; return }
      setFailure({ sessionId, message: authorFacingError(error, '后台命令状态暂未更新') })
    } finally {
      if (reading.current?.controller === controller) reading.current = null
    }
  }, [sessionId])
  const view = record?.sessionId === sessionId ? record.view : null
  const waiting = view && view.pending_count > 0 && view.events.some((event) => event.delivery_state === 'pending' && !event.payload.automatic_delivery_error)
  return { events: view?.events ?? NO_EVENTS, polling: Boolean(view?.has_running_commands || waiting || view?.active_run),
    error: failure?.sessionId === sessionId ? failure.message : null, reload }
}
