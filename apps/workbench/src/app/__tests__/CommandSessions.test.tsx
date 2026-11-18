import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { AgentActivity } from '../../api/agent'
import { actionWords } from '../lib/words'
import { agentFileEffects } from '../lib/agentFileEffects'
import { stepRows } from '../chat/stepRows'
import { StepList } from '../chat/Steps'
import { AgentMessageView, LiveTurn, ProcessBeforeUser } from '../chat/Message'
import type { AgentMessage } from '../../api/agent'

afterEach(cleanup)
const events = (tool: string, action: string, status: string, exitCode: number | null = null, id = 't1'): AgentActivity[] => [
  { activity_id: `${id}-call`, run_id: 'r1', session_id: 's1', sequence: 1, kind: 'tool_call', created_at: '2026-10-11T00:00:00Z',
    payload: { tool_call_id: id, tool_name: tool, arguments: { action, command: tool === 'computer_run_command' ? 'python -u job.py' : undefined } } },
  { activity_id: `${id}-result`, run_id: 'r1', session_id: 's1', sequence: 2, kind: 'tool_result', created_at: '2026-10-11T00:00:00.020Z',
    payload: { tool_call_id: id, status: 'succeeded', output: { schema: 'zhimeng.tool-result.command-session.v1', session_id: 'cmd-one',
      command: 'python -u job.py', status, exit_code: exitCode, stdout: 'Ready> ', stderr: '', stdout_file: 'C:/state/command/output.log', result_revision: 9 } } },
] as unknown as AgentActivity[]

it('a yielded command is visibly running in the background rather than completed', () => {
  const activities = events('computer_run_command', '', 'running')
  render(<StepList rows={stepRows(activities, null)} />)
  expect(screen.getByText('后台运行')).toBeVisible()
  expect(screen.queryByText('完成')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /python -u job.py/ }))
  expect(screen.getByText(/本次读取时命令仍在运行/)).toHaveTextContent('Ready>')
  expect(screen.getByText(/完整输出/)).toHaveTextContent('cmd-one')
})

it.each([
  ['running', null, '后台运行'], ['exited', 0, '已退出 · 0'], ['exited', 7, '已退出 · 7'],
  ['cancelled', null, '已停止'], ['interrupted', null, '已中断'],
] as const)('a session poll exposes actual process outcome %s/%s', (status, code, label) => {
  render(<StepList rows={stepRows(events('command_session', 'read', status, code), null)} />)
  expect(screen.getByText(label)).toBeVisible()
  expect(screen.getByRole('button', { name: '读取命令输出' })).toBeInTheDocument()
})

it('incremental reads keep their own output instead of merging distinct snapshots', () => {
  const first = events('command_session', 'read', 'running')
  const second = events('command_session', 'read', 'exited', 0, 't2').map((item) => ({ ...item, sequence: item.sequence + 2 }))
  expect(actionWords([...first, ...second], null)).toHaveLength(2)
})

it('session input and completion refresh files but do not fabricate a reversible change card', () => {
  const effects = agentFileEffects(events('command_session', 'input', 'exited', 0), 'r1')
  expect(effects.commandRan).toBe(true)
  expect(effects.refreshes).toHaveLength(1)
  expect(effects.writes).toEqual([])
})

it.each(['live', 'history', 'steering'])('opens the command identified by the actual result from %s process', (mode) => {
  const onShowCommands = vi.fn()
  const activities = events('computer_run_command', '', 'exited', 0)
  const message = { message_id: 'm1', session_id: 's1', run_id: 'r1', content: '', role: mode === 'steering' ? 'user' : 'assistant' } as AgentMessage
  if (mode === 'live') render(<LiveTurn activities={activities} runId="r1" startedAt={null} streaming="" snapshot={null} onShowCommands={onShowCommands} />)
  else if (mode === 'history') {
    render(<AgentMessageView message={message} activities={activities} stepsUnloaded={false} onLoadSteps={async () => {}}
      snapshot={null} cards={null} actions={{ onCopy: vi.fn() }} isLatest={false} onShowCommands={onShowCommands} />)
    fireEvent.click(screen.getByRole('button', { name: '查看处理过程' }))
  } else {
    const boundary = { ...activities[0], sequence: 3, kind: 'message', payload: { role: 'user', phase: 'completed', message_id: 'm1' } } as AgentActivity
    render(<ProcessBeforeUser message={message} activities={[...activities, boundary]} snapshot={null} onShowCommands={onShowCommands} />)
  }
  fireEvent.click(screen.getByRole('button', { name: '查看命令输出' }))
  expect(onShowCommands).toHaveBeenCalledExactlyOnceWith('cmd-one')
})

it('does not invent a log link for missing sessions or synchronous commands', () => {
  const activity = events('command_session', 'read', 'running')[0]
  render(<StepList rows={stepRows([activity], null)} onShowCommands={vi.fn()} />)
  expect(screen.queryByRole('button', { name: '查看命令输出' })).toBeNull()
})
