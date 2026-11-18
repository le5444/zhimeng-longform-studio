import { useEffect, useRef, useState } from 'react'
import { readSessionContextState, type SessionContextState } from '../../api/contextState'
import { authorFacingError } from '../../lib/authorFacingError'

export function useContextState(sessionId: string | null, runKey: string, manualCheckpointId?: string) {
  const [record, setRecord] = useState<{ sessionId: string; state: SessionContextState | null; error: string | null } | null>(null)
  const [revision, setRevision] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    const refresh = () => setRevision((value) => value + 1)
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [])
  useEffect(() => {
    const request = ++generation.current
    if (!sessionId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let waitingForBackground = false
    const load = async () => {
      try {
        const state = await readSessionContextState(sessionId, { signal: controller.signal })
        if (state.session_id !== sessionId) throw new Error('对话状态不一致，请重试')
        if (controller.signal.aborted || generation.current !== request) return
        setRecord({ sessionId, state, error: null })
        waitingForBackground = state.background_pending || state.compacting
        // Run结束不代表后台压缩结束；以服务端实际状态收口。
        if (waitingForBackground) timer = setTimeout(() => { void load() }, 1500)
      } catch (error) {
        if (controller.signal.aborted || generation.current !== request) return
        setRecord((previous) => ({ sessionId, state: previous?.sessionId === sessionId ? previous.state : null, error: authorFacingError(error) }))
        if (waitingForBackground) timer = setTimeout(() => { void load() }, 3000)
      }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [sessionId, runKey, manualCheckpointId, revision])
  return { state: record?.sessionId === sessionId ? record.state : null,
    error: record?.sessionId === sessionId ? record.error : null,
    refresh: () => setRevision((value) => value + 1) }
}
