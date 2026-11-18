import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentSession } from '../../api/agent'
import { useConversations } from '../lib/useConversations'

const api = vi.hoisted(() => ({ list: vi.fn(), update: vi.fn(), remove: vi.fn(), rebind: vi.fn() }))
vi.mock('../../api/agent', () => ({
  listAllAgentSessions: api.list, updateAgentSession: api.update,
  deleteAgentSession: api.remove, rebindAgentSessionProject: api.rebind,
}))
afterEach(() => { cleanup(); vi.useRealTimers() })
beforeEach(() => { vi.resetAllMocks(); api.list.mockResolvedValue([session()]) })

function session(overrides: Partial<AgentSession> = {}): AgentSession {
  return { session_id: 'a', title: '原名字', project_id: null, project_revision: null,
    pinned: false, status: 'active', updated_at: '2026-09-16T10:00:00Z',
    last_message_ordinal: 1, last_run_ordinal: 1, ...overrides } as AgentSession
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('左栏对话异步更新', () => {
  it('离线保留已读列表，重连读回最新状态', async () => {
    const hook = renderHook(({ enabled }) => useConversations(enabled, []), { initialProps: { enabled: true } })
    await waitFor(() => expect(hook.result.current.byId.has('a')).toBe(true))
    hook.rerender({ enabled: false })
    expect(hook.result.current.byId.get('a')?.title).toBe('原名字')
    api.list.mockResolvedValue([session({ title: '后台已完成', status: 'idle' })])
    hook.rerender({ enabled: true })
    await waitFor(() => expect(hook.result.current.byId.get('a')?.title).toBe('后台已完成'))
  })
  it('轮询更新后台状态，请求未结束不重叠，卸载停止轮询', async () => {
    vi.useFakeTimers()
    const waiting = deferred<AgentSession[]>()
    const hook = renderHook(() => useConversations(true, []))
    await act(async () => undefined)
    expect(hook.result.current.byId.get('a')?.running).toBe(true)
    api.list.mockReturnValueOnce(waiting.promise)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(api.list).toHaveBeenCalledTimes(2)
    await act(() => vi.advanceTimersByTimeAsync(9000))
    expect(api.list).toHaveBeenCalledTimes(2)
    await act(async () => { waiting.resolve([session({ status: 'idle' })]) })
    expect(hook.result.current.byId.get('a')?.running).toBe(false)
    hook.unmount()
    await act(() => vi.advanceTimersByTimeAsync(9000))
    expect(api.list).toHaveBeenCalledTimes(2)
  })
  it('删除后晚到的旧列表不会复活对话，并自动获取新列表', async () => {
    const lateList = deferred<AgentSession[]>()
    api.list.mockResolvedValueOnce([session()]).mockReturnValueOnce(lateList.promise).mockResolvedValue([])
    const { result } = renderHook(() => useConversations(true, []))
    await waitFor(() => expect(result.current.byId.has('a')).toBe(true))
    act(() => result.current.refresh())
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2))
    await act(() => result.current.remove('a'))
    expect(result.current.byId.has('a')).toBe(false)
    await act(async () => { lateList.resolve([session()]) })
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.byId.has('a')).toBe(false)
    expect(api.list).toHaveBeenCalledTimes(3)
  })

  it('同一条对话连续改名和置顶串行执行，保留两项结果', async () => {
    const rename = deferred<AgentSession>()
    api.update.mockReturnValueOnce(rename.promise).mockResolvedValue(session({ title: '新名字', pinned: true }))
    const { result } = renderHook(() => useConversations(true, []))
    await waitFor(() => expect(result.current.state).toBe('ready'))
    let first!: Promise<void>
    let second!: Promise<void>
    act(() => { first = result.current.rename('a', '新名字'); second = result.current.togglePin('a', true) })
    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1))
    await act(async () => { rename.resolve(session({ title: '新名字' })); await Promise.all([first, second]) })
    expect(api.update).toHaveBeenNthCalledWith(2, 'a', { pinned: true })
    expect(result.current.byId.get('a')).toMatchObject({ title: '新名字', pinned: true })
  })

  it('关闭再启用后的旧异步操作不会盖掉新列表', async () => {
    const oldRename = deferred<AgentSession>()
    api.update.mockReturnValue(oldRename.promise)
    const { result, rerender } = renderHook(({ enabled }) => useConversations(enabled, []), { initialProps: { enabled: true } })
    await waitFor(() => expect(result.current.state).toBe('ready'))
    let operation!: Promise<void>
    act(() => { operation = result.current.rename('a', '过期名字') })
    await waitFor(() => expect(api.update).toHaveBeenCalled())
    rerender({ enabled: false })
    api.list.mockResolvedValue([session({ title: '服务最新名字' })])
    rerender({ enabled: true })
    await waitFor(() => expect(result.current.byId.get('a')?.title).toBe('服务最新名字'))
    await act(async () => { oldRename.resolve(session({ title: '过期名字' })); await operation })
    expect(result.current.byId.get('a')?.title).toBe('服务最新名字')
  })
})
