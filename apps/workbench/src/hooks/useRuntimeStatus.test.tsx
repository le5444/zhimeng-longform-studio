import { act, cleanup, render, screen } from '@testing-library/react'
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
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('Runtime 响应超时，正在重试')

    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('degraded:true')

    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(screen.getByTestId('runtime-state')).toHaveTextContent('offline:false')
  })
})
