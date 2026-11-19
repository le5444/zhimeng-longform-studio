import { useCallback, useEffect, useRef, useState } from 'react'
import { readCommandSession, stopCommandSession, type CommandOutputPage, type CommandReadOptions, type CommandSessionOutput, type CommandSessionSummary } from '../../api/commandSessions'
import { isCancelledRequest } from '../../api/runtime'

export type OutputStream = 'stdout' | 'stderr'
export interface OutputWindow { text: string; start: number; end: number; total: number }
export interface CommandOutputState {
  command: CommandSessionSummary | null
  stdout: OutputWindow | null
  stderr: OutputWindow | null
  error: string | null
  stopError: string | null
  loading: boolean
  loadingEarlier: OutputStream | null
  stopping: boolean
}

const initial = (): CommandOutputState => ({ command: null, stdout: null, stderr: null, error: null, stopError: null, loading: true, loadingEarlier: null, stopping: false })
const reason = (error: unknown) => error instanceof Error ? error.message : '暂时无法读取命令输出'

/** Ranges are UTF-8 byte offsets, never JS string indices. Concurrent pages may overlap. */
export function mergeOutput(current: OutputWindow | null, page: CommandOutputPage): OutputWindow {
  if (!current) return { text: page.text, start: page.start_offset, end: page.next_offset, total: page.total_bytes }
  const total = Math.max(current.total, page.total_bytes)
  if (page.start_offset >= current.start && page.next_offset <= current.end) return { ...current, total }
  const bytes = new TextEncoder().encode(page.text)
  const before = page.start_offset < current.start
    ? new TextDecoder().decode(bytes.slice(0, Math.max(0, current.start - page.start_offset))) : ''
  const after = page.next_offset > current.end
    ? new TextDecoder().decode(bytes.slice(Math.max(0, current.end - page.start_offset))) : ''
  // The API pages are adjacent; keeping the old window on a gap is safer than
  // silently presenting a partial log as continuous output.
  if (page.next_offset < current.start || page.start_offset > current.end) return current
  return { text: before + current.text + after, start: Math.min(current.start, page.start_offset), end: Math.max(current.end, page.next_offset), total }
}

export function useCommandOutput(sessionId: string, commandId: string, active: boolean) {
  const [state, setState] = useState<CommandOutputState>(initial)
  const stateRef = useRef(state)
  const control = useRef<{ read: (earlier?: OutputStream) => void; stop: () => void } | null>(null)
  useEffect(() => {
    stateRef.current = initial()
    setState(stateRef.current)
    if (!active) return
    const controller = new AbortController()
    let alive = true
    let busy = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let queued: OutputStream | true | null = null
    const update = (patch: Partial<CommandOutputState>) => {
      if (!alive) return
      stateRef.current = { ...stateRef.current, ...patch }
      setState(stateRef.current)
    }
    const accept = (result: CommandSessionOutput) => update({
      command: stateRef.current.command && stateRef.current.command.status !== 'running' && result.status === 'running' ? stateRef.current.command : result,
      stdout: mergeOutput(stateRef.current.stdout, result.stdout),
      stderr: mergeOutput(stateRef.current.stderr, result.stderr),
      error: null, loading: false,
    })
    const read = async (earlier?: OutputStream) => {
      clearTimeout(timer)
      if (busy) { queued = earlier ?? true; return }
      busy = true
      const saved = stateRef.current
      if (earlier) update({ loadingEarlier: earlier })
      const cursor: CommandReadOptions = {}
      for (const stream of ['stdout', 'stderr'] as const) {
        if (saved[stream]) {
          if (earlier === stream) cursor[`${stream}_before`] = saved[stream].start
          else cursor[`${stream}_offset`] = saved[stream].end
        }
      }
      let nextDelay = 1500
      try {
        const result = await readCommandSession(sessionId, commandId, cursor, { signal: controller.signal })
        if (!alive) return
        accept(result)
        if (!earlier && (result.stdout.has_more || result.stderr.has_more)) nextDelay = 50
      } catch (error) {
        if (alive && !isCancelledRequest(error)) update({ error: reason(error), loading: false })
        nextDelay = 4000
      } finally {
        busy = false
        update({ loadingEarlier: null })
        if (alive) {
          if (queued) {
            const next = queued
            queued = null
            void read(next === true ? undefined : next)
          } else {
            const savedNow = stateRef.current
            const unread = (savedNow.stdout && savedNow.stdout.end < savedNow.stdout.total) || (savedNow.stderr && savedNow.stderr.end < savedNow.stderr.total)
            if (savedNow.error || savedNow.command?.status === 'running' || unread) timer = setTimeout(() => { void read() }, nextDelay)
          }
        }
      }
    }
    const stop = async () => {
      if (stateRef.current.stopping) return
      update({ stopping: true, stopError: null })
      try {
        const command = await stopCommandSession(sessionId, commandId, { signal: controller.signal })
        if (!alive) return
        update({ command })
        void read()
      } catch (error) {
        if (alive && !isCancelledRequest(error)) update({ stopError: reason(error) })
      } finally { update({ stopping: false }) }
    }
    control.current = { read: (earlier) => { void read(earlier) }, stop: () => { void stop() } }
    void read()
    return () => {
      alive = false
      controller.abort()
      clearTimeout(timer)
      control.current = null
    }
  }, [sessionId, commandId, active])
  return { ...state,
    retry: useCallback(() => control.current?.read(), []),
    loadEarlier: useCallback((stream: OutputStream) => control.current?.read(stream), []),
    stop: useCallback(() => control.current?.stop(), []),
  }
}
