import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useMessageQueue } from '../chat/useMessageQueue'
import type { QueueInput, QueueState, QueueUpdate } from '../../api/agentQueue'
import { RuntimeApiError } from '../../api/runtime'

const api = vi.hoisted(() => ({ readMessageQueue: vi.fn(), enqueueMessage: vi.fn(), updateMessageQueue: vi.fn(), dispatchQueuedMessage: vi.fn(), steerQueuedMessage: vi.fn() }))
vi.mock('../../api/agentQueue', () => api)
const input: Omit<QueueInput, 'item_id'> = { message: '检查配置文件', project_id: null, writer_mode: 'repair_only', manual_file_ids: [], manual_skill_names: ['文档整理'], open_file_path: null }
const state = (sid = 's1'): QueueState => ({ session_id: sid, revision: 1, pause_reason: null, active_run_id: null, items: [{ ...input, item_id: 'message-0001', sending: false }] })
const options = () => ({ sessionId: 's1', enabled: true, blocked: true, runKey: 'r1:running', onAccepted: vi.fn() })
afterEach(() => { cleanup(); vi.useRealTimers() })
beforeEach(() => {
  window.localStorage.clear()
  vi.resetAllMocks()
  api.readMessageQueue.mockImplementation(async (sid: string) => state(sid))
  api.enqueueMessage.mockImplementation(async (sid: string, value: QueueInput) => ({ ...state(sid), revision: 2, items: [{ ...value, sending: false }] }))
  api.updateMessageQueue.mockImplementation(async (sid: string, _revision, change) => ({ ...state(sid), revision: 2, pause_reason: change.action === 'pause' ? 'user' : null }))
  api.dispatchQueuedMessage.mockImplementation(async () => ({ queue: { ...state(), revision: 3, active_run_id: 'r2', items: [] }, accepted: {} }))
  api.steerQueuedMessage.mockImplementation(async (sid: string, itemId: string, runId: string, supplied?: QueueInput) => ({ queue: {
    ...state(sid), revision: 2, active_run_id: runId,
    items: [{ ...(supplied ?? state(sid).items[0]), item_id: itemId, sending: false, delivery: 'steer_pending', steering_run_id: runId }],
  } }))
})

describe('durable message queue', () => {
  it.each(['cancelled', null] as const)('暂停遇到后台收尾冲突时，只有读回已暂停（%s）才消除冲突', async pauseReason => {
    const { result } = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(result.current.state?.revision).toBe(1))
    api.updateMessageQueue.mockRejectedValueOnce(new RuntimeApiError(409, { detail: '队列已更新' }))
    const confirmed = { ...state(), revision: 3, pause_reason: pauseReason }
    api.readMessageQueue.mockResolvedValueOnce(state()).mockResolvedValue(confirmed)
    let saved = false
    await act(async () => { saved = await result.current.update({ action: 'pause' }) })
    expect(saved).toBe(pauseReason !== null)
    expect(result.current.state).toEqual(confirmed)
    expect(result.current.error).toEqual(pauseReason !== null ? null : expect.stringContaining('队列已更新'))
    expect(api.updateMessageQueue).toHaveBeenCalledOnce()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('暂停冲突后的读回失败仍保留原始错误，不声称已暂停', async () => {
    const { result } = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(result.current.state?.revision).toBe(1))
    api.updateMessageQueue.mockRejectedValueOnce(new RuntimeApiError(409, { detail: '队列已更新' }))
    api.readMessageQueue.mockResolvedValueOnce(state()).mockRejectedValue(new Error('读取失败'))
    let saved = true
    await act(async () => { saved = await result.current.update({ action: 'pause' }) })
    expect(saved).toBe(false)
    expect(result.current.error).toContain('队列已更新')
    expect(result.current.state?.pause_reason).toBeNull()
  })

  it('reuses the queue timer for background commands while the user queue is empty', async () => {
    vi.useFakeTimers()
    api.readMessageQueue.mockResolvedValue({ ...state(), items: [], active_run_id: null })
    const onPoll = vi.fn(async () => undefined)
    const props = { ...options(), onPoll, observeBackground: true }
    const hook = renderHook((value) => useMessageQueue(value), { initialProps: props })
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(onPoll.mock.calls.length).toBeGreaterThan(1)
    expect(onPoll).toHaveBeenCalledTimes(api.readMessageQueue.mock.calls.length)
    hook.rerender({ ...props, observeBackground: false })
    const count = onPoll.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(onPoll).toHaveBeenCalledTimes(count)
  })
  it('image-only input keeps uploaded identities through atomic same-run steering', async () => {
    const { result } = renderHook(() => useMessageQueue(options()))
    await act(async () => { await result.current.enqueueAndSteer({ ...input, message: '', image_ids: ['image-one', 'image-two'] }, 'r1') })
    const sent = api.steerQueuedMessage.mock.calls[0][3]
    expect(sent).toMatchObject({ message: '', image_ids: ['image-one', 'image-two'] })
    expect(api.steerQueuedMessage).toHaveBeenCalledWith('s1', sent.item_id, 'r1', sent)
    expect(result.current.state?.items[0].image_ids).toEqual(['image-one', 'image-two'])
    expect(api.enqueueMessage).not.toHaveBeenCalled()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('image-only ordinary enqueue preserves image identities and other attachments', async () => {
    const { result } = renderHook(() => useMessageQueue(options()))
    const attached = { ...input, message: '', image_ids: ['image-queued'], manual_file_ids: ['attached-file'] }
    let savedId = ''
    await act(async () => { savedId = await result.current.enqueue(attached) })
    expect(api.enqueueMessage).toHaveBeenCalledWith('s1', { ...attached, item_id: savedId })
    expect(result.current.state?.items[0]).toMatchObject({ ...attached, item_id: savedId })
    expect(api.steerQueuedMessage).not.toHaveBeenCalled()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('observes the persisted queue without running frontend inspection or dispatch', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMessageQueue({ ...options(), blocked: false }))
    await act(async () => {})
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(result.current.state?.items[0].item_id).toBe('message-0001')
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
    expect(api.readMessageQueue.mock.calls.length).toBeGreaterThan(1)
  })

  it('discovers a backend-started run and refreshes the conversation', async () => {
    api.readMessageQueue.mockResolvedValue({ ...state(), active_run_id: 'backend-run', items: [] })
    const props = options()
    const { result, rerender } = renderHook((next) => useMessageQueue(next), { initialProps: props })
    await waitFor(() => expect(props.onAccepted).toHaveBeenCalledOnce())
    await waitFor(() => expect(result.current.state?.active_run_id).toBe('backend-run'))
    rerender({ ...props, runKey: 'backend-run:running' })
    await act(async () => { await result.current.reload() })
    expect(props.onAccepted).toHaveBeenCalledOnce()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('keeps saved inputs visible while the model is unavailable', async () => {
    const { result } = renderHook(() => useMessageQueue({ ...options(), enabled: false, blocked: false }))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(1))
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('reads persisted items after remount without sending while a run is active', async () => {
    const first = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(first.result.current.state?.items).toHaveLength(1))
    first.unmount()
    const second = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(second.result.current.state?.items[0].message).toBe(input.message))
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('keeps input visible until the backend confirms consumption, including a fast completed run', async () => {
    const props = { ...options(), blocked: false }
    const { result } = renderHook(() => useMessageQueue(props))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(1))
    let done!: (value: QueueState) => void
    api.readMessageQueue.mockReturnValueOnce(new Promise<QueueState>((resolve) => { done = resolve }))
    let reading!: Promise<void>
    act(() => { reading = result.current.reload() })
    expect(result.current.state?.items).toHaveLength(1)
    expect(props.onAccepted).not.toHaveBeenCalled()
    await act(async () => { done({ ...state(), revision: 3, active_run_id: null, items: [] }); await reading })
    expect(result.current.state?.items).toHaveLength(0)
    expect(props.onAccepted).toHaveBeenCalledTimes(1)
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('shows backend delivery failure with saved input and never retries dispatch in the browser', async () => {
    vi.useFakeTimers()
    api.readMessageQueue.mockResolvedValue({ ...state(), pause_reason: 'send_failed' })
    const props = { ...options(), blocked: false }
    const { result } = renderHook(() => useMessageQueue(props))
    await act(async () => {})
    expect(result.current.state?.pause_reason).toBe('send_failed')
    expect(result.current.state?.items).toHaveLength(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000) })
    expect(result.current.state?.items).toHaveLength(1)
    expect(props.onAccepted).not.toHaveBeenCalled()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('a successful refresh clears a transient read error', async () => {
    api.readMessageQueue.mockRejectedValueOnce(new Error('读取时连接断开'))
    const { result } = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(result.current.error).toContain('读取时连接断开'))
    await act(async () => { await result.current.reload() })
    expect(result.current.state?.items).toHaveLength(1)
    expect(result.current.error).toBeNull()
  })

  it.each(['failed', 'cancelled', 'interrupted', 'user'] as const)('server pause %s is resumed through a persisted update only', async (reason) => {
    api.readMessageQueue.mockResolvedValue({ ...state(), pause_reason: reason })
    const { result } = renderHook(() => useMessageQueue({ ...options(), blocked: false }))
    await waitFor(() => expect(result.current.state?.pause_reason).toBe(reason))
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
    await act(async () => { await result.current.update({ action: 'resume' }) })
    expect(api.updateMessageQueue).toHaveBeenCalledWith('s1', 1, { action: 'resume' })
    expect(result.current.state?.pause_reason).toBeNull()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('late backend observation from A never refreshes B or replaces its queue', async () => {
    const a = { ...options(), blocked: false }
    const { result, rerender } = renderHook((props) => useMessageQueue(props), { initialProps: a })
    await waitFor(() => expect(result.current.state?.session_id).toBe('s1'))
    let done!: (value: QueueState) => void
    api.readMessageQueue.mockReturnValueOnce(new Promise<QueueState>((resolve) => { done = resolve }))
    let reading!: Promise<void>
    act(() => { reading = result.current.reload() })
    const b = { ...options(), sessionId: 's2', onAccepted: vi.fn() }
    rerender(b)
    await waitFor(() => expect(result.current.state?.session_id).toBe('s2'))
    await act(async () => { done({ ...state(), revision: 3, active_run_id: 'late-a', items: [] }); await reading })
    expect(result.current.state?.session_id).toBe('s2')
    expect(result.current.state?.items).toHaveLength(1)
    expect(b.onAccepted).not.toHaveBeenCalled()
  })

  it('retrying an uncertain enqueue reuses the same input id', async () => {
    api.enqueueMessage.mockRejectedValueOnce(new Error('timeout'))
    const { result } = renderHook(() => useMessageQueue(options()))
    await act(async () => { await expect(result.current.enqueue(input)).rejects.toThrow('timeout') })
    await act(async () => { await result.current.enqueue(input) })
    const calls = api.enqueueMessage.mock.calls
    expect(calls[0][1].item_id).toBe(calls[1][1].item_id)
    expect(calls[1][1].manual_skill_names).toEqual(['文档整理'])
  })

  it.each(['enqueue', 'steer'] as const)('keeps an uncertain %s identity across A-B-A with attachments', async (kind) => {
    api.readMessageQueue.mockImplementation(async (sid: string) => ({ ...state(sid), items: [] }))
    const sendApi = kind === 'enqueue' ? api.enqueueMessage : api.steerQueuedMessage
    sendApi.mockRejectedValueOnce(new Error('lost acknowledgement'))
    const hook = renderHook((props) => useMessageQueue(props), { initialProps: options() })
    const attached = { ...input, image_ids: ['image-1'], manual_file_ids: ['file-1'], open_file_path: 'data.csv' }
    const submit = () => kind === 'enqueue' ? hook.result.current.enqueue(attached) : hook.result.current.enqueueAndSteer(attached, 'r1')
    await act(async () => { await expect(submit()).rejects.toThrow('lost acknowledgement') })
    hook.rerender({ ...options(), sessionId: 's2' })
    await act(async () => { await submit() })
    hook.rerender(options())
    await act(async () => { await submit() })
    const payloadIndex = kind === 'enqueue' ? 1 : 3
    expect(sendApi.mock.calls[2][payloadIndex]).toEqual(sendApi.mock.calls[0][payloadIndex])
    expect(sendApi.mock.calls[2][payloadIndex]).toMatchObject(attached)
  })

  it.each(['enqueue', 'steer'] as const)('restores an uncertain %s identity after remount', async (kind) => {
    api.readMessageQueue.mockResolvedValue({ ...state(), items: [] })
    const sendApi = kind === 'enqueue' ? api.enqueueMessage : api.steerQueuedMessage
    sendApi.mockRejectedValueOnce(new Error('lost acknowledgement'))
    const first = renderHook(() => useMessageQueue(options()))
    await act(async () => {
      await expect(kind === 'enqueue' ? first.result.current.enqueue(input) : first.result.current.enqueueAndSteer(input, 'r1')).rejects.toThrow('lost acknowledgement')
    })
    first.unmount()
    const restored = renderHook(() => useMessageQueue(options()))
    expect(restored.result.current.hasPendingInput(input)).toBe(true)
    await act(async () => {
      await (kind === 'enqueue' ? restored.result.current.enqueue(input) : restored.result.current.enqueueAndSteer(input, 'r1'))
    })
    const payloadIndex = kind === 'enqueue' ? 1 : 3
    expect(sendApi.mock.calls[1][payloadIndex]).toEqual(sendApi.mock.calls[0][payloadIndex])
    expect(restored.result.current.hasPendingInput(input)).toBe(false)
  })

  it('preserves the original uncertain submission while another draft is sent', async () => {
    api.enqueueMessage.mockRejectedValueOnce(new Error('lost acknowledgement'))
    const hook = renderHook(() => useMessageQueue(options()))
    await act(async () => { await expect(hook.result.current.enqueue(input)).rejects.toThrow('lost acknowledgement') })
    await act(async () => { await hook.result.current.enqueue({ ...input, message: '另一项任务' }) })
    await act(async () => { await hook.result.current.enqueue(input) })
    expect(api.enqueueMessage.mock.calls[2][1]).toEqual(api.enqueueMessage.mock.calls[0][1])
  })

  it('allows intentional identical messages after acknowledgement with different identities', async () => {
    const hook = renderHook(() => useMessageQueue(options()))
    await act(async () => { await hook.result.current.enqueue(input) })
    await act(async () => { await hook.result.current.enqueue(input) })
    expect(api.enqueueMessage.mock.calls[0][1].item_id).not.toBe(api.enqueueMessage.mock.calls[1][1].item_id)
  })

  it('keeps the input unsent if its retry identity cannot be saved', async () => {
    const failing = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    try {
      const hook = renderHook(() => useMessageQueue(options()))
      await act(async () => { await expect(hook.result.current.enqueue(input)).rejects.toThrow('尚未发送') })
      expect(api.enqueueMessage).not.toHaveBeenCalled()
    } finally { failing.mockRestore() }
  })

  it('retains the acknowledged identity until local cleanup succeeds', async () => {
    const failing = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('unavailable') })
    const hook = renderHook(() => useMessageQueue(options()))
    try {
      await act(async () => { await expect(hook.result.current.enqueue(input)).rejects.toThrow('消息已接收') })
    } finally { failing.mockRestore() }
    await act(async () => { await hook.result.current.enqueue(input) })
    expect(api.enqueueMessage.mock.calls[1][1]).toEqual(api.enqueueMessage.mock.calls[0][1])
    expect(hook.result.current.hasPendingInput(input)).toBe(false)
  })

  it('failed edit reports false and leaves the saved queue intact', async () => {
    api.updateMessageQueue.mockRejectedValue(new Error('队列已更新'))
    const { result } = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(1))
    await act(async () => { expect(await result.current.update({ action: 'edit', item_id: 'message-0001', message: '新消息' })).toBe(false) })
    expect(result.current.state?.items[0].message).toBe(input.message)
  })

  it.each<QueueUpdate>([
    { action: 'pause' },
    { action: 'edit', item_id: 'message-0001', message: '检查新的配置文件' },
    { action: 'move', item_id: 'message-0001', direction: 1 },
    { action: 'remove', item_id: 'message-0001' },
  ])('persists $action against a freshly read revision and adopts the server result', async (change) => {
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await waitFor(() => expect(result.current.state?.revision).toBe(1))
    const first = { ...state().items[0], image_ids: ['saved-image'] }
    const second = { ...first, item_id: 'message-0002', message: '检查文档' }
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 7, items: [first, second] })
    const updated = {
      ...state(), revision: 8, pause_reason: change.action === 'pause' ? 'user' as const : null,
      items: change.action === 'edit' ? [{ ...first, message: change.message! }, second]
        : change.action === 'remove' ? [second]
          : change.action === 'move' ? [second, first] : [first, second],
    }
    api.updateMessageQueue.mockResolvedValue(updated)
    await act(async () => { expect(await result.current.update(change)).toBe(true) })
    expect(api.updateMessageQueue).toHaveBeenCalledWith('s1', 7, change)
    expect(result.current.state).toEqual(updated)
    expect(props.onAccepted).not.toHaveBeenCalled()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('a late enqueue acknowledgement for A preserves B retry identity', async () => {
    let completeA!: (value: QueueState) => void
    api.enqueueMessage.mockImplementationOnce(() => new Promise<QueueState>((resolve) => { completeA = resolve }))
    const { result, rerender } = renderHook((props) => useMessageQueue(props), { initialProps: options() })
    let requestA!: Promise<string>
    act(() => { requestA = result.current.enqueue(input) })
    rerender({ ...options(), sessionId: 's2' })
    api.enqueueMessage.mockRejectedValueOnce(new Error('timeout'))
    await act(async () => { await expect(result.current.enqueue(input)).rejects.toThrow('timeout') })
    await act(async () => { completeA(state('s1')); await requestA })
    await act(async () => { await result.current.enqueue(input) })
    const calls = api.enqueueMessage.mock.calls
    expect(calls[1][1].item_id).toBe(calls[2][1].item_id)
    expect(result.current.state?.session_id).toBe('s2')
  })

  it('returns the persisted input id so direct steering retains its original attachments', async () => {
    const { result } = renderHook(() => useMessageQueue(options()))
    let itemId = ''
    await act(async () => { itemId = await result.current.enqueue(input) })
    expect(itemId).toBe(api.enqueueMessage.mock.calls[0][1].item_id)
    expect(api.enqueueMessage.mock.calls[0][1].manual_skill_names).toEqual(['文档整理'])
  })

  it('waits for steering consumption, then keeps watching the run until it ends', async () => {
    vi.useFakeTimers()
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await act(async () => {})
    await act(async () => { expect(await result.current.steer('message-0001', 'r1')).toBe(true) })
    expect(api.steerQueuedMessage).toHaveBeenCalledWith('s1', 'message-0001', 'r1')
    expect(result.current.state?.items[0].delivery).toBe('steer_pending')
    expect(props.onAccepted).not.toHaveBeenCalled()
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 3, active_run_id: 'r1', items: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(result.current.state?.items).toHaveLength(0)
    expect(props.onAccepted).toHaveBeenCalledTimes(1)
    const activeReads = api.readMessageQueue.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(api.readMessageQueue).toHaveBeenCalledTimes(activeReads + 1)
    expect(props.onAccepted).toHaveBeenCalledTimes(1)
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 4, active_run_id: null, items: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    const reads = api.readMessageQueue.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(api.readMessageQueue).toHaveBeenCalledTimes(reads)
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('keeps an ended-run steering rejection in the queue without starting a different run', async () => {
    api.steerQueuedMessage.mockRejectedValue(new Error('当前任务已经结束，消息保留在队列中'))
    const props = { ...options(), blocked: false }
    api.readMessageQueue.mockResolvedValue({ ...state(), active_run_id: 'r1' })
    const { result } = renderHook(() => useMessageQueue(props))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(1))
    await act(async () => { expect(await result.current.steer('message-0001', 'r1')).toBe(false) })
    expect(result.current.error).toContain('消息保留')
    expect(result.current.state?.items[0].message).toBe(input.message)
    expect(props.onAccepted).not.toHaveBeenCalled()
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
  })

  it('keeps a pending input visible on refresh failure and retries the read, not delivery', async () => {
    vi.useFakeTimers()
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await act(async () => {})
    await act(async () => { await result.current.steer('message-0001', 'r1') })
    api.readMessageQueue.mockRejectedValueOnce(new Error('队列读取断开'))
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(result.current.state?.items[0].delivery).toBe('steer_pending')
    expect(result.current.error).toContain('队列读取断开')
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 3, active_run_id: 'r1', items: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(result.current.error).toBeNull()
    expect(props.onAccepted).toHaveBeenCalledTimes(1)
    expect(api.steerQueuedMessage).toHaveBeenCalledTimes(1)
  })

  it('retains cancelled unconsumed input as an ordinary queued item', async () => {
    vi.useFakeTimers()
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await act(async () => {})
    await act(async () => { await result.current.steer('message-0001', 'r1') })
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 3, pause_reason: 'cancelled' })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(result.current.state?.items[0].message).toBe(input.message)
    expect(result.current.state?.pause_reason).toBe('cancelled')
    expect(props.onAccepted).not.toHaveBeenCalled()
  })

  it('a late steering acknowledgement for A cannot change or refresh B', async () => {
    let complete!: (value: unknown) => void
    api.steerQueuedMessage.mockReturnValue(new Promise((resolve) => { complete = resolve }))
    const { result, rerender } = renderHook((props) => useMessageQueue(props), { initialProps: options() })
    await waitFor(() => expect(result.current.state?.session_id).toBe('s1'))
    let request!: Promise<boolean>
    act(() => { request = result.current.steer('message-0001', 'r1') })
    const next = { ...options(), sessionId: 's2' }
    rerender(next)
    await waitFor(() => expect(result.current.state?.session_id).toBe('s2'))
    await act(async () => { complete({ queue: { ...state(), revision: 3, items: [] } }); await request })
    expect(result.current.state?.session_id).toBe('s2')
    expect(result.current.state?.items).toHaveLength(1)
    expect(next.onAccepted).not.toHaveBeenCalled()
  })

  it('recognizes consumption before HTTP acknowledgement only once', async () => {
    api.steerQueuedMessage.mockResolvedValue({ queue: { ...state(), revision: 3, items: [] } })
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(1))
    await act(async () => { await result.current.steer('message-0001', 'r1') })
    await act(async () => { await result.current.steer('message-0001', 'r1') })
    expect(props.onAccepted).toHaveBeenCalledTimes(1)
    expect(result.current.state?.items).toHaveLength(0)
  })

  it('atomically submits direct steering and hands off the draft only after confirming the failed request was saved', async () => {
    api.readMessageQueue.mockResolvedValue({ ...state(), items: [] })
    let rejectSteering!: (reason: Error) => void
    api.steerQueuedMessage.mockReturnValue(new Promise((_resolve, reject) => { rejectSteering = reject }))
    const props = { ...options(), blocked: false, runKey: 'r1:succeeded' }
    const { result } = renderHook(() => useMessageQueue(props))
    await waitFor(() => expect(result.current.state?.items).toHaveLength(0))
    let request!: Promise<{ itemId: string; accepted: boolean }>
    act(() => { request = result.current.enqueueAndSteer(input, 'r1') })
    await waitFor(() => expect(api.steerQueuedMessage).toHaveBeenCalledTimes(1))
    const supplied = api.steerQueuedMessage.mock.calls[0][3]
    const itemId = supplied.item_id
    expect(supplied).toMatchObject(input)
    expect(api.steerQueuedMessage).toHaveBeenCalledWith('s1', itemId, 'r1', supplied)
    expect(api.enqueueMessage).not.toHaveBeenCalled()
    expect(result.current.busy).toBe(true)
    expect(result.current.state?.items).toHaveLength(0)
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 2, items: [{ ...input, item_id: itemId, sending: false }] })
    await act(async () => {
      rejectSteering(new Error('本轮已结束，消息保留'))
      expect(await request).toEqual({ itemId, accepted: false })
    })
    expect(result.current.busy).toBe(false)
    expect(result.current.error).toContain('本轮已结束')
    expect(result.current.state?.items[0].item_id).toBe(itemId)
    expect(api.dispatchQueuedMessage).not.toHaveBeenCalled()
    expect(props.onAccepted).not.toHaveBeenCalled()
  })

  it('rejects direct steering if the original input could not be persisted', async () => {
    api.steerQueuedMessage.mockRejectedValueOnce(new Error('队列写入失败'))
    const { result } = renderHook(() => useMessageQueue(options()))
    await act(async () => {
      await expect(result.current.enqueueAndSteer(input, 'r1')).rejects.toThrow('队列写入失败')
    })
    expect(api.steerQueuedMessage).toHaveBeenCalledOnce()
    expect(api.enqueueMessage).not.toHaveBeenCalled()
    expect(result.current.error).toContain('队列写入失败')
  })

  it('returns a saved id and accepted status while direct input still waits for the runtime', async () => {
    api.steerQueuedMessage.mockImplementation(async (sid: string, itemId: string) => ({ queue: {
      ...state(sid), revision: 3, active_run_id: 'r1', items: [{ ...input, item_id: itemId, sending: false, delivery: 'steer_pending', steering_run_id: 'r1' }],
    } }))
    const props = options()
    const { result } = renderHook(() => useMessageQueue(props))
    await act(async () => {
      const outcome = await result.current.enqueueAndSteer(input, 'r1')
      expect(outcome).toEqual({ itemId: api.steerQueuedMessage.mock.calls[0][3].item_id, accepted: true })
    })
    expect(result.current.state?.items[0].manual_skill_names).toEqual(input.manual_skill_names)
    expect(result.current.state?.items[0].delivery).toBe('steer_pending')
    expect(props.onAccepted).not.toHaveBeenCalled()
  })

  it('reuses an uncertain atomic steering id and keeps its image and file attachments on retry', async () => {
    api.readMessageQueue.mockResolvedValue({ ...state(), items: [] })
    api.steerQueuedMessage.mockRejectedValueOnce(new Error('确认丢失'))
    const { result } = renderHook(() => useMessageQueue(options()))
    const attached = { ...input, image_ids: ['image-retained'], manual_file_ids: ['file-1'], open_file_path: 'data.csv' }
    await act(async () => {
      await expect(result.current.enqueueAndSteer(attached, 'r1')).rejects.toThrow('确认丢失')
    })
    let delivered!: { itemId: string; accepted: boolean }
    await act(async () => { delivered = await result.current.enqueueAndSteer(attached, 'r1') })
    const [first, second] = api.steerQueuedMessage.mock.calls
    expect(first[1]).toBe(second[1])
    expect(first[3]).toEqual(second[3])
    expect(second[3]).toMatchObject(attached)
    expect(delivered).toEqual({ itemId: first[1], accepted: true })
    expect(api.enqueueMessage).not.toHaveBeenCalled()
  })

  it('cannot confirm an atomic write while queue reads fail and retains its retry identity', async () => {
    const { result } = renderHook(() => useMessageQueue(options()))
    await waitFor(() => expect(result.current.state?.session_id).toBe('s1'))
    api.steerQueuedMessage.mockRejectedValueOnce(new Error('请求超时'))
    api.readMessageQueue.mockRejectedValueOnce(new Error('读取也断开'))
    await act(async () => {
      await expect(result.current.enqueueAndSteer(input, 'r1')).rejects.toThrow('请求超时')
    })
    expect(result.current.error).toContain('请求超时')
    await act(async () => { await result.current.enqueueAndSteer(input, 'r1') })
    expect(api.steerQueuedMessage.mock.calls[0][1]).toBe(api.steerQueuedMessage.mock.calls[1][1])
    expect(result.current.error).toBeNull()
  })

  it('keeps B atomic retry identity after a late successful acknowledgement for A', async () => {
    let completeA!: (value: { queue: QueueState }) => void
    api.steerQueuedMessage.mockImplementationOnce(() => new Promise((resolve) => { completeA = resolve }))
    const { result, rerender } = renderHook((props) => useMessageQueue(props), { initialProps: options() })
    let requestA!: Promise<{ itemId: string; accepted: boolean }>
    act(() => { requestA = result.current.enqueueAndSteer(input, 'r1') })
    const b = { ...options(), sessionId: 's2' }
    rerender(b)
    await waitFor(() => expect(result.current.state?.session_id).toBe('s2'))
    api.steerQueuedMessage.mockRejectedValueOnce(new Error('确认超时'))
    await act(async () => { await expect(result.current.enqueueAndSteer(input, 'r2')).rejects.toThrow('确认超时') })
    await act(async () => { completeA({ queue: { ...state(), revision: 3, items: [] } }); await requestA })
    await act(async () => { await result.current.enqueueAndSteer(input, 'r2') })
    expect(api.steerQueuedMessage.mock.calls[1][1]).toBe(api.steerQueuedMessage.mock.calls[2][1])
    expect(result.current.state?.session_id).toBe('s2')
    expect(b.onAccepted).not.toHaveBeenCalled()
  })

  it('stops watching an empty queue and an unmounted conversation', async () => {
    vi.useFakeTimers()
    const { result, unmount } = renderHook(() => useMessageQueue(options()))
    await act(async () => {})
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 2, items: [] })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(result.current.state?.items).toHaveLength(0)
    const emptyReads = api.readMessageQueue.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(api.readMessageQueue).toHaveBeenCalledTimes(emptyReads)
    api.readMessageQueue.mockResolvedValue({ ...state(), revision: 3 })
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(api.readMessageQueue.mock.calls.length).toBeGreaterThan(emptyReads)
    unmount()
    const unmountedReads = api.readMessageQueue.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); window.dispatchEvent(new Event('focus')) })
    expect(api.readMessageQueue).toHaveBeenCalledTimes(unmountedReads)
  })

  it('stops a pending poll from reviving after switching to no conversation', async () => {
    vi.useFakeTimers()
    const props = { ...options(), sessionId: 's1' as string | null }
    const { result, rerender } = renderHook((next) => useMessageQueue(next), { initialProps: props })
    await act(async () => {})
    let finish!: (value: QueueState) => void
    api.readMessageQueue.mockReturnValueOnce(new Promise<QueueState>((resolve) => { finish = resolve }))
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    rerender({ ...props, sessionId: null })
    await act(async () => { finish({ ...state(), revision: 2, active_run_id: 'late-run', items: [] }) })
    expect(result.current.state).toBeNull()
    expect(props.onAccepted).not.toHaveBeenCalled()
    const reads = api.readMessageQueue.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000) })
    expect(api.readMessageQueue).toHaveBeenCalledTimes(reads)
  })
})
