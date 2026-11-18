import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ContextStatus } from '../chat/ContextStatus'
import { useContextState } from '../chat/useContextState'
import type { SessionContextState } from '../../api/contextState'

const api = vi.hoisted(() => ({ readSessionContextState: vi.fn() }))
vi.mock('../../api/contextState', () => api)
const state = (session_id = 's1'): SessionContextState => ({ schema: 'zhimeng.session-context-state.v1', session_id,
  message_count: 8, recent_message_count: 8, compacted_message_count: 0, compacting: false, background_pending: false, latest_checkpoint: null })
const checkpoint = { schema: 'zhimeng.session-checkpoint.v1', checkpoint_id: 'cp1', session_id: 's1', run_id: null,
  through_ordinal: 6, source_message_count: 6, content: '目标：整理资料；待办：复核链接。', content_hash: 'a'.repeat(64), reported_model: null,
  created_at: '2026-10-10T00:00:00Z' } as const

beforeEach(() => { vi.resetAllMocks(); api.readSessionContextState.mockImplementation(async (sid: string) => state(sid)) })
afterEach(() => { cleanup(); vi.useRealTimers() })

it('shows useful compression details and the real summary', () => {
  render(<ContextStatus state={{ ...state(), latest_checkpoint: checkpoint, recent_message_count: 2, compacted_message_count: 6 }} error={null} onRetry={vi.fn()} />)
  fireEvent.click(screen.getByText('摘要与最近对话'))
  expect(screen.getByText(/6 条较早消息已整理为摘要/)).toHaveTextContent('保留 2 条近期消息')
  fireEvent.click(screen.getByText('查看最新对话摘要'))
  expect(screen.getByText(checkpoint.content)).toBeInTheDocument()
})

it('ordinary chat does not show a permanent explanatory context card', () => {
  const view = render(<ContextStatus state={state()} error={null} onRetry={vi.fn()} />)
  expect(view.container).toBeEmptyDOMElement()
})

it('shows active tool compaction and its durable outcome without claiming task completion', () => {
  const record = { run_id: 'r1', state: 'running' as const, folded_results: 0, tokens_before: 481642, tokens_after: null }
  const view = render(<ContextStatus state={{ ...state(), compacting: true, run_compaction: record }} error={null} onRetry={vi.fn()} />)
  expect(screen.getByText('正在整理本轮工具结果…')).toBeInTheDocument()
  expect(screen.queryByText('正在压缩较早对话…')).toBeNull()
  view.rerender(<ContextStatus state={{ ...state(), run_compaction: { ...record, state: 'succeeded', folded_results: 6, tokens_after: 125000 } }} error={null} onRetry={vi.fn()} />)
  expect(screen.getByText(/已整理 6 条工具结果/)).toHaveTextContent('这不代表任务已经完成')
  view.rerender(<ContextStatus state={{ ...state(), run_compaction: { ...record, state: 'interrupted' } }} error={null} onRetry={vi.fn()} />)
  expect(screen.getByText('本轮整理已停止')).toBeInTheDocument()
  expect(screen.getByText(/这次整理没有完成/)).toBeInTheDocument()
})

it('continues after terminal while postprocessing waits and updates automatic compression without reload', async () => {
  vi.useFakeTimers()
  api.readSessionContextState.mockResolvedValueOnce({ ...state(), background_pending: true })
    .mockResolvedValueOnce({ ...state(), background_pending: true, compacting: true })
    .mockResolvedValueOnce({ ...state(), latest_checkpoint: checkpoint, compacted_message_count: 6, recent_message_count: 2 })
  const { result } = renderHook(() => useContextState('s1', 'r1:succeeded'))
  await act(async () => {})
  expect(result.current.state?.background_pending).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(result.current.state?.compacting).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(result.current.state?.latest_checkpoint?.checkpoint_id).toBe('cp1')
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(api.readSessionContextState).toHaveBeenCalledTimes(3)
})

it('ignores late reads from another conversation and discards that conversation summary', async () => {
  let finish!: (value: SessionContextState) => void
  api.readSessionContextState.mockReturnValueOnce(new Promise<SessionContextState>((resolve) => { finish = resolve }))
  const { result, rerender } = renderHook(({ sid }) => useContextState(sid, 'idle'), { initialProps: { sid: 's1' } })
  rerender({ sid: 's2' })
  await waitFor(() => expect(result.current.state?.session_id).toBe('s2'))
  await act(async () => finish({ ...state('s1'), latest_checkpoint: checkpoint }))
  expect(result.current.state?.session_id).toBe('s2')
  expect(result.current.state?.latest_checkpoint).toBeNull()
})

it('read failure preserves last known summary, shows failure and can retry', async () => {
  api.readSessionContextState.mockResolvedValueOnce({ ...state(), latest_checkpoint: checkpoint })
    .mockRejectedValueOnce(new Error('连接暂时中断'))
    .mockResolvedValueOnce(state())
  const { result, rerender } = renderHook(({ key }) => useContextState('s1', key), { initialProps: { key: 'r1:succeeded' } })
  await waitFor(() => expect(result.current.state?.latest_checkpoint).toBeTruthy())
  rerender({ key: 'r2:running' })
  await waitFor(() => expect(result.current.error).toContain('连接暂时中断'))
  expect(result.current.state?.latest_checkpoint?.checkpoint_id).toBe('cp1')
  act(() => result.current.refresh())
  await waitFor(() => expect(result.current.error).toBeNull())
})

it('a transient read failure during compression retries and never labels stale status as current', async () => {
  vi.useFakeTimers()
  api.readSessionContextState.mockResolvedValueOnce({ ...state(), background_pending: true, compacting: true })
    .mockRejectedValueOnce(new Error('暂时断线'))
    .mockResolvedValueOnce({ ...state(), latest_checkpoint: checkpoint })
  const { result } = renderHook(() => useContextState('s1', 'r1:succeeded'))
  await act(async () => {})
  await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
  expect(result.current.error).toContain('暂时断线')
  const view = render(<ContextStatus state={result.current.state} error={result.current.error} onRetry={result.current.refresh} />)
  expect(screen.getByText('上下文状态待刷新')).toBeInTheDocument()
  expect(screen.queryByText('正在压缩较早对话…')).toBeNull()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(result.current.state?.latest_checkpoint).toEqual(checkpoint)
  expect(result.current.error).toBeNull()
  view.unmount()
})
