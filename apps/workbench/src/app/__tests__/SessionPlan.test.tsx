import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AgentActivity, AgentRun } from '../../api/agent'
import type { SessionPlanView } from '../../api/sessionPlan'
import { readSessionPlan } from '../../api/sessionPlan'
import { SessionPlan } from '../chat/SessionPlan'

vi.mock('../../api/sessionPlan', () => ({ readSessionPlan: vi.fn() }))
const read = vi.mocked(readSessionPlan)
const data = (sessionId = 'A', content = 'Verify the saved table'): SessionPlanView => ({ session_id: sessionId, plan: {
  run_id: 'r1', run_ordinal: 1, run_status: 'succeeded', tool_call_id: 't1', updated_at: '2026-10-18T08:00:00Z', source: 'tool_result', status_source: 'agent',
  items: [{ content: 'Read CSV', status: 'completed' }, { content, status: 'in_progress' }],
} })
const run = (status = 'succeeded', id = 'r1') => ({ session_id: 'A', run_id: id, status } as AgentRun)
const mount = (sessionId = 'A') => <SessionPlan key={sessionId} sessionId={sessionId} run={run()} activities={[]} />
afterEach(cleanup)
beforeEach(() => { read.mockReset(); read.mockImplementation(async id => data(id)) })

it('loads a persisted plan without historical activity and keeps it compact', async () => {
  render(mount())
  const section = await screen.findByRole('region', { name: '对话工作计划' })
  expect(section.querySelector('details')).not.toHaveAttribute('open')
  expect(within(section.querySelector('summary')!).getByText('Verify the saved table')).toBeInTheDocument()
  fireEvent.click(section.querySelector('summary')!)
  expect(screen.getByText('来自第 1 轮')).toBeInTheDocument()
  expect(screen.getByText('未完成 1 项')).toBeInTheDocument()
  expect(section.querySelector('.zm-spinner')).toBeNull()
})

it('does not show a plan when none was recorded', async () => {
  read.mockResolvedValue({ session_id: 'A', plan: null })
  render(mount())
  await waitFor(() => expect(read).toHaveBeenCalledOnce())
  expect(screen.queryByRole('region', { name: '对话工作计划' })).toBeNull()
})

it('ignores late A responses across A to B to A navigation', async () => {
  let finish!: (value: SessionPlanView) => void
  read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender } = render(mount())
  rerender(mount('B'))
  await screen.findByTitle('Verify the saved table')
  rerender(mount('A'))
  await waitFor(() => expect(read).toHaveBeenCalledTimes(3))
  await act(async () => finish(data('A', 'Stale first A')))
  expect(screen.queryByText('Stale first A')).toBeNull()
  expect(screen.getByTitle('Verify the saved table')).toBeInTheDocument()
})

it('preserves the last read plan on failure and allows retry', async () => {
  render(mount())
  const section = await screen.findByRole('region', { name: '对话工作计划' })
  fireEvent.click(section.querySelector('summary')!)
  read.mockRejectedValueOnce(new Error('offline'))
  fireEvent.click(screen.getByRole('button', { name: '刷新工作计划' }))
  await screen.findByText('工作计划暂时无法刷新，当前为上次读取的记录。')
  expect(within(section).getByText('未完成 1 项')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重试读取工作计划' }))
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
})

it('refreshes on confirmed plan events but not ordinary text deltas', async () => {
  const { rerender } = render(mount())
  await screen.findByRole('region', { name: '对话工作计划' })
  const events = [{ kind: 'tool_call', session_id: 'A', run_id: 'r1', sequence: 1, payload: { tool_call_id: 't2', tool_name: 'plan_update', arguments: {} } },
    { kind: 'tool_result', session_id: 'A', run_id: 'r1', sequence: 2, payload: { tool_call_id: 't2', status: 'succeeded' } }] as AgentActivity[]
  read.mockResolvedValue(data('A', 'Check final document'))
  rerender(<SessionPlan key="A" sessionId="A" run={run()} activities={events} />)
  await screen.findByTitle('Check final document')
  const count = read.mock.calls.length
  rerender(<SessionPlan key="A" sessionId="A" run={run()} activities={[...events, { kind: 'reasoning', session_id: 'A', run_id: 'r1', sequence: 3, payload: {} } as AgentActivity]} />)
  await act(async () => {})
  expect(read.mock.calls.length).toBe(count)
})

it('does not animate an earlier plan when a different turn is running', async () => {
  render(<SessionPlan sessionId="A" run={run('running', 'r2')} activities={[]} />)
  const section = await screen.findByRole('region', { name: '对话工作计划' })
  fireEvent.click(section.querySelector('summary')!)
  expect(screen.getByText('未完成 1 项')).toBeInTheDocument()
  expect(section.querySelector('.zm-spinner')).toBeNull()
})
