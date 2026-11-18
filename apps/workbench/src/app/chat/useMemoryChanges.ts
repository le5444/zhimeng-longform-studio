import { useEffect, useState } from 'react'
import { readMemoryChanges, type MemoryChangesView } from '../../api/memoryChanges'

const EMPTY: MemoryChangesView['changes'] = {}

export function useMemoryChanges(sessionId: string | null, runIds: string[], runKey: string) {
  const key = JSON.stringify([...new Set(runIds)])
  const [record, setRecord] = useState<{ sessionId: string; view: MemoryChangesView } | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  useEffect(() => {
    if (!sessionId || key === '[]') return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      let again = true
      try {
        const view = await readMemoryChanges(sessionId, JSON.parse(key) as string[], { signal: controller.signal })
        if (controller.signal.aborted) return
        setRecord((previous) => previous?.sessionId === sessionId && JSON.stringify(previous.view) === JSON.stringify(view) ? previous : { sessionId, view })
        setFailure(null)
        again = view.pending
      } catch {
        if (controller.signal.aborted) return
        setFailure(sessionId)
      }
      if (again) timer = setTimeout(() => void load(), 3000)
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [sessionId, key, runKey])
  return { changes: record?.sessionId === sessionId ? record.view.changes : EMPTY, error: Boolean(sessionId && failure === sessionId) }
}
