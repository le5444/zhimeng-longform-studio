import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useBackgroundEvents } from '../chat/useBackgroundEvents'
import { RuntimeApiError } from '../../api/runtime'
import type { BackgroundEventsView, CommandCompletionEvent } from '../../api/backgroundEvents'

const read = vi.hoisted(() => vi.fn())
vi.mock('../../api/backgroundEvents', () => ({ readBackgroundEvents: read }))
const event = (state: 'pending' | 'delivered' = 'pending'): CommandCompletionEvent => ({ sequence: 1, event_id: 'e1', command_session_id: 'c1', owner_session_id: 's1', owner_run_id: 'r1', status: 'exited', exit_code: 7, ended_at: 1, delivery_state: state, delivered_run_id: state === 'delivered' ? 'r2' : null, payload: { command: 'python task.py' } })
const view = (patch: Partial<BackgroundEventsView> = {}): BackgroundEventsView => ({ events: [], next_cursor: 0, active_run: null, latest_run: null, has_running_commands: false, pending_count: 0, ...patch })
beforeEach(() => { read.mockReset(); read.mockResolvedValue(view()) })
afterEach(cleanup)

describe('后台命令观察', () => {
  it('不增加独立时钟，外部轮询更新同序号的交付状态', async () => {
    const onRun = vi.fn()
    read.mockResolvedValueOnce(view({ events: [event()], pending_count: 1 })).mockResolvedValueOnce(view({ events: [event('delivered')] }))
    const { result } = renderHook(() => useBackgroundEvents('s1', onRun))
    expect(read).not.toHaveBeenCalled()
    await act(() => result.current.reload())
    expect(result.current.polling).toBe(true)
    await act(() => result.current.reload())
    expect(result.current.events[0].delivery_state).toBe('delivered')
    expect(result.current.polling).toBe(false)
    expect(onRun).not.toHaveBeenCalled()
  })
  it('同会话最新内部Run交给现有对话，结果条不创建假用户消息', async () => {
    const run = { run_id: 'r2', session_id: 's1', status: 'running', origin_kind: 'command_completion' } as never
    read.mockResolvedValue(view({ active_run: run, has_running_commands: false }))
    const onRun = vi.fn()
    const { result } = renderHook(() => useBackgroundEvents('s1', onRun))
    await act(() => result.current.reload())
    expect(onRun).toHaveBeenCalledWith(run)
    expect(result.current.polling).toBe(true)
  })
  it('自动接续已因连接失败延期时保留原因但不永久空转轮询', async () => {
    read.mockResolvedValue(view({ events: [{ ...event(), payload: { automatic_delivery_error: '模型连接未配置' } }], pending_count: 1 }))
    const hook = renderHook(() => useBackgroundEvents('s1'))
    await act(() => hook.result.current.reload())
    expect(hook.result.current.events[0].payload.automatic_delivery_error).toBe('模型连接未配置')
    expect(hook.result.current.polling).toBe(false)
  })
  it('切会话中止A请求，迟到A结果不能显示到B或触发接续', async () => {
    let done!: (value: BackgroundEventsView) => void
    read.mockReturnValueOnce(new Promise((resolve) => { done = resolve }))
    const onRun = vi.fn()
    const hook = renderHook(({ id }) => useBackgroundEvents(id, onRun), { initialProps: { id: 's1' } })
    let pending!: Promise<void>
    act(() => { pending = hook.result.current.reload() })
    const signal = read.mock.calls[0][1].signal
    hook.rerender({ id: 's2' })
    expect(signal.aborted).toBe(true)
    await act(async () => { done(view({ events: [event()] })); await pending })
    expect(hook.result.current.events).toEqual([])
    expect(onRun).not.toHaveBeenCalled()
  })
  it('接口失败保留已知结果和轮询资格，旧服务404不显示假故障', async () => {
    read.mockResolvedValueOnce(view({ events: [event()], pending_count: 1 })).mockRejectedValueOnce(new Error('网络暂断'))
    const hook = renderHook(() => useBackgroundEvents('s1'))
    await act(() => hook.result.current.reload())
    await act(() => hook.result.current.reload())
    expect(hook.result.current.events).toHaveLength(1)
    expect(hook.result.current.error).toBe('网络暂断')
    expect(hook.result.current.polling).toBe(true)
    hook.unmount()
    read.mockRejectedValue(new RuntimeApiError(404, {}))
    const old = renderHook(() => useBackgroundEvents('old'))
    await act(() => old.result.current.reload())
    const count = read.mock.calls.length
    await act(() => old.result.current.reload())
    expect(read).toHaveBeenCalledTimes(count)
    expect(old.result.current.error).toBeNull()
  })
})
