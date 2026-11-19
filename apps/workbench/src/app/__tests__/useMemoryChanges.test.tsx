import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useMemoryChanges } from '../chat/useMemoryChanges'
import type { MemoryChangesView } from '../../api/memoryChanges'

const read = vi.hoisted(() => vi.fn())
vi.mock('../../api/memoryChanges', () => ({ readMemoryChanges: read }))
const view = (id: string, pending = false): MemoryChangesView => ({ session_id: id, changes: {}, pending })
beforeEach(() => { read.mockReset(); read.mockImplementation(async (id: string) => view(id)) })
afterEach(() => { cleanup(); vi.useRealTimers() })

it('旧会话的迟到结果不会覆盖新会话，也不会在打字重渲染时重新请求', async () => {
  let resolve!: (value: MemoryChangesView) => void
  read.mockReturnValueOnce(new Promise((done) => { resolve = done }))
  const hook = renderHook(({ id, run }) => useMemoryChanges(id, [run], ''), { initialProps: { id: 'a', run: 'r1' } })
  const signal = read.mock.calls[0][2].signal
  hook.rerender({ id: 'b', run: 'r2' })
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
  await act(async () => resolve({ ...view('a'), changes: { r1: [{ action: 'add', text: 'Other conversation', old_text: null, origin: 'tool', evidence: null, source_message_id: null, created_at: '' }] } }))
  expect(signal.aborted).toBe(true)
  expect(hook.result.current.changes).toEqual({})
  hook.rerender({ id: 'b', run: 'r2' })
  expect(read).toHaveBeenCalledTimes(2)
})

it('后台整理完成前继续等，完成后停止轮询', async () => {
  vi.useFakeTimers()
  read.mockResolvedValueOnce(view('a', true)).mockResolvedValue(view('a'))
  renderHook(() => useMemoryChanges('a', ['r1'], 'succeeded'))
  await act(async () => {})
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(read).toHaveBeenCalledTimes(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(9000) })
  expect(read).toHaveBeenCalledTimes(2)
})

it('读取失败可恢复，不把失败当成没有记忆变更', async () => {
  vi.useFakeTimers()
  read.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(view('a'))
  const hook = renderHook(() => useMemoryChanges('a', ['r1'], ''))
  await act(async () => {})
  expect(hook.result.current.error).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(hook.result.current.error).toBe(false)
})
