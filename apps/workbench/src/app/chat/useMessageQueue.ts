import { useCallback, useEffect, useRef, useState } from 'react'
import { enqueueMessage, readMessageQueue, steerQueuedMessage, updateMessageQueue, type QueueInput, type QueueState, type QueueUpdate } from '../../api/agentQueue'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { clearQueueSendReceipt, prepareQueueSendReceipt, readQueueSendReceipt } from '../lib/queueSendReceipt'

export function useMessageQueue({ sessionId, enabled, blocked, runKey, onAccepted, onSteered, onPoll, observeBackground = false }: {
  sessionId: string | null
  enabled: boolean
  blocked: boolean
  runKey: string
  onAccepted: () => void
  onSteered?: () => void
  onPoll?: () => void | Promise<void>
  observeBackground?: boolean
}) {
  const [state, setState] = useState<QueueState | null>(null)
  const [busySession, setBusySession] = useState<string | null>(null)
  const [errorState, setError] = useState<{ sessionId: string; message: string; source: 'read' | 'change' } | null>(null)
  const current = useRef(sessionId)
  current.current = sessionId
  const runKeyRef = useRef(runKey)
  runKeyRef.current = runKey
  const mounted = useRef(true)
  const operations = useRef(new Set<string>())
  const readSequence = useRef(0)
  const accepted = useRef(onAccepted)
  accepted.current = onAccepted
  const pollCallback = useRef(onPoll); pollCallback.current = onPoll
  const steered = useRef(onSteered ?? onAccepted)
  steered.current = onSteered ?? onAccepted
  const adopted = useRef<QueueState | null>(null)
  const consumedSteering = useRef(new Set<string>())
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const adopt = useCallback((next: QueueState, reconcile = false) => {
    if (!mounted.current || current.current !== next.session_id) return
    const previous = adopted.current?.session_id === next.session_id ? adopted.current : null
    if (previous && previous.revision > next.revision) return
    adopted.current = next
    setState(next)
    // Acknowledgement only means pending. Disappearance from the persisted queue
    // means the runtime has written this input into the active conversation.
    const consumed = previous?.items.filter((item) => item.delivery === 'steer_pending'
      && !next.items.some((nextItem) => nextItem.item_id === item.item_id)
      && !consumedSteering.current.has(`${next.session_id}:${item.item_id}`)) ?? []
    consumed.forEach((item) => consumedSteering.current.add(`${next.session_id}:${item.item_id}`))
    if (consumed.length) steered.current()
    const changedRun = next.active_run_id && next.active_run_id !== runKeyRef.current.split(':')[0]
    const completedQueueInput = previous?.items.some((item) => item.delivery !== 'steer_pending'
      && !next.items.some((nextItem) => nextItem.item_id === item.item_id))
    if (reconcile && (changedRun || completedQueueInput)) accepted.current()
  }, [])

  const reload = useCallback(async () => {
    if (!sessionId) return
    void pollCallback.current?.()
    const sequence = ++readSequence.current
    try {
      const next = await readMessageQueue(sessionId)
      if (sequence === readSequence.current) {
        adopt(next, true)
        if (mounted.current && current.current === sessionId) {
          setError((previous) => previous?.sessionId === sessionId && previous.source === 'read' ? null : previous)
        }
      }
    } catch (error) {
      if (mounted.current && current.current === sessionId && sequence === readSequence.current) {
        setError((previous) => previous?.sessionId === sessionId && previous.source === 'change' ? previous : { sessionId, message: authorFacingError(error), source: 'read' })
      }
    }
  }, [adopt, sessionId])

  useEffect(() => { void reload() }, [reload, runKey])
  useEffect(() => {
    const onFocus = () => { void reload() }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [reload])

  const transact = useCallback(async <T,>(operation: () => Promise<T>): Promise<T> => {
    if (!sessionId || operations.current.has(sessionId)) throw new Error('队列正在更新，请稍后再试')
    operations.current.add(sessionId)
    setBusySession(sessionId)
    try {
      const result = await operation()
      if (mounted.current && current.current === sessionId) setError(null)
      return result
    } catch (error) {
      if (mounted.current && current.current === sessionId) setError({ sessionId, message: authorFacingError(error), source: 'change' })
      throw error
    } finally {
      operations.current.delete(sessionId)
      if (mounted.current) setBusySession((value) => value === sessionId ? null : value)
    }
  }, [sessionId])

  const persistInput = useCallback(async (input: Omit<QueueInput, 'item_id'>): Promise<string> => {
    if (!sessionId) throw new Error('对话尚未准备好，请稍后再试')
    const itemId = prepareQueueSendReceipt(sessionId, input)
    adopt(await enqueueMessage(sessionId, { ...input, item_id: itemId }))
    clearQueueSendReceipt(sessionId, input, itemId)
    return itemId
  }, [adopt, sessionId])

  const enqueue = useCallback((input: Omit<QueueInput, 'item_id'>): Promise<string> => (
    transact(() => persistInput(input))
  ), [persistInput, transact])

  const view = state?.session_id === sessionId ? state : null
  const error = errorState?.sessionId === sessionId ? errorState.message : null
  const errorSource = errorState?.sessionId === sessionId ? errorState.source : null
  const awaitingDispatch = Boolean(enabled && !blocked && view?.items.length && !view.pause_reason && !view.active_run_id && !error)
  const busy = busySession === sessionId && sessionId !== null

  useEffect(() => {
    if (!view?.items.length && !view?.active_run_id && !observeBackground) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      await reload()
      if (!stopped) timer = setTimeout(() => { void poll() }, 1_000)
    }
    timer = setTimeout(() => { void poll() }, 1_000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [Boolean(view?.items.length || view?.active_run_id || observeBackground), reload])

  const requestSteering = useCallback(async (itemId: string, expectedRunId: string, input?: QueueInput) => {
    if (!sessionId) throw new Error('对话尚未准备好，请稍后再试')
    const result = input ? await steerQueuedMessage(sessionId, itemId, expectedRunId, input) : await steerQueuedMessage(sessionId, itemId, expectedRunId)
    adopt(result.queue)
    // The input can already be consumed before the HTTP acknowledgement arrives.
    const deliveryKey = `${sessionId}:${itemId}`
    if (!result.queue.items.some((item) => item.item_id === itemId)
      && !consumedSteering.current.has(deliveryKey)
      && mounted.current && current.current === sessionId) {
      consumedSteering.current.add(deliveryKey)
      steered.current()
    }
  }, [adopt, sessionId])

  const steer = useCallback(async (itemId: string, expectedRunId: string): Promise<boolean> => {
    if (!sessionId) return false
    return transact(async () => {
      await requestSteering(itemId, expectedRunId)
      return true
    }).catch(() => { void reload(); return false })
  }, [reload, requestSteering, sessionId, transact])

  const enqueueAndSteer = useCallback(async (input: Omit<QueueInput, 'item_id'>, expectedRunId: string): Promise<{ itemId: string; accepted: boolean }> => {
    if (!sessionId) throw new Error('对话尚未准备好，请稍后再试')
    let itemId: string | null = null
    try {
      return await transact(async () => {
        itemId = prepareQueueSendReceipt(sessionId, input)
        // 入队与引导由后端同一次请求受理，后台派发不能在两步之间抢走输入。
        await requestSteering(itemId, expectedRunId, { ...input, item_id: itemId })
        clearQueueSendReceipt(sessionId, input, itemId)
        return { itemId, accepted: true }
      })
    } catch (error) {
      if (itemId === null) throw error
      // 只有核实消息已在队列，才让输入框交出草稿；丢确认时保留相同身份供重试。
      try {
        const saved = await readMessageQueue(sessionId)
        adopt(saved)
        if (saved.items.some((item) => item.item_id === itemId)) {
          clearQueueSendReceipt(sessionId, input, itemId)
          return { itemId, accepted: false }
        }
      } catch { /* 未能核实受理，原草稿继续留在输入框。 */ }
      throw error
    }
  }, [adopt, requestSteering, sessionId, transact])
  const update = useCallback(async (change: QueueUpdate): Promise<boolean> => {
    if (!sessionId) return false
    return transact(async () => {
      const fresh = await readMessageQueue(sessionId)
      try {
        adopt(await updateMessageQueue(sessionId, fresh.revision, change))
      } catch (error) {
        // 停止任务的后台收尾也会暂停队列；冲突后只确认已达成的暂停，不重放修改。
        if (change.action === 'pause' && error instanceof RuntimeApiError && error.status === 409) {
          const confirmed = await readMessageQueue(sessionId).catch(() => null)
          if (confirmed?.session_id === sessionId && confirmed.revision >= fresh.revision && confirmed.pause_reason) {
            adopt(confirmed)
            return true
          }
        }
        throw error
      }
      return true
    }).catch(() => { void reload(); return false })
  }, [adopt, reload, sessionId, transact])

  const hasPendingInput = useCallback((input: Omit<QueueInput, 'item_id'>): boolean => (
    sessionId !== null && readQueueSendReceipt(sessionId, input) !== null
  ), [sessionId])

  return { state: view, busy, awaitingDispatch, error, errorSource, enqueue, enqueueAndSteer, steer, update, reload, hasPendingInput }
}
