import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { QueuePanel } from '../chat/QueuePanel'
import type { QueueState } from '../../api/agentQueue'
import { T } from '../strings'

afterEach(cleanup)
const state: QueueState = { session_id: 's1', revision: 2, pause_reason: 'failed', active_run_id: null, items: [
  { item_id: 'message-0001', message: '整理 README', sending: false, project_id: null, writer_mode: 'repair_only', manual_file_ids: [], manual_skill_names: [], open_file_path: null },
  { item_id: 'message-0002', message: '检查配置', sending: false, project_id: null, writer_mode: 'repair_only', manual_file_ids: [], manual_skill_names: [], open_file_path: null },
] }
const Q = T.agent.queue
it('renders every pending input, pause reason, reorder/remove and resume commands', async () => {
  const update = vi.fn(async () => true)
  render(<QueuePanel state={state} busy={false} error={null} onUpdate={update} onReload={vi.fn()} />)
  const user = userEvent.setup()
  expect(screen.getByText(Q.reasons.failed)).toBeInTheDocument()
  expect(screen.getByText('检查配置')).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: Q.up })[0]).toBeDisabled()
  await user.click(screen.getAllByRole('button', { name: Q.up })[1])
  expect(update).toHaveBeenCalledWith({ action: 'move', item_id: 'message-0002', direction: -1 })
  await user.click(screen.getAllByRole('button', { name: Q.remove })[1])
  expect(update).toHaveBeenCalledWith({ action: 'remove', item_id: 'message-0002' })
  await user.click(screen.getByRole('button', { name: Q.resume }))
  expect(update).toHaveBeenCalledWith({ action: 'resume' })
})
it('pauses before editing and keeps unsaved text when saving fails', async () => {
  const update = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false).mockResolvedValueOnce(true)
  render(<QueuePanel state={state} busy={false} error={null} onUpdate={update} onReload={vi.fn()} />)
  const user = userEvent.setup()
  await user.click(screen.getAllByRole('button', { name: Q.edit })[0])
  expect(update).toHaveBeenCalledWith({ action: 'pause' })
  const edit = await screen.findByLabelText(Q.editContent)
  await user.clear(edit)
  await user.type(edit, '检查 README 链接')
  await user.click(screen.getByRole('button', { name: Q.save }))
  expect(edit).toHaveValue('检查 README 链接')
  await user.click(screen.getByRole('button', { name: Q.save }))
  await waitFor(() => expect(screen.queryByLabelText(Q.editContent)).toBeNull())
})

it('offers a visible action for each waiting message targeting the active run', async () => {
  const steer = vi.fn(async () => false)
  render(<QueuePanel state={state} busy={false} error={null} activeRunId="r1" onSteer={steer} onUpdate={vi.fn()} onReload={vi.fn()} />)
  const user = userEvent.setup()
  await user.click(screen.getAllByRole('button', { name: '送入本轮' })[1])
  expect(steer).toHaveBeenCalledWith('message-0002', 'r1')
  expect(screen.getByText('检查配置')).toBeInTheDocument()
})

it('shows pending admission honestly and locks the input until the runtime consumes it', () => {
  const pending: QueueState = { ...state, active_run_id: 'r1', items: [
    { ...state.items[0], delivery: 'steer_pending', steering_run_id: 'r1' },
  ] }
  render(<QueuePanel state={pending} busy={false} error={null} activeRunId="r1" onSteer={vi.fn()} onUpdate={vi.fn()} onReload={vi.fn()} />)
  expect(screen.getByRole('status')).toHaveTextContent('待本轮接收')
  expect(screen.getByText('等待本轮接收')).toBeInTheDocument()
  expect(screen.queryByText(Q.waiting)).toBeNull()
  expect(screen.queryByRole('button', { name: Q.resume })).toBeNull()
  expect(screen.queryByText('已送入本轮')).toBeNull()
  expect(screen.queryByRole('button', { name: '送入本轮' })).toBeNull()
  for (const label of [Q.up, Q.down, Q.edit, Q.remove]) expect(screen.getByRole('button', { name: label })).toBeDisabled()
})

it('retains the queued message and refresh action after a steering error', async () => {
  const reload = vi.fn(async () => {})
  render(<QueuePanel state={state} busy={false} error="任务已结束，消息保留" onUpdate={vi.fn()} onReload={reload} />)
  expect(screen.getByRole('alert')).toHaveTextContent('任务已结束，消息保留')
  expect(screen.getByText('整理 README')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '送入本轮' })).toBeNull()
  await userEvent.setup().click(screen.getByRole('button', { name: Q.reload }))
  expect(reload).toHaveBeenCalledTimes(1)
})

it('does not turn a queue read failure into an empty waiting queue', () => {
  render(<QueuePanel state={null} busy={false} error="队列读取断开" errorSource="read" onUpdate={vi.fn()} onReload={vi.fn()} />)
  expect(screen.getAllByText(Q.unavailable)).toHaveLength(2)
  expect(screen.queryByText(Q.count(0))).toBeNull()
  expect(screen.queryByText(Q.waiting)).toBeNull()
})

it('keeps the last known queue status after a change fails', () => {
  render(<QueuePanel state={state} busy={false} error="任务已结束，消息保留" errorSource="change" onUpdate={vi.fn()} onReload={vi.fn()} />)
  expect(screen.getByText(Q.count(2))).toBeInTheDocument()
  expect(screen.getByText(Q.reasons.failed)).toBeInTheDocument()
  expect(screen.queryByText(Q.unavailable)).toBeNull()
})

it('does not offer resume for an unpaused queue after an operation fails', () => {
  render(<QueuePanel state={{ ...state, pause_reason: null }} busy={false} error="操作失败" errorSource="change" onUpdate={vi.fn()} onReload={vi.fn()} />)
  expect(screen.getByRole('button', { name: Q.pause })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: Q.resume })).toBeNull()
})
