import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getAgentRun, type AgentRun } from '../../api/agent'
import { ChangeSource } from '../panel/ChangeSource'

vi.mock('../../api/agent', () => ({ getAgentRun: vi.fn() }))
const run = { run_id: 'run-a', session_id: 'chat-a', project_revision: { project_id: 'project-a' }, objective: '读取 source.csv，保存逐项明细和总额到 report.md。' } as AgentRun
beforeEach(() => { vi.clearAllMocks(); vi.mocked(getAgentRun).mockResolvedValue(run) })
afterEach(cleanup)
const open = () => fireEvent.click(screen.getByText('来源任务'))

it('展开才读取任务，初始要求独立于修改说明', async () => {
  render(<ChangeSource runId="run-a" projectId="project-a" />)
  expect(getAgentRun).not.toHaveBeenCalled()
  open()
  expect(await screen.findByText(run.objective)).toBeInTheDocument()
  expect(screen.getByText('初始要求')).toBeInTheDocument()
})

it('错误显示原因并支持重试；来源不属于当前对话时拒绝显示', async () => {
  vi.mocked(getAgentRun).mockResolvedValue({ ...run, session_id: 'another-chat' })
  render(<ChangeSource runId="run-a" sessionId="chat-a" />)
  open()
  expect(await screen.findByRole('alert')).toHaveTextContent('来源任务不属于')
  expect(screen.queryByText(run.objective)).toBeNull()
  vi.mocked(getAgentRun).mockResolvedValue(run)
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByText(run.objective)).toBeInTheDocument()
})

it('切换来源取消旧读取，晚到结果不覆盖新任务', async () => {
  let finish!: (value: AgentRun) => void
  vi.mocked(getAgentRun).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
  const view = render(<ChangeSource runId="run-a" sessionId="chat-a" />)
  open()
  await waitFor(() => expect(getAgentRun).toHaveBeenCalledOnce())
  const signal = vi.mocked(getAgentRun).mock.calls[0][1]?.signal
  view.rerender(<ChangeSource runId="run-b" sessionId="chat-b" />)
  expect(signal?.aborted).toBe(true)
  vi.mocked(getAgentRun).mockResolvedValue({ ...run, run_id: 'run-b', session_id: 'chat-b', objective: '整理会议记录' })
  open()
  await screen.findByText('整理会议记录')
  await act(async () => finish(run))
  expect(screen.queryByText(run.objective)).toBeNull()
  expect(screen.getByText('整理会议记录')).toBeInTheDocument()
})

it('没有来源编号不伪造任务入口', () => {
  const { container } = render(<ChangeSource runId={null} />)
  expect(container).toBeEmptyDOMElement()
})
