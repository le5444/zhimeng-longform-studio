import { StrictMode } from 'react'
import { act, cleanup, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RuntimeSnapshot } from '../api/runtime'
import { useRuntimeStatus } from './useRuntimeStatus'

const mocks = vi.hoisted(() => ({
  fetchRuntimeSnapshot: vi.fn(),
}))

vi.mock('../api/runtime', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/runtime')>(),
  fetchRuntimeSnapshot: mocks.fetchRuntimeSnapshot,
}))

function snapshot(
  connection: RuntimeSnapshot['connection'],
  ready: boolean,
): RuntimeSnapshot {
  return {
    connection,
    ready,
    serviceStatus: ready ? 'ready' : 'offline',
    runStatus: null,
    version: '0.1.0',
    detail: ready ? 'Runtime 已就绪' : '本机 Runtime 未连接',
    checkedAt: new Date().toISOString(),
  }
}

function Harness() {
  const { runtime } = useRuntimeStatus()
  return <div data-testid="runtime-state">{runtime.connection}:{String(runtime.ready)}:{runtime.detail}</div>
}

describe('useRuntimeStatus', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    mocks.fetchRuntimeSnapshot.mockReset()
  })

  it('StrictMode 每个失败只计一次，第三次失败才转离线', async () => {
    vi.useFakeTimers()
    mocks.fetchRuntimeSnapshot.mockResolvedValue(snapshot('online', true))
    const hook = renderHook(useRuntimeStatus, { wrapper: StrictMode })
    await act(async () => undefined)
    mocks.fetchRuntimeSnapshot.mockResolvedValue(snapshot('offline', false))
    await act(() => hook.result.current.refresh())
    expect(hook.result.current.runtime.ready).toBe(true)
    await act(() => hook.result.current.refresh())
    expect(hook.result.current.runtime.ready).toBe(true)
    await act(() => hook.result.current.refresh())
    expect(hook.result.current.runtime.connection).toBe('offline')
  })

  it('慢请求期间轮询和手动刷新共用请求，卸载取消旧观察', async () => {
    vi.useFakeTimers()
    let resolve!: (value: RuntimeSnapshot) => void
    mocks.fetchRuntimeSnapshot.mockImplementation(() => new Promise<RuntimeSnapshot>(done => { resolve = done }))
    const hook = renderHook(useRuntimeStatus)
    act(() => { void hook.result.current.refresh() })
    await act(() => vi.advanceTimersByTimeAsync(30_000))
    expect(mocks.fetchRuntimeSnapshot).toHaveBeenCalledTimes(1)
    const signal = mocks.fetchRuntimeSnapshot.mock.calls[0][0].signal as AbortSignal
    hook.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => resolve(snapshot('offline', false)))
  })

  it('keeps a bound workspace through two transient polling failures', async () => {
    vi.useFakeTimers()
    mocks.fetchRuntimeSnapshot
      .mockResolvedValueOnce(snapshot('online', true))
      .mockResolvedValue(snapshot('offline', false))
    render(<Harness />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('online:true')

    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('degraded:true')
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('本机服务响应慢，正在重试')

    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('degraded:true')

    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('offline:false')
  })
})
