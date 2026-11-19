import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AgentRun } from '../../api/agent'
import { endSessionGoal, readSessionGoal, type SessionGoalView } from '../../api/sessionGoal'
import { SessionGoal } from '../chat/SessionGoal'

vi.mock('../../api/sessionGoal', () => ({ readSessionGoal: vi.fn(), endSessionGoal: vi.fn() }))
const read = vi.mocked(readSessionGoal)
const end = vi.mocked(endSessionGoal)
const start = vi.fn(), resume = vi.fn(), pause = vi.fn()
const data = (session = 'A', status: 'active' | 'paused' | 'completed' | 'ended' = 'paused'): SessionGoalView => ({ session_id: session, goal: {
  goal_id: `goal-${session}`, session_id: session, objective: `Verify report ${session}`, status,
  revision: 4, run_id: `run-${session}`, stage_count: 2, progress: 'Report saved', evidence: 'Read-back pending',
  requested_status: null, pause_reason: status === 'paused' ? 'Runtime restarted' : null,
  created_at: '2026-11-14', updated_at: '2026-11-14', status_source: 'runtime',
} })
const mount = (sessionId: string | null = 'A', extra = {}) => <SessionGoal key={sessionId} sessionId={sessionId}
  run={null} available busy={false} canStart onStart={start} onResume={resume} onPause={pause} {...extra} />
beforeEach(() => { vi.resetAllMocks(); read.mockImplementation(async id => data(id)) })
afterEach(() => { cleanup(); vi.useRealTimers() })

it('creates a goal only from an explicit action and preserves ordinary chat', async () => {
  read.mockResolvedValue({ session_id: 'A', goal: null })
  render(mount())
  await waitFor(() => expect(screen.getByRole('button', { name: '将输入作为持续目标' })).toBeEnabled())
  expect(start).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '将输入作为持续目标' }))
  expect(start).toHaveBeenCalledOnce()
})

it('loads the paused objective after reload and resumes its exact revision', async () => {
  render(mount())
  fireEvent.click(await screen.findByRole('button', { name: '继续目标' }))
  expect(resume).toHaveBeenCalledWith(data().goal)
  expect(screen.getByText('已暂停')).toBeInTheDocument()
  expect(screen.getByText('Runtime restarted')).toBeInTheDocument()
})

it('exposes an active goal pause and polls new progress without sending another task', async () => {
  read.mockResolvedValue(data('A', 'active'))
  render(mount('A', { run: { session_id: 'A', run_id: 'run-A', status: 'running' } as AgentRun }))
  fireEvent.click(await screen.findByRole('button', { name: '暂停目标' }))
  expect(pause).toHaveBeenCalledOnce()
  expect(start).not.toHaveBeenCalled()
  expect(resume).not.toHaveBeenCalled()
})

it('labels agent completion and lets the user end rather than claim verification', async () => {
  read.mockResolvedValue(data('A', 'completed'))
  end.mockResolvedValue(data('A', 'ended'))
  render(mount())
  await screen.findByText('Agent 判断完成')
  fireEvent.click(screen.getByRole('button', { name: '结束目标' }))
  await screen.findByText('已结束目标')
  expect(end.mock.calls[0][0].revision).toBe(4)
  expect(screen.getByRole('button', { name: '将输入作为持续目标' })).toBeEnabled()
})

it('keeps the previous state on read failure and refuses stale mutations until refresh', async () => {
  render(mount())
  await screen.findByText('已暂停')
  read.mockRejectedValueOnce(new Error('unavailable'))
  fireEvent.click(screen.getByRole('button', { name: '刷新目标' }))
  await screen.findByRole('status')
  expect(screen.getByRole('button', { name: '继续目标' })).toBeDisabled()
  expect(screen.getByText('已暂停')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重试读取目标' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '继续目标' })).toBeEnabled())
})

it('ignores late reads across A to B to A', async () => {
  let finish!: (value: SessionGoalView) => void
  read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender } = render(mount())
  rerender(mount('B'))
  await screen.findByTitle('Verify report B')
  rerender(mount('A'))
  await screen.findByTitle('Verify report A')
  await act(async () => finish({ ...data(), goal: { ...data().goal!, objective: 'Old A' } }))
  expect(screen.queryByTitle('Old A')).toBeNull()
})
