import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CommandCompletions, CompletionOrigin } from '../chat/CommandCompletions'
import type { CommandCompletionEvent } from '../../api/backgroundEvents'
afterEach(cleanup)
const event = (over: Partial<CommandCompletionEvent> = {}): CommandCompletionEvent => ({ sequence: 1, event_id: 'e1', command_session_id: 'c1', owner_session_id: 's1', owner_run_id: 'r1', status: 'exited', exit_code: 7, ended_at: 1, delivery_state: 'delivered', delivered_run_id: 'r2', payload: { command: 'python report.py' }, ...over })
it('退出码与结果交付分开，不把非零退出或模型接续当任务成功', () => {
  const open = vi.fn()
  render(<CommandCompletions events={[event(), event({ event_id: 'e2', command_session_id: 'c2', status: 'cancelled', exit_code: null, delivery_state: 'suppressed' })]} error={null} onRetry={vi.fn()} onOpen={open} />)
  expect(screen.getByText('退出码 7')).toBeInTheDocument()
  expect(screen.getByText('结果已交给 Agent')).toBeInTheDocument()
  expect(screen.getByText('已停止')).toBeInTheDocument()
  expect(screen.getByText('未自动接续')).toBeInTheDocument()
  expect(screen.queryByText('任务完成')).toBeNull()
  fireEvent.click(screen.getAllByRole('button', { name: '查看日志' })[0])
  expect(open).toHaveBeenCalledWith('c1')
})
it('内部回答来源只有轻量说明及实际命令日志入口', () => {
  const open = vi.fn()
  render(<CompletionOrigin commandIds={['c1']} onOpen={open} />)
  expect(screen.getByText('命令结束后继续')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '命令日志' }))
  expect(open).toHaveBeenCalledWith('c1')
})
it('连接故障导致接续未创建时照实展示原因并提供明确核对入口', () => {
  const review = vi.fn()
  render(<CommandCompletions events={[event({ delivery_state: 'pending', delivered_run_id: null, payload: { command: 'python stats.py', automatic_delivery_error: '模型服务当前不可用' } })]} error={null} onRetry={vi.fn()} onReview={review} />)
  expect(screen.getByText('命令结果已保存，自动接续暂未开始')).toBeInTheDocument()
  expect(screen.getByText('模型服务当前不可用')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '核对命令结果' }))
  expect(review).toHaveBeenCalledWith('c1')
  expect(screen.queryByText('等待 Agent 接续')).toBeNull()
})
